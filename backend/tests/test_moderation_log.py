"""Журнал модерации: действия модератора должны оставлять след.

Проверяем не «таблица существует», а ответы на вопросы, которые возникают
позже: кто одобрил событие, на каком основании сняли с публикации, кто удалил
событие (строка обязана пережить удаление) и кто менял роль.
"""

from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import build_engine, build_session_factory
from app.main import create_app
from app.models import Event, ModerationLog, User


def _event_payload(slug: str) -> dict[str, str]:
    return {
        "title": f"Событие {slug}",
        "slug": slug,
        "category": "Культура",
        "date": (date.today() + timedelta(days=5)).isoformat(),
        "time": "18:00",
        "venue": "Площадка",
        "price": "Бесплатно",
    }


def _register(client, email: str, name: str = "Органайзер") -> None:
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "password123", "full_name": name},
    )
    assert resp.status_code == 201, resp.text


def _login(client, email: str) -> str:
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "password123"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _patch_row(db_url: str, model, email_field, email: str, **values) -> None:
    """Правим строку в обход API: роль и подтверждение почты ставит не клиент."""
    engine = build_engine(db_url)
    with build_session_factory(engine)() as session:
        row = session.scalar(select(model).where(email_field == email))
        assert row is not None, email
        for key, value in values.items():
            setattr(row, key, value)
        session.commit()


def _make_staff(client, db_url: str, email: str, role: str) -> str:
    _register(client, email, role)
    _patch_row(db_url, User, User.email, email, role=role, email_verified=True)
    return _login(client, email)


def _pending_event(client, db_url: str, slug: str = "log-me") -> int:
    """Событие, которое ждёт модерации: создаём от организатора."""
    email = "org@example.com"
    _register(client, email)
    _patch_row(db_url, User, User.email, email, role="organizer", email_verified=True)
    token = _login(client, email)
    created = client.post(
        "/api/v1/events", json=_event_payload(slug), headers=_auth(token)
    )
    assert created.status_code == 201, created.text
    # Публичный id события — строка вида "event-3"; в API модерации нужен PK.
    engine = build_engine(db_url)
    with build_session_factory(engine)() as session:
        event = session.scalar(select(Event).where(Event.slug == slug))
        assert event is not None, slug
        return int(event.id)


def _log(client, token: str, **params) -> list[dict]:
    resp = client.get("/api/v1/moderation/log", headers=_auth(token), params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def test_approving_event_is_logged(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'log-approve.sqlite3'}"
    with TestClient(create_app(db_url)) as client:
        event_id = _pending_event(client, db_url)
        moderator = _make_staff(client, db_url, "mod@example.com", "moderator")

        reviewed = client.post(
            f"/api/v1/moderation/events/{event_id}/review",
            json={"decision": "approve"},
            headers=_auth(moderator),
        )
        assert reviewed.status_code == 200, reviewed.text

        entries = _log(client, moderator)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["action"] == "event_approved"
    assert entry["entity_id"] == event_id
    assert entry["actor_email"] == "mod@example.com"
    assert entry["actor_role"] == "moderator"
    assert entry["title"] == "Событие log-me"
    # Причина при одобрении не нужна — и не должна появляться пустой строкой.
    assert entry["details"] is None


def test_rejecting_event_logs_reason(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'log-reject.sqlite3'}"
    with TestClient(create_app(db_url)) as client:
        event_id = _pending_event(client, db_url, slug="reject-me")
        moderator = _make_staff(client, db_url, "mod@example.com", "moderator")

        client.post(
            f"/api/v1/moderation/events/{event_id}/review",
            json={"decision": "reject", "reason": "Нет адреса площадки"},
            headers=_auth(moderator),
        )

        entries = _log(client, moderator, action="event_rejected")
    assert [e["action"] for e in entries] == ["event_rejected"]
    assert entries[0]["details"] == "Нет адреса площадки"


def test_deleted_event_leaves_readable_log_row(tmp_path):
    """Строка журнала обязана пережить удаление события — иначе «кто удалил»."""
    db_url = f"sqlite:///{tmp_path / 'log-delete.sqlite3'}"
    with TestClient(create_app(db_url)) as client:
        event_id = _pending_event(client, db_url, slug="delete-me")
        moderator = _make_staff(client, db_url, "mod@example.com", "moderator")
        client.post(
            f"/api/v1/moderation/events/{event_id}/review",
            json={"decision": "approve"},
            headers=_auth(moderator),
        )
        unpublished = client.post(
            f"/api/v1/moderation/events/{event_id}/unpublish",
            json={"reason": "Дубликат афиши"},
            headers=_auth(moderator),
        )
        assert unpublished.status_code == 200, unpublished.text

        deleted = client.delete(
            f"/api/v1/moderation/events/{event_id}", headers=_auth(moderator)
        )
        assert deleted.status_code == 204, deleted.text

        entries = _log(client, moderator)

    actions = [e["action"] for e in entries]
    assert actions == ["event_deleted", "event_unpublished", "event_approved"]
    deleted_entry = entries[0]
    assert deleted_entry["entity_id"] == event_id
    assert deleted_entry["title"] == "Событие delete-me"
    assert entries[1]["details"] == "Дубликат афиши"


def test_log_is_closed_for_non_staff(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'log-closed.sqlite3'}"
    with TestClient(create_app(db_url)) as client:
        _register(client, "vasya@example.com", "Вася")
        _patch_row(
            db_url, User, User.email, "vasya@example.com", email_verified=True
        )
        visitor = _login(client, "vasya@example.com")

        resp = client.get("/api/v1/moderation/log", headers=_auth(visitor))
    assert resp.status_code == 403


def test_role_change_is_logged_only_when_role_actually_changes(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'log-roles.sqlite3'}"
    with TestClient(create_app(db_url)) as client:
        admin = _make_staff(client, db_url, "boss@example.com", "admin")
        _register(client, "vasya@example.com", "Вася")
        _patch_row(
            db_url, User, User.email, "vasya@example.com", email_verified=True
        )
        target_id = client.get(
            "/api/v1/admin/users", headers=_auth(admin)
        ).json()["items"][-1]["id"]

        client.patch(
            f"/api/v1/admin/users/{target_id}/role",
            json={"role": "moderator"},
            headers=_auth(admin),
        )
        # Повторная установка той же роли — не действие, записи быть не должно.
        client.patch(
            f"/api/v1/admin/users/{target_id}/role",
            json={"role": "moderator"},
            headers=_auth(admin),
        )

        entries = _log(client, admin)
    assert [e["action"] for e in entries] == ["role_changed"]
    assert entries[0]["details"] == "роль visitor → moderator"
    assert entries[0]["entity_id"] == target_id


def test_log_entries_survive_in_raw_table_after_event_gone(tmp_path):
    """Страховка от каскадного удаления: строка журнала остаётся в таблице."""
    db_url = f"sqlite:///{tmp_path / 'log-raw.sqlite3'}"
    with TestClient(create_app(db_url)) as client:
        event_id = _pending_event(client, db_url, slug="gone")
        moderator = _make_staff(client, db_url, "mod@example.com", "moderator")
        client.post(
            f"/api/v1/moderation/events/{event_id}/review",
            json={"decision": "reject", "reason": "Не афиша"},
            headers=_auth(moderator),
        )
        client.delete(
            f"/api/v1/moderation/events/{event_id}", headers=_auth(moderator)
        )

        engine = build_engine(db_url)
        with build_session_factory(engine)() as session:
            assert session.get(Event, event_id) is None
            rows = session.scalars(select(ModerationLog)).all()
    # Порядок в таблице не задан (обе строки в одну секунду) — важен состав.
    assert sorted(row.action for row in rows) == ["event_deleted", "event_rejected"]
