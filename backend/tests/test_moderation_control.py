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
        json={"email": email, "password": "password123", "full_name": "Тест"},
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


def _published_event(client, organizer_token, moderator_token, slug="pub-1"):
    """Организатор создаёт событие, модератор публикует. Возвращает event_id."""
    created = client.post(
        "/api/v1/events", json=_event_payload(slug), headers=_auth(organizer_token)
    ).json()
    event_id = created["event_id"]
    client.post(
        f"/api/v1/moderation/events/{event_id}/review",
        json={"decision": "approve"},
        headers=_auth(moderator_token),
    )
    return event_id


def test_moderator_edits_foreign_published_event(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'mod_edit.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "org@example.com")
        _set_role(db_url, "org@example.com", "organizer")
        org = _login(client, "org@example.com")["access_token"]
        _register(client, "mod@example.com")
        _set_role(db_url, "mod@example.com", "moderator")
        mod = _login(client, "mod@example.com")["access_token"]
        event_id = _published_event(client, org, mod, slug="mod-edit-1")

        # Модератор правит ЧУЖОЕ опубликованное событие.
        resp = client.patch(
            f"/api/v1/moderation/events/{event_id}",
            json={"title": "Исправленное название", "price": "300 руб"},
            headers=_auth(mod),
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Исправленное название"
    assert body["price"] == "300 руб"
    assert body["status"] == "published"  # статус не сбрасывается


def test_admin_edits_foreign_event(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'mod_admin.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "org@example.com")
        _set_role(db_url, "org@example.com", "organizer")
        org = _login(client, "org@example.com")["access_token"]
        _register(client, "adm@example.com")
        _set_role(db_url, "adm@example.com", "admin")
        adm = _login(client, "adm@example.com")["access_token"]
        event_id = _published_event(client, org, adm, slug="adm-edit-1")

        resp = client.patch(
            f"/api/v1/moderation/events/{event_id}",
            json={"venue": "Другая площадка"},
            headers=_auth(adm),
        )

    assert resp.status_code == 200
    assert resp.json()["venue"] == "Другая площадка"


def test_moderator_unpublishes_foreign_event_with_reason(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'mod_unpub.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "org@example.com")
        _set_role(db_url, "org@example.com", "organizer")
        org = _login(client, "org@example.com")["access_token"]
        _register(client, "mod@example.com")
        _set_role(db_url, "mod@example.com", "moderator")
        mod = _login(client, "mod@example.com")["access_token"]
        event_id = _published_event(client, org, mod, slug="mod-unpub-1")

        resp = client.post(
            f"/api/v1/moderation/events/{event_id}/unpublish",
            json={"reason": "Клиент прислал неверную дату, исправьте"},
            headers=_auth(mod),
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "draft"
    assert body["moderation_note"] == "Клиент прислал неверную дату, исправьте"

    # Событие пропало из публичного каталога.
    public = client.get("/api/v1/events")
    assert "mod-unpub-1" not in {e["slug"] for e in public.json()["items"]}


def test_unpublish_requires_reason_and_published_status(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'mod_unpub_rules.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "org@example.com")
        _set_role(db_url, "org@example.com", "organizer")
        org = _login(client, "org@example.com")["access_token"]
        _register(client, "mod@example.com")
        _set_role(db_url, "mod@example.com", "moderator")
        mod = _login(client, "mod@example.com")["access_token"]

        # pending-событие снять нельзя
        pending = client.post(
            "/api/v1/events", json=_event_payload("still-pending"),
            headers=_auth(org),
        ).json()
        resp_pending = client.post(
            f"/api/v1/moderation/events/{pending['event_id']}/unpublish",
            json={"reason": "Передумали публиковать"},
            headers=_auth(mod),
        )
        # пустая причина у published нельзя
        event_id = _published_event(client, org, mod, slug="pub-no-reason")
        resp_empty = client.post(
            f"/api/v1/moderation/events/{event_id}/unpublish",
            json={"reason": "  "},
            headers=_auth(mod),
        )

    assert resp_pending.status_code == 409
    assert resp_empty.status_code == 422


def test_moderator_deletes_foreign_event_and_cleans_favorites(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'mod_del.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "org@example.com")
        _set_role(db_url, "org@example.com", "organizer")
        org = _login(client, "org@example.com")["access_token"]
        _register(client, "mod@example.com")
        _set_role(db_url, "mod@example.com", "moderator")
        mod = _login(client, "mod@example.com")["access_token"]
        _register(client, "vis@example.com")  # посетитель (роль по умолчанию)
        vis = _login(client, "vis@example.com")["access_token"]

        event_id = _published_event(client, org, mod, slug="mod-del-1")
        fav = client.post(
            f"/api/v1/me/favorites/{event_id}", headers=_auth(vis)
        )
        assert fav.status_code == 201

        resp = client.delete(
            f"/api/v1/moderation/events/{event_id}", headers=_auth(mod)
        )
        favs_after = client.get("/api/v1/me/favorites", headers=_auth(vis))

    assert resp.status_code == 204
    assert favs_after.status_code == 200
    assert favs_after.json()["items"] == []


def test_moderation_events_list_filters_by_status(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'mod_list.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "org@example.com")
        _set_role(db_url, "org@example.com", "organizer")
        org = _login(client, "org@example.com")["access_token"]
        _register(client, "mod@example.com")
        _set_role(db_url, "mod@example.com", "moderator")
        mod = _login(client, "mod@example.com")["access_token"]

        client.post("/api/v1/events", json=_event_payload("pending-x"),
                    headers=_auth(org))
        _published_event(client, org, mod, slug="published-x")

        published = client.get(
            "/api/v1/moderation/events?status=published", headers=_auth(mod)
        ).json()
        all_events = client.get(
            "/api/v1/moderation/events", headers=_auth(mod)
        ).json()

    # В БД сидятся демо-события — сравниваем суперсетом, а не точным списком.
    published_slugs = {e["slug"] for e in published["items"]}
    assert {"published-x"} <= published_slugs
    assert "pending-x" not in published_slugs
    assert {"pending-x", "published-x"} <= {e["slug"] for e in all_events["items"]}


def test_organizer_has_no_access_to_moderation_controls(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'mod_forbidden.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "org@example.com")
        _set_role(db_url, "org@example.com", "organizer")
        org = _login(client, "org@example.com")["access_token"]
        _register(client, "mod@example.com")
        _set_role(db_url, "mod@example.com", "moderator")
        mod = _login(client, "mod@example.com")["access_token"]
        event_id = _published_event(client, org, mod, slug="mod-forbidden-1")

        resp_list = client.get("/api/v1/moderation/events", headers=_auth(org))
        resp_patch = client.patch(
            f"/api/v1/moderation/events/{event_id}",
            json={"title": "Хак"},
            headers=_auth(org),
        )
        resp_delete = client.delete(
            f"/api/v1/moderation/events/{event_id}", headers=_auth(org)
        )

    assert resp_list.status_code == 403
    assert resp_patch.status_code == 403
    assert resp_delete.status_code == 403


def test_moderator_controls_return_404_for_missing_event(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'mod_missing.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "mod@example.com")
        _set_role(db_url, "mod@example.com", "moderator")
        mod = _login(client, "mod@example.com")["access_token"]

        resp_patch = client.patch(
            "/api/v1/moderation/events/9999", json={"title": "Нет такого"},
            headers=_auth(mod),
        )
        resp_delete = client.delete(
            "/api/v1/moderation/events/9999", headers=_auth(mod)
        )
        resp_unpub = client.post(
            "/api/v1/moderation/events/9999/unpublish",
            json={"reason": "Нет такого события"},
            headers=_auth(mod),
        )

    assert resp_patch.status_code == 404
    assert resp_delete.status_code == 404
    assert resp_unpub.status_code == 404
