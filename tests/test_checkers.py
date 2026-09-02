from __future__ import annotations

import unittest

from policykit.checkers import PolicyChecker, validate_checker_rules


def rule(rule_id: str, checker: dict, *, severity: str = "blocker") -> dict:
    return {
        "id": rule_id,
        "title": rule_id,
        "statement": "测试规则",
        "status": "approved",
        "severity": severity,
        "source": {"document": "test.md", "section": "tests"},
        "checker": checker,
    }


class CheckerTests(unittest.TestCase):
    def test_regex_forbid(self) -> None:
        checker = PolicyChecker(
            [
                rule(
                    "TEST-THREAD",
                    {
                        "type": "regex_forbid",
                        "pattern": r"\bnew\s+Thread\s*\(",
                        "message": "禁止直接创建线程",
                    },
                )
            ]
        )
        results = checker.check_file("src/Foo.java", "new Thread(task).start();")
        self.assertTrue(any(result.status == "fail" for result in results))
        self.assertTrue(any(result.blocking for result in results))

    def test_path_and_companion_change(self) -> None:
        checker = PolicyChecker(
            [
                rule(
                    "TEST-PATH",
                    {
                        "type": "path_allow",
                        "allowed_paths": ["**/web/**"],
                        "when_pattern": "@RestController",
                    },
                ),
                rule(
                    "TEST-COMPANION",
                    {
                        "type": "companion_change",
                        "trigger_paths": ["**/*Mapper.java"],
                        "required_paths": ["**/*Mapper.xml"],
                    },
                ),
            ]
        )
        path_results = checker.check_file(
            "src/main/java/internal/FooController.java", "@RestController class Foo {}"
        )
        self.assertTrue(any(result.status == "fail" for result in path_results))
        change_results = checker.check_change_set(
            ["src/main/java/FooMapper.java"], include_file_checks=False
        )
        self.assertTrue(any(result.status == "fail" for result in change_results))

    def test_checker_validation_rejects_nested_repeat_regex(self) -> None:
        errors = validate_checker_rules(
            [
                rule(
                    "TEST-REDOS",
                    {"type": "regex_forbid", "pattern": r"(a+)+$"},
                )
            ]
        )
        self.assertTrue(any("嵌套重复量词" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
