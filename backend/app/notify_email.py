"""Письма по уведомлениям кабинета и добор недоставленного.

Зачем отдельный модуль. Уведомление в кабинет видит только тот, кто зашёл на
сайт. Модератор, который ждёт новое событие, и человек, у которого завтра
событие в избранном, могут неделю не открывать кабинет — письмо достаёт их
без захода. Поэтому каждое уведомление в БД получает свою доставку по почте,
а факт отправки фиксируется в самой строке (`emailed_at`).

Правила:

* одно уведомление — не больше одного письма (`emailed_at` ставится после
  успешной отправки);
* отправляем только активным и подтверждённым адресам: неподтверждённому
  адресу писать бессмысленно, а неактивному — вредно;
* сбой почты не отменяет уведомление: строка остаётся с `emailed_at = NULL`,
  попытка считается, и её доберёт следующий запуск (не больше MAX_ATTEMPTS —
  чтобы «адрес не существует» не крутился вечно);
* если почта не настроена вообще, всё молча выключается: уведомления в кабинете
  продолжают работать как раньше.
"""

from __future__ import annotations

import html
import logging
import os
from datetime import datetime

from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.orm import Session

from .mail import MailError, Mailer
from .models import Notification, User, utcnow

logger = logging.getLogger(__name__)

DEFAULT_SITE_BASE = "https://xn--80aaahx3amvmf1c.xn--p1ai"
# Сколько раз пробуем доставить одно письмо, прежде чем сдаться.
MAX_ATTEMPTS = 3
# Сколько писем отправляем за один запуск, если лимит не задан явно.
DEFAULT_LIMIT = 100


def site_base() -> str:
    """Публичный адрес для ссылок в письмах (тот же ключ, что у подтверждения)."""
    configured = os.environ.get("AFISHA_SITE_BASE", "").strip()
    return (configured or DEFAULT_SITE_BASE).rstrip("/")


# --- Тексты писем -----------------------------------------------------------
# kind уведомления → (тема, заголовок, подпись кнопки, куда ведёт кнопка).
# Кнопка ведёт на якорь, который фронт понимает (#notif / #moderation): человек
# попадает сразу на нужный экран, а не на главную.
_ANCHOR_NOTIF = "#notif"
_ANCHOR_MODERATION = "#moderation"

LETTERS: dict[str, tuple[str, str, str, str]] = {
    "event_created": (
        "Новое событие на модерации — «{title}»",
        "Новое событие на модерации",
        "Открыть модерацию",
        _ANCHOR_MODERATION,
    ),
    "event_updated": (
        "Событие изменено — «{title}»",
        "Событие изменено организатором",
        "Открыть модерацию",
        _ANCHOR_MODERATION,
    ),
    "event_resubmitted": (
        "Событие на повторной проверке — «{title}»",
        "Событие на повторной проверке",
        "Открыть модерацию",
        _ANCHOR_MODERATION,
    ),
    "event_approved": (
        "Событие опубликовано — «{title}»",
        "Событие опубликовано",
        "Открыть кабинет",
        _ANCHOR_NOTIF,
    ),
    "event_rejected": (
        "Событие отклонено — «{title}»",
        "Событие не прошло модерацию",
        "Открыть кабинет",
        _ANCHOR_NOTIF,
    ),
    "event_unpublished": (
        "Событие снято с публикации — «{title}»",
        "Событие снято с публикации",
        "Открыть кабинет",
        _ANCHOR_NOTIF,
    ),
    "event_reminder": (
        "Завтра: «{title}»",
        "Событие уже завтра",
        "Посмотреть в афише",
        "",
    ),
}

FOOTNOTE = (
    "Письмо отправлено автоматически — отвечать на него не нужно. "
    "Если что-то непонятно, напишите нам: chetamzavtra@ya.ru."
)


def event_title(notification: Notification) -> str:
    """Название события для темы письма — из текста уведомления.

    Отдельного поля в уведомлении нет: текст собирается шаблоном «Событие
    «…» …», и название в кавычках-ёлочках уже там. Если шаблон поменяют,
    остаётся заголовок уведомления — тема письма всё равно понятная.
    """
    raw = notification.text or ""
    start = raw.find("«")
    end = raw.find("»", start + 1)
    if start != -1 and end > start:
        return raw[start + 1 : end].strip() or notification.title
    return notification.title


def _shell(heading: str, body: str, cta_text: str, cta_url: str) -> str:
    """Общий каркас письма: тот же, что у подтверждения адреса."""
    button = ""
    if cta_url and cta_text:
        button = (
            '<tr><td align="center" style="padding-bottom:22px;">'
            f'<a href="{html.escape(cta_url, quote=True)}" style="display:inline-block;'
            "background:#0099ff;color:#ffffff;text-decoration:none;font-size:15px;"
            "font-weight:700;padding:14px 28px;border-radius:10px;"
            f'">{html.escape(cta_text)}</a></td></tr>'
        )
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f2f5f8;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f2f5f8;padding:28px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border-radius:14px;padding:32px 28px;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#16202b;">
<tr><td style="font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#0079ca;font-weight:700;padding-bottom:14px;">Афиша Новокузнецка</td></tr>
<tr><td style="font-size:22px;font-weight:700;line-height:1.3;padding-bottom:12px;">{html.escape(heading)}</td></tr>
<tr><td style="font-size:15px;line-height:1.6;color:#3d4a58;padding-bottom:22px;">{body}</td></tr>
{button}
<tr><td style="border-top:1px solid #e6ebf1;padding-top:16px;font-size:12px;line-height:1.6;color:#8b97a5;">
{html.escape(FOOTNOTE)}
</td></tr>
</table>
</td></tr></table>
</body></html>"""


def build_letter(notification: Notification, base: str | None = None) -> tuple[str, str, str]:
    """(тема, html, текст) для конкретного уведомления.

    Неизвестный kind не теряем: отправляем универсальное письмо с заголовком и
    текстом уведомления — новый тип уведомления не должен молча не доходить.
    """
    base = (base or site_base()).rstrip("/")
    title = event_title(notification)
    template = LETTERS.get(notification.kind)

    if template is None:
        subject = notification.title
        heading = notification.title
        cta_text, anchor = "Открыть кабинет", _ANCHOR_NOTIF
    else:
        subject_template, heading, cta_text, anchor = template
        subject = subject_template.format(title=title)

    body_text = html.escape(notification.text or "")
    cta_url = f"{base}/{anchor}" if anchor else f"{base}/"

    text = (
        f"{subject}\n\n"
        f"{notification.text}\n\n"
        f"{cta_text}: {cta_url}\n\n"
        f"{FOOTNOTE}\n\n"
        "— Афиша Новокузнецка"
    )
    return subject, _shell(heading, body_text, cta_text, cta_url), text


# --- Доставка ---------------------------------------------------------------


def ensure_schema(engine: Engine) -> None:
    """Добивает колонки доставки в существующую базу.

    Та же прототипная миграция, что в create_app: create_all не умеет ALTER.
    Нужна и задаче-планировщику — она может стартовать раньше, чем сервис
    перезапустится с новым кодом.
    """
    if engine.dialect.name != "sqlite":
        return
    inspector = inspect(engine)
    if "notifications" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("notifications")}
    additions = {
        "emailed_at": "ALTER TABLE notifications ADD COLUMN emailed_at DATETIME",
        "email_attempts": (
            "ALTER TABLE notifications ADD COLUMN email_attempts INTEGER NOT NULL DEFAULT 0"
        ),
        "email_error": "ALTER TABLE notifications ADD COLUMN email_error VARCHAR(300)",
    }
    with engine.begin() as connection:
        for name, statement in additions.items():
            if name not in columns:
                connection.execute(text(statement))


def send_pending(
    session: Session,
    mailer: Mailer | None,
    *,
    limit: int = DEFAULT_LIMIT,
    base: str | None = None,
    notification_ids: list[int] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Отправляет письма по уведомлениям, которых ещё не касались.

    Возвращает счётчики: sent / failed / skipped (нечего отправлять или
    получатель не подходит). Никогда не бросает MailError наружу: сбой почты —
    это запись в строке уведомления и в лог, а не ошибка запроса пользователя.
    """
    stats = {"sent": 0, "failed": 0, "skipped": 0}
    if mailer is None or not mailer.enabled:
        stats["skipped"] = 1
        return stats

    statement = (
        select(Notification, User)
        .join(User, User.id == Notification.user_id)
        .where(
            Notification.emailed_at.is_(None),
            Notification.email_attempts < MAX_ATTEMPTS,
            User.is_active.is_(True),
            User.email_verified.is_(True),
        )
        .order_by(Notification.created_at, Notification.id)
        .limit(max(1, limit))
    )
    if notification_ids is not None:
        if not notification_ids:
            return stats
        statement = statement.where(Notification.id.in_(notification_ids))

    rows = session.execute(statement).all()
    if not rows:
        stats["skipped"] = 1
        return stats

    base_url = (base or site_base()).rstrip("/")
    now = now or utcnow()
    for notification, user in rows:
        subject, html_body, text_body = build_letter(notification, base_url)
        try:
            mailer.send(
                to=user.email, subject=subject, html=html_body, text=text_body
            )
        except MailError as exc:
            notification.email_attempts += 1
            notification.email_error = str(exc)[:300]
            if notification.email_attempts >= MAX_ATTEMPTS:
                logger.error(
                    "Письмо %s для %s не ушло за %s попыток, больше не пробую: %s",
                    notification.kind, user.email, notification.email_attempts, exc,
                )
            else:
                logger.warning(
                    "Письмо %s для %s не ушло (%s), попробую позже",
                    notification.kind, user.email, exc,
                )
            stats["failed"] += 1
        else:
            notification.emailed_at = now
            notification.email_error = None
            stats["sent"] += 1
        session.commit()

    return stats
