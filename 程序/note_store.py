from __future__ import annotations

import base64
import mimetypes
import os
import re
import sqlite3
import stat
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import quote


DEFAULT_GROUP = "[Default]"
DEFAULT_NOTE_FILE = "MyNote-[Default].txt"
NOTE_FILE_PREFIX = "MyNote-"
MAX_TERM_LENGTH = 200
MAX_GROUP_LENGTH = 80
MAX_BODY_LENGTH = 5_000_000
ENTRY_PATTERN = re.compile(
    r"(?m)^([^\r\n]+)\r?\n<font size=5>([^\r\n]*)</font>\r?\n(.*?)\r?\n</>(?=\r?\n|$)",
    re.DOTALL,
)
INVALID_GROUP_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class StoreError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(value: str) -> str:
    return value.strip().casefold()


def _encode_id(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_id(value: str) -> str:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise StoreError("资源编号无效") from error


class NoteStore:
    """Reads and writes the flat text files used by GoldenDict-MyNote."""

    def __init__(self, notes_directory: Path, legacy_database_path: Path | None = None) -> None:
        self.notes_directory = Path(notes_directory).resolve()
        self.notes_directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._group_note_paths: dict[str, Path] = {}
        self._group_resource_directories: dict[str, Path] = {}
        if legacy_database_path is not None:
            self._migrate_legacy_database(Path(legacy_database_path))

    def set_notes_directory(self, notes_directory: Path) -> None:
        with self._lock:
            try:
                path = Path(notes_directory).resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as error:
                raise StoreError("笔记根文件夹不存在或无法访问") from error
            if not path.is_dir():
                raise StoreError("笔记根文件夹不存在或不是文件夹")
            self.notes_directory = path

    def group_resource_directories(self) -> dict[str, str]:
        with self._lock:
            return {
                group: str(directory)
                for group, directory in self._group_resource_directories.items()
            }

    def group_note_paths(self) -> dict[str, str]:
        with self._lock:
            return {filename: str(path) for filename, path in self._group_note_paths.items()}

    def set_group_note_paths(self, paths: dict[str, str]) -> None:
        normalized: dict[str, Path] = {}
        for filename, path_value in paths.items():
            if not self._is_group_filename(filename):
                raise StoreError("分组笔记文件名无效")
            path = Path(path_value).expanduser().resolve()
            if path.name != filename:
                raise StoreError("分组笔记文件名必须符合 MyNote 命名格式")
            normalized[filename] = path
        with self._lock:
            self._group_note_paths = normalized

    def set_group_resource_directories(self, directories: dict[str, str]) -> None:
        normalized: dict[str, Path] = {}
        for group, directory in directories.items():
            group_name = group if group == DEFAULT_GROUP else self._validate_group_name(group)
            path = Path(directory).expanduser().resolve()
            normalized[group_name] = path
        with self._lock:
            self._group_resource_directories = normalized

    @staticmethod
    def _group_file(name: str) -> str:
        return DEFAULT_NOTE_FILE if name == DEFAULT_GROUP else f"{NOTE_FILE_PREFIX}{name}.txt"

    @classmethod
    def _is_group_filename(cls, filename: str) -> bool:
        if filename == DEFAULT_NOTE_FILE:
            return True
        if not filename.startswith(NOTE_FILE_PREFIX) or not filename.endswith(".txt"):
            return False
        group_name = filename[len(NOTE_FILE_PREFIX) : -4]
        try:
            return cls._group_file(cls._validate_group_name(group_name)) == filename
        except StoreError:
            return False

    @staticmethod
    def _group_name(filename: str) -> str:
        if filename == DEFAULT_NOTE_FILE:
            return DEFAULT_GROUP
        return filename[len(NOTE_FILE_PREFIX) : -4]

    def _note_path(self, filename: str) -> Path:
        if not self._is_group_filename(filename):
            raise StoreError("笔记文件路径无效")
        with self._lock:
            configured_path = self._group_note_paths.get(filename)
        if configured_path is not None:
            return configured_path
        path = (self.notes_directory / filename).resolve()
        if path.parent != self.notes_directory:
            raise StoreError("笔记文件路径无效")
        return path

    def _default_group_folder(self, name: str, filename: str | None = None) -> Path:
        filename = filename or self._existing_group_file(name) or self._group_file(name)
        parent = self._note_path(filename).parent
        folder_name = f"MyNote-{name}"
        path = (parent / folder_name).resolve()
        if path.parent != parent:
            raise StoreError("分组目录路径无效")
        return path

    def _group_folder(self, name: str) -> Path:
        with self._lock:
            configured_folder = self._group_resource_directories.get(name)
        if configured_folder is not None:
            return configured_folder
        return self._default_group_folder(name)

    def group_paths(self) -> dict[str, dict[str, str]]:
        with self._lock:
            result = {}
            for group in self.list_groups():
                name = str(group["name"])
                filename = self._existing_group_file(name) or self._group_file(name)
                note_path = self._note_path(filename)
                default_note_path = (self.notes_directory / filename).resolve()
                custom_resources = self._group_resource_directories.get(name)
                result[name] = {
                    "note_path": str(note_path),
                    "default_note_path": str(default_note_path),
                    "resource_directory": str(custom_resources) if custom_resources else "",
                    "default_resource_directory": str(
                        self._default_group_folder(name, filename)
                    ),
                }
            return result

    def sync_paths(self) -> dict[str, Path]:
        with self._lock:
            if not self.notes_directory.is_dir():
                raise StoreError("笔记根文件夹不存在或无法访问", 500)
            paths: dict[str, Path] = {}
            for filename in self._group_files():
                note_path = self._note_path(filename)
                if filename in self._group_note_paths and not note_path.parent.is_dir():
                    raise StoreError(
                        f"分组笔记文件夹不存在或无法访问：{note_path.parent}",
                        500,
                    )
                try:
                    mode = note_path.stat().st_mode
                except FileNotFoundError:
                    continue
                except OSError as error:
                    raise StoreError(
                        f"无法读取分组笔记文件 {note_path.name}: {error}",
                        500,
                    ) from error
                if stat.S_ISREG(mode):
                    paths[f"notes/{filename}"] = note_path

            configured_resource_groups = set(self._group_resource_directories)
            for group in self.list_groups():
                group_name = str(group["name"])
                folder = self._group_folder(group_name)
                if not folder.is_dir():
                    if group_name in configured_resource_groups:
                        raise StoreError(
                            f"分组资源文件夹不存在或无法访问：{folder}",
                            500,
                        )
                    continue

                def raise_walk_error(error: OSError) -> None:
                    raise StoreError(
                        f"无法读取分组资源文件夹 {folder}: {error}",
                        500,
                    ) from error

                for current_directory, _, filenames in os.walk(
                    folder,
                    followlinks=False,
                    onerror=raise_walk_error,
                ):
                    current_path = Path(current_directory)
                    for filename in filenames:
                        candidate = current_path / filename
                        try:
                            mode = candidate.lstat().st_mode
                        except OSError as error:
                            raise StoreError(
                                f"无法读取分组资源文件 {candidate.name}: {error}",
                                500,
                            ) from error
                        if not stat.S_ISREG(mode):
                            continue
                        relative_path = candidate.relative_to(folder)
                        paths[f"assets/{group_name}/{relative_path.as_posix()}"] = candidate
            return paths

    def sync_path(self, logical_path: str) -> Path:
        parts = logical_path.split("/")
        if len(parts) == 2 and parts[0] == "notes":
            filename = parts[1]
            if not self._is_group_filename(filename):
                raise StoreError("同步笔记路径无效")
            return self._note_path(filename)
        if len(parts) >= 3 and parts[0] == "assets":
            group_name = parts[1]
            if group_name != DEFAULT_GROUP:
                group_name = self._validate_group_name(group_name)
            relative_path = "/".join(parts[2:])
            if any(part in {"", ".", ".."} for part in relative_path.split("/")):
                raise StoreError("同步附件路径无效")
            return self.asset_path(group_name, relative_path)
        raise StoreError("同步文件路径无效")

    def sync_roots(self) -> list[Path]:
        with self._lock:
            roots = [self.notes_directory]
            roots.extend(path.parent for path in self._group_note_paths.values())
            roots.extend(
                self._group_folder(str(group["name"]))
                for group in self.list_groups()
            )
            return list(dict.fromkeys(path.resolve() for path in roots))

    @staticmethod
    def _validate_group_name(name: object) -> str:
        if not isinstance(name, str):
            raise StoreError("分组名称必须是文本")
        clean_name = name.strip()
        if not clean_name:
            raise StoreError("分组名称不能为空")
        if len(clean_name) > MAX_GROUP_LENGTH:
            raise StoreError(f"分组名称不能超过 {MAX_GROUP_LENGTH} 个字符")
        if (
            INVALID_GROUP_CHARACTERS.search(clean_name)
            or clean_name in {".", ".."}
            or clean_name.endswith((".", " "))
            or normalize(clean_name) in {normalize(DEFAULT_GROUP), "默认"}
        ):
            raise StoreError("分组名称包含不支持的字符或名称")
        return clean_name

    def _validate_note(self, data: dict[str, object]) -> tuple[str, str, str]:
        term = data.get("term")
        group = data.get("group", DEFAULT_GROUP)
        body = data.get("body", "")
        if not isinstance(term, str) or not term.strip():
            raise StoreError("词条不能为空")
        term = term.strip()
        if (
            len(term) > MAX_TERM_LENGTH
            or "\n" in term
            or "\r" in term
            or "</font>" in term.casefold()
            or "</>" in term
        ):
            raise StoreError(f"词条必须为单行且不能超过 {MAX_TERM_LENGTH} 个字符")
        if not isinstance(body, str):
            raise StoreError("笔记内容必须是文本")
        if len(body) > MAX_BODY_LENGTH:
            raise StoreError(f"笔记内容不能超过 {MAX_BODY_LENGTH} 个字符")
        if not isinstance(group, str):
            raise StoreError("分组名称必须是文本")
        group = group.strip() or DEFAULT_GROUP
        if normalize(group) in {"[default]", "默认"}:
            group = DEFAULT_GROUP
        if group != DEFAULT_GROUP:
            group = self._validate_group_name(group)
        return term, group, body

    def _read_file(self, filename: str) -> str:
        path = self._note_path(filename)
        if not path.exists():
            return ""
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return stream.read()

    def _write_file(self, filename: str, content: str) -> None:
        path = self._note_path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(content)
                stream.flush()
            temporary_path.replace(path)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise StoreError(f"无法保存笔记文件 {path.name}: {error}", 500) from error

    def _group_files(self) -> list[str]:
        filenames = {
            path.name
            for path in self.notes_directory.iterdir()
            if path.is_file()
            and path.resolve().parent == self.notes_directory
            and self._is_group_filename(path.name)
            and path.name not in self._group_note_paths
        }
        filenames.update(self._group_note_paths)
        return sorted(
            filenames,
            key=lambda filename: (filename != DEFAULT_NOTE_FILE, filename.casefold()),
        )

    def _existing_group_file(self, name: str) -> str | None:
        return next(
            (
                filename
                for filename in self._group_files()
                if normalize(self._group_name(filename)) == normalize(name)
            ),
            None,
        )

    def _entries(self, filename: str) -> list[dict[str, object]]:
        content = self._read_file(filename)
        group = self._group_name(filename)
        path = self._note_path(filename)
        file_stat = path.stat() if path.exists() else None
        created_at = (
            datetime.fromtimestamp(file_stat.st_ctime, timezone.utc).isoformat(timespec="seconds")
            if file_stat else ""
        )
        updated_at = (
            datetime.fromtimestamp(file_stat.st_mtime, timezone.utc).isoformat(timespec="seconds")
            if file_stat else ""
        )
        entries = []
        for match in ENTRY_PATTERN.finditer(content):
            term = match.group(1).strip()
            if not term or match.group(2).strip() != term:
                continue
            entries.append(
                {
                    "id": _encode_id(f"{filename}\0{term}"),
                    "term": term,
                    "group": group,
                    "body": match.group(3),
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "_start": match.start(),
                    "_end": match.end(),
                    "_entry": match.group(0),
                    "_filename": filename,
                }
            )
        return entries

    @staticmethod
    def _public_note(note: dict[str, object]) -> dict[str, object]:
        return {key: value for key, value in note.items() if not key.startswith("_")}

    def _find(self, note_id: str) -> dict[str, object] | None:
        decoded = _decode_id(note_id)
        if "\0" not in decoded:
            raise StoreError("笔记编号无效")
        filename, term = decoded.split("\0", 1)
        self._note_path(filename)
        return next(
            (
                note
                for note in self._entries(filename)
                if normalize(str(note["term"])) == normalize(term)
            ),
            None,
        )

    def list_groups(self) -> list[dict[str, object]]:
        with self._lock:
            files = self._group_files()
            if DEFAULT_NOTE_FILE not in files:
                files.insert(0, DEFAULT_NOTE_FILE)
            result = []
            for filename in files:
                name = self._group_name(filename)
                result.append(
                    {
                        "id": _encode_id(filename),
                        "name": name,
                        "is_default": filename == DEFAULT_NOTE_FILE,
                        "note_count": len(self._entries(filename)),
                    }
                )
            return result

    def list_notes(self, query: str = "", group_id: str | None = None) -> list[dict[str, object]]:
        with self._lock:
            selected_filename = _decode_id(group_id) if group_id else None
            if selected_filename is not None:
                self._note_path(selected_filename)
                if selected_filename not in self._group_files():
                    return []
            notes = [
                note
                for filename in self._group_files()
                if selected_filename is None or filename == selected_filename
                for note in self._entries(filename)
            ]
            needle = normalize(query)
            if needle:
                notes = [
                    note
                    for note in notes
                    if needle in normalize(str(note["term"]))
                    or needle in normalize(str(note["body"]))
                    or needle in normalize(str(note["group"]))
                ]
            notes.sort(key=lambda note: (normalize(str(note["term"])), normalize(str(note["group"]))))
            return [self._public_note(note) for note in notes]

    def get_note(self, note_id: str) -> dict[str, object] | None:
        with self._lock:
            note = self._find(note_id)
            return self._public_note(note) if note else None

    def create_note(self, data: dict[str, object]) -> dict[str, object]:
        term, group, body = self._validate_note(data)
        with self._lock:
            filename = self._existing_group_file(group) or self._group_file(group)
            content = self._read_file(filename)
            if any(normalize(str(note["term"])) == normalize(term) for note in self._entries(filename)):
                raise StoreError("该词条在此分组中已存在", 409)
            entry = f"{term}\n<font size=5>{term}</font>\n{body}\n</>"
            self._write_file(filename, content.rstrip() + ("\n" if content.strip() else "") + entry)
        note = self.get_note(_encode_id(f"{filename}\0{term}"))
        if note is None:
            raise StoreError("笔记保存后无法读取", 500)
        return note

    def update_note(self, note_id: str, data: dict[str, object]) -> dict[str, object]:
        term, group, body = self._validate_note(data)
        with self._lock:
            current = self._find(note_id)
            if current is None:
                raise StoreError("没有找到这条笔记", 404)
            filename = str(current["_filename"])
            target_filename = self._existing_group_file(group) or self._group_file(group)
            if filename != target_filename:
                if any(
                    normalize(str(note["term"])) == normalize(term)
                    for note in self._entries(target_filename)
                ):
                    raise StoreError("目标分组中已存在同名词条", 409)
                old_content = self._read_file(filename)
                start, end = int(current["_start"]), int(current["_end"])
                updated_old = old_content[:start] + old_content[end:]
                if updated_old.strip() == "":
                    updated_old = ""
                target_content = self._read_file(target_filename)
                entry = f"{term}\n<font size=5>{term}</font>\n{body}\n</>"
                updated_target = target_content.rstrip() + (
                    "\n" if target_content.strip() else ""
                ) + entry
                self._write_file(target_filename, updated_target)
                try:
                    self._write_file(filename, updated_old)
                except StoreError as error:
                    try:
                        self._write_file(target_filename, target_content)
                    except StoreError as rollback_error:
                        raise StoreError(
                            f"移动失败，且无法回滚目标分组文件: {rollback_error}",
                            500,
                        ) from error
                    raise
            else:
                if normalize(str(current["term"])) != normalize(term) and any(
                    normalize(str(note["term"])) == normalize(term)
                    for note in self._entries(filename)
                ):
                    raise StoreError("该词条在此分组中已存在", 409)
                content = self._read_file(filename)
                replacement = f"{term}\n<font size=5>{term}</font>\n{body}\n</>"
                start, end = int(current["_start"]), int(current["_end"])
                self._write_file(filename, content[:start] + replacement + content[end:])
        note = self.get_note(_encode_id(f"{target_filename}\0{term}"))
        if note is None:
            raise StoreError("笔记保存后无法读取", 500)
        return note

    def delete_note(self, note_id: str) -> bool:
        with self._lock:
            note = self._find(note_id)
            if note is None:
                return False
            filename = str(note["_filename"])
            content = self._read_file(filename)
            start, end = int(note["_start"]), int(note["_end"])
            updated = content[:start] + content[end:]
            self._write_file(filename, updated if updated.strip() else "")
            return True

    def create_group(
        self,
        name: object,
        note_path: Path | None = None,
    ) -> dict[str, object]:
        clean_name = self._validate_group_name(name)
        filename = self._group_file(clean_name)
        with self._lock:
            if self._existing_group_file(clean_name):
                raise StoreError("该分组已存在", 409)
            custom_path = None
            if note_path is not None:
                try:
                    custom_path = Path(note_path).expanduser().resolve()
                except (OSError, RuntimeError, ValueError) as error:
                    raise StoreError("分组 TXT 文件路径无效") from error
                default_path = (self.notes_directory / filename).resolve()
                if custom_path.name != filename:
                    raise StoreError(f"TXT 文件名必须为 {filename}，以保持 MyNote 分组对应关系")
                if not custom_path.parent.is_dir():
                    raise StoreError("TXT 文件所在文件夹不存在或无法访问")
                if custom_path != default_path:
                    for existing_filename in self._group_files():
                        if self._note_path(existing_filename) == custom_path:
                            raise StoreError("此 TXT 文件已指定给另一个分组", 409)
                    if custom_path.exists() and not custom_path.is_file():
                        raise StoreError("分组 TXT 路径不是文件")

            if custom_path is None or custom_path == (self.notes_directory / filename).resolve():
                self._write_file(filename, "")
            else:
                self._group_note_paths[filename] = custom_path
                if not custom_path.exists():
                    try:
                        with custom_path.open("x", encoding="utf-8", newline=""):
                            pass
                    except FileExistsError as error:
                        self._group_note_paths.pop(filename, None)
                        raise StoreError("分组 TXT 文件已存在", 409) from error
                    except OSError as error:
                        self._group_note_paths.pop(filename, None)
                        raise StoreError(f"无法创建分组 TXT 文件: {error}", 500) from error
        return next(group for group in self.list_groups() if group["name"] == clean_name)

    def rename_group(self, group_id: str, name: object) -> dict[str, object]:
        new_name = self._validate_group_name(name)
        filename = _decode_id(group_id)
        self._note_path(filename)
        if filename == DEFAULT_NOTE_FILE:
            raise StoreError("默认分组不能重命名")
        if normalize(self._group_name(filename)) == normalize(new_name):
            return next(group for group in self.list_groups() if group["id"] == group_id)
        target_filename = self._group_file(new_name)
        with self._lock:
            if filename not in self._group_files():
                raise StoreError("没有找到这个分组", 404)
            existing_target = self._existing_group_file(new_name)
            if existing_target and existing_target != filename:
                raise StoreError("该分组已存在", 409)
            old_name = self._group_name(filename)
            old_resource_directory = self._group_resource_directories.get(old_name)
            old_note_path = self._note_path(filename)
            old_note_path_override = self._group_note_paths.get(filename)
            old_default_note_path = self.notes_directory / filename
            new_default_note_path = self.notes_directory / target_filename
            has_default_shadow = (
                old_note_path_override is not None
                and old_default_note_path.is_file()
            )
            if has_default_shadow and new_default_note_path.exists():
                raise StoreError("目标分组的根目录 TXT 文件已存在", 409)
            new_note_path = (
                old_note_path.with_name(target_filename)
                if old_note_path_override is not None
                else self._note_path(target_filename)
            )
            if new_note_path.exists() and new_note_path != old_note_path:
                raise StoreError("目标分组笔记文件已存在", 409)
            if new_name in self._group_resource_directories and new_name != old_name:
                raise StoreError("目标分组已有单独设置的资源文件夹", 409)
            old_folder = self._group_folder(old_name)
            new_folder = (
                old_note_path.parent / f"MyNote-{new_name}"
                if old_name != DEFAULT_GROUP and old_resource_directory is None
                else self._group_folder(new_name)
            )
            if old_resource_directory is None and old_folder.exists() and new_folder.exists():
                raise StoreError("目标分组资源目录已存在", 409)
            moved_folder = False
            moved_note = False
            moved_default_shadow = False
            try:
                if old_resource_directory is None and old_folder.exists():
                    old_folder.replace(new_folder)
                    moved_folder = True
                old_note_path.replace(new_note_path)
                moved_note = True
                if has_default_shadow:
                    old_default_note_path.replace(new_default_note_path)
                    moved_default_shadow = True
            except OSError as error:
                rollback_errors: list[str] = []
                if moved_default_shadow and new_default_note_path.exists():
                    try:
                        new_default_note_path.replace(old_default_note_path)
                    except OSError as rollback_error:
                        rollback_errors.append(str(rollback_error))
                if moved_note and new_note_path.exists():
                    try:
                        new_note_path.replace(old_note_path)
                    except OSError as rollback_error:
                        rollback_errors.append(str(rollback_error))
                if moved_folder and new_folder.exists():
                    try:
                        new_folder.replace(old_folder)
                    except OSError as rollback_error:
                        rollback_errors.append(str(rollback_error))
                if rollback_errors:
                    raise StoreError(
                        f"无法重命名分组，且回滚失败：{error}；{'；'.join(rollback_errors)}",
                        500,
                    ) from error
                raise StoreError(f"无法重命名分组: {error}", 500) from error
            if old_note_path_override is not None:
                self._group_note_paths.pop(filename, None)
                self._group_note_paths[target_filename] = new_note_path
            if old_resource_directory is not None:
                self._group_resource_directories.pop(old_name, None)
                self._group_resource_directories[new_name] = old_resource_directory
        return next(group for group in self.list_groups() if group["name"] == new_name)

    def delete_group(self, group_id: str) -> None:
        filename = _decode_id(group_id)
        self._note_path(filename)
        with self._lock:
            if filename == DEFAULT_NOTE_FILE:
                raise StoreError("默认分组不能删除")
            if filename not in self._group_files():
                raise StoreError("没有找到这个分组", 404)
            if self._entries(filename):
                raise StoreError("请先移动或删除该分组中的笔记")
            try:
                self._note_path(filename).unlink(missing_ok=True)
                if filename in self._group_note_paths:
                    default_path = self.notes_directory / filename
                    default_path.unlink(missing_ok=True)
            except OSError as error:
                raise StoreError(f"無法刪除分組文件: {error}", 500) from error
            self._group_note_paths.pop(filename, None)
            self._group_resource_directories.pop(self._group_name(filename), None)

    def lookup(self, term: str, group_name: str = "") -> list[dict[str, object]]:
        needle = normalize(term)
        if not needle:
            return []
        if normalize(group_name) in {"[default]", "默认"}:
            group_name = DEFAULT_GROUP
        with self._lock:
            return [
                note
                for filename in self._group_files()
                if not group_name or normalize(self._group_name(filename)) == normalize(group_name)
                for note in self._entries(filename)
                if normalize(str(note["term"])) == needle
            ]

    def export_data(self) -> dict[str, object]:
        return {
            "format": "dict-tango-local-notes",
            "version": 1,
            "exported_at": timestamp(),
            "groups": [str(group["name"]) for group in self.list_groups()],
            "notes": [
                {
                    "term": note["term"],
                    "group": note["group"],
                    "body": note["body"],
                }
                for note in self.list_notes()
            ],
        }

    def import_data(self, data: object) -> dict[str, int]:
        if not isinstance(data, dict) or data.get("format") != "dict-tango-local-notes":
            raise StoreError("备份文件格式不正确")
        if data.get("version") != 1 or not isinstance(data.get("notes"), list):
            raise StoreError("暂不支持此备份版本")
        groups = data.get("groups", [])
        if not isinstance(groups, list) or not all(isinstance(group, str) for group in groups):
            raise StoreError("备份中的分组列表无效")
        clean_groups = [
            DEFAULT_GROUP if normalize(group) in {"[default]", "默认"}
            else self._validate_group_name(group)
            for group in groups
            if group.strip()
        ]
        validated_notes = [
            self._validate_note(
                {
                    "term": item.get("term"),
                    "group": item.get("group", DEFAULT_GROUP),
                    "body": item.get("body", ""),
                }
            )
            for item in data["notes"]
            if isinstance(item, dict)
        ]
        if len(validated_notes) != len(data["notes"]):
            raise StoreError("备份中包含无效笔记")
        for _, group, _ in validated_notes:
            if group != DEFAULT_GROUP and group not in clean_groups:
                clean_groups.append(group)
        seen: set[tuple[str, str]] = set()
        for term, group, _ in validated_notes:
            identity = (normalize(group), normalize(term))
            if identity in seen:
                raise StoreError("备份中包含重复词条")
            seen.add(identity)

        created = updated = 0
        with self._lock:
            for group in clean_groups:
                filename = self._group_file(group)
                if filename not in self._group_files():
                    self._write_file(filename, "")
            for term, group, body in validated_notes:
                filename = self._group_file(group)
                existing = next(
                    (
                        note
                        for note in self._entries(filename)
                        if normalize(str(note["term"])) == normalize(term)
                    ),
                    None,
                )
                if existing:
                    self.update_note(
                        str(existing["id"]),
                        {"term": term, "group": group, "body": body},
                    )
                    updated += 1
                else:
                    self.create_note({"term": term, "group": group, "body": body})
                    created += 1
        return {"created": created, "updated": updated}

    def _migrate_legacy_database(self, database_path: Path) -> None:
        marker = self.notes_directory / "config" / "legacy-sqlite-migrated"
        if marker.exists() or not database_path.is_file():
            return
        with self._lock:
            if self._group_files():
                return
            uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(uri, uri=True)
                connection.row_factory = sqlite3.Row
                groups = connection.execute(
                    "SELECT id, name, is_default FROM note_groups ORDER BY id"
                ).fetchall()
                notes = connection.execute(
                    """
                    SELECT n.term, n.body, g.name AS group_name, g.is_default,
                           g.id AS group_id
                    FROM notes n JOIN note_groups g ON g.id = n.group_id
                    ORDER BY n.id
                    """
                ).fetchall()
            except sqlite3.Error as error:
                raise StoreError(f"无法迁移旧版 SQLite 笔记: {error}", 500) from error
            finally:
                if connection is not None:
                    connection.close()
            group_names = {
                row["id"]: (
                    DEFAULT_GROUP
                    if row["is_default"] or normalize(row["name"]) in {"默认", "[default]"}
                    else self._validate_group_name(row["name"])
                )
                for row in groups
            }
            for row in notes:
                self.create_note(
                    {
                        "term": row["term"],
                        "group": (
                            DEFAULT_GROUP
                            if row["is_default"]
                            else group_names.get(row["group_id"], row["group_name"])
                        ),
                        "body": row["body"],
                    }
                )
            for row in groups:
                name = group_names[row["id"]]
                if name != DEFAULT_GROUP and self._group_file(name) not in self._group_files():
                    self.create_group(name)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                f"Legacy database migrated at {timestamp()}\nSource: {database_path.resolve()}\n",
                encoding="utf-8",
            )

    def save_asset(self, group: str, filename: str, content: bytes) -> str:
        group_name = group or DEFAULT_GROUP
        if group_name != DEFAULT_GROUP:
            group_name = self._validate_group_name(group_name)
        clean_name = Path(filename.replace("\\", "/")).name.strip()
        clean_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", clean_name).strip(" .")
        if not clean_name:
            clean_name = "attachment"
        if len(content) > 20_000_000:
            raise StoreError("附件不能超过 20 MB", 413)
        content_type = mimetypes.guess_type(clean_name)[0] or ""
        if not content_type.startswith(("image/", "audio/", "video/")) or content_type == "image/svg+xml":
            raise StoreError("仅支持常见图片、音频和视频附件", 415)
        folder = self._group_folder(group_name)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            stem, suffix = Path(clean_name).stem, Path(clean_name).suffix
            candidate = clean_name
            counter = 1
            while (folder / candidate).exists():
                candidate = f"{stem}_{counter}{suffix}"
                counter += 1
            with tempfile.NamedTemporaryFile("wb", dir=folder, prefix=".upload-", delete=False) as stream:
                temporary_path = Path(stream.name)
                stream.write(content)
                stream.flush()
            temporary_path.replace(folder / candidate)
            return candidate
        except OSError as error:
            if "temporary_path" in locals():
                temporary_path.unlink(missing_ok=True)
            raise StoreError(f"无法保存附件: {error}", 500) from error

    def delete_unreferenced_image_assets(
        self,
        group: str,
        filenames: list[str],
    ) -> list[str]:
        group_name = group or DEFAULT_GROUP
        if group_name != DEFAULT_GROUP:
            group_name = self._validate_group_name(group_name)
        candidates = list(dict.fromkeys(filenames))
        for filename in candidates:
            if (
                not filename
                or Path(filename).name != filename
                or "/" in filename
                or "\\" in filename
            ):
                raise StoreError("图片附件名称无效")
            content_type = mimetypes.guess_type(filename)[0] or ""
            if not content_type.startswith("image/") or content_type == "image/svg+xml":
                raise StoreError("只能清理图片附件")

        deleted: list[str] = []
        with self._lock:
            note_bodies = [
                str(note["body"])
                for note_file in self._group_files()
                for note in self._entries(note_file)
            ]
            folder = self._group_folder(group_name)
            for filename in candidates:
                encoded_name = quote(filename, safe="").casefold()
                if any(
                    filename.casefold() in body.casefold()
                    or encoded_name in body.casefold()
                    for body in note_bodies
                ):
                    continue
                candidate_path = folder / filename
                if candidate_path.is_symlink():
                    continue
                path = self.asset_path(group_name, filename)
                if not path.is_file():
                    continue
                try:
                    path.unlink()
                except OSError as error:
                    raise StoreError(f"无法删除图片附件 {filename}: {error}", 500) from error
                deleted.append(filename)
        return deleted

    def asset_path(self, group: str, filename: str) -> Path:
        group_name = group or DEFAULT_GROUP
        if group_name != DEFAULT_GROUP:
            group_name = self._validate_group_name(group_name)
        normalized_name = filename.replace("\\", "/")
        relative_path = Path(normalized_name)
        if (
            not normalized_name
            or relative_path.is_absolute()
            or any(part in {"", ".", ".."} for part in normalized_name.split("/"))
        ):
            raise StoreError("附件路径无效")
        folder = self._group_folder(group_name)
        path = (folder / relative_path).resolve()
        if not path.is_relative_to(folder):
            raise StoreError("附件路径无效")
        return path

    def group_resources(self, group: str) -> dict[str, list[str]]:
        group_name = group or DEFAULT_GROUP
        if group_name != DEFAULT_GROUP:
            group_name = self._validate_group_name(group_name)
        folder = self._group_folder(group_name)
        resources = {"css": [], "fonts": [], "js": []}
        if not folder.is_dir():
            return resources
        try:
            for entry in folder.iterdir():
                if not entry.is_file() or entry.resolve().parent != folder:
                    continue
                extension = entry.suffix.casefold()
                if extension == ".css":
                    resources["css"].append(entry.name)
                elif extension in {".ttf", ".otf", ".woff", ".woff2"}:
                    resources["fonts"].append(entry.name)
                elif extension == ".js":
                    resources["js"].append(entry.name)
        except OSError as error:
            raise StoreError(f"无法读取分组资源目录: {error}", 500) from error
        for filenames in resources.values():
            filenames.sort(key=str.casefold)
        return resources
