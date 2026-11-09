"""Ежедневная рассылка: напоминания «завтра» + добор недоставленных писем.

Запускается не веб-сервером, а планировщиком (на сервере — `/etc/cron.d/afisha-notify`,
10:00 по Asia/Novokuznetsk). Один процесс, одна короткая работа:

1. создать напоминания всем, у кого завтра событие в избранном;
2. отправить письма по всем уведомлениям, где письмо ещё не ушло (включая те,
   что не дошли вчера из-за сбоя почтового сервиса);
3. напечатать счётчики в лог — по ним видно, работает ли рассылка.

Ручной запуск (в том числе для проверки на живой базе):

    cd /opt/afisha/app/backend
    set -a; . /etc/afisha/afisha.env; set +a
    /opt/afisha/venv/bin/python -m app.notify_job

Ключ `--dry-run` показывает, что было бы отправлено, и ничего не отправляет.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from .db import build_engine, build_session_factory
from .mail import Mailer, build_mailer, config_from_env as mail_config_from_env
from .notifications import generate_all_reminders
from .notify_email import DEFAULT_LIMIT, ensure_schema, send_pending

logger = logging.getLogger("notify_job")

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def default_database_url() -> str:
    """Тот же выбор базы, что у приложения (AFISHA_DATABASE_URL → файл проекта)."""
    return (
        os.environ.get("AFISHA_DATABASE_URL")
        or f"sqlite:///{PROJECT_ROOT / 'afisha.sqlite3'}"
    )


def default_limit() -> int:
    raw = os.environ.get("AFISHA_NOTIFY_LIMIT", "").strip()
    try:
        return int(raw) if raw else DEFAULT_LIMIT
    except ValueError:
        logger.warning("AFISHA_NOTIFY_LIMIT=%r — не число, беру %s", raw, DEFAULT_LIMIT)
        return DEFAULT_LIMIT


def run(
    database_url: str | None = None,
    *,
    mailer: Mailer | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Один прогон рассылки. Возвращает счётчики для лога и тестов."""
    url = database_url or default_database_url()
    engine = build_engine(url)
    # Колонки доставки могли появиться в коде раньше, чем сервис перезапустили.
    ensure_schema(engine)

    if mailer is None:
        mailer = build_mailer(mail_config_from_env())

    session_factory = build_session_factory(engine)
    with session_factory() as session:
        reminders = generate_all_reminders(session)
        if dry_run:
            # Ничего не отправляем: считаем только то, что ждёт письма.
            from sqlalchemy import func, select

            from .models import Notification, User

            pending = session.scalar(
                select(func.count())
                .select_from(Notification)
                .join(User, User.id == Notification.user_id)
                .where(
                    Notification.emailed_at.is_(None),
                    User.is_active.is_(True),
                    User.email_verified.is_(True),
                )
            ) or 0
            logger.info(
                "dry-run: создано напоминаний %s, ждут письма %s", reminders, pending
            )
            return {"reminders": reminders, "sent": 0, "failed": 0, "skipped": 0,
                    "pending": int(pending)}

        stats = send_pending(session, mailer, limit=limit or default_limit())
    result = {"reminders": reminders, **stats}
    logger.info(
        "Напоминаний создано %s, писем отправлено %s, сбоев %s",
        result["reminders"], result["sent"], result["failed"],
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ежедневная рассылка уведомлений афиши")
    parser.add_argument("--dry-run", action="store_true", help="только посчитать, без отправки")
    parser.add_argument("--limit", type=int, default=None, help="максимум писем за прогон")
    parser.add_argument("--database-url", default=None, help="переопределить базу")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        result = run(
            args.database_url, limit=args.limit, dry_run=args.dry_run
        )
    except Exception:
        # Ненулевой код возврата — чтобы сбой был виден в cron/журнале, а не
        # потерялся в тишине.
        logger.exception("Рассылка упала")
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
