"""Публичный (внешний) доступ к афише: гейт, заголовки безопасности, анти-брутфорс.

Локальная разработка и тесты работают как раньше — всё выключается по умолчанию
и включается переменными окружения (см. backend/expose.py и SECURITY.md).

Модель угроз для сценария «показать афишу по ссылке»:
- адрес туннеля публичный: любой, кто его узнал (или перебрал поддомены), получит
  доступ ко всему приложению, включая админку и API → нужен второй барьер;
- по публичному адресу сразу ходят сканеры и боты → нужен лимит на подбор пароля;
- в публичной сети браузер получает документ от чужого хоста → нужны заголовки
  безопасности (CSP, nosniff, запрет фреймов).
"""
import base64
import hmac
import os
import time
from collections import deque

from fastapi import HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp

TRUTHY = {"1", "true", "yes", "on"}


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in TRUTHY


# Путь открыт без гейта: проверка готовности ничего не раскрывает, нужна для
# мониторинга и health-чеков туннеля.
GATE_EXEMPT_PATHS = frozenset({"/api/v1/health"})

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}

# Фронтенд — один index.html с инлайновыми <script>/<style>, поэтому 'unsafe-inline'
# неизбежен без сборки. Это осознанный компромисс для прототипа: CSP здесь защищает
# от подгрузки внешних скриптов, но не от инъекции в сам документ (см. SECURITY.md).
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "img-src 'self' data: https:",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "script-src 'self' 'unsafe-inline'",
    "connect-src 'self'",
])

TOO_MANY_REQUESTS_MESSAGE = "Слишком много попыток. Повторите позже."


def client_ip(request: Request) -> str:
    """IP клиента с учётом прокси.

    За туннелем cloudflared запрос приходит с 127.0.0.1, а реальный адрес клиента
    лежит в CF-Connecting-IP / X-Forwarded-For. Заголовкам верим только как
    дополнительному ключу лимита: подделать их нельзя мимо туннеля, а если и
    подделают — ограничение по конкретному аккаунту всё равно работает.
    """
    for header in ("cf-connecting-ip", "x-forwarded-for"):
        value = request.headers.get(header)
        if value:
            return value.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class SlidingWindowLimiter:
    """In-memory счётчик событий в скользящем окне по ключу.

    Состояние живёт в процессе: для прототипа с одной нодой этого достаточно,
    при нескольких воркерах счётчики нужно выносить в Redis.
    """

    def __init__(self, limit: int, window_seconds: int, message: str = TOO_MANY_REQUESTS_MESSAGE):
        self.limit = limit
        self.window_seconds = window_seconds
        self.message = message
        self._buckets: dict[str, deque[float]] = {}

    def _prune(self, bucket: deque[float], now: float) -> None:
        while bucket and now - bucket[0] > self.window_seconds:
            bucket.popleft()

    def check(self, key: str) -> None:
        """Бросает 429, если лимит по ключу уже исчерпан. Вызывать ДО работы."""
        bucket = self._buckets.get(key)
        if bucket is None:
            return
        now = time.monotonic()
        self._prune(bucket, now)
        if not bucket:
            self._buckets.pop(key, None)
            return
        if len(bucket) >= self.limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=self.message,
                headers={"Retry-After": str(self.window_seconds)},
            )

    def hit(self, key: str) -> None:
        bucket = self._buckets.setdefault(key, deque())
        now = time.monotonic()
        self._prune(bucket, now)
        bucket.append(now)

    def clear(self, key: str) -> None:
        self._buckets.pop(key, None)


class LoginRateLimiter:
    """Анти-брутфорс для /auth/login: два лимита — по аккаунту и по IP.

    Лимит по паре (ip, email) не даёт подбирать пароль к конкретному аккаунту,
    лимит по IP — перебирать много аккаунтов с одного адреса.
    """

    def __init__(
        self,
        max_failures: int = 10,
        window_seconds: int = 300,
        max_failures_per_ip: int = 30,
    ):
        self._by_account = SlidingWindowLimiter(
            max_failures,
            window_seconds,
            "Слишком много неудачных попыток входа. Повторите позже.",
        )
        self._by_ip = SlidingWindowLimiter(
            max_failures_per_ip,
            window_seconds,
            "Слишком много неудачных попыток входа с этого адреса. Повторите позже.",
        )

    @staticmethod
    def _account_key(ip: str, email: str) -> str:
        return f"{ip}|{email.lower()}"

    def check(self, ip: str, email: str) -> None:
        self._by_account.check(self._account_key(ip, email))
        self._by_ip.check(ip)

    def register_failure(self, ip: str, email: str) -> None:
        self._by_account.hit(self._account_key(ip, email))
        self._by_ip.hit(ip)

    def clear_for(self, ip: str, email: str) -> None:
        """Успешный вход снимает лимит по аккаунту (опечатки в пароле — не атака)."""
        self._by_account.clear(self._account_key(ip, email))

    def reset(self) -> None:
        self._by_account._buckets.clear()
        self._by_ip._buckets.clear()


class BasicGateMiddleware(BaseHTTPMiddleware):
    """HTTP Basic как единая «дверь» перед всем приложением.

    Зачем: URL туннеля публичный, а внутри — админка, модерация и API. Гейт
    отсекает случайных прохожих и сканеров ещё до маршрутизации. Это не замена
    аутентификации приложения: он защищает весь сайт целиком (в том числе
    регистрацию), поэтому пароль гейта — отдельный общий секрет для теста.
    """

    def __init__(
        self,
        app: ASGIApp,
        username: str,
        password: str,
        exempt_paths: frozenset[str] | set[str] = frozenset(),
        realm: str = "afisha",
    ):
        super().__init__(app)
        # Сравниваем байты: hmac.compare_digest не принимает str с не-ASCII.
        self.username = username.encode("utf-8")
        self.password = password.encode("utf-8")
        self.exempt_paths = set(exempt_paths)
        self.realm = realm

    def _authorized(self, header: str) -> bool:
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic" or not encoded:
            return False
        try:
            raw = base64.b64decode(encoded.strip(), validate=True)
        except ValueError:
            return False
        # RFC 7617: кодировка — UTF-8, если сервер указал charset (мы указываем),
        # иначе latin-1. Держим оба варианта, чтобы кириллица в пароле работала.
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            decoded = raw.decode("latin-1")
        user, separator, password = decoded.partition(":")
        if not separator:
            return False
        # Оба сравнения выполняются всегда, иначе по времени ответа можно
        # подбирать логин и пароль по отдельности.
        user_ok = hmac.compare_digest(user.encode("utf-8"), self.username)
        password_ok = hmac.compare_digest(password.encode("utf-8"), self.password)
        return user_ok and password_ok

    async def dispatch(self, request: Request, call_next):
        # OPTIONS не гейтим: предполётные CORS-запросы браузера не несут данных.
        if request.method == "OPTIONS" or request.url.path in self.exempt_paths:
            return await call_next(request)
        if not self._authorized(request.headers.get("authorization", "")):
            return PlainTextResponse(
                "Доступ только по логину и паролю из приглашения.",
                status_code=status.HTTP_401_UNAUTHORIZED,
                headers={"WWW-Authenticate": f'Basic realm="{self.realm}", charset="UTF-8"'},
            )
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Заголовки безопасности на все ответы, включая ответы гейта.

    HSTS ставим только когда запрос пришёл по HTTPS (туннель проставляет
    X-Forwarded-Proto): иначе браузер запомнил бы https для localhost и сломал
    локальную разработку.
    """

    def __init__(self, app: ASGIApp, content_security_policy: str | None = None):
        super().__init__(app)
        self.content_security_policy = content_security_policy

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        # 401 от приложения (истёкший Bearer-токен) не должен нести челлендж:
        # Chrome на незнакомую схему в WWW-Authenticate показывает Basic-диалог,
        # и человек вводит туда пароль гейта, который этот 401 не снимает —
        # получается петля из всплывающих окон. Челлендж гейта (Basic) сохраняем.
        challenge = response.headers.get("www-authenticate", "")
        if challenge and not challenge.lower().startswith("basic"):
            del response.headers["www-authenticate"]
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        # Оболочку SPA не кешируем: ссылка на показ остаётся той же, а старый
        # index.html из кеша ломает работу после правок фронтенда.
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers.setdefault("Cache-Control", "no-cache")
        if self.content_security_policy:
            response.headers.setdefault("Content-Security-Policy", self.content_security_policy)
        if request.headers.get("x-forwarded-proto", "").lower() == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response
