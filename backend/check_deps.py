"""Проверка зависимостей бэкенда перед запуском батников (start.bat / expose.bat).

Зачем отдельный скрипт: проверка в батниках была `python -c "import uvicorn, fastapi"`.
На машине пользователя uvicorn и fastapi стояли, а sqlalchemy - нет: проверка
проходила, установка не запускалась, и `backend/expose.py` падал позже на
`ModuleNotFoundError: No module named 'sqlalchemy'`. Здесь состав берётся из
`backend/requirements.txt`, поэтому новая зависимость проверяется автоматически,
а имена недостающих модулей печатаются в окно.

Запуск: python backend/check_deps.py   (код 0 - всё на месте, 1 - чего-то нет)
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

REQUIREMENTS = pathlib.Path(__file__).resolve().parent / "requirements.txt"

# Имя пакета в requirements.txt -> имя модуля для импорта (там, где они расходятся).
IMPORT_ALIASES = {
    "python-multipart": "multipart",
    "python-jose": "jose",
    "pydantic-settings": "pydantic_settings",
    "email-validator": "email_validator",
}

_SEPARATORS = ("===", "==", ">=", "<=", "~=", "!=", ">", "<")


def parse_requirements(text: str) -> list[str]:
    """Имена пакетов из requirements.txt без версий, extras, комментариев и -r строк."""
    names: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = line.split("[", 1)[0]
        for sep in _SEPARATORS:
            name = name.split(sep, 1)[0]
        name = name.strip()
        if name:
            names.append(name)
    return names


def import_name(package: str) -> str:
    """Имя модуля для импорта по имени пакета из requirements.txt."""
    return IMPORT_ALIASES.get(package, package.replace("-", "_"))


def missing_modules(packages: list[str]) -> list[str]:
    """Модули из списка пакетов, которых нет в текущем интерпретаторе."""
    return [import_name(p) for p in packages if importlib.util.find_spec(import_name(p)) is None]


def main() -> int:
    packages = parse_requirements(REQUIREMENTS.read_text(encoding="utf-8"))
    missing = missing_modules(packages)
    if missing:
        print(f"Не установлены зависимости: {', '.join(missing)}")
        return 1
    print(f"Зависимости на месте ({len(packages)} пакетов).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
