"""Tests of GitHub linking, connected repositories and builds from them.

GitHub's API is faked with an httpx mock transport; clones come from local
bare repositories standing in for github.com.
"""

import base64
import dataclasses
import io
import json
import pathlib
import subprocess
import sys
import zipfile

from fastapi import testclient
import httpx
import pytest

from buildserver import api
from buildserver import config
from buildserver import db
from buildserver import git
from buildserver import github
from buildserver import storage
from buildserver import worker

ADMIN = "admin@example.com"
DEV = "dev@team.example.com"
OTHER = "other@team.example.com"
TOKENS = {"gho_admin": (1, "boss"), "gho_dev": (2, "octo"), "gho_b": (3, "b")}

# Stand-in for build.tcl + Vivado: records which commit and submodule it saw.
BUILD_SCRIPT = """
import pathlib, sys
name = sys.argv[1]
out = pathlib.Path("bitfiles/generated") / name
out.mkdir(parents=True)
(out / "marker.txt").write_text(pathlib.Path("marker.txt").read_text())
(out / "sub.txt").write_text(pathlib.Path("lib/sub.txt").read_text())
"""


def run_git(cwd: pathlib.Path, *args: str) -> str:
    """Runs git in a directory and returns its output."""
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "protocol.file.allow=always",
            "-c",
            "init.defaultBranch=main",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@dataclasses.dataclass
class Remote:
    """Local repositories standing in for github.com/o/r and its submodule."""

    root: pathlib.Path
    work: pathlib.Path
    commits: dict[str, str]

    def commit(self, marker: str) -> str:
        """Commits a new marker on the checked-out branch and pushes it."""
        (self.work / "marker.txt").write_text(marker)
        run_git(self.work, "commit", "-qam", marker)
        run_git(self.work, "push", "-q", "origin", "HEAD")
        return run_git(self.work, "rev-parse", "HEAD")


@pytest.fixture(scope="session")
def session_remote(tmp_path_factory) -> Remote:
    """Creates o/r with branches main and dev, tag v1.0 and a submodule.

    Creating repositories is slow, and no test changes them, so they are
    shared by all tests.
    """
    tmp_path = tmp_path_factory.mktemp("remote")
    root = tmp_path / "github"
    for name in ("r", "sub"):
        (root / "o").mkdir(parents=True, exist_ok=True)
        run_git(root / "o", "init", "-q", "--bare", f"{name}.git")

    sub = tmp_path / "sub-work"
    run_git(
        tmp_path, "clone", "-q", (root / "o" / "sub.git").as_uri(), str(sub)
    )
    (sub / "sub.txt").write_text("submodule content")
    run_git(sub, "add", ".")
    run_git(sub, "commit", "-qm", "sub")
    run_git(sub, "push", "-q", "origin", "HEAD:main")

    work = tmp_path / "r-work"
    run_git(tmp_path, "clone", "-q", (root / "o" / "r.git").as_uri(), str(work))
    run_git(work, "symbolic-ref", "HEAD", "refs/heads/main")
    (work / "build.py").write_text(BUILD_SCRIPT)
    (work / "marker.txt").write_text("main-v1")
    run_git(
        work, "submodule", "add", "-q", (root / "o" / "sub.git").as_uri(), "lib"
    )
    run_git(work, "add", ".")
    run_git(work, "commit", "-qm", "v1")
    run_git(work, "tag", "v1.0")
    run_git(work, "push", "-q", "origin", "main", "v1.0")
    commits = {"v1.0": run_git(work, "rev-parse", "HEAD")}
    remote = Remote(root, work, commits)
    commits["main"] = remote.commit("main-v2")
    run_git(work, "checkout", "-q", "-b", "dev")
    commits["dev"] = remote.commit("dev")
    run_git(work, "checkout", "-q", "main")
    return remote


@pytest.fixture
def remote(session_remote, monkeypatch) -> Remote:
    """Makes clones come from the local stand-in for github.com."""
    monkeypatch.setattr(git, "GITHUB_URL", session_remote.root.as_uri())
    monkeypatch.setattr(git, "ALLOWED_PROTOCOLS", ("file", "https"))
    return session_remote


@dataclasses.dataclass
class FakeGitHub:
    """State of the fake GitHub API."""

    branches: list[str]
    tags: list[str]
    revoked: list[str]


def fake_github_client(state: FakeGitHub) -> httpx.Client:
    """Returns an API client whose requests are answered from state."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            assert request.url.path == "/applications/gh-id/grant"
            state.revoked.append(json.loads(request.content)["access_token"])
            return httpx.Response(204)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in TOKENS:
            return httpx.Response(401, json={"message": "Bad credentials"})
        path = request.url.path.lower()  # GitHub ignores case in names.
        page = int(request.url.params.get("page", "1"))
        if path == "/user":
            github_id, login = TOKENS[token]
            return httpx.Response(200, json={"id": github_id, "login": login})
        if path == "/user/repos":
            repos = [
                {"full_name": "o/r", "private": True, "description": "fw"},
                {"full_name": "o/sub", "private": False},
            ]
            return httpx.Response(200, json=repos if page == 1 else [])
        if path == "/repos/o/r":
            return httpx.Response(
                200,
                json={
                    "full_name": "o/r",
                    "default_branch": "main",
                    "permissions": {"pull": True},
                },
            )
        if path == "/repos/o/r/branches":
            items = [{"name": b} for b in state.branches]
            return httpx.Response(200, json=items if page == 1 else [])
        if path == "/repos/o/r/tags":
            items = [{"name": t} for t in state.tags]
            return httpx.Response(200, json=items if page == 1 else [])
        return httpx.Response(404, json={"message": "Not Found"})

    return httpx.Client(
        base_url=github.API_URL, transport=httpx.MockTransport(handle)
    )


@pytest.fixture
def env(tmp_path, auth_settings, remote):
    """Provides settings, records, storage, the app and the fake GitHub."""
    del remote  # Only needed to exist.
    settings = config.Settings(
        data_dir=tmp_path / "data",
        build_command=[sys.executable, "build.py", "{name}"],
        build_script="build.py",
        artifact_dir="bitfiles/generated/{name}",
        keep_workdir=True,
    )
    auth_settings = dataclasses.replace(
        auth_settings, github_client_id="gh-id", github_client_secret="gh-sec"
    )
    app = api.create_app(settings, auth_settings)
    state = FakeGitHub(["main", "dev"], ["v1.0"], [])
    app.state.github_http = fake_github_client(state)
    store = storage.Storage(settings.data_dir)
    return settings, db.Database(store.db_path), store, app, state


def link_github(client: testclient.TestClient, token: str) -> httpx.Response:
    """Completes the GitHub callback as if GitHub had issued a token."""

    async def fake_token(request):
        del request  # Unused.
        return {"access_token": token, "scope": "repo", "token_type": "bearer"}

    client.app.state.oauth.github.authorize_access_token = fake_token
    return client.get("/auth/github/callback", follow_redirects=False)


@pytest.fixture
def dev(env, sign_in):
    """Provides a client signed in as a plain user with GitHub linked."""
    client = testclient.TestClient(env[3])
    sign_in(client, DEV)
    assert link_github(client, "gho_dev").status_code == 303
    return client


@pytest.fixture
def admin(env, sign_in):
    """Provides a client signed in as the administrator, GitHub not linked."""
    client = testclient.TestClient(env[3])
    sign_in(client, ADMIN)
    return client


def add_repo(client, repo="o/r", **body):
    return client.post("/api/repos", json={"repo": repo, **body})


def artifact_files(client, build_id) -> dict[str, str]:
    resp = client.get(f"/api/builds/{build_id}/artifacts")
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        return {n: zf.read(n).decode() for n in zf.namelist()}


# Linking ####################################################################


def test_linking_shows_the_github_login(dev):
    assert dev.get("/api/me").json()["github_login"] == "octo"
    assert "@octo" in dev.get("/").text


def test_linking_needs_a_session_not_a_token(dev):
    token = dev.post("/api/tokens", json={"name": "t"}).json()["token"]
    resp = dev.get(
        "/auth/github/login",
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_github_account_cannot_be_linked_to_two_users(env, dev, sign_in):
    other = testclient.TestClient(env[3])
    sign_in(other, OTHER)
    link_github(other, "gho_dev")
    page = other.get("/").text
    assert "already linked to another user" in page
    assert other.get("/api/me").json()["github_login"] is None


def test_unlink_revokes_the_grant(env, dev):
    state = env[4]
    assert dev.post("/auth/github/unlink", follow_redirects=False).status_code
    assert state.revoked == ["gho_dev"]
    assert dev.get("/api/me").json()["github_login"] is None


# Adding repositories ########################################################


def test_adding_needs_a_linked_account(admin):
    resp = add_repo(admin)
    assert resp.status_code == 400
    assert "link a GitHub account" in resp.json()["detail"]


@pytest.mark.parametrize(
    "text",
    [
        "o/r",
        "https://github.com/o/r",
        "https://github.com/o/r.git",
        "git@github.com:o/r.git",
    ],
)
def test_add_by_url_or_name_selects_default_branch(dev, text):
    resp = add_repo(dev, text)
    assert resp.status_code == 201
    repo = resp.json()
    assert repo["full_name"] == "o/r" and repo["name"] == "r"
    assert (repo["ref"], repo["ref_type"]) == ("main", "branch")
    assert repo["branches"] == ["main", "dev"] and repo["tags"] == ["v1.0"]


def test_duplicate_repository_conflicts(dev):
    assert add_repo(dev).status_code == 201
    assert add_repo(dev, "https://github.com/O/R").status_code == 409


@pytest.mark.parametrize(
    "text",
    ["--upload-pack=evil", "ext::sh -c evil", "file:///c/x", "C:\\repo", "o"],
)
def test_invalid_repository_is_refused(dev, text):
    assert add_repo(dev, text).status_code == 422


def test_unknown_repository_is_refused(dev):
    resp = add_repo(dev, "o/missing")
    assert resp.status_code == 422
    assert "not found" in resp.json()["detail"]


def test_picker_lists_readable_repositories(dev):
    assert [r["full_name"] for r in dev.get("/api/github/repos").json()] == [
        "o/r",
        "o/sub",
    ]
    assert [
        r["full_name"] for r in dev.get("/api/github/repos?q=SUB").json()
    ] == ["o/sub"]
    add_repo(dev)
    html = dev.get("/ui/github/repos").text
    assert '"repo": "o/r"' not in html and '"repo": "o/sub"' in html
    assert "added" in html


# Managing repositories ######################################################


def test_select_tag_and_reject_unknown_refs(dev):
    repo_id = add_repo(dev).json()["id"]
    resp = dev.patch(
        f"/api/repos/{repo_id}", json={"ref": "v1.0", "ref_type": "tag"}
    )
    assert resp.status_code == 200
    assert (resp.json()["ref"], resp.json()["ref_type"]) == ("v1.0", "tag")
    for body in (
        {"ref": "nope"},
        {"ref": "main", "ref_type": "tag"},
        {"ref": "-x"},
    ):
        assert dev.patch(f"/api/repos/{repo_id}", json=body).status_code == 422


def test_repositories_are_per_user_but_admins_see_all(env, dev, admin, sign_in):
    repo_id = add_repo(dev).json()["id"]
    other = testclient.TestClient(env[3])
    sign_in(other, OTHER)
    assert other.get("/api/repos").json() == []
    assert other.get(f"/api/repos/{repo_id}").status_code == 404
    assert other.post(f"/api/repos/{repo_id}/builds").status_code == 404
    assert [r["id"] for r in admin.get("/api/repos").json()] == [repo_id]
    assert admin.post(f"/api/repos/{repo_id}/builds").status_code == 201
    assert DEV in admin.get("/ui/repos").text


def test_update_fetches_refs_and_falls_back_to_default(env, dev):
    state = env[4]
    repo_id = add_repo(dev).json()["id"]
    dev.patch(f"/api/repos/{repo_id}", json={"ref": "dev"})
    state.branches = ["main", "feature"]
    state.tags = ["v2.0", "v1.0"]
    repo = dev.post(f"/api/repos/{repo_id}/fetch").json()
    assert repo["branches"] == ["main", "feature"]
    assert repo["tags"] == ["v2.0", "v1.0"]
    assert repo["ref"] == "main"


def test_delete_keeps_builds(dev):
    repo_id = add_repo(dev).json()["id"]
    build = dev.post(f"/api/repos/{repo_id}/builds").json()
    assert dev.delete(f"/api/repos/{repo_id}").status_code == 204
    assert dev.get("/api/repos").json() == []
    assert dev.get(f"/api/builds/{build['id']}").status_code == 200


# Builds #####################################################################


def test_build_from_branch_clones_with_submodules(env, dev, remote):
    settings, database, store, _, _ = env
    repo_id = add_repo(dev).json()["id"]
    resp = dev.post(f"/api/repos/{repo_id}/builds", json={"ref": "dev"})
    assert resp.status_code == 201
    build = resp.json()
    assert build["repo_full_name"] == "o/r" and build["ref"] == "dev"
    assert build["package_filename"] == "" and build["created_by"] == DEV

    assert worker.run_once(settings, database, store)
    build = dev.get(f"/api/builds/{build['id']}").json()
    assert build["status"] == "succeeded", build["error"]
    assert build["commit_sha"] == remote.commits["dev"]
    files = artifact_files(dev, build["id"])
    assert files["marker.txt"] == "dev"
    assert files["sub.txt"] == "submodule content"


def test_build_from_tag_checks_out_the_tagged_commit(env, dev, remote):
    settings, database, store, _, _ = env
    repo_id = add_repo(dev).json()["id"]
    dev.patch(f"/api/repos/{repo_id}", json={"ref": "v1.0", "ref_type": "tag"})
    build = dev.post(f"/api/repos/{repo_id}/builds").json()
    assert (build["ref"], build["ref_type"]) == ("v1.0", "tag")
    worker.run_once(settings, database, store)
    build = dev.get(f"/api/builds/{build['id']}").json()
    assert build["status"] == "succeeded", build["error"]
    assert build["commit_sha"] == remote.commits["v1.0"]
    assert artifact_files(dev, build["id"])["marker.txt"] == "main-v1"


def test_token_is_kept_out_of_log_and_clone(env, dev):
    settings, database, store, _, _ = env
    repo_id = add_repo(dev).json()["id"]
    build = dev.post(f"/api/repos/{repo_id}/builds").json()
    worker.run_once(settings, database, store)
    secrets = ("gho_dev", base64.b64encode(b"x-access-token:gho_dev").decode())
    log = dev.get(f"/api/builds/{build['id']}/log?tail=0").text
    git_config = (store.work_dir(build["id"]) / ".git" / "config").read_text()
    for secret in secrets:
        assert secret not in log and secret not in git_config


def test_build_fails_when_owner_unlinked(env, dev):
    settings, database, store, _, _ = env
    repo_id = add_repo(dev).json()["id"]
    build = dev.post(f"/api/repos/{repo_id}/builds").json()
    dev.post("/auth/github/unlink")
    worker.run_once(settings, database, store)
    build = dev.get(f"/api/builds/{build['id']}").json()
    assert build["status"] == "failed"
    assert build["error"] == f"GitHub account of {DEV} is not linked"


def test_token_user_can_build_own_repository(env, dev):
    repo_id = add_repo(dev).json()["id"]
    token = dev.post("/api/tokens", json={"name": "ci"}).json()["token"]
    robot = testclient.TestClient(
        env[3], headers={"Authorization": f"Bearer {token}"}
    )
    assert robot.post(f"/api/repos/{repo_id}/builds").status_code == 201


# Front end ##################################################################


def test_ui_add_select_build_and_delete(dev):
    resp = dev.post("/ui/repos", data={"repo": "https://github.com/o/r"})
    assert resp.status_code == 200 and "Connected o/r" in resp.text
    repo_id = dev.get("/api/repos").json()[0]["id"]

    resp = dev.post(f"/ui/repos/{repo_id}/ref", data={"ref": "tag:v1.0"})
    assert 'value="tag:v1.0" selected' in resp.text

    resp = dev.post(f"/ui/repos/{repo_id}/build")
    assert resp.headers["HX-Trigger"] == "refresh"
    assert "Queued r from o/r v1.0" in resp.text
    assert "o/r" in dev.get("/ui/builds").text

    resp = dev.post(f"/ui/repos/{repo_id}/fetch")
    assert "Updated o/r: 2 branches, 1 tags" in resp.text

    resp = dev.post(f"/ui/repos/{repo_id}/delete")
    assert "Removed o/r" in resp.text and "No repositories" in resp.text


def test_ui_reports_errors_inside_the_section(dev):
    resp = dev.post("/ui/repos", data={"repo": "not a repo"})
    assert resp.status_code == 200
    assert 'class="error"' in resp.text and 'value="not a repo"' in resp.text
    resp = dev.post("/ui/repos/999/build")
    assert resp.status_code == 200 and "repository not found" in resp.text
