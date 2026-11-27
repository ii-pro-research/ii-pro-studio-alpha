import hashlib
import shutil
import sqlite3

import pytest

from ii_pro_core import Core
import ii_pro_core.storage as storage
from ii_pro_core.storage import MIGRATIONS, SCHEMA_VERSION
from ii_pro_school import School
from test_school import FakeStudent


def v6_school(path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 6)
        with School(path, FakeStudent()) as school:
            school.install_curriculum()
            school.register_student("fixture", "Offline fixture")
            session = school.create_session("fixture")
            run = school.prepare_attempt(session, "bug-positive-sum")
            school.execute(run.id)
            school.assess(run.id)
            return session, school.export_session(session)


def school_rows(path):
    with sqlite3.connect(path / "core.sqlite3") as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'school_%' AND name NOT LIKE 'school_v2_%' ORDER BY name")]
        return {table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables}


def test_v6_to_v7_backup_and_school_export_unchanged(tmp_path, monkeypatch):
    original, copy = tmp_path / "old", tmp_path / "copy"
    session, expected = v6_school(original, monkeypatch)
    checksum = hashlib.sha256((original / "core.sqlite3").read_bytes()).hexdigest()
    shutil.copytree(original, copy)
    before = school_rows(copy)
    adapter = FakeStudent()
    with School(copy, adapter) as school:
        assert school.export_session(session) == expected
        assert school.store.schema_version == SCHEMA_VERSION
        assert adapter.calls == 0
        assert school.store.one("PRAGMA integrity_check")[0] == "ok"
        assert school.store.all("PRAGMA foreign_key_check") == []
    assert school_rows(copy) == before
    assert hashlib.sha256((original / "core.sqlite3").read_bytes()).hexdigest() == checksum
    backups = list((copy / "backups").glob(f"pre-v{SCHEMA_VERSION}-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_failed_v7_migration_rolls_back_new_tables_and_preserves_history(tmp_path, monkeypatch):
    state = tmp_path / "state"
    session, expected = v6_school(state, monkeypatch)
    before = school_rows(state)
    with monkeypatch.context() as patch:
        patch.setitem(MIGRATIONS, 7, (*MIGRATIONS[7], "INVALID SQL"))
        with pytest.raises(sqlite3.OperationalError):
            Core(state)
    with sqlite3.connect(state / "core.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert db.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 6
        assert db.execute("SELECT name FROM sqlite_master WHERE name='worker_jobs'").fetchone() is None
        assert db.execute("SELECT name FROM sqlite_master WHERE name='artifact_blobs'").fetchone() is None
    assert school_rows(state) == before
    with School(state, FakeStudent()) as school:
        assert school.export_session(session) == expected
