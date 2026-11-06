import os

import config  # noqa: F401  (loads the .env file)
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./cbt.db")

engine = create_engine(
    DATABASE_URL,
    # FastAPI handles requests on different threads
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")   # SQLite ignores foreign keys unless this is set
    cursor.execute("PRAGMA journal_mode=WAL")  # readers don't block the single writer
    cursor.execute("PRAGMA busy_timeout=5000") # wait up to 5s instead of failing when locked
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency: one DB session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
