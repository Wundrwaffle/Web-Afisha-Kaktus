from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import build_engine, build_session_factory
from app.main import create_app
from app.models import User


def _event_payload(slug="organizer-event"):
    return {
        "title": "Событие организатора",
        "slug": slug,
        "category": "Культура",
        "date": (date.today() + timedelta(days=5)).isoformat(),
        "time": "18:00",
        "venue": "Площадка",
        "price": "Бесплатно",
    }


def _register(client, email="organizer@example.com"):
    return client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "password123", "full_name": "Органайзер"},
    )


def _login(client, email="organizer@example.com"):
    return client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "password123"},
    ).json()


def _set_role(database_url: str, email: str, role: str) -> None:
    """Напрямую меняет роль пользователя в тестовой БД — регистрация всегда даёт
    visitor, а назначение ролей (Этап 4 / админка) в Шаге 2 ещё не реализовано."""
    engine = build_engine(database_url)
    with build_session_factory(engine)() as session:
        user = session.scalar(select(User).where(User.email == email))
        assert user is not None, "пользователь для апгрейда роли не найден"
        user.role = role
        session.commit()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_anonymous_cannot_create_event(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'roles_anon.sqlite3'}")
    with TestClient(app) as client:
        response = client.post("/api/v1/events", json=_event_payload())
    assert response.status_code == 401


def test_visitor_cannot_create_event(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'roles_visitor.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client)
        tokens = _login(client)
        response = client.post(
            "/api/v1/events", json=_event_payload(), headers=_auth(tokens["access_token"])
        )
    assert response.status_code == 403


def test_organizer_can_create_event(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'roles_organizer.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client)
        _set_role(db_url, "organizer@example.com", "organizer")
        tokens = _login(client)
        response = client.post(
            "/api/v1/events", json=_event_payload(), headers=_auth(tokens["access_token"])
        )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending_moderation"
    assert body["organizer_id"] is not None


def test_admin_can_create_event(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'roles_admin.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, email="admin@example.com")
        _set_role(db_url, "admin@example.com", "admin")
        tokens = _login(client, email="admin@example.com")
        response = client.post(
            "/api/v1/events", json=_event_payload(), headers=_auth(tokens["access_token"])
        )
    assert response.status_code == 201


def test_moderator_cannot_create_event(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'roles_moderator.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, email="moderator@example.com")
        _set_role(db_url, "moderator@example.com", "moderator")
        tokens = _login(client, email="moderator@example.com")
        response = client.post(
            "/api/v1/events", json=_event_payload(), headers=_auth(tokens["access_token"])
        )
    assert response.status_code == 403


def test_event_remembers_its_organizer(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'roles_owner.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client)
        _set_role(db_url, "organizer@example.com", "organizer")
        tokens = _login(client)
        created = client.post(
            "/api/v1/events", json=_event_payload(), headers=_auth(tokens["access_token"])
        )
        assert created.status_code == 201
        owner_id = created.json()["organizer_id"]

        me = client.get("/api/v1/auth/me", headers=_auth(tokens["access_token"]))
        assert me.json()["id"] == owner_id