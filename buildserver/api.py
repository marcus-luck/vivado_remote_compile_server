"""HTTP API and htmx front end.

Run with:  uvicorn --factory buildserver.api:create_app

/api/... is the JSON API. / and /ui/... serve the front end: / is the full page,
/ui/... return the HTML fragments htmx swaps into it.
"""

import re
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import escape

from . import db as states
from .config import Settings, load_settings
from .db import Database
from .storage import Storage

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PACKAGE_DIR = Path(__file__).parent
CHUNK = 1024 * 1024
LOG_TAIL = 64 * 1024
# htmx stops polling an element when a response has this status.
HTMX_STOP_POLLING = 286


def default_name(filename: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).stem).lstrip("._-")[:64]
    return name or "build"


def duration(build: dict) -> str:
    if not build["started_at"]:
        return ""
    end = datetime.fromisoformat(build["finished_at"]) if build["finished_at"] else datetime.now(timezone.utc)
    seconds = max(0, round((end - datetime.fromisoformat(build["started_at"])).total_seconds()))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return (f"{h}h " if h else "") + (f"{m}m " if h or m else "") + f"{s}s"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    storage = Storage(settings.data_dir)
    db = Database(storage.db_path)
    app = FastAPI(title="Firmware build server")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    templates.env.filters["duration"] = duration

    def present(build: dict) -> dict:
        build = dict(build)
        build["cancel_requested"] = bool(build["cancel_requested"])
        build["has_artifacts"] = storage.artifacts_path(build["id"]).is_file()
        return build

    def get_or_404(build_id: str) -> dict:
        build = db.get(build_id)
        if build is None:
            raise HTTPException(404, "build not found")
        return build

    def store_upload(package: UploadFile, name: str | None) -> dict:
        filename = package.filename or "package.zip"
        name = name or default_name(filename)
        if not NAME_RE.fullmatch(name):
            raise HTTPException(422, "name must be 1-64 characters of A-Z a-z 0-9 . _ - and start with a letter or digit")

        build_id = uuid.uuid4().hex[:12]
        build_dir = storage.build_dir(build_id)
        build_dir.mkdir(parents=True)
        package_path = storage.package_path(build_id)
        try:
            size = 0
            with open(package_path, "wb") as out:
                while chunk := package.file.read(CHUNK):
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise HTTPException(413, f"package exceeds {settings.max_upload_bytes} bytes")
                    out.write(chunk)
            if not zipfile.is_zipfile(package_path):
                raise HTTPException(400, "package must be a zip archive")
        except BaseException:
            shutil.rmtree(build_dir, ignore_errors=True)
            raise
        return db.create(build_id, name, filename)

    def read_log(build_id: str, tail: int) -> str:
        log_path = storage.log_path(build_id)
        if not log_path.is_file():
            return ""
        with open(log_path, "rb") as f:
            if tail > 0:
                f.seek(max(0, log_path.stat().st_size - tail))
            return f.read().decode("utf-8", errors="replace")

    # JSON API ################################################################

    @app.post("/api/builds", status_code=201)
    def create_build(package: UploadFile = File(...), name: str | None = Form(None)):
        return present(store_upload(package, name))

    @app.get("/api/builds")
    def list_builds(limit: int = 200):
        return [present(b) for b in db.list(limit)]

    @app.get("/api/builds/{build_id}")
    def get_build(build_id: str):
        return present(get_or_404(build_id))

    @app.post("/api/builds/{build_id}/cancel")
    def cancel_build(build_id: str):
        get_or_404(build_id)
        db.request_cancel(build_id)
        return present(db.get(build_id))

    @app.get("/api/builds/{build_id}/log", response_class=PlainTextResponse)
    def get_log(build_id: str, tail: int = LOG_TAIL):
        """Last `tail` bytes of the build log (tail=0 for all of it)."""
        get_or_404(build_id)
        return read_log(build_id, tail)

    @app.get("/api/builds/{build_id}/artifacts")
    def download_artifacts(build_id: str):
        build = get_or_404(build_id)
        path = storage.artifacts_path(build_id)
        if not path.is_file():
            raise HTTPException(404, "no artifacts for this build")
        return FileResponse(path, media_type="application/zip", filename=f"{build['name']}-{build_id}.zip")

    # Front end ###############################################################

    def builds_fragment(request: Request):
        return templates.TemplateResponse(request, "_builds.html", {"builds": [present(b) for b in db.list()]})

    @app.get("/", response_class=HTMLResponse)
    def ui_index(request: Request):
        return templates.TemplateResponse(request, "index.html", {"builds": [present(b) for b in db.list()]})

    @app.get("/ui/builds", response_class=HTMLResponse)
    def ui_builds(request: Request):
        return builds_fragment(request)

    @app.post("/ui/builds", response_class=HTMLResponse)
    def ui_create_build(request: Request, package: UploadFile = File(...), name: str = Form("")):
        # Errors come back as a 200 with the message in the form: htmx does not swap 4xx responses.
        try:
            store_upload(package, name)
        except HTTPException as e:
            return templates.TemplateResponse(request, "_form.html", {"name": name, "error": e.detail})
        return templates.TemplateResponse(request, "_form.html", {}, headers={"HX-Trigger": "refresh"})

    @app.post("/ui/builds/{build_id}/cancel", response_class=HTMLResponse)
    def ui_cancel_build(request: Request, build_id: str):
        db.request_cancel(build_id)
        return builds_fragment(request)

    @app.get("/ui/builds/{build_id}/log", response_class=HTMLResponse)
    def ui_log(request: Request, build_id: str):
        build = get_or_404(build_id)
        return templates.TemplateResponse(request, "_log.html", {"build": build, "text": read_log(build_id, LOG_TAIL)})

    @app.get("/ui/builds/{build_id}/log/text", response_class=HTMLResponse)
    def ui_log_text(build_id: str):
        """Log text polled by the open log panel, until the build is over."""
        build = get_or_404(build_id)
        active = build["status"] in (states.QUEUED, states.RUNNING)
        text = read_log(build_id, LOG_TAIL) or "(empty)"
        return HTMLResponse(str(escape(text)), status_code=200 if active else HTMX_STOP_POLLING)

    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    return app
