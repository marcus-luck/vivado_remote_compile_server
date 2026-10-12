"""On-disk layout of the data directory, and zip handling.

Layout:

    <data_dir>/builds.db                  build records and queue
    <data_dir>/builds/<id>/package.zip    uploaded build package
    <data_dir>/builds/<id>/build.log      build output
    <data_dir>/builds/<id>/artifacts.zip  build artifacts
    <data_dir>/work/<id>/                 extracted package, removed after the
                                          build
"""

import os
import pathlib
import zipfile


class Storage:
    """Paths of everything stored in the data directory.

    Attributes:
        data_dir: Root of the data directory.
        builds_dir: Directory holding one subdirectory per build.
        work_root: Directory holding the work directory of the running build.
    """

    def __init__(self, data_dir: pathlib.Path) -> None:
        """Creates the directory layout if it is missing.

        Args:
            data_dir: Root of the data directory.
        """
        self.data_dir = data_dir
        self.builds_dir = data_dir / "builds"
        self.work_root = data_dir / "work"
        self.builds_dir.mkdir(parents=True, exist_ok=True)
        self.work_root.mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> pathlib.Path:
        """Location of the SQLite database file."""
        return self.data_dir / "builds.db"

    def build_dir(self, build_id: str) -> pathlib.Path:
        """Returns the directory holding the files of a build."""
        return self.builds_dir / build_id

    def package_path(self, build_id: str) -> pathlib.Path:
        """Returns the location of the uploaded package of a build."""
        return self.build_dir(build_id) / "package.zip"

    def log_path(self, build_id: str) -> pathlib.Path:
        """Returns the location of the log of a build."""
        return self.build_dir(build_id) / "build.log"

    def artifacts_path(self, build_id: str) -> pathlib.Path:
        """Returns the location of the artifact zip of a build."""
        return self.build_dir(build_id) / "artifacts.zip"

    def work_dir(self, build_id: str) -> pathlib.Path:
        """Returns the directory a build's package is extracted to."""
        return self.work_root / build_id


def extract_package(zip_path: pathlib.Path, dest: pathlib.Path) -> None:
    """Extracts a build package.

    Args:
        zip_path: The package to extract.
        dest: Directory to extract into. Created if it is missing.

    Raises:
        ValueError: If an entry of the package would land outside dest.
        zipfile.BadZipFile: If the package is not a valid zip archive.
    """
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if not (dest / info.filename).resolve().is_relative_to(dest):
                raise ValueError(
                    "package entry escapes the build directory:"
                    f" {info.filename}"
                )
        archive.extractall(dest)


def find_build_root(
    extracted: pathlib.Path, build_script: str
) -> pathlib.Path | None:
    """Finds the directory of an extracted package that holds the build script.

    That is either the package root, or its single top-level folder, which is
    what zipping a folder (rather than its contents) produces.

    Args:
        extracted: Directory the package was extracted to.
        build_script: File name of the build script.

    Returns:
        The directory holding the build script, or None if there is none.
    """
    if (extracted / build_script).is_file():
        return extracted
    entries = list(extracted.iterdir())
    if len(entries) == 1 and (entries[0] / build_script).is_file():
        return entries[0]
    return None


def zip_dir(
    src: pathlib.Path,
    zip_path: pathlib.Path,
    extra: dict[str, pathlib.Path] | None = None,
) -> None:
    """Zips the contents of a directory.

    The archive is written under a temporary name and renamed when complete,
    so a reader never sees a partial zip.

    Args:
        src: Directory whose contents go at the top level of the archive.
        zip_path: The archive to write. Replaced if it exists.
        extra: Additional files to add, as archive name to file path.
    """
    tmp_path = zip_path.with_name(zip_path.name + ".tmp")
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as archive:
        if src.is_dir():
            for path in sorted(src.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(src).as_posix())
        for arcname, path in (extra or {}).items():
            archive.write(path, arcname)
    os.replace(tmp_path, zip_path)
