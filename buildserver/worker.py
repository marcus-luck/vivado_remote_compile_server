"""Build worker: takes queued builds one at a time and runs them.

Run with:  python -m buildserver.worker
"""

import logging
import os
import shutil
import signal
import subprocess
import time

from . import db as states
from .config import NAME_PLACEHOLDER, Settings, load_settings
from .db import Database
from .storage import Storage, extract_package, find_build_root, zip_dir

log = logging.getLogger("buildserver.worker")


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the build process and its children (vivado.bat spawns the real Vivado)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait()


def run_command(settings: Settings, db: Database, build: dict, cwd, logfile) -> tuple[str, int | None, str | None]:
    """Run the build command, returning (status, exit_code, error)."""
    command = [arg.replace(NAME_PLACEHOLDER, build["name"]) for arg in settings.build_command]
    logfile.write(f"[buildserver] running: {' '.join(command)}\n".encode())
    logfile.flush()
    try:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=logfile,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
    except OSError as e:
        return states.FAILED, None, f"could not start build command: {e}"

    deadline = time.monotonic() + settings.build_timeout
    try:
        while True:
            try:
                exit_code = proc.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                pass
            if db.cancel_requested(build["id"]):
                kill_tree(proc)
                return states.CANCELLED, None, None
            if time.monotonic() > deadline:
                kill_tree(proc)
                return states.FAILED, None, f"build timed out after {settings.build_timeout:.0f} s"
    finally:
        if proc.poll() is None:
            kill_tree(proc)

    if exit_code != 0:
        return states.FAILED, exit_code, f"build command exited with code {exit_code}"
    return states.SUCCEEDED, exit_code, None


def run_build(settings: Settings, db: Database, storage: Storage, build: dict) -> None:
    build_id = build["id"]
    work_dir = storage.work_dir(build_id)
    log_path = storage.log_path(build_id)
    status, exit_code, error = states.FAILED, None, None
    try:
        root = None
        with open(log_path, "wb") as logfile:
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
                extract_package(storage.package_path(build_id), work_dir)
                root = find_build_root(work_dir, settings.build_script)
                if root is None:
                    error = f"{settings.build_script} not found at the top level of the package"
                else:
                    status, exit_code, error = run_command(settings, db, build, root, logfile)
            except Exception as e:
                log.exception("build %s failed", build_id)
                status, error = states.FAILED, f"{type(e).__name__}: {e}"
            if error:
                logfile.write(f"[buildserver] {error}\n".encode())

        # Collected whatever the outcome: the reports matter most when a build fails.
        if root is not None:
            artifact_dir = root / settings.artifact_dir.replace(NAME_PLACEHOLDER, build["name"])
            has_artifacts = artifact_dir.is_dir() and any(p.is_file() for p in artifact_dir.rglob("*"))
            if has_artifacts:
                zip_dir(artifact_dir, storage.artifacts_path(build_id), extra={"build.log": log_path})
            elif status == states.SUCCEEDED:
                status = states.FAILED
                error = f"build produced no artifacts in {artifact_dir.relative_to(root).as_posix()}"
    except Exception as e:
        log.exception("build %s failed", build_id)
        status, error = states.FAILED, f"{type(e).__name__}: {e}"
    finally:
        if not settings.keep_workdir:
            shutil.rmtree(work_dir, ignore_errors=True)
    db.finish(build_id, status, exit_code, error)
    log.info("build %s (%s) %s%s", build_id, build["name"], status, f": {error}" if error else "")


def run_once(settings: Settings, db: Database, storage: Storage) -> bool:
    """Run the next queued build, if any. Returns whether one was run."""
    build = db.claim_next()
    if build is None:
        return False
    log.info("build %s (%s) started", build["id"], build["name"])
    run_build(settings, db, storage, build)
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    storage = Storage(settings.data_dir)
    db = Database(storage.db_path)
    interrupted = db.fail_interrupted()
    if interrupted:
        log.warning("marked %d interrupted build(s) as failed", interrupted)
    log.info("worker ready, data dir %s, command %s", settings.data_dir, settings.build_command)
    try:
        while True:
            if not run_once(settings, db, storage):
                time.sleep(settings.poll_interval)
    except KeyboardInterrupt:
        log.info("worker stopped")


if __name__ == "__main__":
    main()
