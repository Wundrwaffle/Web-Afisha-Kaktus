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


def test_organizer_updates_own_event(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'cab_update.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client)
        _set_role(db_url, "organizer@example.com", "organizer")
        tok = _login(client)["access_token"]
        created = _create_as_organizer(client, tok, slug="to-update")
        event_id = created.json()["event_id"]

        patched = client.patch(
            f"/api/v1/me/events/{event_id}",
            json={"title": "Обновлённое название", "price": "500 ₽"},
            headers=_auth(tok),
        )
    assert patched.status_code == 200
    body = patched.json()
    assert body["title"] == "Обновлённое название"
    assert body["price"] == "500 ₽"
    # Незатронутые поля сохранились.
    assert body["slug"] == "to-update"
    assert body["status"] == "pending_moderation"


def test_organizer_cannot_update_others_event(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'cab_update_other.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "a@example.com")
        _set_role(db_url, "a@example.com", "organizer")
        tok_a = _login(client, "a@example.com")["access_token"]
        created = _create_as_organizer(client, tok_a, slug="owned-by-a")

        _register(client, "b@example.com")
        _set_role(db_url, "b@example.com", "organizer")
        tok_b = _login(client, "b@example.com")["access_token"]

        patched = client.patch(
            f"/api/v1/me/events/{created.json()['event_id']}",
            json={"title": "Чужое"},
            headers=_auth(tok_b),
        )
    # Чужое событие не видно и не редактируется — 404, а не 403 (не раскрываем существование).
    assert patched.status_code == 404


def test_organizer_deletes_own_event(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'cab_delete.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client)
        _set_role(db_url, "organizer@example.com", "organizer")
        tok = _login(client)["access_token"]
        created = _create_as_organizer(client, tok, slug="to-delete")
        event_id = created.json()["event_id"]

        deleted = client.delete(
            f"/api/v1/me/events/{event_id}", headers=_auth(tok)
        )
    assert deleted.status_code == 204
    me = client.get("/api/v1/me/events", headers=_auth(tok))
    assert me.json()["total"] == 0


def test_organizer_cannot_delete_others_event(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'cab_delete_other.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "a@example.com")
        _set_role(db_url, "a@example.com", "organizer")
        tok_a = _login(client, "a@example.com")["access_token"]
        created = _create_as_organizer(client, tok_a, slug="owned-by-a")

        _register(client, "b@example.com")
        _set_role(db_url, "b@example.com", "organizer")
        tok_b = _login(client, "b@example.com")["access_token"]

        deleted = client.delete(
            f"/api/v1/me/events/{created.json()['event_id']}",
            headers=_auth(tok_b),
        )
    assert deleted.status_code == 404


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
            json={"decision": "reject", "reason": "Недостаточно информации о площадке"},
            headers=_auth(mod_tok),
        )
        assert ap.json()["status"] == "published"
        assert rj.json()["status"] == "rejected"

        # Очередь пуста
        q2 = client.get("/api/v1/moderation/queue", headers=_auth(mod_tok))
        assert q2.json()["total"] == 0


def test_reject_requires_reason(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'mod_reject_reason.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "org@example.com")
        _set_role(db_url, "org@example.com", "organizer")
        org_tok = _login(client, "org@example.com")["access_token"]
        ev = _create_as_organizer(client, org_tok, slug="ev-reject")
        event_id = ev.json()["event_id"]

        _register(client, "mod@example.com")
        _set_role(db_url, "mod@example.com", "moderator")
        mod_tok = _login(client, "mod@example.com")["access_token"]

        # Отказ без причины → 422
        no_reason = client.post(
            f"/api/v1/moderation/events/{event_id}/review",
            json={"decision": "reject"},
            headers=_auth(mod_tok),
        )
        assert no_reason.status_code == 422

        # Пустая строка тоже не проходит
        blank = client.post(
            f"/api/v1/moderation/events/{event_id}/review",
            json={"decision": "reject", "reason": "   "},
            headers=_auth(mod_tok),
        )
        assert blank.status_code == 422

        # Событие осталось в очереди (не отсеяно ошибочным отказом)
        q = client.get("/api/v1/moderation/queue", headers=_auth(mod_tok))
        assert q.json()["total"] == 1

        # Корректный отказ с причиной
        ok = client.post(
            f"/api/v1/moderation/events/{event_id}/review",
            json={"decision": "reject", "reason": "Нет фото"},
            headers=_auth(mod_tok),
        )
        assert ok.status_code == 200
        assert ok.json()["status"] == "rejected"
        assert ok.json()["moderation_note"] == "Нет фото"


def test_rejected_reason_visible_to_organizer(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'mod_reason_visible.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "org@example.com")
        _set_role(db_url, "org@example.com", "organizer")
        org_tok = _login(client, "org@example.com")["access_token"]
        ev = _create_as_organizer(client, org_tok, slug="ev-visible")
        event_id = ev.json()["event_id"]

        _register(client, "mod@example.com")
        _set_role(db_url, "mod@example.com", "moderator")
        mod_tok = _login(client, "mod@example.com")["access_token"]
        client.post(
            f"/api/v1/moderation/events/{event_id}/review",
            json={"decision": "reject", "reason": "Дубликат события"},
            headers=_auth(mod_tok),
        )

        # Организатор видит причину в своём списке
        me = client.get("/api/v1/me/events", headers=_auth(org_tok))
        item = next(
            e for e in me.json()["items"] if e["event_id"] == event_id
        )
        assert item["status"] == "rejected"
        assert item["moderation_note"] == "Дубликат события"


def test_approve_clears_note(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'mod_approve_clear.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "org@example.com")
        _set_role(db_url, "org@example.com", "organizer")
        org_tok = _login(client, "org@example.com")["access_token"]
        ev = _create_as_organizer(client, org_tok, slug="ev-clear")
        event_id = ev.json()["event_id"]

        _register(client, "mod@example.com")
        _set_role(db_url, "mod@example.com", "moderator")
        mod_tok = _login(client, "mod@example.com")["access_token"]

        client.post(
            f"/api/v1/moderation/events/{event_id}/review",
            json={"decision": "approve", "reason": "лишнее"},
            headers=_auth(mod_tok),
        )
        q = client.get("/api/v1/moderation/queue", headers=_auth(mod_tok))
        # Одобренное событие ушло из очереди и не несёт заметки
        assert q.json()["total"] == 0
        me = client.get("/api/v1/me/events", headers=_auth(org_tok))
        item = next(e for e in me.json()["items"] if e["event_id"] == event_id)
        assert item["status"] == "published"
        assert item["moderation_note"] is None


def test_organizer_can_unpublish_and_republish(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'cab_publish.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "org@example.com")
        _set_role(db_url, "org@example.com", "organizer")
        org_tok = _login(client, "org@example.com")["access_token"]
        ev = _create_as_organizer(client, org_tok, slug="ev-pub")
        event_id = ev.json()["event_id"]

        # Публикуем через модератора
        _register(client, "mod@example.com")
        _set_role(db_url, "mod@example.com", "moderator")
        mod_tok = _login(client, "mod@example.com")["access_token"]
        client.post(
            f"/api/v1/moderation/events/{event_id}/review",
            json={"decision": "approve"},
            headers=_auth(mod_tok),
        )

        # Снимаем с публикации
        unpub = client.post(
            f"/api/v1/me/events/{event_id}/unpublish", headers=_auth(org_tok)
        )
        assert unpub.status_code == 200
        assert unpub.json()["status"] == "draft"

        # Событие ушло из публичного каталога
        public = client.get("/api/v1/events", params={"search": "ev-pub"})
        assert public.json()["total"] == 0

        # Повторно публикуем
        pub = client.post(
            f"/api/v1/me/events/{event_id}/publish", headers=_auth(org_tok)
        )
        assert pub.status_code == 200
        assert pub.json()["status"] == "published"

        # Снова видно в каталоге
        public2 = client.get("/api/v1/events", params={"search": "ev-pub"})
        assert public2.json()["total"] == 1


def test_unpublish_requires_published_status(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'cab_unpub_gate.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "org@example.com")
        _set_role(db_url, "org@example.com", "organizer")
        org_tok = _login(client, "org@example.com")["access_token"]
        ev = _create_as_organizer(client, org_tok, slug="ev-pending")
        event_id = ev.json()["event_id"]

        # pending_moderation нельзя снять с публикации
        unpub = client.post(
            f"/api/v1/me/events/{event_id}/unpublish", headers=_auth(org_tok)
        )
        assert unpub.status_code == 409


def test_publish_requires_draft_status(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'cab_pub_gate.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "org@example.com")
        _set_role(db_url, "org@example.com", "organizer")
        org_tok = _login(client, "org@example.com")["access_token"]
        ev = _create_as_organizer(client, org_tok, slug="ev-notdraft")
        event_id = ev.json()["event_id"]

        # pending_moderation нельзя опубликовать напрямую (минуя модерацию)
        pub = client.post(
            f"/api/v1/me/events/{event_id}/publish", headers=_auth(org_tok)
        )
        assert pub.status_code == 409


def test_organizer_cannot_publish_others_event(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'cab_pub_other.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "a@example.com")
        _set_role(db_url, "a@example.com", "organizer")
        tok_a = _login(client, "a@example.com")["access_token"]
        created = _create_as_organizer(client, tok_a, slug="owned-by-a")

        _register(client, "b@example.com")
        _set_role(db_url, "b@example.com", "organizer")
        tok_b = _login(client, "b@example.com")["access_token"]

        pub = client.post(
            f"/api/v1/me/events/{created.json()['event_id']}/publish",
            headers=_auth(tok_b),
        )
    assert pub.status_code == 404