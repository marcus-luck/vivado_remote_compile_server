"""HTTP API and htmx front end.

Run with:

    uvicorn --factory buildserver.api:create_app

/api/... is the JSON API. / and /ui/... serve the front end: / is the full
page, and the /ui/... routes return the HTML fragments htmx swaps into it.
Sign-in, API tokens and user management live in buildserver.auth; every route
here except / requires a signed-in user or an API token.
"""

import datetime
import pathlib
import re
import shutil
import uuid
import zipfile

import fastapi
from fastapi import responses
from fastapi import staticfiles
from fastapi import templating
import markupsafe
import pydantic
from starlette.middleware import sessions

from buildserver import auth
from buildserver import config
from buildserver import db
from buildserver import storage

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_PACKAGE_DIR = pathlib.Path(__file__).parent
_UPLOAD_CHUNK_BYTES = 1024 * 1024
_LOG_TAIL_BYTES = 64 * 1024
# htmx stops polling an element when a response has this status.
_HTMX_STOP_POLLING = 286


def default_name(filename: str) -> str:
    """Derives a build name from the file name of an uploaded package.

    Args:
        filename: File name of the package, as given by the client.

    Returns:
        The file name without its extension, reduced to the characters allowed
        in a build name, or "build" if nothing usable is left.
    """
    stem = pathlib.Path(filename).stem
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).lstrip("._-")[:64]
    return name or "build"


def duration(build: db.Build) -> str:
    """Formats how long a build ran, or has been running so far.

    Args:
        build: The build record.

    Returns:
        The duration as, for example, "1h 4m 9s", or an empty string if the
        build never started.
    """
    if not build["started_at"]:
        return ""
    if build["finished_at"]:
        end = datetime.datetime.fromisoformat(build["finished_at"])
    else:
        end = datetime.datetime.now(datetime.timezone.utc)
    start = datetime.datetime.fromisoformat(build["started_at"])
    total_seconds = max(0, round((end - start).total_seconds()))
    hours, rest = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return (
        (f"{hours}h " if hours else "")
        + (f"{minutes}m " if hours or minutes else "")
        + f"{seconds}s"
    )


def create_app(
    settings: config.Settings | None = None,
    auth_settings: config.AuthSettings | None = None,
) -> fastapi.FastAPI:
    """Creates the FastAPI application.

    Everything except the sign-in page and the static files requires a
    signed-in user.

    Args:
        settings: The server settings. Loaded from the .env file and the
            environment if omitted.
        auth_settings: The sign-in settings. Loaded from the .env file and
            the environment if omitted.

    Returns:
        The application, serving the JSON API and the front end.
    """
    settings = settings or config.load_settings()
    auth_settings = auth_settings or config.load_auth_settings()
    store = storage.Storage(settings.data_dir)
    database = db.Database(store.db_path)
    app = fastapi.FastAPI(title="Firmware build server")
    app.state.database = database
    app.state.oauth = auth.create_oauth(auth_settings)
    app.add_middleware(
        sessions.SessionMiddleware,
        secret_key=auth_settings.session_secret,
        same_site="lax",
        https_only=auth_settings.public_url.startswith("https://"),
    )
    app.include_router(
        auth.create_router(database, auth_settings, app.state.oauth)
    )
    protected = fastapi.APIRouter(
        dependencies=[fastapi.Depends(auth.current_user)]
    )
    templates = templating.Jinja2Templates(directory=_PACKAGE_DIR / "templates")
    templates.env.filters["duration"] = duration

    def present(build: db.Build) -> db.Build:
        """Returns a build record as it is shown to clients."""
        build = dict(build)
        build["cancel_requested"] = bool(build["cancel_requested"])
        build["has_artifacts"] = store.artifacts_path(build["id"]).is_file()
        return build

    def present_all() -> list[db.Build]:
        """Returns the recent builds as they are shown to clients."""
        return [present(build) for build in database.list_builds()]

    def get_or_404(build_id: str) -> db.Build:
        """Returns a build record, or raises a 404 if there is none."""
        build = database.get(build_id)
        if build is None:
            raise fastapi.HTTPException(404, "build not found")
        return build

    def store_upload(
        package: fastapi.UploadFile, name: str | None, user: db.User
    ) -> db.Build:
        """Saves an uploaded package and queues a build for it.

        Args:
            package: The uploaded build package.
            name: Build name, or None or empty to derive it from the file name
                of the package.
            user: The user queueing the build.

        Returns:
            The new build record.

        Raises:
            fastapi.HTTPException: If the name is invalid (422), the package
                is too large (413) or the package is not a zip archive (400).
        """
        filename = package.filename or "package.zip"
        name = name or default_name(filename)
        if not NAME_RE.fullmatch(name):
            raise fastapi.HTTPException(
                422,
                "name must be 1-64 characters of A-Z a-z 0-9 . _ - and start"
                " with a letter or digit",
            )

        build_id = uuid.uuid4().hex[:12]
        build_dir = store.build_dir(build_id)
        build_dir.mkdir(parents=True)
        package_path = store.package_path(build_id)
        try:
            size = 0
            with open(package_path, "wb") as out:
                while chunk := package.file.read(_UPLOAD_CHUNK_BYTES):
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise fastapi.HTTPException(
                            413,
                            "package exceeds"
                            f" {settings.max_upload_bytes} bytes",
                        )
                    out.write(chunk)
            if not zipfile.is_zipfile(package_path):
                raise fastapi.HTTPException(
                    400, "package must be a zip archive"
                )
        except BaseException:
            shutil.rmtree(build_dir, ignore_errors=True)
            raise
        return database.create(build_id, name, filename, user["email"])

    def read_log(build_id: str, tail: int) -> str:
        """Reads the end of a build log.

        Args:
            build_id: Id of the build.
            tail: Number of bytes to read from the end of the log, or 0 for
                all of it.

        Returns:
            The log text, empty if the build has not produced a log yet.
        """
        log_path = store.log_path(build_id)
        if not log_path.is_file():
            return ""
        with open(log_path, "rb") as log:
            if tail > 0:
                log.seek(max(0, log_path.stat().st_size - tail))
            return log.read().decode("utf-8", errors="replace")

    # JSON API ################################################################

    @protected.post("/api/builds", status_code=201)
    def create_build(
        package: fastapi.UploadFile = fastapi.File(...),
        name: str | None = fastapi.Form(None),
        user: db.User = fastapi.Depends(auth.current_user),
    ) -> db.Build:
        """Uploads a build package and queues a build for it."""
        return present(store_upload(package, name, user))

    @protected.get("/api/builds")
    def list_builds(limit: int = 200) -> list[db.Build]:
        """Lists the most recent builds, newest first."""
        return [present(build) for build in database.list_builds(limit)]

    @protected.get("/api/builds/{build_id}")
    def get_build(build_id: str) -> db.Build:
        """Returns one build."""
        return present(get_or_404(build_id))

    @protected.post("/api/builds/{build_id}/cancel")
    def cancel_build(build_id: str) -> db.Build:
        """Cancels a queued or running build."""
        get_or_404(build_id)
        database.request_cancel(build_id)
        return present(get_or_404(build_id))

    @protected.get(
        "/api/builds/{build_id}/log", response_class=responses.PlainTextResponse
    )
    def get_log(build_id: str, tail: int = _LOG_TAIL_BYTES) -> str:
        """Returns the last `tail` bytes of the build log (0 for all of it)."""
        get_or_404(build_id)
        return read_log(build_id, tail)

    @protected.get("/api/builds/{build_id}/artifacts")
    def download_artifacts(build_id: str) -> responses.FileResponse:
        """Downloads the artifacts of a build as a zip archive."""
        build = get_or_404(build_id)
        path = store.artifacts_path(build_id)
        if not path.is_file():
            raise fastapi.HTTPException(404, "no artifacts for this build")
        return responses.FileResponse(
            path,
            media_type="application/zip",
            filename=f"{build['name']}-{build_id}.zip",
        )

    # Front end ###############################################################

    @app.get("/", response_class=responses.HTMLResponse)
    def ui_index(request: fastapi.Request) -> responses.Response:
        """Serves the front end page, or the sign-in page."""
        user = auth.optional_user(request)
        if user is None:
            return templates.TemplateResponse(
                request, "login.html", {"error": auth.pop_login_error(request)}
            )
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "builds": present_all(),
                "user": user,
                "tokens": database.list_tokens(user["id"]),
            },
        )

    @protected.get("/ui/builds", response_class=responses.HTMLResponse)
    def ui_builds(request: fastapi.Request) -> responses.Response:
        """Serves the build table fragment."""
        return templates.TemplateResponse(
            request, "_builds.html", {"builds": present_all()}
        )

    @protected.post("/ui/builds", response_class=responses.HTMLResponse)
    def ui_create_build(
        request: fastapi.Request,
        package: fastapi.UploadFile = fastapi.File(...),
        name: str = fastapi.Form(""),
        user: db.User = fastapi.Depends(auth.current_user),
    ) -> responses.Response:
        """Queues a build from the upload form and serves the form again.

        An upload error is reported inside the form with status 200, because
        htmx does not swap in 4xx responses.
        """
        try:
            store_upload(package, name, user)
        except fastapi.HTTPException as e:
            return templates.TemplateResponse(
                request, "_form.html", {"name": name, "error": e.detail}
            )
        return templates.TemplateResponse(
            request, "_form.html", {}, headers={"HX-Trigger": "refresh"}
        )

    @protected.post(
        "/ui/builds/{build_id}/cancel", response_class=responses.HTMLResponse
    )
    def ui_cancel_build(
        request: fastapi.Request, build_id: str
    ) -> responses.Response:
        """Cancels a build and serves the build table fragment."""
        database.request_cancel(build_id)
        return templates.TemplateResponse(
            request, "_builds.html", {"builds": present_all()}
        )

    @protected.get(
        "/ui/builds/{build_id}/log", response_class=responses.HTMLResponse
    )
    def ui_log(request: fastapi.Request, build_id: str) -> responses.Response:
        """Serves the log panel fragment of a build."""
        build = get_or_404(build_id)
        return templates.TemplateResponse(
            request,
            "_log.html",
            {"build": build, "text": read_log(build_id, _LOG_TAIL_BYTES)},
        )

    @protected.get(
        "/ui/builds/{build_id}/log/text", response_class=responses.HTMLResponse
    )
    def ui_log_text(build_id: str) -> responses.Response:
        """Serves the log text polled by an open log panel.

        Once the build is over the response tells htmx to stop polling.
        """
        build = get_or_404(build_id)
        active = build["status"] in (db.QUEUED, db.RUNNING)
        text = read_log(build_id, _LOG_TAIL_BYTES) or "(empty)"
        return responses.HTMLResponse(
            str(markupsafe.escape(text)),
            status_code=200 if active else _HTMX_STOP_POLLING,
        )

    def tokens_fragment(
        request: fastapi.Request, user: db.User, **context: object
    ) -> responses.Response:
        """Serves the API token section for a user.

        Args:
            request: The request.
            user: The user whose tokens are listed.
            **context: Further template variables: new_token, error, name.

        Returns:
            The rendered fragment.
        """
        return templates.TemplateResponse(
            request,
            "_tokens.html",
            {"tokens": database.list_tokens(user["id"]), **context},
        )

    @protected.get("/ui/tokens", response_class=responses.HTMLResponse)
    def ui_tokens(
        request: fastapi.Request,
        user: db.User = fastapi.Depends(auth.current_user),
    ) -> responses.Response:
        """Serves the API token section."""
        return tokens_fragment(request, user)

    @protected.post("/ui/tokens", response_class=responses.HTMLResponse)
    def ui_create_token(
        request: fastapi.Request,
        name: str = fastapi.Form(""),
        expires_in_days: str = fastapi.Form(""),
        user: db.User = fastapi.Depends(auth.session_user),
    ) -> responses.Response:
        """Creates an API token and serves the section showing it once.

        Errors are reported inside the section with status 200, because htmx
        does not swap in 4xx responses.
        """
        try:
            body = auth.TokenCreate(
                name=name.strip(), expires_in_days=expires_in_days or None
            )
        except pydantic.ValidationError as e:
            error = "; ".join(
                f"{err['loc'][-1]}: {err['msg']}" for err in e.errors()
            )
            return tokens_fragment(request, user, error=error, name=name)
        _, token = auth.issue_token(
            database, user["id"], body.name, body.expires_in_days
        )
        return tokens_fragment(request, user, new_token=token)

    @protected.post(
        "/ui/tokens/{token_id}/revoke", response_class=responses.HTMLResponse
    )
    def ui_revoke_token(
        request: fastapi.Request,
        token_id: int,
        user: db.User = fastapi.Depends(auth.current_user),
    ) -> responses.Response:
        """Revokes one of the user's API tokens and serves the section."""
        auth.revoke_token(database, user, token_id)
        return tokens_fragment(request, user)

    app.include_router(protected)
    app.mount(
        "/static",
        staticfiles.StaticFiles(directory=_PACKAGE_DIR / "static"),
        name="static",
    )
    return app
