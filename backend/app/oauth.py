"""Вход через внешние сервисы (OAuth 2.0 + PKCE): Яндекс ID, дальше VK ID.

Зачем. Человеку не нужно придумывать пароль, а проверку «это действительно он»
(пароль, второй фактор, свою капчу) делает Яндекс. Мы не храним его пароль —
только заводим у себя аккаунт, привязанный к яндексовому id (таблица `identities`).

Как включается. Только парой ключей из окружения (`AFISHA_OAUTH_YANDEX_CLIENT_ID` +
`AFISHA_OAUTH_YANDEX_CLIENT_SECRET`). Без них `/auth/oauth/providers` отдаёт пустой
список, блока кнопок на форме нет, маршруты отвечают 404 — dev, тесты и публичный
показ работают как раньше (та же логика, что у капчи).

Поток — серверный code flow (секрет никогда не покидает сервер):

    GET /auth/oauth/{provider}/start     302 к провайдеру; state и PKCE-verifier —
                                         в HttpOnly-куке на 10 минут
    GET /auth/oauth/{provider}/callback  сверяем state, меняем code на токен
                                         НА СЕРВЕРЕ, читаем профиль, апсертим
                                         User+Identity, выдаём одноразовый
                                         handoff-код и уходим на фронт
                                         /?oauth=ok&handoff=…
    POST /auth/oauth/exchange            handoff → наша пара JWT

Почему токены не в URL возврата: адрес попадает в историю браузера, Referer и
логи прокси. Код одноразовый, живёт 30 секунд и привязан к адресу клиента —
переслать его в чужую сессию нельзя.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import TokenPair, issue_token_pair
from .db import session_dependency
from .exposure import SlidingWindowLimiter, client_ip
from .models import Identity, User
from .security import hash_password

logger = logging.getLogger(__name__)

# Кука со state и PKCE-verifier. Путь узкий: она нужна только колбэку, а на
# остальные запросы незачем её таскать.
STATE_COOKIE = "afisha_oauth_state"
STATE_TTL_SECONDS = 600

# Одноразовый код «провайдер вернул нас на сайт → фронт меняет его на JWT».
HANDOFF_TTL_SECONDS = 30

# Служебный адрес для аккаунта, если провайдер не отдал почту. `.invalid` —
# резервный домен (RFC 2606): он заведомо не существует и не пройдёт валидацию
# почты, то есть «настоящим» адресом его никто не примет.
SERVICE_EMAIL_DOMAIN = "oauth.invalid"


class OAuthError(Exception):
    """Отказ провайдера или несовместимое состояние аккаунта.

    Код (`code`) — короткий машинный признак для редиректа на фронт, текст —
    для лога. Наружу подробности не отдаём: в них бывают токены и адреса.
    """

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class OAuthProfile:
    """Профиль, приведённый к общему виду любым провайдером."""

    provider_user_id: str
    email: str | None = None
    email_verified: bool = False
    display_name: str | None = None
    avatar_url: str | None = None


class OAuthProvider:
    """Адаптер провайдера: три операции, без логики аккаунтов.

    Логика «кто это у нас» живёт в `_upsert_user`, поэтому второй провайдер
    (VK ID) добавляется только наследником этого класса.
    """

    id: str = ""
    title: str = ""
    authorize_endpoint: str = ""
    token_endpoint: str = ""
    profile_endpoint: str = ""
    scope: str = ""

    def __init__(self, client_id: str, client_secret: str, *, timeout: float = 10.0):
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout

    def authorize_url(self, *, redirect_uri: str, state: str, code_challenge: str = "") -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
        if self.scope:
            params["scope"] = self.scope
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        return f"{self.authorize_endpoint}?{urlencode(params)}"

    def exchange_code(self, *, code: str, redirect_uri: str, code_verifier: str = "") -> str:
        """Обмен одноразового кода на access_token провайдера. Только на сервере."""
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": redirect_uri,
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        try:
            response = httpx.post(self.token_endpoint, data=data, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise OAuthError("token_unreachable", f"token endpoint: {exc}") from exc
        if response.status_code != 200:
            # Тело ответа провайдера в лог целиком не пишем: там может быть эхо
            # наших же параметров.
            raise OAuthError("token_rejected", f"token endpoint: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise OAuthError("token_broken", "token endpoint returned non-JSON") from exc
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise OAuthError("token_missing", "token endpoint returned no access_token")
        return token

    def fetch_profile(self, access_token: str) -> OAuthProfile:  # pragma: no cover - адаптер
        raise NotImplementedError


class YandexProvider(OAuthProvider):
    """Яндекс ID: authorize → code → token → /info.

    Про подтверждённость почты: отдельного флага у Яндекса нет. `default_email` —
    основной адрес аккаунта, и добавить в аккаунт неподтверждённый адрес нельзя
    (Яндекс присылает на него код), поэтому он считается подтверждённым: это
    единственный адрес, который мы вообще видим. Если почты в ответе нет —
    `email_verified=False`, привязка к локальному аккаунту по почте запрещена.
    """

    id = "yandex"
    title = "Яндекс"
    authorize_endpoint = "https://oauth.yandex.ru/authorize"
    token_endpoint = "https://oauth.yandex.ru/token"
    profile_endpoint = "https://login.yandex.ru/info"
    scope = "login:email login:info login:avatar"

    def fetch_profile(self, access_token: str) -> OAuthProfile:
        try:
            response = httpx.get(
                self.profile_endpoint,
                params={"format": "json"},
                headers={"Authorization": f"OAuth {access_token}"},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise OAuthError("profile_unreachable", f"info endpoint: {exc}") from exc
        if response.status_code != 200:
            raise OAuthError("profile_rejected", f"info endpoint: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise OAuthError("profile_broken", "info endpoint returned non-JSON") from exc
        provider_user_id = str(payload.get("id") or "").strip()
        if not provider_user_id:
            raise OAuthError("profile_incomplete", "info endpoint returned no id")
        email = str(payload.get("default_email") or "").strip().lower() or None
        avatar_url = None
        avatar_id = str(payload.get("default_avatar_id") or "").strip()
        # is_avatar_empty=True — это автозаглушка, а не портрет человека.
        if avatar_id and not payload.get("is_avatar_empty"):
            avatar_url = f"https://avatars.yandex.net/get-yapic/{avatar_id}/islands-200"
        full_name = (
            str(payload.get("real_name") or "").strip()
            or " ".join(
                part
                for part in (payload.get("first_name"), payload.get("last_name"))
                if str(part or "").strip()
            ).strip()
            or str(payload.get("display_name") or "").strip()
            or str(payload.get("login") or "").strip()
        )
        return OAuthProfile(
            provider_user_id=provider_user_id,
            email=email,
            email_verified=bool(email),
            display_name=full_name or None,
            avatar_url=avatar_url,
        )


class OAuthConfig:
    """Ключи и адреса возврата. Всё из окружения, в git ничего не попадает."""

    def __init__(
        self,
        *,
        yandex_client_id: str = "",
        yandex_client_secret: str = "",
        redirect_base: str = "",
        frontend_base: str = "",
    ):
        self.yandex_client_id = (yandex_client_id or "").strip()
        self.yandex_client_secret = (yandex_client_secret or "").strip()
        # Публичный адрес API (для redirect_uri). Нужен, когда приложение стоит за
        # прокси и само не знает своего внешнего имени; иначе берём из запроса.
        self.redirect_base = (redirect_base or "").strip().rstrip("/")
        # Куда вернуть человека после входа. В dev фронт живёт на другом порту,
        # чем API, поэтому адрес задаётся явно; в проде это тот же origin.
        self.frontend_base = (frontend_base or "").strip().rstrip("/")

    @property
    def yandex_enabled(self) -> bool:
        # Ключи выдаются парой: половина настройки — это недонастроенный вход.
        return bool(self.yandex_client_id and self.yandex_client_secret)


def config_from_env() -> OAuthConfig:
    return OAuthConfig(
        yandex_client_id=os.environ.get("AFISHA_OAUTH_YANDEX_CLIENT_ID", ""),
        yandex_client_secret=os.environ.get("AFISHA_OAUTH_YANDEX_CLIENT_SECRET", ""),
        redirect_base=os.environ.get("AFISHA_OAUTH_REDIRECT_BASE", ""),
        frontend_base=os.environ.get("AFISHA_OAUTH_FRONTEND_BASE", ""),
    )


def build_providers(config: OAuthConfig) -> dict[str, OAuthProvider]:
    """Только полностью настроенные провайдеры: недонастроенных кнопок не бывает."""
    providers: dict[str, OAuthProvider] = {}
    if config.yandex_enabled:
        providers["yandex"] = YandexProvider(
            config.yandex_client_id, config.yandex_client_secret
        )
    return providers


def profile_ids(session: Session, user_id: int) -> list[str]:
    """Способы входа аккаунта: список провайдеров, привязанных к пользователю."""
    rows = session.scalars(
        select(Identity.provider).where(Identity.user_id == user_id)
    ).all()
    return sorted(set(rows))


class HandoffStore:
    """Одноразовые коды обмена на JWT: 30 секунд, один раз, тот же адрес.

    В памяти процесса: переживать перезапуск коду не нужно (он живёт полминуты),
    а нескольких воркеров у прототипа нет.
    """

    def __init__(self, ttl_seconds: int = HANDOFF_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, tuple[TokenPair, str, float]] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        expired = [key for key, (_, _, expires) in self._items.items() if expires <= now]
        for key in expired:
            self._items.pop(key, None)

    def put(self, pair: TokenPair, ip: str | None) -> str:
        code = secrets.token_urlsafe(32)
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            self._items[code] = (pair, ip or "", now + self.ttl_seconds)
        return code

    def take(self, code: str, ip: str | None) -> TokenPair | None:
        """Забирает код. Возвращает пару только тому же адресу и только один раз."""
        with self._lock:
            entry = self._items.pop(code, None)
        if entry is None:
            return None
        pair, owner_ip, expires = entry
        if expires <= time.monotonic():
            return None
        if owner_ip and (ip or "") != owner_ip:
            return None
        return pair


def _request_origin(request: Request) -> str:
    """Внешний origin запроса (с учётом X-Forwarded-* при --proxy-headers)."""
    return str(request.base_url).rstrip("/")


def _is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").lower()
    return forwarded == "https" or request.url.scheme == "https"


def _redirect(base: str, params: dict[str, str]) -> RedirectResponse:
    separator = "&" if "?" in base else "?"
    return RedirectResponse(
        f"{base.rstrip('/')}{separator}{urlencode(params)}", status_code=302
    )


def _service_email(provider_id: str, provider_user_id: str) -> str:
    # Только ASCII и цифры провайдерского id: в адрес не должно попасть ничего,
    # что превратит его в невалидный мусор.
    safe_id = "".join(ch for ch in provider_user_id if ch.isalnum())[:64] or "user"
    return f"{provider_id}+{safe_id}@{SERVICE_EMAIL_DOMAIN}"


def upsert_oauth_user(session: Session, provider_id: str, profile: OAuthProfile) -> User:
    """Найти или завести аккаунт по профилю провайдера.

    Порядок решений:
    1) есть identity(provider, provider_user_id) → это тот же человек;
    2) почта уже занята локальным аккаунтом и провайдер её подтверждает →
       привязываем к существующему (второго аккаунта с тем же email быть не может —
       users.email уникален);
    3) почта занята, но не подтверждена → отказ `email_taken`: иначе любой, кто
       указал чужой адрес у провайдера, получил бы доступ к чужому кабинету;
    4) почты нет вовсе → заводим аккаунт со служебным адресом.
    Новый аккаунт всегда `visitor`: право добавлять события даёт админ, иначе
    любой вошедший заливал бы события на модерацию.
    """
    identity = session.scalar(
        select(Identity).where(
            Identity.provider == provider_id,
            Identity.provider_user_id == profile.provider_user_id,
        )
    )
    if identity is not None:
        user = session.get(User, identity.user_id)
        if user is None or not user.is_active:
            raise OAuthError("account_inactive")
        # Имя и портрет у провайдера могли измениться — обновляем карточку.
        identity.email = profile.email or identity.email
        identity.display_name = profile.display_name or identity.display_name
        identity.avatar_url = profile.avatar_url or identity.avatar_url
        session.commit()
        return user

    email = (profile.email or "").strip().lower()
    user = session.scalar(select(User).where(User.email == email)) if email else None
    if user is not None:
        if not profile.email_verified:
            raise OAuthError("email_taken")
        if not user.is_active:
            raise OAuthError("account_inactive")
    else:
        user = User(
            email=email or _service_email(provider_id, profile.provider_user_id),
            # Случайный пароль: вход по паролю такому аккаунту недоступен, а схема
            # users (password_hash NOT NULL) остаётся прежней.
            password_hash=hash_password(secrets.token_urlsafe(32)),
            full_name=(profile.display_name or "").strip() or "Гость",
            role="visitor",
            is_active=True,
        )
        session.add(user)
        session.flush()

    session.add(
        Identity(
            user_id=user.id,
            provider=provider_id,
            provider_user_id=profile.provider_user_id,
            email=profile.email,
            email_verified=profile.email_verified,
            display_name=profile.display_name,
            avatar_url=profile.avatar_url,
        )
    )
    try:
        session.commit()
    except IntegrityError:
        # Гонка двух колбэков одного человека: второй проиграл — берём готовую
        # привязку вместо 500.
        session.rollback()
        identity = session.scalar(
            select(Identity).where(
                Identity.provider == provider_id,
                Identity.provider_user_id == profile.provider_user_id,
            )
        )
        user = session.get(User, identity.user_id) if identity is not None else None
        if user is None:
            raise OAuthError("identity_race")
    return user


class HandoffRequest(BaseModel):
    handoff: str = Field(min_length=8, max_length=200)


def build_oauth_routes(
    session_factory,
    config: OAuthConfig,
    *,
    gate_active: bool = False,
    providers: dict[str, OAuthProvider] | None = None,
    start_limiter: SlidingWindowLimiter | None = None,
    exchange_limiter: SlidingWindowLimiter | None = None,
):
    """Собирает OAuth-маршруты с привязкой к session_factory из create_app.

    Роутер создаётся локально на каждый вызов (как и auth-роутер): модульный
    singleton оставил бы замыкание на первую БД и «чужие» тесты писали бы не туда.

    `gate_active` — включён Basic-гейт публичного показа. Тогда провайдеров нет
    вовсе: гость после возврата от Яндекса упёрся бы в пароль гейта, поэтому
    кнопки не показываем (и маршруты не работают).
    """
    router = APIRouter(prefix="/api/v1/auth/oauth", tags=["oauth"])
    provider_map = dict(providers) if providers is not None else build_providers(config)
    if gate_active:
        provider_map = {}

    start_limiter = start_limiter or SlidingWindowLimiter(
        limit=30,
        window_seconds=600,
        message="Слишком много попыток входа через внешний сервис. Повторите позже.",
    )
    exchange_limiter = exchange_limiter or SlidingWindowLimiter(
        limit=60,
        window_seconds=600,
        message="Слишком много попыток обмена кода входа. Повторите позже.",
    )
    handoffs = HandoffStore()

    def get_session():
        yield from session_dependency(session_factory)

    def callback_uri(request: Request, provider_id: str) -> str:
        base = config.redirect_base or _request_origin(request)
        return f"{base}/api/v1/auth/oauth/{provider_id}/callback"

    def frontend_base(request: Request) -> str:
        return config.frontend_base or _request_origin(request)

    def provider_or_404(provider_id: str) -> OAuthProvider:
        impl = provider_map.get(provider_id)
        if impl is None:
            raise HTTPException(status_code=404, detail="OAuth provider is not enabled")
        return impl

    @router.get("/providers")
    def oauth_providers() -> list[dict[str, str]]:
        """Что включено: фронт рисует ровно эти кнопки (без ключей — ни одной)."""
        return [{"id": impl.id, "title": impl.title} for impl in provider_map.values()]

    @router.get("/{provider}/start")
    def oauth_start(provider: str, request: Request) -> RedirectResponse:
        ip = client_ip(request)
        start_limiter.check(ip)
        start_limiter.hit(ip)
        impl = provider_or_404(provider)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        url = impl.authorize_url(
            redirect_uri=callback_uri(request, impl.id), state=state, code_challenge=challenge
        )
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(
            STATE_COOKIE,
            f"{state}|{verifier}",
            max_age=STATE_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            # secure на http сломал бы dev на 127.0.0.1: браузер молча не сохранил
            # бы куку, и вход падал бы на сверке state.
            secure=_is_https(request),
            path="/api/v1/auth/oauth",
        )
        return response

    @router.get("/{provider}/callback")
    def oauth_callback(
        provider: str,
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
        session: Session = Depends(get_session),
    ) -> RedirectResponse:
        impl = provider_or_404(provider)
        front = frontend_base(request)
        stored = request.cookies.get(STATE_COOKIE) or ""
        expected_state, _, verifier = stored.partition("|")

        def finish(params: dict[str, str]) -> RedirectResponse:
            response = _redirect(front, params)
            # state одноразовый: кука больше не нужна ни при успехе, ни при отказе.
            response.delete_cookie(STATE_COOKIE, path="/api/v1/auth/oauth")
            return response

        # Человек нажал «Запретить» у провайдера — это не ошибка сервера.
        if error:
            logger.info("OAuth %s: провайдер вернул error=%s", provider, error)
            return finish({"oauth": "error", "reason": "access_denied" if error == "access_denied" else "provider_error"})
        if not code or not state:
            raise HTTPException(status_code=400, detail="OAuth callback is incomplete")
        # Сверка state побайтно и до любого обмена: иначе чужой колбэк заводит
        # аккаунт на нашем сайте (CSRF-вход в чужую сессию).
        if not expected_state or not hmac.compare_digest(expected_state, state):
            logger.warning("OAuth %s: state не совпал с кукой", provider)
            raise HTTPException(status_code=400, detail="OAuth state mismatch")

        try:
            access_token = impl.exchange_code(
                code=code, redirect_uri=callback_uri(request, impl.id), code_verifier=verifier
            )
            profile = impl.fetch_profile(access_token)
            # Токен Яндекса дальше не нужен: профиль забрали — токен выбрасываем.
            user = upsert_oauth_user(session, impl.id, profile)
        except OAuthError as exc:
            logger.warning("OAuth %s: отказ (%s) %s", provider, exc.code, exc)
            reason = exc.code if exc.code in {"email_taken", "account_inactive"} else "provider_failed"
            return finish({"oauth": "error", "reason": reason})

        pair = issue_token_pair(session, user)
        handoff = handoffs.put(pair, client_ip(request))
        logger.info("OAuth %s: вход user_id=%s", provider, user.id)
        return finish({"oauth": "ok", "handoff": handoff})

    @router.post("/exchange", response_model=TokenPair)
    def oauth_exchange(payload: HandoffRequest, request: Request) -> TokenPair:
        ip = client_ip(request)
        exchange_limiter.check(ip)
        exchange_limiter.hit(ip)
        pair = handoffs.take(payload.handoff, ip)
        if pair is None:
            # Один ответ на «нет кода», «истёк», «уже использован» и «чужой IP»:
            # не подсказываем, какой именно случай сработал.
            raise HTTPException(status_code=401, detail="OAuth handoff is invalid or expired")
        return pair

    return {"router": router, "providers": provider_map, "handoffs": handoffs}
