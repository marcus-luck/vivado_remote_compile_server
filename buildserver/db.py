"""SQLite-backed build and user records.

The builds table doubles as the task queue: the worker claims the oldest row
whose status is QUEUED. The users table holds the accounts that may sign in.
"""

from collections.abc import Iterator
import contextlib
import datetime
import pathlib
import sqlite3
from typing import Any

# A row of the builds table, keyed by column name.
Build = dict[str, Any]
# A row of the users table, keyed by column name.
User = dict[str, Any]

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
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_by TEXT
);
CREATE INDEX IF NOT EXISTS builds_status ON builds (status, created_at);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT,
    picture TEXT,
    google_sub TEXT UNIQUE,
    role TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);
"""


def now() -> str:
    """Returns the current UTC time as an ISO 8601 string."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds"
    )


class Database:
    """Access to the build and user records.

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
            # Databases created before builds recorded who queued them.
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(builds)")
            }
            if "created_by" not in columns:
                conn.execute("ALTER TABLE builds ADD COLUMN created_by TEXT")

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

    def create(
        self,
        build_id: str,
        name: str,
        package_filename: str,
        created_by: str | None = None,
    ) -> Build:
        """Adds a build to the queue.

        Args:
            build_id: Unique id of the new build.
            name: Build name, passed to the build command.
            package_filename: File name of the uploaded package, as given by
                the client.
            created_by: Email of the user who queued the build.

        Returns:
            The new build record.
        """
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO builds"
                " (id, name, status, package_filename, created_at, created_by)"
                " VALUES (?, ?, ?, ?, ?, ?) RETURNING *",
                (build_id, name, QUEUED, package_filename, now(), created_by),
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

    def create_user(self, email: str, role: str) -> User:
        """Adds a user.

        Args:
            email: Email of the Google account, stored in lower case.
            role: Role of the user.

        Returns:
            The new user record.

        Raises:
            sqlite3.IntegrityError: If there already is a user with that
                email.
        """
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO users (email, role, created_at)"
                " VALUES (?, ?, ?) RETURNING *",
                (email.lower(), role, now()),
            ).fetchone()
        return dict(row)

    def get_user(self, user_id: int) -> User | None:
        """Looks up a user by id.

        Args:
            user_id: Id of the user.

        Returns:
            The user record, or None if there is no such user.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_user_by_email(self, email: str) -> User | None:
        """Looks up a user by email, ignoring case.

        Args:
            email: Email of the user.

        Returns:
            The user record, or None if there is no such user.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ?", (email,)
            ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[User]:
        """Lists all users.

        Returns:
            The user records, ordered by email.
        """
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY email").fetchall()
        return [dict(row) for row in rows]

    def update_user(
        self,
        user_id: int,
        role: str | None = None,
        is_active: bool | None = None,
    ) -> User | None:
        """Changes the role or the active flag of a user.

        Args:
            user_id: Id of the user.
            role: New role, or None to leave it unchanged.
            is_active: New active flag, or None to leave it unchanged.

        Returns:
            The updated user record, or None if there is no such user.
        """
        with self._connect() as conn:
            if role is not None:
                conn.execute(
                    "UPDATE users SET role = ? WHERE id = ?", (role, user_id)
                )
            if is_active is not None:
                conn.execute(
                    "UPDATE users SET is_active = ? WHERE id = ?",
                    (int(is_active), user_id),
                )
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def delete_user(self, user_id: int) -> bool:
        """Removes a user.

        Args:
            user_id: Id of the user.

        Returns:
            True if the user existed.
        """
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return cursor.rowcount > 0

    def record_login(
        self,
        user_id: int,
        google_sub: str | None,
        name: str | None,
        picture: str | None,
    ) -> User:
        """Stores the Google profile of a user who just signed in.

        Args:
            user_id: Id of the user.
            google_sub: Google's stable identifier of the account.
            name: Display name of the account.
            picture: URL of the profile picture of the account.

        Returns:
            The updated user record.
        """
        with self._connect() as conn:
            row = conn.execute(
                "UPDATE users SET google_sub = ?, name = ?, picture = ?,"
                " last_login_at = ? WHERE id = ? RETURNING *",
                (google_sub, name, picture, now(), user_id),
            ).fetchone()
        return dict(row)
