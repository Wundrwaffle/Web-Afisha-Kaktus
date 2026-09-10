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


def _set_role(db_url, email, role):
    engine = build_engine(db_url)
    with build_session_factory(engine)() as session:
        user = session.scalar(select(User).where(User.email == email))
        user.role = role
        session.commit()


def _register_role(client, db_url, email, role):
    _register(client, email)
    _set_role(db_url, email, role)
    return _login(client, email)["access_token"]


def _event_payload(slug, days_ahead=5):
    return {
        "title": f"Событие {slug}",
        "slug": slug,
        "category": "Культура",
        "date": (date.today() + timedelta(days=days_ahead)).isoformat(),
        "time": "18:00",
        "venue": "Площадка",
        "price": "Бесплатно",
    }


def _seed_event(db_url, slug, days_ahead=1, status="published"):
    """Кладёт событие напрямую и возвращает его id."""
    engine = build_engine(db_url)
    with build_session_factory(engine)() as session:
        event = Event(
            title=f"Событие {slug}",
            slug=slug,
            status=status,
            category="Культура",
            date=date.today() + timedelta(days=days_ahead),
            time=time(18, 0),
            venue="Площадка",
            price="Бесплатно",
        )
        session.add(event)
        session.commit()
        return event.id


def _notifications(client, token):
    return client.get("/api/v1/me/notifications", headers=_auth(token)).json()["items"]


# --- Триггеры: админу и модератору ---

def test_created_event_notifies_admin_and_moderator(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'n_create.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        org = _register_role(client, db_url, "org@example.com", "organizer")
        adm = _register_role(client, db_url, "adm@example.com", "admin")
        mod = _register_role(client, db_url, "mod@example.com", "moderator")

        client.post("/api/v1/events", json=_event_payload("new-event"), headers=_auth(org))

        adm_items = _notifications(client, adm)
        mod_items = _notifications(client, mod)

    assert any(n["kind"] == "event_created" for n in adm_items)
    assert any(n["kind"] == "event_created" for n in mod_items)


def test_update_event_notifies_staff(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'n_update.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        org = _register_role(client, db_url, "org@example.com", "organizer")
        adm = _register_role(client, db_url, "adm@example.com", "admin")

        created = client.post(
            "/api/v1/events", json=_event_payload("to-edit"), headers=_auth(org)
        ).json()
        client.patch(
            f"/api/v1/me/events/{created['event_id']}",
            json={"title": "Новое название"},
            headers=_auth(org),
        )

        adm_items = _notifications(client, adm)

    assert any(n["kind"] == "event_updated" for n in adm_items)


def test_resubmit_event_notifies_staff(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'n_resubmit.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        org = _register_role(client, db_url, "org@example.com", "organizer")
        mod = _register_role(client, db_url, "mod@example.com", "moderator")
        adm = _register_role(client, db_url, "adm@example.com", "admin")

        created = client.post(
            "/api/v1/events", json=_event_payload("rej-me"), headers=_auth(org)
        ).json()
        client.post(
            f"/api/v1/moderation/events/{created['event_id']}/review",
            json={"decision": "reject", "reason": "Неверная дата"},
            headers=_auth(mod),
        )
        client.post(
            f"/api/v1/me/events/{created['event_id']}/resubmit", headers=_auth(org)
        )

        adm_items = _notifications(client, adm)

    assert any(n["kind"] == "event_resubmitted" for n in adm_items)


# --- Триггеры: организатору ---

def test_reject_notifies_organizer(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'n_reject.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        org = _register_role(client, db_url, "org@example.com", "organizer")
        mod = _register_role(client, db_url, "mod@example.com", "moderator")

        created = client.post(
            "/api/v1/events", json=_event_payload("bad"), headers=_auth(org)
        ).json()
        client.post(
            f"/api/v1/moderation/events/{created['event_id']}/review",
            json={"decision": "reject", "reason": "Дубль события"},
            headers=_auth(mod),
        )

        items = _notifications(client, org)

    rejected = [n for n in items if n["kind"] == "event_rejected"]
    assert rejected, "организатор должен получить уведомление об отказе"
    assert "Дубль события" in rejected[0]["text"]


def test_approve_notifies_organizer(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'n_approve.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        org = _register_role(client, db_url, "org@example.com", "organizer")
        mod = _register_role(client, db_url, "mod@example.com", "moderator")

        created = client.post(
            "/api/v1/events", json=_event_payload("good"), headers=_auth(org)
        ).json()
        client.post(
            f"/api/v1/moderation/events/{created['event_id']}/review",
            json={"decision": "approve"},
            headers=_auth(mod),
        )

        items = _notifications(client, org)

    assert any(n["kind"] == "event_approved" for n in items)


def test_moderator_unpublish_notifies_organizer(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'n_unpublish.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        org = _register_role(client, db_url, "org@example.com", "organizer")
        mod = _register_role(client, db_url, "mod@example.com", "moderator")

        created = client.post(
            "/api/v1/events", json=_event_payload("unpub"), headers=_auth(org)
        ).json()
        client.post(
            f"/api/v1/moderation/events/{created['event_id']}/review",
            json={"decision": "approve"},
            headers=_auth(mod),
        )
        client.post(
            f"/api/v1/moderation/events/{created['event_id']}/unpublish",
            json={"reason": "Спам-контент"},
            headers=_auth(mod),
        )

        items = _notifications(client, org)

    assert any(n["kind"] == "event_unpublished" for n in items)


# --- Напоминания «за день до события» ---

def test_favorite_reminder_generated_once(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'n_reminder.sqlite3'}"
    app = create_app(db_url)
    event_id = _seed_event(db_url, "tomorrow", days_ahead=1)  # завтра

    with TestClient(app) as client:
        _register(client, "vis@example.com")
        tok = _login(client, "vis@example.com")["access_token"]
        client.post(f"/api/v1/me/favorites/{event_id}", headers=_auth(tok))

        first = _notifications(client, tok)
        second = _notifications(client, tok)

    first_reminders = [n for n in first if n["kind"] == "event_reminder"]
    second_reminders = [n for n in second if n["kind"] == "event_reminder"]
    assert len(first_reminders) == 1
    assert len(second_reminders) == 1  # повторный запрос не дублирует


def test_no_reminder_for_non_favorited_or_far_event(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'n_no_reminder.sqlite3'}"
    app = create_app(db_url)
    far_id = _seed_event(db_url, "far", days_ahead=3)  # не завтра
    _seed_event(db_url, "tomorrow-not-fav", days_ahead=1)

    with TestClient(app) as client:
        _register(client, "vis@example.com")
        tok = _login(client, "vis@example.com")["access_token"]
        # Избранное только на дальнее событие — напоминаний быть не должно.
        client.post(f"/api/v1/me/favorites/{far_id}", headers=_auth(tok))

        items = _notifications(client, tok)

    assert not any(n["kind"] == "event_reminder" for n in items)


# --- Чтение / изоляция ---

def test_mark_read_and_read_all(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'n_read.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        org = _register_role(client, db_url, "org@example.com", "organizer")
        mod = _register_role(client, db_url, "mod@example.com", "moderator")

        client.post("/api/v1/events", json=_event_payload("read-one"), headers=_auth(org))
        client.post("/api/v1/events", json=_event_payload("read-two"), headers=_auth(org))
        items = _notifications(client, mod)
        assert len(items) == 2
        assert all(n["is_read"] is False for n in items)

        # Помечаем одно прочитанным.
        first_id = items[0]["id"]
        marked = client.patch(
            f"/api/v1/me/notifications/{first_id}/read", headers=_auth(mod)
        ).json()
        assert marked["is_read"] is True

        # read-all закрывает остальные.
        client.post("/api/v1/me/notifications/read-all", headers=_auth(mod))
        after = _notifications(client, mod)
    assert all(n["is_read"] is True for n in after)


def test_notifications_are_isolated_and_owned(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'n_isolated.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        org = _register_role(client, db_url, "org@example.com", "organizer")
        mod = _register_role(client, db_url, "mod@example.com", "moderator")
        _register(client, "other@example.com")
        other = _login(client, "other@example.com")["access_token"]

        client.post("/api/v1/events", json=_event_payload("iso"), headers=_auth(org))
        mod_items = _notifications(client, mod)
        assert mod_items, "у модератора должны быть уведомления"

        # Чужой пользователь своих уведомлений не видит.
        other_items = _notifications(client, other)
        assert other_items == []

        # И не может пометить чужое уведомление прочитанным.
        mod_notif_id = mod_items[0]["id"]
        resp = client.patch(
            f"/api/v1/me/notifications/{mod_notif_id}/read", headers=_auth(other)
        )
    assert resp.status_code == 404
