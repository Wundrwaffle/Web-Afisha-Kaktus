import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader, OAuth2PasswordBearer
from jose import JWTError
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import email_verification, local_captcha, password_reset
from .mail import MailError, Mailer
from .captcha import (
    CAPTCHA_FAILED_MESSAGE,
    CaptchaConfig,
    CaptchaVerifier,
    build_verifier,
    check_captcha,
)
from .db import session_dependency
from .exposure import LoginRateLimiter, SlidingWindowLimiter, client_ip
from .models import Identity, RefreshToken, User, utcnow
from .security import (
    create_access_token,
    create_pending_token,
    create_refresh_token,
    decode_access_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=2, max_length=160)
    # Токен Яндекс SmartCaptcha. Обязателен только когда капча включена ключами;
    # в dev и тестах поля нет — клиенты, которые о нём не знают, не ломаются.
    captcha_token: str | None = None
    # Своя капча вместо виджета: задание и найденное к нему число. Приходят,
    # когда скрипт Яндекса не загрузился (блокировщик, защита от трекеров).
    captcha_local_token: str | None = None
    captcha_local_nonce: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    # Капча на входе: защищает от перебора паролей ботами. Как и в регистрации,
    # поле необязательно — при выключенной проверке клиенты о нём не знают.
    captcha_token: str | None = None
    # Пара своей капчи — замена виджета, если тот заблокирован.
    captcha_local_token: str | None = None
    captcha_local_nonce: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class VerifyEmailRequest(BaseModel):
    # Токен из ссылки письма. Ограничение длины — чтобы не гонять в хеш килобайты.
    token: str = Field(min_length=16, max_length=200)


class ResendVerificationRequest(BaseModel):
    # Адрес для повторного письма. Нужен, когда человек ещё не может войти:
    # после регистрации и после отказа «подтвердите почту» сессии у него нет.
    email: EmailStr | None = None


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PendingTokenRequest(BaseModel):
    # Токен, выданный при регистрации или при отказе «подтвердите почту».
    pending_token: str = Field(min_length=16, max_length=2000)


class PasswordResetConfirm(BaseModel):
    token: str = Field(min_length=16, max_length=200)
    # Тот же минимум, что при регистрации: сброс не должен разрешать пароль,
    # который не прошёл бы регистрацию.
    new_password: str = Field(min_length=8, max_length=128)


class UserOut(BaseModel):
    id: int
    email: str
    full_name: str
    role: str
    is_active: bool
    # Подтверждён ли адрес ссылкой из письма. Фронт по нему показывает баннер
    # и кнопку «Отправить письмо ещё раз».
    email_verified: bool = False
    # Способы входа: пусто — обычный аккаунт с паролем, ["yandex"] — создан
    # через Яндекс ID. Нужно фронту, чтобы не предлагать «забыли пароль» такому
    # аккаунту и показать, как он входит.
    identities: list[str] = []

    model_config = {"from_attributes": True}


class RegisterOut(UserOut):
    """Профиль плюс «ожидающий» токен: по нему это устройство войдёт само,
    как только адрес подтвердят с другого устройства (телефона)."""

    pending_token: str | None = None


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)

logger = logging.getLogger(__name__)

# Токен приложения читаем в первую очередь из X-Auth-Token. Под Basic-гейтом
# заголовок Authorization занят паролем доступа к сайту, и браузер подставляет
# его сам — если SPA положит туда Bearer, гейт не увидит своих кредов и будет
# бесконечно показывать диалог входа. Bearer оставлен для локальной разработки,
# Swagger и тестов.
api_key_scheme = APIKeyHeader(name="X-Auth-Token", auto_error=False)


def app_token(
    api_key: str | None = Depends(api_key_scheme),
    bearer: str | None = Depends(oauth2_scheme),
) -> str | None:
    return api_key or bearer


def serialize_user(user: User, methods: list[str] | None = None) -> UserOut:
    """Профиль для фронта. `methods` — провайдеры, привязанные к аккаунту."""
    data = UserOut.model_validate(user)
    if methods:
        data.identities = methods
    return data


def login_methods(session: Session, user_id: int) -> list[str]:
    """Способы входа аккаунта: список привязанных провайдеров (Яндекс, позже VK)."""
    rows = session.scalars(
        select(Identity.provider).where(Identity.user_id == user_id)
    ).all()
    return sorted(set(rows))


def issue_token_pair(session: Session, user: User) -> TokenPair:
    """Наша пара JWT + запись refresh-токена.

    Общая точка для входа по паролю и для OAuth: строку RefreshToken нельзя
    забыть ни в одном из путей, иначе сессия «умирает» на первом обновлении.
    """
    refresh_raw, refresh_hash, expires_at = create_refresh_token(user)
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=refresh_hash,
            expires_at=expires_at,
        )
    )
    session.commit()
    return TokenPair(
        access_token=create_access_token(user),
        refresh_token=refresh_raw,
    )


def build_auth_routes(
    session_factory,
    login_limiter: LoginRateLimiter | None = None,
    register_limiter: SlidingWindowLimiter | None = None,
    captcha: CaptchaConfig | None = None,
    captcha_verifier: CaptchaVerifier | None = None,
    mailer: Mailer | None = None,
    require_verified_email: bool = False,
):
    """Собирает auth-эндпоинты с привязкой к session_factory из create_app.

    Роутер создаётся ЛОКАЛЬНО на каждый вызов — иначе модульный singleton-роутер
    оставляет замыкание get_session от первого create_app(), и повторные вызовы
    (например, в тестах с отдельной БД) пишут в «чужую» базу.

    Лимитеры передаются снаружи (создаются в create_app), чтобы состояние не
    переезжало между приложениями/тестами. Капча включается только парой ключей;
    проверяльщик инжектится в тестах, в проде — обычный HTTP к Яндексу.
    """
    router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

    captcha_enabled = captcha is not None and captcha.enabled
    if captcha_enabled and captcha_verifier is None:
        # Забыть проверяльщик при включённых ключах = пропускать всех подряд,
        # поэтому страхуемся здесь, а не в вызывающем коде.
        captcha_verifier = build_verifier(captcha)

    # Отправка писем включается настройками почты. Без них регистрация работает
    # как раньше: аккаунт создаётся, письма нет, маршруты подтверждения — 404.
    mail_enabled = mailer is not None and mailer.enabled

    # Лимит на письма с одного IP: форма повторной отправки — бесплатный
    # генератор спама на чужой ящик. 20 в час, а не 5: за одним адресом сидят
    # кафе и небольшие офисы — у них один NAT и общий счётчик. Пауза на
    # конкретный адрес при этом остаётся отдельной — минута.
    resend_limiter = SlidingWindowLimiter(
        limit=20,
        window_seconds=3600,
        message="Слишком много писем с этого адреса. Попробуйте позже.",
    )
    # Сброс пароля: свой счётчик, чтобы запросы сброса не тратили бюджет писем
    # подтверждения (и наоборот). Попытки с неверным токеном ограничены тоже:
    # 32 случайных байта перебрать нереально, но лишний запрос ни к чему.
    reset_limiter = SlidingWindowLimiter(
        limit=20,
        window_seconds=3600,
        message="Слишком много запросов сброса пароля. Попробуйте позже.",
    )
    reset_confirm_limiter = SlidingWindowLimiter(
        limit=20,
        window_seconds=3600,
        message="Слишком много попыток. Попробуйте позже.",
    )

    def send_confirmation(session: Session, user: User, request: Request) -> bool:
        """Выпускает ссылку и отправляет письмо. False — письмо не ушло.

        Ошибку отправки не превращаем в ошибку регистрации: аккаунт уже создан,
        и человек не должен видеть «регистрация не удалась» из-за почтового
        сервиса. Причину пишем в лог и показываем при повторной отправке.
        """
        raw = email_verification.issue(session, user)
        link = email_verification.confirmation_link(
            email_verification.site_base(request), raw
        )
        subject, html_body, text_body = email_verification.build_letter(user, link)
        try:
            mailer.send(to=user.email, subject=subject, html=html_body, text=text_body)
        except MailError:
            logger.exception("Не удалось отправить подтверждение на %s", user.email)
            return False
        return True

    # Задание своей капчи выдаётся по запросу: без лимита это бесплатный способ
    # забить таблицу строками. Щедро — человеку хватает одного-двух заданий.
    challenge_limiter = SlidingWindowLimiter(
        limit=60,
        window_seconds=600,
        message="Слишком много запросов проверки. Подождите немного.",
    )

    def captcha_passed(payload: BaseModel, ip: str, session: Session) -> bool:
        """Общая проверка для форм входа и регистрации.

        Порядок: сначала токен виджета Яндекса, затем пара своей капчи. Если
        виджет не загрузился (его срезал блокировщик), человек прошёл нашу
        проверку — этого достаточно, чтобы отправить форму.
        """
        return check_captcha(
            captcha_verifier,
            session,
            smart_token=getattr(payload, "captcha_token", None),
            local_token=getattr(payload, "captcha_local_token", None),
            local_nonce=getattr(payload, "captcha_local_nonce", None),
            ip=ip,
        )

    def get_session():
        yield from session_dependency(session_factory)

    def get_current_user(
        token: str | None = Depends(app_token),
        session: Session = Depends(get_session),
    ) -> User:
        if token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
            )
        try:
            payload = decode_access_token(token)
        except JWTError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
            )
        if payload.get("type") != "access":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token type",
            )
        user_id = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid token payload")
        user = session.get(User, int(user_id))
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="User not found or inactive")
        return user

    def get_optional_user(
        token: str | None = Depends(app_token),
        session: Session = Depends(get_session),
    ) -> User | None:
        """Пользователь, если токен пришёл и он валиден; иначе None.

        Нужен повторной отправке письма: сразу после регистрации сессии ещё
        нет, а в кабинет до подтверждения адреса не пускают. Невалидный токен
        здесь не 401 — письмо можно запросить просто по адресу.
        """
        if not token:
            return None
        try:
            payload = decode_access_token(token)
        except JWTError:
            return None
        if payload.get("type") != "access" or payload.get("sub") is None:
            return None
        user = session.get(User, int(payload["sub"]))
        if user is None or not user.is_active:
            return None
        return user

    def require_role(*roles: str):
        def dependency(current_user: User = Depends(get_current_user)) -> User:
            if current_user.role not in roles:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Insufficient permissions",
                )
            return current_user

        return dependency

    def _create_token_pair(session: Session, user: User) -> TokenPair:
        return issue_token_pair(session, user)

    @router.get("/captcha")
    def captcha_settings() -> dict[str, object]:
        """Конфигурация виджета для фронтенда.

        Ключ сервера наружу не отдаётся никогда: он только в окружении и нужен
        лишь для запроса validate. Без пары ключей фронт узнаёт, что капчи нет,
        и не грузит внешний скрипт.

        `local_fallback` — наша капча. Она включается не отдельной настройкой,
        а самим фактом включённой проверки: это замена виджета для тех, кому
        блокировщик срезал скрипт Яндекса, а не второй уровень защиты.
        """
        return {
            "enabled": captcha_enabled,
            "client_key": captcha.client_key if captcha_enabled else "",
            "local_fallback": captcha_enabled,
        }

    @router.post("/captcha/challenge")
    def captcha_challenge(
        request: Request,
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        """Задание своей капчи: токен + сколько нулей должен дать SHA-256.

        Вызывается с фронта, когда внешний виджет не загрузился. При выключенной
        капче отвечаем 404: формам в этом режиме проверка не нужна, и лишний
        публичный эндпоинт ни к чему.
        """
        if not captcha_enabled:
            raise HTTPException(status_code=404, detail="Captcha is disabled")
        ip = client_ip(request)
        # Лимит до выдачи: иначе один скрипт наполнит таблицу заданиями.
        challenge_limiter.check(ip)
        challenge_limiter.hit(ip)
        return local_captcha.issue(session, ip)

    @router.post("/register", response_model=RegisterOut, status_code=201)
    def register(
        payload: RegisterRequest,
        request: Request,
        session: Session = Depends(get_session),
    ) -> RegisterOut:
        if register_limiter is not None:
            # Защита от массовой регистрации с одного адреса при публичном доступе.
            register_limiter.check(client_ip(request))
        if captcha_enabled and not captcha_passed(
            payload, client_ip(request), session
        ):
            # 400, а не 403: это не отказ в доступе, а непройденная проверка —
            # фронт сбрасывает виджет и предлагает пройти её заново.
            raise HTTPException(status_code=400, detail=CAPTCHA_FAILED_MESSAGE)
        email = payload.email.lower()
        existing = session.scalar(select(User).where(User.email == email))
        if existing is not None:
            # Адрес занят неподтверждённым аккаунтом. Отказ 409 здесь — тупик:
            # пароль человек помнит, письмо потерял, зарегистрироваться заново
            # не может. Поэтому отправляем новое письмо (старая ссылка гаснет),
            # а в detail кладём код, по которому фронт покажет экран «проверьте
            # почту» вместо красной ошибки.
            if mail_enabled and not existing.email_verified and existing.is_active:
                ip = client_ip(request)
                sent = False
                if email_verification.cooldown_left(session, existing.id) == 0:
                    resend_limiter.check(ip)
                    resend_limiter.hit(ip)
                    sent = send_confirmation(session, existing, request)
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "email_pending",
                        "email": existing.email,
                        "sent": sent,
                        "message": (
                            f"Мы отправили новое письмо на {existing.email}."
                            if sent
                            else f"Письмо уже отправлено на {existing.email}. "
                                 "Проверьте почту и папку «Спам»."
                        ),
                    },
                )
            raise HTTPException(status_code=409, detail="Email already registered")
        user = User(
            email=email,
            password_hash=hash_password(payload.password),
            full_name=payload.full_name,
            role="visitor",
        )
        session.add(user)
        try:
            session.commit()
        except IntegrityError:
            # Гонка: параллельная регистрация с тем же email.
            session.rollback()
            raise HTTPException(status_code=409, detail="Email already registered")
        session.refresh(user)
        if register_limiter is not None:
            register_limiter.hit(client_ip(request))
        pending_token = None
        if mail_enabled:
            # Аккаунт уже создан: сбой отправки не должен отменять регистрацию.
            send_confirmation(session, user, request)
            # Токен нужен экрану «проверьте почту»: если адрес подтвердят с
            # телефона, эта вкладка войдёт сама, без повторного ввода пароля.
            if not user.email_verified:
                pending_token = create_pending_token(user)
        return RegisterOut(**serialize_user(user).model_dump(), pending_token=pending_token)

    @router.post("/login", response_model=TokenPair)
    def login(
        payload: LoginRequest,
        request: Request,
        session: Session = Depends(get_session),
    ) -> TokenPair:
        email = payload.email.lower()
        ip = client_ip(request)
        # Лимит проверяется ДО проверки пароля: иначе перебор не остановить.
        if login_limiter is not None:
            login_limiter.check(ip, email)
        # Капча — тоже до пароля: бот не должен даже узнавать, есть ли аккаунт.
        if captcha_enabled and not captcha_passed(payload, ip, session):
            raise HTTPException(status_code=400, detail=CAPTCHA_FAILED_MESSAGE)
        user = session.scalar(select(User).where(User.email == email))
        if user is None or not verify_password(payload.password, user.password_hash):
            if login_limiter is not None:
                login_limiter.register_failure(ip, email)
            # Аккаунт, созданный через Яндекс ID, пароля не имеет (в password_hash
            # лежит случайный хеш). Без подсказки человек ищет «забыли пароль»
            # и не понимает, почему вход не работает.
            methods = login_methods(session, user.id) if user is not None else []
            if methods:
                raise HTTPException(
                    status_code=401,
                    detail=(
                        "Этот аккаунт создан через внешний сервис. "
                        "Войдите кнопкой «Продолжить с Яндекс»."
                    ),
                )
            raise HTTPException(status_code=401, detail="Invalid email or password")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account is deactivated")
        if mail_enabled and not user.email_verified:
            # Пароль верный, но адрес не подтверждён. Это не 401 (человек решил
            # бы, что забыл пароль) и не голый 403: в detail — код, по которому
            # фронт покажет экран с кнопкой повторной отправки.
            fresh = False
            if email_verification.cooldown_left(session, user.id) == 0:
                fresh = send_confirmation(session, user, request)
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "email_not_verified",
                    "email": user.email,
                    "sent": fresh,
                    # Этой вкладке не нужно ждать пароля заново: как только
                    # письмо подтвердят (хоть на телефоне), она войдёт сама.
                    "pending_token": create_pending_token(user),
                    "message": (
                        f"Подтвердите почту — без этого вход закрыт. "
                        + (
                            f"Письмо отправлено на {user.email}."
                            if fresh
                            else f"Проверьте письмо на {user.email} или отправьте его ещё раз."
                        )
                    ),
                },
            )
        if login_limiter is not None:
            # Успешный вход обнуляет счётчик по аккаунту: опечатки не должны
            # запирать владельца, а лимит по IP продолжает накапливаться.
            login_limiter.clear_for(ip, email)
        return _create_token_pair(session, user)

    @router.post("/refresh", response_model=TokenPair)
    def refresh(payload: RefreshRequest, session: Session = Depends(get_session)) -> TokenPair:
        token_hash = hash_refresh_token(payload.refresh_token)
        stored = session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        now = datetime.utcnow()
        if stored is None or stored.revoked_at is not None:
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        if stored.expires_at <= now:
            raise HTTPException(status_code=401, detail="Refresh token expired")

        # Ротация: старый refresh-токен отзывается, выдаётся новый.
        stored.revoked_at = now
        user = session.get(User, stored.user_id)
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="User not found or inactive")
        session.commit()
        return _create_token_pair(session, user)

    @router.post("/logout")
    def logout(
        payload: RefreshRequest,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ) -> dict[str, str]:
        token_hash = hash_refresh_token(payload.refresh_token)
        stored = session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        if stored is not None and stored.user_id == current_user.id:
            stored.revoked_at = datetime.utcnow()
            session.commit()
        return {"status": "ok"}

    @router.get("/me", response_model=UserOut)
    def me(
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ) -> UserOut:
        return serialize_user(current_user, login_methods(session, current_user.id))

    # --- Подтверждение почты ---
    @router.get("/mail")
    def mail_settings() -> dict[str, object]:
        """Настроена ли отправка писем — фронт по флагу решает, показывать ли
        «проверьте почту» и баннер в кабинете.

        Как и /captcha: наружу только факт включения, никаких ключей.
        `require_verified` — включает ли сервер требование подтверждённого
        адреса для публикации событий; фронт заранее предупреждает.
        """
        return {
            "enabled": mail_enabled,
            "require_verified": bool(mail_enabled and require_verified_email),
        }

    @router.post("/verify-email")
    def verify_email(
        payload: VerifyEmailRequest,
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        """Гасит токен из письма. Ссылку открывают в любом браузере, поэтому
        авторизация здесь не нужна — достаточно самого токена.

        Ответ всегда 200 со статусом: фронту нужно показать разный текст для
        «подтверждено», «ссылка истекла» и «ссылка уже использована», а не один
        общий «ошибка».
        """
        if not mail_enabled:
            raise HTTPException(status_code=404, detail="Email confirmation is disabled")
        user, result = email_verification.consume(session, payload.token)
        body: dict[str, object] = {"status": result, "user": None}
        if user is not None:
            body["user"] = serialize_user(user, login_methods(session, user.id)).model_dump()
        return body

    @router.post("/verify-status")
    def verify_status(
        payload: PendingTokenRequest,
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        """Ждёт ли это устройство подтверждения адреса — и входит, если да.

        Сценарий: пароль вводили на компьютере, письмо открыли на телефоне.
        Компьютер каждые несколько секунд спрашивает этот эндпоинт и, как
        только адрес подтверждён, получает настоящую пару токенов. Пароль
        повторно вводить не нужно, и в браузере он не хранится.
        """
        if not mail_enabled:
            raise HTTPException(status_code=404, detail="Email confirmation is disabled")
        try:
            data = decode_access_token(payload.pending_token)
        except JWTError:
            raise HTTPException(status_code=401, detail="Invalid pending token")
        if data.get("type") != "pending" or data.get("sub") is None:
            # Access-токен сюда подсунуть нельзя: он для других целей.
            raise HTTPException(status_code=401, detail="Invalid pending token")
        user = session.get(User, int(data["sub"]))
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="Invalid pending token")
        if not user.email_verified:
            return {"status": "pending", "email": user.email}
        pair = _create_token_pair(session, user)
        return {
            "status": "verified",
            "access_token": pair.access_token,
            "refresh_token": pair.refresh_token,
            "user": serialize_user(
                user, login_methods(session, user.id)
            ).model_dump(mode="json"),
        }

    @router.post("/resend-verification")
    def resend_verification(
        request: Request,
        payload: ResendVerificationRequest | None = None,
        current_user: User | None = Depends(get_optional_user),
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        """Ещё одно письмо подтверждения.

        Доступно и без сессии: сразу после регистрации её нет, а в кабинет до
        подтверждения адреса не пускают — иначе человек, закрывший вкладку,
        застрял бы навсегда.

        С сессией отвечаем конкретно про свой аккаунт. Без сессии — нейтрально
        («если такой адрес есть — письмо отправлено»): иначе форма становится
        проверялкой «зарегистрирован ли этот человек». Пауза на адрес — минута,
        бюджет писем считается по IP.
        """
        if not mail_enabled:
            raise HTTPException(status_code=404, detail="Email confirmation is disabled")
        ip = client_ip(request)
        resend_limiter.check(ip)

        if current_user is not None:
            if current_user.email_verified:
                # Не ошибка: человек мог подтвердить адрес с телефона, а на
                # этом экране остался старый статус.
                return {"status": "already", "email": current_user.email}
            left = email_verification.cooldown_left(session, current_user.id)
            if left > 0:
                raise HTTPException(
                    status_code=429,
                    detail=f"Письмо уже отправлено. Повторить можно через {left} с.",
                    headers={"Retry-After": str(left)},
                )
            resend_limiter.hit(ip)
            if not send_confirmation(session, current_user, request):
                raise HTTPException(
                    status_code=502,
                    detail="Не удалось отправить письмо. Попробуйте позже.",
                )
            return {"status": "sent", "email": current_user.email}

        email = (payload.email or "").lower() if payload is not None else ""
        if not email:
            raise HTTPException(
                status_code=401,
                detail="Укажите адрес почты или войдите в кабинет",
            )
        resend_limiter.hit(ip)
        user = session.scalar(select(User).where(User.email == email))
        if user is None or not user.is_active or user.email_verified:
            # Ничего не отправляем и не намекаем, есть ли такой аккаунт.
            return {"status": "sent"}
        if email_verification.cooldown_left(session, user.id) > 0:
            return {"status": "sent"}
        sent = send_confirmation(session, user, request)
        return {"status": "sent" if sent else "failed"}

    # --- Сброс пароля по почте ---
    @router.post("/password-reset/request")
    def password_reset_request(
        payload: PasswordResetRequest,
        request: Request,
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        """Письмо со ссылкой сброса. Ответ всегда один и тот же — существование
        аккаунта не раскрываем.

        Неподтверждённому адресу уходит письмо ПОДТВЕРЖДЕНИЯ, а не сброса:
        иначе сброс пароля стал бы способом попасть в кабинет без проверки
        почты.
        """
        if not mail_enabled:
            raise HTTPException(status_code=404, detail="Email confirmation is disabled")
        ip = client_ip(request)
        reset_limiter.check(ip)
        reset_limiter.hit(ip)
        user = session.scalar(select(User).where(User.email == payload.email.lower()))
        if user is None or not user.is_active:
            return {"status": "sent"}
        if not user.email_verified:
            if email_verification.cooldown_left(session, user.id) == 0:
                send_confirmation(session, user, request)
            return {"status": "sent"}
        if password_reset.cooldown_left(session, user.id) > 0:
            return {"status": "sent"}
        raw = password_reset.issue(session, user)
        link = password_reset.reset_link(email_verification.site_base(request), raw)
        subject, html_body, text_body = password_reset.build_letter(user, link)
        try:
            mailer.send(to=user.email, subject=subject, html=html_body, text=text_body)
        except MailError:
            # Наружу всё равно «sent»: иначе код ответа выдаёт существование
            # аккаунта тому, кто подбирает адреса. Причина — в логе.
            logger.exception("Не удалось отправить сброс пароля на %s", user.email)
        return {"status": "sent"}

    @router.post("/password-reset/confirm")
    def password_reset_confirm(
        payload: PasswordResetConfirm,
        request: Request,
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        """Меняет пароль по токену из письма и отзывает все сессии аккаунта.

        Отзыв refresh-токенов обязателен: если пароль сбрасывают из-за угона,
        украденная сессия не должна пережить сброс. Access-токены живут до
        30 минут — это осознанно оставленный хвост.
        """
        if not mail_enabled:
            raise HTTPException(status_code=404, detail="Email confirmation is disabled")
        ip = client_ip(request)
        reset_confirm_limiter.check(ip)
        reset_confirm_limiter.hit(ip)
        user, result = password_reset.consume(session, payload.token)
        if result != password_reset.STATUS_OK or user is None:
            messages = {
                password_reset.STATUS_EXPIRED: "Ссылка устарела. Запросите новое письмо.",
                password_reset.STATUS_USED: (
                    "Эта ссылка уже использована. Запросите новое письмо."
                ),
                password_reset.STATUS_INVALID: (
                    "Ссылка не подходит. Запросите новое письмо."
                ),
            }
            raise HTTPException(
                status_code=400,
                detail={
                    "code": f"reset_{result}",
                    "message": messages.get(
                        result, messages[password_reset.STATUS_INVALID]
                    ),
                },
            )
        user.password_hash = hash_password(payload.new_password)
        # Ссылка доказывает владение адресом так же, как письмо подтверждения.
        user.email_verified = True
        session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.user_id == user.id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
        )
        session.commit()
        logger.info("Пароль сброшен по почте для %s — сессии отозваны", user.email)
        return {"status": "ok", "email": user.email}
    return {
        "router": router,
        "get_current_user": get_current_user,
        "require_role": require_role,
    }