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
from buildserver import crypto
from buildserver import db
from buildserver import git
from buildserver import sandbox
from buildserver import storage

_logger = logging.getLogger("buildserver.worker")

# A build directory without a record is only deleted once it is this old: the
# API creates the directory just before it inserts the record.
_ORPHAN_GRACE_SECONDS = 3600


class BuildStoppedError(Exception):
    """Raised when a build cannot go on: it failed or was cancelled.

    Attributes:
        status: The final build status, FAILED or CANCELLED.
        reason: Why the build stopped, or None for a plain cancel.
        exit_code: Exit code of the build command, if it ran to the end.
    """

    def __init__(
        self, status: str, reason: str | None, exit_code: int | None = None
    ) -> None:
        """Creates the exception.

        Args:
            status: The final build status, FAILED or CANCELLED.
            reason: Why the build stopped, or None for a plain cancel.
            exit_code: Exit code of the build command, if it ran to the end.
        """
        super().__init__(reason or status)
        self.status = status
        self.reason = reason
        self.exit_code = exit_code


def _stop_unless_succeeded(
    result: tuple[str, int | None, str | None], what: str
) -> None:
    """Raises BuildStoppedError unless a process result is a success.

    Args:
        result: What _run_process() returned.
        what: Name of the process, prefixed to the reason.

    Raises:
        BuildStoppedError: If the process failed or the build was cancelled.
    """
    status, exit_code, error = result
    if status != db.SUCCEEDED:
        raise BuildStoppedError(status, error and f"{what} {error}", exit_code)


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
    run_as: str = "",
    home: pathlib.Path | None = None,
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
        run_as: Account to run the command as (see buildserver.sandbox), or
            empty for the worker's own.
        home: Home directory of the command when run_as is set.

    Returns:
        A tuple (status, exit_code, error): SUCCEEDED, FAILED or CANCELLED,
        the exit code of the command (None if it was killed or never
        started), and a reason for a failure (None otherwise).
    """
    logfile.write(f"[buildserver] running: {' '.join(command)}\n".encode())
    logfile.flush()
    argv = command
    if run_as:
        argv = sandbox.wrap(command, run_as, home, env or {})
        env = None  # sudo starts the command with a clean environment.

    def kill(process: subprocess.Popen) -> None:
        """Kills the command with everything it started."""
        if run_as:
            sandbox.kill_all(run_as)
            process.wait()
        else:
            kill_tree(process)

    try:
        process = subprocess.Popen(
            argv,
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
                kill(process)
                return db.CANCELLED, None, None
            if time.monotonic() > deadline:
                kill(process)
                return db.FAILED, None, f"timed out after {timeout:.0f} s"
    finally:
        if process.poll() is None:
            kill(process)

    if exit_code != 0:
        return db.FAILED, exit_code, f"exited with code {exit_code}"
    return db.SUCCEEDED, exit_code, None


def build_env() -> dict[str, str]:
    """Returns the environment of the build command.

    The worker's environment without the server's own variables
    (BUILDSERVER_*, which hold the session secret, the OAuth secrets and the
    token key) and without git configuration passed through the environment
    (GIT_CONFIG_*, which carries the clone token). Everything else in .env,
    such as a Vivado licence server, reaches the build.

    Returns:
        The environment variables.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("BUILDSERVER_", "GIT_CONFIG_"))
    }


def run_command(
    settings: config.Settings,
    database: db.Database,
    store: storage.Storage,
    build: db.Build,
    cwd: pathlib.Path,
    logfile: BinaryIO,
) -> None:
    """Runs the build command and waits for it to end.

    The command is killed if the build is cancelled or exceeds the build
    timeout. With a build account configured, it runs as that account, with
    the build's fresh home directory.

    Args:
        settings: The server settings.
        database: The build records, polled for a cancel request.
        store: The data directory.
        build: The build being run.
        cwd: Directory to run the command in.
        logfile: Open log file that receives the output of the command.

    Raises:
        BuildStoppedError: If the command failed or the build was cancelled.
    """
    command = [
        argument.replace(config.NAME_PLACEHOLDER, build["name"])
        for argument in settings.build_command
    ]
    result = _run_process(
        command,
        cwd,
        build_env(),
        settings.build_timeout,
        database,
        build,
        logfile,
        run_as=settings.build_user,
        home=store.home_dir(build["id"]),
    )
    _stop_unless_succeeded(result, "build command")


def clone_repo(
    settings: config.Settings,
    database: db.Database,
    build: db.Build,
    dest: pathlib.Path,
    logfile: BinaryIO,
) -> None:
    """Clones the branch or tag of a build from a repository.

    The clone uses the GitHub token of the user recorded as the build's
    source. It gets a build timeout of its own, separate from the build
    command's.

    Args:
        settings: The server settings.
        database: The records, for the token and for a cancel request.
        build: The build being run; its repo_full_name is set.
        dest: Directory to clone into; must not exist.
        logfile: Open log file that receives git's output.

    Raises:
        BuildStoppedError: If the owner's GitHub account is not linked,
            the clone failed, or the build was cancelled.
    """
    link = database.get_github_link(build["source_user_id"])
    if link is None:
        owner = database.get_user(build["source_user_id"])
        who = owner["email"] if owner else "the repository's owner"
        raise BuildStoppedError(
            db.FAILED, f"GitHub account of {who} is not linked"
        )
    command = git.clone_command(
        settings.git, build["repo_full_name"], build["ref"], dest
    )
    result = _run_process(
        command,
        dest.parent,
        git.clone_env(link["access_token"]),
        settings.build_timeout,
        database,
        build,
        logfile,
    )
    _stop_unless_succeeded(result, "git clone")
    database.set_commit(build["id"], git.head_commit(settings.git, dest))


def prepare_source(
    settings: config.Settings,
    database: db.Database,
    store: storage.Storage,
    build: db.Build,
    logfile: BinaryIO,
) -> pathlib.Path:
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
        The directory holding the build script.

    Raises:
        BuildStoppedError: If the source cannot be had or holds no build script.
    """
    work_dir = store.work_dir(build["id"])
    shutil.rmtree(work_dir, ignore_errors=True)
    if build["repo_full_name"]:
        clone_repo(settings, database, build, work_dir, logfile)
        source = "repository"
    else:
        storage.extract_package(store.package_path(build["id"]), work_dir)
        source = "package"
    root = storage.find_build_root(work_dir, settings.build_script)
    if root is None:
        raise BuildStoppedError(
            db.FAILED,
            f"{settings.build_script} not found at the top level of the"
            f" {source}",
        )
    return root


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
    home_dir = store.home_dir(build_id)
    log_path = store.log_path(build_id)
    status, exit_code, error = db.FAILED, None, None
    try:
        root = None
        if settings.build_user:
            # Whatever an earlier build left goes, so it cannot reach this
            # one, even if the worker died before cleaning up after it.
            sandbox.clean_up(settings.build_user, store.work_root)
            _empty_dir(store.work_root)
            sandbox.prepare_home(home_dir)
        with open(log_path, "wb") as logfile:
            # One failing build must not take the worker down.
            try:
                root = prepare_source(settings, database, store, build, logfile)
                run_command(settings, database, store, build, root, logfile)
                status, exit_code = db.SUCCEEDED, 0
            except BuildStoppedError as e:
                status, exit_code, error = e.status, e.exit_code, e.reason
            except Exception as e:  # pylint: disable=broad-exception-caught
                _logger.exception("build %s failed", build_id)
                status, error = db.FAILED, f"{type(e).__name__}: {e}"
            if error:
                logfile.write(f"[buildserver] {error}\n".encode())

        # Artifacts are collected whatever the outcome: the reports matter
        # most when a build fails.
        if root is not None:
            if settings.build_user:
                # No build process may change the files while they are zipped.
                sandbox.clean_up(settings.build_user, store.work_root)
            missing = collect_artifacts(settings, store, build, work_dir, root)
            if missing and status == db.SUCCEEDED:
                status, error = db.FAILED, missing
            elif missing:
                _logger.warning("build %s: %s", build_id, missing)
    except Exception as e:  # pylint: disable=broad-exception-caught
        _logger.exception("build %s failed", build_id)
        status, error = db.FAILED, f"{type(e).__name__}: {e}"
    finally:
        if settings.build_user:
            sandbox.clean_up(settings.build_user, store.work_root)
            shutil.rmtree(home_dir, ignore_errors=True)
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


def collect_artifacts(
    settings: config.Settings,
    store: storage.Storage,
    build: db.Build,
    work_dir: pathlib.Path,
    root: pathlib.Path,
) -> str | None:
    """Zips the artifact directory of a build together with its log.

    The worker can read what the build account cannot, so the artifacts must
    not reach outside the work directory. The artifact directory is refused
    if it, or a directory above it, is a symbolic link, and only regular
    files are zipped. Hard links are no way out either: the work root is its
    own mount, and the build account cannot enter the data directory.

    Args:
        settings: The server settings.
        store: The data directory.
        build: The build. Its processes must have ended.
        work_dir: The work directory of the build.
        root: The directory holding the build script, inside work_dir.

    Returns:
        Why no artifacts were collected, or None if they were.
    """
    relative = settings.artifact_dir.replace(
        config.NAME_PLACEHOLDER, build["name"]
    )
    artifact_dir = root / relative
    if _reaches_outside(work_dir, artifact_dir):
        return f"artifact directory {relative} is outside the work directory"
    if not artifact_dir.is_dir() or not storage.regular_files(artifact_dir):
        return f"build produced no artifacts in {relative}"
    storage.zip_dir(
        artifact_dir,
        store.artifacts_path(build["id"]),
        extra={"build.log": store.log_path(build["id"])},
    )
    return None


def _reaches_outside(work_dir: pathlib.Path, path: pathlib.Path) -> bool:
    """Tells whether a path under the work directory can lead out of it.

    Args:
        work_dir: The work directory of a build.
        path: A path inside it, which need not exist.

    Returns:
        True if the path resolves outside work_dir, or if it, work_dir or a
        directory between them is a symbolic link.
    """
    if not path.resolve().is_relative_to(work_dir.resolve()):
        return True
    return any(
        part.is_symlink()
        for part in (path, *path.parents)
        if part.is_relative_to(work_dir)
    )


def _empty_dir(path: pathlib.Path) -> None:
    """Deletes everything inside a directory, keeping the directory."""
    for entry in path.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


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


def delete_orphaned_dirs(
    database: db.Database,
    store: storage.Storage,
    now: float | None = None,
) -> int:
    """Deletes build directories whose build record is gone.

    A build delete that could not remove every file leaves such a directory.
    Directories younger than an hour are left alone, because a build's
    directory is created just before its record.

    Args:
        database: The build records.
        store: The data directory.
        now: The current time, as time.time() gives it. Defaults to the
            clock.

    Returns:
        The number of directories deleted.
    """
    now = time.time() if now is None else now
    deleted = 0
    for build_dir in store.build_dirs():
        build_id = build_dir.name
        if database.get(build_id) is not None:
            continue
        try:
            if now - build_dir.stat().st_mtime < _ORPHAN_GRACE_SECONDS:
                continue
            store.delete_build_dir(build_id)
        except OSError as e:
            _logger.warning(
                "could not delete files of deleted build %s: %s", build_id, e
            )
            continue
        _logger.info("deleted files of deleted build %s", build_id)
        deleted += 1
    return deleted


def main() -> None:
    """Runs the worker until it is interrupted."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    settings = config.load_settings()
    if settings.build_user:
        # Work files stay writable for the group the build account is in.
        os.umask(0o002)
    else:
        _logger.warning(
            "BUILDSERVER_BUILD_USER is not set: builds run as the worker's"
            " own account and can read the database and its secrets"
        )
    store = storage.Storage(settings.data_dir, settings.work_dir)
    database = db.Database(store.db_path, crypto.cipher_for(settings.token_key))
    interrupted = database.fail_interrupted()
    if interrupted:
        _logger.warning("marked %d interrupted build(s) as failed", interrupted)
    _logger.info(
        "worker ready, data dir %s, work dir %s, build account %s,"
        " command %s, %s",
        settings.data_dir,
        store.work_root,
        settings.build_user or "(the worker's)",
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
                delete_orphaned_dirs(database, store)
                next_expiry = time.monotonic() + settings.retention_interval
            if not run_once(settings, database, store):
                time.sleep(settings.poll_interval)
    except KeyboardInterrupt:
        _logger.info("worker stopped")


if __name__ == "__main__":
    main()
