# Firmware build server

Clients upload a build package (a zip of a firmware project), the server queues it,
a worker builds it, and the build artifacts are offered back as a zip.

- **API + front end**: FastAPI (`buildserver/api.py`). The front end is server-rendered
  (Jinja templates in `buildserver/templates/`) and kept live with htmx; no Node, no build step
- **Task queue**: the `builds` table in SQLite (`buildserver/db.py`); queued rows are claimed oldest first
- **Worker**: a separate process (`buildserver/worker.py`) that runs one build at a time
- **Storage**: everything lives under the data directory (`buildserver/storage.py`)

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

There is no authentication, and a build package runs arbitrary code on the build
host. Only expose the server on a trusted network.

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

Status is one of `queued`, `running`, `succeeded`, `failed`, `cancelled`.

The `/ui/...` routes return the HTML fragments the front end swaps into the page;
they are not meant for clients.

```
curl -F "package=@hdsocv2.zip" -F "name=hdsocv2_v123" http://host:8000/api/builds
curl -OJ http://host:8000/api/builds/<id>/artifacts
```

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
