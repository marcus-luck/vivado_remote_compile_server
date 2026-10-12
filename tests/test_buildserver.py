"""Tests of the API, the front end routes and the worker.

A small Python script stands in for build.tcl and Vivado.
"""

import io
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


def make_package(script: str, prefix: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(prefix + "build.py", script)
    return buf.getvalue()


@pytest.fixture
def env(tmp_path):
    """Provides settings, database, storage and an API client."""
    settings = config.Settings(
        data_dir=tmp_path,
        build_command=[sys.executable, "build.py", "{name}"],
        build_script="build.py",
        artifact_dir="bitfiles/generated/{name}",
    )
    store = storage.Storage(settings.data_dir)
    database = db.Database(store.db_path)
    client = testclient.TestClient(api.create_app(settings))
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
