"""Помощники для уведомлений в личный кабинет.

Email сознательно отложен (решение пользователя): пока только внутренние
уведомления в БД. Когда появится email, отправку писем добавим сюда же,
а напоминания «за день до события» переедет на планировщик (cron/скрипт).
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
) -> None:
    """Создаёт уведомление указанным получателям (без дедупликации — каждый
    переход — отдельное уведомление)."""
    for uid in set(user_ids):
        if uid is None:
            continue
        session.add(
            Notification(
                user_id=uid,
                event_id=event_id,
                kind=kind,
                title=title,
                text=text,
            )
        )


def generate_reminders(session: Session, user_id: int) -> None:
    """Ленивая генерация напоминаний «за день до события».

    Вызывается при запросе уведомлений пользователем. Ищет события в избранном
    пользователя на завтрашнюю дату и создаёт для каждого напоминание — но только
    если его ещё нет (проверка по существующим event_id), чтобы повторные вызовы
    не плодили дубликаты.
    """
    tomorrow = date.today() + timedelta(days=1)

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
            Event.date == tomorrow,
        )
    ).all()

    added = False
    for event in events:
        if event.id in already:
            continue
        session.add(
            Notification(
                user_id=user_id,
                event_id=event.id,
                kind="event_reminder",
                title="Событие уже завтра",
                text=(
                    f"«{event.title}» состоится завтра, {event.date.strftime('%d.%m.%Y')}, "
                    f"в {event.time.strftime('%H:%M')} — {event.venue}."
                ),
            )
        )
        added = True

    if added:
        session.commit()
