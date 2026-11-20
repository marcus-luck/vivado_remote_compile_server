"""The parts of the GitHub REST API the build server uses.

Every function takes an httpx.Client whose base URL is the API (API_URL; tests
pass one with a fake transport) and the OAuth token of the user it acts for.
"""

import re
from typing import Any

import httpx

API_URL = "https://api.github.com"
# Most items one paginated listing returns; GitHub gives at most 100 a page.
MAX_ITEMS = 1000
_PAGE_SIZE = 100
# Pages of the user's repositories searched by the repository picker.
_PICKER_PAGES = 3

_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
_URL_PREFIXES = (
    "https://github.com/",
    "http://github.com/",
    "https://www.github.com/",
    "git@github.com:",
    "ssh://git@github.com/",
)


class GitHubError(Exception):
    """A GitHub API request failed.

    Attributes:
        status: HTTP status of GitHub's answer, or 0 if GitHub could not be
            reached. 401 means the token no longer works.
        message: What went wrong, for showing to the user.
    """

    def __init__(self, status: int, message: str) -> None:
        """Initialises the error.

        Args:
            status: HTTP status of GitHub's answer, or 0.
            message: What went wrong.
        """
        super().__init__(message)
        self.status = status
        self.message = message


def create_client() -> httpx.Client:
    """Creates the HTTP client the API functions are called with."""
    return httpx.Client(base_url=API_URL, timeout=20)


def parse_repo(text: str) -> str:
    """Reads a repository name out of a URL or an "owner/repo" string.

    Args:
        text: A GitHub URL (https or SSH, with or without .git) or
            "owner/repo".

    Returns:
        The repository as "owner/repo".

    Raises:
        ValueError: If the text does not name a GitHub repository.
    """
    name = text.strip()
    for prefix in _URL_PREFIXES:
        if name.lower().startswith(prefix):
            name = name[len(prefix) :]
            break
    name = name.rstrip("/").removesuffix(".git")
    if not _FULL_NAME_RE.fullmatch(name):
        raise ValueError(
            "not a GitHub repository; give a URL such as"
            " https://github.com/owner/repo or owner/repo"
        )
    return name


def _get(
    http: httpx.Client,
    token: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """Sends an authenticated GET request to the API.

    Args:
        http: The API client.
        token: OAuth token of the user.
        path: Path of the API endpoint.
        params: Query parameters.

    Returns:
        The successful response.

    Raises:
        GitHubError: If GitHub cannot be reached or answers with an error.
    """
    try:
        response = http.get(
            path,
            params=params,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    except httpx.HTTPError as e:
        raise GitHubError(0, f"GitHub could not be reached: {e}") from e
    if response.status_code == 401:
        raise GitHubError(
            401, "the GitHub link no longer works; reconnect GitHub"
        )
    if response.status_code == 404:
        raise GitHubError(
            404, "repository not found, or the linked account cannot read it"
        )
    if response.status_code >= 400:
        try:
            detail = response.json().get("message", "")
        except ValueError:
            detail = ""
        raise GitHubError(
            response.status_code,
            f"GitHub answered {response.status_code} {detail}".strip(),
        )
    return response


def _paginate(
    http: httpx.Client,
    token: str,
    path: str,
    params: dict[str, Any] | None = None,
    max_pages: int = MAX_ITEMS // _PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Collects the items of a paginated API listing.

    Args:
        http: The API client.
        token: OAuth token of the user.
        path: Path of the API endpoint.
        params: Further query parameters.
        max_pages: Most pages to request.

    Returns:
        The items of all pages, in GitHub's order.

    Raises:
        GitHubError: If a request fails.
    """
    items = []
    for page in range(1, max_pages + 1):
        batch = _get(
            http,
            token,
            path,
            {**(params or {}), "per_page": _PAGE_SIZE, "page": page},
        ).json()
        items.extend(batch)
        if len(batch) < _PAGE_SIZE:
            break
    return items


def get_user(http: httpx.Client, token: str) -> dict[str, Any]:
    """Returns the account a token belongs to.

    Args:
        http: The API client.
        token: OAuth token of the user.

    Returns:
        GitHub's user object; "id" and "login" are what the server uses.

    Raises:
        GitHubError: If the request fails.
    """
    return _get(http, token, "/user").json()


def list_user_repos(
    http: httpx.Client, token: str, query: str = ""
) -> list[dict[str, Any]]:
    """Lists repositories the user can read, most recently pushed first.

    Args:
        http: The API client.
        token: OAuth token of the user.
        query: Only repositories whose "owner/repo" contains this text,
            ignoring case.

    Returns:
        Dicts with "full_name", "private" and "description".

    Raises:
        GitHubError: If a request fails.
    """
    repos = _paginate(
        http, token, "/user/repos", {"sort": "pushed"}, _PICKER_PAGES
    )
    query = query.strip().lower()
    return [
        {
            "full_name": repo["full_name"],
            "private": repo.get("private", False),
            "description": repo.get("description") or "",
        }
        for repo in repos
        if query in repo["full_name"].lower()
    ]


def get_repo(http: httpx.Client, token: str, full_name: str) -> dict[str, Any]:
    """Looks up a repository the user wants to build from.

    Args:
        http: The API client.
        token: OAuth token of the user.
        full_name: The repository, as "owner/repo".

    Returns:
        A dict with "full_name", as GitHub spells it, and "default_branch".

    Raises:
        GitHubError: If the repository does not exist or the user cannot
            read it.
    """
    repo = _get(http, token, f"/repos/{full_name}").json()
    if not repo.get("permissions", {}).get("pull", True):
        raise GitHubError(403, "the linked account cannot read this repository")
    return {
        "full_name": repo["full_name"],
        "default_branch": repo["default_branch"],
    }


def list_branches(http: httpx.Client, token: str, full_name: str) -> list[str]:
    """Lists the branch names of a repository.

    Args:
        http: The API client.
        token: OAuth token of the user.
        full_name: The repository, as "owner/repo".

    Returns:
        The branch names, at most MAX_ITEMS.

    Raises:
        GitHubError: If a request fails.
    """
    return [
        branch["name"]
        for branch in _paginate(http, token, f"/repos/{full_name}/branches")
    ]


def list_tags(http: httpx.Client, token: str, full_name: str) -> list[str]:
    """Lists the tag names of a repository, newest first as GitHub orders them.

    Args:
        http: The API client.
        token: OAuth token of the user.
        full_name: The repository, as "owner/repo".

    Returns:
        The tag names, at most MAX_ITEMS.

    Raises:
        GitHubError: If a request fails.
    """
    return [
        tag["name"]
        for tag in _paginate(http, token, f"/repos/{full_name}/tags")
    ]


def revoke_grant(
    http: httpx.Client, client_id: str, client_secret: str, token: str
) -> None:
    """Revokes the user's authorisation of the OAuth App, and so the token.

    Args:
        http: The API client.
        client_id: Client id of the OAuth App.
        client_secret: Client secret of the OAuth App.
        token: OAuth token of the user.

    Raises:
        GitHubError: If GitHub cannot be reached or refuses. A token that
            already stopped working (404) is not an error.
    """
    try:
        response = http.request(
            "DELETE",
            f"/applications/{client_id}/grant",
            json={"access_token": token},
            auth=(client_id, client_secret),
            headers={"Accept": "application/vnd.github+json"},
        )
    except httpx.HTTPError as e:
        raise GitHubError(0, f"GitHub could not be reached: {e}") from e
    if response.status_code not in (204, 404):
        raise GitHubError(
            response.status_code,
            f"GitHub refused to revoke the grant ({response.status_code})",
        )
