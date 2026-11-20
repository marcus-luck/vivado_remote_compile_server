"""Tests of the administration commands (buildserver.admin)."""

import io
import sqlite3

import pytest

from buildserver import admin
from buildserver import db


@pytest.fixture
def database(tmp_path):
    """Provides a database with one queued build."""
    (tmp_path / "data").mkdir()
    database = db.Database(tmp_path / "data" / "builds.db")
    database.create("b1", "fw", "pkg.zip", created_by="a@example.com")
    return database


def queue(database: db.Database, build_id: str) -> None:
    database.create(build_id, "fw", "pkg.zip", created_by="a@example.com")


def test_busy_only_when_a_build_runs(database):
    out = io.StringIO()
    assert admin.busy(database, out) == 0
    assert out.getvalue().strip() == "running 0, queued 1"
    database.claim_next()
    out = io.StringIO()
    assert admin.busy(database, out) == 1
    assert out.getvalue().strip() == "running 1, queued 0"


def test_backup_and_restore_round_trip(database, tmp_path):
    copy = io.BytesIO()
    admin.backup(database, copy)
    queue(database, "b2")  # after the backup: gone once it is restored

    copy.seek(0)
    kept = admin.restore(database.path, copy)

    ids = [build["id"] for build in database.list_builds(10)]
    assert ids == ["b1"]
    # The old database, write-ahead log included, is kept aside.
    assert kept.name.startswith("builds.db.before-restore-")
    old = sorted(b["id"] for b in db.Database(kept).list_builds(10))
    assert old == ["b1", "b2"]
    leftovers = {p.name for p in database.path.parent.iterdir()}
    assert leftovers <= {
        database.path.name,
        kept.name,
        f"{kept.name}-wal",
        f"{kept.name}-shm",
    }


def test_restore_without_a_database_keeps_nothing(tmp_path, database):
    copy = io.BytesIO()
    admin.backup(database, copy)
    copy.seek(0)
    assert admin.restore(tmp_path / "new" / "builds.db", copy) is None


def test_restore_refuses_a_file_that_is_not_a_database(database):
    with pytest.raises(ValueError, match="not an SQLite database"):
        admin.restore(database.path, io.BytesIO(b"not a database" * 100))
    assert [b["id"] for b in database.list_builds(10)] == ["b1"]


def test_restore_refuses_a_database_without_builds(database, tmp_path):
    other = tmp_path / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE something (x)")
    conn.commit()
    conn.close()
    with other.open("rb") as f, pytest.raises(ValueError, match="no builds"):
        admin.restore(database.path, f)
    assert [b["id"] for b in database.list_builds(10)] == ["b1"]
