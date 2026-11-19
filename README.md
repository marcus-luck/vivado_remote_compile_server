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

## Deployment (Docker)

The server runs on a Linux host as three containers (`compose.yaml`):

- `caddy`: HTTPS on ports 80 and 443 (see "HTTPS on the local network").
- `api`: the API and front end, reachable only from Caddy.
- `worker`: runs the builds, one at a time.

The database, packages, logs and artifacts live in the `data` volume. Vivado
is not in any image: the host's Linux install is mounted read-only into the
worker at `/opt/Xilinx`.

`deploy/buildserverctl` sets up the host and deploys, from a git checkout of
this repository on the host. It runs as a deploy account in group `docker`;
only `provision` needs sudo.

### New server, step by step

Run the commands on the server, as the account that will deploy (not root),
unless a step says otherwise.

1. **Machine.** Ubuntu Server (any recent release: the containers are Ubuntu
   22.04, which Vivado supports) with the OpenSSH server. At least 8 cores,
   16 GB RAM and 150 GB disk; Vivado alone takes 17 GB for Artix-7.
   - The Ubuntu installer gives the root volume only part of the disk. Grow
     it: `sudo lvextend -r -l +100%FREE /dev/ubuntu-vg/ubuntu-lv`.
   - Hyper-V: a Generation 2 VM boots Ubuntu only with the Secure Boot
     template *Microsoft UEFI Certificate Authority*. Use static memory and
     turn automatic checkpoints off. The *Default Switch* is reachable only
     from the Hyper-V host and changes address on reboots; use an *External*
     switch for a fixed LAN address.
   - VirtualBox on a Windows host with Hyper-V enabled (Docker Desktop, WSL2)
     runs on top of Hyper-V and freezes under load: use Hyper-V instead.
     Bridged over Wi-Fi it is also slow; add a NAT adapter for internet
     traffic.

2. **Checkout and provision.**

   ```
   sudo apt-get install -y git curl
   sudo git clone https://github.com/marcus-luck/vivado_remote_compile_server.git /opt/buildserver
   sudo chown -R "$USER": /opt/buildserver
   sudo /opt/buildserver/deploy/buildserverctl provision
   ```

   `provision` installs Docker and adds you to group `docker` (log in again
   afterwards), creates `.env` with a new session secret and token key and
   `deploy/caddy.env`, creates `/var/backups/buildserver` with a nightly
   backup timer, and lists the settings still empty. Running it again changes
   nothing that is set up.

3. **Vivado.** Download the Linux *Unified Installer* (web installer, `.bin`)
   from AMD and copy it to the server. Then:

   ```
   sudo install -d -o "$USER" -g "$USER" /opt/Xilinx
   chmod +x FPGAs_AdaptiveSoCs_Unified_*_Lin64.bin
   ./FPGAs_AdaptiveSoCs_Unified_*_Lin64.bin --noexec --keep --target ~/xinstall
   cd ~/xinstall
   ./xsetup -b ConfigGen          # choose 2 (Vivado), then 1 (ML Standard)
   ```

   Edit `~/.Xilinx/install_config.txt`: `Destination=/opt/Xilinx`, set the
   device families you build for to `:1` (e.g. `Artix-7 FPGAs:1`) and the
   rest, DocNav and Model Composer included, to `:0`. Then sign in to AMD and
   install:

   ```
   ./xsetup -b AuthTokenGen       # your AMD account; not with sudo
   _JAVA_OPTIONS=-Djava.net.preferIPv4Stack=true \
     ./xsetup -b Install -a XilinxEULA,3rdPartyEULA -c ~/.Xilinx/install_config.txt
   ```

   The token lasts about a week. A failed download leaves a broken partial
   file: delete `/opt/Xilinx/Downloads` before trying again. ML Standard needs
   no licence. Install Vivado before step 2's `provision`, or set
   `BUILDSERVER_VIVADO` by hand (step 7). Vivado may not start on the host
   itself (missing locale and libraries); that is fine, builds run it in the
   worker container.

4. **Google sign-in.** In the Google Cloud console, APIs & Services >
   Credentials, create an OAuth client ID of type *Web application*, with
   `https://buildserver.drystone.solutions/auth/callback` as an authorised
   redirect URI.

5. **Certificate.** If the zone is on Cloudflare: Cloudflare dashboard >
   My Profile > API Tokens > Create Token, template *Edit zone DNS*, that
   zone. Otherwise set `CADDY_TLS=internal` in `deploy/caddy.env` and install
   Caddy's root certificate on the clients after the deploy. See "HTTPS on
   the local network".

6. **DNS.** On the local DNS server, point `buildserver.drystone.solutions`
   at the server's LAN address.

7. **Settings.** Fill in what `provision` listed:

   | File | Setting |
   | --- | --- |
   | `.env` | `BUILDSERVER_PUBLIC_URL=https://buildserver.drystone.solutions` |
   | `.env` | `BUILDSERVER_GOOGLE_CLIENT_ID`, `BUILDSERVER_GOOGLE_CLIENT_SECRET` (step 4) |
   | `.env` | `BUILDSERVER_ADMIN_EMAILS`: your Google account |
   | `.env` | `BUILDSERVER_VIVADO`: set by `provision` if Vivado was installed first, else e.g. `/opt/Xilinx/2025.2/Vivado/bin/vivado` |
   | `deploy/caddy.env` | `CLOUDFLARE_API_TOKEN` (step 5), or `CADDY_TLS=internal` |

   Optional: GitHub repository builds (`BUILDSERVER_GITHUB_CLIENT_ID`,
   `BUILDSERVER_GITHUB_CLIENT_SECRET`, see "Repositories and GitHub").
   Keep a copy of `BUILDSERVER_TOKEN_KEY` somewhere safe.

8. **Deploy.**

   ```
   cd /opt/buildserver
   deploy/buildserverctl deploy        # or a branch, e.g. deploy my-branch
   deploy/buildserverctl status
   ```

   The first deploy builds the images and runs the tests: allow about
   15 minutes. `status` shows `api: healthy` when it is up.

9. **Check.** Open `https://buildserver.drystone.solutions` and sign in with
   the admin account. The first certificate takes a minute;
   `deploy/buildserverctl logs caddy` shows its progress ("certificate
   obtained successfully"). Queue a small build to check Vivado. Before DNS
   is in place, test from another machine with
   `curl --resolve buildserver.drystone.solutions:443:<server-ip> https://buildserver.drystone.solutions/healthz`.

### Deploying

```
deploy/buildserverctl deploy                    # main
deploy/buildserverctl deploy v1.2               # a tag, a branch or a SHA
deploy/buildserverctl status
deploy/buildserverctl logs worker
deploy/buildserverctl rollback
```

A deploy:
1. refuses while a build is running (`--force` fails that build);
2. takes a backup;
3. checks out the ref and runs the tests in the worker image, sandbox tests
   included; if they fail, nothing changes;
4. rebuilds and restarts the containers, then waits for the api's
   `/healthz`. If the new version is not healthy within two minutes, it goes
   back to the previous one.

`.deploy-history` records each deploy; `rollback` deploys the one before
the current, without the tests. Run a single worker: builds are serialised
on purpose (Vivado is CPU and memory bound).

### Backups

`deploy/buildserverctl backup` copies the live database and `.env` to
`/var/backups/buildserver` and keeps the newest 14 of each; the timer runs it
every night, and every deploy runs it too. `.env` goes with the database
because without its `BUILDSERVER_TOKEN_KEY` the stored GitHub tokens cannot
be read. Copy the directory off the host as well.

`deploy/buildserverctl restore /var/backups/buildserver/builds-<date>.db`
backs up the current database, stops the api and the worker, checks the file
and puts it in place. Packages, logs and artifacts are not in the backup.

### Build isolation

A build runs code from an uploaded package or a repository, so the worker
does not run it as itself. In the worker container the worker runs as `app`
and starts each build command through `sudo` as `builder`
(`buildserver/sandbox.py`, `deploy/sudoers`):

- `builder` cannot enter `/data`: no database, no GitHub tokens, no other
  build's package, log or artifacts.
- It gets the environment without the `BUILDSERVER_*` variables, and it
  cannot read the worker's environment through `/proc`.
- Every build gets a fresh home and temporary directory under `/work`. After
  the build all `builder` processes are killed and its files are deleted, so
  nothing (a `~/.Xilinx/Vivado/Vivado_init.tcl`, say) carries over to the next
  build.

A build can still use the network and the CPU, and it sees Vivado and its own
sources. Only give accounts to people you trust to run code on the build
host.

The tests of the isolation need the worker image:

```
docker build --target worker-test -t buildserver-test .
docker run --rm --init -e BUILDSERVER_TEST_SANDBOX=1 buildserver-test
```

### Local development

On any machine, without Docker and without the isolation:

```
uv sync
uv run uvicorn --factory buildserver.api:create_app --host 127.0.0.1 --port 8000
uv run python -m buildserver.worker
uv run pytest
```

The front end is at `http://localhost:8000/`, interactive API docs at `/docs`.
Everything except the sign-in page requires a signed-in user.

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

The `caddy` container is the HTTPS endpoint for the host name in
`BUILDSERVER_PUBLIC_URL` (e.g. `https://buildserver.drystone.solutions`), a
name the local DNS server resolves to the build host (`deploy/Caddyfile`).
Use a name under a public domain even though it resolves only locally:
Google refuses redirect URIs on `.local` and other private names.

`CADDY_TLS` in `deploy/caddy.env` picks the certificate:

- **`cloudflare`** (default): Let's Encrypt, through a DNS challenge on
  Cloudflare, which must host the name's zone. The name is not reachable from
  the internet, so this is the only way Let's Encrypt can check it. Create a
  Cloudflare API token from the *Edit zone DNS* template for the zone and put
  it in `CLOUDFLARE_API_TOKEN`. The image (`deploy/caddy/Dockerfile`) is Caddy
  built with the Cloudflare DNS module.
- **`internal`**: Caddy's own certificate authority, for a zone that isn't on
  Cloudflare. Nothing is needed outside the server, but browsers warn until
  each client trusts Caddy's root certificate. `deploy/buildserverctl ca-cert
  > buildserver-ca.crt` prints it after the first deploy; on Windows, install
  it with `certutil -addstore -f Root buildserver-ca.crt` (admin) or by
  double-clicking it, store *Trusted Root Certification Authorities*. The
  root is valid for 10 years and lives in the `caddy_data` volume: keep that
  volume, or every client must trust a new one.

Both ways:

1. Add `<BUILDSERVER_PUBLIC_URL>/auth/callback` to the OAuth client's
   authorised redirect URIs in Google Cloud.
2. Allow inbound TCP 443 (and 80, for the redirect) in the host's firewall.

Caddy renews the certificate by itself. The secrets in `deploy/caddy.env`
stay out of `.env`, which the api and worker containers get.

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
download holds it open, say), the worker's hourly pass removes the
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
repository the user can reach. The token is stored in the database encrypted
with `BUILDSERVER_TOKEN_KEY`, so a copy of the database alone does not give it
away. The worker passes it to git through environment variables only, so it is
not written to `.git/config`, the build log or the build command's
environment. Builds run as a separate account that can read neither the
database nor the key (see "Build isolation"). *Unlink*
deletes the token and revokes it at GitHub. Linking requires a signed-in
session; an API token cannot link an account.

Submodules are fetched with the same token. Submodule URLs on GitHub may use
https or SSH (`git@github.com:...` is rewritten to https); submodules hosted
elsewhere are not supported, since git is only allowed to use https.

## API

`GET /healthz` needs no sign-in: it returns `{"status": "ok"}` if the server
can read its database, else 503. Docker's health check and
`deploy/buildserverctl` use it.

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
| `BUILDSERVER_DATA_DIR` | required | Database, packages, logs, artifacts, and the work directories unless `BUILDSERVER_WORK_DIR` is set (`/data` in Docker) |
| `BUILDSERVER_VIVADO` | required unless `BUILDSERVER_BUILD_COMMAND` is set | Vivado executable |
| `BUILDSERVER_BUILD_SCRIPT` | required | File that must be at the top level of the package |
| `BUILDSERVER_ARTIFACT_DIR` | required | Directory zipped as the artifacts, relative to the package root. `{name}` is the build name |
| `BUILDSERVER_BUILD_COMMAND` | optional | JSON list of arguments, `{name}` is the build name. Replaces the Vivado command line |
| `BUILDSERVER_BUILD_TIMEOUT` | default `21600` | Seconds before a build is killed |
| `BUILDSERVER_MAX_UPLOAD_BYTES` | default `2147483648` | Upload size limit |
| `BUILDSERVER_KEEP_WORKDIR` | default off | Set to `1` to keep the extracted package after the build |
| `BUILDSERVER_GIT` | default `git` | Git executable the worker clones repositories with |
| `BUILDSERVER_RETENTION_DAYS` | default off | Days after a build finishes before the worker deletes its package and artifacts (the log stays) |
| `BUILDSERVER_TOKEN_KEY` | required with GitHub | Key the GitHub tokens are encrypted with in the database (both processes) |
| `BUILDSERVER_BUILD_USER` | Linux only, default off | Account the build command runs as through sudo (`builder` in Docker); see "Build isolation" |
| `BUILDSERVER_WORK_DIR` | default `<data dir>/work` | Where the work directories are made; outside the data directory when builds run as another account (`/work` in Docker) |

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

In local development on Windows, keep the data directory path short: Vivado fails on paths over 260 characters.

## Tests

```
uv run pytest
```

The tests use a small Python script in place of Vivado, a fake of the GitHub
API, and local git repositories in place of github.com (git must be on PATH).
The build isolation tests run only in the worker image; see "Build
isolation".

## Style

Python code follows the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html),
enforced with ruff:

```
uv run ruff format .
uv run ruff check .
```
