"""Dev-инструмент: создать пользователя с нужной ролью или повысить существующего.

Назначение ролей (Этап 4 / админка) ещё не реализовано — регистрация всегда даёт
роль `visitor`. Этот скрипт закрывает пробел для локальной разработки: создаёт
админа, модератора или организатора напрямую в БД.

Запуск из корня проекта (работает из любой директории):
    python backend/create_admin.py
    python backend/create_admin.py --email admin@afisha.local --password admin123 --role admin --name "Администратор"

Роли: visitor | organizer | moderator | admin
Идемпотентно: если email уже существует — просто назначает указанную роль.
"""
import argparse
import sys
from pathlib import Path

# Разрешаем импорт `from app.*` независимо от текущей рабочей директории.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select

from app.db import build_engine, build_session_factory
from app.models import User
from app.security import hash_password

ROLES = {"visitor", "organizer", "moderator", "admin"}

# База в корне проекта: backend/create_admin.py -> parents[1] = корень.
DEFAULT_DB = Path(__file__).resolve().parents[1] / "afisha.sqlite3"


def main() -> None:
    parser = argparse.ArgumentParser(description="Создать/повысить пользователя")
    parser.add_argument("--email", default="admin@afisha.local")
    parser.add_argument("--password", default="admin123")
    parser.add_argument("--name", default="Администратор")
    parser.add_argument("--role", default="admin", choices=sorted(ROLES))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    args = parser.parse_args()

    engine = build_engine(f"sqlite:///{args.db}")
    session_factory = build_session_factory(engine)

    with session_factory() as session:
        email = args.email.lower()
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(
                email=email,
                password_hash=hash_password(args.password),
                full_name=args.name,
                role=args.role,
            )
            session.add(user)
            action = "создан"
        else:
            user.role = args.role
            if args.name:
                user.full_name = args.name
            action = "обновлён"
        session.commit()
        print(f"Пользователь {email} {action} с ролью '{args.role}'")


if __name__ == "__main__":
    main()
