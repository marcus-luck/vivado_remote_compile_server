"""Build worker: takes queued builds one at a time and runs them.

Run with:

    python -m buildserver.worker
"""

import datetime
import logging
import os
import pathlib
import shutil
import signal
import subprocess
import time
from typing import BinaryIO

from buildserver import config
from buildserver import db
from buildserver import git
from buildserver import storage

_logger = logging.getLogger("buildserver.worker")


def kill_tree(process: subprocess.Popen) -> None:
    """Kills a build process together with its children.

    Killing only the process itself is not enough: vivado.bat spawns the real
    Vivado as a child.

    Args:
        process: The build process, started by run_command().
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def _run_process(
    command: list[str],
    cwd: pathlib.Path,
    env: dict[str, str] | None,
    timeout: float,
    database: db.Database,
    build: db.Build,
    logfile: BinaryIO,
) -> tuple[str, int | None, str | None]:
    """Runs a command of a build and waits for it to end.

    The command is killed if the build is cancelled or the command exceeds
    the timeout.

    Args:
        command: The command, as a list of arguments. Written to the log, so
            it must not hold secrets.
        cwd: Directory to run the command in.
        env: Environment of the command, or None for the worker's own.
        timeout: Seconds the command may run.
        database: The build records, polled for a cancel request.
        build: The build being run.
        logfile: Open log file that receives the output of the command.

    Returns:
        A tuple (status, exit_code, error): SUCCEEDED, FAILED or CANCELLED,
        the exit code of the command (None if it was killed or never
        started), and a reason for a failure (None otherwise).
    """
    logfile.write(f"[buildserver] running: {' '.join(command)}\n".encode())
    logfile.flush()
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=logfile,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
    except OSError as e:
        return db.FAILED, None, f"could not start {command[0]}: {e}"

    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                exit_code = process.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                pass
            if database.cancel_requested(build["id"]):
                kill_tree(process)
                return db.CANCELLED, None, None
            if time.monotonic() > deadline:
                kill_tree(process)
                return db.FAILED, None, f"timed out after {timeout:.0f} s"
    finally:
        if process.poll() is None:
            kill_tree(process)

    if exit_code != 0:
        return db.FAILED, exit_code, f"exited with code {exit_code}"
    return db.SUCCEEDED, exit_code, None


def run_command(
    settings: config.Settings,
    database: db.Database,
    build: db.Build,
    cwd: pathlib.Path,
    logfile: BinaryIO,
) -> tuple[str, int | None, str | None]:
    """Runs the build command and waits for it to end.

    The command is killed if the build is cancelled or exceeds the build
    timeout.

    Args:
        settings: The server settings.
        database: The build records, polled for a cancel request.
        build: The build being run.
        cwd: Directory to run the command in.
        logfile: Open log file that receives the output of the command.

    Returns:
        A tuple (status, exit_code, error): the final build status, the exit
        code of the command (None if it was killed or never started), and a
        reason for a failure (None otherwise).
    """
    command = [
        argument.replace(config.NAME_PLACEHOLDER, build["name"])
        for argument in settings.build_command
    ]
    status, exit_code, error = _run_process(
        command, cwd, None, settings.build_timeout, database, build, logfile
    )
    return status, exit_code, error and f"build command {error}"


def clone_repo(
    settings: config.Settings,
    database: db.Database,
    build: db.Build,
    dest: pathlib.Path,
    logfile: BinaryIO,
) -> tuple[str, str | None]:
    """Clones the branch or tag of a build from a repository.

    The clone uses the GitHub token of the user recorded as the build's
    source, and counts against the build timeout like the build itself.

    Args:
        settings: The server settings.
        database: The records, for the token and for a cancel request.
        build: The build being run; its repo_full_name is set.
        dest: Directory to clone into; must not exist.
        logfile: Open log file that receives git's output.

    Returns:
        A tuple (status, error): SUCCEEDED, FAILED or CANCELLED, and a
        reason for a failure (None otherwise).
    """
    link = database.get_github_link(build["source_user_id"])
    if link is None:
        owner = database.get_user(build["source_user_id"])
        who = owner["email"] if owner else "the repository's owner"
        return db.FAILED, f"GitHub account of {who} is not linked"
    command = git.clone_command(
        settings.git, build["repo_full_name"], build["ref"], dest
    )
    status, _, error = _run_process(
        command,
        dest.parent,
        git.clone_env(link["access_token"]),
        settings.build_timeout,
        database,
        build,
        logfile,
    )
    if status != db.SUCCEEDED:
        return status, error and f"git clone {error}"
    database.set_commit(build["id"], git.head_commit(settings.git, dest))
    return db.SUCCEEDED, None


def prepare_source(
    settings: config.Settings,
    database: db.Database,
    store: storage.Storage,
    build: db.Build,
    logfile: BinaryIO,
) -> tuple[pathlib.Path | None, str, str | None]:
    """Puts the source of a build in its work directory.

    The source is the uploaded package, extracted, or a clone of the
    repository.

    Args:
        settings: The server settings.
        database: The records.
        store: The data directory.
        build: The build being run.
        logfile: Open log file of the build.

    Returns:
        A tuple (root, status, error): the directory holding the build
        script, or None if the build cannot go on; and, when it cannot, the
        status (FAILED or CANCELLED) and the reason.
    """
    work_dir = store.work_dir(build["id"])
    shutil.rmtree(work_dir, ignore_errors=True)
    if build["repo_full_name"]:
        status, error = clone_repo(settings, database, build, work_dir, logfile)
        if status != db.SUCCEEDED:
            return None, status, error
        source = "repository"
    else:
        storage.extract_package(store.package_path(build["id"]), work_dir)
        source = "package"
    root = storage.find_build_root(work_dir, settings.build_script)
    if root is None:
        return (
            None,
            db.FAILED,
            f"{settings.build_script} not found at the top level of the"
            f" {source}",
        )
    return root, db.SUCCEEDED, None


def run_build(
    settings: config.Settings,
    database: db.Database,
    store: storage.Storage,
    build: db.Build,
) -> None:
    """Runs one claimed build from package to artifacts and records the result.

    Extracts the package, runs the build command, zips the artifact directory
    together with the build log, and removes the work directory. Any failure
    along the way is recorded on the build instead of being raised.

    Args:
        settings: The server settings.
        database: The build records.
        store: The data directory.
        build: The build to run, already claimed (status RUNNING).
    """
    build_id = build["id"]
    work_dir = store.work_dir(build_id)
    log_path = store.log_path(build_id)
    status, exit_code, error = db.FAILED, None, None
    try:
        root = None
        with open(log_path, "wb") as logfile:
            # One failing build must not take the worker down.
            try:
                root, status, error = prepare_source(
                    settings, database, store, build, logfile
                )
                if root is not None:
                    status, exit_code, error = run_command(
                        settings, database, build, root, logfile
                    )
            except Exception as e:  # pylint: disable=broad-exception-caught
                _logger.exception("build %s failed", build_id)
                status, error = db.FAILED, f"{type(e).__name__}: {e}"
            if error:
                logfile.write(f"[buildserver] {error}\n".encode())

        # Artifacts are collected whatever the outcome: the reports matter
        # most when a build fails.
        if root is not None:
            artifact_dir = root / settings.artifact_dir.replace(
                config.NAME_PLACEHOLDER, build["name"]
            )
            has_artifacts = artifact_dir.is_dir() and any(
                path.is_file() for path in artifact_dir.rglob("*")
            )
            if has_artifacts:
                storage.zip_dir(
                    artifact_dir,
                    store.artifacts_path(build_id),
                    extra={"build.log": log_path},
                )
            elif status == db.SUCCEEDED:
                status = db.FAILED
                error = (
                    "build produced no artifacts in"
                    f" {artifact_dir.relative_to(root).as_posix()}"
                )
    except Exception as e:  # pylint: disable=broad-exception-caught
        _logger.exception("build %s failed", build_id)
        status, error = db.FAILED, f"{type(e).__name__}: {e}"
    finally:
        if not settings.keep_workdir:
            shutil.rmtree(work_dir, ignore_errors=True)
    database.finish(build_id, status, exit_code, error)
    _logger.info(
        "build %s (%s) %s%s",
        build_id,
        build["name"],
        status,
        f": {error}" if error else "",
    )


def run_once(
    settings: config.Settings, database: db.Database, store: storage.Storage
) -> bool:
    """Runs the next queued build, if there is one.

    Args:
        settings: The server settings.
        database: The build records.
        store: The data directory.

    Returns:
        True if a build was run, False if the queue was empty.
    """
    build = database.claim_next()
    if build is None:
        return False
    _logger.info("build %s (%s) started", build["id"], build["name"])
    run_build(settings, database, store, build)
    return True


def expire_old_files(
    settings: config.Settings,
    database: db.Database,
    store: storage.Storage,
    now: datetime.datetime | None = None,
) -> int:
    """Deletes the package and artifacts of builds past the retention period.

    The build records and logs stay. A build whose files cannot be deleted
    is left as it is and tried again on the next pass.

    Args:
        settings: The server settings.
        database: The build records.
        store: The data directory.
        now: The current time, in UTC. Defaults to the clock.

    Returns:
        The number of builds whose files were deleted.
    """
    if settings.retention_days <= 0:
        return 0
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=settings.retention_days)
    expired = 0
    # Same format as db.now(), so the timestamps compare as strings.
    cutoff_text = cutoff.isoformat(timespec="milliseconds")
    for build in database.list_expirable(cutoff_text):
        try:
            store.expire_files(build["id"])
        except OSError as e:
            _logger.warning(
                "could not delete files of build %s: %s", build["id"], e
            )
            continue
        database.set_files_expired(build["id"])
        _logger.info(
            "deleted package and artifacts of build %s (%s)",
            build["id"],
            build["name"],
        )
        expired += 1
    return expired


def main() -> None:
    """Runs the worker until it is interrupted."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    settings = config.load_settings()
    store = storage.Storage(settings.data_dir)
    database = db.Database(store.db_path)
    interrupted = database.fail_interrupted()
    if interrupted:
        _logger.warning("marked %d interrupted build(s) as failed", interrupted)
    _logger.info(
        "worker ready, data dir %s, command %s, %s",
        settings.data_dir,
        settings.build_command,
        f"build files kept {settings.retention_days:g} days"
        if settings.retention_days > 0
        else "build files kept forever",
    )
    next_expiry = time.monotonic()
    try:
        while True:
            if time.monotonic() >= next_expiry:
                expire_old_files(settings, database, store)
                next_expiry = time.monotonic() + settings.retention_interval
            if not run_once(settings, database, store):
                time.sleep(settings.poll_interval)
    except KeyboardInterrupt:
        _logger.info("worker stopped")


if __name__ == "__main__":
    main()
