"""GitHub repositories users connect to build from.

A user who linked a GitHub account (see buildserver.auth) connects
repositories of it, selects a branch or tag of each, and queues builds of
them. The worker clones the selected ref with the token of the repository's
owner. Users see their own repositories; administrators see everybody's.

The rules (connect, refresh, select a ref, queue a build) are plain
functions that raise NotLinkedError, AlreadyConnectedError, ValueError or
github.GitHubError. The routes translate those into HTTP responses.

Routes:

    /api/repos...           JSON API
    /api/github/repos       repositories the linked account can read
    /ui/repos..., /ui/github/repos   the HTML fragments of the page
"""

from collections.abc import Callable
from collections.abc import Iterator
import contextlib
import sqlite3
from typing import Any
from typing import Literal
import uuid

import fastapi
from fastapi import responses
from fastapi import templating
import httpx
import pydantic

from buildserver import auth
from buildserver import config
from buildserver import db
from buildserver import git
from buildserver import github
from buildserver import names
from buildserver import storage

RefType = Literal["branch", "tag"]


class NotLinkedError(Exception):
    """Raised when a user has no linked GitHub account."""


class AlreadyConnectedError(Exception):
    """Raised when a user connects a repository a second time."""


class RepoCreate(pydantic.BaseModel):
    """Body of a request that connects a repository.

    Attributes:
        repo: The repository: a GitHub URL or "owner/repo".
        name: Name shown on the page and used as the default build name.
            Derived from the repository name if omitted.
    """

    repo: str
    name: str | None = None


class RefSelect(pydantic.BaseModel):
    """Body of a request that selects the branch or tag to build.

    Attributes:
        ref: Name of the branch or tag.
        ref_type: Whether ref is a branch or a tag.
    """

    ref: str
    ref_type: RefType = db.REF_BRANCH


class RepoBuild(pydantic.BaseModel):
    """Body of a request that queues a build of a repository.

    Attributes:
        name: Build name. The repository's name if omitted.
        ref: Branch or tag to build. The selected one if omitted.
        ref_type: Whether ref is a branch or a tag.
    """

    name: str | None = None
    ref: str | None = None
    ref_type: RefType = db.REF_BRANCH


def present_repo(repo: db.Repo) -> dict[str, Any]:
    """Returns a repository record as it is shown to clients."""
    return {
        "id": repo["id"],
        "user_id": repo["user_id"],
        "full_name": repo["full_name"],
        "name": repo["name"],
        "ref": repo["ref"],
        "ref_type": repo["ref_type"],
        "default_branch": repo["default_branch"],
        "branches": repo["branches"],
        "tags": repo["tags"],
        "fetched_at": repo["fetched_at"],
        "created_at": repo["created_at"],
    }


def visible_repos(database: db.Database, user: db.User) -> list[db.Repo]:
    """Lists the repositories a user sees: their own, or all for an admin."""
    return database.list_repos(None if auth.is_admin(user) else user["id"])


def page_context(
    database: db.Database,
    auth_settings: config.AuthSettings,
    user: db.User,
) -> dict[str, Any]:
    """Returns what the repositories section of the page is rendered from.

    Args:
        database: The records.
        auth_settings: The sign-in settings.
        user: The signed-in user.

    Returns:
        Template variables: repos (each with the owner's email as "owner"),
        github_enabled, github_link and show_owner.
    """
    repos = visible_repos(database, user)
    emails = {}
    for repo in repos:
        if repo["user_id"] not in emails:
            owner = database.get_user(repo["user_id"])
            emails[repo["user_id"]] = owner["email"] if owner else "?"
        repo["owner"] = emails[repo["user_id"]]
    return {
        "repos": repos,
        "github_enabled": auth_settings.github_enabled,
        "github_link": database.get_github_link(user["id"]),
        "show_owner": any(repo["user_id"] != user["id"] for repo in repos),
    }


# Rules #######################################################################


def token_of(database: db.Database, user_id: int) -> str:
    """Returns the GitHub token of a user.

    Args:
        database: The records.
        user_id: Id of the user.

    Returns:
        The access token of the user's linked GitHub account.

    Raises:
        NotLinkedError: If the user has not linked a GitHub account.
    """
    link = database.get_github_link(user_id)
    if link is None:
        raise NotLinkedError("link a GitHub account first")
    return link["access_token"]


def fetch_refs(
    http: httpx.Client, token: str, full_name: str
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Fetches a repository with its branches and tags from GitHub.

    Args:
        http: The GitHub API client.
        token: GitHub token to read the repository with.
        full_name: The repository, "owner/repo".

    Returns:
        The repository as github.get_repo() returns it, its branch names and
        its tag names.

    Raises:
        github.GitHubError: If a GitHub request fails.
    """
    info = github.get_repo(http, token, full_name)
    branches = github.list_branches(http, token, info["full_name"])
    tags = github.list_tags(http, token, info["full_name"])
    return info, branches, tags


def connect_repo(
    database: db.Database,
    http: httpx.Client,
    user: db.User,
    text: str,
    name: str | None,
) -> db.Repo:
    """Connects a repository of a user's linked GitHub account.

    Args:
        database: The records.
        http: The GitHub API client.
        user: The user connecting the repository.
        text: The repository: a GitHub URL or "owner/repo".
        name: Display name, or None to derive it from the repository name.

    Returns:
        The new repository record.

    Raises:
        NotLinkedError: If the user has not linked a GitHub account.
        ValueError: If the repository or the name is not valid.
        github.GitHubError: If GitHub refuses or fails a request.
        AlreadyConnectedError: If the user already connected the repository.
    """
    token = token_of(database, user["id"])
    full_name = github.parse_repo(text)
    info, branches, tags = fetch_refs(http, token, full_name)
    name = (name or "").strip() or names.clean(
        info["full_name"].partition("/")[2]
    )
    if not names.NAME_RE.fullmatch(name):
        raise ValueError(names.NAME_ERROR)
    try:
        return database.create_repo(
            user["id"],
            info["full_name"],
            name,
            info["default_branch"],
            branches,
            tags,
        )
    except sqlite3.IntegrityError as e:
        raise AlreadyConnectedError(
            f"{info['full_name']} is already connected"
        ) from e


def refresh_refs(
    database: db.Database, http: httpx.Client, repo: db.Repo
) -> db.Repo:
    """Refetches a repository's branches and tags, with the owner's token.

    The selected ref is kept if it still exists; otherwise the default
    branch is selected.

    Args:
        database: The records.
        http: The GitHub API client.
        repo: The repository.

    Returns:
        The updated repository record.

    Raises:
        NotLinkedError: If the owner's GitHub account is not linked.
        github.GitHubError: If GitHub refuses or fails a request.
    """
    info, branches, tags = fetch_refs(
        http, token_of(database, repo["user_id"]), repo["full_name"]
    )
    database.update_refs(repo["id"], info["default_branch"], branches, tags)
    refs = branches if repo["ref_type"] == db.REF_BRANCH else tags
    if repo["ref"] not in refs:
        database.select_ref(repo["id"], info["default_branch"], db.REF_BRANCH)
    return database.get_repo(repo["id"])


def check_ref(repo: db.Repo, ref: str, ref_type: str) -> None:
    """Checks that ref is a known branch or tag of a repository.

    Args:
        repo: The repository.
        ref: Name of the branch or tag.
        ref_type: Whether ref is a branch or a tag.

    Raises:
        ValueError: If the repository has no such branch or tag.
    """
    refs = repo["branches"] if ref_type == db.REF_BRANCH else repo["tags"]
    if not git.valid_ref(ref) or ref not in refs:
        raise ValueError(f"{repo['full_name']} has no {ref_type} {ref!r}")


def select_ref(
    database: db.Database, repo: db.Repo, ref: str, ref_type: str
) -> db.Repo:
    """Selects the branch or tag a repository is built from.

    Args:
        database: The records.
        repo: The repository.
        ref: Name of the branch or tag.
        ref_type: Whether ref is a branch or a tag.

    Returns:
        The updated repository record.

    Raises:
        ValueError: If the repository has no such branch or tag.
    """
    check_ref(repo, ref, ref_type)
    database.select_ref(repo["id"], ref, ref_type)
    return database.get_repo(repo["id"])


def queue_build(
    database: db.Database,
    store: storage.Storage,
    repo: db.Repo,
    user: db.User,
    name: str | None,
    ref: tuple[str, str] | None,
) -> db.Build:
    """Queues a build of a repository's branch or tag.

    Args:
        database: The records.
        store: The data directory.
        repo: The repository.
        user: The user queueing the build.
        name: Build name, or None for the repository's name.
        ref: The branch or tag as (name, ref_type), or None for the
            selected one.

    Returns:
        The new build record.

    Raises:
        ValueError: If the name or ref is not valid.
    """
    ref_name, ref_type = ref or (repo["ref"], repo["ref_type"])
    check_ref(repo, ref_name, ref_type)
    name = (name or "").strip() or repo["name"]
    if not names.NAME_RE.fullmatch(name):
        raise ValueError(names.NAME_ERROR)
    build_id = uuid.uuid4().hex[:12]
    store.build_dir(build_id).mkdir(parents=True)
    return database.create(
        build_id,
        name,
        "",
        created_by=user["email"],
        repo_full_name=repo["full_name"],
        ref=ref_name,
        ref_type=ref_type,
        source_user_id=repo["user_id"],
    )


# HTTP ########################################################################


def _http_error(e: Exception) -> fastapi.HTTPException:
    """Translates an error of the rules into the response to the client."""
    if isinstance(e, github.GitHubError):
        if e.status == 401:
            return fastapi.HTTPException(400, e.message)
        if e.status in (403, 404):
            return fastapi.HTTPException(422, e.message)
        return fastapi.HTTPException(502, e.message)
    if isinstance(e, NotLinkedError):
        return fastapi.HTTPException(400, str(e))
    if isinstance(e, AlreadyConnectedError):
        return fastapi.HTTPException(409, str(e))
    return fastapi.HTTPException(422, str(e))


@contextlib.contextmanager
def _http_errors() -> Iterator[None]:
    """Turns the errors the rules raise into HTTP exceptions.

    Raises:
        fastapi.HTTPException: For NotLinkedError (400), ValueError (422),
            AlreadyConnectedError (409) and github.GitHubError (400, 422 or
            502).
    """
    try:
        yield
    except (
        NotLinkedError,
        AlreadyConnectedError,
        ValueError,
        github.GitHubError,
    ) as e:
        raise _http_error(e) from e


def create_router(
    database: db.Database,
    store: storage.Storage,
    auth_settings: config.AuthSettings,
    templates: templating.Jinja2Templates,
    present_build: Callable[[db.Build], db.Build],
) -> fastapi.APIRouter:
    """Creates the repository routes.

    The GitHub API client is taken from app.state.github_http at request
    time.

    Args:
        database: The records.
        store: The data directory.
        auth_settings: The sign-in settings.
        templates: The front end templates.
        present_build: Turns a build record into what clients are shown.

    Returns:
        The router, to be included in the application.
    """
    router = fastapi.APIRouter(
        dependencies=[fastapi.Depends(auth.current_user)]
    )

    def require_github() -> None:
        """Raises a 503 if no GitHub OAuth App is configured."""
        if not auth_settings.github_enabled:
            raise fastapi.HTTPException(503, "GitHub is not configured")

    def http_of(request: fastapi.Request) -> httpx.Client:
        """Returns the GitHub API client of the application."""
        return request.app.state.github_http

    def get_owned_or_404(repo_id: int, user: db.User) -> db.Repo:
        """Returns a repository the user may use, or raises a 404."""
        repo = database.get_repo(repo_id)
        if repo is None or (
            repo["user_id"] != user["id"] and not auth.is_admin(user)
        ):
            raise fastapi.HTTPException(404, "repository not found")
        return repo

    def add_repo(
        request: fastapi.Request, user: db.User, text: str, name: str | None
    ) -> db.Repo:
        """Connects a repository; see connect_repo()."""
        require_github()
        with _http_errors():
            return connect_repo(database, http_of(request), user, text, name)

    def update_repo(request: fastapi.Request, repo: db.Repo) -> db.Repo:
        """Refetches a repository's branches and tags; see refresh_refs()."""
        require_github()
        with _http_errors():
            return refresh_refs(database, http_of(request), repo)

    # JSON API ################################################################

    @router.get("/api/repos")
    def list_repos(
        user: db.User = fastapi.Depends(auth.current_user),
    ) -> list[dict[str, Any]]:
        """Lists your repositories, or everybody's for an admin."""
        return [present_repo(r) for r in visible_repos(database, user)]

    @router.get("/api/github/repos")
    def list_github_repos(
        request: fastapi.Request,
        q: str = "",
        user: db.User = fastapi.Depends(auth.current_user),
    ) -> list[dict[str, Any]]:
        """Lists repositories the linked GitHub account can read."""
        require_github()
        with _http_errors():
            return github.list_user_repos(
                http_of(request), token_of(database, user["id"]), q
            )

    @router.post("/api/repos", status_code=201)
    def create_repo(
        request: fastapi.Request,
        body: RepoCreate,
        user: db.User = fastapi.Depends(auth.current_user),
    ) -> dict[str, Any]:
        """Connects a repository of the linked GitHub account."""
        return present_repo(add_repo(request, user, body.repo, body.name))

    @router.get("/api/repos/{repo_id}")
    def get_repo(
        repo_id: int,
        user: db.User = fastapi.Depends(auth.current_user),
    ) -> dict[str, Any]:
        """Returns one repository."""
        return present_repo(get_owned_or_404(repo_id, user))

    @router.post("/api/repos/{repo_id}/fetch")
    def fetch_repo(
        request: fastapi.Request,
        repo_id: int,
        user: db.User = fastapi.Depends(auth.current_user),
    ) -> dict[str, Any]:
        """Refetches the branches and tags of a repository from GitHub."""
        repo = get_owned_or_404(repo_id, user)
        return present_repo(update_repo(request, repo))

    @router.patch("/api/repos/{repo_id}")
    def patch_repo(
        repo_id: int,
        body: RefSelect,
        user: db.User = fastapi.Depends(auth.current_user),
    ) -> dict[str, Any]:
        """Selects the branch or tag a repository is built from."""
        repo = get_owned_or_404(repo_id, user)
        with _http_errors():
            return present_repo(
                select_ref(database, repo, body.ref, body.ref_type)
            )

    @router.delete("/api/repos/{repo_id}", status_code=204)
    def delete_repo(
        repo_id: int,
        user: db.User = fastapi.Depends(auth.current_user),
    ) -> None:
        """Disconnects a repository and cancels its queued builds."""
        database.delete_repo(get_owned_or_404(repo_id, user)["id"])

    @router.post("/api/repos/{repo_id}/builds", status_code=201)
    def build_repo(
        repo_id: int,
        body: RepoBuild | None = None,
        user: db.User = fastapi.Depends(auth.current_user),
    ) -> db.Build:
        """Queues a build of a repository's selected or given branch or tag."""
        body = body or RepoBuild()
        repo = get_owned_or_404(repo_id, user)
        ref = None if body.ref is None else (body.ref, body.ref_type)
        with _http_errors():
            build = queue_build(database, store, repo, user, body.name, ref)
        return present_build(build)

    # Front end ###############################################################

    def fragment(
        request: fastapi.Request,
        user: db.User,
        headers: dict[str, str] | None = None,
        **context: object,
    ) -> responses.Response:
        """Serves the repositories section.

        Args:
            request: The request.
            user: The signed-in user.
            headers: Extra response headers.
            **context: Further template variables: error, message, value.

        Returns:
            The rendered fragment.
        """
        return templates.TemplateResponse(
            request,
            "_repos.html",
            {**page_context(database, auth_settings, user), **context},
            headers=headers,
        )

    def ui_action(
        request: fastapi.Request,
        user: db.User,
        action: Callable[[], str | None],
        refresh_builds: bool = False,
        **context: object,
    ) -> responses.Response:
        """Runs an action and serves the section, reporting errors in it.

        Errors are reported with status 200, because htmx does not swap in
        4xx responses.

        Args:
            request: The request.
            user: The signed-in user.
            action: Does the work; returns a message to show, or None.
            refresh_builds: Whether to make the build table reload.
            **context: Template variables kept when the action fails.

        Returns:
            The rendered fragment.
        """
        try:
            with _http_errors():
                message = action()
        except fastapi.HTTPException as e:
            return fragment(request, user, error=e.detail, **context)
        headers = {"HX-Trigger": "refresh"} if refresh_builds else None
        return fragment(request, user, headers, message=message)

    @router.get("/ui/repos", response_class=responses.HTMLResponse)
    def ui_repos(
        request: fastapi.Request,
        user: db.User = fastapi.Depends(auth.current_user),
    ) -> responses.Response:
        """Serves the repositories section."""
        return fragment(request, user)

    @router.get("/ui/github/repos", response_class=responses.HTMLResponse)
    def ui_github_repos(
        request: fastapi.Request,
        q: str = "",
        user: db.User = fastapi.Depends(auth.current_user),
    ) -> responses.Response:
        """Serves the repository picker's list of matching repositories."""
        try:
            results = list_github_repos(request, q, user)
            error = None
        except fastapi.HTTPException as e:
            results, error = [], e.detail
        connected = {
            r["full_name"].lower() for r in database.list_repos(user["id"])
        }
        return templates.TemplateResponse(
            request,
            "_repo_picker.html",
            {"results": results, "error": error, "connected": connected},
        )

    @router.post("/ui/repos", response_class=responses.HTMLResponse)
    def ui_create_repo(
        request: fastapi.Request,
        repo: str = fastapi.Form(""),
        name: str = fastapi.Form(""),
        user: db.User = fastapi.Depends(auth.current_user),
    ) -> responses.Response:
        """Connects a repository and serves the section."""

        def action() -> str:
            return (
                f"Connected {add_repo(request, user, repo, name)['full_name']}."
            )

        return ui_action(request, user, action, value=repo)

    @router.post(
        "/ui/repos/{repo_id}/fetch", response_class=responses.HTMLResponse
    )
    def ui_fetch_repo(
        request: fastapi.Request,
        repo_id: int,
        user: db.User = fastapi.Depends(auth.current_user),
    ) -> responses.Response:
        """Refetches a repository's branches and tags and serves the section."""

        def action() -> str:
            repo = update_repo(request, get_owned_or_404(repo_id, user))
            return (
                f"Updated {repo['full_name']}: {len(repo['branches'])}"
                f" branches, {len(repo['tags'])} tags."
            )

        return ui_action(request, user, action)

    @router.post(
        "/ui/repos/{repo_id}/ref", response_class=responses.HTMLResponse
    )
    def ui_select_ref(
        request: fastapi.Request,
        repo_id: int,
        ref: str = fastapi.Form(""),
        user: db.User = fastapi.Depends(auth.current_user),
    ) -> responses.Response:
        """Selects a branch or tag, given as "branch:<name>" or "tag:<name>"."""

        def action() -> None:
            ref_type, _, name = ref.partition(":")
            if ref_type not in (db.REF_BRANCH, db.REF_TAG):
                raise ValueError("invalid branch or tag")
            select_ref(
                database, get_owned_or_404(repo_id, user), name, ref_type
            )

        return ui_action(request, user, action)

    @router.post(
        "/ui/repos/{repo_id}/build", response_class=responses.HTMLResponse
    )
    def ui_build_repo(
        request: fastapi.Request,
        repo_id: int,
        user: db.User = fastapi.Depends(auth.current_user),
    ) -> responses.Response:
        """Queues a build of the selected ref and serves the section."""

        def action() -> str:
            repo = get_owned_or_404(repo_id, user)
            build = queue_build(database, store, repo, user, None, None)
            return (
                f"Queued {build['name']} from {repo['full_name']}"
                f" {build['ref']}."
            )

        return ui_action(request, user, action, refresh_builds=True)

    @router.post(
        "/ui/repos/{repo_id}/delete", response_class=responses.HTMLResponse
    )
    def ui_delete_repo(
        request: fastapi.Request,
        repo_id: int,
        user: db.User = fastapi.Depends(auth.current_user),
    ) -> responses.Response:
        """Disconnects a repository and serves the section."""

        def action() -> str:
            repo = get_owned_or_404(repo_id, user)
            database.delete_repo(repo["id"])
            return f"Removed {repo['full_name']}."

        return ui_action(request, user, action)

    return router
