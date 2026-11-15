from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, TypeVar

from note_store import StoreError
from sync_manager import SYNC_DIRECTORY_NAME


REMOTE_PATTERN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*):([^\x00\r\n]*)$")
RCLONE_TIMEOUT_SECONDS = 180
Result = TypeVar("Result")


def validate_rclone_remote(remote: str) -> str:
    if not isinstance(remote, str):
        raise StoreError("OneDrive rclone 路径格式无效")
    remote = remote.strip()
    if not remote:
        return ""
    if len(remote) > 1024 or not REMOTE_PATTERN.fullmatch(remote):
        raise StoreError(
            "OneDrive rclone 路径格式无效，请填写“远程名称:文件夹”，例如 onedrive:MyNote"
        )
    return remote


def _remote_sync_path(remote: str) -> str:
    name, path = remote.split(":", 1)
    if path.rstrip("/"):
        return f"{name}:{path.rstrip('/')}/{SYNC_DIRECTORY_NAME}"
    return f"{name}:{SYNC_DIRECTORY_NAME}"


def _copy_rclone(source: str, destination: str) -> None:
    executable = shutil.which("rclone")
    if executable is None:
        raise StoreError(
            "Termux 未安装 rclone。请先运行 pkg install rclone，并完成 OneDrive 远程配置。"
        )
    try:
        result = subprocess.run(
            [
                executable,
                "copy",
                source,
                destination,
                "--checksum",
                "--checkers",
                "4",
                "--transfers",
                "4",
                "--retries",
                "2",
                "--low-level-retries",
                "2",
            ],
            capture_output=True,
            text=True,
            timeout=RCLONE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise StoreError(
            "OneDrive rclone 同步超时；请检查网络后重试。"
        ) from error
    except OSError as error:
        raise StoreError(f"无法启动 rclone：{error}") from error
    if result.returncode:
        details = (result.stderr or result.stdout).strip()
        if len(details) > 1200:
            details = details[-1200:]
        raise StoreError(
            "OneDrive rclone 传输失败"
            + (f"：{details}" if details else f"（退出码 {result.returncode}）")
        )


def sync_through_rclone(
    remote: str,
    sync_directory: Path,
    sync_callback: Callable[[], Result],
) -> Result:
    validated_remote = validate_rclone_remote(remote)
    if not validated_remote:
        raise StoreError("尚未设置 OneDrive rclone 路径")
    sync_directory = Path(sync_directory)
    local_state = sync_directory / SYNC_DIRECTORY_NAME
    remote_state = _remote_sync_path(validated_remote)

    executable = shutil.which("rclone")
    if executable is None:
        raise StoreError(
            "Termux 未安装 rclone。请先运行 pkg install rclone，并完成 OneDrive 远程配置。"
        )
    try:
        result = subprocess.run(
            [executable, "mkdir", remote_state],
            capture_output=True,
            text=True,
            timeout=RCLONE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise StoreError("连接 OneDrive rclone 远程超时。") from error
    except OSError as error:
        raise StoreError(f"无法启动 rclone：{error}") from error
    if result.returncode:
        details = (result.stderr or result.stdout).strip()
        if len(details) > 1200:
            details = details[-1200:]
        raise StoreError(
            "无法访问 OneDrive rclone 路径"
            + (f"：{details}" if details else f"（退出码 {result.returncode}）")
        )

    _copy_rclone(remote_state, str(local_state))
    result = sync_callback()
    _copy_rclone(str(local_state), remote_state)
    return result
