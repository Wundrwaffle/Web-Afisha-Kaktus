#!/usr/bin/env python3
"""Идемпотентная миграция демо-данных: схлопывает дубликаты площадок.

В корневой afisha.sqlite3 генератор демо-событий непоследовательно добавлял
суффикс «, Новокузнецк» к площадкам, у которых уже есть полное уникальное имя.
В фасете «Место» это давало дубли («Арт-пространство» и «Арт-пространство,
Новокузнецк»). Миграция объединяет только те пары, где базовое имя уже
существует в данных (категория A); родовые имена без города («Клуб, Новокузнецк»,
«Музей, Новокузнецк» и т.п.) не трогаются — это отдельное решение.

Безопасна для повторного запуска: UPDATE ... WHERE venue = ? с уже убранным
суффиксом ничего не изменит.
"""
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "afisha.sqlite3"

# Пары «, Новокузнецк» -> базовое имя (объединять только когда базовое уже есть).
MERGE = [
    "Арт-пространство",
    "Библиотека им. Гоголя",
    "Парк Гагарина",
    "Площадь Побед",
]


def main() -> None:
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    changed = 0
    for base in MERGE:
        suffixed = f"{base}, Новокузнецк"
        cur.execute(
            "UPDATE events SET venue = ? WHERE venue = ?", (base, suffixed)
        )
        n = cur.rowcount
        if n:
            changed += n
            print(f"  {suffixed!r} -> {base!r} ({n} событий)")

    conn.commit()

    # Итоговая проверка: не осталось ли объединяемых дублей.
    for base in MERGE:
        cur.execute("SELECT COUNT(*) FROM events WHERE venue = ?", (f"{base}, Новокузнецк",))
        left = cur.fetchone()[0]
        if left:
            print(f"  WARN: осталось {left} записей {base!r}, Новокузнецк")
    print(f"Готово. Изменено событий: {changed}")
    conn.close()


if __name__ == "__main__":
    main()