"""Cloning GitHub repositories for builds.

The OAuth token reaches git only through environment variables of the clone
process (GIT_CONFIG_COUNT and friends). It is never part of a URL or a command
line, so it does not end up in .git/config, in the build log, or in the
environment of the build command.
"""

import base64
import os
import pathlib
import re
import subprocess

# Where repositories are cloned from. Tests point it at local repositories.
GITHUB_URL = "https://github.com"
# Protocols git may use, for the repository and its submodules. Tests add
# "file".
ALLOWED_PROTOCOLS = ("https",)

_GITHUB_HTTPS = "https://github.com/"
# Submodules that point at GitHub over SSH are fetched over HTTPS instead, so
# the token works for them too.
_SSH_PREFIXES = ("git@github.com:", "ssh://git@github.com/")
_BAD_REF_RE = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//")


def clone_url(full_name: str) -> str:
    """Returns the URL a repository is cloned from.

    Args:
        full_name: The repository, as "owner/repo".
    """
    return f"{GITHUB_URL}/{full_name}.git"


def clone_command(
    git: str, full_name: str, ref: str, dest: pathlib.Path
) -> list[str]:
    """Returns the command that clones one branch or tag of a repository.

    The clone is shallow and includes the submodules. A tag is checked out
    as a detached HEAD.

    Args:
        git: The git executable.
        full_name: The repository, as "owner/repo".
        ref: Name of the branch or tag.
        dest: Directory to clone into; must not exist or be empty.

    Returns:
        The command, as a list of arguments.
    """
    return [
        git,
        "-c",
        "credential.interactive=never",
        "clone",
        "--depth",
        "1",
        "--branch",
        ref,
        "--recurse-submodules",
        "--",
        clone_url(full_name),
        str(dest),
    ]


def clone_env(token: str) -> dict[str, str]:
    """Returns the environment of the clone process.

    Args:
        token: OAuth token of the user whose access the clone uses.

    Returns:
        The current environment, plus git configuration that sends the token
        to GitHub, and settings that make git fail instead of prompting.
    """
    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    config = [
        (
            f"http.{_GITHUB_HTTPS}.extraheader",
            f"Authorization: Basic {credentials}",
        ),
        *(
            (f"url.{_GITHUB_HTTPS}.insteadOf", prefix)
            for prefix in _SSH_PREFIXES
        ),
    ]
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ALLOW_PROTOCOL"] = ":".join(ALLOWED_PROTOCOLS)
    env["GIT_CONFIG_COUNT"] = str(len(config))
    for index, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def head_commit(git: str, repo_dir: pathlib.Path) -> str:
    """Returns the commit a clone has checked out.

    Args:
        git: The git executable.
        repo_dir: The clone.

    Returns:
        The full commit hash.

    Raises:
        subprocess.CalledProcessError: If git fails.
    """
    result = subprocess.run(
        [git, "-C", str(repo_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def valid_ref(name: str) -> bool:
    """Tells whether a name is safe to pass to git as a branch or tag.

    The callers also require the name to be one GitHub listed for the
    repository; this check keeps option-like and malformed names out
    regardless.

    Args:
        name: Name of the branch or tag.

    Returns:
        True if the name is a well-formed ref name.
    """
    return bool(
        name
        and len(name) <= 255
        and not name.startswith(("-", "/", "."))
        and not name.endswith(("/", ".", ".lock"))
        and not _BAD_REF_RE.search(name)
    )
