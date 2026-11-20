"""SQLite-backed build and user records.

The builds table doubles as the task queue: the worker claims the oldest row
whose status is QUEUED. The users table holds the accounts that may use the
server: people, who sign in with Google, and service accounts, which only use
API tokens. The email column is the account identifier: a Google email for a
person, a name without "@" for a service account. The api_tokens table holds
hashes of the API tokens. The github_links table holds the GitHub account a
user linked, with its OAuth token, and the repos table the GitHub repositories
users connected to build from.
"""

from collections.abc import Iterator
import contextlib
import datetime
import json
import pathlib
import sqlite3
from typing import Any

from buildserver import crypto

# A row of the builds table, keyed by column name.
Build = dict[str, Any]
# A row of the users table, keyed by column name.
User = dict[str, Any]
# A row of the api_tokens table, keyed by column name.
Token = dict[str, Any]
# A row of the github_links table, keyed by column name.
GitHubLink = dict[str, Any]
# A row of the repos table, keyed by column name, with branches and tags
# decoded into lists.
Repo = dict[str, Any]

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"

REF_BRANCH = "branch"
REF_TAG = "tag"

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
    created_by TEXT,
    repo_full_name TEXT,
    ref TEXT,
    ref_type TEXT,
    commit_sha TEXT,
    source_user_id INTEGER,
    files_expired_at TEXT
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
    last_login_at TEXT,
    is_service INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    prefix TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS api_tokens_user ON api_tokens (user_id);
CREATE TABLE IF NOT EXISTS github_links (
    user_id INTEGER PRIMARY KEY,
    github_id INTEGER NOT NULL UNIQUE,
    login TEXT NOT NULL,
    access_token TEXT NOT NULL,
    scope TEXT,
    linked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    full_name TEXT NOT NULL COLLATE NOCASE,
    name TEXT NOT NULL,
    ref TEXT NOT NULL,
    ref_type TEXT NOT NULL,
    default_branch TEXT NOT NULL,
    branches TEXT NOT NULL,
    tags TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, full_name)
);
"""

# Columns added to the builds table after it was first created.
_BUILD_COLUMNS = {
    "created_by": "TEXT",
    "repo_full_name": "TEXT",
    "ref": "TEXT",
    "ref_type": "TEXT",
    "commit_sha": "TEXT",
    "source_user_id": "INTEGER",
    "files_expired_at": "TEXT",
}


def now() -> str:
    """Returns the current UTC time as an ISO 8601 string."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds"
    )


class Database:
    """Access to the build and user records.

    Every method opens its own connection, so one instance can be shared
    between threads, and the API and worker processes can each have their own.

    GitHub tokens are stored encrypted with the token cipher; without one,
    no GitHub account can be linked.

    Attributes:
        path: Location of the SQLite database file.
    """

    def __init__(
        self,
        path: pathlib.Path,
        token_cipher: crypto.TokenCipher | None = None,
    ) -> None:
        """Opens the database, creating the schema if it is missing.

        With a token cipher, GitHub tokens stored in plaintext by an earlier
        version are encrypted.

        Args:
            path: Location of the SQLite database file.
            token_cipher: Encrypts the GitHub tokens, or None if no
                BUILDSERVER_TOKEN_KEY is configured.
        """
        self.path = path
        self._cipher = token_cipher
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            # Databases created before builds recorded who queued them, or
            # before builds from repositories.
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(builds)")
            }
            for column, column_type in _BUILD_COLUMNS.items():
                if column not in columns:
                    conn.execute(
                        f"ALTER TABLE builds ADD COLUMN {column} {column_type}"
                    )
            # Databases created before service accounts.
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(users)")
            }
            if "is_service" not in columns:
                conn.execute(
                    "ALTER TABLE users"
                    " ADD COLUMN is_service INTEGER NOT NULL DEFAULT 0"
                )
            # GitHub tokens stored before they were encrypted.
            if self._cipher is not None:
                rows = conn.execute(
                    "SELECT user_id, access_token FROM github_links"
                ).fetchall()
                for row in rows:
                    if not crypto.is_encrypted(row["access_token"]):
                        conn.execute(
                            "UPDATE github_links SET access_token = ?"
                            " WHERE user_id = ?",
                            (
                                self._cipher.encrypt(row["access_token"]),
                                row["user_id"],
                            ),
                        )

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
        repo_full_name: str | None = None,
        ref: str | None = None,
        ref_type: str | None = None,
        source_user_id: int | None = None,
    ) -> Build:
        """Adds a build to the queue.

        A build either has an uploaded package or comes from a GitHub
        repository; for the latter package_filename is empty and the repo
        arguments are set.

        Args:
            build_id: Unique id of the new build.
            name: Build name, passed to the build command.
            package_filename: File name of the uploaded package, as given by
                the client, or empty for a build from a repository.
            created_by: Email of the user who queued the build.
            repo_full_name: The repository to clone, as "owner/repo".
            ref: Branch or tag of the repository to build.
            ref_type: REF_BRANCH or REF_TAG.
            source_user_id: Id of the user whose GitHub link the clone uses.

        Returns:
            The new build record.
        """
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO builds"
                " (id, name, status, package_filename, created_at, created_by,"
                " repo_full_name, ref, ref_type, source_user_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
                (
                    build_id,
                    name,
                    QUEUED,
                    package_filename,
                    now(),
                    created_by,
                    repo_full_name,
                    ref,
                    ref_type,
                    source_user_id,
                ),
            ).fetchone()
        return dict(row)

    def set_commit(self, build_id: str, commit_sha: str) -> None:
        """Records the commit a build from a repository checked out.

        Args:
            build_id: Id of the build.
            commit_sha: Full hash of the commit.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE builds SET commit_sha = ? WHERE id = ?",
                (commit_sha, build_id),
            )

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

    def list_builds(
        self, limit: int = 200, created_by: str | None = None
    ) -> list[Build]:
        """Lists the most recent builds.

        Args:
            limit: Maximum number of builds to return.
            created_by: Only list the builds this user queued (their email or
                service account name). None lists everybody's.

        Returns:
            The build records, newest first.
        """
        where, params = "", ()
        if created_by is not None:
            where, params = " WHERE created_by = ?", (created_by,)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM builds{where}"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (*params, limit),
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

    def delete_build(self, build_id: str) -> bool:
        """Removes a build record, unless the build is running.

        The status check happens in the same statement as the delete, so a
        queued build cannot be claimed by the worker in between.

        Args:
            build_id: Id of the build.

        Returns:
            True if the record was removed, False if there is no such build
            or it is running.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM builds WHERE id = ? AND status != ?",
                (build_id, RUNNING),
            )
        return cursor.rowcount > 0

    def list_expirable(self, cutoff: str) -> list[Build]:
        """Lists finished builds whose files are due to be deleted.

        Args:
            cutoff: ISO 8601 UTC time; builds that finished before it are due.

        Returns:
            The build records whose files have not been expired yet, oldest
            first.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM builds WHERE finished_at < ?"
                " AND files_expired_at IS NULL ORDER BY finished_at",
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_files_expired(self, build_id: str) -> None:
        """Records that the package and artifacts of a build were deleted.

        Args:
            build_id: Id of the build.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE builds SET files_expired_at = ? WHERE id = ?",
                (now(), build_id),
            )

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

    def ping(self) -> None:
        """Checks that the database can be read.

        Raises:
            sqlite3.Error: If it cannot.
        """
        with self._connect() as conn:
            conn.execute("SELECT 1 FROM builds LIMIT 1").fetchall()

    def count_by_status(self) -> dict[str, int]:
        """Counts the builds in each status.

        Returns:
            The number of builds per status; statuses without builds are
            missing.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM builds GROUP BY status"
            ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def backup_to(self, dest: pathlib.Path) -> None:
        """Writes a consistent copy of the database, safe while it is in use.

        The copy is a single file: it does not use a write-ahead log.

        Args:
            dest: The file to write; it is overwritten if it exists.
        """
        target = sqlite3.connect(dest)
        try:
            with self._connect() as conn:
                conn.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()

    def create_user(
        self, email: str, role: str, is_service: bool = False
    ) -> User:
        """Adds a user.

        Args:
            email: Email of the Google account, or name of the service
                account, stored in lower case.
            role: Role of the user.
            is_service: Whether the user is a service account.

        Returns:
            The new user record.

        Raises:
            sqlite3.IntegrityError: If there already is a user with that
                email.
        """
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO users (email, role, created_at, is_service)"
                " VALUES (?, ?, ?, ?) RETURNING *",
                (email.lower(), role, now(), int(is_service)),
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
        """Removes a user, their API tokens, GitHub link and repositories.

        Queued builds of the user's repositories are cancelled, whoever
        queued them. The user's builds themselves are kept.

        Args:
            user_id: Id of the user.

        Returns:
            True if the user existed.
        """
        with self._connect() as conn:
            _cancel_queued(
                conn,
                "repository owner removed",
                "source_user_id = ?",
                (user_id,),
            )
            for table in ("api_tokens", "github_links", "repos"):
                conn.execute(
                    f"DELETE FROM {table} WHERE user_id = ?", (user_id,)
                )
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

    def create_token(
        self,
        user_id: int,
        name: str,
        prefix: str,
        token_hash: str,
        expires_at: str | None,
    ) -> Token:
        """Adds an API token.

        Args:
            user_id: Id of the user the token authenticates as.
            name: What the token is for, as given by its creator.
            prefix: Start of the token, shown to tell tokens apart.
            token_hash: Hash of the token.
            expires_at: When the token stops working, as an ISO 8601 UTC
                string, or None if it does not expire.

        Returns:
            The new token record.
        """
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO api_tokens"
                " (user_id, name, prefix, token_hash, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?) RETURNING *",
                (user_id, name, prefix, token_hash, now(), expires_at),
            ).fetchone()
        return dict(row)

    def get_token(self, token_id: int) -> Token | None:
        """Looks up an API token by id.

        Args:
            token_id: Id of the token.

        Returns:
            The token record, or None if there is no such token.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM api_tokens WHERE id = ?", (token_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_token_by_hash(self, token_hash: str) -> Token | None:
        """Looks up an API token by its hash.

        Args:
            token_hash: Hash of the token.

        Returns:
            The token record, or None if there is no such token.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM api_tokens WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return dict(row) if row else None

    def list_tokens(self, user_id: int) -> list[Token]:
        """Lists the API tokens of a user.

        Args:
            user_id: Id of the user.

        Returns:
            The token records, newest first.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM api_tokens WHERE user_id = ?"
                " ORDER BY created_at DESC, id DESC",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_token(self, token_id: int) -> bool:
        """Removes an API token, revoking it.

        Args:
            token_id: Id of the token.

        Returns:
            True if the token existed.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM api_tokens WHERE id = ?", (token_id,)
            )
        return cursor.rowcount > 0

    def touch_token(self, token_id: int) -> None:
        """Records that an API token was just used.

        Args:
            token_id: Id of the token.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
                (now(), token_id),
            )

    def set_github_link(
        self,
        user_id: int,
        github_id: int,
        login: str,
        access_token: str,
        scope: str | None,
    ) -> GitHubLink:
        """Links a GitHub account to a user, replacing any earlier link.

        Args:
            user_id: Id of the user.
            github_id: GitHub's numeric id of the account.
            login: GitHub user name of the account.
            access_token: OAuth access token of the account.
            scope: Scopes granted to the token, as GitHub reports them.

        Returns:
            The link record.

        Raises:
            sqlite3.IntegrityError: If the GitHub account is linked to another
                user.
            RuntimeError: If no token cipher is configured.
        """
        if self._cipher is None:
            raise RuntimeError(
                "BUILDSERVER_TOKEN_KEY is not set; GitHub tokens are only"
                " stored encrypted"
            )
        stored = self._cipher.encrypt(access_token)
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO github_links"
                " (user_id, github_id, login, access_token, scope, linked_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (user_id) DO UPDATE SET"
                " github_id = excluded.github_id, login = excluded.login,"
                " access_token = excluded.access_token,"
                " scope = excluded.scope, linked_at = excluded.linked_at"
                " RETURNING *",
                (user_id, github_id, login, stored, scope, now()),
            ).fetchone()
        return {**dict(row), "access_token": access_token}

    def get_github_link(self, user_id: int) -> GitHubLink | None:
        """Looks up the GitHub account linked to a user.

        Args:
            user_id: Id of the user.

        Returns:
            The link record, with the token decrypted, or None if the user
            has not linked an account.

        Raises:
            RuntimeError: If the token is encrypted and no token cipher is
                configured.
            ValueError: If the token cannot be decrypted with the key.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM github_links WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return None
        link = dict(row)
        if crypto.is_encrypted(link["access_token"]):
            if self._cipher is None:
                raise RuntimeError(
                    "BUILDSERVER_TOKEN_KEY is not set; the stored GitHub"
                    " tokens cannot be decrypted"
                )
            link["access_token"] = self._cipher.decrypt(link["access_token"])
        return link

    def delete_github_link(self, user_id: int) -> bool:
        """Removes the GitHub link of a user.

        Args:
            user_id: Id of the user.

        Returns:
            True if the user had a link.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM github_links WHERE user_id = ?", (user_id,)
            )
        return cursor.rowcount > 0

    def create_repo(
        self,
        user_id: int,
        full_name: str,
        name: str,
        default_branch: str,
        branches: list[str],
        tags: list[str],
    ) -> Repo:
        """Connects a GitHub repository, with its default branch selected.

        Args:
            user_id: Id of the user connecting the repository.
            full_name: The repository, as "owner/repo".
            name: Name of the repository on the page, and the default build
                name.
            default_branch: The repository's default branch.
            branches: Names of the repository's branches.
            tags: Names of the repository's tags.

        Returns:
            The new repository record.

        Raises:
            sqlite3.IntegrityError: If the user already connected the
                repository.
        """
        timestamp = now()
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO repos (user_id, full_name, name, ref, ref_type,"
                " default_branch, branches, tags, fetched_at, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
                (
                    user_id,
                    full_name,
                    name,
                    default_branch,
                    REF_BRANCH,
                    default_branch,
                    json.dumps(branches),
                    json.dumps(tags),
                    timestamp,
                    timestamp,
                ),
            ).fetchone()
        return _repo(row)

    def get_repo(self, repo_id: int) -> Repo | None:
        """Looks up a repository.

        Args:
            repo_id: Id of the repository.

        Returns:
            The repository record, or None if there is no such repository.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM repos WHERE id = ?", (repo_id,)
            ).fetchone()
        return _repo(row) if row else None

    def list_repos(self, user_id: int | None) -> list[Repo]:
        """Lists the repositories of a user, or of everybody.

        Args:
            user_id: Id of the user, or None for all repositories.

        Returns:
            The repository records, ordered by name.
        """
        with self._connect() as conn:
            if user_id is None:
                rows = conn.execute(
                    "SELECT * FROM repos ORDER BY name COLLATE NOCASE, id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM repos WHERE user_id = ?"
                    " ORDER BY name COLLATE NOCASE, id",
                    (user_id,),
                ).fetchall()
        return [_repo(row) for row in rows]

    def select_ref(self, repo_id: int, ref: str, ref_type: str) -> None:
        """Selects the branch or tag a repository is built from.

        Args:
            repo_id: Id of the repository.
            ref: Name of the branch or tag.
            ref_type: REF_BRANCH or REF_TAG.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE repos SET ref = ?, ref_type = ? WHERE id = ?",
                (ref, ref_type, repo_id),
            )

    def update_refs(
        self,
        repo_id: int,
        default_branch: str,
        branches: list[str],
        tags: list[str],
    ) -> None:
        """Stores freshly fetched branches and tags of a repository.

        Args:
            repo_id: Id of the repository.
            default_branch: The repository's default branch.
            branches: Names of the repository's branches.
            tags: Names of the repository's tags.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE repos SET default_branch = ?, branches = ?, tags = ?,"
                " fetched_at = ? WHERE id = ?",
                (
                    default_branch,
                    json.dumps(branches),
                    json.dumps(tags),
                    now(),
                    repo_id,
                ),
            )

    def delete_repo(self, repo_id: int) -> bool:
        """Disconnects a repository and cancels its queued builds.

        The other builds of the repository are kept. A running build goes
        on.

        Args:
            repo_id: Id of the repository.

        Returns:
            True if the repository existed.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, full_name FROM repos WHERE id = ?", (repo_id,)
            ).fetchone()
            if row is None:
                return False
            _cancel_queued(
                conn,
                "repository disconnected",
                "source_user_id = ? AND repo_full_name = ?",
                (row["user_id"], row["full_name"]),
            )
            conn.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
        return True


def _cancel_queued(
    conn: sqlite3.Connection,
    reason: str,
    where: str,
    params: tuple[Any, ...],
) -> None:
    """Cancels the queued builds that match a condition.

    Args:
        conn: Open connection, inside the caller's transaction.
        reason: Recorded as the error of each cancelled build.
        where: SQL condition on the builds table. Written by this module
            only, never from user input.
        params: Values of the condition's placeholders.
    """
    conn.execute(
        "UPDATE builds SET status = ?, finished_at = ?, error = ?"
        f" WHERE status = ? AND {where}",
        (CANCELLED, now(), reason, QUEUED, *params),
    )


def _repo(row: sqlite3.Row) -> Repo:
    """Converts a row of the repos table into a record."""
    repo = dict(row)
    repo["branches"] = json.loads(repo["branches"])
    repo["tags"] = json.loads(repo["tags"])
    return repo
