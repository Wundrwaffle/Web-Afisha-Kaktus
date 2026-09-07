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


def _reject_event(client, organizer_token, moderator_token, slug):
    created = client.post(
        "/api/v1/events", json=_event_payload(slug), headers=_auth(organizer_token)
    ).json()
    client.post(
        f"/api/v1/moderation/events/{created['event_id']}/review",
        json={"decision": "reject", "reason": "Неверная дата"},
        headers=_auth(moderator_token),
    )
    return created["event_id"]


# --- resubmit (отклонённое → на повторную модерацию) ---

def test_organizer_resubmits_rejected_event(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'resubmit.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "org@example.com")
        _set_role(db_url, "org@example.com", "organizer")
        org = _login(client, "org@example.com")["access_token"]
        _register(client, "mod@example.com")
        _set_role(db_url, "mod@example.com", "moderator")
        mod = _login(client, "mod@example.com")["access_token"]

        event_id = _reject_event(client, org, mod, slug="resubmit-1")
        resp = client.post(
            f"/api/v1/me/events/{event_id}/resubmit", headers=_auth(org)
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending_moderation"
    assert body["moderation_note"] is None  # старая причина снята

    # Событие снова видно модератору в очереди.
    queue = client.get(
        "/api/v1/moderation/events?status=pending_moderation", headers=_auth(mod)
    ).json()
    assert any(e["slug"] == "resubmit-1" for e in queue["items"])


def test_resubmit_requires_rejected_status(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'resubmit_rules.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "org@example.com")
        _set_role(db_url, "org@example.com", "organizer")
        org = _login(client, "org@example.com")["access_token"]

        # pending-событие нельзя resubmit
        pending = client.post(
            "/api/v1/events", json=_event_payload("still-pending"),
            headers=_auth(org),
        ).json()
        resp = client.post(
            f"/api/v1/me/events/{pending['event_id']}/resubmit", headers=_auth(org)
        )

    assert resp.status_code == 409


# --- Админка: список пользователей и смена ролей ---

def test_admin_lists_users(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'admin_list.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "adm@example.com")
        _set_role(db_url, "adm@example.com", "admin")
        adm = _login(client, "adm@example.com")["access_token"]
        _register(client, "vis@example.com")

        resp = client.get("/api/v1/admin/users", headers=_auth(adm))

    assert resp.status_code == 200
    emails = {u["email"] for u in resp.json()["items"]}
    assert {"adm@example.com", "vis@example.com"} <= emails
    # У каждого пользователя есть id, email, full_name, role, is_active, created_at
    sample = resp.json()["items"][0]
    assert {"id", "email", "full_name", "role", "is_active", "created_at"} <= set(sample)


def test_admin_sets_role(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'admin_set.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "adm@example.com")
        _set_role(db_url, "adm@example.com", "admin")
        adm = _login(client, "adm@example.com")["access_token"]
        _register(client, "vis@example.com")
        target = client.get("/api/v1/admin/users", headers=_auth(adm)).json()
        vis = next(u for u in target["items"] if u["email"] == "vis@example.com")

        resp = client.patch(
            f"/api/v1/admin/users/{vis['id']}/role",
            json={"role": "organizer"},
            headers=_auth(adm),
        )

    assert resp.status_code == 200
    assert resp.json()["role"] == "organizer"


def test_admin_cannot_change_own_role(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'admin_self.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "adm@example.com")
        _set_role(db_url, "adm@example.com", "admin")
        adm = _login(client, "adm@example.com")["access_token"]
        me = client.get("/api/v1/auth/me", headers=_auth(adm)).json()

        resp = client.patch(
            f"/api/v1/admin/users/{me['id']}/role",
            json={"role": "visitor"},
            headers=_auth(adm),
        )

    assert resp.status_code == 400


def test_admin_role_validation_and_missing_user(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'admin_validation.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "adm@example.com")
        _set_role(db_url, "adm@example.com", "admin")
        adm = _login(client, "adm@example.com")["access_token"]

        resp_unknown = client.patch(
            "/api/v1/admin/users/1/role",
            json={"role": "superadmin"},
            headers=_auth(adm),
        )
        resp_missing = client.patch(
            "/api/v1/admin/users/9999/role",
            json={"role": "admin"},
            headers=_auth(adm),
        )

    assert resp_unknown.status_code == 422
    assert resp_missing.status_code == 404


def test_admin_endpoints_forbidden_for_non_admin(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'admin_forbidden.sqlite3'}"
    app = create_app(db_url)
    with TestClient(app) as client:
        _register(client, "mod@example.com")
        _set_role(db_url, "mod@example.com", "moderator")
        mod = _login(client, "mod@example.com")["access_token"]

        resp_list = client.get("/api/v1/admin/users", headers=_auth(mod))
        resp_patch = client.patch(
            "/api/v1/admin/users/1/role", json={"role": "admin"}, headers=_auth(mod)
        )

    assert resp_list.status_code == 403
    assert resp_patch.status_code == 403
