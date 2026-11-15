"""Демо-афиша: события на ближайшие недели для прогона сценариев и живой главной.

Даты считаются от сегодняшнего дня, поэтому афиша не «протухает». Повторный запуск
обновляет даты у уже залитых демо-событий (slug начинается с `demo-`) — остальные
события скрипт не трогает.

Часть событий привязана к организаторам команды (`orgtest1..3@afisha.com`), чтобы
у них в кабинете были «свои» события: так проверяется редактирование и модерация.
Остальные идут без организатора — это общая городская афиша.

    python backend/seed_demo.py                    # локальная база проекта
    python backend/seed_demo.py --db /var/lib/afisha/afisha.sqlite3   # прод
    python backend/seed_demo.py --dry-run
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

# slug, title, category, день от сегодня, время, площадка, цена, организатор
DEMO_EVENTS = [
    ("demo-jazz-evening", "Джазовый вечер «Осенний свет»", "Музыка", 0, "19:00", "Филармония", "от 500 ₽", None),
    ("demo-kids-lab", "Детская научная лаборатория", "Для детей", 0, "12:00", "ДК им. Маяковского", "от 300 ₽", None),
    ("demo-standup-night", "Стендап-вечер «Свои шутки»", "Юмор", 1, "20:00", "Кофейня «Точка»", "от 400 ₽", "orgtest1@afisha.com"),
    ("demo-photo-walk", "Фотопрогулка по старому центру", "Культура", 2, "11:00", "Центр города", "Бесплатно", None),
    ("demo-theatre-tour", "Экскурсия за кулисы драмтеатра", "Театр", 3, "18:30", "Драмтеатр", "от 350 ₽", "orgtest1@afisha.com"),
    ("demo-yoga-morning", "Утренняя йога в парке", "Спорт", 4, "08:00", "Парк Гагарина", "Бесплатно", None),
    ("demo-food-market", "Гастрономический маркет выходного дня", "Еда", 5, "12:00", "Площадь Побед", "Вход свободно", "orgtest2@afisha.com"),
    ("demo-lecture-ai", "Лекция «ИИ в дизайне: что уже работает»", "Лекции", 6, "18:30", "Библиотека им. Гоголя", "Бесплатно", None),
    ("demo-ceramics", "Мастер-класс по керамике", "Мастер-классы", 7, "14:00", "Студия «Глина»", "от 700 ₽", "orgtest2@afisha.com"),
    ("demo-cinema-shorts", "Ночь короткометражек", "Кино", 8, "21:00", "Кинотеатр «Октябрь»", "от 200 ₽", None),
    ("demo-rock-local", "Рок-сейшн местных групп", "Музыка", 10, "19:30", "Клуб «Ритм»", "от 600 ₽", "orgtest3@afisha.com"),
    ("demo-kids-quest", "Квест для школьников «Тайны города»", "Для детей", 11, "13:00", "Музей краеведения", "от 250 ₽", None),
    ("demo-book-club", "Книжный клуб: обсуждаем новинки", "Лекции", 13, "18:00", "Библиотека им. Гоголя", "Бесплатно", None),
    ("demo-chess-open", "Открытый шахматный турнир", "Спорт", 14, "10:00", "Шахматный клуб", "Бесплатно", "orgtest3@afisha.com"),
    ("demo-tea-ceremony", "Чайная церемония и дегустация", "Еда", 16, "15:00", "Дом чая", "от 350 ₽", None),
    ("demo-art-market", "Арт-маркет локальных мастеров", "Культура", 18, "11:00", "Арт-пространство", "Бесплатно", "orgtest1@afisha.com"),
    ("demo-classic-concert", "Классический концерт «Струны зимы»", "Музыка", 21, "18:00", "Филармония", "от 450 ₽", None),
    ("demo-halloween-kids", "Детский праздник «Веселье тыквы»", "Для детей", 24, "15:00", "ДК им. Маяковского", "от 300 ₽", "orgtest2@afisha.com"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path(__file__).resolve().parents[1] / "afisha.sqlite3"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    today = date.today()

    organizers = {
        (r["email"] or "").lower(): r["id"]
        for r in con.execute("select id, email from users where role = 'organizer'")
    }
    missing = {o for *_, o in DEMO_EVENTS if o and o.lower() not in organizers}
    if missing:
        print(f"ВНИМАНИЕ: организаторов нет в базе: {sorted(missing)} — их события пойдут без организатора")

    added = updated = 0
    sql_ins = (
        "insert into events (title, slug, status, category, date, time, venue, price, organizer_id) "
        "values (?,?,?,?,?,?,?,?,?)"
    )
    sql_upd = (
        "update events set title=?, status='published', category=?, date=?, time=?, venue=?, price=?, "
        "organizer_id=? where slug=?"
    )

    for slug, title, category, offset, hhmm, venue, price, org in DEMO_EVENTS:
        d = today + timedelta(days=offset)
        t = datetime.strptime(hhmm, "%H:%M").time()
        org_id = organizers.get(org.lower()) if org else None
        exists = con.execute("select id from events where slug = ?", (slug,)).fetchone()
        if exists:
            print(f"  ~ {d} {hhmm}  {title}  (обновление)")
            if not args.dry_run:
                con.execute(sql_upd, (title, category, d.isoformat(), t.isoformat(), venue, price, org_id, slug))
            updated += 1
        else:
            print(f"  + {d} {hhmm}  {title}")
            if not args.dry_run:
                con.execute(sql_ins, (title, slug, "published", category, d.isoformat(), t.isoformat(),
                                      venue, price, org_id))
            added += 1

    if not args.dry_run:
        con.commit()

    total = con.execute("select count(*) from events").fetchone()[0]
    upcoming = con.execute("select count(*) from events where status='published' and date >= ?",
                           (today.isoformat(),)).fetchone()[0]
    print(f"\nДобавлено: {added}, обновлено: {updated}"
          f"{' (сухой прогон)' if args.dry_run else ''}. Всего событий: {total}, предстоящих published: {upcoming}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
