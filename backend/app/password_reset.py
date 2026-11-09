"""Сброс пароля по почте: выпуск ссылки, проверка токена, текст письма.

Устроено так же, как подтверждение адреса (``email_verification``): в БД лежит
только sha256 токена, сам токен уходит лишь в письме. Отличия продиктованы
ценой ошибки:

* ссылка живёт 60 минут, а не сутки: украденный ящик не должен давать доступ
  на весь следующий день;
* после успешной смены пароля вызывающий код отзывает все refresh-токены
  аккаунта — украденная сессия не переживает сброс;
* токен сброса никогда не принимается как токен подтверждения и наоборот:
  это разные таблицы и разные функции.

Сброс **не** обходит требование подтверждённого адреса. Если адрес не
подтверждён, письмо сброса не отправляется — вместо него уходит письмо
подтверждения (см. роут), иначе сброс стал бы способом войти без проверки.
"""

from __future__ import annotations

import hashlib
import html
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import PasswordReset, User, utcnow

# Сколько живёт ссылка сброса. Короче, чем у подтверждения адреса, намеренно.
TOKEN_TTL = timedelta(minutes=60)
# Пауза между письмами одному аккаунту.
RESEND_COOLDOWN = timedelta(seconds=60)

STATUS_OK = "ok"
STATUS_EXPIRED = "expired"
STATUS_USED = "used"
STATUS_INVALID = "invalid"

SUBJECT = "Сброс пароля — Афиша Новокузнецка"


def _hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def issue(session: Session, user: User, now: datetime | None = None) -> str:
    """Выпускает токен сброса и возвращает его в открытом виде.

    Прежние неиспользованные токены гасятся: работает ровно одна ссылка —
    последняя отправленная. Иначе после трёх запросов человек не понимает,
    какая из трёх писем настоящая.
    """
    now = now or utcnow()
    session.execute(
        update(PasswordReset)
        .where(
            PasswordReset.user_id == user.id,
            PasswordReset.used_at.is_(None),
        )
        .values(used_at=now)
    )
    raw = secrets.token_urlsafe(32)
    session.add(
        PasswordReset(
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
        select(PasswordReset.created_at)
        .where(PasswordReset.user_id == user_id)
        .order_by(PasswordReset.created_at.desc(), PasswordReset.id.desc())
        .limit(1)
    )
    if last is None:
        return 0
    left = RESEND_COOLDOWN - (now - last)
    return max(0, int(left.total_seconds()))


def consume(
    session: Session, raw_token: str, now: datetime | None = None
) -> tuple[User | None, str]:
    """Проверяет токен и гасит его. Возвращает (пользователь, статус).

    Пароль здесь НЕ меняется: решение «какой пароль ставить» принимает роут,
    а модуль остаётся про токены. Повторное открытие уже погашенной ссылки —
    не ошибка «не найдено», а именно ``used``: человек должен понять, что
    ссылка одноразовая, а не что она битая.
    """
    now = now or utcnow()
    if not raw_token:
        return None, STATUS_INVALID
    row = session.scalar(
        select(PasswordReset).where(PasswordReset.token_hash == _hash(raw_token))
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
        return user, STATUS_USED
    if row.expires_at <= now:
        return user, STATUS_EXPIRED
    row.used_at = now
    session.commit()
    return user, STATUS_OK


def reset_link(base: str, raw_token: str) -> str:
    """Ссылка на форму нового пароля.

    Токен в #fragment: фрагмент не уходит в Referer и не попадает в логи
    прокси — в отличие от query-параметра.
    """
    return f"{base}/#reset={raw_token}"


def build_letter(user: User, link: str) -> tuple[str, str, str]:
    """(тема, html, текст). Текстовая часть обязательна: с одним HTML письмо
    чаще падает в спам, а часть клиентов показывает его пустым."""
    name = html.escape(user.full_name.split()[0] if user.full_name else "здравствуйте")
    safe_link = html.escape(link, quote=True)
    email = html.escape(user.email)

    text = (
        f"Здравствуйте, {name}!\n\n"
        "Кто-то запросил сброс пароля для аккаунта на афише Новокузнецка.\n"
        "Чтобы задать новый пароль, перейдите по ссылке:\n\n"
        f"{link}\n\n"
        "Ссылка действует 60 минут и срабатывает один раз.\n"
        "Если вы не запрашивали сброс — просто проигнорируйте это письмо,\n"
        "пароль останется прежним.\n\n"
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
<tr><td style="font-size:22px;font-weight:700;line-height:1.3;padding-bottom:12px;">Сброс пароля</td></tr>
<tr><td style="font-size:15px;line-height:1.6;color:#3d4a58;padding-bottom:22px;">
Кто-то запросил сброс пароля для аккаунта <b style="color:#16202b;">{email}</b>.
Нажмите кнопку, чтобы задать новый пароль.
</td></tr>
<tr><td align="center" style="padding-bottom:22px;">
<a href="{safe_link}" style="display:inline-block;background:#0099ff;color:#ffffff;text-decoration:none;font-size:15px;font-weight:700;padding:14px 28px;border-radius:10px;">Задать новый пароль</a>
</td></tr>
<tr><td style="font-size:13px;line-height:1.6;color:#6b7785;padding-bottom:8px;">
Ссылка действует 60 минут и срабатывает один раз.
</td></tr>
<tr><td style="font-size:13px;line-height:1.6;color:#6b7785;">
Если кнопка не открывается, скопируйте адрес в браузер:<br>
<span style="color:#0079ca;word-break:break-all;">{safe_link}</span>
</td></tr>
<tr><td style="border-top:1px solid #e6ebf1;padding-top:16px;font-size:12px;line-height:1.6;color:#8b97a5;">
Если вы не запрашивали сброс — просто проигнорируйте письмо, пароль останется
прежним. Если такие письма приходят без вашего участия, напишите администратору
сайта.
</td></tr>
</table>
</td></tr></table>
</body></html>"""
    return SUBJECT, body, text
