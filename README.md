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

- `caddy`: HTTPS on ports 80 and 443. See "HTTPS on the local network".
- `api`: the API and the front end. Only Caddy can reach it.
- `worker`: runs the builds, one at a time.

The `data` volume holds the database, packages, logs and artifacts. The
images do not contain Vivado. The worker mounts the host's Linux Vivado
install read-only at `/opt/Xilinx`.

`deploy/buildserverctl` sets up the host and deploys. It runs from a git
checkout of this repository on the host, as a deploy account in group
`docker`. Only `provision` needs sudo.

### New server, step by step

Do the steps in sequence. Run the commands on the server, as the deploy
account (not root), unless the step tells you differently. In the commands,
`<host>` is the host name of the server, e.g.
`buildserver.drystone.solutions`.

#### 1. Prepare the machine

1. Install Ubuntu Server (any recent release) with the OpenSSH server.
   - Minimum: 8 cores, 16 GB RAM, 150 GB disk. Vivado for Artix-7 uses
     17 GB.
   - The containers use Ubuntu 22.04, which Vivado supports.
2. Give the root volume all of the disk. The Ubuntu installer uses only
   part of it:

   ```
   sudo lvextend -r -l +100%FREE /dev/ubuntu-vg/ubuntu-lv
   ```

3. Make sure that the deploy account has a home directory. If it does not,
   make one:

   ```
   sudo mkdir -p /home/<account>
   sudo cp -r /etc/skel/. /home/<account>/
   sudo chown -R <account>: /home/<account>
   ```

4. Give the machine a fixed LAN address, e.g. with a DHCP reservation.

On a Hyper-V host:

- Use a Generation 2 VM with the Secure Boot template *Microsoft UEFI
  Certificate Authority*. Ubuntu does not boot with the default template.
- Use static memory. Turn automatic checkpoints off.
- Connect the VM to an *External* switch on a wired adapter. Only the
  Hyper-V host can reach the *Default Switch*, and its address changes when
  the host restarts.

> **CAUTION:** Do not use VirtualBox on a Windows host with Hyper-V enabled
> (Docker Desktop or WSL2 enable it). VirtualBox then runs on top of
> Hyper-V and freezes under load. Use Hyper-V.

#### 2. Get the code

```
sudo apt-get install -y git curl
sudo git clone https://github.com/marcus-luck/vivado_remote_compile_server.git /opt/buildserver
sudo chown -R "$USER": /opt/buildserver
```

#### 3. Install Vivado

1. Download the Linux *Unified Installer* (web installer, `.bin`) from AMD.
   Copy it to the home directory on the server.
2. Extract the installer and make the install directory:

   ```
   sudo install -d -o "$USER" -g "$USER" /opt/Xilinx
   chmod +x FPGAs_AdaptiveSoCs_Unified_*_Lin64.bin
   ./FPGAs_AdaptiveSoCs_Unified_*_Lin64.bin --noexec --keep --target ~/xinstall
   ```

3. Make the install configuration. Select 2 (Vivado), then 1 (ML Standard):

   ```
   cd ~/xinstall
   ./xsetup -b ConfigGen
   ```

   To install the same devices as an existing server, copy its
   `~/.Xilinx/install_config.txt` instead.
4. Edit `~/.Xilinx/install_config.txt`:
   - Set `Destination=/opt/Xilinx`.
   - Set the device families that you build for to `:1`, e.g.
     `Artix-7 FPGAs:1`.
   - Set all other entries to `:0`, DocNav and Model Composer included.
5. Sign in to AMD with your AMD account:

   > **CAUTION:** Do not use sudo for this command. With sudo, the token
   > goes to `/root/.Xilinx` and the install cannot find it.

   ```
   ./xsetup -b AuthTokenGen
   ```

   The token is valid for approximately one week.
6. Start the install. It downloads approximately 17 GB:

   ```
   _JAVA_OPTIONS=-Djava.net.preferIPv4Stack=true \
     ./xsetup -b Install -a XilinxEULA,3rdPartyEULA -c ~/.Xilinx/install_config.txt
   ```

7. Make sure that the install is complete:

   ```
   ls /opt/Xilinx/*/Vivado/bin/vivado
   ```

If the download fails:

1. Make sure that the install did not complete (step 7). A second install
   of the same version fails.
2. Delete `/opt/Xilinx/Downloads`. A failed download leaves a broken
   partial file there.
3. Do step 6 again.

ML Standard needs no licence. Vivado possibly does not start on the host
itself, because the host does not have its locale and libraries. This is
correct: the builds run Vivado in the worker container.

#### 4. Provision the host

```
sudo /opt/buildserver/deploy/buildserverctl provision
```

`provision` does these tasks:

- It installs Docker and adds you to group `docker`. Log out and log in
  again after the first run.
- It makes `.env` with a new session secret and token key, and
  `deploy/caddy.env`.
- It finds Vivado under `/opt/Xilinx` and sets `BUILDSERVER_VIVADO`.
- It makes `/var/backups/buildserver` and a nightly backup timer.
- It lists the settings that are empty.

You can run `provision` again at any time. It does not change what is set
up. If you installed Vivado after `provision`, run `provision` again to set
`BUILDSERVER_VIVADO`.

#### 5. Select the host name

> **CAUTION:** Do not use a `.local` name or other private name. Google
> refuses redirect URIs on these names, and Let's Encrypt does not issue
> certificates for them.

Use a name in a public domain, e.g. `buildserver.drystone.solutions`. The
name must resolve only on the local network.

#### 6. Make the Google sign-in client

1. In the Google Cloud console, go to APIs & Services > Credentials.
2. Make an OAuth client ID of type *Web application*. You can also use the
   client of an existing server.
3. Add `https://<host>/auth/callback` as an authorised redirect URI.
4. Make sure that the OAuth consent screen accepts the admin accounts:
   - *Testing*: add each account as a test user.
   - *Internal*: only accounts of the project's own organisation can sign
     in.

#### 7. Select the certificate

Do one of the two procedures. See "HTTPS on the local network".

- **The domain is on Cloudflare:** In the Cloudflare dashboard, go to My
  Profile > API Tokens > Create Token. Use the template *Edit zone DNS* for
  the zone of the host name. Put the token in `CLOUDFLARE_API_TOKEN` in
  `deploy/caddy.env`.
- **The domain is not on Cloudflare:** Set `CADDY_TLS=internal` in
  `deploy/caddy.env`. After the deploy, install the root certificate on the
  clients (step 11).

#### 8. Add the DNS record

On the local DNS server, point the host name to the LAN address of the
server.

#### 9. Fill in the settings

Fill in the settings that `provision` listed:

| File | Setting |
| --- | --- |
| `.env` | `BUILDSERVER_PUBLIC_URL=https://<host>` |
| `.env` | `BUILDSERVER_GOOGLE_CLIENT_ID`, `BUILDSERVER_GOOGLE_CLIENT_SECRET` (step 6) |
| `.env` | `BUILDSERVER_ADMIN_EMAILS`: the admin Google accounts, separated by commas |
| `.env` | `BUILDSERVER_VIVADO`: `provision` sets it (e.g. `/opt/Xilinx/2025.2/Vivado/bin/vivado`) |
| `deploy/caddy.env` | `CLOUDFLARE_API_TOKEN`, or `CADDY_TLS=internal` (step 7) |

Optional: GitHub repository builds (`BUILDSERVER_GITHUB_CLIENT_ID`,
`BUILDSERVER_GITHUB_CLIENT_SECRET`). See "Repositories and GitHub".

> **WARNING:** Keep a copy of `BUILDSERVER_TOKEN_KEY` in a safe location.
> Without it, the server cannot read the stored GitHub tokens.

#### 10. Deploy

```
cd /opt/buildserver
deploy/buildserverctl deploy
deploy/buildserverctl status
```

`deploy` deploys `main`. To deploy a branch, add its name, e.g.
`deploy/buildserverctl deploy my-branch`.

The first deploy builds the images and runs the tests. It takes
approximately 15 minutes. When the server is ready, `status` shows
`api: healthy`.

#### 11. Install the root certificate (CADDY_TLS=internal only)

Do this step on each client that uses the server.

> **CAUTION:** Do not delete the `caddy_data` volume. It holds the root
> certificate. If you delete it, Caddy makes a new root certificate, and
> you must install it on all the clients again.

1. On the server, write the root certificate to a file:

   ```
   deploy/buildserverctl ca-cert > buildserver-ca.crt
   ```

2. Copy the file to the client.
3. Install the certificate in the store *Trusted Root Certification
   Authorities*:
   - Windows: double-click the file and select Install Certificate > Local
     Machine. Or, as administrator:
     `certutil -addstore -f Root buildserver-ca.crt`.
   - Linux and macOS: add the file to the system trust store. Firefox uses
     its own store.
4. Restart the browser.

The root certificate is valid for 10 years.

#### 12. Do a check

1. Open `https://<host>` and sign in with an admin account.
2. With `CADDY_TLS=cloudflare`, the first certificate takes approximately
   one minute. To see the progress, run `deploy/buildserverctl logs caddy`.
   Find "certificate obtained successfully".
3. Queue a small build to make sure that Vivado runs.

Before the DNS record is available, do a check from a different machine:

```
curl --resolve <host>:443:<server-ip> https://<host>/healthz
```

The correct response is `{"status":"ok"}`.

### Deploying

```
deploy/buildserverctl deploy                    # main
deploy/buildserverctl deploy v1.2               # a tag, a branch or a SHA
deploy/buildserverctl status
deploy/buildserverctl logs worker
deploy/buildserverctl rollback
```

A deploy does these steps:

1. It stops if a build runs. With `--force`, it continues and the running
   build fails.
2. It makes a backup.
3. It checks out the ref and runs the tests in the worker image, sandbox
   tests included. If a test fails, the deploy stops and nothing changes.
4. It builds and restarts the containers. Then it waits for the api's
   `/healthz`. If the new version is not healthy in two minutes, the deploy
   goes back to the previous version.

`.deploy-history` records each deploy. `rollback` deploys the version before
the current one, without the tests.

Run only one worker. The worker runs one build at a time, because Vivado
uses much CPU and memory.

### Backups

`deploy/buildserverctl backup` copies the live database and `.env` to
`/var/backups/buildserver`. It keeps the newest 14 copies of each. The timer
runs it each night, and each deploy runs it too.

The backup includes `.env` because the database needs its
`BUILDSERVER_TOKEN_KEY`. Without the key, the server cannot read the stored
GitHub tokens. Copy the backup directory to a different machine too.

To restore a backup:

```
deploy/buildserverctl restore /var/backups/buildserver/builds-<date>.db
```

`restore` stops the api and the worker, does a check of the file and puts
it in position. It keeps the old database in the `data` volume as
`builds.db.before-restore-<time>`. If the api runs, `restore` also makes a
backup first. Packages, logs and artifacts are not in the backup.

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

The `caddy` container is the HTTPS endpoint (`deploy/Caddyfile`). It uses
the host name in `BUILDSERVER_PUBLIC_URL`, e.g.
`https://buildserver.drystone.solutions`. The local DNS server resolves this
name to the build host.

> **CAUTION:** Use a name in a public domain, also if it resolves only on
> the local network. Google refuses redirect URIs on `.local` and other
> private names.

`CADDY_TLS` in `deploy/caddy.env` selects the certificate:

- **`cloudflare`** (default): a Let's Encrypt certificate, through a DNS
  challenge on Cloudflare. Cloudflare must hold the zone of the host name.
  The internet cannot reach the host, so Let's Encrypt cannot use a
  different challenge. Make a Cloudflare API token with the template
  *Edit zone DNS* for the zone. Put it in `CLOUDFLARE_API_TOKEN`. The image
  (`deploy/caddy/Dockerfile`) is Caddy with the Cloudflare DNS module.
- **`internal`**: a certificate from Caddy's own certificate authority. Use
  it when the zone is not on Cloudflare. The server needs nothing from
  outside. Browsers show a warning until the client trusts Caddy's root
  certificate. To install the root certificate, see step 11 of "New server,
  step by step".

For the two types:

1. Add `<BUILDSERVER_PUBLIC_URL>/auth/callback` to the authorised redirect
   URIs of the OAuth client in Google Cloud.
2. Open inbound TCP 443 in the firewall of the host. Also open TCP 80, for
   the redirect to HTTPS.

Caddy renews the certificate automatically. The secrets in
`deploy/caddy.env` are not in `.env`, because the api and worker containers
get `.env`.

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
