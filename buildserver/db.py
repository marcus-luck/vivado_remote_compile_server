"""SQLite-backed build records.

The builds table doubles as the task queue: the worker claims the oldest row
whose status is QUEUED.
"""

from collections.abc import Iterator
import contextlib
import datetime
import pathlib
import sqlite3
from typing import Any

# A row of the builds table, keyed by column name.
Build = dict[str, Any]

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"

_SCHEMA = """
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
    """Returns the current UTC time as an ISO 8601 string."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds"
    )


class Database:
    """Access to the build records.

    Every method opens its own connection, so one instance can be shared
    between threads, and the API and worker processes can each have their own.

    Attributes:
        path: Location of the SQLite database file.
    """

    def __init__(self, path: pathlib.Path) -> None:
        """Opens the database, creating the schema if it is missing.

        Args:
            path: Location of the SQLite database file.
        """
        self.path = path
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Opens a connection for one transaction.

        The transaction is committed when the block exits normally and rolled
        back if it raises. The connection is closed either way.

        Yields:
            The open connection.
        """
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self, build_id: str, name: str, package_filename: str) -> Build:
        """Adds a build to the queue.

        Args:
            build_id: Unique id of the new build.
            name: Build name, passed to the build command.
            package_filename: File name of the uploaded package, as given by
                the client.

        Returns:
            The new build record.
        """
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO builds"
                " (id, name, status, package_filename, created_at)"
                " VALUES (?, ?, ?, ?, ?) RETURNING *",
                (build_id, name, QUEUED, package_filename, now()),
            ).fetchone()
        return dict(row)

    def get(self, build_id: str) -> Build | None:
        """Looks up a build.

        Args:
            build_id: Id of the build.

        Returns:
            The build record, or None if there is no such build.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM builds WHERE id = ?", (build_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_builds(self, limit: int = 200) -> list[Build]:
        """Lists the most recent builds.

        Args:
            limit: Maximum number of builds to return.

        Returns:
            The build records, newest first.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM builds"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_next(self) -> Build | None:
        """Atomically moves the oldest queued build to RUNNING.

        Returns:
            The claimed build record, or None if the queue is empty.
        """
        with self._connect() as conn:
            row = conn.execute(
                "UPDATE builds SET status = ?, started_at = ? WHERE id = ("
                " SELECT id FROM builds WHERE status = ?"
                " ORDER BY created_at, rowid LIMIT 1"
                ") RETURNING *",
                (RUNNING, now(), QUEUED),
            ).fetchone()
        return dict(row) if row else None

    def finish(
        self,
        build_id: str,
        status: str,
        exit_code: int | None,
        error: str | None,
    ) -> None:
        """Records the outcome of a build.

        Args:
            build_id: Id of the build.
            status: Final status: SUCCEEDED, FAILED or CANCELLED.
            exit_code: Exit code of the build command, or None if it did not
                run to completion.
            error: Human-readable reason for a failure, or None.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE builds SET status = ?, finished_at = ?, exit_code = ?,"
                " error = ? WHERE id = ?",
                (status, now(), exit_code, error, build_id),
            )

    def request_cancel(self, build_id: str) -> None:
        """Cancels a build.

        A queued build is cancelled outright. A running build is flagged for
        the worker to kill. A build that is already over is left untouched.

        Args:
            build_id: Id of the build.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE builds SET status = ?, finished_at = ?"
                " WHERE id = ? AND status = ?",
                (CANCELLED, now(), build_id, QUEUED),
            )
            if cursor.rowcount == 0:
                conn.execute(
                    "UPDATE builds SET cancel_requested = 1"
                    " WHERE id = ? AND status = ?",
                    (build_id, RUNNING),
                )

    def cancel_requested(self, build_id: str) -> bool:
        """Tells whether a build has been flagged for cancellation.

        Args:
            build_id: Id of the build.

        Returns:
            True if cancellation of the build was requested while it ran.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM builds WHERE id = ?", (build_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def fail_interrupted(self) -> int:
        """Marks builds left RUNNING by a dead worker as FAILED.

        Returns:
            The number of builds that were marked.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE builds SET status = ?, finished_at = ?, error = ?"
                " WHERE status = ?",
                (
                    FAILED,
                    now(),
                    "worker stopped while the build was running",
                    RUNNING,
                ),
            )
        return cursor.rowcount
