from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from version import APP_VERSION


GITHUB_OWNER = "mokusa11"
GITHUB_REPOSITORY = "TangoNote"
RELEASES_API_URL = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases/latest"
)
RELEASE_PAGE_URL = (
    f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases/latest"
)
RELEASE_DOWNLOAD_PREFIX = (
    f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases/download/"
)
RELEASE_FORMAT = "dict-tango-release"
MAX_RELEASE_JSON_BYTES = 1_000_000
MAX_MANIFEST_BYTES = 100_000
MAX_ARCHIVE_BYTES = 100_000_000
MAX_ARCHIVE_FILES = 1_000
MAX_UNPACKED_BYTES = 250_000_000
VERSION_PATTERN = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$"
)


class UpdateError(Exception):
    pass


def _version_parts(value: str) -> tuple[tuple[int, int, int], str | None]:
    match = VERSION_PATTERN.fullmatch(value)
    if not match:
        raise UpdateError(f"GitHub Release 使用了无效版本号：{value}")
    return (
        (int(match.group(1)), int(match.group(2)), int(match.group(3))),
        match.group(4),
    )


def _is_newer_version(latest: str, current: str) -> bool:
    latest_numbers, latest_prerelease = _version_parts(latest)
    current_numbers, current_prerelease = _version_parts(current)
    if latest_numbers != current_numbers:
        return latest_numbers > current_numbers
    if latest_prerelease is None:
        return current_prerelease is not None
    if current_prerelease is None:
        return False
    return latest_prerelease != current_prerelease


def _read_url(url: str, limit: int, timeout: int = 8) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"TangoNote/{APP_VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            data = response.read(limit + 1)
    except HTTPError as error:
        if error.code == 404:
            raise UpdateError(
                "GitHub 上没有可访问的稳定版 Release；请确认仓库已公开并已发布更新包。"
            ) from error
        if error.code == 403:
            raise UpdateError(
                "GitHub 暂时限制了更新检查请求，请稍后重试。"
            ) from error
        raise UpdateError(f"GitHub 更新服务返回 HTTP {error.code}。") from error
    except (URLError, TimeoutError, OSError) as error:
        raise UpdateError(f"连接 GitHub 更新服务失败：{error}") from error
    if len(data) > limit:
        raise UpdateError("GitHub 返回的更新信息超过允许大小。")
    return data


def _fetch_release() -> dict[str, object]:
    try:
        payload = json.loads(_read_url(RELEASES_API_URL, MAX_RELEASE_JSON_BYTES))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise UpdateError("无法解析 GitHub Release 信息。") from error
    if not isinstance(payload, dict):
        raise UpdateError("GitHub Release 信息格式无效。")
    return payload


def _release_version(release: dict[str, object]) -> str:
    tag = release.get("tag_name")
    if not isinstance(tag, str):
        raise UpdateError("GitHub Release 缺少版本标签。")
    version = tag.removeprefix("v")
    _version_parts(version)
    return version


def _expected_asset_names(version: str) -> tuple[str, str]:
    return f"TangoNote-{version}.zip", f"TangoNote-{version}.json"


def check_for_updates() -> dict[str, object]:
    release = _fetch_release()
    latest = _release_version(release)
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("GitHub Release 未提供更新附件列表。")
    asset_names = {
        item.get("name")
        for item in assets
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    archive_name, manifest_name = _expected_asset_names(latest)
    if archive_name not in asset_names or manifest_name not in asset_names:
        raise UpdateError(
            f"GitHub Release {latest} 缺少程序 ZIP 或 SHA-256 清单。"
        )
    release_notes = release.get("body")
    if not isinstance(release_notes, str):
        release_notes = ""
    return {
        "current_version": APP_VERSION,
        "latest_version": latest,
        "update_available": _is_newer_version(latest, APP_VERSION),
        "release_url": RELEASE_PAGE_URL,
        "release_notes": release_notes[:20_000],
    }


def _asset_url(release: dict[str, object], asset_name: str) -> str:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("GitHub Release 附件列表格式无效。")
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") != asset_name:
            continue
        url = asset.get("browser_download_url")
        if not isinstance(url, str) or not url.startswith(RELEASE_DOWNLOAD_PREFIX):
            raise UpdateError(f"GitHub Release 附件地址无效：{asset_name}")
        return url
    raise UpdateError(f"GitHub Release 中找不到附件：{asset_name}")


def _download_to_file(url: str, target: Path, limit: int) -> int:
    request = Request(
        url,
        headers={"User-Agent": f"TangoNote/{APP_VERSION}"},
    )
    total = 0
    try:
        with urlopen(request, timeout=30) as response, target.open("wb") as stream:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > limit:
                    raise UpdateError("GitHub 更新文件超过允许大小。")
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
    except UpdateError:
        target.unlink(missing_ok=True)
        raise
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        target.unlink(missing_ok=True)
        raise UpdateError(f"下载 GitHub 更新文件失败：{error}") from error
    return total


def _validate_archive_member(name: str) -> PurePosixPath:
    relative = PurePosixPath(name)
    parts = relative.parts
    if (
        relative.is_absolute()
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
        or parts[0] not in {"程序", "网页"}
        or len(parts) < 2
    ):
        raise UpdateError(f"更新包包含不允许的文件路径：{name}")
    if parts[0] == "程序":
        if (
            parts[1] in {"settings.json", "highlight-rules.json", "note"}
            or "__pycache__" in parts
            or PurePosixPath(name).suffix.lower() in {".pyc", ".pyo", ".sqlite3", ".db"}
        ):
            raise UpdateError(f"更新包包含个人数据或缓存：{name}")
    return relative


def _extract_validated_archive(archive_path: Path, staging_directory: Path) -> int:
    staging_directory.mkdir(parents=True, exist_ok=True)
    total_unpacked = 0
    file_count = 0
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_FILES:
                raise UpdateError("更新包中的文件数量超过允许上限。")
            for member in members:
                if member.is_dir():
                    continue
                relative = _validate_archive_member(member.filename)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode) or member.flag_bits & 0x1:
                    raise UpdateError(
                        f"更新包包含符号链接或加密文件：{member.filename}"
                    )
                total_unpacked += member.file_size
                file_count += 1
                if total_unpacked > MAX_UNPACKED_BYTES:
                    raise UpdateError("更新包解压后超过允许大小。")
                target = staging_directory.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted_size = 0
                with archive.open(member) as source, target.open("xb") as destination:
                    while chunk := source.read(1024 * 1024):
                        extracted_size += len(chunk)
                        if (
                            extracted_size > member.file_size
                            or extracted_size > MAX_UNPACKED_BYTES
                        ):
                            raise UpdateError(
                                f"更新包文件实际大小超过声明值：{member.filename}"
                            )
                        destination.write(chunk)
                if extracted_size != member.file_size:
                    raise UpdateError(
                        f"更新包文件大小校验失败：{member.filename}"
                    )
                permissions = stat.S_IMODE(mode) & 0o777
                if permissions:
                    target.chmod(permissions)
    except UpdateError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, zlib.error) as error:
        raise UpdateError(f"更新 ZIP 无法安全解压：{error}") from error
    if file_count == 0:
        raise UpdateError("更新包不包含程序文件。")
    return file_count


def _write_staging_metadata(path: Path, metadata: dict[str, object]) -> None:
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
            json.dump(metadata, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def prepare_termux_update(staging_directory: Path) -> dict[str, object]:
    staging_directory = Path(staging_directory).resolve()
    staging_directory.mkdir(parents=True, exist_ok=True)
    if any(staging_directory.iterdir()):
        raise UpdateError("更新暂存目录必须为空。")
    release = _fetch_release()
    version = _release_version(release)
    if not _is_newer_version(version, APP_VERSION):
        raise UpdateError(f"当前已是最新版本（{APP_VERSION}）。")
    archive_name, manifest_name = _expected_asset_names(version)
    manifest_path = staging_directory / manifest_name
    archive_path = staging_directory / archive_name
    _download_to_file(
        _asset_url(release, manifest_name),
        manifest_path,
        MAX_MANIFEST_BYTES,
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpdateError("更新清单无法读取或不是有效 JSON。") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != RELEASE_FORMAT
        or manifest.get("version") != version
        or manifest.get("platform") != "windows-termux"
        or manifest.get("archive") != archive_name
        or not isinstance(manifest.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"])
    ):
        raise UpdateError("更新清单格式或版本与 Release 不匹配。")
    _download_to_file(
        _asset_url(release, archive_name),
        archive_path,
        MAX_ARCHIVE_BYTES,
    )
    digest_builder = hashlib.sha256()
    with archive_path.open("rb") as archive_stream:
        for chunk in iter(lambda: archive_stream.read(1024 * 1024), b""):
            digest_builder.update(chunk)
    digest = digest_builder.hexdigest()
    if digest != manifest["sha256"]:
        raise UpdateError("更新包 SHA-256 校验失败；未安装任何文件。")
    package_directory = staging_directory / "package"
    file_count = _extract_validated_archive(archive_path, package_directory)
    metadata = {
        "format": RELEASE_FORMAT,
        "version": version,
        "archive": archive_name,
        "file_count": file_count,
    }
    _write_staging_metadata(staging_directory / "staging.json", metadata)
    return metadata


def install_prepared_update(
    staging_directory: Path,
    project_directory: Path,
) -> dict[str, object]:
    staging_directory = Path(staging_directory).resolve(strict=True)
    project_directory = Path(project_directory).resolve(strict=True)
    try:
        metadata = json.loads(
            (staging_directory / "staging.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise UpdateError("找不到有效的已校验更新暂存信息。") from error
    if not isinstance(metadata, dict) or metadata.get("format") != RELEASE_FORMAT:
        raise UpdateError("更新暂存信息格式无效。")
    version = metadata.get("version")
    if not isinstance(version, str):
        raise UpdateError("更新暂存信息缺少版本号。")
    _version_parts(version)

    package_directory = staging_directory / "package"
    staged_files: list[tuple[Path, Path]] = []
    for directory_name in ("程序", "网页"):
        source_root = package_directory / directory_name
        if not source_root.is_dir():
            raise UpdateError(f"更新暂存包缺少目录：{directory_name}")
        for source in sorted(source_root.rglob("*")):
            if source.is_symlink():
                raise UpdateError(f"更新暂存包包含符号链接：{source}")
            if source.is_file():
                relative = source.relative_to(package_directory)
                _validate_archive_member(relative.as_posix())
                staged_files.append((source, relative))
    if not staged_files or len(staged_files) != metadata.get("file_count"):
        raise UpdateError("更新暂存文件数量与校验信息不一致。")

    backup_root = staging_directory / "backup"
    changed: list[tuple[Path, Path | None]] = []
    try:
        for source, relative in staged_files:
            target = project_directory.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                raise UpdateError(f"拒绝覆盖符号链接：{target}")
            try:
                target.resolve(strict=False).relative_to(project_directory)
            except ValueError as error:
                raise UpdateError(f"更新目标超出程序目录：{target}") from error
            backup: Path | None = None
            if target.exists():
                if not target.is_file():
                    raise UpdateError(f"更新目标不是普通文件：{target}")
                backup = backup_root.joinpath(*relative.parts)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
            changed.append((target, backup))
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".update",
                dir=target.parent,
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(file_descriptor, "wb") as destination, source.open("rb") as input_file:
                    shutil.copyfileobj(input_file, destination)
                    destination.flush()
                    os.fsync(destination.fileno())
                temporary_path.replace(target)
            finally:
                temporary_path.unlink(missing_ok=True)
        metadata["installed_files"] = [relative.as_posix() for _, relative in staged_files]
        _write_staging_metadata(staging_directory / "staging.json", metadata)
    except (OSError, UpdateError) as error:
        rollback_errors: list[str] = []
        for target, backup in reversed(changed):
            try:
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    file_descriptor, temporary_name = tempfile.mkstemp(
                        prefix=f".{target.name}.",
                        suffix=".restore",
                        dir=target.parent,
                    )
                    temporary_path = Path(temporary_name)
                    try:
                        with os.fdopen(file_descriptor, "wb") as destination, backup.open("rb") as source:
                            shutil.copyfileobj(source, destination)
                            destination.flush()
                            os.fsync(destination.fileno())
                        temporary_path.replace(target)
                    finally:
                        temporary_path.unlink(missing_ok=True)
            except OSError as rollback_error:
                rollback_errors.append(f"{target}: {rollback_error}")
        if rollback_errors:
            raise UpdateError(
                f"更新失败：{error}；回滚也未能完整完成：{'；'.join(rollback_errors)}"
            ) from error
        raise UpdateError(f"更新失败，已回滚本次文件更改：{error}") from error
    return {"version": version, "file_count": len(staged_files)}


def rollback_prepared_update(
    staging_directory: Path,
    project_directory: Path,
) -> None:
    staging_directory = Path(staging_directory).resolve(strict=True)
    project_directory = Path(project_directory).resolve(strict=True)
    try:
        metadata = json.loads(
            (staging_directory / "staging.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise UpdateError("无法读取更新回滚记录。") from error
    installed_files = metadata.get("installed_files") if isinstance(metadata, dict) else None
    if not isinstance(installed_files, list) or not all(
        isinstance(relative, str) for relative in installed_files
    ):
        raise UpdateError("更新回滚记录格式无效。")

    backup_root = staging_directory / "backup"
    rollback_errors: list[str] = []
    for name in reversed(installed_files):
        relative = _validate_archive_member(name)
        target = project_directory.joinpath(*relative.parts)
        backup = backup_root.joinpath(*relative.parts)
        try:
            target.resolve(strict=False).relative_to(project_directory)
            if target.is_symlink():
                raise UpdateError(f"拒绝回滚符号链接：{target}")
            if backup.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                file_descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{target.name}.",
                    suffix=".restore",
                    dir=target.parent,
                )
                temporary_path = Path(temporary_name)
                try:
                    with os.fdopen(file_descriptor, "wb") as destination, backup.open("rb") as source:
                        shutil.copyfileobj(source, destination)
                        destination.flush()
                        os.fsync(destination.fileno())
                    temporary_path.replace(target)
                finally:
                    temporary_path.unlink(missing_ok=True)
            else:
                target.unlink(missing_ok=True)
        except (OSError, ValueError, UpdateError) as error:
            rollback_errors.append(f"{target}: {error}")
    if rollback_errors:
        raise UpdateError(f"无法完整回滚更新：{'；'.join(rollback_errors)}")
