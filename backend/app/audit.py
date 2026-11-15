"""Журнал действий модерации.

Пишем строку на каждое действие, которое меняет судьбу чужого контента или
доступа: одобрение/отклонение события, правка, удаление, снятие с публикации,
разбор заявки на организатора, смена роли. Вопрос, на который отвечает журнал,
звучит так: «кто это сделал и на каком основании» — поэтому в строке хранятся
снимки (почта и роль автора, название сущности), а не только внешние ключи.

Коммит делает вызывающий код: запись в журнал не должна коммититься отдельно
от самого действия, иначе при откате останется строка о том, чего не случилось.
"""

from sqlalchemy.orm import Session

from .models import ModerationLog, User


def log_action(
    session: Session,
    actor: User,
    action: str,
    *,
    entity_type: str,
    entity_id: int | None = None,
    title: str | None = None,
    details: str | None = None,
) -> ModerationLog:
    entry = ModerationLog(
        actor_id=actor.id,
        actor_email=actor.email,
        actor_role=actor.role,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        title=_clip(title, 200),
        details=_clip(details, 1000),
    )
    session.add(entry)
    return entry


def serialize_log(entry: ModerationLog) -> dict[str, object]:
    return {
        "id": entry.id,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "actor_email": entry.actor_email,
        "actor_role": entry.actor_role,
        "action": entry.action,
        "entity_type": entry.entity_type,
        "entity_id": entry.entity_id,
        "title": entry.title,
        "details": entry.details,
    }


def _clip(value: str | None, limit: int) -> str | None:
    """Обрезаем, а не падаем: длинная причина не должна ронять действие."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"
