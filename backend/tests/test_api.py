from datetime import date, time, timedelta

from fastapi.testclient import TestClient

from app.main import DEMO_EVENTS, create_app


def test_health_check_returns_ok(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'test.sqlite3'}")

    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_public_events_are_loaded_from_sqlite(tmp_path):
    database_path = tmp_path / "events.sqlite3"
    app = create_app(f"sqlite:///{database_path}")

    with TestClient(app) as client:
        response = client.get("/api/v1/events")

    assert response.status_code == 200
    body = response.json()
    assert body["items"]
    assert all(event["status"] == "published" for event in body["items"])
    assert body["total"] == len(body["items"])
    assert database_path.exists()

    # A second app instance must read the same persisted events.
    second_app = create_app(f"sqlite:///{database_path}")
    with TestClient(second_app) as client:
        second_response = client.get("/api/v1/events")

    assert second_response.json() == body


def test_event_detail_returns_published_event_by_slug(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'detail.sqlite3'}")

    with TestClient(app) as client:
        response = client.get("/api/v1/events/gorod-zvuchit")

    assert response.status_code == 200
    assert response.json()["title"] == "Город звучит"
    assert response.json()["slug"] == "gorod-zvuchit"


def test_event_detail_returns_404_for_unknown_slug(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'missing.sqlite3'}")

    with TestClient(app) as client:
        response = client.get("/api/v1/events/not-found")

    assert response.status_code == 404


def test_events_support_search_category_and_date_filters(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'filters.sqlite3'}")
    bolshoy_date = DEMO_EVENTS[1]["date"].isoformat()

    with TestClient(app) as client:
        search_response = client.get("/api/v1/events", params={"search": "экран"})
        category_response = client.get("/api/v1/events", params={"category": "Музыка"})
        date_response = client.get(
            "/api/v1/events",
            params={"date_from": bolshoy_date, "date_to": bolshoy_date},
        )

    assert search_response.json()["total"] == 1
    assert search_response.json()["items"][0]["slug"] == "bolshoy-ekran"
    assert category_response.json()["total"] == 1
    assert category_response.json()["items"][0]["category"] == "Музыка"
    assert date_response.json()["total"] == 1
    assert date_response.json()["items"][0]["date"] == bolshoy_date


def test_api_allows_frontend_origin(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'cors.sqlite3'}")

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/events",
            headers={"Origin": "http://127.0.0.1:4173"},
        )

    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:4173"


def test_events_support_title_sorting(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'sorting.sqlite3'}")

    with TestClient(app) as client:
        response = client.get("/api/v1/events", params={"sort": "title"})

    titles = [event["title"] for event in response.json()["items"]]
    assert titles == ["Большой экран", "Город звучит"]


def test_search_is_case_insensitive_for_cyrillic(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'cyr.sqlite3'}")

    with TestClient(app) as client:
        upper = client.get("/api/v1/events", params={"search": "ЭКРАН"})
        lower = client.get("/api/v1/events", params={"search": "экран"})

    # SQLite без ICU не сворачивает регистр кириллицы; наш кастомный lower()
    # должен находить «Большой экран» в обоих регистрах.
    assert upper.json()["total"] == 1
    assert upper.json()["items"][0]["slug"] == "bolshoy-ekran"
    assert lower.json()["total"] == 1
    assert lower.json()["items"][0]["slug"] == "bolshoy-ekran"


def test_title_sort_orders_cyrillic_case_insensitively(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'sort_cyr.sqlite3'}")

    with TestClient(app) as client:
        response = client.get("/api/v1/events", params={"sort": "title"})

    titles = [event["title"] for event in response.json()["items"]]
    assert titles == ["Большой экран", "Город звучит"]


def test_calendar_returns_events_inside_date_range(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'calendar.sqlite3'}")
    bolshoy_date = DEMO_EVENTS[1]["date"].isoformat()

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/events/calendar",
            params={"start_date": bolshoy_date, "end_date": bolshoy_date},
        )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["slug"] == "bolshoy-ekran"


def test_events_pagination_limit_and_offset(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'pag.sqlite3'}"
    app = create_app(db_url)

    # Создаём достаточно published-событий в будущем, чтобы была пагинация.
    from sqlalchemy import select

    from app.db import build_engine, build_session_factory
    from app.models import Event

    engine = build_engine(db_url)
    with build_session_factory(engine)() as session:
        for i in range(5):
            session.add(Event(
                title=f"Пагинация {i}",
                slug=f"pagination-{i}",
                status="published",
                category="Тест",
                date=date.today() + timedelta(days=1 + i),
                time=time(18, 0),
                venue="Площадка",
                price="Бесплатно",
            ))
        session.commit()

    with TestClient(app) as client:
        page1 = client.get("/api/v1/events", params={"limit": 2, "offset": 0})
        page2 = client.get("/api/v1/events", params={"limit": 2, "offset": 2})

    body1 = page1.json()
    body2 = page2.json()

    # total — полное число совпадений (демо 2 + наши 5), не зависит от limit.
    assert body1["total"] == 7
    assert len(body1["items"]) == 2
    assert body1["has_more"] is True

    assert body2["total"] == 7
    assert len(body2["items"]) == 2
    assert body2["has_more"] is True

    # Страницы не пересекаются по slug.
    slugs1 = {e["slug"] for e in body1["items"]}
    slugs2 = {e["slug"] for e in body2["items"]}
    assert slugs1.isdisjoint(slugs2)


def test_events_pagination_last_page_has_no_more(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'pag_last.sqlite3'}"
    app = create_app(db_url)

    with TestClient(app) as client:
        # Демо-событий всего 2; запрашиваем limit=2 offset=0 — это вся выдача.
        body = client.get("/api/v1/events", params={"limit": 2, "offset": 0}).json()

    assert body["total"] == 2
    assert len(body["items"]) == 2
    assert body["has_more"] is False


def test_create_event_starts_in_pending_moderation(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'create.sqlite3'}")
    future_date = (date.today() + timedelta(days=3)).isoformat()
    payload = {
        "title": "Новая выставка",
        "slug": "novaya-vystavka",
        "category": "Культура",
        "date": future_date,
        "time": "19:00",
        "venue": "Арт-пространство",
        "price": "Бесплатно",
    }

    with TestClient(app) as client:
        # Анонимный вызов теперь отклоняется (401): событие создаёт организатор.
        anonymous = client.post("/api/v1/events", json=payload)

    assert anonymous.status_code == 401
