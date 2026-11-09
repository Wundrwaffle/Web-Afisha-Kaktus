"""Письма по уведомлениям и ежедневная задача рассылки.

Проверяем то, что не видно в интерфейсе: письмо уходит один раз, сбой почты не
ломает действие и не теряет уведомление, напоминания создаются задачей для всех
(а не только для того, кто открыл кабинет), неподтверждённым адресам писем нет.
"""

import sqlite3
from datetime import date, time, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import build_engine, build_session_factory
from app.mail import MailConfig, MailError, Mailer
from app.main import create_app
from app.models import Event, Favorite, Notification, User
from app.notify_email import MAX_ATTEMPTS
from app.notify_job import run as run_job

PASSWORD = "password123"


class CaptureMailer(Mailer):
    """Складывает письма в список; умеет «ломаться» по команде."""

    def __init__(self, *, fail: bool = False):
        super().__init__(MailConfig(from_email="info@example.com", transport="console"))
        self.sent: list[dict[str, str]] = []
        self.fail = fail

    def send(self, *, to: str, subject: str, html: str, text: str = "") -> None:
        if self.fail:
            raise MailError("почтовый сервис недоступен")
        self.sent.append({"to": to, "subject": subject, "html": html, "text": text})


def _db(tmp_path, name):
    return f"sqlite:///{tmp_path / (name + '.sqlite3')}"


def _session(db_url):
    return build_session_factory(build_engine(db_url))()


def _register(client, email, full_name="Тест Тестов"):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "full_name": full_name},
    )
    assert response.status_code == 201, response.text
    return response


def _confirm_and_set_role(db_url, email, role):
    """Подтверждаем адрес и ставим роль напрямую: гейт почты иначе не пустит."""
    with _session(db_url) as session:
        user = session.scalar(select(User).where(User.email == email))
        user.email_verified = True
        user.role = role
        session.commit()


def _login(client, email):
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _payload(slug, days_ahead=5):
    return {
        "title": f"Событие {slug}",
        "slug": slug,
        "category": "Культура",
        "date": (date.today() + timedelta(days=days_ahead)).isoformat(),
        "time": "18:00",
        "venue": "Площадка",
        "price": "Бесплатно",
    }


def _setup_org_and_mod(client, db_url, mailer):
    _register(client, "org@example.com")
    _register(client, "mod@example.com")
    _confirm_and_set_role(db_url, "org@example.com", "organizer")
    _confirm_and_set_role(db_url, "mod@example.com", "moderator")
    return _login(client, "org@example.com"), _login(client, "mod@example.com")


def _letters_to(mailer, email):
    return [letter for letter in mailer.sent if letter["to"] == email]


# --- Письма по модерации --------------------------------------------------


def test_approval_emails_organizer_once(tmp_path):
    db_url = _db(tmp_path, "approve")
    mailer = CaptureMailer()
    with TestClient(create_app(db_url, mailer=mailer)) as client:
        org, mod = _setup_org_and_mod(client, db_url, mailer)
        mailer.sent.clear()  # письма подтверждения адреса нам не интересны

        created = client.post(
            "/api/v1/events", json=_payload("good"), headers=_auth(org)
        ).json()
        client.post(
            f"/api/v1/moderation/events/{created['event_id']}/review",
            json={"decision": "approve"},
            headers=_auth(mod),
        )

        letters = _letters_to(mailer, "org@example.com")
        assert len(letters) == 1, "организатору должно уйти одно письмо об одобрении"
        assert "опубликовано" in letters[0]["subject"].lower()
        assert "Событие good" in letters[0]["subject"]
        assert "#notif" in letters[0]["text"], "кнопка должна вести в кабинет"

    with _session(db_url) as session:
        approved = session.scalars(
            select(Notification).where(Notification.kind == "event_approved")
        ).all()
        assert len(approved) == 1
        assert approved[0].emailed_at is not None
        assert approved[0].email_attempts == 0


def test_staff_letter_links_to_moderation(tmp_path):
    db_url = _db(tmp_path, "staff")
    mailer = CaptureMailer()
    with TestClient(create_app(db_url, mailer=mailer)) as client:
        org, _mod = _setup_org_and_mod(client, db_url, mailer)
        mailer.sent.clear()

        client.post("/api/v1/events", json=_payload("fresh"), headers=_auth(org))

        letters = _letters_to(mailer, "mod@example.com")
        assert len(letters) == 1
        assert "модерации" in letters[0]["subject"].lower()
        assert "#moderation" in letters[0]["text"]
        assert "Событие fresh" in letters[0]["text"]


def test_failed_letter_does_not_break_action_and_is_retried(tmp_path):
    db_url = _db(tmp_path, "retry")
    mailer = CaptureMailer(fail=True)
    with TestClient(create_app(db_url, mailer=mailer)) as client:
        org, mod = _setup_org_and_mod(client, db_url, mailer)

        created = client.post(
            "/api/v1/events", json=_payload("retry-me"), headers=_auth(org)
        )
        assert created.status_code == 201, "сбой почты не должен ломать создание"
        event_id = created.json()["event_id"]
        review = client.post(
            f"/api/v1/moderation/events/{event_id}/review",
            json={"decision": "reject", "reason": "Неверная дата"},
            headers=_auth(mod),
        )
        assert review.status_code == 200

    with _session(db_url) as session:
        rejected = session.scalars(
            select(Notification).where(Notification.kind == "event_rejected")
        ).all()
        assert len(rejected) == 1
        assert rejected[0].emailed_at is None, "письмо не ушло — строка ждёт добора"
        assert rejected[0].email_attempts == 1
        assert "недоступен" in (rejected[0].email_error or "")

    # Второй прогон с рабочей почтой добирает письма: модератору о новом событии,
    # организатору об отказе.
    working = CaptureMailer()
    stats = run_job(db_url, mailer=working)
    assert stats["sent"] == 2
    assert _letters_to(working, "org@example.com")
    assert _letters_to(working, "mod@example.com")


def test_attempts_are_capped(tmp_path):
    db_url = _db(tmp_path, "cap")
    mailer = CaptureMailer(fail=True)
    with TestClient(create_app(db_url, mailer=mailer)) as client:
        org, mod = _setup_org_and_mod(client, db_url, mailer)
        created = client.post(
            "/api/v1/events", json=_payload("cap-me"), headers=_auth(org)
        ).json()
        client.post(
            f"/api/v1/moderation/events/{created['event_id']}/review",
            json={"decision": "approve"},
            headers=_auth(mod),
        )

    # Неудачные попытки уже были при создании и одобрении; добиваем до потолка.
    for _ in range(MAX_ATTEMPTS):
        run_job(db_url, mailer=mailer)

    working = CaptureMailer()
    stats = run_job(db_url, mailer=working)
    assert stats["sent"] == 0, "после потолка попыток письма больше не шлём"
    assert not working.sent


# --- Напоминания «завтра» -------------------------------------------------


def _seed_event(db_url, slug, days_ahead=1, status="published"):
    with _session(db_url) as session:
        event = Event(
            title=f"Событие {slug}",
            slug=slug,
            status=status,
            category="Культура",
            date=date.today() + timedelta(days=days_ahead),
            time=time(19, 30),
            venue="Площадка",
            price="Бесплатно",
        )
        session.add(event)
        session.commit()
        return event.id


def _seed_favorite(db_url, email, event_id):
    with _session(db_url) as session:
        user = session.scalar(select(User).where(User.email == email))
        session.add(Favorite(user_id=user.id, event_id=event_id))
        session.commit()


def test_job_creates_reminders_for_everyone_and_emails_them(tmp_path):
    db_url = _db(tmp_path, "reminders")
    mailer = CaptureMailer()
    with TestClient(create_app(db_url)) as client:
        _register(client, "lazy@example.com")
        _register(client, "away@example.com")
    _confirm_and_set_role(db_url, "lazy@example.com", "visitor")
    _confirm_and_set_role(db_url, "away@example.com", "visitor")

    event_id = _seed_event(db_url, "tomorrow")
    _seed_favorite(db_url, "lazy@example.com", event_id)
    _seed_favorite(db_url, "away@example.com", event_id)
    _seed_event(db_url, "later", days_ahead=10)

    stats = run_job(db_url, mailer=mailer)
    assert stats["reminders"] == 2
    assert stats["sent"] == 2
    for email in ("lazy@example.com", "away@example.com"):
        letters = _letters_to(mailer, email)
        assert len(letters) == 1
        assert "Завтра" in letters[0]["subject"]

    # Повторный прогон дублей не делает — и писем, и напоминаний ровно по одному.
    again = run_job(db_url, mailer=mailer)
    assert again["reminders"] == 0
    assert again["sent"] == 0

    with _session(db_url) as session:
        reminders = session.scalars(
            select(Notification).where(Notification.kind == "event_reminder")
        ).all()
        assert len(reminders) == 2
        assert all(n.emailed_at is not None for n in reminders)


def test_unverified_and_inactive_get_no_letter(tmp_path):
    db_url = _db(tmp_path, "skip")
    mailer = CaptureMailer()
    with TestClient(create_app(db_url, mailer=mailer)) as client:
        _register(client, "unverified@example.com")
        _register(client, "blocked@example.com")
    # Письма подтверждения адреса ушли при регистрации — к рассылке они не относятся.
    mailer.sent.clear()
    _confirm_and_set_role(db_url, "blocked@example.com", "visitor")
    with _session(db_url) as session:
        blocked = session.scalar(select(User).where(User.email == "blocked@example.com"))
        blocked.is_active = False
        # Уведомления создаём вручную: обе строки должны остаться без письма.
        for email in ("unverified@example.com", "blocked@example.com"):
            user = session.scalar(select(User).where(User.email == email))
            session.add(
                Notification(
                    user_id=user.id, kind="event_approved", title="Событие опубликовано",
                    text="Событие «Тест» одобрено модератором и опубликовано.",
                )
            )
        session.commit()

    stats = run_job(db_url, mailer=mailer)
    assert stats["sent"] == 0
    assert not mailer.sent


def test_job_without_mail_config_sends_nothing(tmp_path, monkeypatch):
    """Почта не настроена — задача работает и молчит (иначе упадёт на сервере без ключей)."""
    for name in (
        "AFISHA_MAIL_FROM", "AFISHA_MAIL_API_TOKEN", "AFISHA_MAIL_SMTP_USER",
        "AFISHA_MAIL_SMTP_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    db_url = _db(tmp_path, "nomail")
    with TestClient(create_app(db_url)) as client:
        _register(client, "someone@example.com")
    _confirm_and_set_role(db_url, "someone@example.com", "visitor")
    event_id = _seed_event(db_url, "silent")
    _seed_favorite(db_url, "someone@example.com", event_id)

    stats = run_job(db_url)  # mailer=None → соберётся из пустого окружения
    assert stats["reminders"] == 1
    assert stats["sent"] == 0
    assert stats["skipped"] == 1


def test_ignore_unsent_rows_without_recipient(tmp_path):
    """Уведомление без пользователя (удалённый аккаунт) не должно ронять рассылку."""
    db_url = _db(tmp_path, "orphan")
    mailer = CaptureMailer()
    with TestClient(create_app(db_url)) as client:
        _register(client, "gone@example.com")
    with _session(db_url) as session:
        user = session.scalar(select(User).where(User.email == "gone@example.com"))
        session.delete(user)
        session.commit()

    assert run_job(db_url, mailer=mailer)["sent"] == 0


def test_letter_is_readable_without_html(tmp_path):
    """Текстовая часть обязательна: часть клиентов не показывает HTML."""
    db_url = _db(tmp_path, "text")
    mailer = CaptureMailer()
    with TestClient(create_app(db_url, mailer=mailer)) as client:
        org, _mod = _setup_org_and_mod(client, db_url, mailer)
        mailer.sent.clear()
        client.post("/api/v1/events", json=_payload("readable"), headers=_auth(org))

    letter = mailer.sent[0]
    assert letter["text"].strip()
    assert "http" in letter["text"]
    assert "chetamzavtra@ya.ru" in letter["text"]
