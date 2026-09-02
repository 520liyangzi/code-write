from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from policykit.cli import main


class CliEndToEndTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(list(args))
        return result, stdout.getvalue(), stderr.getvalue()

    def test_prepare_review_activate_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            status, _, error = self.run_cli("--home", str(home), "init")
            self.assertEqual(0, status, error)
            source = home / "policy-sources" / "company" / "Java编码规范.md"
            source.write_text(
                "# 集合规范\n\n- 禁止向 `Map.of` 传入可能为空的值。\n",
                encoding="utf-8",
            )

            status, output, error = self.run_cli("--home", str(home), "prepare")
            self.assertEqual(0, status, error)
            self.assertIn("1 条待审阅", output)

            status, output, error = self.run_cli("--home", str(home), "review")
            self.assertEqual(0, status, error)
            self.assertIn("0 条包含可执行 checker 草案", output)
            review = home / ".policy-work" / "REVIEW_ME.md"
            text = review.read_text(encoding="utf-8")
            text = text.replace(
                "- [ ] 接受并启用 <!-- decision:approved -->",
                "- [x] 接受并启用 <!-- decision:approved -->",
                1,
            )
            review.write_text(text, encoding="utf-8")

            status, output, error = self.run_cli("--home", str(home), "activate")
            self.assertEqual(0, status, error)
            self.assertIn("已批准并激活：1", output)
            approved = json.loads(
                (home / ".policy-work" / "approved-rules.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(1, len(approved["rules"]))

            status, output, error = self.run_cli(
                "--home", str(home), "search", "--query", "Map.of 空值"
            )
            self.assertEqual(0, status, error)
            self.assertIn("命中 1 条", output)

    def test_review_rejects_invalid_checker_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.run_cli("--home", str(home), "init")
            source = home / "policy-sources" / "company" / "Java编码规范.md"
            source.write_text("# 线程\n\n- 禁止直接创建线程。\n", encoding="utf-8")
            self.run_cli("--home", str(home), "prepare")
            candidates_path = home / ".policy-work" / "candidates.json"
            candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
            candidates["rules"][0]["metadata"]["checks"] = [
                {"type": "regex_forbid", "pattern": "("}
            ]
            candidates_path.write_text(
                json.dumps(candidates, ensure_ascii=False), encoding="utf-8"
            )
            status, _, error = self.run_cli("--home", str(home), "review")
            self.assertEqual(2, status)
            self.assertIn("正则无效", error)

    def test_hook_cli_accepts_pre_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.run_cli("--home", str(home), "init")
            rules = home / ".policy-work" / "approved-rules.json"
            rules.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "policy_version": "test",
                        "rules": [],
                    }
                ),
                encoding="utf-8",
            )
            original_stdin = sys.stdin
            try:
                sys.stdin = io.StringIO(
                    json.dumps(
                        {
                            "session_id": "cli-shell",
                            "cwd": str(home),
                            "tool_name": "PowerShell",
                            "tool_input": {"command": "mvn -q test"},
                        }
                    )
                )
                status, output, error = self.run_cli(
                    "--home", str(home), "hook", "pre-shell"
                )
            finally:
                sys.stdin = original_stdin
            self.assertEqual(0, status, error)
            self.assertEqual({}, json.loads(output))

    def test_receipt_status_includes_path_applicable_rule_not_ranked_by_bm25(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.run_cli("--home", str(home), "init")
            rules = home / ".policy-work" / "approved-rules.json"
            rules.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "policy_version": "path-test",
                        "rules": [
                            {
                                "id": "PATH-ONLY",
                                "title": "文件位置约定",
                                "statement": "控制层文件遵循专用约定",
                                "status": "approved",
                                "severity": "major",
                                "source": {"document": "project.md"},
                                "metadata": {
                                    "checks": [
                                        {
                                            "type": "ai_review",
                                            "include_paths": ["**/special/**"],
                                        }
                                    ]
                                },
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            status, output, error = self.run_cli(
                "--home",
                str(home),
                "search",
                "--query",
                "完全无关词",
                "--file",
                "src/special/Foo.java",
                "--session",
                "path-session",
                "--receipt",
                "--json",
            )
            self.assertEqual(0, status, error)
            payload = json.loads(output)
            self.assertEqual("matched", payload["status"])
            self.assertEqual(["PATH-ONLY"], payload["receipt"]["matched_rule_ids"])
            self.assertEqual("matched", payload["receipt"]["status"])
            self.assertNotIn("results", payload)
            self.assertNotIn("matched_rules", payload["receipt"])

    def test_receipt_json_has_hard_size_limit_and_no_rule_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.run_cli("--home", str(home), "init")
            rules = home / ".policy-work" / "approved-rules.json"
            rule_values = []
            for index in range(20):
                rule_values.append(
                    {
                        "id": f"LONG-{index:02d}",
                        "title": f"长规则 {index}",
                        "statement": "必须遵循此项目约定。" + ("很长的规范正文" * 120),
                        "status": "approved",
                        "severity": "major",
                        "source": {"document": "project.md"},
                        "metadata": {
                            "checks": [
                                {
                                    "type": "ai_review",
                                    "include_paths": ["**/special/**"],
                                }
                            ]
                        },
                    }
                )
            rules.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "policy_version": "long-test",
                        "rules": rule_values,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            status, output, error = self.run_cli(
                "--home",
                str(home),
                "search",
                "--file",
                "src/special/Foo.java",
                "--session",
                "long-session",
                "--receipt",
                "--json",
            )
            self.assertEqual(0, status, error)
            self.assertLessEqual(len(output.rstrip("\n")), 8000)
            payload = json.loads(output)
            self.assertNotIn("results", payload)
            self.assertNotIn("matched_rules", payload["receipt"])
            self.assertEqual(20, len(payload["receipt"]["matched_rule_ids"]))


if __name__ == "__main__":
    unittest.main()
