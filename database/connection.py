"""
Database connection.

Supports both SQLite (default, zero-config for local dev) and PostgreSQL
(e.g. Neon, for production) based on the JOBS_DB setting:

  - SQLite:    a bare path like "database/jobs.db" (relative paths are anchored
               to the project root, and the folder is auto-created).
  - Postgres:  a full URL like "postgresql://user:pass@host/dbname".
               A "postgres://" scheme is normalized to "postgresql://", and the
               psycopg (v3) driver is used.

This keeps local dev frictionless (SQLite) while allowing the same code to run
against Postgres in deployment.
"""

from pathlib import Path

from sqlmodel import Session, create_engine

from config import get_settings

settings = get_settings()

_raw = (settings.JOBS_DB or "").strip()


def _is_postgres(url: str) -> bool:
    return url.startswith("postgres://") or url.startswith("postgresql")


if _is_postgres(_raw):
    # --- PostgreSQL (e.g. Neon) ---
    # Normalize legacy scheme and pin the psycopg v3 driver.
    url = _raw
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]

    DATABASE_URL = url
    CONNECT_ARGS: dict = {}
    engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)

else:
    # --- SQLite (default) ---
    # Anchor relative paths to the project root (parent of this file's folder)
    # so the app works regardless of the current working directory.
    _path = Path(_raw) if _raw else Path("database/jobs.db")
    if not _path.is_absolute():
        PROJECT_ROOT = Path(__file__).resolve().parent.parent
        _path = PROJECT_ROOT / _path

    # SQLite creates the file but not the parent folder — make sure it exists.
    _path.parent.mkdir(parents=True, exist_ok=True)

    DATABASE_URL = f"sqlite:///{_path}"
    CONNECT_ARGS = {"check_same_thread": False}
    engine = create_engine(DATABASE_URL, echo=False, connect_args=CONNECT_ARGS)


def get_db():
    """Yield a SQLModel session, closing it when done."""
    db = Session(engine)
    try:
        yield db
    finally:
        db.close()
