"""Google sign-in, sessions and the user management API.

Users sign in with a Google account through OAuth (OpenID Connect). A signed
session cookie then identifies them. Who may sign in is decided by the users
table: an account has to be added by an administrator first, unless it is
listed as an administrator in the configuration or belongs to an allowed
domain.

Routes:

    /auth/login, /auth/callback, /auth/logout   sign-in flow
    /api/me                                     the signed-in user
    /api/users...                               user management, admins only
"""

import re
import sqlite3
from typing import Any
from typing import Literal

from authlib.integrations import starlette_client
import fastapi
from fastapi import responses
import pydantic

from buildserver import config
from buildserver import db

ROLE_ADMIN = "admin"
ROLE_USER = "user"

Role = Literal["admin", "user"]

_GOOGLE_METADATA_URL = (
    "https://accounts.google.com/.well-known/openid-configuration"
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SESSION_USER_ID = "user_id"
_SESSION_LOGIN_ERROR = "login_error"


class UserCreate(pydantic.BaseModel):
    """Body of a request that adds a user.

    Attributes:
        email: Email of the Google account that may sign in.
        role: Role of the new user.
    """

    email: str
    role: Role = ROLE_USER


class UserUpdate(pydantic.BaseModel):
    """Body of a request that changes a user. Omitted fields are left alone.

    Attributes:
        role: New role.
        is_active: Whether the user may sign in.
    """

    role: Role | None = None
    is_active: bool | None = None


def present_user(user: db.User) -> dict[str, Any]:
    """Returns a user record as it is shown to clients."""
    return {
        "id": user["id"],
        "email": user["email"],
        "name": user["name"],
        "picture": user["picture"],
        "role": user["role"],
        "is_active": bool(user["is_active"]),
        "created_at": user["created_at"],
        "last_login_at": user["last_login_at"],
    }


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


def current_user(request: fastapi.Request) -> db.User:
    """FastAPI dependency that requires a signed-in user.

    Args:
        request: The request.

    Returns:
        The signed-in user.

    Raises:
        fastapi.HTTPException: With status 401 if nobody is signed in.
    """
    user = optional_user(request)
    if user is None:
        raise fastapi.HTTPException(401, "not signed in")
    return user


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
    """Creates the OAuth client registry with Google registered.

    Args:
        auth_settings: The sign-in settings.

    Returns:
        The registry. Its `google` attribute is the Google client.
    """
    oauth = starlette_client.OAuth()
    oauth.register(
        name="google",
        client_id=auth_settings.google_client_id,
        client_secret=auth_settings.google_client_secret,
        server_metadata_url=_GOOGLE_METADATA_URL,
        client_kwargs={"scope": "openid email profile"},
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

    # User management API #####################################################

    @router.get("/api/me")
    def get_me(
        user: db.User = fastapi.Depends(current_user),
    ) -> dict[str, Any]:
        """Returns the signed-in user."""
        return present_user(user)

    admins_only = [fastapi.Depends(admin_user)]

    @router.get("/api/users", dependencies=admins_only)
    def list_users() -> list[dict[str, Any]]:
        """Lists all users."""
        return [present_user(user) for user in database.list_users()]

    @router.post("/api/users", status_code=201, dependencies=admins_only)
    def create_user(body: UserCreate) -> dict[str, Any]:
        """Adds a user, allowing that Google account to sign in."""
        email = body.email.strip().lower()
        if not _EMAIL_RE.fullmatch(email):
            raise fastapi.HTTPException(422, "email is not a valid address")
        try:
            return present_user(database.create_user(email, body.role))
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

    return router
