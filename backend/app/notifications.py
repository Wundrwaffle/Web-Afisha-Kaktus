"""Помощники для уведомлений в личный кабинет.

Напоминания «за день до события» рождаются двумя путями: лениво, когда человек
сам открывает кабинет (чтобы он увидел напоминание сразу, а не после ночной
задачи), и ежедневной задачей `app.notify_job` — она делает то же самое для
всех, кто сегодня на сайт не зайдёт. Дубликатов нет: перед созданием проверяем,
что напоминание по этому событию уже есть.
"""

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Event, Favorite, Notification, User


def staff_ids(session: Session) -> list[int]:
    """id всех администраторов и модераторов — получателей служебных уведомлений."""
    return list(
        session.scalars(
            select(User.id).where(User.role.in_(["admin", "moderator"]))
        ).all()
    )


def notify_users(
    session: Session,
    user_ids: list[int] | set[int],
    *,
    kind: str,
    title: str,
    text: str,
    event_id: int | None = None,
) -> list[Notification]:
    """Создаёт уведомление указанным получателям (без дедупликации — каждый
    переход — отдельное уведомление). Возвращает созданные строки: вызывающий
    код отправляет по ним письма (см. notify_email.send_pending)."""
    created: list[Notification] = []
    for uid in set(user_ids):
        if uid is None:
            continue
        notification = Notification(
            user_id=uid,
            event_id=event_id,
            kind=kind,
            title=title,
            text=text,
        )
        session.add(notification)
        created.append(notification)
    return created


def _remind_one(session: Session, user_id: int, day: date) -> list[Notification]:
    """Напоминания одному пользователю о событиях в избранном на указанную дату."""
    already = set(
        session.scalars(
            select(Notification.event_id).where(
                Notification.user_id == user_id,
                Notification.kind == "event_reminder",
            )
        ).all()
    )

    events = session.scalars(
        select(Event)
        .join(Favorite, Favorite.event_id == Event.id)
        .where(
            Favorite.user_id == user_id,
            Event.status == "published",
            Event.date == day,
        )
    ).all()

    created: list[Notification] = []
    for event in events:
        if event.id in already:
            continue
        notification = Notification(
            user_id=user_id,
            event_id=event.id,
            kind="event_reminder",
            title="Событие уже завтра",
            text=(
                f"«{event.title}» состоится завтра, {event.date.strftime('%d.%m.%Y')}, "
                f"в {event.time.strftime('%H:%M')} — {event.venue}."
            ),
        )
        session.add(notification)
        created.append(notification)

    if created:
        session.commit()
    return created


def generate_reminders(session: Session, user_id: int) -> list[Notification]:
    """Ленивая генерация напоминаний «за день до события» при открытии кабинета."""
    return _remind_one(session, user_id, date.today() + timedelta(days=1))


def generate_all_reminders(session: Session, day: date | None = None) -> int:
    """То же для всех, у кого есть избранное на завтрашний день (задача-планировщик).

    Возвращает число созданных напоминаний. Обходим только тех, у кого есть
    избранное, — иначе пришлось бы перебирать всю таблицу пользователей.
    """
    target = day or (date.today() + timedelta(days=1))
    user_ids = session.scalars(
        select(Favorite.user_id).distinct()
    ).all()
    created = 0
    for user_id in user_ids:
        created += len(_remind_one(session, user_id, target))
    return created
