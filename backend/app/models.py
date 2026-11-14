from datetime import date, datetime, time

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    """Naive UTC «сейчас». Храним время в UTC без tz-метки, чтобы избежать
    несоответствия naive/aware при чтении из SQLite."""
    return datetime.utcnow()


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organizer_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(180), unique=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    time: Mapped[time] = mapped_column(Time, nullable=False)
    venue: Mapped[str] = mapped_column(String(180), nullable=False)
    price: Mapped[str] = mapped_column(String(80), nullable=False)
    moderation_note: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    # Логин для входа — отдельно от почты: человек вводит «логин или email».
    # NULL у аккаунтов, созданных до появления логина: вход по адресу работает,
    # а бэкфилл проставляет им логин из адреса при старте приложения.
    login: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    # Профиль посетителя/организатора. Заполняется при регистрации или при
    # подаче заявки на организатора; у посетителя большинство полей пустые.
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    org_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    inn: Mapped[str | None] = mapped_column(String(16), nullable=True)
    social_personal: Mapped[str | None] = mapped_column(String(300), nullable=True)
    social_org: Mapped[str | None] = mapped_column(String(300), nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="visitor")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Подтверждён ли адрес ссылкой из письма. Вход без подтверждения закрыт,
    # поэтому аккаунты, существовавшие до миграции, получают 1 (грандфазеринг).
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class Identity(Base):
    """Связка «аккаунт у внешнего провайдера» → наш пользователь.

    Один человек может привязать несколько провайдеров (Яндекс, позже VK),
    поэтому уникальна пара (provider, provider_user_id), а не user_id.
    Пароль OAuth-аккаунта — случайный хеш: схему users не ломаем (password_hash
    NOT NULL), но войти по паролю такому аккаунту невозможно.
    """

    __tablename__ = "identities"
    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id", name="uq_identity_provider_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # Почта, которую провайдер отдал на момент привязки (у Яндекса — default_email).
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    display_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    linked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class OrganizerApplication(Base):
    """Заявка на статус организатора — отдельная сущность, а не флаг на users.

    Причина: заявку можно отклонить и подать повторно, а история проверок и
    причина отказа должны сохраняться. Роль в users при этом не меняется, пока
    администратор не одобрит заявку (до одобрения человек остаётся visitor).
    Поля профиля дублируются снимком: заявку проверяют по тому, что человек
    отправил, даже если позже он поправит профиль.
    """

    __tablename__ = "organizer_applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    # pending | approved | rejected
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    phone: Mapped[str] = mapped_column(String(40), nullable=False)
    city: Mapped[str] = mapped_column(String(120), nullable=False)
    org_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    inn: Mapped[str | None] = mapped_column(String(16), nullable=True)
    social_personal: Mapped[str | None] = mapped_column(String(300), nullable=True)
    social_org: Mapped[str | None] = mapped_column(String(300), nullable=True)
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Причина отказа — её видит организатор в кабинете и в письме.
    note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    # Когда админам уже напомнили, что заявка висит дольше двух рабочих дней.
    # Поле, а не отдельная таблица: напоминание нужно ровно одно на заявку.
    stale_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class EmailVerification(Base):
    """Ссылка подтверждения адреса. Хранится только sha256 токена.

    Строка — журнал: used_at ставится и при подтверждении, и при выпуске новой
    ссылки (старая гаснет). Поэтому «сколько писем отправляли» видно по числу
    строк, и пауза между письмами считается без отдельного поля.
    """

    __tablename__ = "email_verifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    # Адрес на момент выпуска: если его сменили, старая ссылка недействительна.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )


class PasswordReset(Base):
    """Ссылка сброса пароля. Хранится только sha256 токена, как у подтверждения.

    Отдельная таблица, а не общий журнал с email_verifications: у сброса другой
    срок жизни (60 минут вместо суток) и другая цена ошибки, поэтому смешивать
    типы токенов в одной таблице — значит рано или поздно перепутать проверку.
    """

    __tablename__ = "password_resets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    # Адрес на момент выпуска: если его сменили, старая ссылка недействительна.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )


class Favorite(Base):
    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("user_id", "event_id", name="uq_favorite_user_event"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class LocalCaptchaChallenge(Base):
    """Задание своей капчи (замена виджета, когда тот заблокирован).

    Хранится в БД, а не в памяти процесса: задание должно переживать перезапуск
    и быть видимым всем воркерам, иначе решение отвергнется «через шаг».
    Строка одноразовая (used_at) и живёт 5 минут.
    """

    __tablename__ = "local_captcha_challenges"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Сколько ведущих шестнадцатеричных нулей требует решение.
    difficulty: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Notification(Base):
    """Уведомление в личный кабинет. Одно событие может породить несколько
    уведомлений одному пользователю (например, отклонено дважды) — поэтому
    уникального ключа нет; идемпотентность нужна только напоминаниям, её
    обеспечивает generate_reminders проверкой на существование."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    text: Mapped[str] = mapped_column(String(1000), nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    # Письмо об уведомлении. NULL в emailed_at — ещё не отправляли (или отправка
    # сорвалась): такие строки добирает ежедневная задача app.notify_job.
    # Попытки считаем, чтобы «адрес не существует» не превращался в вечный цикл.
    emailed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    email_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    email_error: Mapped[str | None] = mapped_column(String(300), nullable=True)