"""
Bring a database up to the schema the code expects, at startup, safely and repeatably.

Alembic owns the schema (alembic/versions). Releases that add a migration used to need a manual
`alembic upgrade head`; a forgotten one made every query on the changed table fail with "no such column".
`ensure_current` is called when the API, the scheduled pass or any agent starts:

1. A SQLite database that is behind the newest revision is backed up (data/backups/jobpilot_before_<revision>_<time>.db),
   then upgraded with Alembic.
2. Any model column still missing afterwards (a database stamped at the right revision but built differently) is added
   with ALTER TABLE ADD COLUMN, nullable columns only, and the older-rows defaults are filled in.

Nothing is dropped or rewritten. A second call finds nothing to do. A database with no alembic_version table
is not JobPilot's migrated database, so it is left alone.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel

import jobpilot.db.models  # noqa: F401  (registers the tables on SQLModel.metadata)
from jobpilot.config import ROOT, get_config
from jobpilot.db.session import _sqlite_path, get_engine

log = logging.getLogger("jobpilot.migrate")

# What rows that predate a column get. Anything not listed stays NULL.
OLDER_ROW_DEFAULTS = {("agent_runs", "caller"): "'unknown'", ("agent_runs", "auto_submit_requested"): "0"}
_checked: set[str] = set()


def _alembic_config(url: str) -> AlembicConfig:
    cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    return cfg


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        if "alembic_version" not in sa.inspect(connection).get_table_names():
            return None
        return connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()


def backup_before(url: str, revision: str | None) -> Path | None:
    """A consistent copy (SQLite's online backup API, so the WAL is included) before a schema change."""
    path = _sqlite_path(url)
    if path is None or not path.is_file():
        return None
    target_dir = path.parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{path.stem}_before_{revision or 'none'}_{datetime.now():%Y%m%d_%H%M%S}.db"
    source, copy = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True), sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()
    return target


def add_missing_columns(engine: Engine) -> list[str]:
    """ALTER TABLE ADD COLUMN for model columns the database lacks. Returns "table.column" for each one added."""
    added: list[str] = []
    with engine.begin() as connection:
        existing_tables = set(sa.inspect(connection).get_table_names())
        for name, table in SQLModel.metadata.tables.items():
            if name not in existing_tables:
                continue   # a missing table is a real migration's job
            have = {c["name"] for c in sa.inspect(connection).get_columns(name)}
            for column in table.columns:
                if column.name in have:
                    continue
                if not column.nullable and column.server_default is None:
                    raise RuntimeError(f"{name}.{column.name} is missing and is NOT NULL without a default; "
                                       f"it needs an Alembic migration (alembic upgrade head)")
                ddl = column.type.compile(dialect=engine.dialect)
                connection.execute(sa.text(f'ALTER TABLE "{name}" ADD COLUMN "{column.name}" {ddl}'))
                default = OLDER_ROW_DEFAULTS.get((name, column.name))
                if default is not None:
                    connection.execute(sa.text(f'UPDATE "{name}" SET "{column.name}" = {default} WHERE "{column.name}" IS NULL'))
                added.append(f"{name}.{column.name}")
    return added


def ensure_current(url: str | None = None, *, force: bool = False) -> bool:
    """Migrate the database at `url` (default: DATABASE_URL) if it is behind. True if anything changed.
    Checked once per process per database unless force=True."""
    url = url or get_config().database_url
    if url in _checked and not force:
        return False
    engine = get_engine(url)
    revision = current_revision(engine)
    if revision is None:
        return False   # not a migrated JobPilot database: nothing to do here
    changed = False
    head = ScriptDirectory.from_config(_alembic_config(url)).get_current_head()
    if revision != head:
        backup = backup_before(url, revision)
        log.warning("database is at revision %s, code expects %s: upgrading%s", revision, head,
                    f" (backup: {backup})" if backup else "")
        command.upgrade(_alembic_config(url), "head")
        changed = True
    added = add_missing_columns(engine)
    if added:
        log.warning("added missing columns: %s", ", ".join(added))
        changed = True
    _checked.add(url)
    return changed
