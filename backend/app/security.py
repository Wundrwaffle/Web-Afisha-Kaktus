import hashlib
import os
import secrets
from datetime import datetime, timedelta

from jose import JWTError, jwt
from passlib.context import CryptContext

# Доступ к ролям читаем из модуля моделей во избежание дублирования строковых литералов.
from .models import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 30
ALGORITHM = "HS256"

# Секрет для подписи JWT. По умолчанию генерируется случайно при старте,
# но для development/тестов это безопасно только пока токены не должны
# переживать перезапуск. В production задаётся через окружение.
SECRET_KEY = os.environ.get("AFISHA_SECRET_KEY") or secrets.token_urlsafe(64)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def _now_utc() -> datetime:
    return datetime.utcnow()


def create_access_token(user: User) -> str:
    expire = _now_utc() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "type": "access",
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(user: User) -> tuple[str, str, datetime]:
    """Возвращает (сырой токен, sha256-хеш токена, время истечения).

    В базе хранится только хеш — утечка таблицы не раскрывает работающие токены.
    """
    raw = secrets.token_urlsafe(48)
    expires_at = _now_utc() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    return raw, hash_refresh_token(raw), expires_at


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def decode_access_token(token: str) -> dict:
    """Декодирует access-токен; бросает JWTError при невалидном/истёкшем токене."""
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])