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
sign in, serve the server under a host name over HTTPS and set
`BUILDSERVER_PUBLIC_URL` to that address; bare IP addresses such as
`http://192.168.x.x:8000` are refused by Google. See below.

### HTTPS on the local network

`deploy/Caddyfile` puts Caddy in front of uvicorn as the HTTPS endpoint for
`buildserver.drystone.solutions`, a name the local DNS server resolves to the
build host. The name is not reachable from the internet, so Caddy gets its
Let's Encrypt certificate through a DNS challenge on Cloudflare, which hosts the
`drystone.solutions` zone.

1. Get Caddy with the Cloudflare DNS module (the plain release lacks it). The
   download service at caddyserver.com builds it on demand but can stall; with
   Go installed, build it instead:
   `go run github.com/caddyserver/xcaddy/cmd/xcaddy@latest build --with github.com/caddy-dns/cloudflare`
   On the current build host it is `%LOCALAPPDATA%\Programs\Caddy\caddy.exe`.
2. Create a Cloudflare API token from the *Edit zone DNS* template for the
   `drystone.solutions` zone. Copy `deploy/caddy.env.template` to
   `deploy/caddy.env` and put the token there. Keep it out of `.env`: variables
   in `.env` are passed on to build commands.
3. Add `https://buildserver.drystone.solutions/auth/callback` to the OAuth
   client's authorised redirect URIs in Google Cloud.
4. In `.env`, set `BUILDSERVER_PUBLIC_URL=https://buildserver.drystone.solutions`.
5. Allow inbound TCP 443 in Windows Firewall (admin PowerShell):
   `New-NetFirewallRule -DisplayName "Build server HTTPS" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow -Profile Private`
6. Run uvicorn on loopback only, so all traffic goes through Caddy, and start
   Caddy from the repository root:

   ```
   uv run uvicorn --factory buildserver.api:create_app --host 127.0.0.1 --port 8000
   caddy run --config deploy/Caddyfile --envfile deploy/caddy.env
   ```

Caddy renews the certificate by itself.

Who may sign in:

- accounts in `BUILDSERVER_ADMIN_EMAILS`: always, as administrators;
- accounts an administrator added through `POST /api/users`;
- if `BUILDSERVER_ALLOWED_DOMAINS` is set, any account of those domains, as a plain user.

Everyone else is refused with a message on the sign-in page. Only verified Google
emails are accepted. Users are `admin` or `user`; both can queue, cancel and
download builds, only admins can manage users. Deactivating a user ends their
session at once and stops their API tokens working.

Robots (CI jobs, scripts not tied to a person) get a **service account**: a user
an admin adds with `"is_service": true`, named instead of having an email. It
cannot sign in with Google; it authenticates only with API tokens an admin
creates for it (see below). Builds it queues show its name in `created_by`.

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

### Authentication

Every route accepts either the session cookie set by signing in (so the
interactive docs at `/docs` work in the browser) or an API token:

```
curl -H "Authorization: Bearer bst_..." http://localhost:8000/api/me
curl -H "Authorization: Bearer bst_..." -F package=@proj.zip -F name=nightly \
     http://localhost:8000/api/builds
```

Create a personal token in the *API tokens* section of the web page, or with
`POST /api/tokens` while signed in. The token is shown once, at creation; only
its hash is stored. A token acts as its user, with the same role, but cannot
create further tokens. It stops working when revoked, when it expires (if
created with an expiry), or when its user is deactivated or deleted. A request
with an invalid `Authorization: Bearer` header is refused even if it also has a
valid session. In `/docs`, *Authorize* takes a token.

For a robot, an admin adds a service account and creates its token (both from a
signed-in session, e.g. in `/docs`):

```
POST /api/users                {"email": "ci-nightly", "is_service": true}
POST /api/users/{id}/tokens    {"name": "gitlab runner", "expires_in_days": 365}
```

| Method | Path | |
| --- | --- | --- |
| `GET` | `/api/tokens` | Your tokens (never the token itself) |
| `POST` | `/api/tokens` | Create a token. JSON: `{"name": ..., "expires_in_days": 1-3650 \| null}`. Session only |
| `DELETE` | `/api/tokens/{id}` | Revoke one of your tokens (admins: any token) |
| `GET` | `/api/users/{id}/tokens` | A user's tokens (admins) |
| `POST` | `/api/users/{id}/tokens` | Create a token for a user, e.g. a service account (admins, session only) |

### Builds

| Method | Path | |
| --- | --- | --- |
| `POST` | `/api/builds` | Upload a package. Multipart form: `package` (zip), optional `name` |
| `GET` | `/api/builds` | List builds, newest first |
| `GET` | `/api/builds/{id}` | One build |
| `GET` | `/api/builds/{id}/log?tail=65536` | Build log as text (`tail=0` for all of it) |
| `GET` | `/api/builds/{id}/artifacts` | Artifact zip |
| `POST` | `/api/builds/{id}/cancel` | Cancel a queued or running build |

Status is one of `queued`, `running`, `succeeded`, `failed`, `cancelled`. Each
build records the email (or service account name) of the user who queued it in
`created_by`.

### Users

| Method | Path | |
| --- | --- | --- |
| `GET` | `/api/me` | The authenticated user |
| `GET` | `/api/users` | List users (admins) |
| `POST` | `/api/users` | Add a user. JSON: `{"email": ..., "role": "user" \| "admin", "is_service": false}` (admins) |
| `GET` | `/api/users/{id}` | One user (admins) |
| `PATCH` | `/api/users/{id}` | Change `role` and/or `is_active` (admins) |
| `DELETE` | `/api/users/{id}` | Remove a user and their tokens; their builds are kept (admins) |

Admins cannot demote, deactivate or delete their own account.

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
