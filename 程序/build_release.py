from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from version import APP_VERSION


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RELEASE_FORMAT = "dict-tango-release"
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
RELEASE_DIRECTORIES = ("程序", "网页")
EXCLUDED_FILES = {
    Path("程序/settings.json"),
    Path("程序/highlight-rules.json"),
    Path("程序/test_app.py"),
    Path("程序/build_release.py"),
}
EXCLUDED_DIRECTORY_PARTS = {"__pycache__", ".pytest_cache"}


def _is_excluded(relative_path: Path) -> bool:
    if relative_path in EXCLUDED_FILES:
        return True
    if relative_path.parts[:2] == ("程序", "note"):
        return True
    if any(part in EXCLUDED_DIRECTORY_PARTS for part in relative_path.parts):
        return True
    return relative_path.suffix.lower() in {".pyc", ".pyo"}


def _release_files(project_root: Path) -> list[tuple[Path, Path]]:
    files: list[tuple[Path, Path]] = []
    for directory_name in RELEASE_DIRECTORIES:
        directory = project_root / directory_name
        if not directory.is_dir():
            raise FileNotFoundError(f"发布目录不存在：{directory}")
        for path in sorted(directory.rglob("*")):
            relative_path = path.relative_to(project_root)
            if path.is_file() and not path.is_symlink() and not _is_excluded(relative_path):
                files.append((path, relative_path))
    if not files:
        raise ValueError("没有找到可打包的程序文件")
    return files


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, value: object) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def build_release_archive(
    project_root: Path,
    output_directory: Path,
    version: str = APP_VERSION,
) -> tuple[Path, Path]:
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("版本号必须使用 MAJOR.MINOR.PATCH 格式")

    project_root = Path(project_root).resolve(strict=True)
    output_directory = Path(output_directory).resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    files = _release_files(project_root)
    archive_path = output_directory / f"DictTango-Notes-{version}-Windows.zip"
    manifest_path = output_directory / f"DictTango-Notes-{version}-Windows.json"
    temporary_archive: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=output_directory,
            prefix=f".{archive_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_archive = Path(stream.name)

        with zipfile.ZipFile(
            temporary_archive,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for source, relative_path in files:
                archive.write(source, relative_path.as_posix())

        checksum = _sha256(temporary_archive)
        temporary_archive.replace(archive_path)
        _atomic_write_json(
            manifest_path,
            {
                "format": RELEASE_FORMAT,
                "version": version,
                "platform": "windows",
                "minimum_python": "3.10",
                "archive": archive_path.name,
                "sha256": checksum,
                "file_count": len(files),
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )
    finally:
        if temporary_archive is not None:
            temporary_archive.unlink(missing_ok=True)

    return archive_path, manifest_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a Windows release archive without user data."
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=PROJECT_ROOT / "dist",
    )
    args = parser.parse_args()
    archive_path, manifest_path = build_release_archive(
        PROJECT_ROOT,
        args.output_directory,
    )
    print(f"Release archive: {archive_path}")
    print(f"Release manifest: {manifest_path}")


if __name__ == "__main__":
    main()
