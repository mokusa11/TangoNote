from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path

from note_store import NoteStore, StoreError, normalize


HIGHLIGHT_GROUP = "J5词典"
MAX_CATEGORY_LENGTH = 40
MAX_PAGE_SIZE = 100
MAX_EXPLICIT_BATCH_SIZE = 5000


class HighlightManager:
    def __init__(self, store: NoteStore, settings_path: Path) -> None:
        self.store = store
        self.settings_path = Path(settings_path)
        self._lock = threading.RLock()
        self._rules = self._load_rules()

    def reload_rules(self) -> None:
        with self._lock:
            self._rules = self._load_rules()

    def copy_to(self, settings_path: Path) -> HighlightManager:
        destination = Path(settings_path).resolve()
        with self._lock:
            if destination == self.settings_path.resolve():
                return self
            if destination.exists():
                return HighlightManager(self.store, destination)

            manager = HighlightManager(self.store, destination)
            manager._rules = {
                key: {
                    "included": bool(rule["included"]),
                    "categories": list(rule["categories"]),
                }
                for key, rule in self._rules.items()
            }
            manager._write_rules(manager._rules)
            return manager

    def _load_rules(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"无法读取高光设置：{error}") from error
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("高光设置格式无效或版本不受支持")
        configured_rules = data.get("rules")
        if not isinstance(configured_rules, dict):
            raise ValueError("高光设置格式无效")
        rules: dict[str, dict[str, object]] = {}
        for key, rule in configured_rules.items():
            if (
                not isinstance(key, str)
                or not key
                or key != normalize(key)
                or not isinstance(rule, dict)
            ):
                raise ValueError("高光词头规则格式无效")
            included = rule.get("included", True)
            categories = rule.get("categories", [])
            if not isinstance(included, bool) or not isinstance(categories, list):
                raise ValueError("高光词头规则格式无效")
            clean_categories: list[str] = []
            seen_categories: set[str] = set()
            try:
                for category in categories:
                    clean_category = self._validate_category(category)
                    category_key = normalize(clean_category)
                    if category_key not in seen_categories:
                        clean_categories.append(clean_category)
                        seen_categories.add(category_key)
            except StoreError as error:
                raise ValueError("高光分类设置格式无效") from error
            rules[key] = {
                "included": included,
                "categories": clean_categories,
            }
        return rules

    @staticmethod
    def _validate_category(value: object) -> str:
        if not isinstance(value, str):
            raise StoreError("分类名称必须是文本")
        category = value.strip()
        if not category or len(category) > MAX_CATEGORY_LENGTH:
            raise StoreError(f"分类名称不能为空且不能超过 {MAX_CATEGORY_LENGTH} 个字符")
        if any(character in category for character in "\r\n\x00"):
            raise StoreError("分类名称不能包含换行或控制字符")
        return category

    def _records(self) -> list[dict[str, object]]:
        group = next(
            (
                item
                for item in self.store.list_groups()
                if item["name"] == HIGHLIGHT_GROUP
            ),
            None,
        )
        if group is None:
            raise StoreError(f"没有找到“{HIGHLIGHT_GROUP}”分组", 404)

        notes = self.store.list_notes(group_id=str(group["id"]))
        records: dict[str, dict[str, object]] = {}
        for note in notes:
            term = str(note["term"]).strip()
            key = normalize(term)
            if key in records:
                continue
            rule = self._rules.get(key, {})
            categories = rule.get("categories", [])
            records[key] = {
                "term": term,
                "included": rule.get("included", True),
                "categories": list(categories) if isinstance(categories, list) else [],
            }
        return list(records.values())

    @staticmethod
    def _validate_filters(
        query: object,
        status: object,
        category: object,
    ) -> tuple[str, str, str]:
        if not isinstance(query, str) or len(query) > 200:
            raise StoreError("词头搜索内容无效")
        if not isinstance(status, str) or status not in {
            "all",
            "included",
            "excluded",
        }:
            raise StoreError("发送状态筛选无效")
        if not isinstance(category, str):
            raise StoreError("分类筛选无效")
        return query.strip(), str(status), category.strip()

    @staticmethod
    def _filter_records(
        records: list[dict[str, object]],
        query: str,
        status: str,
        category: str,
    ) -> list[dict[str, object]]:
        needle = normalize(query)
        category_key = normalize(category)
        return [
            record
            for record in records
            if (not needle or needle in normalize(str(record["term"])))
            and (
                status == "all"
                or bool(record["included"]) == (status == "included")
            )
            and (
                not category_key
                or any(
                    normalize(str(item)) == category_key
                    for item in record["categories"]
                )
            )
        ]

    def list_highlights(
        self,
        query: object = "",
        status: object = "all",
        category: object = "",
        page: object = 1,
        page_size: object = 50,
    ) -> dict[str, object]:
        query_value, status_value, category_value = self._validate_filters(
            query, status, category
        )
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or page < 1
            or isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= MAX_PAGE_SIZE
        ):
            raise StoreError("分页参数无效")
        with self._lock:
            records = self._records()
            filtered = self._filter_records(
                records, query_value, status_value, category_value
            )
            category_names: dict[str, str] = {}
            for record in records:
                for item in record["categories"]:
                    category_names.setdefault(normalize(str(item)), str(item))
            categories = sorted(category_names.values(), key=normalize)
            included_count = sum(bool(record["included"]) for record in records)
            start = (page - 1) * page_size
            return {
                "items": filtered[start : start + page_size],
                "page": page,
                "page_size": page_size,
                "total": len(filtered),
                "pages": max(1, (len(filtered) + page_size - 1) // page_size),
                "included_count": included_count,
                "excluded_count": len(records) - included_count,
                "categories": categories,
            }

    def update_highlights(self, data: object) -> dict[str, int]:
        if not isinstance(data, dict):
            raise StoreError("高光批量设置格式无效")
        included = data.get("included")
        if included is not None and not isinstance(included, bool):
            raise StoreError("发送状态设置无效")

        category_action = data.get("category_action")
        category_name: str | None = None
        if category_action is not None:
            if not isinstance(category_action, str):
                raise StoreError("分类操作无效")
            if category_action not in {"add", "remove"}:
                raise StoreError("分类操作无效")
            category_name = self._validate_category(data.get("category_name"))
        if included is None and category_action is None:
            raise StoreError("请选择要执行的批量操作")

        all_matching = data.get("all_matching", False)
        if not isinstance(all_matching, bool):
            raise StoreError("批量选择范围无效")
        if all_matching:
            query, status, category = self._validate_filters(
                data.get("query", ""),
                data.get("status", "all"),
                data.get("category", ""),
            )
            requested_terms = None
        else:
            requested = data.get("terms")
            if (
                not isinstance(requested, list)
                or not requested
                or len(requested) > MAX_EXPLICIT_BATCH_SIZE
                or any(
                    not isinstance(term, str)
                    or not term.strip()
                    or len(term) > 200
                    for term in requested
                )
            ):
                raise StoreError("请选择有效的词头，单次最多操作 5000 条")
            requested_terms = {normalize(term) for term in requested}

        with self._lock:
            records = self._records()
            if category_name is not None:
                category_name = next(
                    (
                        str(item)
                        for record in records
                        for item in record["categories"]
                        if normalize(str(item)) == normalize(category_name)
                    ),
                    category_name,
                )
            if all_matching:
                selected = self._filter_records(records, query, status, category)
            else:
                by_key = {normalize(str(record["term"])): record for record in records}
                missing = requested_terms - by_key.keys()
                if missing:
                    raise StoreError("部分词头已不在 J5 词典中，请刷新后重试", 409)
                selected = [by_key[key] for key in requested_terms]
            if not selected:
                raise StoreError("当前选择没有匹配的词头")

            updated_rules = {
                key: {
                    "included": bool(rule.get("included", True)),
                    "categories": list(rule.get("categories", [])),
                }
                for key, rule in self._rules.items()
            }
            changed = 0
            for record in selected:
                key = normalize(str(record["term"]))
                rule = updated_rules.get(
                    key,
                    {"included": True, "categories": []},
                )
                previous = (
                    bool(rule["included"]),
                    list(rule["categories"]),
                )
                if included is not None:
                    rule["included"] = included
                if category_name is not None:
                    categories = list(rule["categories"])
                    existing = next(
                        (
                            item
                            for item in categories
                            if normalize(str(item)) == normalize(category_name)
                        ),
                        None,
                    )
                    if category_action == "add" and existing is None:
                        categories.append(category_name)
                    elif category_action == "remove" and existing is not None:
                        categories.remove(existing)
                    rule["categories"] = categories
                if previous != (bool(rule["included"]), list(rule["categories"])):
                    changed += 1
                if rule["included"] is True and not rule["categories"]:
                    updated_rules.pop(key, None)
                else:
                    updated_rules[key] = rule

            if changed:
                self._write_rules(updated_rules)
                self._rules = updated_rules
            return {"selected": len(selected), "changed": changed}

    def dictionary_terms(self) -> tuple[list[str], int]:
        with self._lock:
            records = self._records()
            return [
                str(record["term"])
                for record in records
                if record["included"]
            ], len(records)

    def _write_rules(self, rules: dict[str, dict[str, object]]) -> None:
        temporary_path: Path | None = None
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.settings_path.parent,
                prefix=f".{self.settings_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(
                    json.dumps(
                        {"version": 1, "rules": rules},
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n"
                )
                stream.flush()
            temporary_path.replace(self.settings_path)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise StoreError(f"无法保存高光设置：{error}", 500) from error
