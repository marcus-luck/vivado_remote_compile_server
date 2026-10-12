"""Settings, read from BUILDSERVER_* variables in the .env file of the working
directory. Variables already set in the environment take precedence."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

NAME_PLACEHOLDER = "{name}"
ENV_FILE = ".env"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    # Command run in the root of the extracted package. "{name}" is replaced by the build name.
    build_command: list[str]
    # File that must exist in the package; marks the directory the command runs in.
    build_script: str = "build.tcl"
    # Directory (relative to the package root) collected into the artifact zip.
    artifact_dir: str = "bitfiles/generated/{name}"
    build_timeout: float = 6 * 3600
    max_upload_bytes: int = 2 * 1024**3
    keep_workdir: bool = False
    poll_interval: float = 2.0


def find_vivado() -> str:
    """Newest installed Vivado, falling back to whatever is on PATH."""
    candidates = []
    for root in (r"C:\AMD\Vivado", r"C:\Xilinx\Vivado", "/tools/Xilinx/Vivado", "/opt/Xilinx/Vivado"):
        for exe in ("vivado.bat", "vivado"):
            candidates += Path(root).glob(f"*/bin/{exe}")
    if candidates:
        return str(max(candidates, key=lambda p: p.parts[-3]))
    return "vivado"


def load_settings() -> Settings:
    # Loaded into os.environ, so the build command inherits the file's variables
    # too (e.g. a Vivado licence server).
    load_dotenv(Path.cwd() / ENV_FILE)
    env = os.environ
    script = env.get("BUILDSERVER_BUILD_SCRIPT", "build.tcl")
    if "BUILDSERVER_BUILD_COMMAND" in env:
        command = json.loads(env["BUILDSERVER_BUILD_COMMAND"])
        if not isinstance(command, list) or not all(isinstance(a, str) for a in command):
            raise ValueError("BUILDSERVER_BUILD_COMMAND must be a JSON list of strings")
    else:
        vivado = env.get("BUILDSERVER_VIVADO") or find_vivado()
        command = [vivado, "-mode", "batch", "-source", script, "-tclargs", NAME_PLACEHOLDER]
    return Settings(
        data_dir=Path(env.get("BUILDSERVER_DATA_DIR", "data")).resolve(),
        build_command=command,
        build_script=script,
        artifact_dir=env.get("BUILDSERVER_ARTIFACT_DIR", "bitfiles/generated/{name}"),
        build_timeout=float(env.get("BUILDSERVER_BUILD_TIMEOUT", 6 * 3600)),
        max_upload_bytes=int(env.get("BUILDSERVER_MAX_UPLOAD_BYTES", 2 * 1024**3)),
        keep_workdir=env.get("BUILDSERVER_KEEP_WORKDIR", "") not in ("", "0"),
    )
