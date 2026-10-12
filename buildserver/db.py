"""SQLite-backed build records. The table doubles as the task queue: the worker
claims the oldest row with status 'queued'."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"

SCHEMA = """
CREATE TABLE IF NOT EXISTS builds (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    package_filename TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    exit_code INTEGER,
    error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS builds_status ON builds (status, created_at);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self, build_id: str, name: str, package_filename: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO builds (id, name, status, package_filename, created_at)"
                " VALUES (?, ?, ?, ?, ?) RETURNING *",
                (build_id, name, QUEUED, package_filename, now()),
            ).fetchone()
        return dict(row)

    def get(self, build_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
        return dict(row) if row else None

    def list(self, limit: int = 200) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM builds ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def claim_next(self) -> dict | None:
        """Atomically move the oldest queued build to 'running' and return it."""
        with self._connect() as conn:
            row = conn.execute(
                "UPDATE builds SET status = ?, started_at = ? WHERE id = ("
                " SELECT id FROM builds WHERE status = ? ORDER BY created_at, rowid LIMIT 1"
                ") RETURNING *",
                (RUNNING, now(), QUEUED),
            ).fetchone()
        return dict(row) if row else None

    def finish(self, build_id: str, status: str, exit_code: int | None, error: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE builds SET status = ?, finished_at = ?, exit_code = ?, error = ? WHERE id = ?",
                (status, now(), exit_code, error, build_id),
            )

    def request_cancel(self, build_id: str) -> None:
        """Cancel a queued build outright; flag a running one for the worker to kill."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE builds SET status = ?, finished_at = ? WHERE id = ? AND status = ?",
                (CANCELLED, now(), build_id, QUEUED),
            )
            if cur.rowcount == 0:
                conn.execute(
                    "UPDATE builds SET cancel_requested = 1 WHERE id = ? AND status = ?",
                    (build_id, RUNNING),
                )

    def cancel_requested(self, build_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM builds WHERE id = ?", (build_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def fail_interrupted(self) -> int:
        """Mark builds left 'running' by a dead worker as failed."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE builds SET status = ?, finished_at = ?, error = ? WHERE status = ?",
                (FAILED, now(), "worker stopped while the build was running", RUNNING),
            )
        return cur.rowcount
