"""Build worker: takes queued builds one at a time and runs them.

Run with:

    python -m buildserver.worker
"""

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
    logfile.write(f"[buildserver] running: {' '.join(command)}\n".encode())
    logfile.flush()
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=logfile,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
    except OSError as e:
        return db.FAILED, None, f"could not start build command: {e}"

    deadline = time.monotonic() + settings.build_timeout
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
                return (
                    db.FAILED,
                    None,
                    f"build timed out after {settings.build_timeout:.0f} s",
                )
    finally:
        if process.poll() is None:
            kill_tree(process)

    if exit_code != 0:
        return (
            db.FAILED,
            exit_code,
            f"build command exited with code {exit_code}",
        )
    return db.SUCCEEDED, exit_code, None


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
                shutil.rmtree(work_dir, ignore_errors=True)
                storage.extract_package(store.package_path(build_id), work_dir)
                root = storage.find_build_root(work_dir, settings.build_script)
                if root is None:
                    error = (
                        f"{settings.build_script} not found at the top level"
                        " of the package"
                    )
                else:
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
        "worker ready, data dir %s, command %s",
        settings.data_dir,
        settings.build_command,
    )
    try:
        while True:
            if not run_once(settings, database, store):
                time.sleep(settings.poll_interval)
    except KeyboardInterrupt:
        _logger.info("worker stopped")


if __name__ == "__main__":
    main()
