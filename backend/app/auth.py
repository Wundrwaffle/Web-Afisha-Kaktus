from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader, OAuth2PasswordBearer
from jose import JWTError
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import session_dependency
from .exposure import LoginRateLimiter, SlidingWindowLimiter, client_ip
from .models import RefreshToken, User
from .security import (
    create_access_token,
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


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    id: int
    email: str
    full_name: str
    role: str
    is_active: bool

    model_config = {"from_attributes": True}


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)

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


def serialize_user(user: User) -> UserOut:
    return UserOut.model_validate(user)


def build_auth_routes(
    session_factory,
    login_limiter: LoginRateLimiter | None = None,
    register_limiter: SlidingWindowLimiter | None = None,
):
    """Собирает auth-эндпоинты с привязкой к session_factory из create_app.

    Роутер создаётся ЛОКАЛЬНО на каждый вызов — иначе модульный singleton-роутер
    оставляет замыкание get_session от первого create_app(), и повторные вызовы
    (например, в тестах с отдельной БД) пишут в «чужую» базу.

    Лимитеры передаются снаружи (создаются в create_app), чтобы состояние не
    переезжало между приложениями/тестами.
    """
    router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

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

    @router.post("/register", response_model=UserOut, status_code=201)
    def register(
        payload: RegisterRequest,
        request: Request,
        session: Session = Depends(get_session),
    ) -> UserOut:
        if register_limiter is not None:
            # Защита от массовой регистрации с одного адреса при публичном доступе.
            register_limiter.check(client_ip(request))
        email = payload.email.lower()
        existing = session.scalar(select(User).where(User.email == email))
        if existing is not None:
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
        return serialize_user(user)

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
        user = session.scalar(select(User).where(User.email == email))
        if user is None or not verify_password(payload.password, user.password_hash):
            if login_limiter is not None:
                login_limiter.register_failure(ip, email)
            raise HTTPException(status_code=401, detail="Invalid email or password")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account is deactivated")
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
    def me(current_user: User = Depends(get_current_user)) -> UserOut:
        return serialize_user(current_user)

    return {
        "router": router,
        "get_current_user": get_current_user,
        "require_role": require_role,
    }