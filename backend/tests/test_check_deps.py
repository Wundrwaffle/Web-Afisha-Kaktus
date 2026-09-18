"""Тесты проверки зависимостей (backend/check_deps.py).

Ловушка, ради которой это написано: батники проверяли только `import uvicorn, fastapi`,
поэтому неполный набор пакетов выглядел как исправный, а падение случалось позже -
в `expose.py`, на импорте sqlalchemy. Тесты держат проверку привязанной к
requirements.txt: новая зависимость не может провалиться в тихую эвристику.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import check_deps  # noqa: E402

REAL_REQUIREMENTS = check_deps.parse_requirements(
    check_deps.REQUIREMENTS.read_text(encoding="utf-8")
)


def test_parse_requirements_strips_versions_extras_comments_and_includes():
    text = (
        "# комментарий\n"
        "fastapi>=0.110.0\n"
        "uvicorn[standard]>=0.29.0\n"
        "bcrypt==4.0.1\n"
        "\n"
        "-r other.txt\n"
    )
    assert check_deps.parse_requirements(text) == ["fastapi", "uvicorn", "bcrypt"]


def test_real_requirements_cover_the_package_that_broke_the_show():
    # Показ падал именно на sqlalchemy: она обязана быть в проверке.
    assert "sqlalchemy" in REAL_REQUIREMENTS
    assert check_deps.import_name("sqlalchemy") == "sqlalchemy"


def test_import_aliases_only_name_real_requirements():
    # Мёртвая строка в карте алиасов = опечатка, которую иначе никто не заметит.
    unknown = sorted(set(check_deps.IMPORT_ALIASES) - set(REAL_REQUIREMENTS))
    assert unknown == []


def test_every_requirement_maps_to_a_module_name():
    for package in REAL_REQUIREMENTS:
        assert check_deps.import_name(package)
        assert "[" not in check_deps.import_name(package)


def test_missing_package_is_detected():
    # Отсутствующий пакет должен попадать в отчёт - это и есть причина бага.
    assert check_deps.missing_modules(["definitely-not-installed-package"]) == [
        "definitely_not_installed_package"
    ]


def test_installed_packages_are_reported_as_present():
    assert check_deps.missing_modules(["pytest", "sqlalchemy"]) == []
