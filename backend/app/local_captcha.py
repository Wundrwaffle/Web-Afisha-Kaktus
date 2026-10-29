"""Своя капча: замена виджета Яндекса, когда тот не загрузился.

Зачем вторая проверка. Виджет SmartCaptcha живёт на чужом домене, и его режет
всё, что фильтрует сторонние скрипты: блокировщики рекламы, режим «только
необходимое» в Firefox, корпоративные прокси. Тогда `window.smartCaptcha` не
появляется, форма встаёт молча — человек уходит, а мы не узнаём почему.

Как устроено. Сервер выдаёт одноразовое задание: случайный токен и число
ведущих шестнадцатеричных нулей, которые должен дать SHA-256 от «токен:число».
Браузер перебирает числа, пока не найдёт подходящее (proof-of-work), и
отправляет пару вместе с формой. Сервер пересчитывает ОДИН хеш и сверяет.

Почему работа, а не картинка. Картинку с искажённым текстом распознаёт любой
OCR, а «перетащите ползунок» обходится ботом одной строкой JS. Вычислительная
стоимость — единственный барьер, который нельзя обойти без затрат: решение
стоит ~65 тысяч хешей на клиенте (доли секунды у человека, заметно дороже для
потока спама), проверка — один хеш на сервере.

Границы. Это не «тест на человека», а тест на стоимость. Он не остановит
целенаправленную атаку с фермы браузеров, поэтому живёт только вместе с
лимитами по IP и очередью модерации, а не вместо них.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from .models import LocalCaptchaChallenge, utcnow

# Сколько живёт задание. Столько же времени уходит на заполнение формы.
CHALLENGE_TTL_SECONDS = 300

# Ведущие шестнадцатеричные нули. 4 нуля ≈ 16 бит ≈ 65 536 хешей в среднем:
# доли секунды в браузере, но в тысячу раз дороже для бота, который шлёт формы
# пачками. Поднимать выше 5 не стоит — на слабом телефоне станет заметно.
DEFAULT_DIFFICULTY = 4

# Решение — число в десятичной записи; длиннее просто не бывает.
MAX_NONCE_LENGTH = 24


def hash_for(token: str, nonce: str) -> str:
    """Именно то, что считают обе стороны: hex(SHA-256('токен:число'))."""
    return hashlib.sha256(f"{token}:{nonce}".encode("utf-8")).hexdigest()


def prune_expired(session: Session) -> None:
    """Убирает просроченные задания, чтобы таблица не росла.

    Запас в два TTL: строки нужны и после истечения — по ним понятно, что
    человек открыл форму давно и проверка честно провалилась.
    """
    session.execute(
        delete(LocalCaptchaChallenge).where(
            LocalCaptchaChallenge.created_at
            < utcnow() - timedelta(seconds=CHALLENGE_TTL_SECONDS * 2)
        )
    )


def issue(session: Session, ip: str | None = None) -> dict[str, object]:
    """Новое задание для фронтенда. Ответ уходит клиенту как есть."""
    prune_expired(session)
    challenge = LocalCaptchaChallenge(
        token=secrets.token_urlsafe(24),
        difficulty=DEFAULT_DIFFICULTY,
        ip=(ip or None),
    )
    session.add(challenge)
    session.commit()
    prefix = "0" * challenge.difficulty
    return {
        "token": challenge.token,
        "difficulty": challenge.difficulty,
        # Готовый префикс: фронт не должен сам выводить правило из difficulty.
        "prefix": prefix,
        "algorithm": "sha256-hex-prefix",
        "expires_in": CHALLENGE_TTL_SECONDS,
    }


def verify(
    session: Session,
    token: str | None,
    nonce: str | None,
    ip: str | None = None,
) -> bool:
    """Проверка решения: одноразово, с TTL, с пересчётом хеша.

    IP не сверяем: за мобильным оператором адрес меняется за время заполнения
    формы, и человек получал бы отказ без причины. Одноразовость и TTL дают
    то же покрытие — переслать чужое решение можно только один раз и в течение
    пяти минут.
    """
    token = (token or "").strip()
    nonce = (nonce or "").strip()
    if not token or not nonce or len(nonce) > MAX_NONCE_LENGTH:
        return False
    challenge = session.get(LocalCaptchaChallenge, token)
    if challenge is None or challenge.used_at is not None:
        return False
    if challenge.created_at < utcnow() - timedelta(seconds=CHALLENGE_TTL_SECONDS):
        return False
    if not hash_for(token, nonce).startswith("0" * challenge.difficulty):
        return False
    # Гасим задание до ответа: повторная отправка той же пары не пройдёт.
    challenge.used_at = utcnow()
    session.commit()
    return True
