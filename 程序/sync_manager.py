from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from highlight_manager import HighlightManager
from note_store import NoteStore, StoreError


SYNC_FORMAT = "dict-tango-local-sync"
SYNC_VERSION = 1
SYNC_DIRECTORY_NAME = ".dict-tango-sync"
MAX_SYNC_FILE_BYTES = 100_000_000
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DEVICE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_sync_state_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / ".local" / "state"
    return base / "DictTango" / "sync-state.json"


class SyncManager:
    def __init__(
        self,
        store: NoteStore,
        highlight_manager: HighlightManager,
        sync_directory: Path | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.store = store
        self.highlight_manager = highlight_manager
        self.sync_directory = Path(sync_directory).expanduser().resolve() if sync_directory else None
        self.state_path = (state_path or default_sync_state_path()).resolve()
        self._lock = threading.RLock()

    def validate_sync_directory(self, directory: str | Path) -> Path:
        try:
            path = Path(directory).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as error:
            raise StoreError("同步文件夹不存在或无法访问") from error
        if not path.is_dir():
            raise StoreError("同步路径不是文件夹")

        source_roots = self.store.sync_roots()
        for source_root in source_roots:
            if path == source_root or source_root in path.parents:
                raise StoreError(
                    "同步文件夹不能位于笔记或附件目录内部"
                )
        return path

    def status(self) -> dict[str, Any]:
        with self._lock, self.store._lock:
            state = self._read_state()
            if not self.state_path.exists():
                self._write_state(state)
            return {
                "configured": self.sync_directory is not None,
                "sync_directory": str(self.sync_directory) if self.sync_directory else "",
                "device_id": state["device_id"],
                "last_sync_at": state["last_sync_at"],
                "conflicts": [
                    self._public_conflict(path, conflict)
                    for path, conflict in sorted(state["conflicts"].items())
                ],
            }

    def set_sync_directory(self, directory: str) -> Path | None:
        if not isinstance(directory, str):
            raise StoreError("同步文件夹路径无效")
        if not directory.strip():
            return None
        return self.validate_sync_directory(directory)

    def sync(self) -> dict[str, Any]:
        with self._lock, self.store._lock:
            if self.sync_directory is None:
                raise StoreError("请先在设置中选择 OneDrive 同步文件夹")
            remote_root = self._prepare_remote_root()
            state = self._read_state()
            events_by_path = self._read_events(remote_root)
            local_files = self._scan_local_files()
            baseline: dict[str, dict[str, Any]] = state["baseline"]
            conflicts: dict[str, dict[str, Any]] = state["conflicts"]
            uploaded = 0
            downloaded = 0

            paths = set(local_files) | set(baseline) | set(events_by_path) | set(conflicts)
            for logical_path in sorted(paths):
                local_path = local_files.get(logical_path, self._resolve_path(logical_path))
                local_hash = self._file_hash(local_path) if local_path.is_file() else None
                heads = self._heads(events_by_path.get(logical_path, []))
                head_ids = sorted(str(event["id"]) for event in heads)
                remote_hashes = {event["content_hash"] for event in heads}
                if len(remote_hashes) > 1:
                    conflicts[logical_path] = self._make_conflict(
                        local_hash, heads
                    )
                    continue
                head = heads[0] if heads else None
                remote_hash = head["content_hash"] if head else None
                base = baseline.get(logical_path)

                if logical_path in conflicts:
                    if not heads:
                        raise StoreError(
                            f"同步数据不完整：云端找不到“{logical_path}”的冲突版本"
                        )
                    conflicts[logical_path] = self._make_conflict(
                        local_hash, heads
                    )
                    continue

                if base is None:
                    if head and local_hash == remote_hash:
                        baseline[logical_path] = self._baseline_record(
                            remote_hash, head_ids
                        )
                        continue
                    if head and local_hash is None and remote_hash is not None:
                        self._apply_event(
                            remote_root,
                            logical_path,
                            head,
                            local_path,
                            None,
                        )
                        baseline[logical_path] = self._baseline_record(
                            remote_hash, head_ids
                        )
                        downloaded += 1
                        continue
                    if not head and local_hash is None:
                        continue
                    if local_hash is not None and head is None:
                        content = self._read_local_content(local_path, local_hash)
                        event = self._publish_event(
                            remote_root,
                            logical_path,
                            content,
                            [],
                            state["device_id"],
                        )
                        events_by_path.setdefault(logical_path, []).append(event)
                        baseline[logical_path] = self._baseline_record(
                            local_hash, [event["id"]]
                        )
                        uploaded += 1
                        continue
                    if head:
                        conflicts[logical_path] = self._make_conflict(
                            local_hash, heads
                        )
                    continue

                base_heads = set(base["heads"])
                if base_heads and not heads:
                    raise StoreError(
                        f"同步数据不完整：云端找不到“{logical_path}”的已知版本"
                    )
                local_changed = local_hash != base["content_hash"]
                remote_changed = set(head_ids) != base_heads
                if local_hash == remote_hash and head:
                    baseline[logical_path] = self._baseline_record(
                        remote_hash, head_ids
                    )
                    continue
                if local_changed and remote_changed:
                    if (
                        logical_path.startswith("notes/")
                        and len(heads) == 1
                        and head is not None
                        and isinstance(base["content_hash"], str)
                        and local_hash is not None
                        and remote_hash is not None
                    ):
                        base_content = self._read_blob(
                            remote_root,
                            {"content_hash": base["content_hash"]},
                        )
                        local_content = self._read_local_content(
                            local_path, local_hash
                        )
                        remote_content = self._read_blob(remote_root, head)
                        merged_content = self._merge_independent_line_changes(
                            base_content,
                            local_content,
                            remote_content,
                        )
                        if merged_content is not None:
                            event = self._publish_event(
                                remote_root,
                                logical_path,
                                merged_content,
                                head_ids,
                                state["device_id"],
                            )
                            self._apply_event(
                                remote_root,
                                logical_path,
                                event,
                                local_path,
                                local_hash,
                            )
                            baseline[logical_path] = self._baseline_record(
                                hashlib.sha256(merged_content).hexdigest(),
                                [event["id"]],
                            )
                            uploaded += 1
                            continue
                    conflicts[logical_path] = self._make_conflict(
                        local_hash, heads
                    )
                    continue
                if local_changed:
                    content = (
                        self._read_local_content(local_path, local_hash)
                        if local_hash is not None
                        else None
                    )
                    if content is None and local_path.exists():
                        raise StoreError(
                            f"本机文件“{logical_path}”在同步过程中发生变化，请重新同步",
                            409,
                        )
                    event = self._publish_event(
                        remote_root,
                        logical_path,
                        content,
                        head_ids,
                        state["device_id"],
                    )
                    events_by_path.setdefault(logical_path, []).append(event)
                    baseline[logical_path] = self._baseline_record(
                        local_hash, [event["id"]]
                    )
                    uploaded += 1
                    continue
                if remote_changed:
                    if head is None:
                        raise StoreError(
                            f"同步数据不完整：云端版本“{logical_path}”缺失"
                        )
                    self._apply_event(remote_root, logical_path, head, local_path, local_hash)
                    baseline[logical_path] = self._baseline_record(
                        remote_hash, head_ids
                    )
                    downloaded += 1
                    continue
                baseline[logical_path] = self._baseline_record(
                    local_hash, head_ids
                )

            state["baseline"] = baseline
            state["conflicts"] = conflicts
            state["last_sync_at"] = _now()
            self._write_state(state)
            return {
                "uploaded": uploaded,
                "downloaded": downloaded,
                "last_sync_at": state["last_sync_at"],
                "conflicts": [
                    self._public_conflict(path, conflict)
                    for path, conflict in sorted(conflicts.items())
                ],
            }

    def resolve_conflict(self, logical_path: str, choice: str) -> dict[str, Any]:
        with self._lock, self.store._lock:
            if self.sync_directory is None:
                raise StoreError("请先在设置中选择 OneDrive 同步文件夹")
            remote_root = self._prepare_remote_root()
            state = self._read_state()
            conflict = state["conflicts"].get(logical_path)
            if conflict is None:
                raise StoreError("没有找到这条待处理的同步冲突", 404)
            local_path = self._resolve_path(logical_path)
            current_hash = self._file_hash(local_path) if local_path.is_file() else None
            if current_hash != conflict["local_hash"]:
                conflict["local_hash"] = current_hash
                state["conflicts"][logical_path] = conflict
                self._write_state(state)
                raise StoreError("本机文件在冲突出现后又发生变化，请重新同步后处理", 409)

            events_by_path = self._read_events(remote_root)
            heads = self._heads(events_by_path.get(logical_path, []))
            head_ids = sorted(str(event["id"]) for event in heads)
            expected_ids = sorted(
                str(event["id"]) for event in conflict["remote_heads"]
            )
            if head_ids != expected_ids:
                conflict["remote_heads"] = heads
                state["conflicts"][logical_path] = conflict
                self._write_state(state)
                raise StoreError("云端文件在冲突出现后又发生变化，请重新同步后处理", 409)
            if not heads:
                raise StoreError("云端冲突版本已不存在，请重新同步后处理", 409)

            if choice == "local":
                content = (
                    self._read_local_content(local_path, current_hash)
                    if current_hash is not None
                    else None
                )
                if content is None and local_path.exists():
                    raise StoreError(
                        f"本机文件“{logical_path}”在冲突处理过程中发生变化，请重新同步",
                        409,
                    )
                chosen_hash = current_hash
            else:
                selected = next(
                    (event for event in heads if event["id"] == choice),
                    None,
                )
                if selected is None:
                    raise StoreError("选择的云端版本无效")
                content = self._read_blob(remote_root, selected) if selected["content_hash"] else None
                chosen_hash = selected["content_hash"]

            resolution = self._publish_event(
                remote_root,
                logical_path,
                content,
                head_ids,
                state["device_id"],
            )
            self._apply_event(
                remote_root, logical_path, resolution, local_path, current_hash
            )
            state["baseline"][logical_path] = self._baseline_record(
                chosen_hash, [resolution["id"]]
            )
            state["conflicts"].pop(logical_path)
            state["last_sync_at"] = _now()
            self._write_state(state)
            return {
                "resolved": logical_path,
                "last_sync_at": state["last_sync_at"],
                "conflicts": [
                    self._public_conflict(path, item)
                    for path, item in sorted(state["conflicts"].items())
                ],
            }

    def _scan_local_files(self) -> dict[str, Path]:
        files = self.store.sync_paths()
        highlight_path = self.highlight_manager.settings_path.resolve()
        if highlight_path.is_file():
            files["highlights/rules.json"] = highlight_path
        return files

    def _resolve_path(self, logical_path: str) -> Path:
        if logical_path == "highlights/rules.json":
            return self.highlight_manager.settings_path.resolve()
        if logical_path.startswith("notes/"):
            return self.store.sync_path(logical_path)
        if logical_path.startswith("assets/"):
            return self.store.sync_path(logical_path)
        raise StoreError("同步文件路径无效")

    @staticmethod
    def _file_hash(path: Path) -> str:
        try:
            size = path.stat().st_size
            if size > MAX_SYNC_FILE_BYTES:
                raise StoreError(f"同步文件超过 {MAX_SYNC_FILE_BYTES // 1_000_000} MB 限制")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except StoreError:
            raise
        except OSError as error:
            raise StoreError(f"无法读取本机同步文件 {path.name}: {error}", 500) from error

    @staticmethod
    def _read_local_content(path: Path, expected_hash: str) -> bytes:
        try:
            content = path.read_bytes()
        except OSError as error:
            raise StoreError(f"无法读取本机同步文件 {path.name}: {error}", 500) from error
        if len(content) > MAX_SYNC_FILE_BYTES:
            raise StoreError(
                f"同步文件超过 {MAX_SYNC_FILE_BYTES // 1_000_000} MB 限制"
            )
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise StoreError(
                f"本机文件“{path.name}”在同步过程中发生变化，请重新同步",
                409,
            )
        return content

    def _prepare_remote_root(self) -> Path:
        if self.sync_directory is None:
            raise StoreError("请先在设置中选择 OneDrive 同步文件夹")
        try:
            root = self.validate_sync_directory(self.sync_directory)
            self.sync_directory = root
            remote_root = root / SYNC_DIRECTORY_NAME
            remote_root.mkdir(parents=True, exist_ok=True)
            (remote_root / "events").mkdir(exist_ok=True)
            (remote_root / "blobs").mkdir(exist_ok=True)
            marker = remote_root / "sync-info.json"
            if marker.exists():
                data = self._read_json_file(marker, "同步目录标记")
                if (
                    not isinstance(data, dict)
                    or data.get("format") != SYNC_FORMAT
                    or data.get("version") != SYNC_VERSION
                ):
                    raise StoreError("OneDrive 同步目录格式不受支持")
            else:
                self._atomic_write_json(
                    marker,
                    {"format": SYNC_FORMAT, "version": SYNC_VERSION},
                )
            return remote_root
        except StoreError:
            raise
        except OSError as error:
            raise StoreError(f"无法访问 OneDrive 同步目录: {error}", 500) from error

    def _read_events(self, remote_root: Path) -> dict[str, list[dict[str, Any]]]:
        events: dict[str, dict[str, Any]] = {}
        event_directory = remote_root / "events"
        try:
            event_paths = sorted(event_directory.glob("*.json"))
        except OSError as error:
            raise StoreError(f"无法读取同步事件: {error}", 500) from error
        for event_path in event_paths:
            event = self._read_json_file(event_path, "同步事件")
            if not isinstance(event, dict):
                raise StoreError("同步事件格式无效")
            event_id = event.get("id")
            logical_path = event.get("path")
            parents = event.get("parents")
            content_hash = event.get("content_hash")
            if (
                event.get("format") != SYNC_FORMAT
                or event.get("version") != SYNC_VERSION
                or not isinstance(event_id, str)
                or not DEVICE_ID_PATTERN.fullmatch(event_id)
                or event_path.stem != event_id
                or not isinstance(logical_path, str)
                or not isinstance(parents, list)
                or "content_hash" not in event
                or any(
                    not isinstance(parent, str)
                    or not DEVICE_ID_PATTERN.fullmatch(parent)
                    for parent in parents
                )
                or (content_hash is not None and (
                    not isinstance(content_hash, str)
                    or not HASH_PATTERN.fullmatch(content_hash)
                ))
                or not isinstance(event.get("device_id"), str)
                or not DEVICE_ID_PATTERN.fullmatch(event["device_id"])
                or not isinstance(event.get("created_at"), str)
            ):
                raise StoreError(f"同步事件格式无效：{event_path.name}")
            self._resolve_path(logical_path)
            if event_id in events:
                raise StoreError(f"同步事件编号重复：{event_id}")
            events[event_id] = event

        by_path: dict[str, list[dict[str, Any]]] = {}
        for event in events.values():
            for parent_id in event["parents"]:
                parent = events.get(parent_id)
                if parent is None or parent["path"] != event["path"]:
                    raise StoreError(
                        f"同步历史不完整：事件 {event['id']} 引用了缺失或不匹配的父版本"
                    )
            by_path.setdefault(event["path"], []).append(event)
        if any(items and not self._heads(items) for items in by_path.values()):
            raise StoreError("同步历史包含循环引用")
        return by_path

    @staticmethod
    def _heads(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        parent_ids = {
            parent_id
            for event in events
            for parent_id in event["parents"]
        }
        return sorted(
            (event for event in events if event["id"] not in parent_ids),
            key=lambda event: (event["created_at"], event["id"]),
        )

    def _publish_event(
        self,
        remote_root: Path,
        logical_path: str,
        content: bytes | None,
        parents: list[str],
        device_id: str,
    ) -> dict[str, Any]:
        if content is not None and len(content) > MAX_SYNC_FILE_BYTES:
            raise StoreError(
                f"同步文件超过 {MAX_SYNC_FILE_BYTES // 1_000_000} MB 限制"
            )
        content_hash: str | None = None
        if content is not None:
            content_hash = hashlib.sha256(content).hexdigest()
            blob_path = self._blob_path(remote_root, content_hash)
            if blob_path.exists():
                if self._file_hash(blob_path) != content_hash:
                    raise StoreError(f"云端附件校验失败：{content_hash}")
            else:
                self._atomic_write_bytes(blob_path, content)

        event = {
            "format": SYNC_FORMAT,
            "version": SYNC_VERSION,
            "id": uuid.uuid4().hex,
            "device_id": device_id,
            "path": logical_path,
            "parents": sorted(set(parents)),
            "content_hash": content_hash,
            "created_at": _now(),
        }
        self._atomic_write_json(
            remote_root / "events" / f"{event['id']}.json",
            event,
        )
        return event

    def _apply_event(
        self,
        remote_root: Path,
        logical_path: str,
        event: dict[str, Any],
        local_path: Path,
        expected_hash: str | None,
    ) -> None:
        current_hash = self._file_hash(local_path) if local_path.is_file() else None
        if current_hash != expected_hash:
            raise StoreError(
                f"本机文件“{logical_path}”在同步过程中发生变化，请重新同步",
                409,
            )
        content_hash = event["content_hash"]
        if content_hash is None:
            try:
                local_path.unlink(missing_ok=True)
            except OSError as error:
                raise StoreError(f"无法应用云端删除“{logical_path}”: {error}", 500) from error
            if logical_path == "highlights/rules.json":
                self.highlight_manager.reload_rules()
            return
        content = self._read_blob(remote_root, event)
        if local_path.exists() and not local_path.is_file():
            raise StoreError(f"同步目标不是文件：{local_path}")
        self._atomic_write_bytes(local_path, content)
        if logical_path == "highlights/rules.json":
            self.highlight_manager.reload_rules()

    def _read_blob(self, remote_root: Path, event: dict[str, Any]) -> bytes:
        content_hash = event["content_hash"]
        if not isinstance(content_hash, str):
            raise StoreError("同步版本没有附件内容")
        path = self._blob_path(remote_root, content_hash)
        try:
            content = path.read_bytes()
        except OSError as error:
            raise StoreError(f"云端同步文件暂不可用 {content_hash}: {error}", 500) from error
        if len(content) > MAX_SYNC_FILE_BYTES or hashlib.sha256(content).hexdigest() != content_hash:
            raise StoreError(f"云端同步文件校验失败：{content_hash}")
        return content

    @staticmethod
    def _blob_path(remote_root: Path, content_hash: str) -> Path:
        return remote_root / "blobs" / content_hash[:2] / content_hash[2:]

    def _make_conflict(
        self,
        local_hash: str | None,
        heads: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "local_hash": local_hash,
            "remote_heads": heads,
            "updated_at": _now(),
        }

    @staticmethod
    def _baseline_record(
        content_hash: str | None,
        heads: list[str],
    ) -> dict[str, Any]:
        return {"content_hash": content_hash, "heads": sorted(set(heads))}

    @staticmethod
    def _merge_independent_line_changes(
        base_content: bytes,
        local_content: bytes,
        remote_content: bytes,
    ) -> bytes | None:
        if local_content == remote_content:
            return local_content
        base_lines = base_content.splitlines(keepends=True)

        def changes(content: bytes) -> list[tuple[int, int, list[bytes]]]:
            content_lines = content.splitlines(keepends=True)
            matcher = SequenceMatcher(
                None,
                base_lines,
                content_lines,
                autojunk=False,
            )
            return [
                (base_start, base_end, content_lines[new_start:new_end])
                for operation, base_start, base_end, new_start, new_end
                in matcher.get_opcodes()
                if operation != "equal"
            ]

        local_changes = changes(local_content)
        remote_changes = changes(remote_content)
        merged_changes = list(local_changes)
        for remote_change in remote_changes:
            start, end, replacement = remote_change
            duplicate = False
            for local_start, local_end, local_replacement in local_changes:
                overlaps = (
                    (start == end and local_start == local_end and start == local_start)
                    or (
                        start == end
                        and local_start <= start < local_end
                    )
                    or (
                        local_start == local_end
                        and start <= local_start < end
                    )
                    or max(start, local_start) < min(end, local_end)
                )
                if not overlaps:
                    continue
                if (
                    start == local_start
                    and end == local_end
                    and replacement == local_replacement
                ):
                    duplicate = True
                    break
                return None
            if not duplicate:
                merged_changes.append(remote_change)

        merged_changes.sort(key=lambda change: (change[0], change[1]))
        output: list[bytes] = []
        cursor = 0
        for start, end, replacement in merged_changes:
            if start < cursor:
                return None
            output.extend(base_lines[cursor:start])
            output.extend(replacement)
            cursor = end
        output.extend(base_lines[cursor:])
        return b"".join(output)

    @staticmethod
    def _public_conflict(
        logical_path: str,
        conflict: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "path": logical_path,
            "local_hash": conflict["local_hash"],
            "updated_at": conflict["updated_at"],
            "remote_versions": [
                {
                    "id": event["id"],
                    "device_id": event["device_id"],
                    "content_hash": event["content_hash"],
                    "created_at": event["created_at"],
                }
                for event in conflict["remote_heads"]
            ],
        }

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "format": SYNC_FORMAT,
                "version": SYNC_VERSION,
                "device_id": uuid.uuid4().hex,
                "last_sync_at": "",
                "baseline": {},
                "conflicts": {},
            }
        data = self._read_json_file(self.state_path, "本机同步状态")
        if (
            not isinstance(data, dict)
            or data.get("format") != SYNC_FORMAT
            or data.get("version") != SYNC_VERSION
            or not isinstance(data.get("device_id"), str)
            or not DEVICE_ID_PATTERN.fullmatch(data["device_id"])
            or not isinstance(data.get("last_sync_at"), str)
            or not isinstance(data.get("baseline"), dict)
            or not isinstance(data.get("conflicts"), dict)
        ):
            raise StoreError("本机同步状态格式无效或版本不受支持")
        for path, record in data["baseline"].items():
            if (
                not isinstance(path, str)
                or not isinstance(record, dict)
                or (
                    record.get("content_hash") is not None
                    and (
                        not isinstance(record["content_hash"], str)
                        or not HASH_PATTERN.fullmatch(record["content_hash"])
                    )
                )
                or not isinstance(record.get("heads"), list)
                or any(
                    not isinstance(item, str)
                    or not DEVICE_ID_PATTERN.fullmatch(item)
                    for item in record["heads"]
                )
            ):
                raise StoreError("本机同步基线格式无效")
            self._resolve_path(path)
        for path, conflict in data["conflicts"].items():
            if (
                not isinstance(path, str)
                or not isinstance(conflict, dict)
                or (
                    conflict.get("local_hash") is not None
                    and (
                        not isinstance(conflict["local_hash"], str)
                        or not HASH_PATTERN.fullmatch(conflict["local_hash"])
                    )
                )
                or not isinstance(conflict.get("remote_heads"), list)
                or not conflict["remote_heads"]
                or not isinstance(conflict.get("updated_at"), str)
            ):
                raise StoreError("本机同步冲突记录格式无效")
            self._resolve_path(path)
            for event in conflict["remote_heads"]:
                if (
                    not isinstance(event, dict)
                    or event.get("path") != path
                    or not isinstance(event.get("id"), str)
                    or not DEVICE_ID_PATTERN.fullmatch(event["id"])
                    or not isinstance(event.get("device_id"), str)
                    or not DEVICE_ID_PATTERN.fullmatch(event["device_id"])
                    or (
                        event.get("content_hash") is not None
                        and (
                            not isinstance(event["content_hash"], str)
                            or not HASH_PATTERN.fullmatch(event["content_hash"])
                        )
                    )
                    or not isinstance(event.get("created_at"), str)
                ):
                    raise StoreError("本机同步冲突版本格式无效")
        return data

    def _write_state(self, state: dict[str, Any]) -> None:
        self._atomic_write_json(self.state_path, state)

    @staticmethod
    def _read_json_file(path: Path, description: str) -> Any:
        try:
            if path.stat().st_size > 10_000_000:
                raise StoreError(f"{description}文件过大")
            return json.loads(path.read_text(encoding="utf-8"))
        except StoreError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StoreError(f"无法读取{description}：{error}", 500) from error

    @classmethod
    def _atomic_write_json(cls, path: Path, value: object) -> None:
        cls._atomic_write_bytes(
            path,
            (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )

    @staticmethod
    def _atomic_write_bytes(path: Path, content: bytes) -> None:
        temporary_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(path)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise StoreError(f"无法写入同步文件 {path.name}: {error}", 500) from error
