from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from policykit.config import DEFAULT_CONFIG
from policykit.cli import build_parser
from policykit.review import read_review_decisions
from policykit.studio import (
    MAX_BODY_BYTES,
    MAX_FILE_BYTES,
    PolicyStudio,
    StudioError,
    create_server,
)


class PolicyStudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.static_root = self.home / "static"
        self.static_root.mkdir()
        (self.static_root / "index.html").write_text(
            "<!doctype html><title>Policy Studio Test</title>",
            encoding="utf-8",
        )
        (self.static_root / "app.js").write_text(
            "document.body.dataset.ready = '1';",
            encoding="utf-8",
        )
        self.config = deepcopy(DEFAULT_CONFIG)
        self.studio = PolicyStudio(
            self.home,
            self.config,
            static_root=self.static_root,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def import_rule_document(self, name: str = "Java编码规范.md") -> None:
        result = self.studio.import_documents(
            {
                "scope": "company",
                "files": [
                    {
                        "name": name,
                        "content": (
                            "# 集合规范\n\n"
                            "- 禁止向 `Map.of` 传入可能为空的值。\n"
                        ),
                    }
                ],
            }
        )
        self.assertEqual(1, result["imported_count"])

    def prepare_one_rule(self) -> dict[str, object]:
        self.import_rule_document()
        prepared = self.studio.prepare({})
        self.assertEqual(1, prepared["candidate_count"])
        review = self.studio.review()
        self.assertEqual(1, review["candidate_count"])
        return review["rules"][0]

    def approve_rule(self) -> dict[str, object]:
        rule = self.prepare_one_rule()
        return self.studio.save_decision(
            {
                "rule_id": rule["id"],
                "decision": "approved",
                "review_hash": rule["review_hash"],
                "decision_hash": rule["decision_hash"],
                "edited_statement": "",
                "notes": "Studio 审批测试",
            }
        )

    @contextmanager
    def running_server(self):
        server = create_server(
            self.home,
            self.config,
            port=0,
            static_root=self.static_root,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def request(
        self,
        server,
        method: str,
        path: str,
        payload: object | None = None,
        *,
        studio_header: bool = True,
        content_type: str = "application/json",
    ) -> tuple[int, object, dict[str, str]]:
        host, port = server.server_address[:2]
        connection = HTTPConnection(host, port, timeout=5)
        body = None
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = content_type
            if studio_header:
                headers["X-PolicyKit-Studio"] = "1"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = {key.casefold(): value for key, value in response.getheaders()}
        media_type = response_headers.get("content-type", "")
        value: object
        if media_type.startswith("application/json"):
            value = json.loads(raw.decode("utf-8"))
        else:
            value = raw
        status = response.status
        connection.close()
        return status, value, response_headers

    def test_import_rejects_traversal_absolute_non_markdown_and_overwrite(self) -> None:
        bad_names = (
            "../escape.md",
            "folder/rule.md",
            r"folder\rule.md",
            r"C:\escape.md",
            "/absolute.md",
            "rule.txt",
        )
        for name in bad_names:
            with self.subTest(name=name):
                with self.assertRaises(StudioError) as captured:
                    self.studio.import_documents(
                        {
                            "scope": "company",
                            "files": [{"name": name, "content": "# test"}],
                        }
                    )
                self.assertIn(captured.exception.status, {400, 409})
        self.import_rule_document()
        with self.assertRaises(StudioError) as captured:
            self.studio.import_documents(
                {
                    "scope": "company",
                    "files": [
                        {"name": "Java编码规范.md", "content": "# replacement"}
                    ],
                }
            )
        self.assertEqual(409, captured.exception.status)
        self.assertEqual("document_exists", captured.exception.code)
        source = self.home / "policy-sources" / "company" / "Java编码规范.md"
        self.assertIn("Map.of", source.read_text(encoding="utf-8"))

    def test_import_enforces_scope_size_and_reserved_markers(self) -> None:
        for scope in ("unknown", "", "COMPANY/../project"):
            with self.subTest(scope=scope):
                with self.assertRaises(StudioError) as captured:
                    self.studio.import_documents(
                        {"scope": scope, "files": [{"name": "x.md", "content": "x"}]}
                    )
                self.assertEqual("invalid_scope", captured.exception.code)
        with self.assertRaises(StudioError) as captured:
            self.studio.import_documents(
                {
                    "scope": "company",
                    "files": [{"name": "large.md", "content": "x" * (MAX_FILE_BYTES + 1)}],
                }
            )
        self.assertEqual(413, captured.exception.status)
        with self.assertRaises(StudioError) as captured:
            self.studio.import_documents(
                {
                    "scope": "company",
                    "files": [
                        {
                            "name": "marker.md",
                            "content": "<!-- POLICYKIT-RULE id=bad -->",
                        }
                    ],
                }
            )
        self.assertEqual("reserved_review_marker", captured.exception.code)

    def test_decision_is_atomic_persistent_and_stale_hash_is_rejected(self) -> None:
        rule = self.prepare_one_rule()
        review_path = self.home / ".policy-work" / "REVIEW_ME.md"
        result = self.studio.save_decision(
            {
                "rule_id": rule["id"],
                "decision": "approved",
                "review_hash": rule["review_hash"],
                "decision_hash": rule["decision_hash"],
                "edited_statement": "",
                "notes": "必须保留来源",
            }
        )
        self.assertEqual("approved", result["rule"]["decision"])
        text = review_path.read_text(encoding="utf-8")
        self.assertIn("- [x] 接受并启用", text)
        self.assertIn(f'review_hash="{rule["review_hash"]}"', text)
        self.assertFalse(review_path.with_suffix(".md.tmp").exists())
        decisions = read_review_decisions(review_path)
        self.assertEqual("approved", decisions[0].decision)
        self.assertEqual("必须保留来源", decisions[0].notes)

        restarted = PolicyStudio(
            self.home,
            self.config,
            static_root=self.static_root,
        )
        self.assertEqual("approved", restarted.review()["rules"][0]["decision"])

        before = sha256(review_path.read_bytes()).hexdigest()
        with self.assertRaises(StudioError) as captured:
            restarted.save_decision(
                {
                    "rule_id": rule["id"],
                    "decision": "rejected",
                    "review_hash": "0" * 64,
                    "decision_hash": rule["decision_hash"],
                    "edited_statement": "",
                    "notes": "stale",
                }
            )
        self.assertEqual(409, captured.exception.status)
        self.assertEqual("stale_review", captured.exception.code)
        self.assertEqual(before, sha256(review_path.read_bytes()).hexdigest())

    def test_modified_requires_body_and_round_trips_canonical_review(self) -> None:
        rule = self.prepare_one_rule()
        with self.assertRaises(StudioError) as captured:
            self.studio.save_decision(
                {
                    "rule_id": rule["id"],
                    "decision": "modified",
                    "review_hash": rule["review_hash"],
                    "decision_hash": rule["decision_hash"],
                    "edited_statement": "",
                    "notes": "",
                }
            )
        self.assertEqual("modified_statement_required", captured.exception.code)
        result = self.studio.save_decision(
            {
                "rule_id": rule["id"],
                "decision": "modified",
                "review_hash": rule["review_hash"],
                "decision_hash": rule["decision_hash"],
                "edited_statement": "禁止向 Map.of 传入 null 键或值。",
                "notes": "明确空值范围",
            }
        )
        self.assertEqual("modified", result["rule"]["decision"])
        persisted = self.studio.review()["rules"][0]
        self.assertEqual("禁止向 Map.of 传入 null 键或值。", persisted["edited_statement"])
        self.assertEqual("明确空值范围", persisted["notes"])

    def test_stale_decision_revision_cannot_overwrite_newer_browser_tab(self) -> None:
        stale_view = self.prepare_one_rule()
        saved = self.studio.save_decision(
            {
                "rule_id": stale_view["id"],
                "decision": "approved",
                "review_hash": stale_view["review_hash"],
                "decision_hash": stale_view["decision_hash"],
                "edited_statement": "",
                "notes": "first tab",
            }
        )
        self.assertNotEqual(
            stale_view["decision_hash"],
            saved["rule"]["decision_hash"],
        )
        with self.assertRaises(StudioError) as captured:
            self.studio.save_decision(
                {
                    "rule_id": stale_view["id"],
                    "decision": "rejected",
                    "review_hash": stale_view["review_hash"],
                    "decision_hash": stale_view["decision_hash"],
                    "edited_statement": "",
                    "notes": "stale second tab",
                }
            )
        self.assertEqual(409, captured.exception.status)
        self.assertEqual("stale_decision", captured.exception.code)
        self.assertEqual("approved", self.studio.review()["rules"][0]["decision"])

    def test_prepare_requires_confirmation_before_resetting_decisions(self) -> None:
        self.approve_rule()
        with self.assertRaises(StudioError) as captured:
            self.studio.prepare({})
        self.assertEqual(409, captured.exception.status)
        self.assertEqual("review_decisions_exist", captured.exception.code)
        self.assertEqual(
            {"approved": 1},
            captured.exception.details["decision_counts"],
        )
        prepared = self.studio.prepare({"confirm_reset": True})
        self.assertEqual(1, prepared["reset_decision_count"])
        self.assertEqual("pending_review", self.studio.review()["rules"][0]["decision"])

    def test_prepare_protects_pending_review_notes(self) -> None:
        rule = self.prepare_one_rule()
        self.studio.save_decision(
            {
                "rule_id": rule["id"],
                "decision": "pending_review",
                "review_hash": rule["review_hash"],
                "decision_hash": rule["decision_hash"],
                "edited_statement": "",
                "notes": "需要部门确认适用边界",
            }
        )
        with self.assertRaises(StudioError) as captured:
            self.studio.prepare({})
        self.assertEqual("review_decisions_exist", captured.exception.code)
        self.assertEqual(
            {"pending_review": 1},
            captured.exception.details["decision_counts"],
        )

    def test_activate_and_search_use_sqlite_index_only(self) -> None:
        self.approve_rule()
        activated = self.studio.activate({"policy_version": "studio-test-v1"})
        self.assertEqual(1, activated["counts"]["approved"])
        self.assertTrue((self.home / ".policy-work" / "approved-rules.json").is_file())
        index_path = self.home / ".policy-work" / "search-index.db"
        self.assertTrue(index_path.is_file())
        self.assertTrue((self.home / ".policy-work" / "GLOBAL_MD_BLOCK.md").is_file())

        result = self.studio.search(
            {
                "query": "Map.of 空值",
                "file": "src/main/java/demo/Example.java",
                "code": "return Map.of(\"key\", value);",
                "limit": 10,
            }
        )
        self.assertEqual("sqlite", result["index_backend"])
        self.assertEqual(str(index_path.resolve()), result["index_path"])
        self.assertEqual("studio-test-v1", result["policy_version"])
        self.assertEqual(1, result["result_count"])
        self.assertTrue(result["results"][0]["reasons"])

        index_path.unlink()
        with self.assertRaises(StudioError) as captured:
            self.studio.search({"query": "Map.of", "file": "", "code": "", "limit": 10})
        self.assertEqual("search_index_missing", captured.exception.code)

    def test_partial_activation_bundle_mismatch_fails_closed(self) -> None:
        self.approve_rule()
        self.studio.activate({"policy_version": "stable-v1"})
        with patch(
            "policykit.studio.build_sqlite_index",
            side_effect=OSError("simulated index failure"),
        ):
            with self.assertRaises(OSError):
                self.studio.activate({"policy_version": "broken-v2"})

        status = self.studio.status()
        self.assertEqual("broken-v2", status["policy_version"])
        self.assertFalse(status["index_ready"])
        self.assertFalse(status["activated"])
        self.assertIn("policy_version", status["index_error"])
        with self.assertRaises(StudioError) as captured:
            self.studio.search(
                {"query": "Map.of", "file": "Foo.java", "code": "", "limit": 10}
            )
        self.assertEqual("search_index_invalid", captured.exception.code)

    def test_modified_approved_json_content_fails_bundle_validation(self) -> None:
        self.approve_rule()
        self.studio.activate({"policy_version": "tamper-test-v1"})
        approved_path = self.home / ".policy-work" / "approved-rules.json"
        payload = json.loads(approved_path.read_text(encoding="utf-8"))
        payload["rules"][0]["statement"] = "被手工改写但未重新激活"
        approved_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

        status = self.studio.status()
        self.assertFalse(status["activated"])
        self.assertIn("bundle_id", status["index_error"])
        with self.assertRaises(StudioError) as captured:
            self.studio.search(
                {"query": "Map.of", "file": "Foo.java", "code": "", "limit": 10}
            )
        self.assertEqual("search_bundle_invalid", captured.exception.code)

    def test_activate_requires_at_least_one_approved_rule(self) -> None:
        self.prepare_one_rule()
        with self.assertRaises(StudioError) as captured:
            self.studio.activate({})
        self.assertEqual(400, captured.exception.status)
        self.assertEqual("no_approved_rules", captured.exception.code)

    def test_status_separates_current_review_from_previous_active_bundle(self) -> None:
        self.approve_rule()
        self.studio.activate({"policy_version": "active-before-reset"})
        before = self.studio.status()
        self.assertEqual(1, before["approved_count"])
        self.assertEqual(1, before["active_rule_count"])
        self.assertTrue(before["activated"])

        self.studio.prepare({"confirm_reset": True})
        after = self.studio.status()
        self.assertEqual(0, after["approved_count"])
        self.assertEqual(1, after["active_rule_count"])
        self.assertEqual("active-before-reset", after["policy_version"])
        self.assertTrue(after["activated"])

    def test_http_server_security_static_traversal_and_workflow(self) -> None:
        with self.running_server() as server:
            self.assertEqual("127.0.0.1", server.server_address[0])
            status, body, headers = self.request(server, "GET", "/")
            self.assertEqual(200, status)
            self.assertIn(b"Policy Studio Test", body)
            self.assertEqual("nosniff", headers["x-content-type-options"])
            self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
            self.assertNotIn("access-control-allow-origin", headers)

            host, port = server.server_address[:2]
            connection = HTTPConnection(host, port, timeout=5)
            connection.putrequest("GET", "/", skip_host=True)
            connection.putheader("Host", "attacker.example")
            connection.endheaders()
            response = connection.getresponse()
            hostile_body = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(403, response.status)
            self.assertEqual("host_not_allowed", hostile_body["error"]["code"])

            status, body, _ = self.request(server, "GET", "/%2e%2e/policykit.json")
            self.assertEqual(404, status)
            self.assertFalse(body["ok"])
            status, body, _ = self.request(server, "GET", "/..%5cpolicykit.json")
            self.assertEqual(404, status)
            self.assertFalse(body["ok"])

            status, body, _ = self.request(server, "GET", "/api/status")
            self.assertEqual(200, status)
            self.assertTrue(body["ok"])
            self.assertEqual(0, body["documents_count"])
            status, body, _ = self.request(server, "GET", "/api/review/raw")
            self.assertEqual(200, status)
            self.assertFalse(body["exists"])
            self.assertEqual("", body["content"])

            import_payload = {
                "scope": "company",
                "files": [
                    {
                        "name": "server.md",
                        "content": "# 异常规范\n\n- 捕获异常后必须记录日志。\n",
                    }
                ],
            }
            status, body, _ = self.request(
                server,
                "POST",
                "/api/documents/import",
                import_payload,
                studio_header=False,
            )
            self.assertEqual(403, status)
            self.assertEqual("studio_header_required", body["error"]["code"])
            status, body, _ = self.request(
                server,
                "POST",
                "/api/documents/import",
                import_payload,
                content_type="text/plain",
            )
            self.assertEqual(415, status)
            self.assertEqual("unsupported_media_type", body["error"]["code"])

            host, port = server.server_address[:2]
            connection = HTTPConnection(host, port, timeout=5)
            connection.putrequest("POST", "/api/prepare")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("X-PolicyKit-Studio", "1")
            connection.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
            connection.endheaders()
            response = connection.getresponse()
            oversized_body = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(413, response.status)
            self.assertEqual("body_too_large", oversized_body["error"]["code"])

            status, body, headers = self.request(
                server, "POST", "/api/documents/import", import_payload
            )
            self.assertEqual(200, status)
            self.assertEqual(1, body["imported_count"])
            self.assertEqual("no-store", headers["cache-control"])
            self.assertNotIn("access-control-allow-origin", headers)
            status, body, _ = self.request(server, "POST", "/api/prepare", {})
            self.assertEqual(200, status)
            self.assertEqual(1, body["candidate_count"])
            status, body, _ = self.request(server, "GET", "/api/review")
            self.assertEqual(200, status)
            rule = body["rules"][0]
            status, raw_body, _ = self.request(server, "GET", "/api/review/raw")
            self.assertEqual(200, status)
            self.assertIn("POLICYKIT-RULE", raw_body["content"])
            status, body, _ = self.request(
                server,
                "POST",
                "/api/review/decision",
                {
                    "rule_id": rule["id"],
                    "decision": "approved",
                    "review_hash": rule["review_hash"],
                    "decision_hash": rule["decision_hash"],
                    "edited_statement": "",
                    "notes": "HTTP 审批",
                },
            )
            self.assertEqual(200, status)
            self.assertEqual("approved", body["rule"]["decision"])
            status, body, _ = self.request(
                server,
                "POST",
                "/api/activate",
                {"policy_version": "http-v1"},
            )
            self.assertEqual(200, status)
            self.assertEqual(1, body["counts"]["approved"])
            status, body, _ = self.request(
                server,
                "POST",
                "/api/search",
                {
                    "query": "捕获异常 日志",
                    "file": "src/main/java/demo/Foo.java",
                    "code": "catch (Exception e) {}",
                    "limit": 5,
                },
            )
            self.assertEqual(200, status)
            self.assertEqual("sqlite", body["index_backend"])
            self.assertEqual("http-v1", body["policy_version"])
            self.assertEqual(1, body["result_count"])

    def test_ui_cli_defaults_to_loopback_and_browser_open(self) -> None:
        args = build_parser().parse_args(["ui"])
        self.assertEqual("127.0.0.1", args.host)
        self.assertEqual(8765, args.port)
        self.assertFalse(args.no_open)
        with self.assertRaisesRegex(ValueError, "回环地址"):
            create_server(
                self.home,
                self.config,
                host="0.0.0.0",
                port=0,
                static_root=self.static_root,
            )


if __name__ == "__main__":
    unittest.main()
