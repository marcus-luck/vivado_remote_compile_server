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
        git: Git executable, used to clone repositories for builds.
        retention_days: Days after a build finishes before its package and
            artifacts are deleted; 0 keeps them forever.
        retention_interval: Seconds between the worker's retention passes.
        token_key: Key the GitHub tokens are encrypted with in the database
            (see buildserver.crypto); empty if GitHub is not used.
    """

    data_dir: pathlib.Path
    build_command: list[str]
    build_script: str
    artifact_dir: str
    build_timeout: float = 6 * 3600
    max_upload_bytes: int = 2 * 1024**3
    keep_workdir: bool = False
    poll_interval: float = 2.0
    git: str = "git"
    retention_days: float = 0
    retention_interval: float = 3600
    token_key: str = dataclasses.field(default="", repr=False)


@dataclasses.dataclass(frozen=True)
class AuthSettings:
    """Configuration of Google sign-in, used by the API server only.

    Attributes:
        public_url: URL users reach the server at, without a trailing slash.
            The OAuth redirect URI is derived from it.
        google_client_id: Client id of the Google OAuth client.
        google_client_secret: Client secret of the Google OAuth client.
        session_secret: Key that signs the session cookie.
        admin_emails: Google accounts that are always administrators. They are
            how the first administrator gets in.
        allowed_domains: Email domains whose Google accounts may sign in
            without being added by an administrator first.
        github_client_id: Client id of the GitHub OAuth App users link their
            GitHub accounts through, or empty if GitHub is not configured.
        github_client_secret: Client secret of the GitHub OAuth App.
    """

    public_url: str
    google_client_id: str
    google_client_secret: str
    session_secret: str
    admin_emails: frozenset[str]
    allowed_domains: frozenset[str] = frozenset()
    github_client_id: str = ""
    github_client_secret: str = ""

    @property
    def github_enabled(self) -> bool:
        """Whether users can link GitHub accounts and build repositories."""
        return bool(self.github_client_id and self.github_client_secret)


def _load_env_file() -> None:
    """Loads the .env file of the working directory into os.environ."""
    dotenv.load_dotenv(pathlib.Path.cwd() / ENV_FILE)


def _split(value: str) -> frozenset[str]:
    """Parses a comma-separated list into a set of lower-case items."""
    return frozenset(
        item.strip().lower() for item in value.split(",") if item.strip()
    )


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
    The .env file is loaded into os.environ. The build command gets the
    variables that do not start with BUILDSERVER_ (a Vivado licence server,
    for example); see worker.build_env().

    Returns:
        The settings.

    Raises:
        ValueError: If a required variable is missing,
            BUILDSERVER_BUILD_COMMAND is not a JSON list of strings, or a
            numeric variable does not hold a number.
    """
    _load_env_file()
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
        git=env.get("BUILDSERVER_GIT") or defaults["git"],
        retention_days=float(
            env.get("BUILDSERVER_RETENTION_DAYS") or defaults["retention_days"]
        ),
        token_key=env.get("BUILDSERVER_TOKEN_KEY", ""),
    )


def load_auth_settings() -> AuthSettings:
    """Builds the sign-in settings from the .env file and the environment.

    Returns:
        The sign-in settings.

    Raises:
        ValueError: If a required variable is missing.
    """
    _load_env_file()
    return AuthSettings(
        public_url=_require("BUILDSERVER_PUBLIC_URL").rstrip("/"),
        google_client_id=_require("BUILDSERVER_GOOGLE_CLIENT_ID"),
        google_client_secret=_require("BUILDSERVER_GOOGLE_CLIENT_SECRET"),
        session_secret=_require("BUILDSERVER_SESSION_SECRET"),
        admin_emails=_split(_require("BUILDSERVER_ADMIN_EMAILS")),
        allowed_domains=_split(
            os.environ.get("BUILDSERVER_ALLOWED_DOMAINS", "")
        ),
        github_client_id=os.environ.get("BUILDSERVER_GITHUB_CLIENT_ID", ""),
        github_client_secret=os.environ.get(
            "BUILDSERVER_GITHUB_CLIENT_SECRET", ""
        ),
    )
