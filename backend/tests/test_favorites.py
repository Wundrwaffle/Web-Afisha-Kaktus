from datetime import date, time, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import build_engine, build_session_factory
from app.main import create_app
from app.models import Event, User


def _register(client, email="user@example.com"):
    return client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "password123", "full_name": "Тест Тестов"},
    )


def _login(client, email="user@example.com"):
    return client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "password123"},
    ).json()


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _seed_event(db_url, slug="fav-event"):
    """Кладёт опубликованное событие напрямую и возвращает его id."""
    engine = build_engine(db_url)
    with build_session_factory(engine)() as session:
        event = Event(
            title=f"Событие {slug}",
            slug=slug,
            status="published",
            category="Культура",
            date=date.today() + timedelta(days=5),
            time=time(18, 0),
            venue="Площадка",
            price="Бесплатно",
        )
        session.add(event)
        session.commit()
        return event.id


def test_visitor_can_add_and_list_favorite(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'fav_visitor.sqlite3'}"
    app = create_app(db_url)
    event_id = _seed_event(db_url)

    with TestClient(app) as client:
        _register(client)
        token = _login(client)["access_token"]

        added = client.post(f"/api/v1/me/favorites/{event_id}", headers=_auth(token))
        assert added.status_code == 201

        listing = client.get("/api/v1/me/favorites", headers=_auth(token))
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    assert body["items"][0]["event_id"] == event_id


def test_favorites_are_per_user(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'fav_peruser.sqlite3'}"
    app = create_app(db_url)
    event_id = _seed_event(db_url)

    with TestClient(app) as client:
        _register(client, "a@example.com")
        tok_a = _login(client, "a@example.com")["access_token"]
        _register(client, "b@example.com")
        tok_b = _login(client, "b@example.com")["access_token"]

        client.post(f"/api/v1/me/favorites/{event_id}", headers=_auth(tok_a))
        mine = client.get("/api/v1/me/favorites", headers=_auth(tok_b))

    assert mine.status_code == 200
    assert mine.json()["total"] == 0


def test_remove_favorite_is_idempotent(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'fav_remove.sqlite3'}"
    app = create_app(db_url)
    event_id = _seed_event(db_url)

    with TestClient(app) as client:
        _register(client)
        token = _login(client)["access_token"]

        client.post(f"/api/v1/me/favorites/{event_id}", headers=_auth(token))
        removed = client.delete(f"/api/v1/me/favorites/{event_id}", headers=_auth(token))
        assert removed.status_code == 204

        # Повторное удаление не ломается (204, ничего не делает).
        again = client.delete(f"/api/v1/me/favorites/{event_id}", headers=_auth(token))
        assert again.status_code == 204

        listing = client.get("/api/v1/me/favorites", headers=_auth(token))
    assert listing.json()["total"] == 0


def test_duplicate_favorite_is_idempotent(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'fav_dup.sqlite3'}"
    app = create_app(db_url)
    event_id = _seed_event(db_url)

    with TestClient(app) as client:
        _register(client)
        token = _login(client)["access_token"]

        first = client.post(f"/api/v1/me/favorites/{event_id}", headers=_auth(token))
        second = client.post(f"/api/v1/me/favorites/{event_id}", headers=_auth(token))
        assert first.status_code == 201
        assert second.status_code == 201

        listing = client.get("/api/v1/me/favorites", headers=_auth(token))
    assert listing.json()["total"] == 1


def test_add_favorite_requires_auth(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'fav_auth.sqlite3'}"
    app = create_app(db_url)
    event_id = _seed_event(db_url)

    with TestClient(app) as client:
        anonymous = client.post(f"/api/v1/me/favorites/{event_id}")
    assert anonymous.status_code == 401


def test_favorite_unknown_event_404(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'fav_404.sqlite3'}"
    app = create_app(db_url)

    with TestClient(app) as client:
        _register(client)
        token = _login(client)["access_token"]
        resp = client.post("/api/v1/me/favorites/999999", headers=_auth(token))
    assert resp.status_code == 404
