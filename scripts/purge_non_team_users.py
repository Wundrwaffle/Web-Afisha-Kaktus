"""Разовая чистка: оставить только аккаунты команды, дочерние записи и события удаляемых организаторов убрать.

По умолчанию — сухой прогон. Запись только с --apply (одна транзакция).

    python purge_non_team_users.py --db afisha.sqlite3 --spec .secrets/team-accounts.json
    python purge_non_team_users.py --db afisha.sqlite3 --spec .secrets/team-accounts.json --apply

Дополнительно защищённые аккаунты (например личные учётки владельца, которых нет
в спеке команды) перечисляются через --keep-email — они не удаляются никогда.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

CHILD_TABLES = ("favorites", "notifications", "refresh_tokens", "identities",
                "email_verifications", "password_resets", "organizer_applications",
                "moderation_log")
# moderation_log ссылается на актора не через user_id, а через actor_id
CHILD_COLUMNS = {"moderation_log": "actor_id"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--keep-email", action="append", default=[],
                    help="email, который нельзя удалять (можно повторять)")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    accounts = spec["accounts"] if isinstance(spec, dict) else spec
    keep_emails = [a["email"].strip().lower() for a in accounts]
    for extra in args.keep_email:
        if extra.strip().lower() not in keep_emails:
            keep_emails.append(extra.strip().lower())

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = OFF")

    users = con.execute("select id,email,role from users order by id").fetchall()
    keep_ids = [u["id"] for u in users if (u["email"] or "").lower() in keep_emails]
    drop = [u for u in users if u["id"] not in keep_ids]
    existing = {(u["email"] or "").lower() for u in users}
    missing = [e for e in keep_emails if e not in existing]
    if missing:
        raise SystemExit(f"В базе нет защищённых аккаунтов: {missing} — останавливаюсь, чтобы не удалить лишнее")
    if not keep_ids:
        raise SystemExit("Не нашёл ни одного защищённого аккаунта — останавливаюсь")
    if not drop:
        print("Удалять нечего: в базе только защищённые аккаунты")
        return 0

    drop_ids = [u["id"] for u in drop]
    q = ",".join("?" * len(drop_ids))

    events = con.execute("select id,title,status,organizer_id from events").fetchall()
    doomed_events = [e for e in events if e["organizer_id"] in set(drop_ids)]
    doomed_event_ids = [e["id"] for e in doomed_events]
    eq = ",".join("?" * len(doomed_event_ids)) if doomed_event_ids else None

    print(f"БД: {args.db}")
    print(f"Пользователи: {len(users)} → останется {len(keep_ids)}, удалится {len(drop_ids)}")
    for u in (u for u in users if u["id"] in keep_ids):
        print(f"  + #{u['id']} {u['email']} ({u['role']})")
    for u in drop:
        print(f"  - #{u['id']} {u['email']} ({u['role']})")
    print(f"События: {len(events)} → удалится {len(doomed_events)}, останется {len(events) - len(doomed_events)}")
    for e in doomed_events[:10]:
        print(f"  - #{e['id']} {e['title'][:50]} [{e['status']}]")

    plan = [("notifications", "delete from notifications where user_id in (%s)" % q, drop_ids)]
    if eq:
        plan.append(("notifications", f"delete from notifications where event_id in ({eq})", doomed_event_ids))
    plan.append(("favorites", "delete from favorites where user_id in (%s)" % q, drop_ids))
    if eq:
        plan.append(("favorites", f"delete from favorites where event_id in ({eq})", doomed_event_ids))
    for t in CHILD_TABLES:
        if t in ("favorites", "notifications"):
            continue
        col = CHILD_COLUMNS.get(t, "user_id")
        plan.append((t, f"delete from {t} where {col} in ({q})", drop_ids))
    if eq:
        plan.append(("events", f"delete from events where id in ({eq})", doomed_event_ids))
    plan.append(("users", "delete from users where id in (%s)" % q, drop_ids))

    for table, sql, params in plan:
        n = con.execute(sql.replace("delete from", "select count(*) from", 1), params).fetchone()[0]
        print(f"  {table}: {n}")
        if args.apply:
            con.execute(sql, params)

    if not args.apply:
        print("Сухой прогон. Для записи добавь --apply")
        con.close()
        return 0

    con.commit()
    left_users = con.execute("select count(*) from users").fetchone()[0]
    left_events = con.execute("select count(*) from events").fetchone()[0]
    orphans = con.execute(
        "select count(*) from events e left join users u on u.id = e.organizer_id "
        "where e.organizer_id is not null and u.id is null"
    ).fetchone()[0]
    demo_events = con.execute("select count(*) from events where organizer_id is null").fetchone()[0]
    # `x.<col> is not null` обязателен: у уведомлений event_id и у служебных таблиц
    # user_id бывают NULL, и без этой оговорки LEFT JOIN считает их «висячими».
    dangling = 0
    for t, col in (("favorites", "user_id"), ("notifications", "user_id"),
                   ("refresh_tokens", "user_id"), ("identities", "user_id"),
                   ("email_verifications", "user_id"), ("password_resets", "user_id"),
                   ("organizer_applications", "user_id"), ("moderation_log", "actor_id")):
        dangling += con.execute(
            f"select count(*) from {t} x left join users u on u.id = x.{col}"
            f" where x.{col} is not null and u.id is null"
        ).fetchone()[0]
    for t in ("favorites", "notifications"):
        dangling += con.execute(
            f"select count(*) from {t} x left join events e on e.id = x.event_id"
            " where x.event_id is not null and e.id is null"
        ).fetchone()[0]
    print(f"ИТОГ: пользователей {left_users}, событий {left_events} "
          f"(демо без организатора: {demo_events}), осиротевших событий {orphans}, висячих ссылок {dangling}")
    print("роли:", [(r["role"], r["c"]) for r in con.execute("select role, count(*) c from users group by role")])
    con.close()
    if orphans or dangling:
        print("ВНИМАНИЕ: остались битые ссылки")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
