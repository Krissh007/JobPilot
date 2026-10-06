"""Request-scoped DB session and small helpers shared by the routers."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator

from fastapi import HTTPException, Request
from sqlmodel import Session

from jobpilot.db.session import get_engine

IST = timezone(timedelta(hours=5, minutes=30))


def get_session(request: Request) -> Iterator[Session]:
    with Session(get_engine(request.app.state.db_url)) as session:
        yield session


def ist_date(value: datetime | None) -> date | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST).date()


def today_ist() -> date:
    return datetime.now(IST).date()


def get_or_404(session: Session, model: Any, ident: int) -> Any:
    row = session.get(model, ident)
    if row is None:
        raise HTTPException(404, f"{model.__tablename__[:-1]} {ident} not found")
    return row
