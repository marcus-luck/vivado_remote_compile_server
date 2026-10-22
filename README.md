# Firmware build server

Clients upload a build package (a zip of a firmware project), the server queues it,
a worker builds it, and the build artifacts are offered back as a zip.

- **API + front end**: FastAPI (`buildserver/api.py`). The front end is server-rendered
  (Jinja templates in `buildserver/templates/`) and kept live with htmx; no Node, no build step
- **Task queue**: the `builds` table in SQLite (`buildserver/db.py`); queued rows are claimed oldest first
- **Worker**: a separate process (`buildserver/worker.py`) that runs one build at a time
- **Storage**: everything lives under the data directory (`buildserver/storage.py`)
- **Users**: Google sign-in over OAuth, a `users` table, and a user management API (`buildserver/auth.py`)

## Running

Two processes, started from this directory:

```
uv sync
uv run uvicorn --factory buildserver.api:create_app --host 0.0.0.0 --port 8000
uv run python -m buildserver.worker
```

The front end is at `http://<host>:8000/`, interactive API docs at `/docs`.

Run a single worker: builds are serialised on purpose (Vivado is CPU, memory and
licence bound), and a starting worker marks any build still `running` as failed.

Everything except the sign-in page requires a signed-in user. A build package
still runs arbitrary code on the build host, so only give accounts to people you
would trust with a shell on it.

## Users and sign-in

Users sign in with their Google account. Setup, once:

1. In the Google Cloud console, APIs & Services > Credentials, create an OAuth
   client ID of type *Web application*.
2. Add `<BUILDSERVER_PUBLIC_URL>/auth/callback` as an authorised redirect URI,
   e.g. `http://localhost:8000/auth/callback`.
3. Put the client ID and secret, the public URL, a session secret and your own
   Google account (as `BUILDSERVER_ADMIN_EMAILS`) in `.env`.

Google only accepts `http://` redirect URIs for `localhost`. For other machines to
sign in, serve the server under a host name over HTTPS (a reverse proxy in front of
uvicorn, for example) and set `BUILDSERVER_PUBLIC_URL` to that address; bare IP
addresses such as `http://192.168.x.x:8000` are refused by Google.

Who may sign in:

- accounts in `BUILDSERVER_ADMIN_EMAILS`: always, as administrators;
- accounts an administrator added through `POST /api/users`;
- if `BUILDSERVER_ALLOWED_DOMAINS` is set, any account of those domains, as a plain user.

Everyone else is refused with a message on the sign-in page. Only verified Google
emails are accepted. Users are `admin` or `user`; both can queue, cancel and
download builds, only admins can manage users. Deactivating a user ends their
session at once.

## Build packages

A package is a zip of the project with `build.tcl` at its top level (or inside a
single top-level folder). Include everything the build reads, submodules too
(`gige_core_udp_rgmii`, `_SOURCE/common_firmware`, ...); the server does not run git.

The worker extracts the package and runs, in that directory:

```
vivado -mode batch -source build.tcl -tclargs <build name>
```

which matches the `build.tcl` convention of the firmware projects: output goes to
`bitfiles/generated/<build name>/`. That directory, plus `build.log`, becomes the
artifact zip. It is collected for failed builds as well, so timing and DRC reports
of a build that died in routing are still downloadable.

## API

| Method | Path | |
| --- | --- | --- |
| `POST` | `/api/builds` | Upload a package. Multipart form: `package` (zip), optional `name` |
| `GET` | `/api/builds` | List builds, newest first |
| `GET` | `/api/builds/{id}` | One build |
| `GET` | `/api/builds/{id}/log?tail=65536` | Build log as text (`tail=0` for all of it) |
| `GET` | `/api/builds/{id}/artifacts` | Artifact zip |
| `POST` | `/api/builds/{id}/cancel` | Cancel a queued or running build |

Status is one of `queued`, `running`, `succeeded`, `failed`, `cancelled`. Each
build records the email of the user who queued it in `created_by`.

| Method | Path | |
| --- | --- | --- |
| `GET` | `/api/me` | The signed-in user |
| `GET` | `/api/users` | List users (admins) |
| `POST` | `/api/users` | Add a user. JSON: `{"email": ..., "role": "user" \| "admin"}` (admins) |
| `GET` | `/api/users/{id}` | One user (admins) |
| `PATCH` | `/api/users/{id}` | Change `role` and/or `is_active` (admins) |
| `DELETE` | `/api/users/{id}` | Remove a user; their builds are kept (admins) |

Admins cannot demote, deactivate or delete their own account.

All routes authenticate with the session cookie set by signing in, so the API is
easiest to use from the browser: the interactive docs at `/docs` work once you
are signed in. There are no API tokens yet, so scripts (curl) cannot
authenticate.

The `/ui/...` routes return the HTML fragments the front end swaps into the page;
they are not meant for clients.

## Configuration

Configuration lives in a `.env` file in the directory the processes are started
from. Copy `.env.template` to `.env` and edit it. No path has a default in the
code: the processes refuse to start if a required variable is missing.
Variables set in the real environment win over the file, and any other variable
in the file (a Vivado licence server, say) is passed on to the build command.

Read by both processes:

| Variable | | |
| --- | --- | --- |
| `BUILDSERVER_DATA_DIR` | required | Database, packages, logs, artifacts, work directories |
| `BUILDSERVER_VIVADO` | required unless `BUILDSERVER_BUILD_COMMAND` is set | Vivado executable |
| `BUILDSERVER_BUILD_SCRIPT` | required | File that must be at the top level of the package |
| `BUILDSERVER_ARTIFACT_DIR` | required | Directory zipped as the artifacts, relative to the package root. `{name}` is the build name |
| `BUILDSERVER_BUILD_COMMAND` | optional | JSON list of arguments, `{name}` is the build name. Replaces the Vivado command line |
| `BUILDSERVER_BUILD_TIMEOUT` | default `21600` | Seconds before a build is killed |
| `BUILDSERVER_MAX_UPLOAD_BYTES` | default `2147483648` | Upload size limit |
| `BUILDSERVER_KEEP_WORKDIR` | default off | Set to `1` to keep the extracted package after the build |

Read by the API server only:

| Variable | | |
| --- | --- | --- |
| `BUILDSERVER_PUBLIC_URL` | required | URL users open the server at; the OAuth redirect URI is derived from it |
| `BUILDSERVER_GOOGLE_CLIENT_ID` | required | Google OAuth client ID |
| `BUILDSERVER_GOOGLE_CLIENT_SECRET` | required | Google OAuth client secret |
| `BUILDSERVER_SESSION_SECRET` | required | Key that signs the session cookie |
| `BUILDSERVER_ADMIN_EMAILS` | required | Comma-separated accounts that are always administrators |
| `BUILDSERVER_ALLOWED_DOMAINS` | optional | Comma-separated domains whose accounts may sign in as plain users |

On Windows keep the data directory path short: Vivado fails on paths over 260 characters.

## Tests

```
uv run pytest
```

The tests use a small Python script in place of Vivado.

## Style

Python code follows the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html),
enforced with ruff:

```
uv run ruff format .
uv run ruff check .
```
