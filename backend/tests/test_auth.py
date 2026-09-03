from fastapi.testclient import TestClient

from app.main import create_app


def _register(client, email="visitor@example.com", password="password123", full_name="Тест Тестов"):
    return client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": full_name},
    )


def test_register_creates_visitor(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'auth.sqlite3'}")
    with TestClient(app) as client:
        response = _register(client)

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "visitor@example.com"
    assert body["role"] == "visitor"
    assert body["is_active"] is True
    assert "password" not in body
    assert "password_hash" not in body


def test_register_rejects_duplicate_email(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'auth_dup.sqlite3'}")
    with TestClient(app) as client:
        first = _register(client)
        second = _register(client)

    assert first.status_code == 201
    assert second.status_code == 409


def test_register_validates_email_and_password_length(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'auth_validate.sqlite3'}")
    with TestClient(app) as client:
        bad_email = client.post(
            "/api/v1/auth/register",
            json={"email": "not-an-email", "password": "password123", "full_name": "А Б"},
        )
        short_password = client.post(
            "/api/v1/auth/register",
            json={"email": "x@example.com", "password": "short", "full_name": "А Б"},
        )

    assert bad_email.status_code == 422
    assert short_password.status_code == 422


def test_login_returns_tokens_and_me(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'auth_login.sqlite3'}")
    with TestClient(app) as client:
        _register(client)
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "visitor@example.com", "password": "password123"},
        )
        assert login.status_code == 200
        tokens = login.json()
        assert tokens["access_token"]
        assert tokens["refresh_token"]
        assert tokens["token_type"] == "bearer"

        me = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        assert me.status_code == 200
        assert me.json()["email"] == "visitor@example.com"


def test_login_rejects_wrong_password(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'auth_badpass.sqlite3'}")
    with TestClient(app) as client:
        _register(client)
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "visitor@example.com", "password": "wrong-password"},
        )

    assert response.status_code == 401


def test_me_requires_auth(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'auth_me.sqlite3'}")
    with TestClient(app) as client:
        no_token = client.get("/api/v1/auth/me")
        bad_token = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer not-a-valid-token"},
        )

    assert no_token.status_code == 401
    assert bad_token.status_code == 401


def test_refresh_rotates_token_and_revokes_old(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'auth_refresh.sqlite3'}")
    with TestClient(app) as client:
        _register(client)
        tokens = client.post(
            "/api/v1/auth/login",
            json={"email": "visitor@example.com", "password": "password123"},
        ).json()

        refreshed = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": tokens["refresh_token"]},
        )
        assert refreshed.status_code == 200
        new_tokens = refreshed.json()
        # Ротация: выдан НОВЫЙ refresh-токен (access может совпасть, т.к. без jti/iat
        # два JWT для одного юзера в одну секунду идентичны — это ок для безопасности).
        assert new_tokens["refresh_token"] != tokens["refresh_token"]

        # Старый refresh-токен отозван — повторное использование запрещено.
        reuse = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": tokens["refresh_token"]},
        )
        assert reuse.status_code == 401


def test_logout_revokes_refresh_token(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'auth_logout.sqlite3'}")
    with TestClient(app) as client:
        _register(client)
        tokens = client.post(
            "/api/v1/auth/login",
            json={"email": "visitor@example.com", "password": "password123"},
        ).json()

        logout = client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": tokens["refresh_token"]},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        assert logout.status_code == 200

        # После logout refresh-токен больше не действует.
        reuse = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": tokens["refresh_token"]},
        )
        assert reuse.status_code == 401