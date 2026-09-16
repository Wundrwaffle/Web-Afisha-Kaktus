"""Тесты публичного режима: гейт, заголовки безопасности, лимиты, раздача статики.

Проверяем именно то, что защищает приложение при доступе из внешней сети:
- гейт по HTTP Basic закрывает весь сайт (включая админку и регистрацию);
- заголовки безопасности стоят на ответах, в том числе на ответе гейта;
- подбор пароля упирается в 429, успешный вход счётчик по аккаунту сбрасывает;
- в публичном режиме Swagger/OpenAPI и кросс-доменный доступ закрыты;
- фронтенд отдаётся тем же приложением (один origin для публичной ссылки).
"""
import base64

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

GATE_USER = "tester"
GATE_PASSWORD = "gate-secret"


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'exposure.sqlite3'}"


def _basic(user: str = GATE_USER, password: str = GATE_PASSWORD) -> dict[str, str]:
    encoded = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def _register(client, email="visitor@example.com", password="password123"):
    return client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Тест Тестов"},
    )


def _login(client, email="visitor@example.com", password="password123", headers=None):
    return client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
        headers=headers or {},
    )


def test_gate_requires_credentials(tmp_path):
    app = create_app(
        f"sqlite:///{tmp_path / 'gate.sqlite3'}",
        gate_user=GATE_USER,
        gate_password=GATE_PASSWORD,
    )
    with TestClient(app) as client:
        anonymous = client.get("/api/v1/events")
        wrong = client.get("/api/v1/events", headers=_basic(password="nope"))
        allowed = client.get("/api/v1/events", headers=_basic())

    assert anonymous.status_code == 401
    assert "Basic" in anonymous.headers["WWW-Authenticate"]
    assert wrong.status_code == 401
    assert allowed.status_code == 200


def test_gate_covers_the_whole_site_but_not_health(db_url):
    app = create_app(db_url, gate_user=GATE_USER, gate_password=GATE_PASSWORD, serve_static=True)
    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        site = client.get("/")
        admin = client.get("/api/v1/admin/users")

    assert health.status_code == 200
    assert site.status_code == 401
    assert admin.status_code == 401


def test_gate_allows_valid_credentials_with_non_ascii_password(db_url):
    """Пароль с не-ASCII и пробелами не должен ломать разбор Basic-заголовка."""
    password = "пароль с пробелами"
    app = create_app(db_url, gate_user="кирилл", gate_password=password)
    with TestClient(app) as client:
        allowed = client.get("/api/v1/events", headers=_basic("кирилл", password))
        denied = client.get("/api/v1/events", headers=_basic("кирилл", "пароль с пробелами "))

    assert allowed.status_code == 200
    assert denied.status_code == 401


def test_gate_rejects_malformed_authorization_header(db_url):
    app = create_app(db_url, gate_user=GATE_USER, gate_password=GATE_PASSWORD)
    with TestClient(app) as client:
        not_base64 = client.get("/api/v1/events", headers={"Authorization": "Basic not-base64!"})
        empty = client.get("/api/v1/events", headers={"Authorization": "Basic"})
        wrong_scheme = client.get("/api/v1/events", headers={"Authorization": "Bearer abc"})
        no_colon = client.get(
            "/api/v1/events",
            headers={"Authorization": "Basic " + base64.b64encode(b"onlyuser").decode()},
        )

    assert [r.status_code for r in (not_base64, empty, wrong_scheme, no_colon)] == [401] * 4


def test_app_401_has_no_www_authenticate_challenge(db_url):
    """401 от приложения не должен звать браузер на диалог входа.

    Chrome на незнакомую схему в WWW-Authenticate показывает окно Basic, а
    пароль гейта такой 401 не снимает — получается петля всплывающих окон.
    """
    app = create_app(db_url, gate_user=GATE_USER, gate_password=GATE_PASSWORD)
    with TestClient(app) as client:
        without_token = client.get("/api/v1/auth/me", headers=_basic())
        bad_token = client.get(
            "/api/v1/auth/me", headers={**_basic(), "X-Auth-Token": "not-a-token"}
        )

    assert without_token.status_code == 401
    assert "WWW-Authenticate" not in without_token.headers
    assert bad_token.status_code == 401
    assert "WWW-Authenticate" not in bad_token.headers


def test_gate_keeps_its_basic_challenge(db_url):
    """Челлендж гейта обязан остаться: без него браузер не спросит пароль."""
    app = create_app(db_url, gate_user=GATE_USER, gate_password=GATE_PASSWORD)
    with TestClient(app) as client:
        denied = client.get("/api/v1/events")

    assert denied.status_code == 401
    assert denied.headers["WWW-Authenticate"].startswith("Basic")


def test_application_token_comes_in_x_auth_token(db_url):
    """Токен приложения читается из X-Auth-Token, когда урок уже занят гейтом.

    Под Basic-гейтом в Authorization лежит пароль доступа, и браузер подставляет
    его сам. Если SPA положит туда Bearer, гейт своих кредов не увидит и вход в
    кабинет сломается — поэтому основным заголовком стал X-Auth-Token.
    """
    app = create_app(db_url, gate_user=GATE_USER, gate_password=GATE_PASSWORD)
    with TestClient(app) as client:
        client.post(
            "/api/v1/auth/register",
            json={"email": "visitor@example.com", "password": "password123", "full_name": "Тест"},
            headers=_basic(),
        )
        token = _login(client, headers=_basic()).json()["access_token"]

        via_api_key = client.get("/api/v1/auth/me", headers={**_basic(), "X-Auth-Token": token})
        bearer_under_gate = client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
        )

    assert via_api_key.status_code == 200
    assert via_api_key.json()["email"] == "visitor@example.com"
    # Bearer в Authorization до приложения не доходит: заголовок читает гейт, и
    # браузер не может подставить туда пароль доступа. Отсюда переезд на X-Auth-Token.
    assert bearer_under_gate.status_code == 401
    assert bearer_under_gate.headers["WWW-Authenticate"].startswith("Basic")


def test_bearer_still_works_without_gate(db_url):
    """Локальная разработка, Swagger и старые клиенты продолжают жить на Bearer."""
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client)
        token = _login(client).json()["access_token"]
        response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200


def test_html_shell_is_not_cached(db_url):
    """Оболочку SPA не кешируем: у товарища иначе остаётся старый фронтенд."""
    app = create_app(db_url, serve_static=True)
    with TestClient(app) as client:
        shell = client.get("/")
        api = client.get("/api/v1/events")

    assert shell.headers["content-type"].startswith("text/html")
    assert shell.headers["Cache-Control"] == "no-cache"
    assert api.headers.get("Cache-Control") != "no-cache"


def test_gate_is_off_by_default(db_url):
    """Локальная разработка и тесты работают без гейта."""
    app = create_app(db_url)
    with TestClient(app) as client:
        response = client.get("/api/v1/events")

    assert response.status_code == 200


def test_security_headers_on_api_and_gate_responses(db_url):
    app = create_app(db_url, gate_user=GATE_USER, gate_password=GATE_PASSWORD)
    with TestClient(app) as client:
        api = client.get("/api/v1/events", headers=_basic())
        gated = client.get("/api/v1/events")

    for response in (api, gated):
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "no-referrer"


def test_hsts_only_behind_https_proxy(db_url):
    app = create_app(db_url)
    with TestClient(app) as client:
        tunnel = client.get("/api/v1/health", headers={"X-Forwarded-Proto": "https"})
        direct = client.get("/api/v1/health")

    assert "max-age" in tunnel.headers["Strict-Transport-Security"]
    assert "Strict-Transport-Security" not in direct.headers


def test_public_mode_hides_docs_and_blocks_cross_origin(db_url):
    app = create_app(db_url, public_mode=True)
    with TestClient(app) as client:
        docs = client.get("/docs")
        openapi = client.get("/openapi.json")
        cross_origin = client.get("/api/v1/events", headers={"Origin": "http://evil.example"})

    assert docs.status_code == 404
    assert openapi.status_code == 404
    assert "access-control-allow-origin" not in cross_origin.headers


def test_dev_mode_keeps_docs_and_local_origin(db_url):
    app = create_app(db_url)
    with TestClient(app) as client:
        docs = client.get("/docs")
        local = client.get(
            "/api/v1/events", headers={"Origin": "http://127.0.0.1:4173"}
        )

    assert docs.status_code == 200
    assert local.headers["access-control-allow-origin"] == "http://127.0.0.1:4173"


def test_login_rate_limit_locks_password_guessing(db_url):
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client)
        failures = [_login(client, password="wrong-password").status_code for _ in range(10)]
        blocked = _login(client, password="wrong-password")
        # Даже правильный пароль уже не принимается: окно лимита не истекло.
        still_blocked = _login(client)

    assert failures == [401] * 10
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"]
    assert still_blocked.status_code == 429


def test_login_rate_limit_resets_on_success(db_url):
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client)
        for _ in range(5):
            _login(client, password="wrong-password")
        success = _login(client)
        after_success = _login(client, password="wrong-password")

    assert success.status_code == 200
    # После успешного входа счётчик по аккаунту обнулён, окно начинается заново.
    assert after_success.status_code == 401


def test_login_rate_limit_is_per_account_not_global(db_url):
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, email="first@example.com")
        _register(client, email="second@example.com")
        for _ in range(10):
            _login(client, email="first@example.com", password="wrong-password")
        first = _login(client, email="first@example.com", password="wrong-password")
        second = _login(client, email="second@example.com")

    assert first.status_code == 429
    assert second.status_code == 200


def test_register_rate_limit(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'reg.sqlite3'}")
    with TestClient(app) as client:
        codes = [
            _register(client, email=f"user{index}@example.com").status_code
            for index in range(21)
        ]

    assert codes[:20] == [201] * 20
    assert codes[20] == 429


def test_public_mode_serves_frontend_from_api_origin(db_url):
    app = create_app(db_url, serve_static=True, public_mode=True)
    with TestClient(app) as client:
        page = client.get("/")
        missing_api = client.get("/api/v1/unknown-route")

    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "<html" in page.text.lower()
    # Публикация схемы API закрыта, но сами маршруты работают.
    assert missing_api.status_code in (404, 405)


def test_static_disabled_by_default(db_url):
    app = create_app(db_url)
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 404


def test_client_ip_prefers_proxy_headers():
    from app.exposure import client_ip

    class _Request:
        def __init__(self, headers, host="127.0.0.1"):
            self.headers = headers
            self.client = type("C", (), {"host": host})()

    assert client_ip(_Request({"cf-connecting-ip": "203.0.113.7"})) == "203.0.113.7"
    assert client_ip(_Request({"x-forwarded-for": "203.0.113.7, 10.0.0.1"})) == "203.0.113.7"
    assert client_ip(_Request({})) == "127.0.0.1"
