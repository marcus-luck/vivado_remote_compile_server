"""Google sign-in, sessions, API tokens and the user management API.

People sign in with a Google account through OAuth (OpenID Connect). A signed
session cookie then identifies them. Who may sign in is decided by the users
table: an account has to be added by an administrator first, unless it is
listed as an administrator in the configuration or belongs to an allowed
domain.

Scripts and robots authenticate with an API token instead, sent as
"Authorization: Bearer <token>". A person creates tokens for themselves while
signed in. Robots get service accounts: users an administrator adds, which
cannot sign in with Google and use only the tokens an administrator creates
for them. Tokens cannot be used to create tokens.

A signed-in user can link a GitHub account, through a GitHub OAuth App, to
build from its repositories (see buildserver.repos). Linking needs a session,
not a token.

Routes:

    /auth/login, /auth/callback, /auth/logout   sign-in flow
    /auth/github/...                            linking a GitHub account
    /api/me                                     the authenticated user
    /api/tokens...                              the user's own API tokens
    /api/users...                               user management, admins only
"""

import datetime
import hashlib
import re
import secrets
import sqlite3
from typing import Any
from typing import Literal

from authlib.integrations import starlette_client
import fastapi
from fastapi import responses
from fastapi import security
import pydantic

from buildserver import config
from buildserver import db
from buildserver import github

ROLE_ADMIN = "admin"
ROLE_USER = "user"

Role = Literal["admin", "user"]

_GOOGLE_METADATA_URL = (
    "https://accounts.google.com/.well-known/openid-configuration"
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SERVICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SESSION_USER_ID = "user_id"
_SESSION_LOGIN_ERROR = "login_error"
_SESSION_GITHUB_MESSAGE = "github_message"
_GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
# Reading private repositories needs the full "repo" scope; OAuth Apps have no
# read-only scope for them.
_GITHUB_SCOPE = "repo"

TOKEN_PREFIX = "bst_"
# Characters after TOKEN_PREFIX that are stored to tell tokens apart.
_TOKEN_SHOWN_CHARS = 8
_MAX_TOKEN_DAYS = 3650

_bearer = security.HTTPBearer(
    auto_error=False,
    description="An API token, created on the web page or at /api/tokens.",
)


class UserCreate(pydantic.BaseModel):
    """Body of a request that adds a user.

    Attributes:
        email: Email of the Google account that may sign in, or for a
            service account its name.
        role: Role of the new user.
        is_service: Whether to add a service account, which cannot sign in
            and authenticates only with API tokens.
    """

    email: str
    role: Role = ROLE_USER
    is_service: bool = False


class UserUpdate(pydantic.BaseModel):
    """Body of a request that changes a user. Omitted fields are left alone.

    Attributes:
        role: New role.
        is_active: Whether the user may sign in.
    """

    role: Role | None = None
    is_active: bool | None = None


class TokenCreate(pydantic.BaseModel):
    """Body of a request that creates an API token.

    Attributes:
        name: What the token is for, e.g. "laptop" or "nightly CI".
        expires_in_days: Days until the token stops working, or None for a
            token that works until it is revoked.
    """

    name: str = pydantic.Field(min_length=1, max_length=64)
    expires_in_days: int | None = pydantic.Field(
        default=None, ge=1, le=_MAX_TOKEN_DAYS
    )


def present_user(user: db.User) -> dict[str, Any]:
    """Returns a user record as it is shown to clients."""
    return {
        "id": user["id"],
        "email": user["email"],
        "name": user["name"],
        "picture": user["picture"],
        "role": user["role"],
        "is_active": bool(user["is_active"]),
        "is_service": bool(user["is_service"]),
        "created_at": user["created_at"],
        "last_login_at": user["last_login_at"],
    }


def present_token(token: db.Token) -> dict[str, Any]:
    """Returns a token record as it is shown to clients, without its hash."""
    return {
        "id": token["id"],
        "user_id": token["user_id"],
        "name": token["name"],
        "prefix": token["prefix"],
        "created_at": token["created_at"],
        "expires_at": token["expires_at"],
        "last_used_at": token["last_used_at"],
    }


def _hash_token(token: str) -> str:
    """Returns the hash under which a token is stored.

    Tokens are long random strings, so a fast hash is enough.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(
    database: db.Database,
    user_id: int,
    name: str,
    expires_in_days: int | None,
) -> tuple[db.Token, str]:
    """Creates an API token.

    Args:
        database: The user records.
        user_id: Id of the user the token authenticates as.
        name: What the token is for.
        expires_in_days: Days until the token stops working, or None if it
            does not expire.

    Returns:
        The token record and the token itself. Only its hash is stored, so
        this is the only time the token is available.
    """
    secret = secrets.token_urlsafe(32)
    expires_at = None
    if expires_in_days is not None:
        expires_at = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=expires_in_days)
        ).isoformat(timespec="milliseconds")
    token = TOKEN_PREFIX + secret
    record = database.create_token(
        user_id,
        name.strip(),
        secret[:_TOKEN_SHOWN_CHARS],
        _hash_token(token),
        expires_at,
    )
    return record, token


def revoke_token(database: db.Database, user: db.User, token_id: int) -> bool:
    """Revokes an API token on behalf of a user.

    Users may revoke their own tokens; administrators may revoke any.

    Args:
        database: The user records.
        user: The user asking to revoke the token.
        token_id: Id of the token.

    Returns:
        True if the token was revoked, False if there is no such token or it
        belongs to someone else.
    """
    token = database.get_token(token_id)
    if token is None:
        return False
    if token["user_id"] != user["id"] and user["role"] != ROLE_ADMIN:
        return False
    return database.delete_token(token_id)


def user_for_token(database: db.Database, token: str) -> db.User | None:
    """Finds the user an API token authenticates as.

    Args:
        database: The user records.
        token: The token, as sent by the client.

    Returns:
        The user, or None if the token is unknown or expired, or the user
        has been deactivated.
    """
    record = database.get_token_by_hash(_hash_token(token))
    if record is None:
        return None
    if record["expires_at"] is not None and record["expires_at"] <= db.now():
        return None
    user = database.get_user(record["user_id"])
    if user is None or not user["is_active"]:
        return None
    database.touch_token(record["id"])
    return user


def optional_user(request: fastapi.Request) -> db.User | None:
    """Finds the user a request's session belongs to.

    Args:
        request: The request. Its app must hold the database in
            app.state.database.

    Returns:
        The signed-in user, or None if the request has no valid session or the
        user has been removed or deactivated.
    """
    user_id = request.session.get(_SESSION_USER_ID)
    if user_id is None:
        return None
    user = request.app.state.database.get_user(user_id)
    if user is None or not user["is_active"]:
        return None
    return user


def current_user(
    request: fastapi.Request,
    credentials: security.HTTPAuthorizationCredentials | None = fastapi.Depends(
        _bearer
    ),
) -> db.User:
    """FastAPI dependency that requires an API token or a signed-in user.

    A request that carries a bearer token is authenticated by the token
    alone, even if it also has a session.

    Args:
        request: The request.
        credentials: The bearer token of the request, if it has one.

    Returns:
        The authenticated user.

    Raises:
        fastapi.HTTPException: With status 401 if the token is not valid, or
            there is no token and nobody is signed in.
    """
    if credentials is not None:
        user = user_for_token(
            request.app.state.database, credentials.credentials
        )
        if user is None:
            raise fastapi.HTTPException(
                401,
                "invalid or expired API token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return user
    user = optional_user(request)
    if user is None:
        raise fastapi.HTTPException(401, "not signed in")
    return user


def session_user(
    request: fastapi.Request,
    credentials: security.HTTPAuthorizationCredentials | None = fastapi.Depends(
        _bearer
    ),
) -> db.User:
    """FastAPI dependency that requires a signed-in user, not an API token.

    Used where a leaked token must not be able to do more, such as creating
    further tokens.

    Args:
        request: The request.
        credentials: The bearer token of the request, if it has one.

    Returns:
        The signed-in user.

    Raises:
        fastapi.HTTPException: With status 403 if the request uses an API
            token, or 401 if nobody is signed in.
    """
    if credentials is not None:
        raise fastapi.HTTPException(
            403, "API tokens cannot be used for this; sign in on the web page"
        )
    return current_user(request, None)


def admin_user(
    user: db.User = fastapi.Depends(current_user),
) -> db.User:
    """FastAPI dependency that requires a signed-in administrator.

    Args:
        user: The signed-in user.

    Returns:
        The signed-in user.

    Raises:
        fastapi.HTTPException: With status 403 if the user is not an
            administrator.
    """
    if user["role"] != ROLE_ADMIN:
        raise fastapi.HTTPException(403, "administrators only")
    return user


def pop_login_error(request: fastapi.Request) -> str | None:
    """Returns, once, the reason the last sign-in attempt was refused."""
    return request.session.pop(_SESSION_LOGIN_ERROR, None)


def pop_github_message(request: fastapi.Request) -> str | None:
    """Returns, once, why the last attempt to link or unlink GitHub failed."""
    return request.session.pop(_SESSION_GITHUB_MESSAGE, None)


def resolve_sign_in(
    database: db.Database,
    auth_settings: config.AuthSettings,
    userinfo: dict[str, Any],
) -> db.User | None:
    """Decides whether a Google account may sign in.

    An account may sign in if it is a known, active user. Accounts listed as
    administrators in the configuration, and accounts of an allowed domain,
    are added to the users table the first time they sign in.

    Args:
        database: The user records.
        auth_settings: The sign-in settings.
        userinfo: Claims of the Google ID token.

    Returns:
        The user record, updated with the Google profile, or None if the
        account may not sign in.
    """
    email = (userinfo.get("email") or "").lower()
    if not email or not userinfo.get("email_verified"):
        return None
    is_configured_admin = email in auth_settings.admin_emails

    user = database.get_user_by_email(email)
    if user is not None and user["is_service"]:
        return None
    if user is None:
        if is_configured_admin:
            user = database.create_user(email, ROLE_ADMIN)
        elif email.rpartition("@")[2] in auth_settings.allowed_domains:
            user = database.create_user(email, ROLE_USER)
        else:
            return None
    elif is_configured_admin and (
        user["role"] != ROLE_ADMIN or not user["is_active"]
    ):
        # A configured administrator cannot be locked out through the API.
        user = database.update_user(user["id"], ROLE_ADMIN, True)
    if not user["is_active"]:
        return None
    return database.record_login(
        user["id"],
        userinfo.get("sub"),
        userinfo.get("name"),
        userinfo.get("picture"),
    )


def create_oauth(auth_settings: config.AuthSettings) -> starlette_client.OAuth:
    """Creates the OAuth client registry with Google and GitHub registered.

    Args:
        auth_settings: The sign-in settings.

    Returns:
        The registry. Its `google` attribute is the Google client, and its
        `github` attribute the GitHub client if GitHub is configured.
    """
    oauth = starlette_client.OAuth()
    oauth.register(
        name="google",
        client_id=auth_settings.google_client_id,
        client_secret=auth_settings.google_client_secret,
        server_metadata_url=_GOOGLE_METADATA_URL,
        client_kwargs={"scope": "openid email profile"},
    )
    if auth_settings.github_enabled:
        oauth.register(
            name="github",
            client_id=auth_settings.github_client_id,
            client_secret=auth_settings.github_client_secret,
            authorize_url=_GITHUB_AUTHORIZE_URL,
            access_token_url=_GITHUB_TOKEN_URL,
            client_kwargs={"scope": _GITHUB_SCOPE},
        )
    return oauth


def create_router(
    database: db.Database,
    auth_settings: config.AuthSettings,
    oauth: starlette_client.OAuth,
) -> fastapi.APIRouter:
    """Creates the sign-in and user management routes.

    Args:
        database: The user records.
        auth_settings: The sign-in settings.
        oauth: The OAuth client registry, from create_oauth().

    Returns:
        The router, to be included in the application.
    """
    router = fastapi.APIRouter()
    redirect_uri = f"{auth_settings.public_url}/auth/callback"
    github_redirect_uri = f"{auth_settings.public_url}/auth/github/callback"
    home_url = f"{auth_settings.public_url}/"

    def get_or_404(user_id: int) -> db.User:
        """Returns a user record, or raises a 404 if there is none."""
        user = database.get_user(user_id)
        if user is None:
            raise fastapi.HTTPException(404, "user not found")
        return user

    # Sign-in flow ############################################################

    @router.get("/auth/login", include_in_schema=False)
    async def login(request: fastapi.Request) -> responses.Response:
        """Sends the browser to Google to sign in."""
        return await oauth.google.authorize_redirect(request, redirect_uri)

    @router.get("/auth/callback", include_in_schema=False)
    async def callback(request: fastapi.Request) -> responses.Response:
        """Completes the sign-in when Google sends the browser back."""
        try:
            token = await oauth.google.authorize_access_token(request)
        except starlette_client.OAuthError as e:
            request.session[_SESSION_LOGIN_ERROR] = (
                f"Google sign-in failed: {e.description or e.error}"
            )
            return responses.RedirectResponse(home_url, status_code=303)

        userinfo = token.get("userinfo") or {}
        user = resolve_sign_in(database, auth_settings, userinfo)
        request.session.clear()
        if user is None:
            request.session[_SESSION_LOGIN_ERROR] = (
                f"{userinfo.get('email') or 'This account'} is not"
                " authorised to use this server. Ask an administrator to"
                " add it."
            )
        else:
            request.session[_SESSION_USER_ID] = user["id"]
        return responses.RedirectResponse(home_url, status_code=303)

    @router.post("/auth/logout", include_in_schema=False)
    def logout(request: fastapi.Request) -> responses.Response:
        """Signs the user out."""
        request.session.clear()
        return responses.RedirectResponse(home_url, status_code=303)

    # Linking GitHub ##########################################################

    def require_github() -> None:
        """Raises a 503 if no GitHub OAuth App is configured."""
        if not auth_settings.github_enabled:
            raise fastapi.HTTPException(503, "GitHub is not configured")

    @router.get("/auth/github/login", include_in_schema=False)
    async def github_login(
        request: fastapi.Request,
        user: db.User = fastapi.Depends(session_user),
    ) -> responses.Response:
        """Sends the browser to GitHub to link an account."""
        del user  # Only required to be signed in.
        require_github()
        return await oauth.github.authorize_redirect(
            request, github_redirect_uri
        )

    @router.get("/auth/github/callback", include_in_schema=False)
    async def github_callback(request: fastapi.Request) -> responses.Response:
        """Stores the link when GitHub sends the browser back."""
        require_github()
        user = optional_user(request)
        if user is None:
            return responses.RedirectResponse(home_url, status_code=303)
        try:
            token = await oauth.github.authorize_access_token(request)
        except starlette_client.OAuthError as e:
            request.session[_SESSION_GITHUB_MESSAGE] = (
                f"Linking GitHub failed: {e.description or e.error}"
            )
            return responses.RedirectResponse(home_url, status_code=303)
        try:
            account = github.get_user(
                request.app.state.github_http, token["access_token"]
            )
        except github.GitHubError as e:
            request.session[_SESSION_GITHUB_MESSAGE] = (
                f"Linking GitHub failed: {e.message}"
            )
            return responses.RedirectResponse(home_url, status_code=303)
        try:
            database.set_github_link(
                user["id"],
                account["id"],
                account["login"],
                token["access_token"],
                token.get("scope"),
            )
        except sqlite3.IntegrityError:
            request.session[_SESSION_GITHUB_MESSAGE] = (
                f"GitHub account {account['login']} is already linked to"
                " another user."
            )
        return responses.RedirectResponse(home_url, status_code=303)

    @router.post("/auth/github/unlink", include_in_schema=False)
    def github_unlink(
        request: fastapi.Request,
        user: db.User = fastapi.Depends(session_user),
    ) -> responses.Response:
        """Unlinks the user's GitHub account and revokes its token."""
        link = database.get_github_link(user["id"])
        if link is not None:
            if auth_settings.github_enabled:
                try:
                    github.revoke_grant(
                        request.app.state.github_http,
                        auth_settings.github_client_id,
                        auth_settings.github_client_secret,
                        link["access_token"],
                    )
                except github.GitHubError as e:
                    request.session[_SESSION_GITHUB_MESSAGE] = (
                        "Unlinked, but GitHub did not confirm revoking the"
                        f" token ({e.message}); revoke it in your GitHub"
                        " settings."
                    )
            database.delete_github_link(user["id"])
        return responses.RedirectResponse(home_url, status_code=303)

    # API tokens ##############################################################

    @router.get("/api/tokens")
    def list_tokens(
        user: db.User = fastapi.Depends(current_user),
    ) -> list[dict[str, Any]]:
        """Lists the API tokens of the authenticated user."""
        return [present_token(t) for t in database.list_tokens(user["id"])]

    @router.post("/api/tokens", status_code=201)
    def create_token(
        body: TokenCreate,
        user: db.User = fastapi.Depends(session_user),
    ) -> dict[str, Any]:
        """Creates an API token for the signed-in user.

        The response is the only place the token is shown.
        """
        record, token = issue_token(
            database, user["id"], body.name, body.expires_in_days
        )
        return {**present_token(record), "token": token}

    @router.delete("/api/tokens/{token_id}", status_code=204)
    def delete_token(
        token_id: int,
        user: db.User = fastapi.Depends(current_user),
    ) -> None:
        """Revokes one of the user's tokens, or any token for an admin."""
        if not revoke_token(database, user, token_id):
            raise fastapi.HTTPException(404, "token not found")

    # User management API #####################################################

    @router.get("/api/me")
    def get_me(
        user: db.User = fastapi.Depends(current_user),
    ) -> dict[str, Any]:
        """Returns the authenticated user."""
        link = database.get_github_link(user["id"])
        return {
            **present_user(user),
            "github_login": link["login"] if link else None,
        }

    admins_only = [fastapi.Depends(admin_user)]

    @router.get("/api/users", dependencies=admins_only)
    def list_users() -> list[dict[str, Any]]:
        """Lists all users."""
        return [present_user(user) for user in database.list_users()]

    @router.post("/api/users", status_code=201, dependencies=admins_only)
    def create_user(body: UserCreate) -> dict[str, Any]:
        """Adds a user, allowing that Google account to sign in.

        With is_service, adds a service account instead, named by the email
        field.
        """
        email = body.email.strip().lower()
        if body.is_service:
            if not _SERVICE_NAME_RE.fullmatch(email):
                raise fastapi.HTTPException(
                    422,
                    "a service account name must be 1-64 characters of"
                    " a-z 0-9 . _ - and start with a letter or digit",
                )
        elif not _EMAIL_RE.fullmatch(email):
            raise fastapi.HTTPException(422, "email is not a valid address")
        try:
            return present_user(
                database.create_user(email, body.role, body.is_service)
            )
        except sqlite3.IntegrityError as e:
            raise fastapi.HTTPException(
                409, "a user with that email already exists"
            ) from e

    @router.get("/api/users/{user_id}", dependencies=admins_only)
    def get_user(user_id: int) -> dict[str, Any]:
        """Returns one user."""
        return present_user(get_or_404(user_id))

    @router.patch("/api/users/{user_id}")
    def update_user(
        user_id: int,
        body: UserUpdate,
        admin: db.User = fastapi.Depends(admin_user),
    ) -> dict[str, Any]:
        """Changes the role of a user, or activates or deactivates them."""
        get_or_404(user_id)
        locks_self_out = body.role == ROLE_USER or body.is_active is False
        if user_id == admin["id"] and locks_self_out:
            raise fastapi.HTTPException(
                409, "you cannot demote or deactivate your own account"
            )
        return present_user(
            database.update_user(user_id, body.role, body.is_active)
        )

    @router.delete("/api/users/{user_id}", status_code=204)
    def delete_user(
        user_id: int,
        admin: db.User = fastapi.Depends(admin_user),
    ) -> None:
        """Removes a user. Builds they queued are kept."""
        get_or_404(user_id)
        if user_id == admin["id"]:
            raise fastapi.HTTPException(
                409, "you cannot delete your own account"
            )
        database.delete_user(user_id)

    @router.get("/api/users/{user_id}/tokens", dependencies=admins_only)
    def list_user_tokens(user_id: int) -> list[dict[str, Any]]:
        """Lists the API tokens of any user."""
        get_or_404(user_id)
        return [present_token(t) for t in database.list_tokens(user_id)]

    @router.post(
        "/api/users/{user_id}/tokens",
        status_code=201,
        dependencies=[*admins_only, fastapi.Depends(session_user)],
    )
    def create_user_token(user_id: int, body: TokenCreate) -> dict[str, Any]:
        """Creates an API token for any user, typically a service account.

        The response is the only place the token is shown.
        """
        get_or_404(user_id)
        record, token = issue_token(
            database, user_id, body.name, body.expires_in_days
        )
        return {**present_token(record), "token": token}

    return router
