"""Engine, session factory and DB initialisation (one SQLite file)."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import DATA_DIR, DATABASE_URL

DATA_DIR.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - trivial
    if DATABASE_URL.startswith("sqlite"):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(drop: bool = False) -> None:
    """Create all tables (optionally dropping them first)."""
    from app import models  # noqa: F401  (register models)

    if drop:
        models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)
    models.add_missing_columns(engine)  # an existing database gets the columns added since it was created


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
