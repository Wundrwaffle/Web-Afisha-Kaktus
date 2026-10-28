import sqlite3

from fastapi.testclient import TestClient

from app.captcha import CaptchaConfig, CaptchaVerifier
from app.main import create_app


class StubVerifier:
    """Заглушка вместо запроса к Яндексу: запоминает аргументы, отдаёт заданный ответ."""

    def __init__(self, result: bool = True):
        self.result = result
        self.calls: list[tuple[str | None, str | None]] = []

    def verify(self, token, ip=None):
        self.calls.append((token, ip))
        return self.result


def _app(tmp_path, *, captcha=True, verifier=None, name="captcha"):
    config = CaptchaConfig(client_key="ysc1_test", server_key="ysc2_test") if captcha else None
    return create_app(
        f"sqlite:///{tmp_path / (name + '.sqlite3')}",
        captcha_config=config,
        captcha_verifier=verifier,
    )


def _register(client, captcha_token=None, email="visitor@example.com"):
    body = {"email": email, "password": "password123", "full_name": "Тест Тестов"}
    if captcha_token is not None:
        body["captcha_token"] = captcha_token
    return client.post("/api/v1/auth/register", json=body)


def test_captcha_disabled_without_keys(tmp_path):
    """Без ключей регистрация работает как раньше — dev, тесты и показ не ломаются."""
    app = create_app(f"sqlite:///{tmp_path / 'off.sqlite3'}")
    with TestClient(app) as client:
        config = client.get("/api/v1/auth/captcha")
        response = _register(client)

    assert config.status_code == 200
    assert config.json() == {"enabled": False, "client_key": ""}
    assert response.status_code == 201


def test_captcha_config_exposes_client_key_only(tmp_path):
    verifier = StubVerifier(True)
    app = _app(tmp_path, verifier=verifier)
    with TestClient(app) as client:
        config = client.get("/api/v1/auth/captcha").json()

    assert config == {"enabled": True, "client_key": "ysc1_test"}
    # Ключ сервера наружу не отдаём ни в каком виде.
    assert "ysc2_test" not in str(config)


def test_register_requires_passed_captcha(tmp_path):
    verifier = StubVerifier(False)
    app = _app(tmp_path, verifier=verifier)
    with TestClient(app) as client:
        response = _register(client, captcha_token="bad-token")
        verifier.result = True  # вход проверяем отдельно от отказа регистрации
        login = client.post(
            "/api/v1/auth/login",
            json={
                "email": "visitor@example.com",
                "password": "password123",
                "captcha_token": "token",
            },
        )

    assert response.status_code == 400
    assert "робот" in response.json()["detail"]
    # Первый вызов — регистрация с плохим токеном, второй — вход с хорошим.
    assert verifier.calls == [("bad-token", "testclient"), ("token", "testclient")]
    # Аккаунт не создан: без прохождения проверки регистрация не доходит до БД.
    assert login.status_code == 401


def test_register_rejects_missing_captcha_token(tmp_path):
    verifier = StubVerifier(True)
    app = _app(tmp_path, verifier=verifier)
    with TestClient(app) as client:
        missing = _register(client)
        empty = _register(client, captcha_token="   ", email="other@example.com")

    assert missing.status_code == 400
    assert empty.status_code == 400
    assert verifier.calls == []


def test_register_creates_account_after_passed_captcha(tmp_path):
    verifier = StubVerifier(True)
    app = _app(tmp_path, verifier=verifier)
    with TestClient(app) as client:
        response = _register(client, captcha_token="good-token")
        login = client.post(
            "/api/v1/auth/login",
            json={
                "email": "visitor@example.com",
                "password": "password123",
                "captcha_token": "token",
            },
        )

    assert response.status_code == 201
    assert response.json()["role"] == "visitor"
    assert verifier.calls == [
        ("good-token", "testclient"),
        ("token", "testclient"),
    ]
    assert login.status_code == 200


def test_verifier_maps_provider_status(monkeypatch):
    verifier = CaptchaVerifier("ysc2_test")
    payloads = []

    def fake_post(payload):
        payloads.append(payload)
        return {"status": "ok", "message": "", "host": "чотамзавтра.рф"}

    monkeypatch.setattr(verifier, "_post", fake_post)
    assert verifier.verify("token", "203.0.113.7") is True
    assert payloads == [{"secret": "ysc2_test", "token": "token", "ip": "203.0.113.7"}]

    monkeypatch.setattr(verifier, "_post", lambda payload: {"status": "failed", "message": ""})
    assert verifier.verify("token") is False


def test_verifier_skips_network_for_empty_token(monkeypatch):
    verifier = CaptchaVerifier("ysc2_test")

    def explode(payload):
        raise AssertionError("без токена в сеть ходить нельзя")

    monkeypatch.setattr(verifier, "_post", explode)
    assert verifier.verify(None) is False
    assert verifier.verify("  ") is False


def test_verifier_fails_open_on_provider_error(monkeypatch):
    """Сбой Яндекса не должен останавливать регистрацию (рекомендация документации)."""
    verifier = CaptchaVerifier("ysc2_test")

    def boom(payload):
        raise OSError("сеть недоступна")

    monkeypatch.setattr(verifier, "_post", boom)
    assert verifier.verify("token") is True


def test_public_mode_csp_allows_smartcaptcha_only_when_enabled(tmp_path):
    with_smartcaptcha = create_app(
        f"sqlite:///{tmp_path / 'csp_on.sqlite3'}",
        public_mode=True,
        captcha_config=CaptchaConfig(client_key="ysc1_test", server_key="ysc2_test"),
    )
    without = create_app(
        f"sqlite:///{tmp_path / 'csp_off.sqlite3'}",
        public_mode=True,
        captcha_config=CaptchaConfig(),
    )
    with TestClient(with_smartcaptcha) as client:
        csp_on = client.get("/api/v1/health").headers["content-security-policy"]
    with TestClient(without) as client:
        csp_off = client.get("/api/v1/health").headers["content-security-policy"]

    assert "script-src 'self' 'unsafe-inline' https://smartcaptcha.cloud.yandex.ru" in csp_on
    assert "frame-src https://smartcaptcha.cloud.yandex.ru" in csp_on
    assert "smartcaptcha" not in csp_off


def _promote_to_organizer(db_path, email="visitor@example.com"):
    """Роль меняем прямо в БД: через API это делает только админ."""
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("UPDATE users SET role = 'organizer' WHERE email = ?", (email,))
        connection.commit()
    finally:
        connection.close()


EVENT_PAYLOAD = {
    "title": "Город звучит",
    "slug": "gorod-zvuchit-captcha",
    "category": "Музыка",
    "date": "2030-05-01",
    "time": "18:00",
    "venue": "Парк Гагарина",
    "price": "Бесплатно",
}


def _event_client(tmp_path, result=True):
    """Приложение с включённой капчей и готовым организатором."""
    verifier = StubVerifier(result)
    db_path = tmp_path / "events.sqlite3"
    app = create_app(
        f"sqlite:///{db_path}",
        captcha_config=CaptchaConfig(client_key="ysc1_test", server_key="ysc2_test"),
        captcha_verifier=verifier,
    )
    client = TestClient(app)
    _register(client, captcha_token="token")
    _promote_to_organizer(db_path)
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "visitor@example.com", "password": "password123", "captcha_token": "token"},
    )
    return client, verifier, login.json()["access_token"]


def test_login_requires_captcha(tmp_path):
    verifier = StubVerifier(True)
    app = _app(tmp_path, verifier=verifier)
    with TestClient(app) as client:
        _register(client, captcha_token="token")
        verifier.result = False
        denied = client.post(
            "/api/v1/auth/login",
            json={"email": "visitor@example.com", "password": "password123", "captcha_token": "bad"},
        )
        verifier.result = True
        no_token = client.post(
            "/api/v1/auth/login",
            json={"email": "visitor@example.com", "password": "password123"},
        )
        allowed = client.post(
            "/api/v1/auth/login",
            json={"email": "visitor@example.com", "password": "password123", "captcha_token": "token"},
        )

    assert denied.status_code == 400
    assert no_token.status_code == 400
    assert allowed.status_code == 200
    assert allowed.json()["access_token"]


def test_event_create_and_update_require_captcha(tmp_path):
    client, verifier, token = _event_client(tmp_path)
    headers = {"X-Auth-Token": token}

    no_token = client.post("/api/v1/events", json=EVENT_PAYLOAD, headers=headers)
    verifier.result = False
    bad_token = client.post(
        "/api/v1/events", json={**EVENT_PAYLOAD, "captcha_token": "bad"}, headers=headers
    )
    verifier.result = True
    created = client.post(
        "/api/v1/events", json={**EVENT_PAYLOAD, "captcha_token": "ok"}, headers=headers
    )
    event_id = created.json()["event_id"]

    edit_denied = client.patch(
        f"/api/v1/me/events/{event_id}", json={"title": "Новое имя"}, headers=headers
    )
    edit_ok = client.patch(
        f"/api/v1/me/events/{event_id}",
        json={"title": "Новое имя", "captcha_token": "ok"},
        headers=headers,
    )

    assert no_token.status_code == 400
    assert bad_token.status_code == 400
    assert created.status_code == 201
    assert edit_denied.status_code == 400
    assert edit_ok.status_code == 200
    assert edit_ok.json()["title"] == "Новое имя"


def test_captcha_token_never_reaches_event_row(tmp_path):
    """Токен проверки — не поле модели: exclude=True, иначе insert упадёт."""
    client, verifier, token = _event_client(tmp_path)
    created = client.post(
        "/api/v1/events",
        json={**EVENT_PAYLOAD, "captcha_token": "ok"},
        headers={"X-Auth-Token": token},
    )
    assert created.status_code == 201
