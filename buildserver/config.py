"""Build server settings.

Settings are read from BUILDSERVER_* variables in the .env file of the working
directory. Variables already set in the environment take precedence over the
file.
"""

import dataclasses
import json
import os
import pathlib

import dotenv

NAME_PLACEHOLDER = "{name}"
ENV_FILE = ".env"


@dataclasses.dataclass(frozen=True)
class Settings:
    """Configuration shared by the API server and the build worker.

    Attributes:
        data_dir: Directory holding the database, packages, logs, artifacts
            and work directories.
        build_command: Command run in the root of the extracted package.
            NAME_PLACEHOLDER in any argument is replaced by the build name.
        build_script: File that must exist in a package. It marks the
            directory the build command runs in.
        artifact_dir: Directory, relative to the package root, that is
            collected into the artifact zip. May contain NAME_PLACEHOLDER.
        build_timeout: Seconds a build may run before it is killed.
        max_upload_bytes: Largest accepted build package, in bytes.
        keep_workdir: Whether to keep the extracted package after the build.
        poll_interval: Seconds the worker sleeps when the queue is empty.
    """

    data_dir: pathlib.Path
    build_command: list[str]
    build_script: str
    artifact_dir: str
    build_timeout: float = 6 * 3600
    max_upload_bytes: int = 2 * 1024**3
    keep_workdir: bool = False
    poll_interval: float = 2.0


def _require(name: str) -> str:
    """Reads a variable that has no default.

    Args:
        name: Name of the environment variable.

    Returns:
        The value of the variable.

    Raises:
        ValueError: If the variable is not set or is empty.
    """
    value = os.environ.get(name)
    if not value:
        raise ValueError(
            f"{name} is not set. Set it in {ENV_FILE} (see .env.template)."
        )
    return value


def load_settings() -> Settings:
    """Builds the settings from the .env file and the environment.

    Every path comes from the configuration; none has a default in the code.
    The .env file is loaded into os.environ, so the build command inherits
    its variables too (a Vivado licence server, for example).

    Returns:
        The settings.

    Raises:
        ValueError: If a required variable is missing,
            BUILDSERVER_BUILD_COMMAND is not a JSON list of strings, or a
            numeric variable does not hold a number.
    """
    dotenv.load_dotenv(pathlib.Path.cwd() / ENV_FILE)
    env = os.environ
    script = _require("BUILDSERVER_BUILD_SCRIPT")
    if env.get("BUILDSERVER_BUILD_COMMAND"):
        command = json.loads(env["BUILDSERVER_BUILD_COMMAND"])
        if not isinstance(command, list) or not all(
            isinstance(argument, str) for argument in command
        ):
            raise ValueError(
                "BUILDSERVER_BUILD_COMMAND must be a JSON list of strings"
            )
    else:
        command = [
            _require("BUILDSERVER_VIVADO"),
            "-mode",
            "batch",
            "-source",
            script,
            "-tclargs",
            NAME_PLACEHOLDER,
        ]
    defaults = {
        field.name: field.default for field in dataclasses.fields(Settings)
    }
    return Settings(
        data_dir=pathlib.Path(_require("BUILDSERVER_DATA_DIR")).resolve(),
        build_command=command,
        build_script=script,
        artifact_dir=_require("BUILDSERVER_ARTIFACT_DIR"),
        build_timeout=float(
            env.get("BUILDSERVER_BUILD_TIMEOUT") or defaults["build_timeout"]
        ),
        max_upload_bytes=int(
            env.get("BUILDSERVER_MAX_UPLOAD_BYTES")
            or defaults["max_upload_bytes"]
        ),
        keep_workdir=env.get("BUILDSERVER_KEEP_WORKDIR", "") not in ("", "0"),
    )
