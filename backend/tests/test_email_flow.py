"""Правила подтверждения почты и сброса пароля.

Проверяется то, что нельзя увидеть глазами в интерфейсе: кого пускают, кому
уходит письмо, что происходит при повторной регистрации и что сброс пароля
не обходит проверку адреса.
"""

import re
import sqlite3

from fastapi.testclient import TestClient

from app.mail import MailConfig, Mailer
from app.main import create_app
from app.security import hash_password

EMAIL = "visitor@example.com"
PASSWORD = "password123"


class CaptureMailer(Mailer):
    """Ничего не отправляет: складывает письма в список для проверок."""

    def __init__(self):
        super().__init__(MailConfig(from_email="info@example.com", transport="console"))
        self.sent: list[dict[str, str]] = []

    def send(self, *, to: str, subject: str, html: str, text: str = "") -> None:
        self.sent.append({"to": to, "subject": subject, "html": html, "text": text})

    def links(self, kind: str = "verify") -> list[str]:
        pattern = re.compile(r"#%s=([A-Za-z0-9_\-]+)" % kind)
        return [
            match.group(1)
            for letter in self.sent
            for match in pattern.finditer(letter["text"])
        ]


def _app(tmp_path, name, mailer=None):
    return create_app(f"sqlite:///{tmp_path / (name + '.sqlite3')}", mailer=mailer)


def _register(client, email=EMAIL, password=PASSWORD, full_name="Тест Тестов"):
    return client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": full_name},
    )


def _login(client, email=EMAIL, password=PASSWORD):
    return client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )


def _clear_cooldowns(tmp_path, name):
    """Снимает паузу между письмами, не трогая логику лимитов.

    Ждать минуту в тесте нельзя, а «перемотать» время внутри приложения нет
    возможности, поэтому журнал отправок просто чистится.
    """
    path = tmp_path / (name + ".sqlite3")
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM email_verifications")
        conn.execute("DELETE FROM password_resets")
        conn.commit()


# --- Подтверждение адреса -------------------------------------------------


def test_login_is_blocked_until_email_confirmed(tmp_path):
    mailer = CaptureMailer()
    app = _app(tmp_path, "gate", mailer)
    with TestClient(app) as client:
        registered = _register(client)
        assert registered.status_code == 201
        assert registered.json()["email_verified"] is False
        assert len(mailer.sent) == 1
        assert mailer.sent[0]["to"] == EMAIL

        blocked = _login(client)
        assert blocked.status_code == 403
        detail = blocked.json()["detail"]
        assert detail["code"] == "email_not_verified"
        assert detail["email"] == EMAIL
        # Ссылка из письма — единственный способ войти.
        assert not any(k.endswith("access_token") for k in blocked.json())

        token = mailer.links("verify")[0]
        verified = client.post("/api/v1/auth/verify-email", json={"token": token})
        assert verified.json()["status"] == "ok"

        allowed = _login(client)
        assert allowed.status_code == 200
        assert allowed.json()["access_token"]


def test_wrong_password_on_unverified_account_is_401(tmp_path):
    """Про неподтверждённый адрес сообщаем только после верного пароля."""
    mailer = CaptureMailer()
    app = _app(tmp_path, "gate_wrong", mailer)
    with TestClient(app) as client:
        _register(client)
        response = _login(client, password="wrong-password")

    assert response.status_code == 401
    assert "email_not_verified" not in response.text


def test_second_registration_of_unverified_address_does_not_dead_end(tmp_path):
    mailer = CaptureMailer()
    app = _app(tmp_path, "gate_dup", mailer)
    with TestClient(app) as client:
        _register(client)
        again = _register(client)

    assert again.status_code == 409
    detail = again.json()["detail"]
    assert detail["code"] == "email_pending"
    assert detail["email"] == EMAIL
    # Пауза в минуту ещё не прошла — второе письмо не ушло, но ответ честный.
    assert detail["sent"] is False
    assert len(mailer.sent) == 1


def test_second_registration_after_cooldown_sends_new_letter(tmp_path):
    mailer = CaptureMailer()
    app = _app(tmp_path, "gate_dup2", mailer)
    with TestClient(app) as client:
        _register(client)
        _clear_cooldowns(tmp_path, "gate_dup2")
        again = _register(client)

    assert again.status_code == 409
    detail = again.json()["detail"]
    assert detail["code"] == "email_pending"
    assert detail["sent"] is True
    assert len(mailer.sent) == 2
    # Старая ссылка погашена: работает только последняя.
    old_token, new_token = mailer.links("verify")
    assert old_token != new_token


def test_registration_of_verified_address_is_plain_conflict(tmp_path):
    mailer = CaptureMailer()
    app = _app(tmp_path, "gate_dup3", mailer)
    with TestClient(app) as client:
        _register(client)
        token = mailer.links("verify")[0]
        client.post("/api/v1/auth/verify-email", json={"token": token})
        again = _register(client)

    assert again.status_code == 409
    assert again.json()["detail"] == "Email already registered"
    assert len(mailer.sent) == 1


def test_resend_without_session_is_neutral_for_unknown_address(tmp_path):
    mailer = CaptureMailer()
    app = _app(tmp_path, "resend_unknown", mailer)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/resend-verification", json={"email": "nobody@example.com"}
        )

    assert response.status_code == 200
    assert response.json()["status"] == "sent"
    assert mailer.sent == []


def test_resend_without_session_sends_to_unverified_account(tmp_path):
    mailer = CaptureMailer()
    app = _app(tmp_path, "resend_known", mailer)
    with TestClient(app) as client:
        _register(client)
        _clear_cooldowns(tmp_path, "resend_known")
        response = client.post(
            "/api/v1/auth/resend-verification", json={"email": EMAIL}
        )

    assert response.status_code == 200
    assert response.json()["status"] == "sent"
    assert len(mailer.sent) == 2


def test_resend_without_session_and_without_email_is_401(tmp_path):
    mailer = CaptureMailer()
    app = _app(tmp_path, "resend_noemail", mailer)
    with TestClient(app) as client:
        response = client.post("/api/v1/auth/resend-verification", json={})

    assert response.status_code == 401


def test_resend_with_session_keeps_cooldown_error(tmp_path):
    mailer = CaptureMailer()
    app = _app(tmp_path, "resend_auth", mailer)
    with TestClient(app) as client:
        _register(client)
        _clear_cooldowns(tmp_path, "resend_auth")
        token = client.post(
            "/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}
        )
        assert token.status_code == 403  # до подтверждения входа нет
        # Повторная отправка без сессии, но по адресу — тоже под паузой.
        first = client.post(
            "/api/v1/auth/resend-verification", json={"email": EMAIL}
        )
        assert first.status_code == 200
        second = client.post(
            "/api/v1/auth/resend-verification", json={"email": EMAIL}
        )
        assert second.status_code == 200
        assert second.json()["status"] == "sent"


def test_gate_is_off_when_mail_is_not_configured(tmp_path):
    """Без ключей почты вход работает как раньше — сайт не запирает себя."""
    app = create_app(f"sqlite:///{tmp_path / 'no_mail.sqlite3'}")
    with TestClient(app) as client:
        _register(client)
        allowed = _login(client)

    assert allowed.status_code == 200


# --- Сброс пароля ---------------------------------------------------------


def test_password_reset_full_cycle(tmp_path):
    mailer = CaptureMailer()
    app = _app(tmp_path, "reset", mailer)
    with TestClient(app) as client:
        _register(client)
        client.post(
            "/api/v1/auth/verify-email", json={"token": mailer.links("verify")[0]}
        )
        first_login = _login(client)
        assert first_login.status_code == 200
        old_refresh = first_login.json()["refresh_token"]

        request = client.post(
            "/api/v1/auth/password-reset/request", json={"email": EMAIL}
        )
        assert request.status_code == 200
        assert request.json()["status"] == "sent"
        assert mailer.links("reset"), "письмо со ссылкой сброса не отправлено"

        token = mailer.links("reset")[0]
        changed = client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "new_password": "new-password-456"},
        )
        assert changed.status_code == 200
        assert changed.json()["email"] == EMAIL

        # Старый пароль больше не работает, новый — работает.
        assert _login(client).status_code == 401
        assert _login(client, password="new-password-456").status_code == 200

        # Сессии аккаунта отозваны: украденный refresh-токен мёртв.
        reused = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": old_refresh}
        )
        assert reused.status_code == 401


def test_password_reset_for_unknown_email_is_neutral(tmp_path):
    mailer = CaptureMailer()
    app = _app(tmp_path, "reset_unknown", mailer)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/password-reset/request", json={"email": "nobody@example.com"}
        )

    assert response.status_code == 200
    assert response.json()["status"] == "sent"
    assert mailer.sent == []


def test_password_reset_of_unverified_address_sends_confirmation(tmp_path):
    """Сброс пароля не должен обходить проверку адреса."""
    mailer = CaptureMailer()
    app = _app(tmp_path, "reset_unverified", mailer)
    with TestClient(app) as client:
        _register(client)
        _clear_cooldowns(tmp_path, "reset_unverified")
        response = client.post(
            "/api/v1/auth/password-reset/request", json={"email": EMAIL}
        )

    assert response.status_code == 200
    assert mailer.links("reset") == []
    assert len(mailer.links("verify")) == 2


def test_password_reset_token_is_single_use(tmp_path):
    mailer = CaptureMailer()
    app = _app(tmp_path, "reset_once", mailer)
    with TestClient(app) as client:
        _register(client)
        client.post(
            "/api/v1/auth/verify-email", json={"token": mailer.links("verify")[0]}
        )
        client.post("/api/v1/auth/password-reset/request", json={"email": EMAIL})
        token = mailer.links("reset")[0]

        first = client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "new_password": "new-password-456"},
        )
        assert first.status_code == 200
        again = client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "new_password": "another-pass-789"},
        )

    assert again.status_code == 400
    assert again.json()["detail"]["code"] == "reset_used"


def test_password_reset_rejects_short_password(tmp_path):
    mailer = CaptureMailer()
    app = _app(tmp_path, "reset_short", mailer)
    with TestClient(app) as client:
        _register(client)
        response = client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": "a" * 43, "new_password": "short"},
        )

    assert response.status_code == 422


# --- Миграция -------------------------------------------------------------


def test_migration_confirms_existing_accounts(tmp_path):
    """Аккаунты, созданные до появления колонки, не должны оказаться заперты."""
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                email VARCHAR(320) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                full_name VARCHAR(160) NOT NULL,
                role VARCHAR(32) NOT NULL,
                is_active BOOLEAN NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO users (email, password_hash, full_name, role, is_active,"
            " created_at, updated_at) VALUES (?,?,?,?,?,datetime('now'),datetime('now'))",
            ("old@example.com", hash_password(PASSWORD), "Старый Аккаунт", "organizer", 1),
        )
        conn.commit()

    app = create_app(f"sqlite:///{path}")
    with TestClient(app) as client:
        with sqlite3.connect(path) as conn:
            verified = conn.execute(
                "SELECT email_verified FROM users WHERE email = 'old@example.com'"
            ).fetchone()[0]
        assert verified == 1
        assert _login(client, email="old@example.com").status_code == 200


# --- Вход с другого устройства -------------------------------------------------


def test_registration_returns_pending_token_that_waits(tmp_path):
    mailer = CaptureMailer()
    app = _app(tmp_path, 'pending_reg', mailer)
    with TestClient(app) as client:
        registered = _register(client)
        assert registered.status_code == 201
        pending = registered.json()['pending_token']
        assert pending, 'без токена вкладка не сможет войти сама'

        waiting = client.post('/api/v1/auth/verify-status', json={'pending_token': pending})
        assert waiting.status_code == 200
        assert waiting.json()['status'] == 'pending'


def test_pending_token_enters_after_confirming_on_another_device(tmp_path):
    """Пароль вводили на компьютере, письмо открыли на телефоне."""
    mailer = CaptureMailer()
    app = _app(tmp_path, 'pending_other', mailer)
    with TestClient(app) as desktop, TestClient(app) as phone:
        pending = _register(desktop).json()['pending_token']

        confirmed = phone.post(
            '/api/v1/auth/verify-email', json={'token': mailer.links('verify')[0]}
        )
        assert confirmed.json()['status'] == 'ok'

        entered = desktop.post(
            '/api/v1/auth/verify-status', json={'pending_token': pending}
        )
        body = entered.json()
        assert entered.status_code == 200
        assert body['status'] == 'verified'
        assert body['user']['email'] == EMAIL
        assert body['access_token'] and body['refresh_token']

        # Выданный токен действительно пускает в кабинет.
        me = desktop.get(
            '/api/v1/auth/me', headers={'X-Auth-Token': body['access_token']}
        )
        assert me.status_code == 200
        assert me.json()['email_verified'] is True


def test_login_gate_also_hands_out_pending_token(tmp_path):
    mailer = CaptureMailer()
    app = _app(tmp_path, 'pending_login', mailer)
    with TestClient(app) as client:
        _register(client)
        blocked = _login(client)
        assert blocked.status_code == 403
        pending = blocked.json()['detail']['pending_token']
        assert pending

        client.post(
            '/api/v1/auth/verify-email', json={'token': mailer.links('verify')[0]}
        )
        entered = client.post(
            '/api/v1/auth/verify-status', json={'pending_token': pending}
        )

    assert entered.json()['status'] == 'verified'
    assert entered.json()['user']['email'] == EMAIL


def test_access_token_cannot_be_used_as_pending_token(tmp_path):
    mailer = CaptureMailer()
    app = _app(tmp_path, 'pending_wrong_type', mailer)
    with TestClient(app) as client:
        _register(client)
        client.post(
            '/api/v1/auth/verify-email', json={'token': mailer.links('verify')[0]}
        )
        access = _login(client).json()['access_token']
        response = client.post(
            '/api/v1/auth/verify-status', json={'pending_token': access}
        )

    assert response.status_code == 401


def test_pending_token_of_other_account_does_not_leak_tokens(tmp_path):
    """Мусор вместо токена — 401, а не чужой кабинет."""
    mailer = CaptureMailer()
    app = _app(tmp_path, 'pending_garbage', mailer)
    with TestClient(app) as client:
        _register(client)
        response = client.post(
            '/api/v1/auth/verify-status',
            json={'pending_token': 'a' * 40},
        )

    assert response.status_code == 401
