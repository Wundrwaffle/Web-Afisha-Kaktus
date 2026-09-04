from datetime import date, time, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .db import Base, build_engine, build_session_factory, session_dependency
from .models import Event, User
from .auth import build_auth_routes


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_URL = f"sqlite:///{PROJECT_ROOT / 'afisha.sqlite3'}"

# Демо-события датируются ОТНОСИТЕЛЬНО сегодняшнего дня (в будущем), иначе со
# временем они «устаревают» и публичный каталог (date_from = today) их не отдаёт.
_TODAY = date.today()

DEMO_EVENTS = [
    {
        "title": "Город звучит",
        "slug": "gorod-zvuchit",
        "status": "published",
        "category": "Музыка",
        "date": _TODAY + timedelta(days=1),
        "time": time(18, 0),
        "venue": "Парк Гагарина",
        "price": "Бесплатно",
    },
    {
        "title": "Большой экран",
        "slug": "bolshoy-ekran",
        "status": "published",
        "category": "Кино",
        "date": _TODAY + timedelta(days=2),
        "time": time(21, 30),
        "venue": "Площадь Побед",
        "price": "от 350 ₽",
    },
]


class EventCreate(BaseModel):
    title: str = Field(min_length=3, max_length=160)
    slug: str = Field(min_length=3, max_length=180, pattern=r"^[a-z0-9-]+$")
    category: str = Field(min_length=2, max_length=80)
    date: date
    time: time
    venue: str = Field(min_length=2, max_length=180)
    price: str = Field(min_length=1, max_length=80)


def serialize_event(event: Event) -> dict[str, object]:
    return {
        "id": f"event-{event.id}",
        "event_id": event.id,
        "title": event.title,
        "slug": event.slug,
        "status": event.status,
        "category": event.category,
        "date": event.date.isoformat(),
        "time": event.time.strftime("%H:%M"),
        "venue": event.venue,
        "price": event.price,
        "organizer_id": event.organizer_id,
    }


def seed_demo_events(session: Session) -> None:
    if session.scalar(select(Event.id).limit(1)) is not None:
        return

    session.add_all(Event(**event) for event in DEMO_EVENTS)
    session.commit()


def create_app(database_url: str = DEFAULT_DATABASE_URL) -> FastAPI:
    engine = build_engine(database_url)
    Base.metadata.create_all(engine)

    # Лёгкая прототипная миграция: колонки, добавленные позже, которых нет в старых
    # sqlite-файлах. create_all не умеет ALTER существующие таблицы, поэтому добиваем
    # недостающие колонки вручную (идемпотентно — при наличии ничего не делаем).
    if database_url.startswith("sqlite"):
        from sqlalchemy import text as _text
        from sqlalchemy import inspect as _inspect
        _insp = _inspect(engine)
        if "events" in _insp.get_table_names():
            _cols = {c["name"] for c in _insp.get_columns("events")}
            if "organizer_id" not in _cols:
                with engine.begin() as _conn:
                    _conn.execute(_text(
                        "ALTER TABLE events ADD COLUMN organizer_id INTEGER REFERENCES users(id)"
                    ))

    session_factory = build_session_factory(engine)

    app = FastAPI(
        title="чтоунастамзавтра API",
        version="0.2.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:4173", "http://localhost:4173", "http://localhost:8080", "http://127.0.0.1:8080", "null"],
        allow_origin_regex=r"https?://(127\.0\.0\.1|localhost|192\.168\.\d+\.\d+)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    def get_session():
        yield from session_dependency(session_factory)

    with session_factory() as session:
        seed_demo_events(session)

    auth = build_auth_routes(session_factory)
    app.include_router(auth["router"])
    get_current_user = auth["get_current_user"]
    require_role = auth["require_role"]

    @app.get("/api/v1/health")
    def health_check() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/events", status_code=201)
    def create_event(
        payload: EventCreate,
        session: Session = Depends(get_session),
        current_user: User = Depends(require_role("organizer", "admin")),
    ) -> dict[str, object]:
        event = Event(
            **payload.model_dump(),
            organizer_id=current_user.id,
            status="pending_moderation",
        )
        session.add(event)
        session.commit()
        session.refresh(event)
        return serialize_event(event)

    @app.get("/api/v1/events")
    def list_events(
        search: str | None = Query(default=None, min_length=1),
        category: str | None = None,
        venue: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        sort: str = "date",
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        statement = select(Event).where(Event.status == "published")

        # По умолчанию показываем события только с сегодняшнего дня (не прошедшие).
        if date_from is None:
            date_from = date.today()

        if search:
            # SQLite ilike не «сворачивает» регистр для кириллицы (только ASCII),
            # поэтому поиск по заглавной/строчной кириллице не находил нижний/верхний
            # регистр. Нормализуем обе стороны через lower().
            pattern = f"%{search.lower()}%"
            statement = statement.where(
                or_(
                    func.lower(Event.title).like(pattern),
                    func.lower(Event.category).like(pattern),
                    func.lower(Event.venue).like(pattern),
                )
            )
        if category:
            statement = statement.where(Event.category == category)
        if venue:
            statement = statement.where(Event.venue == venue)
        if date_from:
            statement = statement.where(Event.date >= date_from)
        if date_to:
            statement = statement.where(Event.date <= date_to)

        statement = statement.order_by(
            func.lower(Event.title) if sort == "title" else Event.date, Event.time
        )
        events = session.scalars(statement).all()
        items = [serialize_event(event) for event in events]
        return {"items": items, "total": len(items)}

    @app.get("/api/v1/meta/venues")
    def list_venues(session: Session = Depends(get_session)) -> dict[str, object]:
        statement = (
            select(Event.venue)
            .where(Event.status == "published")
            .order_by(Event.venue)
            .distinct()
        )
        venues = session.scalars(statement).all()
        return {"items": venues}

    @app.get("/api/v1/events/calendar")
    def calendar_events(
        start_date: date,
        end_date: date,
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        statement = (
            select(Event)
            .where(
                Event.status == "published",
                Event.date >= start_date,
                Event.date <= end_date,
            )
            .order_by(Event.date, Event.time)
        )
        events = session.scalars(statement).all()
        items = [serialize_event(event) for event in events]
        return {"items": items, "total": len(items)}

    @app.get("/api/v1/events/{slug}")
    def get_event(slug: str, session: Session = Depends(get_session)) -> dict[str, object]:
        statement = select(Event).where(
            Event.slug == slug,
            Event.status == "published",
        )
        event = session.scalar(statement)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found")
        return serialize_event(event)

    # --- Кабинет организатора ---
    @app.get("/api/v1/me/events")
    def my_events(
        session: Session = Depends(get_session),
        current_user: User = Depends(require_role("organizer", "admin")),
    ) -> dict[str, object]:
        statement = (
            select(Event)
            .where(Event.organizer_id == current_user.id)
            .order_by(Event.date.desc(), Event.time)
        )
        events = session.scalars(statement).all()
        items = [serialize_event(event) for event in events]
        return {"items": items, "total": len(items)}

    # --- Модерация ---
    class ReviewRequest(BaseModel):
        decision: str = Field(pattern=r"^(approve|reject)$")

    @app.get("/api/v1/moderation/queue")
    def moderation_queue(
        session: Session = Depends(get_session),
        _: User = Depends(require_role("moderator", "admin")),
    ) -> dict[str, object]:
        statement = (
            select(Event)
            .where(Event.status == "pending_moderation")
            .order_by(Event.date, Event.time)
        )
        events = session.scalars(statement).all()
        items = [serialize_event(event) for event in events]
        return {"items": items, "total": len(items)}

    @app.post("/api/v1/moderation/events/{event_id}/review")
    def review_event(
        event_id: int,
        payload: ReviewRequest,
        session: Session = Depends(get_session),
        _: User = Depends(require_role("moderator", "admin")),
    ) -> dict[str, object]:
        event = session.get(Event, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found")
        if event.status != "pending_moderation":
            raise HTTPException(status_code=409, detail="Event is not pending moderation")
        event.status = "published" if payload.decision == "approve" else "rejected"
        session.commit()
        session.refresh(event)
        return serialize_event(event)

    return app


app = create_app()
