"""Отправка писем: подтверждение адреса, дальше — уведомления и напоминания.

Два транспорта, выбор — переменной окружения:

* ``api``     — HTTPS-API smtp.bz (по умолчанию). Ходит по 443, поэтому работает
  там, где исходящие SMTP-порты закрыты (частая история у хостингов);
* ``smtp``    — обычный SMTP (connect.smtp.bz:2525/587/465). Нужен, если API
  недоступно;
* ``console`` — письмо печатается в лог. Для локальной проверки текста и ссылки
  без реальной отправки.

Как включается. Только настройками из окружения (``AFISHA_MAIL_*``): без них
``MailConfig.enabled`` = False, регистрация работает как раньше, письма не
уходят, а маршруты подтверждения отвечают 404 — dev и тесты не меняются.
Та же логика, что у капчи и OAuth.

Секреты (токен API, пароль SMTP) живут только в окружении/``.secrets`` и в git
не попадают.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

import httpx

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.smtp.bz/v1/smtp/send"
DEFAULT_SMTP_HOST = "connect.smtp.bz"
DEFAULT_SMTP_PORT = 2525
DEFAULT_FROM_NAME = "Афиша Новокузнецка"
DEFAULT_TIMEOUT = 15.0


class MailError(Exception):
    """Письмо не ушло. Наружу отдаём короткий текст, подробности — в лог."""


@dataclass(frozen=True)
class MailConfig:
    from_email: str = ""
    from_name: str = DEFAULT_FROM_NAME
    transport: str = "api"
    api_token: str = ""
    api_url: str = DEFAULT_API_URL
    smtp_host: str = DEFAULT_SMTP_HOST
    smtp_port: int = DEFAULT_SMTP_PORT
    smtp_user: str = ""
    smtp_password: str = ""
    # starttls (587) | ssl (465) | none (2525 без шифрования)
    smtp_security: str = "starttls"
    timeout: float = DEFAULT_TIMEOUT

    @property
    def enabled(self) -> bool:
        """Половина настройки — это недонастроенная отправка, поэтому пара ключей."""
        if not self.from_email:
            return False
        if self.transport == "console":
            return True
        if self.transport == "smtp":
            return bool(self.smtp_host and self.smtp_user and self.smtp_password)
        return bool(self.api_token)


def config_from_env() -> MailConfig:
    def number(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        try:
            return int(raw) if raw else default
        except ValueError:
            logger.warning("%s=%r — не число, беру %s", name, raw, default)
            return default

    return MailConfig(
        from_email=os.environ.get("AFISHA_MAIL_FROM", "").strip(),
        from_name=os.environ.get("AFISHA_MAIL_FROM_NAME", DEFAULT_FROM_NAME).strip()
        or DEFAULT_FROM_NAME,
        transport=(os.environ.get("AFISHA_MAIL_TRANSPORT", "api").strip().lower() or "api"),
        api_token=os.environ.get("AFISHA_MAIL_API_TOKEN", "").strip(),
        api_url=os.environ.get("AFISHA_MAIL_API_URL", DEFAULT_API_URL).strip() or DEFAULT_API_URL,
        smtp_host=os.environ.get("AFISHA_MAIL_SMTP_HOST", DEFAULT_SMTP_HOST).strip()
        or DEFAULT_SMTP_HOST,
        smtp_port=number("AFISHA_MAIL_SMTP_PORT", DEFAULT_SMTP_PORT),
        smtp_user=os.environ.get("AFISHA_MAIL_SMTP_USER", "").strip(),
        smtp_password=os.environ.get("AFISHA_MAIL_SMTP_PASSWORD", "").strip(),
        smtp_security=(os.environ.get("AFISHA_MAIL_SMTP_SECURITY", "starttls").strip().lower()
                       or "starttls"),
    )


class Mailer:
    """Базовый отправитель: подклассы умеют только транспорт."""

    def __init__(self, config: MailConfig):
        self.config = config

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def send(self, *, to: str, subject: str, html: str, text: str = "") -> None:  # pragma: no cover
        raise NotImplementedError


class ApiMailer(Mailer):
    """smtp.bz HTTP API: один POST, никаких портов кроме 443."""

    def send(self, *, to: str, subject: str, html: str, text: str = "") -> None:
        payload: dict[str, str] = {
            "name": self.config.from_name,
            "from": self.config.from_email,
            "to": to,
            "subject": subject,
            "html": html,
        }
        if text:
            payload["text"] = text
        try:
            response = httpx.post(
                self.config.api_url,
                json=payload,
                headers={"Authorization": self.config.api_token},
                timeout=self.config.timeout,
            )
        except httpx.HTTPError as exc:
            raise MailError(f"API недоступно: {exc}") from exc
        if response.status_code != 200:
            raise MailError(f"API ответило HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise MailError("API вернуло не JSON") from exc
        if not body.get("result"):
            # В теле бывает описание причины (лимит, заблокированный адрес).
            reason = str(body.get("message") or body.get("error") or "")[:300]
            raise MailError(f"API отказало: {reason or 'без описания'}")
        logger.info("Письмо отправлено %s (messageid=%s)", to, body.get("messageid"))


class SmtpMailer(Mailer):
    """Обычный SMTP: STARTTLS, SSL или без шифрования."""

    def _message(self, to: str, subject: str, html: str, text: str) -> EmailMessage:
        message = EmailMessage()
        message["From"] = f"{self.config.from_name} <{self.config.from_email}>"
        message["To"] = to
        message["Subject"] = subject
        # Текстовая часть обязательна: письмо только из HTML сильнее падает в спам.
        message.set_content(text or "Откройте письмо в почтовом клиенте с поддержкой HTML.")
        message.add_alternative(html, subtype="html")
        return message

    def send(self, *, to: str, subject: str, html: str, text: str = "") -> None:
        message = self._message(to, subject, html, text)
        host, port = self.config.smtp_host, self.config.smtp_port
        try:
            if self.config.smtp_security == "ssl":
                client: smtplib.SMTP = smtplib.SMTP_SSL(
                    host, port, timeout=self.config.timeout,
                    context=ssl.create_default_context(),
                )
            else:
                client = smtplib.SMTP(host, port, timeout=self.config.timeout)
                if self.config.smtp_security == "starttls":
                    client.starttls(context=ssl.create_default_context())
            with client:
                client.login(self.config.smtp_user, self.config.smtp_password)
                client.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"SMTP: {exc}") from exc
        logger.info("Письмо отправлено %s через SMTP %s:%s", to, host, port)


class ConsoleMailer(Mailer):
    """Ничего не отправляет: печатает письмо в лог (локальная проверка)."""

    def send(self, *, to: str, subject: str, html: str, text: str = "") -> None:
        logger.warning(
            "CONSOLE-ПИСЬМО → %s | %s\n%s", to, subject, text or html
        )


def build_mailer(config: MailConfig) -> Mailer | None:
    """Отправитель по настройкам или None, если отправка не настроена."""
    if not config.enabled:
        return None
    if config.transport == "console":
        return ConsoleMailer(config)
    if config.transport == "smtp":
        return SmtpMailer(config)
    if config.transport == "api":
        return ApiMailer(config)
    logger.error("Неизвестный транспорт почты %r — письма отключены", config.transport)
    return None
