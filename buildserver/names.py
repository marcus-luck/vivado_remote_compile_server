"""Build names: what is allowed, and defaults derived from other names.

A build name is passed to the build command and becomes part of file names,
so it is limited to a safe set of characters.
"""

import pathlib
import re

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
NAME_ERROR = (
    "name must be 1-64 characters of A-Z a-z 0-9 . _ - and start with a"
    " letter or digit"
)


def clean(text: str) -> str:
    """Reduces any text to a valid build name.

    Args:
        text: The text, e.g. a file or repository name.

    Returns:
        The text with runs of disallowed characters replaced by "_", or
        "build" if nothing usable is left.
    """
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", text).lstrip("._-")[:64]
    return name or "build"


def default_name(filename: str) -> str:
    """Derives a build name from the file name of an uploaded package.

    Args:
        filename: File name of the package, as given by the client.

    Returns:
        The file name without its extension, reduced to the characters allowed
        in a build name, or "build" if nothing usable is left.
    """
    return clean(pathlib.Path(filename).stem)
