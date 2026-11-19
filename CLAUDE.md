# Firmware build server

Clients upload a zipped firmware project; the server queues it, a worker builds it
with Vivado (`build.tcl`), and the artifacts are downloadable as a zip.

## Architecture

Two processes (the `api` and `worker` containers) sharing one `.env` and one data directory (`BUILDSERVER_DATA_DIR`, `/data` in Docker):

- `buildserver/api.py`: FastAPI app. `/api/builds` is the JSON API (upload, list, log, artifacts, cancel, delete); a build is visible only to its creator (`created_by`) and admins, others get 404 (`can_access`, `get_or_404`); `/` and `/ui/...` serve the front end.
- `buildserver/worker.py`: single worker, one build at a time. Extracts the package (or clones the repository's branch or tag with submodules, using the repository owner's GitHub token), runs the build command, zips `bitfiles/generated/<name>/` plus the log. Hourly, between builds, deletes the package and artifacts of builds finished more than `BUILDSERVER_RETENTION_DAYS` ago (row and log stay, `files_expired_at` set). The same pass deletes build directories whose row is gone (a delete that could not remove every file). Failures stop a build by raising `BuildStoppedError`.
- `buildserver/auth.py`: Google OAuth sign-in (Authlib), session cookie, API tokens (`Authorization: Bearer bst_...`, sha256-hashed in the `api_tokens` table), service accounts (users with `is_service`, token-only), `current_user`/`session_user`/`admin_user` dependencies, `/api/me`, `/api/tokens` and admin-only `/api/users` CRUD. All other routes require a signed-in user or a token; creating tokens requires a session.
- `buildserver/repos.py`: `/api/repos` and `/ui/repos` routes. Users connect GitHub repositories (per user; admins see all), select a branch or tag, and queue builds of them. The rules are plain module-level functions raising `NotLinkedError`, `AlreadyConnectedError`, `ValueError` or `github.GitHubError`; the routes map those to HTTP codes. GitHub accounts are linked in `auth.py` (`/auth/github/...`, OAuth App, `repo` scope, session only).
- `buildserver/github.py`: the GitHub REST calls (user, repos, branches, tags, revoke), over an `httpx.Client` kept on `app.state.github_http`.
- `buildserver/git.py`: the clone command and its environment. The token goes to git only through `GIT_CONFIG_*` environment variables, never a URL, command line or log.
- `buildserver/names.py`: valid build names and defaults.
- `buildserver/sandbox.py`: with `BUILDSERVER_BUILD_USER` (Linux), the build command runs as that account through `sudo -n -u <user> -- env -i ... sh -c 'umask 002; exec "$@"'`, with a fresh home `<work_root>/<id>.home`. Killing is `sudo -u <user> kill -KILL -1`. Before and after each build the worker kills the account's processes, deletes its temp files and empties the work root (`BUILDSERVER_WORK_DIR`, outside the data dir). `tests/test_sandbox.py` fakes sudo; its `sandbox` tests need the worker image.
- `buildserver/crypto.py`: Fernet encryption of the GitHub tokens in `github_links` (`BUILDSERVER_TOKEN_KEY`; `db.Database` takes the cipher, encrypts on write, decrypts on read, migrates plaintext rows on open). The build command gets `worker.build_env()`: the environment without `BUILDSERVER_*` and `GIT_CONFIG_*`.
- `buildserver/db.py`: SQLite `builds` table, which is also the task queue (oldest `queued` row is claimed), `users`, `api_tokens`, `github_links` and `repos` tables.
- `buildserver/storage.py`: on-disk layout and zip handling.
- `buildserver/admin.py`: `python -m buildserver.admin busy|backup|restore`, run inside the containers by `deploy/buildserverctl`; backup and restore take `-` to stream the database through stdout/stdin.
- `deploy/buildserverctl`: bash, on the host, from its git checkout (e.g. `/opt/buildserver`). `provision` (sudo: Docker, `.env` with generated secrets, `/var/backups/buildserver`, the `deploy/buildserver-backup.*` systemd timer), `deploy [ref]` (refuses while a build runs, backup, checkout, `test`, `compose up`, waits for `/healthz`, rolls back on failure, appends to git-ignored `.deploy-history`), `rollback`, `status`, `logs`, `backup`, `restore`, `test`, `ca-cert`. Its body is only functions plus a last `main "$@"; exit` line, so a checkout can replace the file mid-deploy.
- `buildserver/config.py`: settings from `BUILDSERVER_*` variables in `.env` (see `.env.template`); the real environment overrides the file. No paths are hardcoded in the code: every path setting is required and comes from `.env`.
- `buildserver/templates/`: htmx front end. `index.html` is the page; the `_*.html` fragments are what `/ui/...` returns for htmx to swap in. No build step.
- `buildserver/static/`: stylesheet, vendored htmx, a few lines of JS.

## Review agent

`.claude/agents/uncle-bob.md`: read-only reviewer in the persona of Uncle Bob
(Clean Code, SOLID, TDD, Clean Architecture). Use it for reviews, refactoring
advice and design questions; it proposes changes, it does not make them.

## Style

Python follows the Google Python Style Guide: 80 columns, Google docstrings
(`Args:`, `Returns:`, `Raises:`) on every module, class and function, absolute
imports of modules rather than names (`from buildserver import db`, then
`db.Database`), one import per line. Ruff enforces it; run both ruff commands
before finishing a change.

## Commands

Deployed on a Linux host with Docker (`compose.yaml`, `Dockerfile`): `caddy`
(`deploy/caddy/Dockerfile`) serves the host of `BUILDSERVER_PUBLIC_URL`
(e.g. `https://buildserver.drystone.solutions`, local DNS only) with a
Let's Encrypt certificate through a Cloudflare DNS challenge or, with
`CADDY_TLS=internal` in git-ignored `deploy/caddy.env`, Caddy's own CA
(`buildserverctl ca-cert` prints its root), and proxies to `api:8000`; `worker` mounts the host's Linux Vivado at
`/opt/Xilinx` read-only and runs builds as `builder` (`deploy/sudoers`). Both
share the `data` volume (0700, user `app`). Google sign-in needs HTTPS, so
don't publish the api port.

```
sudo deploy/buildserverctl provision
deploy/buildserverctl deploy [ref]
deploy/buildserverctl status
deploy/buildserverctl logs worker
deploy/buildserverctl backup
docker run --rm -v "$PWD:/mnt" -w /mnt koalaman/shellcheck:stable deploy/buildserverctl
docker build --target worker-test -t buildserver-test .
docker run --rm --init -e BUILDSERVER_TEST_SANDBOX=1 buildserver-test
uv run uvicorn --factory buildserver.api:create_app --host 127.0.0.1 --port 8000
uv run python -m buildserver.worker
uv run pytest
uv run ruff format .
uv run ruff check .
```

Tests use a Python script in place of Vivado, and fake Google's token response
(`sign_in` fixture in `tests/conftest.py`); everything after Google's redirect runs for real.
`tests/test_repos.py` fakes the GitHub API with `httpx.MockTransport` and clones
from local bare repositories (`git.GITHUB_URL` monkeypatched). See `README.md` for the API and configuration.
