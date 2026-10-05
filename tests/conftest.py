from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlmodel import Session

from jobpilot.db.session import get_engine

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    """A fresh database built by the real Alembic migrations."""
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    return url


@pytest.fixture
def session(db_url: str):
    with Session(get_engine(db_url)) as s:
        yield s
