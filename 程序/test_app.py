from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import sqlite3
import socket
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from highlight_manager import HighlightManager
from build_release import build_release_archive
from rclone_sync import sync_through_rclone, validate_rclone_remote
from update_manager import (
    RELEASE_FORMAT,
    UpdateError,
    _extract_validated_archive,
    check_for_updates,
    install_prepared_update,
    prepare_termux_update,
    rollback_prepared_update,
)
import update_termux
from app import (
    DEFAULT_GROUP,
    LocalNoteServer,
    NoteHandler,
    StoreError,
    load_group_resource_directories,
    load_group_note_paths,
    load_highlight_rules_path,
    load_notes_directory_setting,
    load_sync_directory_setting,
    request_paste_image,
)
from note_store import DEFAULT_NOTE_FILE, NoteStore


WEB_DIR = Path(__file__).resolve().parent.parent / "网页"


class LocalNoteServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.notes_dir = self.root / "note"
        self.settings_path = self.root / "settings.json"
        self.server = LocalNoteServer(
            ("127.0.0.1", 0),
            self.notes_dir,
            WEB_DIR,
            settings_path=self.settings_path,
            sync_state_path=self.root / "sync-state.json",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp_dir.cleanup()

    def test_disconnect_while_sending_error_response_does_not_escape_handler(self) -> None:
        for error_type in (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
        ):
            with self.subTest(error=error_type.__name__):
                handler = object.__new__(NoteHandler)
                handler.server = SimpleNamespace(test_mode=False)
                handler.command = "POST"
                handler.close_connection = False
                handler.send_response = Mock()
                handler.send_header = Mock()
                handler.end_headers = Mock()
                handler.wfile = Mock()
                handler.wfile.write.side_effect = error_type()

                handler._send(409, b"duplicate", "text/html; charset=utf-8")

                self.assertTrue(handler.close_connection)

    def request_json(
        self, path: str, method: str = "GET", data: object | None = None
    ) -> tuple[int, object]:
        payload = None if data is None else json.dumps(data).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=payload,
            method=method,
            headers={"Content-Type": "application/json"} if payload else {},
        )
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def request_form(self, path: str, data: dict[str, str]) -> str:
        request = Request(
            self.base_url + path,
            data=urlencode(data).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urlopen(request, timeout=3) as response:
            return response.url

    def test_reads_and_updates_golden_dict_mynote_files_in_place(self) -> None:
        source = (
            "MyNote header\n\n"
            "apple\n<font size=5>apple</font>\n"
            '<p>fruit <strong>is</strong> good</p>\n</>\n\nfooter\n'
        )
        (self.notes_dir / DEFAULT_NOTE_FILE).write_text(source, encoding="utf-8")
        notes = self.server.store.lookup("APPLE")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["body"], '<p>fruit <strong>is</strong> good</p>')

        updated = self.server.store.update_note(
            str(notes[0]["id"]),
            {"term": "apple", "group": DEFAULT_GROUP, "body": "<p>changed</p>"},
        )
        saved = (self.notes_dir / DEFAULT_NOTE_FILE).read_text(encoding="utf-8")
        self.assertIn("MyNote header", saved)
        self.assertIn("footer", saved)
        self.assertIn("apple\n<font size=5>apple</font>\n<p>changed</p>\n</>", saved)
        self.assertEqual(updated["body"], "<p>changed</p>")
        self.assertFalse((self.notes_dir / "notes.sqlite3").exists())

    def test_create_search_move_and_delete_use_mynote_group_files(self) -> None:
        note = self.server.store.create_note(
            {"term": "  apple ", "group": "水果", "body": "果实"}
        )
        self.assertEqual(note["term"], "apple")
        self.assertEqual(note["group"], "水果")
        path = self.notes_dir / "MyNote-水果.txt"
        self.assertIn("<font size=5>apple</font>", path.read_text(encoding="utf-8"))
        self.assertEqual(
            [item["id"] for item in self.server.store.list_notes("果实")],
            [note["id"]],
        )

        moved = self.server.store.update_note(
            str(note["id"]),
            {"term": "apple", "group": DEFAULT_GROUP, "body": "new body"},
        )
        self.assertEqual(moved["group"], DEFAULT_GROUP)
        self.assertEqual(self.server.store.lookup("apple")[0]["body"], "new body")
        self.assertEqual(path.read_text(encoding="utf-8"), "")
        self.assertTrue(self.server.store.delete_note(str(moved["id"])))
        self.assertEqual(self.server.store.lookup("apple"), [])

    def test_default_group_uses_mynote_filename_and_resource_directory(self) -> None:
        paths = self.server.store.group_paths()[DEFAULT_GROUP]

        self.assertEqual(
            paths["note_path"],
            str((self.notes_dir / "MyNote-[Default].txt").resolve()),
        )
        self.assertEqual(
            paths["default_resource_directory"],
            str((self.notes_dir / "MyNote-[Default]").resolve()),
        )
        self.server.store.save_asset(DEFAULT_GROUP, "asset.png", b"asset")
        self.assertTrue((self.notes_dir / "MyNote-[Default]" / "asset.png").is_file())
        self.assertFalse((self.notes_dir / "Default").exists())

    def test_legacy_default_group_paths_are_not_supported_or_renamed(self) -> None:
        legacy_note = self.notes_dir / "GoldenDict-MyNote.txt"
        legacy_note.write_text(
            "MyNote header\n\nlegacy\n<font size=5>legacy</font>\nbody\n</>\n",
            encoding="utf-8",
        )
        legacy_resources = self.notes_dir / "Default"
        legacy_resources.mkdir()
        (legacy_resources / "existing.png").write_bytes(b"existing")

        self.assertEqual(self.server.store.lookup("legacy"), [])
        self.assertTrue(legacy_note.is_file())
        self.assertTrue((legacy_resources / "existing.png").is_file())
        self.assertFalse((self.notes_dir / "MyNote-[Default].txt").exists())
        self.assertFalse((self.notes_dir / "MyNote-[Default]").exists())

    def test_legacy_group_names_are_not_supported_or_renamed(self) -> None:
        legacy_note = self.notes_dir / "GoldenDict-MyNote-旧组.txt"
        legacy_note.write_text(
            "MyNote header\n\nlegacy\n<font size=5>legacy</font>\nbody\n</>\n",
            encoding="utf-8",
        )
        legacy_resources = self.notes_dir / "旧组"
        legacy_resources.mkdir()
        (legacy_resources / "existing.png").write_bytes(b"existing")

        self.assertEqual(self.server.store.lookup("legacy"), [])
        self.assertNotIn(
            "旧组",
            [group["name"] for group in self.server.store.list_groups()],
        )
        self.assertTrue(legacy_note.is_file())
        self.assertTrue((legacy_resources / "existing.png").is_file())
        self.assertFalse((self.notes_dir / "MyNote-旧组.txt").exists())
        self.assertFalse((self.notes_dir / "MyNote-旧组").exists())

    def test_case_insensitive_duplicate_guard(self) -> None:
        self.server.store.create_note(
            {"term": "Apple", "group": DEFAULT_GROUP, "body": "one"}
        )
        with self.assertRaises(StoreError) as duplicate:
            self.server.store.create_note(
                {"term": "APPLE", "group": DEFAULT_GROUP, "body": "two"}
            )
        self.assertEqual(duplicate.exception.status, 409)

    def test_group_names_are_case_insensitive(self) -> None:
        group = self.server.store.create_group("Study")
        note = self.server.store.create_note(
            {"term": "word", "group": "study", "body": "same group"}
        )
        self.assertEqual(note["group"], "Study")
        with self.assertRaises(StoreError):
            self.server.store.create_group("STUDY")
        self.assertEqual(
            [item["name"] for item in self.server.store.list_groups()].count("Study"),
            1,
        )

    def test_lookup_displays_notes_with_editor_links(self) -> None:
        self.server.store.create_note(
            {"term": "referenced", "group": DEFAULT_GROUP, "body": "Reference body"}
        )
        self.server.store.create_note(
            {"term": "linked word", "group": DEFAULT_GROUP, "body": "Linked note body"}
        )
        safe_note = self.server.store.create_note(
            {
                "term": "safe word",
                "group": DEFAULT_GROUP,
                "body": (
                    '<p onclick="alert(1)">Safe <strong>bold</strong></p>'
                    "<script>alert(2)</script> @@@LINK=referenced "
                    "[http://127.0.0.1:8766/lookup?word=linked%20word&group=J5%E8%AF%8D%E5%85%B8]"
                ),
            }
        )
        with urlopen(
            self.base_url + "/lookup?" + urlencode({"word": "safe word"}),
            timeout=3,
        ) as response:
            page = response.read().decode("utf-8")
        self.assertIn("<p>Safe <strong>bold</strong></p>", page)
        self.assertNotIn("onclick", page)
        self.assertNotIn("alert(2)", page)
        self.assertIn("Reference body", page)
        self.assertIn(
            '<a class="note-link" href="/lookup?word=linked+word&amp;group=J5%E8%AF%8D%E5%85%B8">linked word</a>',
            page,
        )
        editor_query = urlencode({"note_id": str(safe_note["id"])})
        self.assertIn(
            f'<a class="edit-link" href="/editor?{editor_query}">编辑</a>',
            page,
        )
        self.assertIn(
            f'<button class="delete-link" type="button" data-delete-note '
            f'data-note-id="{safe_note["id"]}" data-note-term="safe word">删除</button>',
            page,
        )
        self.assertLess(page.index('class="delete-link"'), page.index('class="edit-link"'))
        self.assertIn('<dialog class="lookup-delete-dialog"', page)
        self.assertIn('action="/delete-note"', page)
        self.assertIn('.showModal();', page)
        self.assertIn('.close();', page)
        with urlopen(self.base_url + "/editor?" + editor_query, timeout=3) as response:
            editor_page = response.read().decode("utf-8")
        self.assertIn('value="safe word"', editor_page)

        delete_request = Request(
            self.base_url + "/delete-note",
            data=urlencode(
                {
                    "note_id": str(safe_note["id"]),
                    "return_term": "safe word",
                    "return_group": "",
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urlopen(delete_request, timeout=3) as response:
            deleted_page = response.read().decode("utf-8")
            self.assertIn("没有找到", deleted_page)
            self.assertIn("word=safe+word", response.url)
        self.assertEqual(self.server.store.lookup("safe word"), [])

        with urlopen(
            self.base_url
            + "/lookup?"
            + urlencode({"word": "linked word", "group": "J5词典"}),
            timeout=3,
        ) as response:
            linked_page = response.read().decode("utf-8")
        self.assertIn("没有找到", linked_page)

        self.server.store.create_note(
            {"term": "linked word", "group": "J5词典", "body": "J5 linked note body"}
        )
        with urlopen(
            self.base_url
            + "/lookup?"
            + urlencode({"word": "linked word", "group": "J5词典"}),
            timeout=3,
        ) as response:
            linked_page = response.read().decode("utf-8")
        self.assertIn("J5 linked note body", linked_page)
        self.assertNotIn("Linked note body", linked_page)

        with urlopen(
            self.base_url + "/lookup?" + urlencode({"word": "missing", "edit": "true"}),
            timeout=3,
        ) as response:
            page = response.read().decode("utf-8")
        self.assertIn("没有找到", page)
        self.assertNotIn("contenteditable", page)

        request = Request(
            self.base_url + "/edit",
            data=urlencode({"term": "not allowed"}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with self.assertRaises(HTTPError) as not_found:
            urlopen(request, timeout=3)
        self.assertEqual(not_found.exception.code, 404)

    def test_lookup_only_accepts_standard_route_and_filters_group(self) -> None:
        self.server.store.create_note(
            {"term": "same word", "group": DEFAULT_GROUP, "body": "Default body"}
        )
        self.server.store.create_note(
            {"term": "same word", "group": "J5词典", "body": "J5 body"}
        )

        with urlopen(
            self.base_url + "/lookup?" + urlencode({"word": "same word"}),
            timeout=3,
        ) as response:
            all_groups_page = response.read().decode("utf-8")
        self.assertIn("Default body", all_groups_page)
        self.assertIn("J5 body", all_groups_page)

        with urlopen(
            self.base_url
            + "/lookup?"
            + urlencode({"word": "same word", "group": "J5词典"}),
            timeout=3,
        ) as response:
            selected_group_page = response.read().decode("utf-8")
        self.assertIn("J5 body", selected_group_page)
        self.assertNotIn("Default body", selected_group_page)

        for legacy_path in ("/search/same%20word", "/serach/same%20word", "/lookup/same%20word"):
            with self.subTest(path=legacy_path):
                with self.assertRaises(HTTPError) as not_found:
                    urlopen(self.base_url + legacy_path, timeout=3)
                self.assertEqual(not_found.exception.code, 404)

    def test_lookup_loads_group_resources_and_opt_in_scripts(self) -> None:
        self.server.store.create_note(
            {"term": "math", "group": DEFAULT_GROUP, "body": "<p>$x^2$</p>"}
        )
        resource_dir = self.notes_dir / "MyNote-[Default]"
        resource_dir.mkdir()
        (resource_dir / "notes.css").write_text(".content { color: red; }", encoding="utf-8")
        (resource_dir / "LocalFont.woff2").write_bytes(b"font-data")
        (resource_dir / "mathjax.js").write_text("window.mathjaxLoaded = true;", encoding="utf-8")

        with urlopen(self.base_url + "/lookup?word=math", timeout=3) as response:
            page = response.read().decode("utf-8")
        self.assertIn('href="/assets/Default/notes.css"', page)
        self.assertIn('font-family:"LocalFont"', page)
        self.assertIn('src:url("/assets/Default/LocalFont.woff2")', page)
        self.assertIn("<p>$x^2$</p>", page)
        self.assertNotIn('src="/assets/Default/mathjax.js"', page)
        nonce = page.split("nonce=\"", 1)[1].split('"', 1)[0]
        self.assertIn(f"script-src 'self' 'nonce-{nonce}';", page)

        with self.assertRaises(HTTPError) as disabled_script:
            urlopen(self.base_url + "/assets/Default/mathjax.js", timeout=3)
        self.assertEqual(disabled_script.exception.code, 403)

        self.server.allow_note_scripts = True
        with urlopen(self.base_url + "/lookup?word=math", timeout=3) as response:
            page = response.read().decode("utf-8")
        self.assertIn('window.MathJax={tex:{', page)
        self.assertIn('inlineMath:[["$","$"],["\\\\(","\\\\)"]]', page)
        self.assertIn('src="/assets/Default/mathjax.js"', page)
        nonce = page.split("nonce=\"", 1)[1].split('"', 1)[0]
        self.assertIn(f"script-src 'self' 'nonce-{nonce}';", page)
        with urlopen(self.base_url + "/assets/Default/mathjax.js", timeout=3) as response:
            self.assertEqual(response.headers["Content-Type"], "text/javascript; charset=utf-8")
            self.assertEqual(response.read(), b"window.mathjaxLoaded = true;")
        with urlopen(self.base_url + "/assets/Default/notes.css", timeout=3) as response:
            self.assertEqual(response.headers["Content-Type"], "text/css; charset=utf-8")
        with urlopen(self.base_url + "/assets/Default/LocalFont.woff2", timeout=3) as response:
            self.assertEqual(response.headers["Content-Type"], "font/woff2")

    def test_j5_dictionary_endpoint_returns_headwords_to_loopback_origins_only(self) -> None:
        self.server.store.create_group("J5词典")
        self.server.store.create_note(
            {
                "term": "铁路词头",
                "group": "J5词典",
                "body": "私有正文不应随词头接口返回",
            }
        )
        self.server.store.create_note(
            {
                "term": "另一个词头",
                "group": "J5词典",
                "body": "second note",
            }
        )

        origin = "http://127.0.0.1:35555"
        request = Request(
            self.base_url + "/api/dictionary/j5",
            headers={"Origin": origin},
        )
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], origin)
            self.assertEqual(response.headers["Vary"], "Origin")
        self.assertEqual(payload, {"terms": ["另一个词头", "铁路词头"]})

        with self.assertRaises(HTTPError) as rejected:
            urlopen(
                Request(
                    self.base_url + "/api/dictionary/j5",
                    headers={"Origin": "https://example.com"},
                ),
                timeout=3,
            )
        self.assertEqual(rejected.exception.code, 403)
        self.assertNotIn("Access-Control-Allow-Origin", rejected.exception.headers)

    def test_highlight_management_filters_batches_and_controls_outbound_terms(self) -> None:
        self.server.store.create_group("J5词典")
        for term in ("alpha", "beta", "gamma", "delta"):
            self.server.store.create_note(
                {"term": term, "group": "J5词典", "body": "private body"}
            )

        with urlopen(self.base_url + "/highlights", timeout=3) as response:
            page = response.read().decode("utf-8")
            self.assertIn("Content-Security-Policy", response.headers)
        self.assertIn("高光管理", page)
        self.assertIn('id="select-all-matching"', page)
        self.assertIn('id="selection-rectangle"', page)
        self.assertIn("Shift 点击同页另一个复选框", page)
        with urlopen(self.base_url + "/", timeout=3) as response:
            homepage = response.read().decode("utf-8")
        self.assertIn('href="/highlights">高光管理</a>', homepage)

        status, first_page = self.request_json(
            "/api/highlights?page=1&page_size=2"
        )
        self.assertEqual(status, 200)
        self.assertEqual(first_page["total"], 4)
        self.assertEqual(first_page["included_count"], 4)
        self.assertEqual(len(first_page["items"]), 2)

        _, excluded = self.request_json(
            "/api/highlights",
            "PUT",
            {
                "terms": ["beta", "gamma"],
                "included": False,
                "category_name": "基础",
                "category_action": "add",
            },
        )
        self.assertEqual(excluded, {"selected": 2, "changed": 2})

        _, categorized = self.request_json(
            "/api/highlights",
            "PUT",
            {
                "all_matching": True,
                "query": "a",
                "status": "included",
                "category": "",
                "category_name": "字母 A",
                "category_action": "add",
            },
        )
        self.assertEqual(categorized, {"selected": 2, "changed": 2})
        _, category_results = self.request_json(
            "/api/highlights?category=%E5%AD%97%E6%AF%8D+A"
        )
        self.assertEqual(
            {item["term"] for item in category_results["items"]},
            {"alpha", "delta"},
        )
        self.assertEqual(category_results["total"], 2)

        request = Request(
            self.base_url + "/api/dictionary/j5",
            headers={"Origin": "http://127.0.0.1:35555"},
        )
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload, {"terms": ["alpha", "delta"]})

        restored = HighlightManager(
            self.server.store,
            self.settings_path.with_name("highlight-rules.json"),
        )
        restored_results = restored.list_highlights(category="基础")
        self.assertEqual(restored_results["total"], 2)
        self.assertEqual(restored_results["excluded_count"], 2)

        _, removed_category = self.request_json(
            "/api/highlights",
            "PUT",
            {
                "terms": ["beta", "gamma"],
                "category_name": "基础",
                "category_action": "remove",
            },
        )
        self.assertEqual(removed_category, {"selected": 2, "changed": 2})

        _, excluded_all = self.request_json(
            "/api/highlights",
            "PUT",
            {"all_matching": True, "included": False},
        )
        self.assertEqual(excluded_all, {"selected": 4, "changed": 2})
        empty_request = Request(
            self.base_url + "/api/dictionary/j5",
            headers={"Origin": "http://127.0.0.1:35555"},
        )
        with urlopen(empty_request, timeout=3) as response:
            self.assertEqual(json.loads(response.read().decode("utf-8")), {"terms": []})

    def test_highlight_rules_path_can_be_changed_without_losing_existing_rules(self) -> None:
        self.server.store.create_group("J5词典")
        self.server.store.create_note(
            {"term": "铁路词头", "group": "J5词典", "body": "private body"}
        )
        self.request_json(
            "/api/highlights",
            "PUT",
            {"terms": ["铁路词头"], "included": False},
        )

        default_path = self.settings_path.with_name("highlight-rules.json")
        self.assertEqual(load_highlight_rules_path(self.settings_path), default_path.resolve())
        _, current_settings = self.request_json("/api/settings/highlight-rules")
        self.assertEqual(current_settings["highlight_rules_path"], str(default_path.resolve()))

        custom_path = self.root / "custom config" / "highlights.json"
        _, updated_settings = self.request_json(
            "/api/settings/highlight-rules",
            "PUT",
            {"highlight_rules_path": str(custom_path)},
        )
        self.assertEqual(updated_settings["highlight_rules_path"], str(custom_path.resolve()))
        self.assertTrue(custom_path.is_file())
        self.assertEqual(
            json.loads(custom_path.read_text(encoding="utf-8"))["rules"]["铁路词头"][
                "included"
            ],
            False,
        )
        self.assertTrue(default_path.is_file())
        self.assertEqual(
            json.loads(self.settings_path.read_text(encoding="utf-8"))[
                "highlight_rules_path"
            ],
            str(custom_path.resolve()),
        )
        self.request_json(
            "/api/settings/notes-directory",
            "PUT",
            {"notes_directory": str(self.notes_dir)},
        )
        self.assertEqual(
            load_highlight_rules_path(self.settings_path),
            custom_path.resolve(),
        )

        existing_path = self.root / "existing-highlights.json"
        existing_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "rules": {
                        "铁路词头": {
                            "included": True,
                            "categories": ["来自目标文件"],
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.request_json(
            "/api/settings/highlight-rules",
            "PUT",
            {"highlight_rules_path": str(existing_path)},
        )
        self.assertEqual(
            self.server.highlight_manager.list_highlights()["items"][0]["categories"],
            ["来自目标文件"],
        )

        with self.assertRaises(HTTPError) as invalid_extension:
            self.request_json(
                "/api/settings/highlight-rules",
                "PUT",
                {"highlight_rules_path": str(self.root / "not-json.txt")},
            )
        self.assertEqual(invalid_extension.exception.code, 400)
        self.assertEqual(
            self.server.highlight_manager.settings_path.resolve(),
            existing_path.resolve(),
        )

        corrupt_path = self.root / "corrupt-highlights.json"
        corrupt_path.write_text("{invalid json", encoding="utf-8")
        with self.assertRaises(HTTPError) as invalid_file:
            self.request_json(
                "/api/settings/highlight-rules",
                "PUT",
                {"highlight_rules_path": str(corrupt_path)},
            )
        self.assertEqual(invalid_file.exception.code, 400)
        self.assertEqual(
            self.server.highlight_manager.settings_path.resolve(),
            existing_path.resolve(),
        )

    def test_sync_stops_if_notes_root_disappears_without_publishing_deletions(self) -> None:
        sync_directory = self.root / "OneDrive"
        sync_directory.mkdir()
        self.server.sync_manager.sync_directory = (
            self.server.sync_manager.set_sync_directory(str(sync_directory))
        )
        self.server.sync_manager.sync()
        remote_root = sync_directory / ".dict-tango-sync"
        events_before = self.server.sync_manager._read_events(remote_root)
        event_ids_before = {
            str(event["id"])
            for events in events_before.values()
            for event in events
        }

        self.notes_dir.rename(self.root / "offline-notes")
        with self.assertRaises(StoreError):
            self.server.sync_manager.sync()

        events_after = self.server.sync_manager._read_events(remote_root)
        event_ids_after = {
            str(event["id"])
            for events in events_after.values()
            for event in events
        }
        self.assertEqual(event_ids_after, event_ids_before)

    def test_onedrive_sync_transfers_notes_assets_rules_and_preserves_conflicts(self) -> None:
        sync_directory = self.root / "OneDrive" / "DictTango"
        sync_directory.mkdir(parents=True)
        status, initial_status = self.request_json("/api/sync/status")
        self.assertEqual(status, 200)
        self.assertFalse(initial_status["configured"])

        self.server.store.create_group("J5词典")
        note = self.server.store.create_note(
            {
                "term": "sync term",
                "group": "J5词典",
                "body": "original note",
            }
        )
        other_note = self.server.store.create_note(
            {
                "term": "other term",
                "group": "J5词典",
                "body": "other original",
            }
        )
        asset_content = b"\x89PNG\r\n\x1a\nsync-asset"
        asset_name = self.server.store.save_asset(
            "J5词典", "sync.png", asset_content
        )
        self.request_json(
            "/api/highlights",
            "PUT",
            {
                "terms": ["sync term"],
                "included": False,
                "category_name": "跨设备",
                "category_action": "add",
            },
        )
        _, configured = self.request_json(
            "/api/settings/sync-directory",
            "PUT",
            {"sync_directory": str(sync_directory)},
        )
        self.assertEqual(configured["sync_directory"], str(sync_directory.resolve()))
        self.assertEqual(
            load_sync_directory_setting(self.settings_path),
            sync_directory.resolve(),
        )
        self.request_json(
            "/api/settings/notes-directory",
            "PUT",
            {"notes_directory": str(self.notes_dir)},
        )
        self.assertEqual(
            load_sync_directory_setting(self.settings_path),
            sync_directory.resolve(),
        )
        uploaded_status, uploaded = self.request_json(
            "/api/sync/run", "POST", {}
        )
        self.assertEqual(uploaded_status, 200)
        self.assertEqual(uploaded["uploaded"], 3)
        self.assertEqual(uploaded["downloaded"], 0)
        _, persisted_status = self.request_json("/api/sync/status")
        self.assertEqual(persisted_status["device_id"], initial_status["device_id"])

        second_notes = self.root / "second device" / "notes"
        second_settings = self.root / "second device" / "settings.json"
        second_settings.parent.mkdir(parents=True)
        second_server = LocalNoteServer(
            ("127.0.0.1", 0),
            second_notes,
            WEB_DIR,
            settings_path=second_settings,
            sync_state_path=self.root / "second device" / "sync-state.json",
        )
        second_thread = threading.Thread(
            target=second_server.serve_forever,
            daemon=True,
        )
        second_thread.start()
        second_url = f"http://127.0.0.1:{second_server.server_address[1]}"

        def request_second(
            path: str,
            method: str = "GET",
            body: object | None = None,
        ) -> tuple[int, object]:
            payload = None if body is None else json.dumps(body).encode("utf-8")
            request = Request(
                second_url + path,
                data=payload,
                method=method,
                headers={"Content-Type": "application/json"} if payload else {},
            )
            with urlopen(request, timeout=3) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
                return response.status, response_payload

        try:
            request_second(
                "/api/settings/sync-directory",
                "PUT",
                {"sync_directory": str(sync_directory)},
            )
            _, downloaded = request_second("/api/sync/run", "POST", {})
            self.assertEqual(downloaded["downloaded"], 3)
            self.assertEqual(
                second_server.store.lookup("sync term")[0]["body"],
                "original note",
            )
            self.assertEqual(
                second_server.store.lookup("other term")[0]["body"],
                "other original",
            )
            self.assertEqual(
                second_server.store.asset_path("J5词典", asset_name).read_bytes(),
                asset_content,
            )
            _, second_highlights = request_second("/api/highlights")
            synchronized_highlight = next(
                item for item in second_highlights["items"]
                if item["term"] == "sync term"
            )
            self.assertFalse(synchronized_highlight["included"])
            self.assertEqual(
                synchronized_highlight["categories"],
                ["跨设备"],
            )

            second_note = second_server.store.lookup("sync term")[0]
            second_other_note = second_server.store.lookup("other term")[0]
            self.server.store.update_note(
                str(note["id"]),
                {"term": "sync term", "group": "J5词典", "body": "device one"},
            )
            second_server.store.update_note(
                str(second_other_note["id"]),
                {"term": "other term", "group": "J5词典", "body": "device two"},
            )
            self.request_json("/api/sync/run", "POST", {})
            _, merge_result = request_second("/api/sync/run", "POST", {})
            self.assertEqual(merge_result["conflicts"], [])
            self.assertEqual(
                second_server.store.lookup("sync term")[0]["body"],
                "device one",
            )
            self.assertEqual(
                second_server.store.lookup("other term")[0]["body"],
                "device two",
            )
            self.request_json("/api/sync/run", "POST", {})
            self.assertEqual(
                self.server.store.lookup("other term")[0]["body"],
                "device two",
            )

            self.server.store.update_note(
                str(note["id"]),
                {"term": "sync term", "group": "J5词典", "body": "device one conflict"},
            )
            second_server.store.update_note(
                str(second_note["id"]),
                {"term": "sync term", "group": "J5词典", "body": "device two conflict"},
            )
            self.request_json("/api/sync/run", "POST", {})
            _, conflict_result = request_second("/api/sync/run", "POST", {})
            self.assertEqual(len(conflict_result["conflicts"]), 1)
            conflict = conflict_result["conflicts"][0]
            self.assertEqual(conflict["path"], "notes/MyNote-J5词典.txt")
            self.assertEqual(len(conflict["remote_versions"]), 1)

            _, resolution = request_second(
                "/api/sync/resolve",
                "POST",
                {"path": conflict["path"], "choice": "local"},
            )
            self.assertEqual(resolution["conflicts"], [])
            self.request_json("/api/sync/run", "POST", {})
            self.assertEqual(
                self.server.store.lookup("sync term")[0]["body"],
                "device two conflict",
            )

            self.server.store.asset_path("J5词典", asset_name).unlink()
            self.request_json("/api/sync/run", "POST", {})
            request_second("/api/sync/run", "POST", {})
            self.assertFalse(
                second_server.store.asset_path("J5词典", asset_name).exists()
            )
        finally:
            second_server.shutdown()
            second_server.server_close()
            second_thread.join(timeout=3)
        _, disabled = self.request_json(
            "/api/settings/sync-directory",
            "PUT",
            {"sync_directory": ""},
        )
        self.assertEqual(disabled["sync_directory"], "")
        self.assertTrue((sync_directory / ".dict-tango-sync").is_dir())
        self.assertEqual(self.server.store.lookup("sync term")[0]["body"], "device two conflict")

    def test_sync_directory_rejects_missing_or_overlapping_paths(self) -> None:
        with self.assertRaises(HTTPError) as missing:
            self.request_json(
                "/api/settings/sync-directory",
                "PUT",
                {"sync_directory": str(self.root / "missing")},
            )
        self.assertEqual(missing.exception.code, 400)
        with self.assertRaises(HTTPError) as overlapping:
            self.request_json(
                "/api/settings/sync-directory",
                "PUT",
                {"sync_directory": str(self.notes_dir)},
            )
        self.assertEqual(overlapping.exception.code, 400)
        self.assertIsNone(load_sync_directory_setting(self.settings_path))
        self.assertIsNone(self.server.sync_manager.sync_directory)

    def test_escaped_mynote_html_is_rendered_and_loaded_as_editable_markup(self) -> None:
        note = self.server.store.create_note(
            {
                "term": "escaped html",
                "group": DEFAULT_GROUP,
                "body": "&lt;b&gt;bold&lt;/b&gt;&lt;br&gt;next line &amp;lt;IPA&amp;gt;",
            }
        )
        with urlopen(
            self.base_url + "/lookup?" + urlencode({"word": "escaped html"}),
            timeout=3,
        ) as response:
            page = response.read().decode("utf-8")
        self.assertIn("<b>bold</b><br>next line", page)
        self.assertNotIn("&lt;b&gt;", page)
        self.assertIn("&lt;IPA&gt;", page)

        with urlopen(
            self.base_url + "/editor?" + urlencode({"note_id": str(note["id"])}),
            timeout=3,
        ) as response:
            page = response.read().decode("utf-8")
        self.assertIn(
            '<textarea id="body-source" class="html-source" '
            'aria-label="笔记 HTML 源码" hidden>&lt;b&gt;bold&lt;/b&gt;&lt;br&gt;next line &amp;lt;IPA&amp;gt;</textarea>',
            page,
        )

    def test_browser_editor_saves_my_note_compatible_markup(self) -> None:
        editor = self.server.store.create_note(
            {"term": "apple", "group": DEFAULT_GROUP, "body": "old"}
        )
        with urlopen(
            self.base_url + "/editor?" + urlencode({"note_id": str(editor["id"])}),
            timeout=3,
        ) as response:
            page = response.read().decode("utf-8")
        self.assertIn('<textarea id="body-editor" class="rich-editor"', page)
        self.assertNotIn('contenteditable="true"', page)
        self.assertIn('data-command="bold"', page)
        self.assertIn('id="body-source"', page)
        self.assertIn("编辑 HTML 源码", page)
        self.assertIn("粘贴模式", page)
        self.assertIn('id="paste-mode"', page)
        self.assertIn('id="localize-pasted-images"', page)
        self.assertIn('id="clean-current-source"', page)
        self.assertIn('id="delete-current-source"', page)
        self.assertIn('id="deleted-image-assets" name="deleted_image_assets"', page)
        self.assertLess(
            page.index('<div class="actions"><a class="cancel"'),
            page.index('id="delete-current-source"'),
        )
        self.assertLess(
            page.index('id="delete-current-source"'),
            page.index('id="clean-current-source"'),
        )
        self.assertLess(
            page.index('id="clean-current-source"'),
            page.index('type="submit">保存笔记'),
        )
        self.assertIn(".actions.source-mode>button", page)
        self.assertIn("min-height:36px", page)
        self.assertIn("connect-src 'self'", page)
        self.assertIn('<option value="nh3">NH3 清理</option>', page)
        self.assertIn('<option value="klembord">Klembord 兼容</option>', page)
        self.assertIn('<option value="sanitizer">HTML Sanitizer</option>', page)
        self.assertIn("flex-wrap:wrap", page)
        self.assertIn('<div class="source-tools" hidden>', page)
        self.assertIn("<summary>格式</summary>", page)
        self.assertIn("<summary>插入</summary>", page)
        self.assertIn("<summary>链接</summary>", page)
        self.assertIn("<summary>查找/替换</summary>", page)
        self.assertIn("max-height:45vh", page)
        self.assertIn('max-height:calc(100vh - 16px)', page)
        self.assertIn('.tool-group[data-panel-placement="above"]>.tool-panel', page)
        self.assertEqual(page.count('name="note-source-tool-groups"'), 4)
        self.assertNotIn(".tool-group:nth-last-child", page)
        self.assertIn("全部替换", page)
        self.assertIn("插入图片", page)
        script = (WEB_DIR / "editor.js").read_text(encoding="utf-8")
        self.assertIn('fetch("/api/paste"', script)
        self.assertIn('cleanSourceButton.addEventListener("click"', script)
        self.assertIn('sourceEditor.addEventListener("paste", handleHtmlPaste)', script)

        self.assertLess(
            script.index('sourceEditor.addEventListener("paste", handleHtmlPaste)'),
            script.index('sourceEditor.addEventListener("paste", handleImagePaste)'),
        )
        self.assertIn("if (event.defaultPrevented) return;", script)
        self.assertIn('localize_images: localizeImages', script)
        self.assertIn('clipboard?.getData("text/plain")', script)
        self.assertIn('localStorage.setItem(pasteModeStorageKey', script)
        self.assertIn('localStorage.setItem(localizeImagesStorageKey', script)
        self.assertIn("cleanSourceButton.hidden = false", script)
        self.assertIn("cleanSourceButton.hidden = true", script)
        self.assertIn("deleteSourceButton.hidden = false", script)
        self.assertIn("deleteSourceButton.hidden = true", script)
        self.assertIn('deleteSourceButton.addEventListener("click"', script)
        self.assertIn("queueImageAssetCleanup(sourceEditor.value, group)", script)
        self.assertIn('sourceEditor.value = "";', script)
        self.assertIn("localizeImagesField?.checked ?? true", script)
        self.assertIn('group.addEventListener("toggle"', script)
        self.assertIn("positionToolPanel(group)", script)
        self.assertIn('function serializePlainText(value)', script)
        self.assertIn('join(visualLineBreak)', script)
        self.assertIn('const visualLineBreak = "<br>\\n";', script)
        self.assertIn(
            "editor.value = htmlSourceToPlainText(sourceEditor.value);",
            script,
        )
        self.assertIn("function htmlSourceToPlainText(source)", script)
        self.assertIn('parsed.querySelectorAll("script,style,iframe,object,svg,math,template")', script)
        self.assertNotIn("serializeVisualChildren", script)

        fields = urlencode(
            {
                "note_id": str(editor["id"]),
                "term": "apple",
                "group": DEFAULT_GROUP,
                "body": '<p>updated <strong>rich</strong></p>',
            }
        ).encode("utf-8")
        request = Request(
            self.base_url + "/editor",
            data=fields,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urlopen(request, timeout=3) as response:
            self.assertIn("note=", response.url)
        stored = (self.notes_dir / DEFAULT_NOTE_FILE).read_text(encoding="utf-8")
        self.assertIn("<p>updated <strong>rich</strong></p>", stored)
        self.assertNotIn("notes.sqlite3", str(self.server.store.notes_directory))

    def test_editor_group_picker_lists_groups_and_keeps_freeform_creation(self) -> None:
        self.server.store.create_group("J5词典")
        with urlopen(
            self.base_url + "/editor?" + urlencode({"term": "new word"}),
            timeout=3,
        ) as response:
            page = response.read().decode("utf-8")
        self.assertIn(
            'id="group" name="group" inputmode="text" list="group-options"',
            page,
        )
        self.assertIn('<datalist id="group-options">', page)
        self.assertIn('<option value="[Default]"></option>', page)
        self.assertIn('<option value="J5词典"></option>', page)
        self.assertIn("保存时会自动创建", page)

        script = (WEB_DIR / "editor.js").read_text(encoding="utf-8")
        self.assertIn('const groupStorageKey = "dictTangoLastNoteGroup";', script)
        self.assertIn("groupField.addEventListener(\"change\"", script)
        self.assertIn("findGroupOption(rememberedGroup)", script)

        response_url = self.request_form(
            "/editor",
            {
                "note_id": "",
                "modal": "0",
                "term": "new group word",
                "group": "自定义新组",
                "body": "new group body",
            },
        )
        self.assertIn("/?note=", response_url)
        self.assertEqual(
            self.server.store.lookup("new group word")[0]["group"],
            "自定义新组",
        )

    def test_paste_modes_sanitize_clipboard_html(self) -> None:
        clipboard_html = (
            "<html><head><title>metadata</title><style>body{display:none}</style>"
            "</head><body><h1 onclick='bad()'>Heading</h1>"
            "<p class='copied' onclick='bad()' style='color:red'>Safe "
            "<strong>text</strong></p>"
            "<b style='color:rgb(34,34,34);font-family:Arial;font-size:medium'>Bold</b>"
            "<span style='color:rgb(34,34,34)'>Styled span</span><script>bad()</script>"
            "<img src='javascript:bad()' alt='image'><hr>"
            "<p>@@@<em>preserved</em>@@@</p></body></html>"
        )
        status, nh3_result = self.request_json(
            "/api/paste",
            "POST",
            {"html": clipboard_html, "group": DEFAULT_GROUP, "mode": "nh3"},
        )
        self.assertEqual(status, 200)
        nh3_html = nh3_result["html"]
        self.assertIn("Heading", nh3_html)
        self.assertIn("<p>Safe <strong>text</strong></p>", nh3_html)
        self.assertIn("<b>Bold</b>Styled span", nh3_html)
        self.assertIn("@@@<em>preserved</em>@@@", nh3_html)
        self.assertNotIn("<h1", nh3_html)
        self.assertNotIn("<hr", nh3_html)
        self.assertNotIn("metadata", nh3_html)
        self.assertNotIn("display:none", nh3_html)
        self.assertNotIn("font-family", nh3_html)
        self.assertNotIn("font-size", nh3_html)
        self.assertNotIn("rgb(", nh3_html)
        self.assertNotIn("bad()", nh3_html)
        self.assertNotIn("class=", nh3_html)

        _, klembord_result = self.request_json(
            "/api/paste",
            "POST",
            {"html": clipboard_html, "group": DEFAULT_GROUP, "mode": "klembord"},
        )
        self.assertEqual(klembord_result["html"], nh3_html)

        _, sanitizer_result = self.request_json(
            "/api/paste",
            "POST",
            {"html": clipboard_html, "group": DEFAULT_GROUP, "mode": "sanitizer"},
        )
        self.assertIn("<h1>Heading</h1>", sanitizer_result["html"])
        self.assertIn("<span>Styled span</span>", sanitizer_result["html"])
        self.assertIn("<hr>", sanitizer_result["html"])

        _, plain_result = self.request_json(
            "/api/paste",
            "POST",
            {
                "html": "Plain <strong onclick='bad()'>text</strong><script>bad()</script>",
                "group": DEFAULT_GROUP,
                "mode": "sanitizer",
            },
        )
        self.assertEqual(plain_result["html"], "Plain <strong>text</strong>")

        request = Request(
            self.base_url + "/api/paste",
            data=json.dumps(
                {"html": clipboard_html, "group": DEFAULT_GROUP, "mode": "raw"}
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as invalid_mode:
            urlopen(request, timeout=3)
        self.assertEqual(invalid_mode.exception.code, 400)

    def test_paste_localizes_data_images_and_blocks_private_remote_urls(self) -> None:
        image_bytes = b"\x89PNG\r\n\x1a\nvalid test image"
        data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
        _, localized = self.request_json(
            "/api/paste",
            "POST",
            {
                "html": f'<p>before<img src="{data_url}" alt="local">after</p>',
                "group": DEFAULT_GROUP,
                "mode": "nh3",
            },
        )
        self.assertEqual(localized["warnings"], [])
        self.assertEqual(localized["localized_images"], 1)
        match = re.search(r'<img src="(pasted-image-[0-9a-f]+\.png)" alt="local"', localized["html"])
        self.assertIsNotNone(match)
        self.assertEqual(
            (self.notes_dir / "MyNote-[Default]" / match.group(1)).read_bytes(),
            image_bytes,
        )
        self.server.store.create_note(
            {
                "term": "localized image",
                "group": DEFAULT_GROUP,
                "body": localized["html"],
            }
        )
        with urlopen(
            self.base_url + "/lookup?" + urlencode({"word": "localized image"}),
            timeout=3,
        ) as response:
            page = response.read().decode("utf-8")
        self.assertIn(f'src="/assets/Default/{match.group(1)}"', page)

        with patch(
            "app.request_paste_image",
            return_value=(200, None, "image/png", image_bytes),
        ) as fetch_image:
            _, remote = self.request_json(
                "/api/paste",
                "POST",
                {
                    "html": '<img src="https://images.example.test/photo.png" alt="remote">',
                    "group": DEFAULT_GROUP,
                    "mode": "nh3",
                },
            )
        fetch_image.assert_called_once()
        self.assertEqual(remote["warnings"], [])
        self.assertEqual(remote["localized_images"], 1)
        self.assertTrue(remote["localization_enabled"])
        remote_match = re.search(
            r'<img src="(pasted-image-[0-9a-f]+\.png)" alt="remote"',
            remote["html"],
        )
        self.assertIsNotNone(remote_match)
        self.assertEqual(
            (self.notes_dir / "MyNote-[Default]" / remote_match.group(1)).read_bytes(),
            image_bytes,
        )

        with patch("app.request_paste_image") as fetch_image:
            _, not_localized = self.request_json(
                "/api/paste",
                "POST",
                {
                    "html": '<img src="https://images.example.test/photo.png" alt="remote">',
                    "group": DEFAULT_GROUP,
                    "mode": "nh3",
                    "localize_images": False,
                },
            )
        fetch_image.assert_not_called()
        self.assertEqual(not_localized["warnings"], [])
        self.assertEqual(not_localized["localized_images"], 0)
        self.assertFalse(not_localized["localization_enabled"])
        self.assertIn("https://images.example.test/photo.png", not_localized["html"])

        private_url = "http://127.0.0.1/health"
        _, blocked = self.request_json(
            "/api/paste",
            "POST",
            {
                "html": f'<img src="{private_url}" alt="remote">',
                "group": DEFAULT_GROUP,
                "mode": "sanitizer",
            },
        )
        self.assertEqual(blocked["localized_images"], 0)
        self.assertIn("非公网网络", blocked["warnings"][0])
        self.assertIn(private_url, blocked["html"])

    def test_paste_image_download_reads_response_after_connection_close(self) -> None:
        image_bytes = b"\x89PNG\r\n\x1a\nvalid test image"

        class ImageHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(image_bytes)))
                self.end_headers()
                self.wfile.write(image_bytes)

            def log_message(self, format: str, *args: object) -> None:
                pass

        image_server = ThreadingHTTPServer(("127.0.0.1", 0), ImageHandler)
        image_thread = threading.Thread(
            target=image_server.serve_forever,
            daemon=True,
        )
        image_thread.start()
        try:
            port = image_server.server_address[1]
            source = f"http://images.example.test:{port}/photo.png"
            resolved_address = (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", port),
            )
            with patch(
                "app.resolve_public_paste_image_url",
                return_value=(
                    urlsplit(source),
                    "images.example.test",
                    port,
                    [resolved_address],
                ),
            ):
                status, location, content_type, content = request_paste_image(
                    source,
                    time.monotonic() + 5,
                )
        finally:
            image_server.shutdown()
            image_server.server_close()
            image_thread.join(timeout=3)

        self.assertEqual(status, 200)
        self.assertIsNone(location)
        self.assertEqual(content_type, "image/png")
        self.assertEqual(content, image_bytes)

    def test_deleting_source_removes_only_unreferenced_local_images_after_save(self) -> None:
        image_bytes = b"\x89PNG\r\n\x1a\nvalid test image"
        self.server.store.save_asset(DEFAULT_GROUP, "orphaned.png", image_bytes)
        self.server.store.save_asset(DEFAULT_GROUP, "shared.png", image_bytes)
        note_to_clear = self.server.store.create_note(
            {
                "term": "clear images",
                "group": DEFAULT_GROUP,
                "body": '<img src="orphaned.png"><img src="shared.png">',
            }
        )
        self.server.store.create_note(
            {
                "term": "keep shared image",
                "group": DEFAULT_GROUP,
                "body": '<img src="/assets/Default/shared.png">',
            }
        )

        self.request_form(
            "/editor",
            {
                "note_id": str(note_to_clear["id"]),
                "term": "clear images",
                "group": DEFAULT_GROUP,
                "body": "",
                "deleted_image_assets": json.dumps(
                    [
                        {"group": DEFAULT_GROUP, "filename": "orphaned.png"},
                        {"group": DEFAULT_GROUP, "filename": "shared.png"},
                    ]
                ),
            },
        )

        self.assertFalse((self.notes_dir / "MyNote-[Default]" / "orphaned.png").exists())
        self.assertTrue((self.notes_dir / "MyNote-[Default]" / "shared.png").exists())
        self.assertEqual(self.server.store.get_note(str(note_to_clear["id"]))["body"], "")

    def test_editor_accepts_html_source_and_renders_sanitized_markup(self) -> None:
        payload = urlencode(
            {
                "note_id": "",
                "modal": "1",
                "term": "html source",
                "group": DEFAULT_GROUP,
                "body": '<p class="ignored" onclick="bad()">Hello <strong>HTML</strong></p><script>bad()</script>',
            }
        ).encode("utf-8")
        request = Request(
            self.base_url + "/editor",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self.base_url,
            },
        )
        with urlopen(request, timeout=3):
            pass

        saved = self.server.store.lookup("html source")[0]["body"]
        self.assertIn("<p class=\"ignored\" onclick=\"bad()\">", saved)
        with urlopen(
            self.base_url + "/lookup?" + urlencode({"word": "html source"}),
            timeout=3,
        ) as response:
            page = response.read().decode("utf-8")
        self.assertIn("<p>Hello <strong>HTML</strong></p>", page)
        self.assertNotIn("onclick", page)
        self.assertNotIn("<script>bad()</script>", page)

    def test_visual_plain_text_keeps_html_like_text_literal(self) -> None:
        note = self.server.store.create_note(
            {
                "term": "plain html text",
                "group": DEFAULT_GROUP,
                "body": "literal &#60;strong&#62;markup&#60;/strong&#62; "
                "&#60;script&#62;not code&#60;/script&#62; &#38; text",
            }
        )
        with urlopen(
            self.base_url + "/lookup?" + urlencode({"word": note["term"]}),
            timeout=3,
        ) as response:
            page = response.read().decode("utf-8")
        self.assertIn(
            "literal &lt;strong&gt;markup&lt;/strong&gt; "
            "&lt;script&gt;not code&lt;/script&gt; &amp; text",
            page,
        )
        self.assertNotIn("<script>not code</script>", page)

        script = (WEB_DIR / "editor.js").read_text(encoding="utf-8")
        self.assertIn(
            "editor.value = htmlSourceToPlainText(sourceEditor.value);",
            script,
        )
        self.assertIn("function serializePlainText(value)", script)
        self.assertNotIn("contenteditable", script)
        self.assertNotIn("sanitizedMarkup", script)

    def test_editor_source_breaks_are_multiline_without_extra_visual_lines(self) -> None:
        note = self.server.store.create_note(
            {
                "term": "formatted breaks",
                "group": DEFAULT_GROUP,
                "body": "first<br>\nsecond",
            }
        )
        with urlopen(
            self.base_url + "/editor?" + urlencode({"note_id": str(note["id"])}),
            timeout=3,
        ) as response:
            page = response.read().decode("utf-8")
        self.assertIn(
            'aria-label="笔记 HTML 源码" hidden>first&lt;br&gt;\nsecond</textarea>',
            page,
        )
        self.assertIn(
            'aria-label="笔记纯文本内容">first\nsecond</textarea>',
            page,
        )
        self.assertIn("源码中的换行仅供阅读，不会额外显示", page)

    def test_lookup_ignores_source_newlines_after_break_tags(self) -> None:
        note = self.server.store.create_note(
            {
                "term": "single visual breaks",
                "group": DEFAULT_GROUP,
                "body": "first<br>\nsecond<br>\nthird",
            }
        )
        with urlopen(
            self.base_url + "/lookup?" + urlencode({"word": note["term"]}),
            timeout=3,
        ) as response:
            page = response.read().decode("utf-8")
        self.assertIn("first<br>second<br>third", page)
        self.assertNotIn("first<br>\nsecond", page)

    def test_modal_editor_save_stays_in_editor_until_parent_closes_dialog(self) -> None:
        payload = json.dumps(
            {"term": "api word", "group": DEFAULT_GROUP, "body": "created"}
        ).encode("utf-8")
        api_request = Request(
            self.base_url + "/api/notes",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": self.base_url,
            },
        )
        with urlopen(api_request, timeout=3) as response:
            self.assertEqual(response.status, 201)

        with urlopen(
            self.base_url + "/editor?" + urlencode({"modal": "1", "term": "modal word"}),
            timeout=3,
        ) as response:
            page = response.read().decode("utf-8")
        self.assertIn('name="modal" value="1"', page)

        request = Request(
            self.base_url + "/editor",
            data=urlencode(
                {
                    "note_id": "",
                    "modal": "1",
                    "term": "modal word",
                    "group": DEFAULT_GROUP,
                    "body": "saved in dialog",
                }
            ).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self.base_url,
            },
        )
        with urlopen(request, timeout=3) as response:
            self.assertIn("/editor?", response.url)
            self.assertIn("saved=1", response.url)
            self.assertIn("modal=1", response.url)
        self.assertEqual(self.server.store.lookup("modal word")[0]["body"], "saved in dialog")

    def test_write_origin_allows_loopback_alias_and_forwarded_host_without_port(self) -> None:
        requests = (
            ("127.0.0.1", self.base_url, "forwarded host"),
            (
                f"127.0.0.1:{self.server.server_address[1]}",
                f"http://localhost:{self.server.server_address[1]}",
                "loopback alias",
            ),
        )
        for host, origin, term in requests:
            request_payload = json.dumps(
                {"term": term, "group": DEFAULT_GROUP, "body": "saved"}
            ).encode("utf-8")
            request = Request(
                self.base_url + "/api/notes",
                data=request_payload,
                method="POST",
                headers={"Content-Type": "application/json", "Host": host, "Origin": origin},
            )
            with urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 201)

    def test_write_origin_accepts_webview_same_origin_metadata(self) -> None:
        payload = json.dumps(
            {"term": "webview metadata", "group": DEFAULT_GROUP, "body": "saved"}
        ).encode("utf-8")
        for headers in (
            {
                "Content-Type": "application/json",
                "Origin": "null",
                "Sec-Fetch-Site": "same-origin",
            },
            {
                "Content-Type": "application/json",
                "Referer": self.base_url + "/",
                "Sec-Fetch-Site": "same-origin",
            },
        ):
            request = Request(
                self.base_url + "/api/notes",
                data=payload,
                method="POST",
                headers=headers,
            )
            with urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 201)
            payload = json.dumps(
                {
                    "term": "webview metadata " + str(len(headers)),
                    "group": DEFAULT_GROUP,
                    "body": "saved",
                }
            ).encode("utf-8")

    def test_editor_keeps_unsupported_original_markup_on_no_op_open(self) -> None:
        raw_body = '<div><script>window.noteCode()</script><link rel="stylesheet" href="x.css"></div>'
        note = self.server.store.create_note(
            {"term": "raw", "group": DEFAULT_GROUP, "body": raw_body}
        )
        with urlopen(
            self.base_url + "/editor?" + urlencode({"note_id": str(note["id"])}),
            timeout=3,
        ) as response:
            page = response.read().decode("utf-8")
        self.assertIn("&lt;script&gt;window.noteCode()&lt;/script&gt;", page)
        self.assertNotIn("<script>window.noteCode()</script>", page)

    def test_editor_uses_full_width_without_card_container(self) -> None:
        with urlopen(
            self.base_url + "/editor?" + urlencode({"term": "wide editor", "modal": "1"}),
            timeout=3,
        ) as response:
            page = response.read().decode("utf-8")
        self.assertIn('<label for="term">词头</label>', page)
        self.assertNotIn('<label for="term">词条</label>', page)
        self.assertIn("html,body{width:100%;height:100%", page)
        self.assertIn("main{width:100%;max-width:none", page)
        self.assertIn(
            ".editor{min-width:0;min-height:100%;max-width:100%;padding:16px;"
            "border:0;border-radius:0;background:#fff;box-shadow:none}",
            page,
        )

    def test_homepage_uses_native_delete_confirmation_dialog(self) -> None:
        note = self.server.store.create_note(
            {"term": "confirm delete", "group": DEFAULT_GROUP, "body": "remove"}
        )
        with urlopen(self.base_url + "/", timeout=3) as response:
            page = response.read().decode("utf-8")
        self.assertIn('<dialog id="delete-dialog"', page)
        self.assertIn('id="delete-confirmation-message"', page)
        self.assertIn('id="cancel-delete-button"', page)
        self.assertIn('id="confirm-delete-button"', page)
        self.assertNotIn('id="delete-frame"', page)
        self.assertIn('<dialog id="note-actions-dialog"', page)
        self.assertIn('id="note-actions-edit"', page)
        self.assertIn('id="note-actions-delete"', page)
        self.assertIn('id="note-actions-cancel"', page)

        script = (WEB_DIR / "app.js").read_text(encoding="utf-8")
        styles = (WEB_DIR / "styles.css").read_text(encoding="utf-8")
        self.assertIn('byId("cancel-delete-button").addEventListener("click", () => deleteDialog.close())', script)
        self.assertIn('method: "DELETE"', script)
        self.assertIn('}, 550);', script)
        self.assertIn('"ContextMenu"', script)
        self.assertIn("openDeleteConfirmation(note)", script)
        self.assertIn('textContent = `词条：${note.term}`', script)
        self.assertIn(".note-actions-dialog { width: max-content;", styles)

        self.request_json(f"/api/notes/{note['id']}", "DELETE")
        self.assertEqual(self.server.store.lookup("confirm delete"), [])

    def test_homepage_places_group_info_below_library_and_imports_from_dialog(self) -> None:
        page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        styles = (WEB_DIR / "styles.css").read_text(encoding="utf-8")
        library_column = page.index('<div class="library-column">')
        library = page.index('<aside class="sidebar card">', library_column)
        group_info = page.index('<section id="current-group-info"', library)
        group_info_end = page.index("</section>", group_info)
        library_column_end = page.index("</div>", group_info_end)
        detail = page.index('<section class="detail card"', library_column_end)

        self.assertLess(library_column, library)
        self.assertLess(library, group_info)
        self.assertLess(group_info, library_column_end)
        self.assertLess(library_column_end, detail)
        sidebar_heading = page.index('<div class="sidebar-heading">', library)
        quick_sync_button = page.index('id="quick-sync-button"', sidebar_heading)
        search_input = page.index('id="search-input"', sidebar_heading)
        self.assertLess(sidebar_heading, quick_sync_button)
        self.assertLess(quick_sync_button, search_input)
        self.assertIn('<dialog id="sync-report-dialog"', page)
        self.assertIn('id="sync-report-uploaded"', page)
        self.assertIn('id="sync-report-downloaded"', page)
        self.assertIn('id="sync-report-conflict-count"', page)
        script = (WEB_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('byId("quick-sync-button").addEventListener("click", quickSync)', script)
        quick_sync = script[script.index("async function quickSync()"):script.index("function showSyncReport")]
        self.assertIn('await runSync({ showReport: true });', quick_sync)
        self.assertIn("showSyncReport(syncResult, syncError)", script)
        self.assertNotIn('id="backup"', page)
        self.assertIn('id="open-import-dialog"', page)
        self.assertIn('<dialog id="import-dialog"', page)
        self.assertIn('<dialog id="settings-dialog"', page)
        self.assertLess(page.index("导出备份"), page.index("导入备份"))
        self.assertLess(
            page.index('id="open-import-dialog"'),
            page.index('id="open-settings-dialog"'),
        )
        settings_dialog = page.index('<dialog id="settings-dialog"')
        notes_directory_form = page.index('id="notes-directory-form"')
        highlight_rules_form = page.index('id="highlight-rules-form"')
        library = page.index('<aside class="sidebar card">')
        library_end = page.index("</aside>", library)
        self.assertGreater(notes_directory_form, settings_dialog)
        self.assertGreater(notes_directory_form, library_end)
        self.assertGreater(highlight_rules_form, notes_directory_form)
        self.assertIn('id="highlight-rules-path"', page)
        self.assertIn('id="save-highlight-rules-button"', page)
        self.assertIn('id="sync-directory-form"', page)
        self.assertIn('id="sync-directory"', page)
        self.assertIn('id="run-sync-button"', page)
        self.assertIn('id="sync-conflicts"', page)
        self.assertNotIn("<summary>本地笔记根文件夹</summary>", page)
        self.assertIn('id="import-file"', page)
        self.assertIn('id="import-button"', page)
        self.assertIn('id="current-group-note-path"', page)
        self.assertIn('id="current-group-resource-path"', page)
        app_script = (WEB_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('api("/api/settings/highlight-rules")', app_script)
        self.assertIn('byId("highlight-rules-form").addEventListener', app_script)
        self.assertIn('api("/api/sync/run"', app_script)
        self.assertIn('api("/api/sync/resolve"', app_script)
        self.assertIn(
            ".layout { display: grid; grid-template-columns: 340px minmax(0, 1fr);",
            styles,
        )
        self.assertIn(
            ".library-column { display: flex; min-width: 0; flex-direction: column; gap: 18px; }",
            styles,
        )
        self.assertIn(
            ".sidebar { display: flex; flex: 1 1 auto; flex-direction: column; min-height: 570px;",
            styles,
        )
        self.assertIn(
            ".sidebar { flex: 0 0 auto; min-height: 0; padding: 15px 13px 10px; }",
            styles,
        )
        self.assertIn(
            ".results-list { display: grid; grid-template-columns: minmax(0, 1fr); "
            "gap: 4px; max-height: min(42vh, 360px); min-width: 0; min-height: 0; "
            "margin-top: 6px; overflow-x: hidden; overflow-y: auto; overscroll-behavior: contain; }",
            styles,
        )
        self.assertIn(".result-item { width: 100%; min-width: 0;", styles)
        self.assertIn(".results-list { max-height: 210px; }", styles)
        self.assertIn(
            ".layout { grid-template-columns: minmax(0, 1fr); gap: 12px; }",
            styles,
        )

    def test_group_management_and_protected_default_group(self) -> None:
        _, group = self.request_json("/api/groups", "POST", {"name": "学习"})
        _, renamed = self.request_json(
            f"/api/groups/{group['id']}", "PUT", {"name": "已改名"}
        )
        self.assertTrue((self.notes_dir / "MyNote-已改名.txt").exists())
        _, groups = self.request_json("/api/groups")
        self.assertIn("已改名", [item["name"] for item in groups])
        default = next(item for item in groups if item["is_default"])
        with self.assertRaises(HTTPError) as delete_default:
            self.request_json(f"/api/groups/{default['id']}", "DELETE")
        self.assertEqual(delete_default.exception.code, 400)
        self.request_json(f"/api/groups/{renamed['id']}", "DELETE")

    def test_create_group_with_custom_paths_creates_txt_only_at_selected_path(self) -> None:
        external_dir = self.root / "external"
        external_dir.mkdir()
        external_note_path = external_dir / "MyNote-test.txt"
        resource_dir = self.root / "resources"
        resource_dir.mkdir()

        _, group = self.request_json(
            "/api/groups",
            "POST",
            {
                "name": "test",
                "note_path": str(external_note_path),
                "resource_directory": str(resource_dir),
            },
        )

        self.assertEqual(group["name"], "test")
        self.assertTrue(external_note_path.is_file())
        self.assertEqual(external_note_path.read_text(encoding="utf-8"), "")
        self.assertFalse((self.notes_dir / "MyNote-test.txt").exists())
        paths = self.server.store.group_paths()["test"]
        self.assertEqual(paths["note_path"], str(external_note_path.resolve()))
        self.assertEqual(paths["resource_directory"], str(resource_dir.resolve()))
        self.assertEqual(
            paths["default_resource_directory"],
            str((external_dir / "MyNote-test").resolve()),
        )
        self.assertEqual(
            load_group_note_paths(self.settings_path),
            {"MyNote-test.txt": str(external_note_path.resolve())},
        )
        self.assertEqual(
            load_group_resource_directories(self.settings_path),
            {"test": str(resource_dir.resolve())},
        )

    def test_create_group_with_existing_custom_txt_preserves_its_contents(self) -> None:
        external_dir = self.root / "external"
        external_dir.mkdir()
        external_note_path = external_dir / "MyNote-existing.txt"
        original_content = (
            "MyNote header\n\nexisting\n"
            "<font size=5>existing</font>\nkept\n</>\n"
        )
        external_note_path.write_text(original_content, encoding="utf-8")

        self.request_json(
            "/api/groups",
            "POST",
            {
                "name": "existing",
                "note_path": str(external_note_path),
                "resource_directory": "",
            },
        )

        self.assertEqual(external_note_path.read_text(encoding="utf-8"), original_content)
        self.assertEqual(self.server.store.lookup("existing")[0]["body"], "kept")
        self.assertFalse((self.notes_dir / "MyNote-existing.txt").exists())

    def test_create_group_cleans_up_new_txt_if_settings_cannot_be_saved(self) -> None:
        external_dir = self.root / "external"
        external_dir.mkdir()
        external_note_path = external_dir / "MyNote-test.txt"

        with patch.object(
            self.server,
            "_write_settings",
            side_effect=StoreError("settings unavailable"),
        ):
            with self.assertRaisesRegex(StoreError, "settings unavailable"):
                self.server.create_group("test", str(external_note_path))

        self.assertFalse(external_note_path.exists())
        self.assertFalse((self.notes_dir / "MyNote-test.txt").exists())
        self.assertEqual(self.server.store.group_note_paths(), {})
        self.assertNotIn(
            "test",
            [group["name"] for group in self.server.store.list_groups()],
        )

    def test_group_management_changes_and_persists_notes_root(self) -> None:
        self.server.store.create_note(
            {"term": "old-root", "group": DEFAULT_GROUP, "body": "keep"}
        )
        new_notes_dir = self.root / "new notes"
        new_notes_dir.mkdir()
        (new_notes_dir / DEFAULT_NOTE_FILE).write_text(
            "MyNote header\n\nnew-root\n<font size=5>new-root</font>\nnew content\n</>\n",
            encoding="utf-8",
        )

        page = urlopen(self.base_url + "/", timeout=3).read().decode("utf-8")
        self.assertIn('id="notes-directory-form"', page)
        self.assertNotIn('id="group-paths-form"', page)
        self.assertIn('id="edit-group-path-button"', page)
        self.assertIn('id="group-path-dialog"', page)
        self.assertIn('name="note-path-mode" value="default"', page)
        self.assertIn('name="note-path-mode" value="custom"', page)
        self.assertIn('name="resource-path-mode" value="default"', page)
        self.assertIn('name="resource-path-mode" value="custom"', page)
        self.assertIn('id="group-note-path"', page)
        self.assertIn('id="group-resource-path"', page)
        self.assertIn('id="add-group-button"', page)
        self.assertIn('id="group-dialog"', page)
        self.assertIn(">下一步：设置路径</button>", page)
        self.assertLess(
            page.index('id="group-cancel-button"'),
            page.index('id="group-delete-button"'),
        )
        self.assertLess(
            page.index('id="group-delete-button"'),
            page.index('id="edit-group-path-button"'),
        )
        self.assertLess(
            page.index('id="edit-group-path-button"'),
            page.index('id="group-save-button"'),
        )
        app_script = (WEB_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("openNewGroupPathDialog(name);", app_script)
        self.assertIn("function openGroupPathDialog(groupId = state.editingGroupId)", app_script)
        self.assertIn(
            'byId("edit-group-path-button").addEventListener("click", () => openGroupPathDialog());',
            app_script,
        )
        self.assertIn('api("/api/groups", {', app_script)
        self.assertNotIn('href="/groups"', page)
        with urlopen(self.base_url + "/groups", timeout=3) as response:
            self.assertEqual(response.url.rstrip("/"), self.base_url)
        _, settings = self.request_json("/api/settings/notes-directory")
        self.assertEqual(settings["notes_directory"], str(self.notes_dir.resolve()))
        self.request_json(
            "/api/settings/notes-directory",
            "PUT",
            {"notes_directory": str(new_notes_dir)},
        )

        self.assertEqual(self.server.store.notes_directory, new_notes_dir.resolve())
        self.assertEqual(self.server.store.lookup("new-root")[0]["body"], "new content")
        self.assertEqual(self.server.store.lookup("old-root"), [])
        old_notes_file = self.notes_dir / DEFAULT_NOTE_FILE
        self.assertTrue(old_notes_file.exists())
        self.assertIn("old-root", old_notes_file.read_text(encoding="utf-8"))
        self.assertEqual(load_notes_directory_setting(self.settings_path), new_notes_dir.resolve())

    def test_invalid_notes_root_is_rejected_without_switching(self) -> None:
        with self.assertRaises(HTTPError) as response:
            self.request_json(
                "/api/settings/notes-directory",
                "PUT",
                {"notes_directory": str(self.root / "missing")},
            )
        self.assertEqual(response.exception.code, 400)
        with self.assertRaises(HTTPError) as invalid_type:
            self.request_json(
                "/api/settings/notes-directory",
                "PUT",
                {"notes_directory": None},
            )
        self.assertEqual(invalid_type.exception.code, 400)
        self.assertEqual(self.server.store.notes_directory, self.notes_dir.resolve())
        self.assertFalse(self.settings_path.exists())

    def test_group_txt_path_and_optional_resource_override_survive_rename(self) -> None:
        _, group = self.request_json("/api/groups", "POST", {"name": "资料"})
        external_dir = self.root / "external notes"
        external_dir.mkdir()
        external_note_path = external_dir / "MyNote-资料.txt"
        external_note_path.write_text(
            "MyNote header\n\nexternal-note\n"
            "<font size=5>external-note</font>\nfrom external path\n</>\n",
            encoding="utf-8",
        )
        default_resource_dir = external_dir / "MyNote-资料"
        resource_dir = self.root / "shared resources"
        resource_dir.mkdir()
        _, settings = self.request_json(
            "/api/settings/group-paths",
            "PUT",
            {
                "group": group["name"],
                "note_path": str(external_note_path),
                "resource_directory": "",
            },
        )
        group_paths = settings["group_paths"]["资料"]
        self.assertEqual(group_paths["note_path"], str(external_note_path.resolve()))
        self.assertEqual(
            group_paths["default_resource_directory"],
            str(default_resource_dir.resolve()),
        )
        self.assertEqual(group_paths["resource_directory"], "")
        self.assertEqual(self.server.store.lookup("external-note")[0]["body"], "from external path")
        self.assertEqual(
            load_notes_directory_setting(self.settings_path),
            self.notes_dir.resolve(),
        )
        self.assertEqual(
            load_group_note_paths(self.settings_path),
            {external_note_path.name: str(external_note_path.resolve())},
        )

        payload = b"resource image"
        self.request_json(
            "/api/assets",
            "POST",
            {
                "group": "资料",
                "filename": "sample.png",
                "content": base64.b64encode(payload).decode("ascii"),
            },
        )
        self.assertEqual((default_resource_dir / "sample.png").read_bytes(), payload)
        with urlopen(
            self.base_url + f"/assets/{quote('资料')}/sample.png",
            timeout=3,
        ) as response:
            self.assertEqual(response.read(), payload)

        _, settings = self.request_json(
            "/api/settings/group-paths",
            "PUT",
            {
                "group": group["name"],
                "note_path": str(external_note_path),
                "resource_directory": str(resource_dir),
            },
        )
        self.assertEqual(
            settings["group_paths"]["资料"]["resource_directory"],
            str(resource_dir.resolve()),
        )
        self.assertEqual(
            load_group_resource_directories(self.settings_path),
            {"资料": str(resource_dir.resolve())},
        )
        self.request_json(
            "/api/assets",
            "POST",
            {
                "group": "资料",
                "filename": "sample-custom.png",
                "content": base64.b64encode(payload).decode("ascii"),
            },
        )
        self.assertEqual((resource_dir / "sample-custom.png").read_bytes(), payload)
        self.request_json(
            "/api/settings/notes-directory",
            "PUT",
            {"notes_directory": str(self.notes_dir)},
        )
        restarted_server = LocalNoteServer(
            ("127.0.0.1", 0),
            self.notes_dir,
            WEB_DIR,
            settings_path=self.settings_path,
        )
        try:
            self.assertEqual(
                restarted_server.store.group_note_paths(),
                {external_note_path.name: str(external_note_path.resolve())},
            )
            self.assertEqual(
                restarted_server.store.group_resource_directories(),
                {"资料": str(resource_dir.resolve())},
            )
            self.assertEqual(
                restarted_server.store.lookup("external-note")[0]["body"],
                "from external path",
            )
        finally:
            restarted_server.server_close()

        with urlopen(
            self.base_url + f"/assets/{quote('资料')}/sample-custom.png",
            timeout=3,
        ) as response:
            self.assertEqual(response.read(), payload)

        _, renamed = self.request_json(
            f"/api/groups/{group['id']}",
            "PUT",
            {"name": "资料改名"},
        )
        _, settings = self.request_json("/api/settings/group-paths")
        self.assertEqual(
            settings["group_paths"]["资料改名"]["note_path"],
            str((external_dir / "MyNote-资料改名.txt").resolve()),
        )
        self.assertEqual(
            settings["group_paths"]["资料改名"]["resource_directory"],
            str(resource_dir.resolve()),
        )
        self.assertTrue((external_dir / "MyNote-资料改名.txt").is_file())
        root_group_path = settings["group_paths"]["资料改名"]["default_note_path"]
        self.request_json(
            "/api/settings/group-paths",
            "PUT",
            {
                "group": renamed["name"],
                "note_path": root_group_path,
                "resource_directory": str(resource_dir),
            },
        )
        self.assertEqual(self.server.store.lookup("external-note"), [])
        self.assertIn(
            "external-note",
            (external_dir / "MyNote-资料改名.txt").read_text(encoding="utf-8"),
        )
        self.request_json(
            "/api/settings/group-paths",
            "PUT",
            {
                "group": renamed["name"],
                "note_path": str(external_dir / "MyNote-资料改名.txt"),
                "resource_directory": str(resource_dir),
            },
        )
        self.assertEqual(len(self.server.store.lookup("external-note")), 1)
        self.request_json(
            "/api/assets",
            "POST",
            {
                "group": renamed["name"],
                "filename": "renamed.png",
                "content": base64.b64encode(payload).decode("ascii"),
            },
        )
        self.assertTrue((resource_dir / "renamed.png").is_file())

        self.request_json(
            "/api/settings/group-paths",
            "PUT",
            {
                "group": renamed["name"],
                "note_path": str((external_dir / "MyNote-资料改名.txt")),
                "resource_directory": "",
            },
        )
        self.request_json(
            "/api/assets",
            "POST",
            {
                "group": renamed["name"],
                "filename": "default.png",
                "content": base64.b64encode(payload).decode("ascii"),
            },
        )
        self.assertTrue((external_dir / "MyNote-资料改名" / "default.png").is_file())
        self.request_json(
            "/api/settings/group-paths",
            "PUT",
            {
                "group": renamed["name"],
                "note_path": str(external_dir / "MyNote-资料改名.txt"),
                "resource_directory": str(resource_dir),
            },
        )
        note = self.server.store.lookup("external-note")[0]
        self.request_json(f"/api/notes/{note['id']}", "DELETE")
        self.request_json(f"/api/groups/{renamed['id']}", "DELETE")
        _, settings = self.request_json("/api/settings/group-paths")
        self.assertNotIn("资料改名", settings["group_paths"])
        self.assertTrue((resource_dir / "sample-custom.png").is_file())
        self.assertFalse((external_dir / "MyNote-资料改名.txt").exists())
        self.assertFalse(
            (self.notes_dir / "MyNote-资料改名.txt").exists()
        )

    def test_delete_group_succeeds_when_configured_note_file_is_missing(self) -> None:
        _, group = self.request_json("/api/groups", "POST", {"name": "test"})
        external_dir = self.root / "external"
        external_dir.mkdir()
        external_note_path = external_dir / "MyNote-test.txt"
        external_note_path.write_text("", encoding="utf-8")
        self.request_json(
            "/api/settings/group-paths",
            "PUT",
            {
                "group": "test",
                "note_path": str(external_note_path),
                "resource_directory": "",
            },
        )
        external_note_path.unlink()

        self.request_json(f"/api/groups/{group['id']}", "DELETE")

        _, groups = self.request_json("/api/groups")
        _, settings = self.request_json("/api/settings/group-paths")
        self.assertNotIn("test", [item["name"] for item in groups])
        self.assertNotIn("MyNote-test.txt", settings["group_paths"])
        self.assertFalse((self.notes_dir / "MyNote-test.txt").exists())

    def test_group_with_notes_cannot_be_deleted(self) -> None:
        group = self.server.store.create_group("学习")
        self.server.store.create_note(
            {"term": "grouped", "group": "学习", "body": "body"}
        )
        with self.assertRaises(StoreError):
            self.server.store.delete_group(str(group["id"]))

    def test_backup_export_import_and_validation(self) -> None:
        self.server.store.create_note(
            {"term": "memo", "group": "词汇", "body": "<p>old</p>"}
        )
        exported = self.server.store.export_data()
        result = self.server.store.import_data(exported)
        self.assertEqual(result, {"created": 0, "updated": 1})
        self.assertEqual(self.server.store.lookup("memo")[0]["body"], "<p>old</p>")

        backup = {
            "format": "dict-tango-local-notes",
            "version": 1,
            "groups": ["新分组"],
            "notes": [
                {"term": "valid", "group": "新分组", "body": "uncommitted"},
                {"term": "", "group": "新分组", "body": "invalid"},
            ],
        }
        with self.assertRaises(StoreError):
            self.server.store.import_data(backup)
        self.assertEqual(self.server.store.lookup("valid"), [])
        self.assertNotIn("新分组", [item["name"] for item in self.server.store.list_groups()])

    def test_asset_upload_and_rendering_use_mynote_group_folder(self) -> None:
        payload = b"sample image bytes"
        self.request_json(
            "/api/assets",
            "POST",
            {
                "group": DEFAULT_GROUP,
                "filename": "sample.png",
                "content": base64.b64encode(payload).decode("ascii"),
            },
        )
        self.assertEqual(
            (self.notes_dir / "MyNote-[Default]" / "sample.png").read_bytes(),
            payload,
        )
        self.server.store.create_note(
            {
                "term": "image",
                "group": DEFAULT_GROUP,
                "body": '<img src="sample.png" alt="sample" onerror="bad()">',
            }
        )
        with urlopen(
            self.base_url + "/lookup?" + urlencode({"word": "image"}),
            timeout=3,
        ) as response:
            page = response.read().decode("utf-8")
        self.assertIn('src="/assets/Default/sample.png"', page)
        self.assertNotIn("onerror", page)
        with urlopen(self.base_url + "/assets/Default/sample.png", timeout=3) as response:
            self.assertEqual(response.read(), payload)

    def test_legacy_sqlite_data_is_migrated_once_into_mynote_files(self) -> None:
        legacy_path = self.root / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.executescript(
                """
                CREATE TABLE note_groups (
                    id INTEGER PRIMARY KEY, name TEXT, name_key TEXT,
                    is_default INTEGER, created_at TEXT
                );
                CREATE TABLE notes (
                    id INTEGER PRIMARY KEY, term TEXT, term_key TEXT, group_id INTEGER,
                    body TEXT, created_at TEXT, updated_at TEXT
                );
                INSERT INTO note_groups VALUES (1, '默认', '默认', 1, 'now');
                INSERT INTO note_groups VALUES (2, '学习', '学习', 0, 'now');
                INSERT INTO note_groups VALUES (3, 'default', 'default', 0, 'now');
                INSERT INTO notes VALUES (1, 'apple', 'apple', 1, 'fruit', 'now', 'now');
                INSERT INTO notes VALUES (2, 'book', 'book', 2, 'read', 'now', 'now');
                INSERT INTO notes VALUES (3, 'word', 'word', 3, 'custom group', 'now', 'now');
                """
            )
        finally:
            connection.close()
        target = self.root / "migrated-note"
        migrated = NoteStore(target, legacy_path)
        self.assertEqual(len(migrated.lookup("apple")), 1)
        self.assertEqual(migrated.lookup("book")[0]["group"], "学习")
        self.assertEqual(migrated.lookup("word")[0]["group"], "default")
        self.assertTrue((target / DEFAULT_NOTE_FILE).exists())
        self.assertTrue((target / "MyNote-学习.txt").exists())
        self.assertTrue((target / "MyNote-default.txt").exists())
        marker = target / "config" / "legacy-sqlite-migrated"
        self.assertTrue(marker.exists())

    def test_rejects_cross_origin_write_and_non_local_host(self) -> None:
        payload = json.dumps(
            {"term": "blocked", "group": DEFAULT_GROUP, "body": ""}
        ).encode("utf-8")
        request = Request(
            self.base_url + "/api/notes",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json", "Origin": "http://example.com"},
        )
        with self.assertRaises(HTTPError) as forbidden:
            urlopen(request, timeout=3)
        self.assertEqual(forbidden.exception.code, 403)

        wrong_port = Request(
            self.base_url + "/api/notes",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": "http://127.0.0.1:1",
            },
        )
        with self.assertRaises(HTTPError) as forbidden:
            urlopen(wrong_port, timeout=3)
        self.assertEqual(forbidden.exception.code, 403)

        request = Request(self.base_url + "/health", headers={"Host": "example.com"})
        with self.assertRaises(HTTPError) as forbidden:
            urlopen(request, timeout=3)
        self.assertEqual(forbidden.exception.code, 403)

    def test_management_page_and_head_request(self) -> None:
        request = Request(self.base_url + "/", method="HEAD")
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("Content-Security-Policy", response.headers)
            self.assertGreater(int(response.headers["Content-Length"]), 0)
            self.assertNotIn("X-Test-Mode-Page", response.headers)
        with urlopen(self.base_url + "/", timeout=3) as response:
            page = response.read().decode("utf-8")
        self.assertIn("新建笔记", page)
        self.assertIn('<dialog id="editor-dialog"', page)
        self.assertIn('id="editor-frame"', page)
        self.assertIn('type="file"', page)
        self.assertNotIn("local-debug-panel", page)
        with urlopen(self.base_url + "/styles.css", timeout=3) as response:
            styles = response.read().decode("utf-8")
            self.assertNotIn("X-Test-Mode-Page", response.headers)
        self.assertIn(".editor-dialog { width: min(940px, calc(100vw - 32px));", styles)
        self.assertIn(
            ".editor-dialog[open] { display: flex; flex-direction: column; }",
            styles,
        )
        self.assertIn(".delete-dialog { width: min(420px, calc(100vw - 32px));", styles)
        self.assertIn(".note-content * { min-width: 0 !important; max-width: 100% !important; }", styles)
        self.assertIn(".layout { grid-template-columns: minmax(0, 1fr); gap: 12px; }", styles)
        self.assertIn(".detail-toolbar, .button-row { flex-wrap: wrap; }", styles)
        self.assertIn('id="delete-dialog"', page)
        self.assertIn('id="cancel-delete-button"', page)
        self.assertIn('id="confirm-delete-button"', page)

    def test_test_mode_shows_page_and_request_debug_details(self) -> None:
        self.server.test_mode = True
        request = Request(self.base_url + "/?from=test")
        with urlopen(request, timeout=3) as response:
            page = response.read().decode("utf-8")
            self.assertEqual(
                response.headers["X-Test-Mode-Page"],
                quote("笔记管理首页", safe=""),
            )
            self.assertEqual(
                response.headers["X-Test-Mode-Request"],
                "GET /?from=test",
            )
            self.assertEqual(response.headers["X-Test-Mode-Status"], "200")
        self.assertIn('aria-label="测试模式调试信息"', page)
        self.assertIn("当前页面</dt><dd>笔记管理首页</dd>", page)
        self.assertIn("HTTP 请求</dt><dd>GET /?from=test</dd>", page)
        self.assertIn("响应状态</dt><dd>200</dd>", page)
        self.assertIn(str(self.notes_dir), page)
        self.assertIn(
            "<details><summary>测试模式 · 笔记管理首页</summary>",
            page,
        )
        self.assertNotIn("<details open>", page)
        self.assertIn(
            ".local-debug-panel{position:fixed!important;left:14px!important;right:auto!important;",
            page,
        )
        self.assertIn("top:auto!important;bottom:14px!important;", page)
        self.assertNotIn(".local-debug-panel{position:fixed;right:14px;", page)

        with urlopen(self.base_url + "/lookup?word=debug", timeout=3) as response:
            lookup_page = response.read().decode("utf-8")
        self.assertIn("笔记查询显示页", lookup_page)
        self.assertIn('X-Test-Mode-Page', response.headers)

        with urlopen(self.base_url + "/highlights", timeout=3) as response:
            highlights_page = response.read().decode("utf-8")
            self.assertEqual(
                response.headers["X-Test-Mode-Page"],
                quote("高光管理页", safe=""),
            )
        self.assertIn("测试模式 · 高光管理页", highlights_page)
        with urlopen(self.base_url + "/highlights.js", timeout=3) as response:
            self.assertIn(
                "all_matching: true",
                response.read().decode("utf-8"),
            )

        with urlopen(
            self.base_url + "/editor?" + urlencode({"term": "debug"}),
            timeout=3,
        ) as response:
            editor_page = response.read().decode("utf-8")
        self.assertIn("测试模式 · 笔记编辑页", editor_page)

        note = self.server.store.create_note(
            {"term": "debug delete", "group": DEFAULT_GROUP, "body": "body"}
        )
        with urlopen(
            self.base_url + "/delete-note?" + urlencode({"note_id": str(note["id"])}),
            timeout=3,
        ) as response:
            delete_page = response.read().decode("utf-8")
        self.assertIn("测试模式 · 笔记删除确认页", delete_page)

        missing_note = self.server.store.create_note(
            {"term": "missing debug note", "group": DEFAULT_GROUP, "body": "body"}
        )
        self.assertTrue(self.server.store.delete_note(str(missing_note["id"])))
        with self.assertRaises(HTTPError) as not_found:
            urlopen(
                self.base_url
                + "/editor?"
                + urlencode({"note_id": missing_note["id"]}),
                timeout=3,
            )
        error_page = not_found.exception.read().decode("utf-8")
        self.assertEqual(not_found.exception.code, 404)
        self.assertEqual(
            not_found.exception.headers["X-Test-Mode-Page"],
            quote("错误提示页", safe=""),
        )
        self.assertIn("测试模式 · 错误提示页", error_page)

        with urlopen(self.base_url + "/api/groups", timeout=3) as response:
            self.assertEqual(
                response.headers["X-Test-Mode-Page"],
                quote("笔记 API 接口", safe=""),
            )
            self.assertEqual(response.headers["X-Test-Mode-Status"], "200")

        class StopRedirects(HTTPRedirectHandler):
            def redirect_request(
                self, req, fp, code, msg, headers, newurl
            ):
                return None

        opener = build_opener(StopRedirects())
        with self.assertRaises(HTTPError) as redirect:
            opener.open(self.base_url + "/groups", timeout=3)
        self.assertEqual(redirect.exception.code, 303)
        self.assertEqual(
            redirect.exception.headers["X-Test-Mode-Page"],
            quote("分组兼容重定向", safe=""),
        )
        self.assertEqual(
            redirect.exception.headers["X-Test-Mode-Request"],
            "GET /groups",
        )


class ReleaseBuilderTests(unittest.TestCase):
    def test_release_archive_excludes_user_data_and_has_verifiable_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            program = root / "程序"
            web = root / "网页"
            (program / "note").mkdir(parents=True)
            web.mkdir()
            (program / "app.py").write_text("print('app')", encoding="utf-8")
            (web / "index.html").write_text("<main></main>", encoding="utf-8")
            (program / "settings.json").write_text('{"private": true}', encoding="utf-8")
            (program / "highlight-rules.json").write_text(
                '{"private": true}',
                encoding="utf-8",
            )
            (program / "note" / "MyNote-private.txt").write_text(
                "private note",
                encoding="utf-8",
            )
            (program / "test_app.py").write_text("test data", encoding="utf-8")

            archive_path, manifest_path = build_release_archive(
                root,
                root / "release output",
                "2.3.4",
            )

            self.assertEqual(archive_path.name, "TangoNote-2.3.4.zip")
            self.assertEqual(manifest_path.name, "TangoNote-2.3.4.json")
            with zipfile.ZipFile(archive_path) as archive:
                archived_names = set(archive.namelist())
            self.assertIn("程序/app.py", archived_names)
            self.assertIn("网页/index.html", archived_names)
            self.assertNotIn("程序/settings.json", archived_names)
            self.assertNotIn("程序/highlight-rules.json", archived_names)
            self.assertNotIn("程序/note/MyNote-private.txt", archived_names)
            self.assertNotIn("程序/test_app.py", archived_names)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["version"], "2.3.4")
            self.assertEqual(manifest["platform"], "windows-termux")
            self.assertEqual(
                manifest["sha256"],
                hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            )


class UpdateManagerTests(unittest.TestCase):
    @staticmethod
    def release_payload(version: str = "0.2.0") -> dict[str, object]:
        return {
            "tag_name": f"v{version}",
            "html_url": f"https://github.com/mokusa11/TangoNote/releases/tag/v{version}",
            "body": "Bug fixes",
            "assets": [
                {
                    "name": f"TangoNote-{version}.zip",
                    "browser_download_url": (
                        f"https://github.com/mokusa11/TangoNote/releases/download/"
                        f"v{version}/TangoNote-{version}.zip"
                    ),
                },
                {
                    "name": f"TangoNote-{version}.json",
                    "browser_download_url": (
                        f"https://github.com/mokusa11/TangoNote/releases/download/"
                        f"v{version}/TangoNote-{version}.json"
                    ),
                },
            ],
        }

    def test_check_for_updates_reports_a_new_release(self) -> None:
        payload = self.release_payload()
        with patch(
            "update_manager.urlopen",
            return_value=io.BytesIO(json.dumps(payload).encode("utf-8")),
        ):
            result = check_for_updates()

        self.assertTrue(result["update_available"])
        self.assertEqual(result["current_version"], "0.1.0")
        self.assertEqual(result["latest_version"], "0.2.0")
        self.assertEqual(
            result["release_url"],
            "https://github.com/mokusa11/TangoNote/releases/latest",
        )
        self.assertEqual(result["release_notes"], "Bug fixes")

    def test_check_for_updates_rejects_releases_without_both_assets(self) -> None:
        payload = self.release_payload()
        payload["assets"] = payload["assets"][:1]
        with patch(
            "update_manager.urlopen",
            return_value=io.BytesIO(json.dumps(payload).encode("utf-8")),
        ):
            with self.assertRaisesRegex(UpdateError, "缺少程序 ZIP"):
                check_for_updates()

    def test_termux_update_download_verifies_manifest_and_extracts_program(self) -> None:
        payload = self.release_payload()
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("程序/app.py", "print('updated')\n")
            archive.writestr("网页/index.html", "<main>updated</main>")
        archive_data = archive_bytes.getvalue()
        manifest_data = json.dumps(
            {
                "format": RELEASE_FORMAT,
                "version": "0.2.0",
                "platform": "windows-termux",
                "archive": "TangoNote-0.2.0.zip",
                "sha256": hashlib.sha256(archive_data).hexdigest(),
            }
        ).encode("utf-8")
        responses = [
            io.BytesIO(json.dumps(payload).encode("utf-8")),
            io.BytesIO(manifest_data),
            io.BytesIO(archive_data),
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            staging = Path(temporary_directory)
            with patch("update_manager.urlopen", side_effect=responses):
                result = prepare_termux_update(staging)

            self.assertEqual(result["version"], "0.2.0")
            self.assertEqual(result["file_count"], 2)
            self.assertEqual(
                (staging / "package" / "程序" / "app.py").read_text(
                    encoding="utf-8"
                ),
                "print('updated')\n",
            )

    def test_update_installer_preserves_user_data_and_can_roll_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            program = project / "程序"
            web = project / "网页"
            staging = root / "staging"
            package = staging / "package"
            (program / "note").mkdir(parents=True)
            web.mkdir(parents=True)
            (program / "app.py").write_text("old code", encoding="utf-8")
            (program / "settings.json").write_text("private settings", encoding="utf-8")
            (program / "highlight-rules.json").write_text("private rules", encoding="utf-8")
            (program / "note" / "private.txt").write_text("private note", encoding="utf-8")
            (web / "index.html").write_text("old page", encoding="utf-8")
            (package / "程序").mkdir(parents=True)
            (package / "网页").mkdir(parents=True)
            (package / "程序" / "app.py").write_text("new code", encoding="utf-8")
            (package / "程序" / "new_module.py").write_text("new module", encoding="utf-8")
            (package / "网页" / "index.html").write_text("new page", encoding="utf-8")
            (staging / "staging.json").write_text(
                json.dumps(
                    {
                        "format": RELEASE_FORMAT,
                        "version": "0.2.0",
                        "file_count": 3,
                    }
                ),
                encoding="utf-8",
            )

            installed = install_prepared_update(staging, project)

            self.assertEqual(installed["version"], "0.2.0")
            self.assertEqual((program / "app.py").read_text(encoding="utf-8"), "new code")
            self.assertEqual(
                (program / "settings.json").read_text(encoding="utf-8"),
                "private settings",
            )
            self.assertEqual(
                (program / "highlight-rules.json").read_text(encoding="utf-8"),
                "private rules",
            )
            self.assertEqual(
                (program / "note" / "private.txt").read_text(encoding="utf-8"),
                "private note",
            )
            self.assertEqual((web / "index.html").read_text(encoding="utf-8"), "new page")

            rollback_prepared_update(staging, project)

            self.assertEqual((program / "app.py").read_text(encoding="utf-8"), "old code")
            self.assertFalse((program / "new_module.py").exists())
            self.assertEqual((web / "index.html").read_text(encoding="utf-8"), "old page")

    def test_update_archive_rejects_path_traversal_and_private_files(self) -> None:
        for member_name in ("../outside.py", "程序/settings.json", "程序/note/private.txt"):
            with self.subTest(member=member_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive_path = root / "malicious.zip"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr(member_name, "not allowed")
                with self.assertRaises(UpdateError):
                    _extract_validated_archive(archive_path, root / "stage")


class TermuxUpdateTests(unittest.TestCase):
    def test_update_refuses_to_replace_files_used_by_unmanaged_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_directory = Path(temporary_directory) / "程序"
            app_directory.mkdir()
            with (
                patch("update_termux._service_is_running", return_value=False),
                patch("update_termux._service_is_responding", return_value=True),
                patch("update_termux.prepare_termux_update") as prepare_update,
            ):
                with self.assertRaisesRegex(UpdateError, "无法确认其启动进程"):
                    update_termux.update(app_directory)

            prepare_update.assert_not_called()


class RcloneSyncTests(unittest.TestCase):
    def test_remote_validation_rejects_shell_like_or_malformed_values(self) -> None:
        self.assertEqual(validate_rclone_remote(" onedrive:MyNote "), "onedrive:MyNote")
        for remote in ("--config=x:MyNote", "onedrive", "onedrive:MyNote\nrclone"):
            with self.subTest(remote=remote), self.assertRaises(StoreError):
                validate_rclone_remote(remote)

    def test_sync_downloads_remote_then_uploads_after_local_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            trace: list[str] = []

            def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
                trace.append(
                    f"{command[1]} {command[2]}"
                    + (f" -> {command[3]}" if len(command) > 3 else "")
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch("rclone_sync.shutil.which", return_value="rclone"),
                patch("rclone_sync.subprocess.run", side_effect=run),
            ):
                result = sync_through_rclone(
                    "onedrive:MyNote",
                    Path(temporary_directory),
                    lambda: trace.append("local-sync") or {"uploaded": 1},
                )

        self.assertEqual(result, {"uploaded": 1})
        self.assertEqual(
            trace,
            [
                "mkdir onedrive:MyNote/.dict-tango-sync",
                f"copy onedrive:MyNote/.dict-tango-sync -> "
                f"{Path(temporary_directory) / '.dict-tango-sync'}",
                "local-sync",
                f"copy {Path(temporary_directory) / '.dict-tango-sync'} -> "
                "onedrive:MyNote/.dict-tango-sync",
            ],
        )

    def test_missing_rclone_is_reported_instead_of_running_local_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch("rclone_sync.shutil.which", return_value=None):
                with self.assertRaisesRegex(StoreError, "未安装 rclone"):
                    sync_through_rclone(
                        "onedrive:MyNote",
                        Path(temporary_directory),
                        lambda: self.fail("must not run local-only sync"),
                    )


class UpdateAndTermuxApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.server = LocalNoteServer(
            ("127.0.0.1", 0),
            self.root / "note",
            WEB_DIR,
            settings_path=self.root / "settings.json",
            sync_state_path=self.root / "sync-state.json",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp_dir.cleanup()

    def request_json(
        self,
        path: str,
        method: str = "GET",
        payload: dict[str, object] | None = None,
    ) -> tuple[int, object]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Origin": self.base_url}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_app_info_and_manual_update_check_api(self) -> None:
        with patch(
            "app.check_for_updates",
            return_value={
                "current_version": "0.1.0",
                "latest_version": "0.2.0",
                "update_available": True,
                "release_url": "https://github.com/mokusa11/TangoNote/releases/latest",
                "release_notes": "Bug fixes",
            },
        ):
            status, update = self.request_json("/api/updates/check")
        self.assertEqual(status, 200)
        self.assertTrue(update["update_available"])
        info_status, info = self.request_json("/api/app-info")
        self.assertEqual(info_status, 200)
        self.assertEqual(info["version"], "0.1.0")
        self.assertEqual(info["platform"], "desktop")
        with urlopen(self.base_url + "/", timeout=3) as response:
            page = response.read().decode("utf-8")
        self.assertIn('id="check-updates-button"', page)
        self.assertIn('id="sync-remote-form"', page)

    def test_update_check_network_errors_are_visible_to_the_page(self) -> None:
        with patch("app.check_for_updates", side_effect=UpdateError("GitHub 不可用")):
            with self.assertRaises(HTTPError) as response:
                urlopen(self.base_url + "/api/updates/check", timeout=3)
        self.assertEqual(response.exception.code, 502)
        self.assertIn("GitHub 不可用", response.exception.read().decode("utf-8"))

    def test_termux_rclone_setting_uses_a_private_local_cache(self) -> None:
        cache_directory = self.root / "termux-cache"
        self.server.termux_mode = True
        with patch("app.DEFAULT_TERMUX_SYNC_DIRECTORY", cache_directory):
            status, result = self.request_json(
                "/api/settings/sync-remote",
                "PUT",
                {"sync_remote": "onedrive:MyNote"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(result["sync_remote"], "onedrive:MyNote")
            status, sync_status = self.request_json("/api/sync/status")

        self.assertEqual(status, 200)
        self.assertTrue(sync_status["configured"])
        self.assertEqual(sync_status["sync_remote"], "onedrive:MyNote")
        self.assertTrue(cache_directory.is_dir())
        settings = json.loads((self.root / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["sync_remote"], "onedrive:MyNote")
        self.assertEqual(Path(settings["sync_directory"]), cache_directory.resolve())



if __name__ == "__main__":
    unittest.main()
