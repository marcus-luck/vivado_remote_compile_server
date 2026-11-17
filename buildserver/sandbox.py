"""Runs build commands as a separate, unprivileged account (Linux only).

A build runs code from an uploaded package or a repository. With
BUILDSERVER_BUILD_USER set, the worker runs the build command as that account
through sudo, so the build cannot read what the worker can: the database
(GitHub tokens, users), the data directory of other builds, or the worker's
environment (the token key, OAuth secrets), which /proc only shows to the
same account.

What the deployment provides (see the Dockerfile):

- the build account, whose primary group the worker's account shares;
- a sudoers rule that lets the worker's account run commands as the build
  account, and nothing else;
- a work root owned by that group with the setgid bit, the only place the
  build account can write, and a data directory it cannot enter.

Each build gets a fresh home directory and temporary directory. After the
build every process of the build account is killed and the files it left in
the shared temporary directories are deleted, so a build cannot leave a
process or a file (a Vivado_init.tcl in ~/.Xilinx, say) behind for the next
one.
"""

from collections.abc import Mapping
import logging
import pathlib
import subprocess

_logger = logging.getLogger("buildserver.sandbox")

SUDO = "sudo"
# Variables set for the account itself; anything the environment passes for
# them is replaced.
_ACCOUNT_VARIABLES = ("HOME", "TMPDIR", "USER", "LOGNAME")
# Shared temporary directories the build account can write to.
_SHARED_TMP = ("/tmp", "/var/tmp", "/dev/shm")
# Files the build creates stay writable for the worker's group, so the worker
# can delete them.
_WRAPPER = 'umask 002; exec "$@"'


def wrap(
    command: list[str],
    user: str,
    home: pathlib.Path,
    env: Mapping[str, str],
) -> list[str]:
    """Returns the command line that runs a command as the build account.

    sudo starts the command with a clean environment; env -i then sets
    exactly the given variables plus the account's own HOME, TMPDIR, USER
    and LOGNAME. The variables appear on the command line, which only the
    build account and the worker see, so env must hold no secrets: pass
    worker.build_env().

    Args:
        command: The build command.
        user: The build account.
        home: The build's fresh home directory; TMPDIR is home/tmp.
        env: Environment variables for the command.

    Returns:
        The command line to run instead of command.
    """
    pairs = [
        f"{key}={value}"
        for key, value in sorted(env.items())
        if key.upper() not in _ACCOUNT_VARIABLES
    ]
    account = [
        f"HOME={home}",
        f"TMPDIR={home / 'tmp'}",
        f"USER={user}",
        f"LOGNAME={user}",
    ]
    return [
        SUDO,
        "-n",
        "-u",
        user,
        "--",
        "env",
        "-i",
        *pairs,
        *account,
        "sh",
        "-c",
        _WRAPPER,
        "build",
        *command,
    ]


def kill_command(user: str) -> list[str]:
    """Returns the command that kills every process of the build account.

    The worker's account cannot signal the build directly: the process it
    started is sudo, which runs as root. kill -1 run as the build account
    reaches every process of that account, including ones that left the
    build's process group.

    Args:
        user: The build account.

    Returns:
        The command line.
    """
    return [SUDO, "-n", "-u", user, "--", "kill", "-KILL", "-1"]


def cleanup_command(user: str) -> list[str]:
    """Returns the command that deletes the build account's temporary files.

    Args:
        user: The build account.

    Returns:
        The command line: a find over the shared temporary directories.
    """
    return [
        SUDO,
        "-n",
        "-u",
        user,
        "--",
        "find",
        *_SHARED_TMP,
        "-mindepth",
        "1",
        "-user",
        user,
        "-delete",
    ]


def release_command(user: str, work_root: pathlib.Path) -> list[str]:
    """Returns the command that hands the build's files back to the worker.

    A build can take away the group's access to the files it created, which
    would stop the worker from deleting them. Run as the build account, which
    owns them, this restores it. Files the build account does not own are
    skipped.

    Args:
        user: The build account.
        work_root: The directory holding the work and home directories.

    Returns:
        The command line.
    """
    return [
        SUDO,
        "-n",
        "-u",
        user,
        "--",
        "chmod",
        "-R",
        "-f",
        "g+rwX",
        str(work_root),
    ]


def kill_all(user: str) -> None:
    """Kills every process of the build account.

    Args:
        user: The build account.
    """
    # kill -1 also hits the kill process itself, so it never exits 0.
    subprocess.run(kill_command(user), capture_output=True, check=False)


def prepare_home(home: pathlib.Path) -> None:
    """Creates a build's fresh home directory and its temporary directory.

    Args:
        home: The home directory; must not exist.
    """
    (home / "tmp").mkdir(parents=True)


def clean_up(user: str, work_root: pathlib.Path) -> None:
    """Ends everything a build left behind.

    Kills every process of the build account, deletes its files in the
    shared temporary directories, and makes its files under the work root
    deletable by the worker, which then deletes the home and work
    directories.

    Args:
        user: The build account.
        work_root: The directory holding the work and home directories.
    """
    kill_all(user)
    for command in (cleanup_command(user), release_command(user, work_root)):
        result = subprocess.run(
            command, capture_output=True, check=False, text=True
        )
        # find and chmod report the entries they may not touch, with 1.
        if result.returncode not in (0, 1):
            _logger.warning(
                "%s as %s failed: %s",
                command[5],
                user,
                result.stderr.strip(),
            )
