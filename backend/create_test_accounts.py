"""Dev-инструмент: завести набор аккаунтов команды (роли admin/moderator/organizer).

Отличие от `create_admin.py`: тот правит ОДИН аккаунт флагами, и пароль/роль
приходится помнить по памяти. Здесь аккаунты описаны файлом-спецификацией,
поэтому один и тот же набор заводится и в локальной БД, и в боевой, с
одинаковыми паролями — файл с доступами, который раздают людям, не расходится
с тем, что реально лежит в базе.

Что ставится помимо email/пароля/роли:
  * `email_verified = True` — адреса вида `admin1@afisha.com` не существуют,
    письмо подтверждения дойти не может, а без подтверждения вход закрыт (403).
  * `is_active = True` — снятые аккаунты этим же скриптом возвращаются в строй.

Идемпотентно: существующий email получает НОВЫЙ пароль из спецификации и
указанную роль. Это осознанно: скриптом чинят «потеряли пароль» и «съехала роль».

Запуск из корня проекта:
    python backend/create_test_accounts.py --spec .secrets/team-accounts.json
    python backend/create_test_accounts.py --spec spec.json --db /var/lib/afisha/afisha.sqlite3

Формат спецификации — JSON-массив:
    [{"email": "admin1@afisha.com", "password": "...", "role": "admin", "name": "Админ 1"}]
"""
import argparse
import json
import sys
from pathlib import Path

# Разрешаем импорт `from app.*` независимо от текущей рабочей директории.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select

from app.db import build_engine, build_session_factory
from app.models import User
from app.security import hash_password

ROLES = {"visitor", "organizer", "moderator", "admin"}

# База в корне проекта: backend/create_test_accounts.py -> parents[1] = корень.
DEFAULT_DB = Path(__file__).resolve().parents[1] / "afisha.sqlite3"


def load_spec(path: Path) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise SystemExit(f"{path}: ожидается непустой JSON-массив аккаунтов")

    seen: set[str] = set()
    for row in rows:
        email = str(row.get("email", "")).strip().lower()
        password = str(row.get("password", ""))
        role = str(row.get("role", "")).strip()
        if not email or "@" not in email:
            raise SystemExit(f"{path}: плохой email {email!r}")
        if len(password) < 8:
            raise SystemExit(f"{email}: пароль короче 8 символов — вход его отвергнет")
        if role not in ROLES:
            raise SystemExit(f"{email}: неизвестная роль {role!r}")
        if email in seen:
            raise SystemExit(f"{path}: email {email} указан дважды")
        seen.add(email)
        row["email"] = email
        row["role"] = role
        row["password"] = password
        row["name"] = str(row.get("name") or email).strip()
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Завести аккаунты команды по спецификации")
    parser.add_argument("--spec", required=True, help="JSON-файл со списком аккаунтов")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="путь к sqlite-базе")
    args = parser.parse_args()

    spec_path = Path(args.spec).expanduser()
    if not spec_path.exists():
        raise SystemExit(f"Спецификация не найдена: {spec_path}")
    rows = load_spec(spec_path)

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"База не найдена: {db_path}")

    engine = build_engine(f"sqlite:///{db_path.as_posix()}")
    session_factory = build_session_factory(engine)

    created = updated = 0
    with session_factory() as session:
        for row in rows:
            user = session.scalar(select(User).where(User.email == row["email"]))
            if user is None:
                user = User(
                    email=row["email"],
                    password_hash=hash_password(row["password"]),
                    full_name=row["name"],
                    role=row["role"],
                    email_verified=True,
                    is_active=True,
                )
                session.add(user)
                created += 1
                action = "создан"
            else:
                user.password_hash = hash_password(row["password"])
                user.full_name = row["name"]
                user.role = row["role"]
                user.email_verified = True
                user.is_active = True
                updated += 1
                action = "обновлён"
            print(f"  {row['email']:<26} {row['role']:<10} {action}")
        session.commit()

    print(f"\nБаза: {db_path}")
    print(f"Итого: создано {created}, обновлено {updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
