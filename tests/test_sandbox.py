r"""Tests of running builds as a separate account (buildserver.sandbox).

The unit tests run anywhere: they check the command lines and, with sudo
faked away, how the worker uses them. The tests marked `sandbox` need the
Linux worker image, with its build account and work root, and run only with
BUILDSERVER_TEST_SANDBOX=1:

    docker compose run --rm -e BUILDSERVER_TEST_SANDBOX=1 worker \\
        python -m pytest -m sandbox
"""

import dataclasses
import io
import json
import os
import pathlib
import subprocess
import sys
import uuid
import zipfile

from fastapi import testclient
import pytest

from buildserver import api
from buildserver import config
from buildserver import db
from buildserver import sandbox
from buildserver import storage
from buildserver import worker

PROBE_BUILD = """
import json, os, pathlib, sys
out = pathlib.Path("bitfiles/generated") / sys.argv[1]
out.mkdir(parents=True)
result = {{"uid": os.getuid(), "home": os.environ["HOME"]}}
result["home_was_fresh"] = not (pathlib.Path.home() / "marker").exists()
(pathlib.Path.home() / "marker").write_text("left by an earlier build")
for name, path in {targets!r}.items():
    try:
        with open(path, "rb") as f:
            f.read(1)
        result[name] = "read"
    except OSError as e:
        result[name] = type(e).__name__
(out / "probe.json").write_text(json.dumps(result))
"""
# Leaves a process behind that has left the build's session.
DAEMON_BUILD = """
import pathlib, subprocess, sys
subprocess.Popen(["sleep", "300"], start_new_session=True)
out = pathlib.Path("bitfiles/generated") / sys.argv[1]
out.mkdir(parents=True)
(out / "done").write_text("yes")
"""
SLOW_BUILD = "import time\ntime.sleep(120)\n"
GOOD_BUILD = """
import pathlib, sys
out = pathlib.Path("bitfiles/generated") / sys.argv[1]
out.mkdir(parents=True)
(out / "fw.bit").write_bytes(b"bitstream")
"""


def make_package(script: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("build.py", script)
    return buf.getvalue()


def upload(client, script: str) -> dict:
    resp = client.post(
        "/api/builds",
        files={"package": ("pkg.zip", make_package(script), "application/zip")},
        data={"name": "fw"},
    )
    assert resp.status_code == 201
    return resp.json()


def artifact(client, build_id: str, name: str) -> bytes:
    resp = client.get(f"/api/builds/{build_id}/artifacts")
    assert resp.status_code == 200, client.get(f"/api/builds/{build_id}").json()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        return zf.read(name)


def make_env(tmp_path, auth_settings, sign_in, **settings_args):
    settings = config.Settings(
        data_dir=tmp_path / "data",
        build_command=[sys.executable, "build.py", "{name}"],
        build_script="build.py",
        artifact_dir="bitfiles/generated/{name}",
        **settings_args,
    )
    store = storage.Storage(settings.data_dir, settings.work_dir)
    database = db.Database(store.db_path)
    client = testclient.TestClient(api.create_app(settings, auth_settings))
    sign_in(client, "admin@example.com")
    return settings, database, store, client


# Command lines ##############################################################


def test_wrap_runs_as_the_account_with_a_clean_environment():
    home = pathlib.Path("/work/abc.home")
    argv = sandbox.wrap(
        ["vivado", "-mode", "batch"],
        "builder",
        home,
        {"PATH": "/usr/bin", "HOME": "/home/app", "XILINXD_LICENSE_FILE": "x"},
    )
    assert argv[:7] == ["sudo", "-n", "-u", "builder", "--", "env", "-i"]
    assert "PATH=/usr/bin" in argv and "XILINXD_LICENSE_FILE=x" in argv
    assert "HOME=/home/app" not in argv
    assert f"HOME={home}" in argv and f"TMPDIR={home / 'tmp'}" in argv
    assert argv[argv.index("-c") + 1] == 'umask 002; exec "$@"'
    assert argv[-3:] == ["vivado", "-mode", "batch"]


def test_wrap_gets_no_secrets_from_build_env(monkeypatch):
    monkeypatch.setenv("BUILDSERVER_TOKEN_KEY", "key-s3cret")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "token-s3cret")
    argv = sandbox.wrap(
        ["vivado"], "builder", pathlib.Path("/w/h"), worker.build_env()
    )
    assert not any("s3cret" in argument for argument in argv)


def test_kill_and_cleanup_run_as_the_account():
    assert sandbox.kill_command("builder") == [
        "sudo",
        "-n",
        "-u",
        "builder",
        "--",
        "kill",
        "-KILL",
        "-1",
    ]
    cleanup = sandbox.cleanup_command("builder")
    assert cleanup[:6] == ["sudo", "-n", "-u", "builder", "--", "find"]
    assert cleanup[-3:] == ["-user", "builder", "-delete"]
    release = sandbox.release_command("builder", pathlib.Path("/work"))
    assert release[5:] == [
        "chmod",
        "-R",
        "-f",
        "g+rwX",
        str(pathlib.Path("/work")),
    ]


@pytest.mark.skipif(os.name != "nt", reason="the check is for Windows")
def test_build_account_is_refused_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "BUILDSERVER_DATA_DIR=store\n"
        "BUILDSERVER_VIVADO=vivado\n"
        "BUILDSERVER_BUILD_SCRIPT=build.tcl\n"
        "BUILDSERVER_ARTIFACT_DIR=out/{name}\n"
        "BUILDSERVER_BUILD_USER=builder\n"
    )
    with pytest.raises(ValueError, match="only supported on Linux"):
        config.load_settings()


# The worker with sudo faked away ############################################


def test_worker_wraps_the_build_and_cleans_up(
    tmp_path, auth_settings, sign_in, monkeypatch
):
    settings, database, store, client = make_env(
        tmp_path, auth_settings, sign_in, build_user="builder"
    )
    seen = {}

    def fake_wrap(command, user, home, env):
        seen.update(user=user, home=home, env=env, tmp=(home / "tmp").is_dir())
        return command

    cleanups = []
    monkeypatch.setattr(sandbox, "wrap", fake_wrap)
    monkeypatch.setattr(
        sandbox, "clean_up", lambda user, root: cleanups.append((user, root))
    )
    monkeypatch.setenv("BUILDSERVER_SESSION_SECRET", "session-s3cret")
    stale = store.work_root / "stale.home"
    stale.mkdir(parents=True)

    build = upload(client, GOOD_BUILD)
    worker.run_once(settings, database, store)

    assert database.get(build["id"])["status"] == "succeeded"
    assert seen["user"] == "builder" and seen["tmp"]
    assert seen["home"] == store.home_dir(build["id"])
    assert "BUILDSERVER_SESSION_SECRET" not in seen["env"]
    assert cleanups == [("builder", store.work_root)] * 2
    assert not stale.exists() and not seen["home"].exists()


def test_worker_kills_through_the_account(
    tmp_path, auth_settings, sign_in, monkeypatch
):
    settings, database, store, client = make_env(
        tmp_path, auth_settings, sign_in, build_user="builder", build_timeout=2
    )
    killed = []
    monkeypatch.setattr(sandbox, "wrap", lambda command, *_: command)
    monkeypatch.setattr(sandbox, "clean_up", lambda *_: None)

    def fake_kill_all(user):
        killed.append(user)
        worker.kill_tree(running.pop())

    real_popen = subprocess.Popen
    running = []

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        running.append(process)
        return process

    monkeypatch.setattr(sandbox, "kill_all", fake_kill_all)
    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    build = upload(client, SLOW_BUILD)
    worker.run_once(settings, database, store)

    build = database.get(build["id"])
    assert build["status"] == "failed" and "timed out" in build["error"]
    assert killed == ["builder"]


# In the worker image ########################################################

needs_sandbox = pytest.mark.skipif(
    os.environ.get("BUILDSERVER_TEST_SANDBOX") != "1",
    reason="needs the worker image; set BUILDSERVER_TEST_SANDBOX=1",
)
BUILD_USER = os.environ.get("BUILDSERVER_TEST_BUILD_USER", "builder")


@pytest.fixture
def sandboxed(tmp_path, auth_settings, sign_in):
    """Provides an environment whose builds run as the build account."""
    old_umask = os.umask(0o002)
    work_root = (
        pathlib.Path(os.environ.get("BUILDSERVER_TEST_WORK_DIR", "/work"))
        / f"test-{uuid.uuid4().hex[:8]}"
    )
    try:
        yield make_env(
            tmp_path,
            auth_settings,
            sign_in,
            build_user=BUILD_USER,
            work_dir=work_root,
        )
    finally:
        sandbox.clean_up(BUILD_USER, work_root)
        subprocess.run(["rm", "-rf", str(work_root)], check=False)
        os.umask(old_umask)


def builder_processes() -> str:
    result = subprocess.run(
        ["pgrep", "-u", BUILD_USER, "-a"], capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.mark.sandbox
@needs_sandbox
def test_build_cannot_read_the_workers_secrets(sandboxed):
    settings, database, store, client = sandboxed
    first = upload(client, "")  # only its package is needed as a target
    targets = {
        "database": str(store.db_path),
        "other_package": str(store.package_path(first["id"])),
        "worker_environ": f"/proc/{os.getpid()}/environ",
    }
    script = PROBE_BUILD.format(targets=targets)
    database.request_cancel(first["id"])
    probes = []
    for _ in range(2):
        build = upload(client, script)
        worker.run_once(settings, database, store)
        probes.append(json.loads(artifact(client, build["id"], "probe.json")))

    uid = int(
        subprocess.run(
            ["id", "-u", BUILD_USER], capture_output=True, text=True, check=True
        ).stdout
    )
    for probe in probes:
        assert probe["uid"] == uid
        assert probe["database"] == "PermissionError"
        assert probe["other_package"] == "PermissionError"
        assert probe["worker_environ"] == "PermissionError"
        assert probe["home_was_fresh"] is True
    assert probes[0]["home"] != probes[1]["home"]


@pytest.mark.sandbox
@needs_sandbox
def test_build_leaves_no_process_behind(sandboxed):
    settings, database, store, client = sandboxed
    build = upload(client, DAEMON_BUILD)
    worker.run_once(settings, database, store)
    assert database.get(build["id"])["status"] == "succeeded"
    assert builder_processes() == ""


@pytest.mark.sandbox
@needs_sandbox
def test_timed_out_build_is_killed(sandboxed):
    settings, database, store, client = sandboxed
    settings = dataclasses.replace(settings, build_timeout=2)
    build = upload(client, SLOW_BUILD)
    worker.run_once(settings, database, store)
    assert "timed out" in database.get(build["id"])["error"]
    assert builder_processes() == ""
    assert not store.home_dir(build["id"]).exists()
