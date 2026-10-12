"""On-disk layout and zip handling.

    <data_dir>/builds.db
    <data_dir>/builds/<id>/package.zip    uploaded build package
    <data_dir>/builds/<id>/build.log      build output
    <data_dir>/builds/<id>/artifacts.zip  build artifacts
    <data_dir>/work/<id>/                 extracted package, removed after the build
"""

import os
import zipfile
from pathlib import Path


class Storage:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.builds_dir = data_dir / "builds"
        self.work_root = data_dir / "work"
        self.builds_dir.mkdir(parents=True, exist_ok=True)
        self.work_root.mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "builds.db"

    def build_dir(self, build_id: str) -> Path:
        return self.builds_dir / build_id

    def package_path(self, build_id: str) -> Path:
        return self.build_dir(build_id) / "package.zip"

    def log_path(self, build_id: str) -> Path:
        return self.build_dir(build_id) / "build.log"

    def artifacts_path(self, build_id: str) -> Path:
        return self.build_dir(build_id) / "artifacts.zip"

    def work_dir(self, build_id: str) -> Path:
        return self.work_root / build_id


def extract_package(zip_path: Path, dest: Path) -> None:
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if not (dest / info.filename).resolve().is_relative_to(dest):
                raise ValueError(f"package entry escapes the build directory: {info.filename}")
        zf.extractall(dest)


def find_build_root(extracted: Path, build_script: str) -> Path | None:
    """Directory holding the build script: the package root, or its single
    top-level folder (what you get when zipping a folder rather than its contents)."""
    if (extracted / build_script).is_file():
        return extracted
    entries = list(extracted.iterdir())
    if len(entries) == 1 and (entries[0] / build_script).is_file():
        return entries[0]
    return None


def zip_dir(src: Path, zip_path: Path, extra: dict[str, Path] | None = None) -> None:
    """Zip the contents of src (plus extra arcname -> file entries) to zip_path."""
    tmp = zip_path.with_name(zip_path.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        if src.is_dir():
            for path in sorted(src.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(src).as_posix())
        for arcname, path in (extra or {}).items():
            zf.write(path, arcname)
    os.replace(tmp, zip_path)
