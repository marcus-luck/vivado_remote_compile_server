"""Tests of the API, the front end routes and the worker.

A small Python script stands in for build.tcl and Vivado.
"""

import dataclasses
import datetime
import io
import json
import os
import sys
import threading
import time
import zipfile

from fastapi import testclient
import pytest

from buildserver import api
from buildserver import config
from buildserver import db
from buildserver import storage
from buildserver import worker

# Stand-in for build.tcl + Vivado: writes <name>.bit where the real flow would.
GOOD_BUILD = """
import pathlib, sys
name = sys.argv[1]
out = pathlib.Path("bitfiles/generated") / name
(out / "route").mkdir(parents=True)
(out / f"{name}.bit").write_bytes(b"bitstream")
(out / "route" / "timing.rpt").write_text("met")
print("building", name)
"""
FAILING_BUILD = "import sys\nprint('synthesis failed')\nsys.exit(3)\n"
SLOW_BUILD = "import time\ntime.sleep(120)\n"
# Writes its environment where the artifacts are collected.
ENV_BUILD = """
import json, os, pathlib, sys
out = pathlib.Path("bitfiles/generated") / sys.argv[1]
out.mkdir(parents=True)
(out / "env.json").write_text(json.dumps(dict(os.environ)))
"""


def make_package(script: str, prefix: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(prefix + "build.py", script)
    return buf.getvalue()


@pytest.fixture
def env(tmp_path, auth_settings, sign_in):
    """Provides settings, database, storage and a signed-in API client."""
    settings = config.Settings(
        data_dir=tmp_path,
        build_command=[sys.executable, "build.py", "{name}"],
        build_script="build.py",
        artifact_dir="bitfiles/generated/{name}",
    )
    store = storage.Storage(settings.data_dir)
    database = db.Database(store.db_path)
    client = testclient.TestClient(api.create_app(settings, auth_settings))
    sign_in(client, "admin@example.com")
    return settings, database, store, client


def upload(
    client,
    package: bytes,
    name: str | None = "fw_v1",
    filename: str = "pkg.zip",
):
    data = {"name": name} if name else {}
    return client.post(
        "/api/builds",
        files={"package": (filename, package, "application/zip")},
        data=data,
    )


def test_build_succeeds_and_artifacts_download(env):
    settings, database, store, client = env
    resp = upload(client, make_package(GOOD_BUILD))
    assert resp.status_code == 201
    build = resp.json()
    assert build["status"] == "queued" and not build["has_artifacts"]
    assert build["created_by"] == "admin@example.com"

    assert worker.run_once(settings, database, store) is True
    assert worker.run_once(settings, database, store) is False

    build = client.get(f"/api/builds/{build['id']}").json()
    assert (
        build["status"] == "succeeded"
        and build["exit_code"] == 0
        and build["has_artifacts"]
    )
    assert "building fw_v1" in client.get(f"/api/builds/{build['id']}/log").text

    resp = client.get(f"/api/builds/{build['id']}/artifacts")
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert set(zf.namelist()) == {
            "fw_v1.bit",
            "route/timing.rpt",
            "build.log",
        }
        assert zf.read("fw_v1.bit") == b"bitstream"
    assert not store.work_dir(build["id"]).exists()


def test_build_command_gets_no_server_secrets(env, monkeypatch):
    settings, database, store, client = env
    monkeypatch.setenv("BUILDSERVER_SESSION_SECRET", "session-s3cret")
    monkeypatch.setenv("BUILDSERVER_TOKEN_KEY", "key-s3cret")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("XILINXD_LICENSE_FILE", "2100@licence-host")
    build = upload(client, make_package(ENV_BUILD)).json()
    worker.run_once(settings, database, store)

    resp = client.get(f"/api/builds/{build['id']}/artifacts")
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        build_env = json.loads(zf.read("env.json"))
    assert build_env["XILINXD_LICENSE_FILE"] == "2100@licence-host"
    for key in build_env:
        assert not key.upper().startswith(("BUILDSERVER_", "GIT_CONFIG_"))
    assert "s3cret" not in json.dumps(build_env)


def test_package_with_single_top_level_folder(env):
    settings, database, store, client = env
    build = upload(
        client, make_package(GOOD_BUILD, prefix="my_project/")
    ).json()
    worker.run_once(settings, database, store)
    assert (
        client.get(f"/api/builds/{build['id']}").json()["status"] == "succeeded"
    )


def test_failing_build(env):
    settings, database, store, client = env
    build = upload(client, make_package(FAILING_BUILD)).json()
    worker.run_once(settings, database, store)
    build = client.get(f"/api/builds/{build['id']}").json()
    assert build["status"] == "failed" and build["exit_code"] == 3
    assert not build["has_artifacts"]
    assert client.get(f"/api/builds/{build['id']}/artifacts").status_code == 404
    assert (
        "synthesis failed" in client.get(f"/api/builds/{build['id']}/log").text
    )


def needs_symlinks(tmp_path):
    """Skips the test where symbolic links cannot be made (Windows)."""
    try:
        os.symlink(tmp_path, tmp_path / "probe-link")
    except OSError:
        pytest.skip("cannot make symbolic links here")
    os.unlink(tmp_path / "probe-link")


def test_zip_dir_skips_links_and_special_files(tmp_path):
    needs_symlinks(tmp_path)
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "builds.db").write_text("secret")
    src = tmp_path / "src"
    (src / "route").mkdir(parents=True)
    (src / "fw.bit").write_text("bitstream")
    (src / "route" / "timing.rpt").write_text("met")
    os.symlink(secret_dir / "builds.db", src / "db")
    os.symlink(secret_dir, src / "data")
    if hasattr(os, "mkfifo"):
        os.mkfifo(src / "fifo")

    storage.zip_dir(src, tmp_path / "out.zip")

    with zipfile.ZipFile(tmp_path / "out.zip") as zf:
        assert zf.namelist() == ["fw.bit", "route/timing.rpt"]


# Makes the artifact directory a link to a directory outside the package.
OUTSIDE_BUILD = """
import os, pathlib, sys
outside = pathlib.Path(sys.argv[2])
(outside / "generated" / sys.argv[1]).mkdir(parents=True)
(outside / "generated" / sys.argv[1] / "builds.db").write_text("secret")
os.symlink(outside, "bitfiles")
"""


def test_artifact_directory_outside_the_work_directory(env, tmp_path):
    settings, database, store, client = env
    needs_symlinks(tmp_path)
    outside = tmp_path / "outside"
    settings = dataclasses.replace(
        settings,
        build_command=[sys.executable, "build.py", "{name}", str(outside)],
    )
    build = upload(client, make_package(OUTSIDE_BUILD)).json()
    worker.run_once(settings, database, store)

    build = client.get(f"/api/builds/{build['id']}").json()
    assert build["status"] == "failed"
    assert "outside the work directory" in build["error"]
    assert not build["has_artifacts"]


def test_missing_build_script(env):
    settings, database, store, client = env
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "no build script here")
    build = upload(client, buf.getvalue()).json()
    worker.run_once(settings, database, store)
    build = client.get(f"/api/builds/{build['id']}").json()
    assert (
        build["status"] == "failed" and "build.py not found" in build["error"]
    )


def test_builds_run_in_queue_order_and_list_newest_first(env):
    settings, database, store, client = env
    first = upload(client, make_package(GOOD_BUILD), name="first").json()
    second = upload(client, make_package(GOOD_BUILD), name="second").json()
    assert [b["id"] for b in client.get("/api/builds").json()] == [
        second["id"],
        first["id"],
    ]
    worker.run_once(settings, database, store)
    assert (
        client.get(f"/api/builds/{first['id']}").json()["status"] == "succeeded"
    )
    assert (
        client.get(f"/api/builds/{second['id']}").json()["status"] == "queued"
    )


def test_upload_validation(env):
    _, _, store, client = env
    assert upload(client, b"not a zip").status_code == 400
    assert (
        upload(client, make_package(GOOD_BUILD), name="../evil").status_code
        == 422
    )
    assert client.get("/api/builds").json() == []
    assert list(store.builds_dir.iterdir()) == []
    # name defaults to the sanitised package file name
    assert (
        upload(
            client,
            make_package(GOOD_BUILD),
            name=None,
            filename="my fw (2).zip",
        ).json()["name"]
        == "my_fw_2_"
    )
    assert client.get("/api/builds/doesnotexist").status_code == 404


def test_cancel_queued_build(env):
    settings, database, store, client = env
    build = upload(client, make_package(GOOD_BUILD)).json()
    assert (
        client.post(f"/api/builds/{build['id']}/cancel").json()["status"]
        == "cancelled"
    )
    assert worker.run_once(settings, database, store) is False


def test_cancel_running_build(env):
    settings, database, store, client = env
    build = upload(client, make_package(SLOW_BUILD)).json()
    thread = threading.Thread(
        target=worker.run_once, args=(settings, database, store)
    )
    thread.start()
    deadline = time.monotonic() + 10
    while (
        client.get(f"/api/builds/{build['id']}").json()["status"] != "running"
    ):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert (
        client.post(f"/api/builds/{build['id']}/cancel").json()[
            "cancel_requested"
        ]
        is True
    )
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert (
        client.get(f"/api/builds/{build['id']}").json()["status"] == "cancelled"
    )


def test_interrupted_builds_are_failed_on_worker_start(env):
    _, database, _, client = env
    build = upload(client, make_package(GOOD_BUILD)).json()
    database.claim_next()
    assert database.fail_interrupted() == 1
    assert client.get(f"/api/builds/{build['id']}").json()["status"] == "failed"


def test_settings_come_from_env_file(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {"BUILDSERVER_BUILD_TIMEOUT": "60"})
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "BUILDSERVER_DATA_DIR=store\n"
        "BUILDSERVER_VIVADO=C:\\Xilinx\\Vivado\\2023.2\\bin\\vivado.bat\n"
        "BUILDSERVER_BUILD_SCRIPT=build.tcl\n"
        "BUILDSERVER_ARTIFACT_DIR=out/{name}\n"
        "BUILDSERVER_BUILD_TIMEOUT=120\n"
        "BUILDSERVER_KEEP_WORKDIR=1\n"
        "BUILDSERVER_RETENTION_DAYS=14\n"
        "XILINXD_LICENSE_FILE=2100@host\n"
    )
    settings = config.load_settings()
    assert settings.data_dir == tmp_path / "store"
    assert settings.build_command[:2] == [
        r"C:\Xilinx\Vivado\2023.2\bin\vivado.bat",
        "-mode",
    ]
    assert settings.build_script == "build.tcl"
    assert settings.artifact_dir == "out/{name}"
    assert settings.keep_workdir is True
    assert settings.retention_days == 14
    assert settings.build_timeout == 60  # the real environment wins over .env
    assert (
        os.environ["XILINXD_LICENSE_FILE"] == "2100@host"
    )  # inherited by the build command

    # A full build command makes the Vivado path unnecessary.
    (tmp_path / ".env").write_text(
        "BUILDSERVER_DATA_DIR=store\n"
        "BUILDSERVER_BUILD_SCRIPT=build.tcl\n"
        "BUILDSERVER_ARTIFACT_DIR=out/{name}\n"
        """BUILDSERVER_BUILD_COMMAND='["radiantc", "build.tcl", "{name}"]'\n"""
    )
    monkeypatch.setattr(os, "environ", {})
    assert config.load_settings().build_command == [
        "radiantc",
        "build.tcl",
        "{name}",
    ]


@pytest.mark.parametrize(
    "missing",
    [
        "BUILDSERVER_DATA_DIR",
        "BUILDSERVER_VIVADO",
        "BUILDSERVER_BUILD_SCRIPT",
        "BUILDSERVER_ARTIFACT_DIR",
    ],
)
def test_path_settings_have_no_default(tmp_path, monkeypatch, missing):
    variables = {
        "BUILDSERVER_DATA_DIR": "store",
        "BUILDSERVER_VIVADO": "vivado",
        "BUILDSERVER_BUILD_SCRIPT": "build.tcl",
        "BUILDSERVER_ARTIFACT_DIR": "out/{name}",
    }
    del variables[missing]
    monkeypatch.setattr(os, "environ", variables)
    monkeypatch.chdir(tmp_path)  # no .env here
    with pytest.raises(ValueError, match=missing):
        config.load_settings()


def ui_upload(client, package: bytes, name: str = "ui_fw"):
    return client.post(
        "/ui/builds",
        files={"package": ("pkg.zip", package, "application/zip")},
        data={"name": name},
    )


def test_health_check_needs_no_sign_in(tmp_path, auth_settings):
    settings = config.Settings(
        data_dir=tmp_path,
        build_command=["true"],
        build_script="build.py",
        artifact_dir="out",
    )
    client = testclient.TestClient(api.create_app(settings, auth_settings))
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_front_end_is_served(env):
    client = env[3]
    page = client.get("/").text
    assert "hx-get" in page and "No builds yet." in page
    assert client.get("/static/vendor/htmx.min.js").status_code == 200


def test_ui_upload_list_and_cancel(env):
    _, _, _, client = env
    resp = ui_upload(client, make_package(GOOD_BUILD))
    assert resp.status_code == 200 and resp.headers["HX-Trigger"] == "refresh"
    assert "error" not in resp.text

    build = client.get("/api/builds").json()[0]
    table = client.get("/ui/builds").text
    assert "ui_fw" in table and "queued" in table and "Cancel" in table

    table = client.post(f"/ui/builds/{build['id']}/cancel").text
    assert "cancelled" in table and "Cancel</button>" not in table


def test_ui_upload_error_is_shown_in_the_form(env):
    _, _, _, client = env
    resp = ui_upload(client, b"not a zip", name="keep_me")
    assert resp.status_code == 200 and "HX-Trigger" not in resp.headers
    assert (
        "package must be a zip archive" in resp.text
        and 'value="keep_me"' in resp.text
    )
    assert client.get("/api/builds").json() == []


def test_ui_log_polls_until_build_is_over(env):
    settings, database, store, client = env
    ui_upload(
        client, make_package("print('<b>synth</b>')\nraise SystemExit(1)\n")
    )
    build = client.get("/api/builds").json()[0]
    assert client.get(f"/ui/builds/{build['id']}/log/text").status_code == 200

    worker.run_once(settings, database, store)
    resp = client.get(f"/ui/builds/{build['id']}/log/text")
    assert resp.status_code == 286
    assert "&lt;b&gt;synth&lt;/b&gt;" in resp.text
    assert (
        "&lt;b&gt;synth&lt;/b&gt;"
        in client.get(f"/ui/builds/{build['id']}/log").text
    )
    assert "Download" not in client.get("/ui/builds").text


def other_client(client, sign_in, email: str = "dev@team.example.com"):
    """Returns a second client of the same app, signed in as another user."""
    other = testclient.TestClient(client.app)
    sign_in(other, email)
    return other


def test_creator_deletes_build_with_its_files(env, sign_in):
    settings, database, store, client = env
    mine = other_client(client, sign_in)
    build = upload(mine, make_package(GOOD_BUILD)).json()
    worker.run_once(settings, database, store)
    assert store.build_dir(build["id"]).is_dir()

    assert mine.delete(f"/api/builds/{build['id']}").status_code == 204
    assert mine.get(f"/api/builds/{build['id']}").status_code == 404
    assert not store.build_dir(build["id"]).exists()
    assert mine.delete(f"/api/builds/{build['id']}").status_code == 404


def test_only_creator_or_admin_deletes(env, sign_in):
    _, _, _, admin = env
    build = upload(other_client(admin, sign_in), make_package(GOOD_BUILD))
    build_id = build.json()["id"]
    stranger = other_client(admin, sign_in, "eve@team.example.com")
    assert stranger.delete(f"/api/builds/{build_id}").status_code == 404
    assert "Delete</button>" not in stranger.get("/ui/builds").text

    assert "Delete</button>" in admin.get("/ui/builds").text
    assert admin.delete(f"/api/builds/{build_id}").status_code == 204


def test_builds_are_visible_only_to_creator_and_admin(env, sign_in):
    settings, database, store, admin = env
    mine = other_client(admin, sign_in)
    build = upload(mine, make_package(GOOD_BUILD), name="secret_fw").json()
    worker.run_once(settings, database, store)
    stranger = other_client(admin, sign_in, "eve@team.example.com")
    upload(stranger, make_package(GOOD_BUILD), name="eves_fw")

    base = f"/api/builds/{build['id']}"
    for path in (base, f"{base}/log", f"{base}/artifacts"):
        assert stranger.get(path).status_code == 404, path
        assert mine.get(path).status_code == 200, path
        assert admin.get(path).status_code == 200, path
    for path in (
        f"/ui/builds/{build['id']}/log",
        f"/ui/builds/{build['id']}/log/text",
    ):
        assert stranger.get(path).status_code == 404, path
    assert stranger.post(f"{base}/cancel").status_code == 404

    assert [b["name"] for b in stranger.get("/api/builds").json()] == [
        "eves_fw"
    ]
    assert [b["name"] for b in mine.get("/api/builds").json()] == ["secret_fw"]
    assert {b["name"] for b in admin.get("/api/builds").json()} == {
        "secret_fw",
        "eves_fw",
    }
    assert "secret_fw" not in stranger.get("/ui/builds").text
    assert "secret_fw" in admin.get("/ui/builds").text


def test_strangers_cannot_cancel_from_the_table(env, sign_in):
    _, database, _, admin = env
    build = upload(other_client(admin, sign_in), make_package(GOOD_BUILD))
    stranger = other_client(admin, sign_in, "eve@team.example.com")
    stranger.post(f"/ui/builds/{build.json()['id']}/cancel")
    assert database.get(build.json()["id"])["status"] == "queued"


def test_deleted_queued_build_is_not_run(env):
    settings, database, store, client = env
    build = upload(client, make_package(GOOD_BUILD)).json()
    assert client.delete(f"/api/builds/{build['id']}").status_code == 204
    assert worker.run_once(settings, database, store) is False


def test_running_build_cannot_be_deleted(env):
    _, database, store, client = env
    build = upload(client, make_package(GOOD_BUILD)).json()
    database.claim_next()
    resp = client.delete(f"/api/builds/{build['id']}")
    assert resp.status_code == 409
    assert store.package_path(build["id"]).is_file()
    assert "Delete</button>" not in client.get("/ui/builds").text


def test_ui_delete(env):
    _, _, _, client = env
    ui_upload(client, make_package(GOOD_BUILD), name="doomed")
    build = client.get("/api/builds").json()[0]
    assert "Delete</button>" in client.get("/ui/builds").text
    table = client.post(f"/ui/builds/{build['id']}/delete").text
    assert "doomed" not in table and "No builds yet." in table


def test_files_left_by_a_failed_delete_are_swept_later(env, monkeypatch):
    settings, database, store, client = env
    gone = upload(client, make_package(GOOD_BUILD), name="gone").json()
    kept = upload(client, make_package(GOOD_BUILD), name="kept").json()
    worker.run_once(settings, database, store)

    def locked(self, build_id):
        raise PermissionError(f"{build_id}/artifacts.zip is open")

    with monkeypatch.context() as patch:
        patch.setattr(storage.Storage, "delete_build_dir", locked)
        assert client.delete(f"/api/builds/{gone['id']}").status_code == 204
    assert client.get(f"/api/builds/{gone['id']}").status_code == 404
    assert store.build_dir(gone["id"]).is_dir()

    # Young directories may belong to a build being created: left alone.
    assert worker.delete_orphaned_dirs(database, store) == 0
    later = time.time() + 2 * 3600
    assert worker.delete_orphaned_dirs(database, store, later) == 1
    assert not store.build_dir(gone["id"]).exists()
    assert store.package_path(kept["id"]).is_file()


def test_retention_expires_old_files_and_keeps_log(env):
    settings, database, store, client = env
    old = upload(client, make_package(GOOD_BUILD), name="old").json()
    worker.run_once(settings, database, store)
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        days=2
    )
    recent = upload(client, make_package(GOOD_BUILD), name="new").json()

    # Disabled by default.
    assert worker.expire_old_files(settings, database, store, later) == 0

    settings = dataclasses.replace(settings, retention_days=1)
    # The recent build is still queued, so it has no finish time yet.
    assert worker.expire_old_files(settings, database, store, later) == 1
    assert not store.package_path(old["id"]).exists()
    assert not store.artifacts_path(old["id"]).exists()
    assert store.log_path(old["id"]).is_file()
    old = client.get(f"/api/builds/{old['id']}").json()
    assert old["files_expired_at"] and not old["has_artifacts"]
    assert old["status"] == "succeeded"
    assert "files expired" in client.get("/ui/builds").text
    assert client.get(f"/api/builds/{old['id']}/log").text

    worker.run_once(settings, database, store)
    assert worker.expire_old_files(settings, database, store) == 0
    assert store.artifacts_path(recent["id"]).is_file()
    assert store.package_path(recent["id"]).is_file()
