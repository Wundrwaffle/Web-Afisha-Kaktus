from fastapi.testclient import TestClient

from backend.app.main import create_app


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

    with TestClient(app) as client:
        search_response = client.get("/api/v1/events", params={"search": "экран"})
        category_response = client.get("/api/v1/events", params={"category": "Музыка"})
        date_response = client.get(
            "/api/v1/events",
            params={"date_from": "2026-06-23", "date_to": "2026-06-23"},
        )

    assert search_response.json()["total"] == 1
    assert search_response.json()["items"][0]["slug"] == "bolshoy-ekran"
    assert category_response.json()["total"] == 1
    assert category_response.json()["items"][0]["category"] == "Музыка"
    assert date_response.json()["total"] == 1
    assert date_response.json()["items"][0]["date"] == "2026-06-23"


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


def test_calendar_returns_events_inside_date_range(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'calendar.sqlite3'}")

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/events/calendar",
            params={"start_date": "2026-06-23", "end_date": "2026-06-23"},
        )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["slug"] == "bolshoy-ekran"


def test_create_event_starts_in_pending_moderation(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'create.sqlite3'}")
    payload = {
        "title": "Новая выставка",
        "slug": "novaya-vystavka",
        "category": "Культура",
        "date": "2026-06-25",
        "time": "19:00",
        "venue": "Арт-пространство",
        "price": "Бесплатно",
    }

    with TestClient(app) as client:
        create_response = client.post("/api/v1/events", json=payload)
        public_response = client.get("/api/v1/events")

    assert create_response.status_code == 201
    created_event = create_response.json()
    assert created_event["slug"] == "novaya-vystavka"
    assert created_event["status"] == "pending_moderation"
    assert public_response.json()["total"] == 2
    assert all(event["slug"] != "novaya-vystavka" for event in public_response.json()["items"])
