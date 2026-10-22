# Firmware build server

Clients upload a zipped firmware project; the server queues it, a worker builds it
with Vivado (`build.tcl`), and the artifacts are downloadable as a zip.

## Architecture

Two processes sharing one `.env` and one data directory (`BUILDSERVER_DATA_DIR`, default `./data`):

- `buildserver/api.py`: FastAPI app. `/api/builds` is the JSON API (upload, list, log, artifacts, cancel); `/` and `/ui/...` serve the front end.
- `buildserver/worker.py`: single worker, one build at a time. Extracts the package, runs the build command, zips `bitfiles/generated/<name>/` plus the log.
- `buildserver/auth.py`: Google OAuth sign-in (Authlib), session cookie, `current_user`/`admin_user` dependencies, `/api/me` and admin-only `/api/users` CRUD. All other routes require a signed-in user.
- `buildserver/db.py`: SQLite `builds` table, which is also the task queue (oldest `queued` row is claimed), and `users` table.
- `buildserver/storage.py`: on-disk layout and zip handling.
- `buildserver/config.py`: settings from `BUILDSERVER_*` variables in `.env` (see `.env.template`); the real environment overrides the file. No paths are hardcoded in the code: every path setting is required and comes from `.env`.
- `buildserver/templates/`: htmx front end. `index.html` is the page; the `_*.html` fragments are what `/ui/...` returns for htmx to swap in. No build step.
- `buildserver/static/`: stylesheet, vendored htmx, a few lines of JS.

## Progress tracking

Tasks live in the ClickUp list FW_build_server; use the `clickup-tasks` skill
(`.claude/skills/clickup-tasks/`) to list, add, start and check them off. Hooks
in `.claude/settings.json` (script `.claude/hooks/clickup_tasks.py`) ask for a
task list at session start and for task updates after replies that edit files.

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

Tests use a Python script in place of Vivado, and fake Google's token response
(`sign_in` fixture in `tests/conftest.py`); everything after Google's redirect runs for real. See `README.md` for the API and configuration.
