"""Заявки на статус организатора.

Два пути к одной сущности:
  • регистрация «Я организатор» — заявка создаётся сразу вместе с аккаунтом;
  • посетитель в кабинете нажимает «Разместить афишу» и заполняет ту же форму.

Заявка живёт отдельной таблицей, а не флагом на пользователе: отклонённую
заявку можно подать повторно, и история с причиной отказа должна сохраняться.
Роль меняется только при одобрении — до этого человек остаётся visitor и
по-прежнему может лайкать, добавлять в избранное и получать уведомления.
"""

from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import OrganizerApplication, User, utcnow
from .notifications import staff_ids

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED)

# Сколько рабочих дней заявка может висеть, прежде чем админам придёт
# напоминание. Считаем именно рабочие дни: заявка, поданная в пятницу вечером,
# не должна «просрочиться» в субботу.
PENDING_BUSINESS_DAYS = 2

# Поля профиля, которые заявка переносит в карточку пользователя.
PROFILE_FIELDS = (
    "full_name",
    "phone",
    "city",
    "org_name",
    "inn",
    "social_personal",
    "social_org",
    "birth_date",
)


def latest_application(session: Session, user_id: int) -> OrganizerApplication | None:
    """Последняя заявка пользователя (или None, если он её никогда не подавал)."""
    return session.scalar(
        select(OrganizerApplication)
        .where(OrganizerApplication.user_id == user_id)
        .order_by(
            OrganizerApplication.created_at.desc(),
            OrganizerApplication.id.desc(),
        )
    )


def business_days_between(start, end) -> int:
    """Число рабочих дней (пн–пт) между двумя моментами.

    Праздники не учитываем: календаря производства на сайте нет, а грубая
    оценка здесь лучше, чем «просроченные» заявки в выходные.
    """
    if end <= start:
        return 0
    days = 0
    cursor = start.date()
    last = end.date()
    while cursor < last:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def serialize_application(
    application: OrganizerApplication, user: User | None = None
) -> dict[str, object]:
    """Заявка для кабинета и админки. Контакты отдаём только админу."""
    data: dict[str, object] = {
        "id": application.id,
        "user_id": application.user_id,
        "status": application.status,
        "full_name": application.full_name,
        "phone": application.phone,
        "city": application.city,
        "org_name": application.org_name,
        "inn": application.inn,
        "social_personal": application.social_personal,
        "social_org": application.social_org,
        "birth_date": application.birth_date.isoformat()
        if application.birth_date
        else None,
        "note": application.note,
        "created_at": application.created_at.isoformat()
        if application.created_at
        else None,
        "reviewed_at": application.reviewed_at.isoformat()
        if application.reviewed_at
        else None,
    }
    if user is not None:
        data.update(
            {
                "email": user.email,
                "login": user.login,
                "role": user.role,
                "email_verified": user.email_verified,
            }
        )
    return data


def build_organizer_routes(
    session_factory,
    *,
    get_session,
    get_current_user,
    require_role,
    mailer=None,
    require_verified_email: bool = False,
    notify=None,
):
    """Роутер заявок. Зависимости приходят из create_app, как у auth-роутов.

    `notify` — функция «уведомление в кабинет + письмо» из main.py: одобрение и
    отклонение обязаны дойти до человека обоими каналами (ТЗ, п.6).
    """
    router = APIRouter(prefix="/api/v1", tags=["organizers"])

    class ApplicationIn(BaseModel):
        # ФИО, телефон и город обязательны — по ним админ проверяет заявку.
        full_name: str = Field(min_length=2, max_length=160)
        phone: str = Field(min_length=5, max_length=40)
        city: str = Field(min_length=2, max_length=120)
        org_name: str | None = Field(default=None, max_length=200)
        inn: str | None = Field(default=None, max_length=16)
        social_personal: str | None = Field(default=None, max_length=300)
        social_org: str | None = Field(default=None, max_length=300)
        birth_date: date | None = None

    class ReviewIn(BaseModel):
        decision: Literal["approve", "reject"]
        # Причина обязательна при отказе: человек должен знать, что исправить.
        reason: str | None = Field(default=None, max_length=1000)

    def _fields(payload: ApplicationIn) -> dict[str, object]:
        data = payload.model_dump()
        # Пустые строки из формы — это «не заполнено», а не пустое значение.
        for key, value in data.items():
            if isinstance(value, str):
                data[key] = value.strip() or None
        return data

    @router.get("/me/organizer-application")
    def my_application(
        session: Session = Depends(get_session),
        current_user: User = Depends(get_current_user),
    ) -> dict[str, object]:
        application = latest_application(session, current_user.id)
        return {
            "application": serialize_application(application)
            if application is not None
            else None
        }

    @router.post("/me/organizer-application", status_code=201)
    def submit_application(
        payload: ApplicationIn,
        session: Session = Depends(get_session),
        current_user: User = Depends(get_current_user),
    ) -> dict[str, object]:
        if current_user.role in ("organizer", "admin"):
            raise HTTPException(status_code=409, detail="Вы уже организатор")
        if (
            require_verified_email
            and mailer is not None
            and mailer.enabled
            and not current_user.email_verified
        ):
            # Заявку проверяют вручную и пишут ответ на почту — неподтверждённый
            # адрес сделал бы эту переписку бессмысленной.
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "email_not_verified",
                    "message": (
                        "Подтвердите адрес почты — после этого можно подать "
                        "заявку. Письмо пришло при регистрации, повторно его "
                        "можно отправить в кабинете."
                    ),
                },
            )
        previous = latest_application(session, current_user.id)
        if previous is not None and previous.status == STATUS_PENDING:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "pending",
                    "message": "Заявка уже на проверке — ответ придёт на почту.",
                },
            )
        fields = _fields(payload)
        application = OrganizerApplication(
            user_id=current_user.id,
            status=STATUS_PENDING,
            full_name=fields["full_name"],
            phone=fields["phone"],
            city=fields["city"],
            org_name=fields["org_name"],
            inn=fields["inn"],
            social_personal=fields["social_personal"],
            social_org=fields["social_org"],
            birth_date=fields["birth_date"],
        )
        session.add(application)
        # Профиль тоже обновляем: данные не должны жить только внутри заявки,
        # иначе после одобрения в карточке останутся старые поля.
        for name in PROFILE_FIELDS:
            setattr(current_user, name, fields[name])
        if not current_user.full_name:
            current_user.full_name = fields["full_name"]
        session.commit()
        session.refresh(application)
        return serialize_application(application)

    @router.get("/admin/organizer-applications")
    def list_applications(
        status: str = STATUS_PENDING,
        session: Session = Depends(get_session),
        _: User = Depends(require_role("admin")),
    ) -> dict[str, object]:
        if status not in STATUSES and status != "all":
            raise HTTPException(status_code=422, detail="Unknown application status")
        statement = select(OrganizerApplication).order_by(
            OrganizerApplication.created_at.desc(), OrganizerApplication.id.desc()
        )
        if status != "all":
            statement = statement.where(OrganizerApplication.status == status)
        applications = session.scalars(statement).all()
        user_ids = {application.user_id for application in applications}
        users = (
            {
                user.id: user
                for user in session.scalars(
                    select(User).where(User.id.in_(user_ids))
                ).all()
            }
            if user_ids
            else {}
        )
        _remind_stale(session, applications)
        return {
            "items": [
                serialize_application(application, users.get(application.user_id))
                for application in applications
            ],
            "total": len(applications),
        }

    def _remind_stale(session: Session, applications) -> None:
        """Напоминание админам о заявках, висящих дольше двух рабочих дней.

        Одно на заявку (stale_notified_at): иначе каждый заход в админку
        добавлял бы новое уведомление и колокольчик стал бы бесполезен.
        """
        if notify is None:
            return
        now = utcnow()
        stale = [
            application
            for application in applications
            if application.status == STATUS_PENDING
            and application.stale_notified_at is None
            and business_days_between(application.created_at, now)
            >= PENDING_BUSINESS_DAYS
        ]
        if not stale:
            return
        for application in stale:
            notify(
                session,
                staff_ids(session),
                kind="organizer_app_stale",
                title="Заявка организатора ждёт проверки",
                text=(
                    f"Заявка от {application.full_name} "
                    f"({application.city}) висит дольше "
                    f"{PENDING_BUSINESS_DAYS} рабочих дней."
                ),
            )
            application.stale_notified_at = now
        session.commit()

    @router.post("/admin/organizer-applications/{application_id}/review")
    def review_application(
        application_id: int,
        payload: ReviewIn,
        session: Session = Depends(get_session),
        current_user: User = Depends(require_role("admin")),
    ) -> dict[str, object]:
        application = session.get(OrganizerApplication, application_id)
        if application is None:
            raise HTTPException(status_code=404, detail="Application not found")
        if application.status != STATUS_PENDING:
            raise HTTPException(status_code=409, detail="Заявка уже обработана")
        user = session.get(User, application.user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        reason = (payload.reason or "").strip()
        if payload.decision == "reject" and not reason:
            raise HTTPException(status_code=400, detail="Укажите причину отказа")
        application.reviewed_at = utcnow()
        application.reviewed_by = current_user.id
        if payload.decision == "approve":
            application.status = STATUS_APPROVED
            application.note = None
            user.role = "organizer"
            for name in PROFILE_FIELDS:
                value = getattr(application, name)
                if value:
                    setattr(user, name, value)
            session.commit()
            if notify is not None:
                # ТЗ п.6: письмо + уведомление в кабинет. Баннер «заявка
                # одобрена» рисует фронт по полю organizer_status.
                notify(
                    session,
                    [user.id],
                    kind="organizer_approved",
                    title="Вам одобрен статус организатора",
                    text=(
                        "Заявка одобрена — теперь вы можете размещать афиши. "
                        "Кнопка «Разместить афишу» есть в личном кабинете."
                    ),
                )
        else:
            application.status = STATUS_REJECTED
            application.note = reason
            session.commit()
            if notify is not None:
                notify(
                    session,
                    [user.id],
                    kind="organizer_rejected",
                    title="Заявка организатора отклонена",
                    text=(
                        f"Причина: {reason}. Роль осталась прежней — заявку "
                        "можно подать повторно из личного кабинета."
                    ),
                )
        session.refresh(application)
        return serialize_application(application, user)

    return router
