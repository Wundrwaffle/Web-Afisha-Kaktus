"""Своя капча: работает, когда внешний виджет не загрузился.

Внешняя проверка здесь всегда отвечает отказом — это и есть сценарий «скрипт
Яндекса срезал блокировщик». Задание ищем так же, как браузер: берём у сервера
токен и перебираем числа, пока SHA-256 не начнётся с нужного числа нулей.
"""

import sqlite3

from fastapi.testclient import TestClient

from app import local_captcha
from app.captcha import CaptchaConfig
from app.main import create_app


class DenyVerifier:
    """Внешняя проверка недоступна: и токена нет, и Яндекс отвечает отказом."""

    def __init__(self):
        self.calls: list[tuple[str | None, str | None]] = []

    def verify(self, token, ip=None):
        self.calls.append((token, ip))
        return False


def _app(tmp_path, *, captcha=True, name="local_captcha"):
    db_path = tmp_path / f"{name}.sqlite3"
    verifier = DenyVerifier()
    config = CaptchaConfig(client_key="ysc1_test", server_key="ysc2_test") if captcha else None
    app = create_app(
        f"sqlite:///{db_path}",
        captcha_config=config,
        captcha_verifier=verifier,
    )
    return app, db_path, verifier


def _solve(client) -> dict[str, str]:
    """Решение своей капчи так, как его считает браузер."""
    payload = client.post("/api/v1/auth/captcha/challenge").json()
    prefix = payload["prefix"]
    nonce = 0
    while not local_captcha.hash_for(payload["token"], str(nonce)).startswith(prefix):
        nonce += 1
    return {"captcha_local_token": payload["token"], "captcha_local_nonce": str(nonce)}


def _register_body(email="visitor@example.com", **extra):
    return {"email": email, "password": "password123", "full_name": "Тест Тестов", **extra}


def test_challenge_is_issued_only_when_captcha_enabled(tmp_path):
    app_off, _, _ = _app(tmp_path, captcha=False, name="off")
    app_on, _, _ = _app(tmp_path, name="on")

    with TestClient(app_off) as client:
        disabled = client.post("/api/v1/auth/captcha/challenge")
    with TestClient(app_on) as client:
        issued = client.post("/api/v1/auth/captcha/challenge")

    assert disabled.status_code == 404
    assert issued.status_code == 200
    body = issued.json()
    assert body["token"] and len(body["token"]) >= 20
    assert body["difficulty"] == local_captcha.DEFAULT_DIFFICULTY
    assert body["prefix"] == "0" * local_captcha.DEFAULT_DIFFICULTY
    assert body["expires_in"] == local_captcha.CHALLENGE_TTL_SECONDS


def test_register_passes_with_local_captcha_when_widget_blocked(tmp_path):
    app, _, verifier = _app(tmp_path)
    with TestClient(app) as client:
        solution = _solve(client)
        created = client.post("/api/v1/auth/register", json=_register_body(**solution))
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "visitor@example.com", "password": "password123", **_solve(client)},
        )

    assert created.status_code == 201, created.text
    # Внешний виджет не спрашивали: токена нет, значит и запроса к Яндексу нет.
    assert verifier.calls == []
    assert login.status_code == 200


def test_local_captcha_rejects_wrong_nonce(tmp_path):
    app, _, _ = _app(tmp_path)
    with TestClient(app) as client:
        payload = client.post("/api/v1/auth/captcha/challenge").json()
        response = client.post(
            "/api/v1/auth/register",
            json=_register_body(captcha_local_token=payload["token"], captcha_local_nonce="0"),
        )

    assert response.status_code == 400
    assert "робот" in response.json()["detail"]


def test_local_captcha_rejects_unknown_token(tmp_path):
    app, _, _ = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/register",
            json=_register_body(captcha_local_token="нет-такого", captcha_local_nonce="1"),
        )

    assert response.status_code == 400


def test_local_captcha_is_single_use(tmp_path):
    app, _, _ = _app(tmp_path)
    with TestClient(app) as client:
        solution = _solve(client)
        first = client.post("/api/v1/auth/register", json=_register_body(**solution))
        second = client.post(
            "/api/v1/auth/register", json=_register_body(email="other@example.com", **solution)
        )

    assert first.status_code == 201
    # То же решение второй раз не годится: задание погашено.
    assert second.status_code == 400


def test_local_captcha_expires(tmp_path):
    app, db_path, _ = _app(tmp_path)
    with TestClient(app) as client:
        solution = _solve(client)
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "UPDATE local_captcha_challenges SET created_at = '2000-01-01 00:00:00.000000'"
            )
            connection.commit()
        finally:
            connection.close()
        response = client.post("/api/v1/auth/register", json=_register_body(**solution))

    assert response.status_code == 400


def test_local_captcha_missing_pair_is_rejected_before_network(tmp_path):
    app, _, verifier = _app(tmp_path)
    with TestClient(app) as client:
        only_token = client.post(
            "/api/v1/auth/register", json=_register_body(captcha_local_token="token")
        )
        empty = client.post("/api/v1/auth/register", json=_register_body())

    assert only_token.status_code == 400
    assert empty.status_code == 400
    # Пустые значения не должны доходить до Яндекса: проверять нечего.
    assert verifier.calls == []


def test_event_create_accepts_local_captcha(tmp_path):
    app, db_path, _ = _app(tmp_path)
    with TestClient(app) as client:
        client.post("/api/v1/auth/register", json=_register_body(**_solve(client)))
        connection = sqlite3.connect(db_path)
        try:
            connection.execute("UPDATE users SET role = 'organizer'")
            connection.commit()
        finally:
            connection.close()
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "visitor@example.com", "password": "password123", **_solve(client)},
        )
        headers = {"X-Auth-Token": login.json()["access_token"]}
        created = client.post(
            "/api/v1/events",
            json={
                "title": "Город звучит",
                "slug": "gorod-zvuchit-local",
                "category": "Музыка",
                "date": "2030-05-01",
                "time": "18:00",
                "venue": "Парк Гагарина",
                "price": "Бесплатно",
                **_solve(client),
            },
            headers=headers,
        )

    assert created.status_code == 201, created.text


def test_challenge_endpoint_is_rate_limited(tmp_path):
    app, _, _ = _app(tmp_path)
    with TestClient(app) as client:
        statuses = [client.post("/api/v1/auth/captcha/challenge").status_code for _ in range(62)]

    # Лимит не даёт наполнять таблицу заданиями бесплатно.
    assert statuses[0] == 200
    assert 429 in statuses
