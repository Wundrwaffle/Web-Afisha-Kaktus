from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def _sqlite_lower(value: str) -> str:
    # SQLite без ICU сворачивает регистр только для ASCII; кириллица в lower()
    # не опускается. Регистрируем свою функцию на Python-е str.lower(), чтобы
    # func.lower() в запросах нормализовал и кириллицу.
    if value is None:
        return None
    return str(value).lower()


def build_engine(database_url: str):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)
    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _register_lower(dbapi_conn, _connection_record):
            dbapi_conn.create_function("lower", 1, _sqlite_lower, deterministic=True)
    return engine


def build_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def session_dependency(session_factory) -> Generator[Session, None, None]:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
