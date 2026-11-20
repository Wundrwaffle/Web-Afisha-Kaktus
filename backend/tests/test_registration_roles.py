"""Разделение регистрации: посетитель и организатор.

Проверяется то, что нельзя увидеть в интерфейсе:
  • логин отдельно от почты (и вход по любому из них);
  • регистрация организатора НЕ даёт роль — только заявку pending;
  • заявку проверяет админ: одобрение меняет роль, отказ оставляет visitor;
  • после отказа можно подать повторно, во время проверки — нельзя;
  • чужие заявки и админские ручки закрыты ролями.
"""

import re
import sqlite3

from fastapi.testclient import TestClient

from app.main import create_app
from app.mail import MailConfig, Mailer

PASSWORD = "password123"
VISITOR = {
    "email": "visitor@example.com",
    "password": PASSWORD,
    "login": "vasya",
    "birth_date": "1990-05-17",
}
ORGANIZER = {
    "email": "org-new@example.com",
    "password": PASSWORD,
    "login": "orgnew",
    "birth_date": "1985-02-03",
    "account_type": "organizer",
    "full_name": "Иванов Иван Иванович",
    "phone": "+79001234567",
    "city": "Новокузнецк",
    "accept_responsibility": True,
    "org_name": "ИП Иванов",
    "inn": "422100000001",
    "social_personal": "https://vk.com/ivanov",
    "social_org": "https://vk.com/ivanov_events",
}


class CaptureMailer(Mailer):
    """Письма складываем в список: сеть в тестах не нужна."""

    def __init__(self):
        super().__init__(MailConfig(from_email="info@example.com", transport="console"))
        self.sent: list[dict[str, str]] = []

    def send(self, *, to: str, subject: str, html: str, text: str = "") -> None:
        self.sent.append({"to": to, "subject": subject, "html": html, "text": text})

    def links(self) -> list[str]:
        pattern = re.compile(r"#verify=([A-Za-z0-9_\-]+)")
        return [
            match.group(1)
            for letter in self.sent
            for match in pattern.finditer(letter["text"])
        ]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _register(client, payload):
    return client.post("/api/v1/auth/register", json=payload)


def _login(client, identifier, password=PASSWORD):
    return client.post(
        "/api/v1/auth/login", json={"login": identifier, "password": password}
    )


def _promote(db_path, email, role):
    """Роль ставим напрямую в БД: админская ручка тут не предмет проверки."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE users SET role = ? WHERE email = ?", (role, email))
        conn.commit()


def _verify(db_path, email):
    """Подтверждение адреса без письма: токен ходит по почте, а она в тесте — список."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE users SET email_verified = 1 WHERE email = ?", (email,))
        conn.commit()


# --- Логин как отдельный идентификатор ------------------------------------


def test_visitor_registers_with_login_and_can_log_in_by_login_or_email(tmp_path):
    db_path = tmp_path / "login.sqlite3"
    with TestClient(create_app(f"sqlite:///{db_path}")) as client:
        created = _register(client, VISITOR)
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["role"] == "visitor"
        assert body["login"] == "vasya"
        assert body["organizer_status"] is None

        by_login = _login(client, "vasya")
        assert by_login.status_code == 200, by_login.text
        by_email = _login(client, VISITOR["email"].upper())
        assert by_email.status_code == 200, by_email.text


def test_second_account_cannot_take_same_login(tmp_path):
    db_path = tmp_path / "login-taken.sqlite3"
    with TestClient(create_app(f"sqlite:///{db_path}")) as client:
        assert _register(client, VISITOR).status_code == 201
        taken = _register(
            client,
            {
                "email": "other@example.com",
                "password": PASSWORD,
                "login": "vasya",
            },
        )
        assert taken.status_code == 409
        assert taken.json()["detail"]["code"] == "login_taken"

        invalid = _register(
            client, {"email": "x@example.com", "password": PASSWORD, "login": "a"}
        )
        # min_length в схеме: слишком короткий логин отсекается до обработчика.
        assert invalid.status_code == 422


def test_older_client_without_login_still_works(tmp_path):
    """Регистрация без логина (старый фронт, скрипты) выводит его из адреса."""
    db_path = tmp_path / "legacy.sqlite3"
    with TestClient(create_app(f"sqlite:///{db_path}")) as client:
        created = _register(
            client,
            {"email": "legacy@example.com", "password": PASSWORD, "full_name": "Тест"},
        )
        assert created.status_code == 201, created.text
        assert created.json()["login"] == "legacy"


# --- Организатор: заявка вместо роли --------------------------------------


def test_organizer_registration_creates_pending_application_not_role(tmp_path):
    db_path = tmp_path / "org-reg.sqlite3"
    with TestClient(create_app(f"sqlite:///{db_path}")) as client:
        created = _register(client, ORGANIZER)
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["role"] == "visitor"
        assert body["organizer_status"] == "pending"
        assert body["full_name"] == ORGANIZER["full_name"]

        # Заявка видна админу в разделе «На верификации» со всеми полями.
        _register(
            client,
            {"email": "boss@example.com", "password": PASSWORD, "login": "boss"},
        )
        _promote(db_path, "boss@example.com", "admin")
        admin = _login(client, "boss").json()["access_token"]
        queue = client.get(
            "/api/v1/admin/organizer-applications", headers=_auth(admin)
        ).json()
        assert queue["total"] == 1
        item = queue["items"][0]
        assert item["status"] == "pending"
        assert item["city"] == "Новокузнецк"
        assert item["inn"] == "422100000001"
        assert item["email"] == ORGANIZER["email"]

        # Роль по-прежнему visitor: до одобрения событий не создаёт.
        token = _login(client, "orgnew").json()["access_token"]
        resp = client.post(
            "/api/v1/events",
            json={
                "title": "Раньше времени",
                "slug": "too-early",
                "category": "Музыка",
                "date": "2030-01-01",
                "time": "18:00",
                "venue": "Клуб",
                "price": "Бесплатно",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 403


def test_organizer_registration_requires_contacts(tmp_path):
    db_path = tmp_path / "org-fields.sqlite3"
    with TestClient(create_app(f"sqlite:///{db_path}")) as client:
        payload = dict(ORGANIZER)
        payload.pop("phone")
        payload["email"] = "no-phone@example.com"
        payload["login"] = "nophone"
        resp = _register(client, payload)
        assert resp.status_code == 400
        assert "телефон" in resp.json()["detail"]["message"]


def test_organizer_registration_without_responsibility_is_refused(tmp_path):
    """Без галочки об ответственности заявка не создаётся вовсе.

    Галочка — входной фильтр, а не строчка для вида: модератор не должен
    вручную выяснять, готов ли человек отвечать за контент. Поэтому отказ
    приходит до создания аккаунта, и в очереди модерации ничего не появляется.
    """
    db_path = tmp_path / "org-responsibility.sqlite3"
    with TestClient(create_app(f"sqlite:///{db_path}")) as client:
        payload = dict(ORGANIZER)
        payload["accept_responsibility"] = False
        resp = _register(client, payload)
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["code"] == "responsibility_required"

        # Аккаунт не создан: входить не под чем.
        assert _login(client, "orgnew").status_code == 401

        # И заявки в очереди нет — модератору разбирать нечего.
        _register(
            client,
            {"email": "boss@example.com", "password": PASSWORD, "login": "boss"},
        )
        _promote(db_path, "boss@example.com", "admin")
        admin = _login(client, "boss").json()["access_token"]
        queue = client.get(
            "/api/v1/admin/organizer-applications", headers=_auth(admin)
        ).json()
        assert queue["total"] == 0

        # С галочкой та же анкета проходит.
        payload["accept_responsibility"] = True
        ok = _register(client, payload)
        assert ok.status_code == 201, ok.text
        assert ok.json()["organizer_status"] == "pending"


# --- Апгрейд посетителя из кабинета ---------------------------------------


def test_visitor_applies_from_cabinet_admin_approves(tmp_path):
    db_path = tmp_path / "upgrade.sqlite3"
    mailer = CaptureMailer()
    with TestClient(create_app(f"sqlite:///{db_path}", mailer=mailer)) as client:
        _register(client, VISITOR)
        _verify(db_path, VISITOR["email"])
        visitor = _login(client, "vasya").json()["access_token"]

        applied = client.post(
            "/api/v1/me/organizer-application",
            json={
                "full_name": "Петров Пётр",
                "phone": "+79005554433",
                "city": "Новокузнецк",
                "accept_responsibility": True,
                "org_name": "Клуб «Кубик»",
            },
            headers=_auth(visitor),
        )
        assert applied.status_code == 201, applied.text
        assert applied.json()["status"] == "pending"

        # Вторая заявка поверх висящей — отказ, а не дубль в админке.
        again = client.post(
            "/api/v1/me/organizer-application",
            json={"full_name": "Петров Пётр", "phone": "+79005554433", "city": "Новокузнецк", "accept_responsibility": True},
            headers=_auth(visitor),
        )
        assert again.status_code == 409
        assert again.json()["detail"]["code"] == "pending"

        _register(
            client,
            {"email": "boss@example.com", "password": PASSWORD, "login": "boss"},
        )
        _promote(db_path, "boss@example.com", "admin")
        # Почта в этом тесте настроена, значит вход без подтверждения адреса
        # закрыт — админ тоже должен быть подтверждён.
        _verify(db_path, "boss@example.com")
        admin = _login(client, "boss").json()["access_token"]
        application_id = client.get(
            "/api/v1/admin/organizer-applications", headers=_auth(admin)
        ).json()["items"][0]["id"]

        approved = client.post(
            f"/api/v1/admin/organizer-applications/{application_id}/review",
            json={"decision": "approve"},
            headers=_auth(admin),
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "approved"

        me = client.get("/api/v1/auth/me", headers=_auth(visitor)).json()
        assert me["role"] == "organizer"
        assert me["organizer_status"] == "approved"

        # Одобрение обязано дойти уведомлением в кабинет (и письмом, если почта есть).
        notes = client.get(
            "/api/v1/me/notifications", headers=_auth(visitor)
        ).json()["items"]
        assert any(n["kind"] == "organizer_approved" for n in notes)


def test_cabinet_application_without_responsibility_is_refused(tmp_path):
    """Посетитель без галочки заявку не подаёт: отказано — ничего не создано.

    Проверяется и то, что согласие сохраняется на заявке: модератор видит в
    карточке, что человек его дал, и не переспрашивает это отдельно.
    """
    db_path = tmp_path / "cabinet-responsibility.sqlite3"
    payload = {
        "full_name": "Петров Пётр",
        "phone": "+79001234433",
        "city": "Новокузнецк",
    }
    with TestClient(create_app(f"sqlite:///{db_path}")) as client:
        _register(client, VISITOR)
        _verify(db_path, VISITOR["email"])
        visitor = _login(client, "vasya").json()["access_token"]

        refused = client.post(
            "/api/v1/me/organizer-application", json=payload, headers=_auth(visitor)
        )
        assert refused.status_code == 400, refused.text
        assert refused.json()["detail"]["code"] == "responsibility_required"
        mine = client.get("/api/v1/me/organizer-application", headers=_auth(visitor))
        assert mine.json()["application"] is None

        accepted = client.post(
            "/api/v1/me/organizer-application",
            json={**payload, "accept_responsibility": True},
            headers=_auth(visitor),
        )
        assert accepted.status_code == 201, accepted.text
        body = accepted.json()
        assert body["responsibility_accepted"] is True
        assert body["responsibility_accepted_at"]


def test_rejected_application_keeps_visitor_and_allows_resubmit(tmp_path):
    db_path = tmp_path / "reject.sqlite3"
    with TestClient(create_app(f"sqlite:///{db_path}")) as client:
        _register(client, VISITOR)
        _verify(db_path, VISITOR["email"])
        visitor = _login(client, "vasya").json()["access_token"]
        client.post(
            "/api/v1/me/organizer-application",
            json={"full_name": "Петров Пётр", "phone": "+79005554433", "city": "Новокузнецк", "accept_responsibility": True},
            headers=_auth(visitor),
        )
        _register(
            client,
            {"email": "boss@example.com", "password": PASSWORD, "login": "boss"},
        )
        _promote(db_path, "boss@example.com", "admin")
        admin = _login(client, "boss").json()["access_token"]
        application_id = client.get(
            "/api/v1/admin/organizer-applications", headers=_auth(admin)
        ).json()["items"][0]["id"]

        # Причина отказа обязательна — это не вкусовщина, а текст для человека.
        empty = client.post(
            f"/api/v1/admin/organizer-applications/{application_id}/review",
            json={"decision": "reject"},
            headers=_auth(admin),
        )
        assert empty.status_code == 400

        rejected = client.post(
            f"/api/v1/admin/organizer-applications/{application_id}/review",
            json={"decision": "reject", "reason": "Не указан ИНН"},
            headers=_auth(admin),
        )
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "rejected"
        assert rejected.json()["note"] == "Не указан ИНН"

        me = client.get("/api/v1/auth/me", headers=_auth(visitor)).json()
        assert me["role"] == "visitor"
        assert me["organizer_status"] == "rejected"

        # Повторная подача после отказа разрешена.
        resubmit = client.post(
            "/api/v1/me/organizer-application",
            json={
                "full_name": "Петров Пётр",
                "phone": "+79005554433",
                "city": "Новокузнецк",
                "accept_responsibility": True,
                "inn": "422100000002",
            },
            headers=_auth(visitor),
        )
        assert resubmit.status_code == 201
        assert resubmit.json()["status"] == "pending"


def test_admin_routes_are_closed_for_visitor(tmp_path):
    db_path = tmp_path / "closed.sqlite3"
    with TestClient(create_app(f"sqlite:///{db_path}")) as client:
        _register(client, VISITOR)
        visitor = _login(client, "vasya").json()["access_token"]
        listed = client.get(
            "/api/v1/admin/organizer-applications", headers=_auth(visitor)
        )
        assert listed.status_code == 403
        my = client.get("/api/v1/me/organizer-application", headers=_auth(visitor))
        assert my.status_code == 200
        assert my.json()["application"] is None


def test_organizer_cannot_apply_twice(tmp_path):
    db_path = tmp_path / "already.sqlite3"
    with TestClient(create_app(f"sqlite:///{db_path}")) as client:
        _register(client, ORGANIZER)
        _promote(db_path, ORGANIZER["email"], "organizer")
        token = _login(client, "orgnew").json()["access_token"]
        resp = client.post(
            "/api/v1/me/organizer-application",
            json={"full_name": "Иванов", "phone": "+79001234567", "city": "Новокузнецк", "accept_responsibility": True},
            headers=_auth(token),
        )
        assert resp.status_code == 409


# --- Входящие уведомления о заявках ---------------------------------------


def _staff_token(client, db_path, email, login, role="admin"):
    """Заводит служебный аккаунт нужной роли и возвращает его токен."""
    _register(client, {"email": email, "password": PASSWORD, "login": login})
    _promote(db_path, email, role)
    return _login(client, login).json()["access_token"]


def _kinds(client, token):
    notes = client.get("/api/v1/me/notifications", headers=_auth(token)).json()["items"]
    return [note["kind"] for note in notes]


def test_organizer_registration_notifies_moderators(tmp_path):
    """Заявка с регистрации должна «прийти» тем, кто её разбирает.

    Разбирает модератор — он и должен получить уведомление. Админ получает
    копию: доступ на платформу его тоже касается.
    """
    db_path = tmp_path / "notify-new.sqlite3"
    with TestClient(create_app(f"sqlite:///{db_path}")) as client:
        admin = _staff_token(client, db_path, "boss@example.com", "boss")
        moderateur = _staff_token(client, db_path, "mod@example.com", "moder", "moderator")

        assert _register(client, ORGANIZER).status_code == 201

        assert "organizer_application" in _kinds(client, moderateur)
        assert "organizer_application" in _kinds(client, admin)


def test_visitor_registration_does_not_notify_admins(tmp_path):
    db_path = tmp_path / "notify-visitor.sqlite3"
    with TestClient(create_app(f"sqlite:///{db_path}")) as client:
        admin = _staff_token(client, db_path, "boss@example.com", "boss")
        assert _register(client, VISITOR).status_code == 201
        assert "organizer_application" not in _kinds(client, admin)


def test_cabinet_application_notifies_staff(tmp_path):
    db_path = tmp_path / "notify-cabinet.sqlite3"
    with TestClient(create_app(f"sqlite:///{db_path}")) as client:
        admin = _staff_token(client, db_path, "boss@example.com", "boss")
        moderateur = _staff_token(client, db_path, "mod@example.com", "moder", "moderator")
        _register(client, VISITOR)
        _verify(db_path, VISITOR["email"])
        visitor = _login(client, "vasya").json()["access_token"]
        applied = client.post(
            "/api/v1/me/organizer-application",
            json={"full_name": "Петров Пётр", "phone": "+79001234433", "city": "Новокузнецк", "accept_responsibility": True},
            headers=_auth(visitor),
        )
        assert applied.status_code == 201, applied.text
        assert "organizer_application" in _kinds(client, moderateur)
        assert "organizer_application" in _kinds(client, admin)


# --- Кто разбирает заявки --------------------------------------------------


def test_moderator_reviews_organizer_applications(tmp_path):
    """Заявки на организатора — работа модератора, а не админа.

    Модератор открывает список и выносит решение: одобрение сразу меняет роль.
    Старый админский путь остаётся синонимом, чтобы не ломать уже написанный
    код и закладки, но штатный — /moderation/.
    """
    db_path = tmp_path / "mod-review.sqlite3"
    with TestClient(create_app(f"sqlite:///{db_path}")) as client:
        _register(client, VISITOR)
        _verify(db_path, VISITOR["email"])
        visitor = _login(client, "vasya").json()["access_token"]
        assert (
            client.post(
                "/api/v1/me/organizer-application",
                json={
                    "full_name": "Петров Пётр",
                    "phone": "+79001234433",
                    "city": "Новокузнецк",
                    "accept_responsibility": True,
                },
                headers=_auth(visitor),
            ).status_code
            == 201
        )
        moderateur = _staff_token(
            client, db_path, "mod@example.com", "moder", "moderator"
        )

        listed = client.get(
            "/api/v1/moderation/organizer-applications", headers=_auth(moderateur)
        )
        assert listed.status_code == 200, listed.text
        application_id = listed.json()["items"][0]["id"]

        approved = client.post(
            f"/api/v1/moderation/organizer-applications/{application_id}/review",
            json={"decision": "approve"},
            headers=_auth(moderateur),
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "approved"

        me = client.get("/api/v1/auth/me", headers=_auth(visitor)).json()
        assert me["role"] == "organizer"
        assert me["organizer_status"] == "approved"
