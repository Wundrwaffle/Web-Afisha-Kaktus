"""Юридические документы: скачивание по коротким адресам.

ТЗ требует, чтобы `/terms`, `/privacy`, `/cookies` и `/recommendations`
открывались без авторизации и вели на соответствующий документ.
Документы — не страницы, а файлы: пользователь скачивает PDF с читаемым
именем, поэтому отдаём их FileResponse-ом, а не статикой (StaticFiles отдал
бы имя файла на диске).

Файлы лежат в docs/legal/ и собираются `backend/build_legal_stubs.py`.
Пока это заглушки: после публикации настоящих редакций меняются имена
в LEGAL_DOCS, сами роуты остаются те же.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

LEGAL_DIR = Path(__file__).resolve().parents[2] / "docs" / "legal"

# slug в адресе → (файл на диске, имя при скачивании)
LEGAL_DOCS: dict[str, tuple[str, str]] = {
    "terms": (
        "terms-draft.pdf",
        "Пользовательское соглашение — чотамзавтра.рф (черновик).pdf",
    ),
    "privacy": (
        "privacy-draft.pdf",
        "Политика конфиденциальности — чотамзавтра.рф (черновик).pdf",
    ),
    "cookies": (
        "cookies-draft.pdf",
        "Политика cookie — чотамзавтра.рф (черновик).pdf",
    ),
    "recommendations": (
        "recommendations-draft.pdf",
        "Правила применения рекомендательных технологий — чотамзавтра.рф (черновик).pdf",
    ),
}


def legal_document_path(slug: str) -> Path:
    try:
        filename = LEGAL_DOCS[slug][0]
    except KeyError:  # pragma: no cover - защита от опечатки в вызове
        raise HTTPException(status_code=404, detail="Document not found") from None
    path = LEGAL_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Document not found")
    return path


def download_legal_document(slug: str) -> FileResponse:
    return FileResponse(
        legal_document_path(slug),
        media_type="application/pdf",
        filename=LEGAL_DOCS[slug][1],
    )


def offer_redirect() -> RedirectResponse:
    """/offer не существует как отдельный документ: ТЗ требует вести на /terms."""
    return RedirectResponse(url="/terms", status_code=301)


def register_legal_routes(app: FastAPI) -> None:
    for slug in LEGAL_DOCS:
        app.add_api_route(
            f"/{slug}",
            partial(download_legal_document, slug=slug),
            methods=["GET"],
            name=f"legal-{slug}",
            include_in_schema=False,
        )
    app.add_api_route(
        "/offer",
        offer_redirect,
        methods=["GET"],
        name="legal-offer-redirect",
        include_in_schema=False,
    )
