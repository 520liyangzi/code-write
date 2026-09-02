from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from io import StringIO
import tempfile
import unittest
from pathlib import Path

from policykit.hooks import _ai_review_evidence, handle_hook, main_hook, prepare_receipt
from policykit.model import PolicyRule
from policykit.review import bundle_fingerprint
from policykit.search import build_sqlite_index


class HookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.rules = self.home / "approved.json"
        self.rules.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "policy_version": "test",
                    "rules": [
                        {
                            "id": "TEST-THREAD",
                            "title": "线程",
                            "statement": "禁止直接创建线程",
                            "status": "approved",
                            "severity": "blocker",
                            "source": {"document": "test.md"},
                            "checker": {
                                "type": "regex_forbid",
                                "pattern": r"\bnew\s+Thread\s*\(",
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.index = self.home / "search-index.db"
        self._rebuild_index()
        self.config = {
            "paths": {
                "approved_rules": str(self.rules),
                "search_index": str(self.index),
                "receipts_dir": str(self.home / "receipts"),
                "audit_dir": str(self.home / "audit"),
            },
            "runtime": {
                "require_receipt": True,
                "fail_closed": True,
                "block_severities": ["blocker"],
            },
        }
        self.target = self.home / "Foo.java"
        self.payload = {
            "session_id": "test-session",
            "cwd": str(self.home),
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.target), "content": "class Foo {}"},
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _rebuild_index(self) -> None:
        payload = json.loads(self.rules.read_text(encoding="utf-8"))
        rules = [PolicyRule.from_dict(item) for item in payload["rules"]]
        version = str(payload["policy_version"])
        bundle_id = bundle_fingerprint(rules, version)
        payload["bundle_id"] = bundle_id
        self.rules.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        build_sqlite_index(
            rules,
            self.index,
            approved_only=True,
            policy_version=version,
            bundle_id=bundle_id,
        )

    def test_first_write_blocks_then_retry_is_authorized(self) -> None:
        first = handle_hook("pre-edit", self.payload, self.config, self.home)
        first_output = first["hookSpecificOutput"]
        self.assertEqual("deny", first_output["permissionDecision"])
        self.assertIn("首写已阻止", first_output["permissionDecisionReason"])

        second = handle_hook("pre-edit", self.payload, self.config, self.home)
        self.assertIn("additionalContext", second["hookSpecificOutput"])

    def test_missing_search_index_is_fail_closed(self) -> None:
        self.index.unlink()
        prepared = prepare_receipt(
            self.target,
            "missing-index-session",
            query="线程",
            config=self.config,
            home=self.home,
            cwd=self.home,
        )
        self.assertTrue(prepared["blocking"])
        self.assertFalse(prepared["receipt_issued"])
        self.assertIn("正式检索索引", prepared["error"])

    def test_mismatched_bundle_id_is_fail_closed(self) -> None:
        payload = json.loads(self.rules.read_text(encoding="utf-8"))
        payload["bundle_id"] = "f" * 64
        self.rules.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        prepared = prepare_receipt(
            self.target,
            "mismatch-index-session",
            query="线程",
            config=self.config,
            home=self.home,
            cwd=self.home,
        )
        self.assertTrue(prepared["blocking"])
        self.assertFalse(prepared["receipt_issued"])
        self.assertIn("bundle_id", prepared["error"])

    def test_modified_rule_content_with_stale_bundle_id_is_fail_closed(self) -> None:
        payload = json.loads(self.rules.read_text(encoding="utf-8"))
        payload["rules"][0]["statement"] = "规则包被部分改写"
        self.rules.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        prepared = prepare_receipt(
            self.target,
            "tampered-bundle-session",
            query="线程",
            config=self.config,
            home=self.home,
            cwd=self.home,
        )
        self.assertTrue(prepared["blocking"])
        self.assertFalse(prepared["receipt_issued"])
        self.assertIn("内容与 bundle_id 不一致", prepared["error"])

    def test_missing_bundle_id_is_fail_closed(self) -> None:
        payload = json.loads(self.rules.read_text(encoding="utf-8"))
        payload.pop("bundle_id")
        self.rules.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        prepared = prepare_receipt(
            self.target,
            "missing-bundle-session",
            config=self.config,
            home=self.home,
            cwd=self.home,
        )
        self.assertTrue(prepared["blocking"])
        self.assertIn("64 位 bundle_id", prepared["error"])

    def test_skill_can_prepare_receipt_and_checker_blocks_bad_code(self) -> None:
        prepared = prepare_receipt(
            self.target,
            "test-session",
            query="创建线程",
            config=self.config,
            home=self.home,
            cwd=self.home,
        )
        self.assertTrue(prepared["receipt_issued"])
        pre = handle_hook("pre-edit", self.payload, self.config, self.home)
        self.assertIn("additionalContext", pre["hookSpecificOutput"])

        self.target.write_text("class Foo { void x(){ new Thread(() -> {}).start(); } }", encoding="utf-8")
        post_payload = dict(self.payload)
        post_payload["tool_response"] = {"success": True}
        post = handle_hook("post-edit", post_payload, self.config, self.home)
        self.assertEqual("block", post["decision"])
        self.assertIn("TEST-THREAD", post["reason"])
        reports = list((self.home / "audit" / "reports").glob("*.md"))
        self.assertTrue(reports)

    def test_hook_context_includes_structured_rule_details(self) -> None:
        self.rules.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "policy_version": "structured-test",
                    "rules": [
                        {
                            "id": "G.EDV.02",
                            "title": "禁止直接使用外部数据构造格式化字符串",
                            "statement": "禁止直接使用外部数据构造格式化字符串",
                            "status": "approved",
                            "severity": "blocker",
                            "source": {"document": "security.md"},
                            "trigger_terms": ["String.format"],
                            "metadata": {
                                "structured_format": True,
                                "level": "要求",
                                "description": "格式模板必须由程序定义。",
                                "negative_example": "String.format(formatFromRequest, value);",
                                "positive_example": "String.format(\"%s\", value);",
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self._rebuild_index()
        prepared = prepare_receipt(
            self.target,
            "structured-context",
            query="格式化输出",
            code="String.format(formatFromRequest, value);",
            config=self.config,
            home=self.home,
            cwd=self.home,
        )
        context = prepared["context"]
        self.assertIn("G.EDV.02", context)
        self.assertIn("【级别：要求】", context)
        self.assertIn("【描述】格式模板必须由程序定义。", context)
        self.assertIn("【反例】String.format(formatFromRequest, value);", context)
        self.assertIn("【正例】String.format(\"%s\", value);", context)

    def test_pre_hook_exception_uses_pretooluse_deny_schema(self) -> None:
        output = StringIO()
        status = main_hook(
            "pre-edit",
            stdin=StringIO("{not-json"),
            stdout=output,
            config=self.config,
            home=self.home,
        )
        self.assertEqual(0, status)
        payload = json.loads(output.getvalue())
        self.assertEqual(
            "deny", payload["hookSpecificOutput"]["permissionDecision"]
        )
        self.assertIn("运行失败", payload["hookSpecificOutput"]["permissionDecisionReason"])

    def test_common_shell_write_is_blocked_but_read_only_command_is_not(self) -> None:
        shell_payload = {
            "session_id": "shell-session",
            "cwd": str(self.home),
            "tool_name": "PowerShell",
            "tool_input": {
                "command": f"Set-Content -LiteralPath '{self.target}' -Value 'class Foo {{}}'"
            },
        }
        blocked = handle_hook("pre-shell", shell_payload, self.config, self.home)
        self.assertEqual(
            "deny", blocked["hookSpecificOutput"]["permissionDecision"]
        )

        shell_payload["tool_input"] = {"command": "mvn -q test"}
        self.assertEqual({}, handle_hook("pre-shell", shell_payload, self.config, self.home))

        shell_payload["tool_input"] = {"command": "rg Foo.java > matches.txt"}
        self.assertEqual({}, handle_hook("pre-shell", shell_payload, self.config, self.home))

        shell_payload["tool_input"] = {
            "command": 'echo x > "C:\\work dir\\Foo.java"'
        }
        quoted = handle_hook("pre-shell", shell_payload, self.config, self.home)
        self.assertEqual("deny", quoted["hookSpecificOutput"]["permissionDecision"])

    def test_ai_only_rule_requires_review_round_trip_and_stays_self_attested(self) -> None:
        self.rules.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "policy_version": "ai-test",
                    "rules": [
                        {
                            "id": "TEST-AI",
                            "title": "异常处理",
                            "statement": "捕获异常后必须按项目规范处理",
                            "status": "approved",
                            "severity": "major",
                            "source": {"document": "test.md"},
                            "metadata": {"checks": [{"type": "ai_review"}]},
                            "trigger_terms": ["catch", "异常"],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self._rebuild_index()
        prepared = prepare_receipt(
            self.target,
            "test-session",
            query="catch 异常",
            config=self.config,
            home=self.home,
            cwd=self.home,
        )
        self.assertTrue(prepared["receipt_issued"])
        handle_hook("pre-edit", self.payload, self.config, self.home)
        self.target.write_text(
            "class Foo { void x(){ try {} catch (Exception e) {} } }",
            encoding="utf-8",
        )
        post_payload = dict(self.payload)
        post_payload["tool_response"] = {"success": True}
        post = handle_hook("post-edit", post_payload, self.config, self.home)
        self.assertIn("additionalContext", post["hookSpecificOutput"])

        stop_payload = {"session_id": "test-session", "cwd": str(self.home)}
        first_stop = handle_hook("stop", stop_payload, self.config, self.home)
        self.assertEqual("block", first_stop["decision"])
        self.assertIn("AI 语义审查", first_stop["reason"])
        stop_payload["last_assistant_message"] = (
            "[TEST-AI] 已逐个检查 catch 块并补齐处理；AI 语义审查通过。"
        )
        self.assertEqual({}, handle_hook("stop", stop_payload, self.config, self.home))

        audit_text = (self.home / "audit" / "sessions" / "test-session.jsonl").read_text(
            encoding="utf-8"
        )
        self.assertIn("ai_review_self_attested", audit_text)

    def test_pre_edit_merges_real_proposed_code_with_proactive_receipt(self) -> None:
        self.rules.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "policy_version": "merge-test",
                    "rules": [
                        {
                            "id": "TEST-MAP",
                            "title": "Map 空值",
                            "statement": "Map.of 的值必须预防空值",
                            "status": "approved",
                            "severity": "major",
                            "source": {"document": "test.md"},
                            "metadata": {"checks": [{"type": "ai_review"}]},
                            "trigger_terms": ["Map.of"],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self._rebuild_index()
        prepared = prepare_receipt(
            self.target,
            "test-session",
            query="重命名局部变量",
            config=self.config,
            home=self.home,
            cwd=self.home,
        )
        self.assertEqual([], prepared["matched_rule_ids"])

        payload = dict(self.payload)
        payload["tool_input"] = {
            "file_path": str(self.target),
            "content": "class Foo { Object x(){ return Map.of(\"k\", value); } }",
        }
        pre = handle_hook("pre-edit", payload, self.config, self.home)
        context = pre["hookSpecificOutput"]["additionalContext"]
        self.assertIn("TEST-MAP", context)

    def test_parallel_receipts_in_one_session_do_not_lose_state(self) -> None:
        targets = [self.home / f"Parallel{index}.java" for index in range(8)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(
                    lambda path: prepare_receipt(
                        path,
                        "parallel-session",
                        query="线程",
                        config=self.config,
                        home=self.home,
                        cwd=self.home,
                    ),
                    targets,
                )
            )
        self.assertTrue(all(item["receipt_issued"] for item in results))
        state = json.loads(
            (self.home / "receipts" / "parallel-session.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(len(targets), len(state["receipts"]))

    def test_ai_evidence_requires_positive_conclusion_for_each_rule(self) -> None:
        valid, _, invalid = _ai_review_evidence(
            {
                "last_assistant_message": (
                    "R1：审查未通过，存在严重问题。\n"
                    "R2：AI 语义审查通过。"
                )
            },
            ["R1", "R2"],
        )
        self.assertFalse(valid)
        self.assertEqual(["R1"], invalid)


if __name__ == "__main__":
    unittest.main()
