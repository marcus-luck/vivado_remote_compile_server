"""Tests of API tokens and service accounts."""

import io
import sys
import zipfile

from fastapi import testclient
import pytest

from buildserver import api
from buildserver import config

ADMIN = "admin@example.com"
DEV = "dev@team.example.com"


@pytest.fixture
def app(tmp_path, auth_settings):
    settings = config.Settings(
        data_dir=tmp_path,
        build_command=[sys.executable, "build.py", "{name}"],
        build_script="build.py",
        artifact_dir="out/{name}",
    )
    return api.create_app(settings, auth_settings)


@pytest.fixture
def admin(app, sign_in):
    """Provides a client signed in as the configured administrator."""
    client = testclient.TestClient(app)
    assert sign_in(client, ADMIN).status_code == 303
    return client


@pytest.fixture
def dev(app, sign_in):
    """Provides a client signed in as a plain user."""
    client = testclient.TestClient(app)
    assert sign_in(client, DEV).status_code == 303
    return client


def bearer(app, token: str) -> testclient.TestClient:
    """Returns a client without a session that sends an API token."""
    return testclient.TestClient(
        app, headers={"Authorization": f"Bearer {token}"}
    )


def package() -> bytes:
    """Returns a minimal build package."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("build.py", "print('hi')\n")
    return buf.getvalue()


def test_token_authenticates_api_requests(app, dev):
    resp = dev.post("/api/tokens", json={"name": "laptop"})
    assert resp.status_code == 201
    created = resp.json()
    token = created["token"]
    assert token.startswith("bst_") and created["prefix"] == token[4:12]
    assert created["expires_at"] is None and created["last_used_at"] is None

    robot = bearer(app, token)
    assert robot.get("/api/me").json()["email"] == DEV
    resp = robot.post(
        "/api/builds",
        files={"package": ("proj.zip", package(), "application/zip")},
    )
    assert resp.status_code == 201 and resp.json()["created_by"] == DEV

    listed = dev.get("/api/tokens").json()
    assert [t["name"] for t in listed] == ["laptop"]
    assert "token" not in listed[0] and "token_hash" not in listed[0]
    assert listed[0]["last_used_at"]


def test_invalid_tokens_are_refused(app, dev):
    resp = bearer(app, "bst_nonsense").get("/api/me")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"

    # A bad token is not rescued by a valid session.
    resp = dev.get("/api/me", headers={"Authorization": "Bearer bst_nope"})
    assert resp.status_code == 401


def test_expiring_token(app, dev):
    resp = dev.post("/api/tokens", json={"name": "ci", "expires_in_days": 30})
    assert resp.json()["expires_at"] > resp.json()["created_at"]
    for days in (0, 3651):
        bad = dev.post(
            "/api/tokens", json={"name": "x", "expires_in_days": days}
        )
        assert bad.status_code == 422
    assert dev.post("/api/tokens", json={"name": ""}).status_code == 422


def test_expired_token_is_refused(app, dev):
    token = dev.post("/api/tokens", json={"name": "old"}).json()
    database = app.state.database
    with database._connect() as conn:
        conn.execute(
            "UPDATE api_tokens SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00.000+00:00", token["id"]),
        )
    assert bearer(app, token["token"]).get("/api/me").status_code == 401


def test_tokens_cannot_create_tokens(app, dev):
    token = dev.post("/api/tokens", json={"name": "a"}).json()["token"]
    robot = bearer(app, token)
    assert robot.post("/api/tokens", json={"name": "b"}).status_code == 403
    resp = robot.post("/ui/tokens", data={"name": "b"})
    assert resp.status_code == 403
    assert len(dev.get("/api/tokens").json()) == 1


def test_revoking_tokens(app, admin, dev):
    mine = dev.post("/api/tokens", json={"name": "mine"}).json()
    admins = admin.post("/api/tokens", json={"name": "admin's"}).json()

    # Users cannot see or revoke other users' tokens.
    assert dev.delete(f"/api/tokens/{admins['id']}").status_code == 404
    assert [t["name"] for t in dev.get("/api/tokens").json()] == ["mine"]

    # A token can revoke itself.
    robot = bearer(app, mine["token"])
    assert robot.delete(f"/api/tokens/{mine['id']}").status_code == 204
    assert robot.get("/api/me").status_code == 401
    assert dev.delete(f"/api/tokens/{mine['id']}").status_code == 404

    # An admin can revoke anyone's token.
    other = dev.post("/api/tokens", json={"name": "other"}).json()
    assert admin.delete(f"/api/tokens/{other['id']}").status_code == 204
    assert bearer(app, other["token"]).get("/api/me").status_code == 401


def test_deactivated_and_deleted_users_lose_their_tokens(app, admin, dev):
    me = dev.get("/api/me").json()
    token = dev.post("/api/tokens", json={"name": "t"}).json()["token"]
    robot = bearer(app, token)

    admin.patch(f"/api/users/{me['id']}", json={"is_active": False})
    assert robot.get("/api/me").status_code == 401
    admin.patch(f"/api/users/{me['id']}", json={"is_active": True})
    assert robot.get("/api/me").status_code == 200

    assert admin.delete(f"/api/users/{me['id']}").status_code == 204
    assert robot.get("/api/me").status_code == 401
    assert app.state.database.list_tokens(me["id"]) == []


def test_service_account(app, admin, dev, sign_in):
    resp = admin.post(
        "/api/users", json={"email": "CI-Nightly", "is_service": True}
    )
    assert resp.status_code == 201
    robot_user = resp.json()
    assert robot_user["email"] == "ci-nightly" and robot_user["is_service"]

    for bad in ("a@b.com", "-x", "with space", ""):
        resp = admin.post("/api/users", json={"email": bad, "is_service": True})
        assert resp.status_code == 422, bad

    # Only admins make tokens for other users, and only from a session.
    url = f"/api/users/{robot_user['id']}/tokens"
    assert dev.post(url, json={"name": "ci"}).status_code == 403
    assert (
        admin.post("/api/users/9999/tokens", json={"name": "ci"}).status_code
        == 404
    )
    created = admin.post(url, json={"name": "ci"}).json()
    robot = bearer(app, created["token"])
    assert robot.post(url, json={"name": "more"}).status_code == 403
    assert [t["name"] for t in admin.get(url).json()] == ["ci"]
    assert dev.get(url).status_code == 403

    resp = robot.post(
        "/api/builds",
        files={"package": ("proj.zip", package(), "application/zip")},
    )
    assert resp.json()["created_by"] == "ci-nightly"
    assert robot.get("/api/tokens").json()[0]["name"] == "ci"

    # A service account can never sign in with Google.
    client = testclient.TestClient(app)
    sign_in(client, "ci-nightly")
    assert client.get("/api/me").status_code == 401


def test_web_page_token_management(app, dev):
    page = dev.get("/").text
    assert "API tokens" in page and "No API tokens." in page

    resp = dev.post(
        "/ui/tokens", data={"name": "laptop", "expires_in_days": ""}
    )
    assert resp.status_code == 200
    token = dev.get("/api/tokens").json()[0]
    assert token["expires_at"] is None
    assert "will not be shown again" in resp.text and "bst_" in resp.text

    # The token is shown only once.
    listing = dev.get("/ui/tokens").text
    assert "will not be shown again" not in listing
    assert f"bst_{token['prefix']}" in listing

    error = dev.post("/ui/tokens", data={"name": " ", "expires_in_days": "30"})
    assert error.status_code == 200 and 'class="error"' in error.text

    resp = dev.post(f"/ui/tokens/{token['id']}/revoke")
    assert resp.status_code == 200 and "No API tokens." in resp.text
