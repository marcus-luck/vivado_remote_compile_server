# Firmware build server

Clients upload a zipped firmware project; the server queues it, a worker builds it
with Vivado (`build.tcl`), and the artifacts are downloadable as a zip.

## Architecture

Two processes sharing one `.env` and one data directory (`BUILDSERVER_DATA_DIR`, default `./data`):

- `buildserver/api.py`: FastAPI app. `/api/builds` is the JSON API (upload, list, log, artifacts, cancel); `/` and `/ui/...` serve the front end.
- `buildserver/worker.py`: single worker, one build at a time. Extracts the package, runs the build command, zips `bitfiles/generated/<name>/` plus the log.
- `buildserver/db.py`: SQLite `builds` table, which is also the task queue (oldest `queued` row is claimed).
- `buildserver/storage.py`: on-disk layout and zip handling.
- `buildserver/config.py`: settings from `BUILDSERVER_*` variables in `.env` (see `.env.template`); the real environment overrides the file. No paths are hardcoded in the code: every path setting is required and comes from `.env`.
- `buildserver/templates/`: htmx front end. `index.html` is the page; the `_*.html` fragments are what `/ui/...` returns for htmx to swap in. No build step.
- `buildserver/static/`: stylesheet, vendored htmx, a few lines of JS.

## Style

Python follows the Google Python Style Guide: 80 columns, Google docstrings
(`Args:`, `Returns:`, `Raises:`) on every module, class and function, absolute
imports of modules rather than names (`from buildserver import db`, then
`db.Database`), one import per line. Ruff enforces it; run both ruff commands
before finishing a change.

## Commands

```
uv run uvicorn --factory buildserver.api:create_app --port 8000
uv run python -m buildserver.worker
uv run pytest
uv run ruff format .
uv run ruff check .
```

Tests use a Python script in place of Vivado. See `README.md` for the API and configuration.
