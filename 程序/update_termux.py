from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from update_manager import (
    UpdateError,
    install_prepared_update,
    prepare_termux_update,
    rollback_prepared_update,
)


def _service_is_running(app_directory: Path) -> bool:
    pid_file = Path.home() / ".local" / "state" / "dicttango" / "service.pid"
    try:
        pid_text = pid_file.read_text(encoding="ascii").strip()
        pid = int(pid_text)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        command_line = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except (OSError, ValueError):
        return False
    expected_script = str((app_directory / "app.py").resolve()).encode()
    return expected_script in command_line


def _service_is_responding() -> bool:
    try:
        with urlopen("http://127.0.0.1:8766/", timeout=1):
            return True
    except (OSError, URLError, TimeoutError):
        return False


def _run_service_script(app_directory: Path, action: str) -> None:
    launcher = app_directory / "启动笔记服务-Termux.sh"
    if not launcher.is_file():
        raise UpdateError(f"找不到 Termux 服务脚本：{launcher}")
    environment = os.environ.copy()
    environment["DICTTANGO_APP_DIR"] = str(app_directory)
    try:
        result = subprocess.run(
            ["bash", str(launcher), action],
            capture_output=True,
            text=True,
            env=environment,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"Termux 服务脚本执行失败：{error}") from error
    if result.returncode:
        details = (result.stderr or result.stdout).strip()
        raise UpdateError(
            f"Termux 服务{ '停止' if action == 'stop' else '启动' }失败"
            + (f"：{details[-1200:]}" if details else f"（退出码 {result.returncode}）")
        )


def update(app_directory: Path) -> dict[str, object]:
    app_directory = Path(app_directory).resolve(strict=True)
    project_directory = app_directory.parent
    was_running = _service_is_running(app_directory)
    if not was_running and _service_is_responding():
        raise UpdateError(
            "检测到正在运行的笔记服务，但无法确认其启动进程；请先停止服务后再更新。"
        )
    with tempfile.TemporaryDirectory(prefix="tangonote-update-") as temporary:
        staging_directory = Path(temporary)
        staged = prepare_termux_update(staging_directory)
        package_files = [
            path
            for path in (staging_directory / "package").rglob("*")
            if path.is_file()
        ]
        if staged["file_count"] != len(package_files):
            raise UpdateError("更新暂存目录校验失败。")
        for path in package_files:
            if path.suffix == ".py":
                try:
                    compile(path.read_text(encoding="utf-8"), str(path), "exec")
                except (OSError, UnicodeDecodeError, SyntaxError) as error:
                    raise UpdateError(f"更新包中的 Python 文件无法编译：{path.name}") from error
        service_stopped = was_running
        try:
            if was_running:
                _run_service_script(app_directory, "stop")
            installed = install_prepared_update(
                staging_directory,
                project_directory,
            )
            if was_running:
                try:
                    _run_service_script(app_directory, "start")
                    service_stopped = False
                except UpdateError:
                    if _service_is_running(app_directory):
                        _run_service_script(app_directory, "stop")
                    rollback_prepared_update(staging_directory, project_directory)
                    raise
        except UpdateError:
            if service_stopped and not _service_is_running(app_directory):
                _run_service_script(app_directory, "start")
            raise
    return installed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download, verify, and safely install the latest TangoNote release on Termux."
    )
    parser.add_argument(
        "--app-directory",
        type=Path,
        default=Path(__file__).resolve().parent,
    )
    arguments = parser.parse_args()
    try:
        result = update(arguments.app_directory)
    except (UpdateError, OSError, subprocess.TimeoutExpired) as error:
        print(f"更新失败：{error}", file=sys.stderr)
        return 1
    print(
        f"已更新到 TangoNote {result['version']}，"
        f"校验并安装 {result['file_count']} 个程序文件。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
