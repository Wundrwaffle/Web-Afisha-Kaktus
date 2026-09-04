from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.main import create_app


def _event_payload(slug):
    return {
        "title": f"Событие {slug}",
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


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _set_role(db_url, email, role):
    from sqlalchemy import select

    from app.db import build_engine, build_session_factory
    from app.models import User

    engine = build_engine(db_url)
    with build_session_factory(engine)() as session:
        user = session.scalar(select(User).where(User.email == email))
        user.role = role
        session.commit()


def _create_as_organizer(client, token, slug="mine-1"):
    return client.post(
        "/api/v1/events", json=_event_payload(slug), headers=_auth(token)
    )


def test_organizer_lists_only_own_events(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'cab_own.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        # Организатор А
        _register(client, "a@example.com")
        _set_role(db_url, "a@example.com", "organizer")
        tok_a = _login(client, "a@example.com")["access_token"]
        _create_as_organizer(client, tok_a, slug="event-a")

        # Организатор Б
        _register(client, "b@example.com")
        _set_role(db_url, "b@example.com", "organizer")
        tok_b = _login(client, "b@example.com")["access_token"]
        _create_as_organizer(client, tok_b, slug="event-b")

        me = client.get("/api/v1/me/events", headers=_auth(tok_a))
    assert me.status_code == 200
    slugs = {e["slug"] for e in me.json()["items"]}
    assert slugs == {"event-a"}


def test_my_events_shows_all_statuses(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'cab_status.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client)
        _set_role(db_url, "organizer@example.com", "organizer")
        tok = _login(client)["access_token"]
        _create_as_organizer(client, tok, slug="pending-one")
        _create_as_organizer(client, tok, slug="pending-two")

        # Оба события в pending — видны организатору (в отличие от публичного каталога).
        me = client.get("/api/v1/me/events", headers=_auth(tok))
    assert me.status_code == 200
    assert me.json()["total"] == 2


def test_visitor_my_events_is_forbidden(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'cab_visitor.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "plain@example.com")
        tok = _login(client, "plain@example.com")["access_token"]
        me = client.get("/api/v1/me/events", headers=_auth(tok))
    assert me.status_code == 403


def test_moderation_queue_requires_role(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'mod_gate.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "vis@example.com")
        tok = _login(client, "vis@example.com")["access_token"]
        q = client.get("/api/v1/moderation/queue", headers=_auth(tok))
    assert q.status_code == 403


def test_moderator_reviews_queue(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'mod_flow.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        # Организатор создаёт два события
        _register(client, "org@example.com")
        _set_role(db_url, "org@example.com", "organizer")
        org_tok = _login(client, "org@example.com")["access_token"]
        ev1 = _create_as_organizer(client, org_tok, slug="ev-1")
        ev2 = _create_as_organizer(client, org_tok, slug="ev-2")
        id1 = ev1.json()["event_id"]
        id2 = ev2.json()["event_id"]

        # Модератор
        _register(client, "mod@example.com")
        _set_role(db_url, "mod@example.com", "moderator")
        mod_tok = _login(client, "mod@example.com")["access_token"]

        # Очередь содержит оба
        q = client.get("/api/v1/moderation/queue", headers=_auth(mod_tok))
        assert q.status_code == 200
        assert {e["event_id"] for e in q.json()["items"]} == {id1, id2}

        # Одобряем первое, отклоняем второе
        ap = client.post(
            f"/api/v1/moderation/events/{id1}/review",
            json={"decision": "approve"},
            headers=_auth(mod_tok),
        )
        rj = client.post(
            f"/api/v1/moderation/events/{id2}/review",
            json={"decision": "reject"},
            headers=_auth(mod_tok),
        )
        assert ap.json()["status"] == "published"
        assert rj.json()["status"] == "rejected"

        # Очередь пуста
        q2 = client.get("/api/v1/moderation/queue", headers=_auth(mod_tok))
        assert q2.json()["total"] == 0