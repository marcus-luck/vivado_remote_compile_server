"""Administration commands, run by deploy/buildserverctl inside a container.

Run with:

    python -m buildserver.admin busy
    python -m buildserver.admin backup <file>|-
    python -m buildserver.admin restore <file>|-

"-" is standard output for backup and standard input for restore, so the
database can be streamed in and out of a container. Restore replaces the
database: stop the api and the worker first.
"""

import argparse
import datetime
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
from typing import BinaryIO
from typing import TextIO

from buildserver import config
from buildserver import db
from buildserver import storage


def busy(database: db.Database, out: TextIO | None = None) -> int:
    """Reports the running and queued builds.

    Args:
        database: The build server database.
        out: Where the counts are written; standard output if omitted.

    Returns:
        1 if a build is running, else 0.
    """
    out = out or sys.stdout
    counts = database.count_by_status()
    running = counts.get(db.RUNNING, 0)
    queued = counts.get(db.QUEUED, 0)
    print(f"running {running}, queued {queued}", file=out)
    return 1 if running else 0


def backup(database: db.Database, dest: BinaryIO) -> None:
    """Writes a consistent copy of the database.

    Args:
        database: The build server database; it may be in use.
        dest: Where the copy is written.
    """
    with tempfile.TemporaryDirectory(dir=database.path.parent) as tmp:
        copy = pathlib.Path(tmp) / "backup.db"
        database.backup_to(copy)
        with copy.open("rb") as f:
            shutil.copyfileobj(f, dest)
    dest.flush()


def check_backup(path: pathlib.Path) -> None:
    """Checks that a file is a build server database.

    Args:
        path: The file to check.

    Raises:
        ValueError: If it is not an SQLite database with a builds table, or
            it is damaged.
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            (result,) = conn.execute("PRAGMA integrity_check").fetchone()
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise ValueError(f"not an SQLite database: {e}") from e
    if result != "ok":
        raise ValueError(f"the database is damaged: {result}")
    if "builds" not in tables:
        raise ValueError("not a build server database: no builds table")


def restore(db_path: pathlib.Path, source: BinaryIO) -> pathlib.Path | None:
    """Replaces the database with a backup.

    The backup is checked before anything is replaced. The old database is
    kept next to it as builds.db.before-restore-<time>, with its write-ahead
    log, so a wrong restore can be undone. Only run this while the api and
    the worker are stopped.

    Args:
        db_path: Location of the database to replace.
        source: The backup.

    Returns:
        Where the old database was kept, or None if there was none.

    Raises:
        ValueError: If the backup is not a build server database.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=db_path.parent, suffix=".restore")
    tmp = pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            shutil.copyfileobj(source, f)
        check_backup(tmp)
        kept = _keep_old_database(db_path)
        os.replace(tmp, db_path)
        return kept
    finally:
        for leftover in ("", "-wal", "-shm", "-journal"):
            pathlib.Path(f"{tmp}{leftover}").unlink(missing_ok=True)


def _keep_old_database(db_path: pathlib.Path) -> pathlib.Path | None:
    """Moves a database and its write-ahead log aside.

    The write-ahead log moves with it, so it is applied when the kept copy
    is opened, and not to the database that takes its place.

    Args:
        db_path: The database.

    Returns:
        The new location, or None if there was no database.
    """
    pathlib.Path(f"{db_path}-shm").unlink(missing_ok=True)
    if not db_path.exists():
        pathlib.Path(f"{db_path}-wal").unlink(missing_ok=True)
        return None
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    kept = db_path.with_name(f"{db_path.name}.before-restore-{stamp}")
    wal = pathlib.Path(f"{db_path}-wal")
    if wal.exists():
        os.replace(wal, f"{kept}-wal")
    os.replace(db_path, kept)
    return kept


def main(argv: list[str] | None = None) -> int:
    """Runs one administration command.

    Args:
        argv: The command line arguments, without the program name.

    Returns:
        The exit status.
    """
    parser = argparse.ArgumentParser(prog="python -m buildserver.admin")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("busy", help="count running and queued builds")
    backup_parser = commands.add_parser("backup", help="copy the database")
    backup_parser.add_argument("dest", help="file to write, or - for stdout")
    restore_parser = commands.add_parser(
        "restore", help="replace the database (api and worker stopped)"
    )
    restore_parser.add_argument("source", help="backup file, or - for stdin")
    args = parser.parse_args(argv)

    settings = config.load_settings()
    db_path = storage.Storage(settings.data_dir).db_path
    if args.command == "restore":
        try:
            if args.source == "-":
                kept = restore(db_path, sys.stdin.buffer)
            else:
                with open(args.source, "rb") as f:
                    kept = restore(db_path, f)
        except ValueError as e:
            print(f"restore refused: {e}", file=sys.stderr)
            return 2
        if kept:
            print(f"kept the old database as {kept}", file=sys.stderr)
        print(f"restored {db_path}", file=sys.stderr)
        return 0
    # Opening the database migrates it, which a restore must not do to the
    # file it replaces.
    database = db.Database(db_path)
    if args.command == "busy":
        return busy(database)
    if args.dest == "-":
        backup(database, sys.stdout.buffer)
    else:
        with open(args.dest, "wb") as f:
            backup(database, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
