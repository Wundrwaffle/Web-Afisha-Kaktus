from datetime import date, time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .db import Base, build_engine, build_session_factory, session_dependency
from .models import Event


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_URL = f"sqlite:///{PROJECT_ROOT / 'afisha.sqlite3'}"

DEMO_EVENTS = [
    {
        "title": "Город звучит",
        "slug": "gorod-zvuchit",
        "status": "published",
        "category": "Музыка",
        "date": date(2026, 6, 22),
        "time": time(18, 0),
        "venue": "Парк Гагарина",
        "price": "Бесплатно",
    },
    {
        "title": "Большой экран",
        "slug": "bolshoy-ekran",
        "status": "published",
        "category": "Кино",
        "date": date(2026, 6, 23),
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
        "title": event.title,
        "slug": event.slug,
        "status": event.status,
        "category": event.category,
        "date": event.date.isoformat(),
        "time": event.time.strftime("%H:%M"),
        "venue": event.venue,
        "price": event.price,
    }


def seed_demo_events(session: Session) -> None:
    if session.scalar(select(Event.id).limit(1)) is not None:
        return

    session.add_all(Event(**event) for event in DEMO_EVENTS)
    session.commit()


def create_app(database_url: str = DEFAULT_DATABASE_URL) -> FastAPI:
    engine = build_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = build_session_factory(engine)

    app = FastAPI(
        title="чтоунастамзавтра API",
        version="0.2.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:4173", "http://localhost:4173"],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    def get_session():
        yield from session_dependency(session_factory)

    with session_factory() as session:
        seed_demo_events(session)

    @app.get("/api/v1/health")
    def health_check() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/events", status_code=201)
    def create_event(payload: EventCreate, session: Session = Depends(get_session)) -> dict[str, object]:
        event = Event(**payload.model_dump(), status="pending_moderation")
        session.add(event)
        session.commit()
        session.refresh(event)
        return serialize_event(event)

    @app.get("/api/v1/events")
    def list_events(
        search: str | None = Query(default=None, min_length=1),
        category: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        sort: str = "date",
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        statement = select(Event).where(Event.status == "published")

        if search:
            pattern = f"%{search}%"
            statement = statement.where(
                or_(
                    Event.title.ilike(pattern),
                    Event.category.ilike(pattern),
                    Event.venue.ilike(pattern),
                )
            )
        if category:
            statement = statement.where(Event.category == category)
        if date_from:
            statement = statement.where(Event.date >= date_from)
        if date_to:
            statement = statement.where(Event.date <= date_to)

        statement = statement.order_by(Event.title if sort == "title" else Event.date, Event.time)
        events = session.scalars(statement).all()
        items = [serialize_event(event) for event in events]
        return {"items": items, "total": len(items)}

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

    return app


app = create_app()
