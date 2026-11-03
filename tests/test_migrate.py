"""
Startup migration: a database built before the agent_runs origin columns (revision 0005) is brought up to date
by ensure_current, the API and agents then work on it, and running it again changes nothing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from jobpilot.agents._run import agent_run
from jobpilot.api.main import create_app
from jobpilot.db import migrate
from jobpilot.db.models import AgentName, AgentRun
from jobpilot.db.session import get_engine
from tests.conftest import ROOT

NEW_COLUMNS = {"caller", "auto_submit_requested", "auto_submit_setting"}


def build(tmp_path: Path, revision: str) -> str:
    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, revision)
    return url


def raw(url: str) -> sqlite3.Connection:
    return sqlite3.connect(url.removeprefix("sqlite:///"))


def columns(url: str) -> set[str]:
    with raw(url) as db:
        return {row[1] for row in db.execute("PRAGMA table_info(agent_runs)")}


def add_old_runs(url: str) -> None:
    with raw(url) as db:
        db.executemany(
            "INSERT INTO agent_runs (agent, started_at, status, found, processed, error, log, is_example) "
            "VALUES (?, '2026-10-20 10:00:00', 'ok', 3, 5, '', 'old log', 0)", [("discovery",), ("apply",)])


@pytest.fixture
def old_db(tmp_path):
    migrate._checked.clear()
    url = build(tmp_path, "0005")
    add_old_runs(url)
    assert not NEW_COLUMNS & columns(url)   # really the old schema
    return url


def test_old_database_is_migrated_and_old_runs_get_safe_defaults(old_db):
    assert migrate.ensure_current(old_db) is True
    assert NEW_COLUMNS <= columns(old_db)
    with raw(old_db) as db:
        rows = db.execute("SELECT agent, caller, auto_submit_requested, auto_submit_setting, found, log FROM agent_runs ORDER BY id").fetchall()
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0007"
    assert rows == [("discovery", "unknown", 0, None, 3, "old log"), ("apply", "unknown", 0, None, 3, "old log")]


def test_a_backup_of_the_old_database_is_taken_first(old_db):
    migrate.ensure_current(old_db)
    backups = list((Path(old_db.removeprefix("sqlite:///")).parent / "backups").glob("old_before_0005_*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("SELECT count(*) FROM agent_runs").fetchone()[0] == 2
        assert NEW_COLUMNS.isdisjoint({row[1] for row in db.execute("PRAGMA table_info(agent_runs)")})


def test_running_the_migration_twice_is_harmless(old_db):
    assert migrate.ensure_current(old_db) is True
    with raw(old_db) as db:
        db.execute("UPDATE agent_runs SET caller = 'dashboard', auto_submit_requested = 1 WHERE id = 1")
    assert migrate.ensure_current(old_db, force=True) is False                 # nothing left to do
    assert migrate.ensure_current(old_db, force=True) is False
    with raw(old_db) as db:
        assert db.execute("SELECT count(*) FROM agent_runs").fetchone()[0] == 2
        assert db.execute("SELECT caller, auto_submit_requested FROM agent_runs WHERE id = 1").fetchone() == ("dashboard", 1)
    assert len(list((Path(old_db.removeprefix("sqlite:///")).parent / "backups").glob("*.db"))) == 1   # no second backup


def test_alembic_upgrade_after_the_startup_migration_is_a_no_op(old_db):
    migrate.ensure_current(old_db)
    cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", old_db)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")   # what README says to run by hand: must not fail on an already-migrated database


def test_migration_0006_tolerates_columns_that_are_already_there(tmp_path):
    migrate._checked.clear()
    url = build(tmp_path, "0005")
    with raw(url) as db:
        db.execute("ALTER TABLE agent_runs ADD COLUMN caller VARCHAR(16)")   # added by hand before alembic got to it
    assert migrate.ensure_current(url) is True
    assert NEW_COLUMNS <= columns(url)


def test_api_meta_and_runs_work_on_a_migrated_old_database(old_db):
    with TestClient(create_app(old_db, test_mode=True)) as client:   # entering the client runs the startup hook
        meta = client.get("/api/meta")
        runs = client.get("/api/runs")
    assert meta.status_code == 200 and meta.json()["db"] == "ok"
    assert runs.status_code == 200 and {r["agent"] for r in runs.json()} == {"discovery", "apply"}
    assert {r["caller"] for r in runs.json()} == {"unknown"}


def test_an_unmigrated_old_database_fails_the_way_production_did(tmp_path):
    migrate._checked.clear()
    stale = build(tmp_path, "0005")    # control: the old schema with no startup migration
    with pytest.raises(OperationalError, match="agent_runs.caller"):
        with Session(get_engine(stale)) as session:
            session.exec(select(AgentRun)).all()


def test_agents_migrate_a_stale_database_before_recording_a_run(old_db):
    with agent_run(AgentName.discovery, old_db) as recorder:
        recorder.log("hello")
    with raw(old_db) as db:
        assert db.execute("SELECT count(*) FROM agent_runs").fetchone()[0] == 3
        assert db.execute("SELECT caller FROM agent_runs ORDER BY id DESC LIMIT 1").fetchone()[0] is None   # a fresh CLI run, not "unknown"


def test_columns_dropped_from_a_database_already_at_head_are_added_back(tmp_path):
    migrate._checked.clear()
    url = build(tmp_path, "head")
    add_old_runs(url)
    with raw(url) as db:
        for name in NEW_COLUMNS:
            db.execute(f"ALTER TABLE agent_runs DROP COLUMN {name}")
    assert migrate.ensure_current(url) is True
    with raw(url) as db:
        assert db.execute("SELECT caller, auto_submit_requested FROM agent_runs").fetchall() == [("unknown", 0), ("unknown", 0)]
    assert migrate.ensure_current(url, force=True) is False


def test_a_database_without_alembic_is_left_alone(tmp_path):
    migrate._checked.clear()
    path = tmp_path / "plain.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE agent_runs (id INTEGER PRIMARY KEY)")
    url = f"sqlite:///{path.as_posix()}"
    assert migrate.ensure_current(url) is False
    assert columns(url) == {"id"}


def test_a_required_column_without_a_default_asks_for_a_real_migration(tmp_path, monkeypatch):
    migrate._checked.clear()
    url = build(tmp_path, "head")
    with raw(url) as db:
        db.execute("ALTER TABLE agent_runs DROP COLUMN is_example")   # NOT NULL in the models, with only a Python-side default
    monkeypatch.setattr(migrate, "current_revision", lambda engine: "0007")
    with pytest.raises(RuntimeError, match="is_example"):
        migrate.add_missing_columns(get_engine(url))
