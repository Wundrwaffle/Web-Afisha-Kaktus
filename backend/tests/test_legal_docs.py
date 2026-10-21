"""Юридические документы по коротким адресам.

Проверяем то, что требует ТЗ и что легко сломать:
- /terms, /privacy, /cookies, /recommendations отдают PDF без авторизации;
- файл приходит как вложение с читаемым русским именем (а не именем на диске);
- /offer не существует отдельным документом и редиректит на /terms (301);
- в публичном режиме static-mount на «/» не перехватывает эти адреса.
"""
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.legal import LEGAL_DOCS, LEGAL_DIR
from app.main import create_app

DOC_SLUGS = ["terms", "privacy", "cookies", "recommendations"]


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'legal.sqlite3'}"


@pytest.fixture
def client(db_url):
    return TestClient(create_app(database_url=db_url))


def _download_name(response) -> str:
    """Имя файла из Content-Disposition (RFC 5987, utf-8''...)."""
    disposition = response.headers["content-disposition"]
    encoded = disposition.split("filename*=utf-8''", 1)[1].strip()
    return unquote(encoded.strip('"'))


def test_all_legal_documents_present_on_disk():
    missing = [name for name, _ in LEGAL_DOCS.values() if not (LEGAL_DIR / name).is_file()]
    assert missing == [], f"нет файлов документов: {missing}"


@pytest.mark.parametrize("slug", DOC_SLUGS)
def test_document_is_downloadable_without_auth(client, slug):
    response = client.get(f"/{slug}")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    assert response.headers["content-disposition"].startswith("attachment")


@pytest.mark.parametrize("slug", DOC_SLUGS)
def test_download_name_is_human_readable(client, slug):
    response = client.get(f"/{slug}")
    name = _download_name(response)

    assert name.endswith(".pdf")
    assert "чтоунастамзавтра.рф" in name
    # Пока документы не вычитаны, имя обязано кричать, что это заглушка.
    assert "черновик" in name.lower()


def test_offer_redirects_to_terms(client):
    response = client.get("/offer", follow_redirects=False)

    assert response.status_code == 301
    assert response.headers["location"] == "/terms"


def test_public_mode_static_mount_does_not_shadow_documents(db_url):
    """В публичном режиме StaticFiles висит на «/» — роуты должны быть раньше."""
    client = TestClient(create_app(database_url=db_url, serve_static=True))

    response = client.get("/terms")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"


def test_documents_are_not_exposed_in_openapi(client):
    schema = client.get("/openapi.json").json()

    assert not [path for path in schema["paths"] if path in {f"/{s}" for s in DOC_SLUGS}]
