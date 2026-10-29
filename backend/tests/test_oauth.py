"""Вход через Яндекс ID: поток, безопасность и апсерт аккаунта.

Провайдер в тестах — заглушка (`StubProvider`): сеть к Яндексу не нужна, поэтому
набор работает без реальных ключей и без домена. Проверяем именно то, что можно
сломать правкой кода: сверку state, одноразовость handoff, привязку по почте,
запрет входа по паролю для OAuth-аккаунта и утечку client_secret.
"""

import sqlite3
from urllib.parse import parse_qsl, urlparse

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import User
from app.oauth import (
    HandoffStore,
    OAuthConfig,
    OAuthError,
    OAuthProfile,
    OAuthProvider,
    YandexProvider,
    build_providers,
    config_from_env,
    upsert_oauth_user,
)

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"

DEFAULT_PROFILE = OAuthProfile(
    provider_user_id="1000034426",
    email="yandex@example.com",
    email_verified=True,
    display_name="Иван Яндексов",
    avatar_url="https://avatars.yandex.net/get-yapic/123/islands-200",
)


class StubProvider(OAuthProvider):
    """Тот же интерфейс, что у реального адаптера, но без сети."""

    id = "yandex"
    title = "Яндекс"

    def __init__(self, profile=None, *, exchange_error=None):
        super().__init__(CLIENT_ID, CLIENT_SECRET)
        self.profile = profile or DEFAULT_PROFILE
        self.exchange_error = exchange_error
        self.calls: list[tuple] = []

    def exchange_code(self, *, code, redirect_uri, code_verifier=""):
        self.calls.append(("exchange", code, redirect_uri, code_verifier))
        if self.exchange_error:
            raise OAuthError(self.exchange_error)
        return f"yandex-token-for-{code}"

    def fetch_profile(self, access_token):
        self.calls.append(("profile", access_token))
        return self.profile


def _config() -> OAuthConfig:
    return OAuthConfig(yandex_client_id=CLIENT_ID, yandex_client_secret=CLIENT_SECRET)


def _app(tmp_path, *, config=None, providers=None, gate_password=None, name="oauth"):
    return create_app(
        f"sqlite:///{tmp_path / (name + '.sqlite3')}",
        oauth_config=config if config is not None else _config(),
        oauth_providers=providers,
        gate_password=gate_password,
    )


def _start(client):
    response = client.get("/api/v1/auth/oauth/yandex/start", follow_redirects=False)
    query = dict(parse_qsl(urlparse(response.headers["location"]).query))
    return response, query


def _callback(client, state, code="auth-code"):
    return client.get(
        f"/api/v1/auth/oauth/yandex/callback?code={code}&state={state}",
        follow_redirects=False,
    )


def _redirect_params(response):
    return dict(parse_qsl(urlparse(response.headers["location"]).query))


def _db_users(db_path):
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM users").fetchall()]
    finally:
        connection.close()


def _db_identities(db_path):
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM identities").fetchall()]
    finally:
        connection.close()


def _register(client, email="local@example.com", password="password123"):
    return client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Локальный Человек"},
    )


def _oauth_login(client, provider):
    """Один полный вход через заглушку провайдера."""
    _, query = _start(client)
    response = _callback(client, query["state"])
    params = _redirect_params(response)
    assert params["oauth"] == "ok"
    exchange = client.post(
        "/api/v1/auth/oauth/exchange", json={"handoff": params["handoff"]}
    )
    return response, exchange


# --- Конфигурация -----------------------------------------------------------


def test_providers_empty_without_keys(tmp_path):
    app = _app(tmp_path, config=OAuthConfig())
    with TestClient(app) as client:
        assert client.get("/api/v1/auth/oauth/providers").json() == []
        # И start для выключенного провайдера — 404, а не кривой редирект.
        assert (
            client.get("/api/v1/auth/oauth/yandex/start", follow_redirects=False).status_code
            == 404
        )


def test_providers_lists_yandex_with_keys(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/v1/auth/oauth/providers").json() == [
            {"id": "yandex", "title": "Яндекс"}
        ]


def test_half_configured_provider_is_not_offered():
    """Только client_id без секрета — это недонастроенный вход, кнопки нет."""
    assert build_providers(OAuthConfig(yandex_client_id="id")) == {}
    assert set(build_providers(_config())) == {"yandex"}


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("AFISHA_OAUTH_YANDEX_CLIENT_ID", " env-id ")
    monkeypatch.setenv("AFISHA_OAUTH_YANDEX_CLIENT_SECRET", "env-secret")
    monkeypatch.setenv("AFISHA_OAUTH_REDIRECT_BASE", "https://example.com/")
    config = config_from_env()
    assert config.yandex_enabled is True
    assert config.yandex_client_id == "env-id"
    assert config.redirect_base == "https://example.com"


def test_providers_hidden_under_basic_gate(tmp_path):
    """Публичный показ под гейтом: после возврата гость упёрся бы в пароль гейта."""
    app = _app(tmp_path, gate_password="gate-pass")
    with TestClient(app) as client:
        anonymous = client.get("/api/v1/auth/oauth/providers")
        allowed = client.get(
            "/api/v1/auth/oauth/providers", auth=("afisha", "gate-pass")
        )
    assert anonymous.status_code == 401
    assert allowed.json() == []


# --- Старт и колбэк ---------------------------------------------------------


def test_start_sets_state_cookie_and_pkce(tmp_path):
    app = _app(tmp_path, providers={"yandex": StubProvider()})
    with TestClient(app) as client:
        response, query = _start(client)

    assert response.status_code == 302
    assert query["client_id"] == CLIENT_ID
    assert query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"]
    assert query["state"]
    assert query["redirect_uri"].endswith("/api/v1/auth/oauth/yandex/callback")
    cookie = response.headers["set-cookie"].lower()
    # Кука HttpOnly и с узким путём: JS её не читает, на другие запросы не таскается.
    assert "httponly" in cookie
    assert "path=/api/v1/auth/oauth" in cookie
    assert "samesite=lax" in cookie
    # Секрет приложения в браузер не попадает.
    assert CLIENT_SECRET not in response.headers["location"]


def test_callback_creates_visitor_and_returns_handoff(tmp_path):
    provider = StubProvider()
    app = _app(tmp_path, providers={"yandex": provider})
    db_path = tmp_path / "oauth.sqlite3"
    with TestClient(app) as client:
        _, query = _start(client)
        response = _callback(client, query["state"])
        params = _redirect_params(response)
        exchange = client.post(
            "/api/v1/auth/oauth/exchange", json={"handoff": params["handoff"]}
        )
        me = client.get(
            "/api/v1/auth/me",
            headers={"X-Auth-Token": exchange.json()["access_token"]},
        )

    assert response.status_code == 302
    assert params["oauth"] == "ok"
    # Код обмена одноразовый — в URL только он, токенов там нет.
    assert "refresh_token" not in params
    assert exchange.status_code == 200
    assert exchange.json()["refresh_token"]
    assert me.status_code == 200
    assert me.json()["role"] == "visitor"
    assert me.json()["full_name"] == "Иван Яндексов"
    assert me.json()["identities"] == ["yandex"]

    users = _db_users(db_path)
    assert len(users) == 1
    assert users[0]["email"] == "yandex@example.com"
    assert users[0]["role"] == "visitor"
    identities = _db_identities(db_path)
    assert len(identities) == 1
    assert identities[0]["provider"] == "yandex"
    assert identities[0]["provider_user_id"] == "1000034426"
    assert identities[0]["email_verified"] == 1


def test_second_login_returns_same_account(tmp_path):
    provider = StubProvider()
    app = _app(tmp_path, providers={"yandex": provider})
    db_path = tmp_path / "oauth.sqlite3"
    with TestClient(app) as client:
        _, first = _oauth_login(client, provider)
        first_me = client.get(
            "/api/v1/auth/me",
            headers={"X-Auth-Token": first.json()["access_token"]},
        )
        _, second = _oauth_login(client, provider)
        second_me = client.get(
            "/api/v1/auth/me",
            headers={"X-Auth-Token": second.json()["access_token"]},
        )

    assert first_me.json()["id"] == second_me.json()["id"]
    assert len(_db_users(db_path)) == 1
    assert len(_db_identities(db_path)) == 1


def test_callback_rejects_foreign_state(tmp_path):
    provider = StubProvider()
    app = _app(tmp_path, providers={"yandex": provider})
    db_path = tmp_path / "oauth.sqlite3"
    with TestClient(app) as client:
        _start(client)
        response = _callback(client, "not-the-state-from-cookie")

    assert response.status_code == 400
    # Аккаунт не создан и код у провайдера не обменивался.
    assert _db_users(db_path) == []
    assert provider.calls == []


def test_callback_requires_state_cookie(tmp_path):
    app = _app(tmp_path, providers={"yandex": StubProvider()})
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/auth/oauth/yandex/callback?code=abc&state=xyz",
            follow_redirects=False,
        )
    assert response.status_code == 400


def test_access_denied_redirects_with_error(tmp_path):
    app = _app(tmp_path, providers={"yandex": StubProvider()})
    with TestClient(app) as client:
        _start(client)
        response = client.get(
            "/api/v1/auth/oauth/yandex/callback?error=access_denied&state=ignored",
            follow_redirects=False,
        )
    params = _redirect_params(response)
    assert response.status_code == 302
    assert params == {"oauth": "error", "reason": "access_denied"}


def test_provider_failure_does_not_create_account(tmp_path):
    provider = StubProvider(exchange_error="token_rejected")
    app = _app(tmp_path, providers={"yandex": provider})
    db_path = tmp_path / "oauth.sqlite3"
    with TestClient(app) as client:
        _, query = _start(client)
        response = _callback(client, query["state"])

    assert _redirect_params(response) == {"oauth": "error", "reason": "provider_failed"}
    assert _db_users(db_path) == []


# --- Одноразовый handoff ----------------------------------------------------


def test_handoff_cannot_be_reused(tmp_path):
    provider = StubProvider()
    app = _app(tmp_path, providers={"yandex": provider})
    with TestClient(app) as client:
        _, query = _start(client)
        params = _redirect_params(_callback(client, query["state"]))
        first = client.post("/api/v1/auth/oauth/exchange", json={"handoff": params["handoff"]})
        second = client.post("/api/v1/auth/oauth/exchange", json={"handoff": params["handoff"]})

    assert first.status_code == 200
    assert second.status_code == 401


def test_exchange_rejects_unknown_handoff(tmp_path):
    app = _app(tmp_path, providers={"yandex": StubProvider()})
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/oauth/exchange", json={"handoff": "never-issued-code"}
        )
    assert response.status_code == 401


def test_handoff_store_expires_and_binds_ip():
    store = HandoffStore(ttl_seconds=30)
    pair = object()
    code = store.put(pair, "203.0.113.7")
    # Чужой адрес не получает сессию даже с верным кодом.
    assert store.take(code, "198.51.100.1") is None

    code = store.put(pair, "203.0.113.7")
    assert store.take(code, "203.0.113.7") is pair

    expired = HandoffStore(ttl_seconds=0)
    code = expired.put(pair, "203.0.113.7")
    assert expired.take(code, "203.0.113.7") is None


# --- Аккаунты и пароль ------------------------------------------------------


def test_oauth_account_cannot_log_in_with_password(tmp_path):
    provider = StubProvider()
    app = _app(tmp_path, providers={"yandex": provider})
    with TestClient(app) as client:
        _, exchange = _oauth_login(client, provider)
        refresh_token = exchange.json()["refresh_token"]
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "yandex@example.com", "password": "что-угодно-123"},
        )
        refreshed = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})

    assert login.status_code == 401
    assert "внешний сервис" in login.json()["detail"]
    # Обновление сессии при этом работает: аккаунт обычный, просто без пароля.
    assert refreshed.status_code == 200


def test_confirmed_email_links_existing_account(tmp_path):
    """Почта совпала и подтверждена провайдером → привязка, второго аккаунта нет."""
    provider = StubProvider()
    app = _app(tmp_path, providers={"yandex": provider})
    db_path = tmp_path / "oauth.sqlite3"
    with TestClient(app) as client:
        _register(client, email="yandex@example.com")
        connection = sqlite3.connect(db_path)
        connection.execute("UPDATE users SET role = 'organizer' WHERE email = ?", ("yandex@example.com",))
        connection.commit()
        connection.close()

        _, exchange = _oauth_login(client, provider)
        me = client.get(
            "/api/v1/auth/me", headers={"X-Auth-Token": exchange.json()["access_token"]}
        )

    assert me.status_code == 200
    # Роль не сброшена: это тот же человек, а не новый visitor.
    assert me.json()["role"] == "organizer"
    assert me.json()["identities"] == ["yandex"]
    assert len(_db_users(db_path)) == 1
    assert len(_db_identities(db_path)) == 1


def test_unconfirmed_email_never_links_existing_account(tmp_path):
    """Неподтверждённая почта = попытка захвата чужого кабинета → отказ, не привязка."""
    profile = OAuthProfile(
        provider_user_id="999",
        email="yandex@example.com",
        email_verified=False,
        display_name="Чужой Человек",
    )
    provider = StubProvider(profile)
    app = _app(tmp_path, providers={"yandex": provider})
    db_path = tmp_path / "oauth.sqlite3"
    with TestClient(app) as client:
        _register(client, email="yandex@example.com")
        _, query = _start(client)
        response = _callback(client, query["state"])
        # Вход по паролю прежнего владельца работает как раньше.
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "yandex@example.com", "password": "password123"},
        )

    assert _redirect_params(response) == {"oauth": "error", "reason": "email_taken"}
    assert login.status_code == 200
    assert len(_db_users(db_path)) == 1
    assert _db_identities(db_path) == []


def test_account_without_email_gets_service_address(tmp_path):
    profile = OAuthProfile(provider_user_id="77", email=None, display_name="Без Почты")
    provider = StubProvider(profile)
    app = _app(tmp_path, providers={"yandex": provider})
    db_path = tmp_path / "oauth.sqlite3"
    with TestClient(app) as client:
        _, exchange = _oauth_login(client, provider)

    assert exchange.status_code == 200
    users = _db_users(db_path)
    assert len(users) == 1
    # Служебный адрес: заведомо несуществующий домен, не «почта» человека.
    assert users[0]["email"].endswith("@oauth.invalid")


def test_upsert_creates_single_identity_under_race(tmp_path):
    """Две последовательные привязки одного профиля дают одну запись, не 500."""
    from sqlalchemy import func, select

    from app.db import Base, build_engine, build_session_factory

    engine = build_engine(f"sqlite:///{tmp_path / 'race.sqlite3'}")
    Base.metadata.create_all(engine)
    session_factory = build_session_factory(engine)
    with session_factory() as session:
        first_id = upsert_oauth_user(session, "yandex", DEFAULT_PROFILE).id
    with session_factory() as session:
        second_id = upsert_oauth_user(session, "yandex", DEFAULT_PROFILE).id
    with session_factory() as session:
        users = session.scalar(select(func.count()).select_from(User))
    assert first_id == second_id
    assert users == 1
    assert len(_db_identities(tmp_path / "race.sqlite3")) == 1


# --- Реальный адаптер Яндекса ----------------------------------------------


def test_yandex_authorize_url_contains_scope_and_pkce():
    provider = YandexProvider(CLIENT_ID, CLIENT_SECRET)
    url = provider.authorize_url(
        redirect_uri="https://example.com/api/v1/auth/oauth/yandex/callback",
        state="state-1",
        code_challenge="challenge-1",
    )
    query = dict(parse_qsl(urlparse(url).query))
    assert url.startswith("https://oauth.yandex.ru/authorize?")
    assert query["scope"] == "login:email login:info login:avatar"
    assert query["state"] == "state-1"
    assert query["code_challenge"] == "challenge-1"
    assert query["code_challenge_method"] == "S256"
    assert CLIENT_SECRET not in url


def test_yandex_profile_maps_fields(monkeypatch):
    provider = YandexProvider(CLIENT_ID, CLIENT_SECRET)
    payload = {
        "id": "1000034426",
        "default_email": "Ivan@Example.COM",
        "first_name": "Иван",
        "last_name": "Иванов",
        "display_name": "ivan",
        "is_avatar_empty": False,
        "default_avatar_id": "131652443",
    }

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return payload

    monkeypatch.setattr("app.oauth.httpx.get", lambda *a, **kw: Response())
    profile = provider.fetch_profile("token")
    assert profile.provider_user_id == "1000034426"
    assert profile.email == "ivan@example.com"
    assert profile.email_verified is True
    assert profile.display_name == "Иван Иванов"
    assert profile.avatar_url == (
        "https://avatars.yandex.net/get-yapic/131652443/islands-200"
    )


def test_yandex_profile_without_email_is_unverified(monkeypatch):
    provider = YandexProvider(CLIENT_ID, CLIENT_SECRET)

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"id": "42", "login": "no-email", "is_avatar_empty": True}

    monkeypatch.setattr("app.oauth.httpx.get", lambda *a, **kw: Response())
    profile = provider.fetch_profile("token")
    assert profile.email is None
    assert profile.email_verified is False
    assert profile.avatar_url is None


def test_yandex_exchange_failure_raises_oauth_error(monkeypatch):
    provider = YandexProvider(CLIENT_ID, CLIENT_SECRET)

    class Response:
        status_code = 400

        @staticmethod
        def json():
            return {"error": "invalid_grant"}

    monkeypatch.setattr("app.oauth.httpx.post", lambda *a, **kw: Response())
    with pytest.raises(OAuthError) as error:
        provider.exchange_code(code="bad", redirect_uri="https://example.com/cb")
    assert error.value.code == "token_rejected"
