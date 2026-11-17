# Firmware build server

Clients upload a build package (a zip of a firmware project), the server queues it,
a worker builds it, and the build artifacts are offered back as a zip.

- **API + front end**: FastAPI (`buildserver/api.py`). The front end is server-rendered
  (Jinja templates in `buildserver/templates/`) and kept live with htmx; no Node, no build step
- **Task queue**: the `builds` table in SQLite (`buildserver/db.py`); queued rows are claimed oldest first
- **Worker**: a separate process (`buildserver/worker.py`) that runs one build at a time
- **Storage**: everything lives under the data directory (`buildserver/storage.py`)
- **Users**: Google sign-in over OAuth, a `users` table, and a user management API (`buildserver/auth.py`)
- **Repositories**: users link a GitHub account and build branches or tags of its repositories (`buildserver/repos.py`, `github.py`, `git.py`)

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
(`gige_core_udp_rgmii`, `_SOURCE/common_firmware`, ...). To build straight from
git instead, connect the repository (see below).

The worker extracts the package and runs, in that directory:

```
vivado -mode batch -source build.tcl -tclargs <build name>
```

which matches the `build.tcl` convention of the firmware projects: output goes to
`bitfiles/generated/<build name>/`. That directory, plus `build.log`, becomes the
artifact zip. It is collected for failed builds as well, so timing and DRC reports
of a build that died in routing are still downloadable.

## Who sees a build

A build is visible only to the user who queued it and to administrators. Only
they see it in the build table and `GET /api/builds`, and only they can read
its log, download its artifacts, cancel or delete it. For anyone else its URLs
answer 404, as if it did not exist. This holds for uploaded packages and for
builds from a repository: a build an admin queues from your repository is the
admin's.

## Deleting builds and retention

*Delete* in the build table (or `DELETE /api/builds/{id}`) removes a build and
all its files. A running build has to be cancelled first. If a file cannot be deleted (a
download holds it open on Windows, say), the worker's hourly pass removes the
leftover directory later.

With `BUILDSERVER_RETENTION_DAYS` set, the worker deletes the package and the
artifact zip of every build that finished more than that many days ago. It
checks at start-up and then hourly, between builds. The build stays in the list
with its log and is marked *files expired*. Unset or `0` keeps files forever.

## Repositories and GitHub

Instead of uploading a package, a user can link their GitHub account and
connect repositories of it. Each connected repository has a selected branch or
tag; *Build* queues a build of it, *Update* refetches the branches and tags from
GitHub, *Delete* disconnects it (its builds are kept, its queued builds cancelled). The worker makes a
shallow clone of the branch tip or the tagged commit, with submodules, and
builds it like an extracted package: `build.tcl` must be at the top of the
repository. The build records the commit it built.

Repositories are per user: you see the ones you connected, admins see all.
A build of someone else's repository by an admin still clones with the token of
the repository's owner.

Setup, once:

1. On GitHub, Settings > Developer settings > OAuth Apps > New OAuth App. Set
   the callback URL to `<BUILDSERVER_PUBLIC_URL>/auth/github/callback`, e.g.
   `https://buildserver.drystone.solutions/auth/github/callback`. A host name
   only the local network resolves is fine: GitHub only redirects the browser.
2. Put its client ID and a client secret in `.env` as
   `BUILDSERVER_GITHUB_CLIENT_ID` and `BUILDSERVER_GITHUB_CLIENT_SECRET`, and
   restart the API server.
3. If the repositories belong to an organisation that restricts OAuth Apps, an
   organisation owner has to approve the app (GitHub asks on the consent page).

Users then press *Connect GitHub* in the *Repositories* section, and add
repositories by searching their own or pasting a URL (`https://github.com/owner/repo`,
`git@github.com:owner/repo.git` or `owner/repo`).

**What the token can do.** OAuth Apps have no read-only scope for private
repositories, so the link asks for `repo`: read *and write* access to every
repository the user can reach. The token is stored in the database. The worker
passes it to git through environment variables only, so it is not written to
`.git/config`, the build log or the build command's environment; but builds run
uploaded code as the same Windows user that can read the database. *Unlink*
deletes the token and revokes it at GitHub. Linking requires a signed-in
session; an API token cannot link an account.

Submodules are fetched with the same token. Submodule URLs on GitHub may use
https or SSH (`git@github.com:...` is rewritten to https); submodules hosted
elsewhere are not supported, since git is only allowed to use https.

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
| `GET` | `/api/builds` | List your builds (everybody's for an admin), newest first |
| `GET` | `/api/builds/{id}` | One build |
| `GET` | `/api/builds/{id}/log?tail=65536` | Build log as text (`tail=0` for all of it) |
| `GET` | `/api/builds/{id}/artifacts` | Artifact zip |
| `POST` | `/api/builds/{id}/cancel` | Cancel a queued or running build |
| `DELETE` | `/api/builds/{id}` | Delete a build and its files; 409 while running |

Every `/api/builds/{id}...` route answers 404 unless you queued the build or
are an admin. Status is one of `queued`, `running`, `succeeded`, `failed`,
`cancelled`. Each
build records the email (or service account name) of the user who queued it in
`created_by`. A build from a repository has an empty `package_filename` and
sets `repo_full_name`, `ref`, `ref_type` (`branch` or `tag`) and, once cloned,
`commit_sha`. `files_expired_at` is set once retention has deleted the
package and artifacts.

### Repositories

| Method | Path | |
| --- | --- | --- |
| `GET` | `/api/github/repos?q=` | Repositories the linked GitHub account can read, filtered by `q` |
| `GET` | `/api/repos` | Your connected repositories (admins: everybody's) |
| `POST` | `/api/repos` | Connect one. JSON: `{"repo": <URL or owner/repo>, "name": ...}`. The default branch is selected |
| `GET` | `/api/repos/{id}` | One repository, with its `branches` and `tags` |
| `POST` | `/api/repos/{id}/fetch` | Refetch branches and tags from GitHub |
| `PATCH` | `/api/repos/{id}` | Select what to build. JSON: `{"ref": ..., "ref_type": "branch" \| "tag"}` |
| `DELETE` | `/api/repos/{id}` | Disconnect; builds are kept, queued ones cancelled |
| `POST` | `/api/repos/{id}/builds` | Queue a build. Optional JSON: `{"name": ..., "ref": ..., "ref_type": ...}`; defaults are the repository's name and selected ref |

Linking is done in the browser: `/auth/github/login` and `POST /auth/github/unlink`.
`/api/me` shows the linked account as `github_login`.

### Users

| Method | Path | |
| --- | --- | --- |
| `GET` | `/api/me` | The authenticated user |
| `GET` | `/api/users` | List users (admins) |
| `POST` | `/api/users` | Add a user. JSON: `{"email": ..., "role": "user" \| "admin", "is_service": false}` (admins) |
| `GET` | `/api/users/{id}` | One user (admins) |
| `PATCH` | `/api/users/{id}` | Change `role` and/or `is_active` (admins) |
| `DELETE` | `/api/users/{id}` | Remove a user and their tokens; their builds are kept, queued builds of their repositories cancelled (admins) |

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
| `BUILDSERVER_GIT` | default `git` | Git executable the worker clones repositories with |
| `BUILDSERVER_RETENTION_DAYS` | default off | Days after a build finishes before the worker deletes its package and artifacts (the log stays) |

Read by the API server only:

| Variable | | |
| --- | --- | --- |
| `BUILDSERVER_PUBLIC_URL` | required | URL users open the server at; the OAuth redirect URI is derived from it |
| `BUILDSERVER_GOOGLE_CLIENT_ID` | required | Google OAuth client ID |
| `BUILDSERVER_GOOGLE_CLIENT_SECRET` | required | Google OAuth client secret |
| `BUILDSERVER_SESSION_SECRET` | required | Key that signs the session cookie |
| `BUILDSERVER_ADMIN_EMAILS` | required | Comma-separated accounts that are always administrators |
| `BUILDSERVER_ALLOWED_DOMAINS` | optional | Comma-separated domains whose accounts may sign in as plain users |
| `BUILDSERVER_GITHUB_CLIENT_ID` | optional | Client ID of the GitHub OAuth App; enables repositories |
| `BUILDSERVER_GITHUB_CLIENT_SECRET` | optional | Client secret of the GitHub OAuth App |

On Windows keep the data directory path short: Vivado fails on paths over 260 characters.

## Tests

```
uv run pytest
```

The tests use a small Python script in place of Vivado, a fake of the GitHub
API, and local git repositories in place of github.com (git must be on PATH).

## Style

Python code follows the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html),
enforced with ruff:

```
uv run ruff format .
uv run ruff check .
```
