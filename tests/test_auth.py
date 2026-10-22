"""Tests of Google sign-in, sessions and the user management API.

Google itself is faked: the sign_in fixture answers the token request, and
everything from the callback on runs for real.
"""

import os
import sys

from fastapi import testclient
import pytest

from buildserver import api
from buildserver import config

ADMIN = "admin@example.com"


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


def test_everything_but_the_sign_in_page_needs_a_session(app):
    client = testclient.TestClient(app)
    page = client.get("/")
    assert page.status_code == 200 and "Sign in with Google" in page.text
    for path in ("/api/builds", "/ui/builds", "/api/me", "/api/users"):
        assert client.get(path).status_code == 401, path
    assert client.get("/static/style.css").status_code == 200


def test_configured_admin_is_created_on_first_sign_in(admin):
    resp = admin.get("/api/me")
    assert resp.status_code == 200
    me = resp.json()
    assert me["email"] == ADMIN and me["role"] == "admin"
    assert me["name"] == "Admin" and me["last_login_at"]
    assert "Sign out" in admin.get("/").text


def test_unknown_account_is_refused(app, sign_in):
    client = testclient.TestClient(app)
    resp = sign_in(client, "stranger@gmail.com")
    assert resp.headers["location"] == "http://testserver/"
    assert client.get("/api/me").status_code == 401
    assert "stranger@gmail.com is not authorised" in client.get("/").text
    assert "not authorised" not in client.get("/").text  # shown once


def test_unverified_email_is_refused(app, sign_in):
    client = testclient.TestClient(app)
    sign_in(client, ADMIN, verified=False)
    assert client.get("/api/me").status_code == 401


def test_allowed_domain_signs_in_as_plain_user(app, sign_in):
    client = testclient.TestClient(app)
    sign_in(client, "dev@team.example.com")
    assert client.get("/api/me").json()["role"] == "user"
    assert client.get("/api/builds").status_code == 200
    assert client.get("/api/users").status_code == 403
    assert (
        client.post("/api/users", json={"email": "x@y.com"}).status_code == 403
    )


def test_logout_ends_the_session(admin):
    resp = admin.post("/auth/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert admin.get("/api/me").status_code == 401


def test_user_management(app, admin, sign_in):
    resp = admin.post("/api/users", json={"email": " Dev@Gmail.com "})
    assert resp.status_code == 201
    dev = resp.json()
    assert dev["email"] == "dev@gmail.com" and dev["role"] == "user"
    assert dev["is_active"] is True and dev["last_login_at"] is None

    duplicate = admin.post("/api/users", json={"email": "dev@gmail.com"})
    assert duplicate.status_code == 409
    assert admin.post("/api/users", json={"email": "nope"}).status_code == 422
    bad_role = admin.post("/api/users", json={"email": "a@b.com", "role": "x"})
    assert bad_role.status_code == 422

    emails = [user["email"] for user in admin.get("/api/users").json()]
    assert emails == [ADMIN, "dev@gmail.com"]
    assert admin.get(f"/api/users/{dev['id']}").json()["email"] == dev["email"]
    assert admin.get("/api/users/9999").status_code == 404

    # The added account can now sign in.
    dev_client = testclient.TestClient(app)
    sign_in(dev_client, "dev@gmail.com")
    assert dev_client.get("/api/me").json()["id"] == dev["id"]

    promoted = admin.patch(f"/api/users/{dev['id']}", json={"role": "admin"})
    assert promoted.json()["role"] == "admin"
    assert dev_client.get("/api/users").status_code == 200

    # Deactivating ends the user's session and blocks signing in again.
    resp = admin.patch(f"/api/users/{dev['id']}", json={"is_active": False})
    assert resp.json()["is_active"] is False
    assert dev_client.get("/api/me").status_code == 401
    sign_in(dev_client, "dev@gmail.com")
    assert dev_client.get("/api/me").status_code == 401

    assert admin.delete(f"/api/users/{dev['id']}").status_code == 204
    assert admin.get(f"/api/users/{dev['id']}").status_code == 404
    assert admin.delete(f"/api/users/{dev['id']}").status_code == 404


def test_admin_cannot_lock_themselves_out(admin):
    me = admin.get("/api/me").json()
    demote = admin.patch(f"/api/users/{me['id']}", json={"role": "user"})
    assert demote.status_code == 409
    deactivate = admin.patch(
        f"/api/users/{me['id']}", json={"is_active": False}
    )
    assert deactivate.status_code == 409
    assert admin.delete(f"/api/users/{me['id']}").status_code == 409
    assert admin.get("/api/me").json()["role"] == "admin"


def test_configured_admin_is_restored_on_sign_in(app, admin, sign_in):
    other = admin.post(
        "/api/users", json={"email": "boss@example.com", "role": "admin"}
    ).json()
    boss = testclient.TestClient(app)
    sign_in(boss, "boss@example.com")
    me = admin.get("/api/me").json()
    boss.patch(f"/api/users/{me['id']}", json={"is_active": False})
    assert admin.get("/api/me").status_code == 401

    # Signing in again restores the configured administrator.
    sign_in(admin, ADMIN)
    assert admin.get("/api/me").json()["role"] == "admin"
    assert other["role"] == "admin"


def test_auth_settings_come_from_env_file(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "BUILDSERVER_PUBLIC_URL=https://builds.example.com/\n"
        "BUILDSERVER_GOOGLE_CLIENT_ID=id\n"
        "BUILDSERVER_GOOGLE_CLIENT_SECRET=secret\n"
        "BUILDSERVER_SESSION_SECRET=key\n"
        "BUILDSERVER_ADMIN_EMAILS=A@example.com, b@example.com\n"
    )
    auth_settings = config.load_auth_settings()
    assert auth_settings.public_url == "https://builds.example.com"
    assert auth_settings.admin_emails == {"a@example.com", "b@example.com"}
    assert auth_settings.allowed_domains == frozenset()

    (tmp_path / ".env").write_text("BUILDSERVER_PUBLIC_URL=http://x\n")
    monkeypatch.setattr(os, "environ", {})
    with pytest.raises(ValueError, match="BUILDSERVER_GOOGLE_CLIENT_ID"):
        config.load_auth_settings()
