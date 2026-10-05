"""The one database session layer every agent and the API share."""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from jobpilot.config import get_config


def _sqlite_path(url: str) -> Path | None:
    prefix = "sqlite:///"
    if url.startswith(prefix) and ":memory:" not in url:
        return Path(url[len(prefix):])
    return None


def _on_connect(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    # WAL lets the dashboard read while an agent writes.
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    url = url or get_config().database_url
    path = _sqlite_path(url)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False})
    event.listen(engine, "connect", _on_connect)
    return engine


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    """Commit on success, roll back on error."""
    with Session(get_engine(url)) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
