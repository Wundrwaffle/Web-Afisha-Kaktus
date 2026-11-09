"""Подтверждение адреса почты: выпуск ссылки, проверка токена, текст письма.

Схема простая и без JWT: в БД лежит sha256 от токена, сам токен уходит только
в письме. Значит, утечка базы не даёт возможности подтвердить чужой адрес, а
перебор токена бессмысленен — 32 случайных байта.

Токен одноразовый и живёт 24 часа. Запрос нового письма гасит предыдущий
токен: если человек нажал «отправить ещё раз», старая ссылка перестаёт
работать, и неясностей «какая из трёх ссылок настоящая» не возникает.
"""

from __future__ import annotations

import hashlib
import html
import os
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import EmailVerification, User, utcnow

# Сколько живёт ссылка из письма.
TOKEN_TTL = timedelta(hours=24)
# Пауза между письмами одному адресу: защита от «нажму 20 раз».
RESEND_COOLDOWN = timedelta(seconds=60)

STATUS_OK = "ok"
STATUS_EXPIRED = "expired"
STATUS_USED = "used"
STATUS_INVALID = "invalid"


def _hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def issue(session: Session, user: User, now: datetime | None = None) -> str:
    """Выпускает новый токен подтверждения и возвращает его в открытом виде.

    Прежние неиспользованные токены этого пользователя гасятся (used_at), чтобы
    работала ровно одна ссылка — последняя отправленная.
    """
    now = now or utcnow()
    session.execute(
        update(EmailVerification)
        .where(
            EmailVerification.user_id == user.id,
            EmailVerification.used_at.is_(None),
        )
        .values(used_at=now)
    )
    raw = secrets.token_urlsafe(32)
    session.add(
        EmailVerification(
            user_id=user.id,
            email=user.email,
            token_hash=_hash(raw),
            expires_at=now + TOKEN_TTL,
        )
    )
    session.commit()
    return raw


def cooldown_left(session: Session, user_id: int, now: datetime | None = None) -> int:
    """Сколько секунд ещё нельзя отправлять новое письмо (0 — можно)."""
    now = now or utcnow()
    last = session.scalar(
        select(EmailVerification.created_at)
        .where(EmailVerification.user_id == user_id)
        .order_by(EmailVerification.created_at.desc(), EmailVerification.id.desc())
        .limit(1)
    )
    if last is None:
        return 0
    left = RESEND_COOLDOWN - (now - last)
    return max(0, int(left.total_seconds()))


def consume(
    session: Session, raw_token: str, now: datetime | None = None
) -> tuple[User | None, str]:
    """Проверяет токен. Возвращает (пользователь, статус).

    Повторное открытие уже сработавшей ссылки — не ошибка: человек мог нажать
    дважды или обновить страницу. Если адрес уже подтверждён, отдаём ok.
    """
    now = now or utcnow()
    if not raw_token:
        return None, STATUS_INVALID
    row = session.scalar(
        select(EmailVerification).where(EmailVerification.token_hash == _hash(raw_token))
    )
    if row is None:
        return None, STATUS_INVALID
    user = session.get(User, row.user_id)
    if user is None:
        return None, STATUS_INVALID
    if row.email.lower() != user.email.lower():
        # Адрес успели сменить — старая ссылка к нему не относится.
        return user, STATUS_INVALID
    if row.used_at is not None:
        return (user, STATUS_OK) if user.email_verified else (user, STATUS_USED)
    if row.expires_at <= now:
        return user, STATUS_EXPIRED
    row.used_at = now
    user.email_verified = True
    session.commit()
    session.refresh(user)
    return user, STATUS_OK


def site_base(request) -> str:
    """Публичный адрес сайта для ссылок в письмах.

    В продакшене фронтенд и API живут на одном origin (страницу отдаёт бэкенд),
    тогда как за nginx request.base_url — это внутренний адрес. Поэтому основной
    источник — AFISHA_SITE_BASE, а request.base_url остаётся запасным вариантом
    для локального запуска.
    """
    configured = os.environ.get("AFISHA_SITE_BASE", "").strip()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def confirmation_link(base: str, raw_token: str) -> str:
    """Ссылка на подтверждение.

    Токен в #fragment, а не в query: фрагмент не уходит в Referer, поэтому
    токен не утечёт во внешние скрипты (капча Яндекса, счётчики), которые
    успевают загрузиться до того, как страница его прочитает.
    """
    return f"{base}/#verify={raw_token}"


SUBJECT = "Подтвердите почту — Афиша Новокузнецка"


def build_letter(user: User, link: str) -> tuple[str, str, str]:
    """(тема, html, текст). Текстовая часть обязательна: с одним HTML письмо
    чаще попадает в спам, а часть клиентов показывает его пустым."""
    name = html.escape(user.full_name.split()[0] if user.full_name else "привет")
    safe_link = html.escape(link, quote=True)
    email = html.escape(user.email)

    text = (
        f"Здравствуйте, {name}!\n\n"
        "Вы регистрировались на афише Новокузнецка. Осталось подтвердить адрес "
        "электронной почты — перейдите по ссылке:\n\n"
        f"{link}\n\n"
        "Ссылка действует 24 часа и срабатывает один раз. Если вы не "
        "регистрировались, просто удалите это письмо: без подтверждения адрес "
        "не используется.\n\n"
        "— Афиша Новокузнецка"
    )

    body = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f2f5f8;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f2f5f8;padding:28px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border-radius:14px;padding:32px 28px;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#16202b;">
<tr><td style="font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#0079ca;font-weight:700;padding-bottom:14px;">Афиша Новокузнецка</td></tr>
<tr><td style="font-size:22px;font-weight:700;line-height:1.3;padding-bottom:12px;">Подтвердите почту</td></tr>
<tr><td style="font-size:15px;line-height:1.6;color:#3d4a58;padding-bottom:22px;">
Здравствуйте, {name}! Вы указали адрес <b style="color:#16202b;">{email}</b> при регистрации.
Нажмите кнопку — и адрес будет подтверждён. Это нужно, чтобы на него приходили
напоминания о событиях и можно было восстановить доступ.
</td></tr>
<tr><td align="center" style="padding-bottom:22px;">
<a href="{safe_link}" style="display:inline-block;background:#0099ff;color:#ffffff;text-decoration:none;font-size:15px;font-weight:700;padding:14px 28px;border-radius:10px;">Подтвердить почту</a>
</td></tr>
<tr><td style="font-size:13px;line-height:1.6;color:#6b7785;padding-bottom:8px;">
Ссылка действует 24 часа и срабатывает один раз.
</td></tr>
<tr><td style="font-size:13px;line-height:1.6;color:#6b7785;">
Если кнопка не открывается, скопируйте адрес в браузер:<br>
<span style="color:#0079ca;word-break:break-all;">{safe_link}</span>
</td></tr>
<tr><td style="border-top:1px solid #e6ebf1;padding-top:16px;font-size:12px;line-height:1.6;color:#8b97a5;">
Вы получили письмо, потому что этот адрес указан при регистрации на афише
Новокузнецка. Если это не так — удалите письмо, адрес не будет использоваться.
</td></tr>
</table>
</td></tr></table>
</body></html>"""
    return SUBJECT, body, text
