from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from policykit.extractor import extract_markdown
from policykit.review import (
    ReviewFormatError,
    apply_review_decisions,
    parse_review_decisions,
    render_review,
)
from policykit.search import PolicySearchIndex, SQLitePolicyIndex, build_sqlite_index


SAMPLE = """# Java 编码规范

## 集合

- 禁止向 `Map.of` 传入可能为空的 key 或 value。
- 建议普通局部变量使用有意义的名称。

## 异常

捕获异常后必须进行处理，不得静默吞掉异常。

下面只是背景说明，不是强制规则。
"""


class PolicyCoreTests(unittest.TestCase):
    def test_extract_review_activate_and_search(self) -> None:
        rules = extract_markdown(SAMPLE, "Java编码规范.md", scope="company")
        self.assertEqual(3, len(rules))
        self.assertTrue(all(rule.status == "pending_review" for rule in rules))
        self.assertTrue(all(rule.source.document == "Java编码规范.md" for rule in rules))

        review = render_review(rules)
        review = review.replace(
            "- [ ] 接受并启用 <!-- decision:approved -->",
            "- [x] 接受并启用 <!-- decision:approved -->",
            1,
        )
        decisions = parse_review_decisions(review)
        reviewed = apply_review_decisions(rules, decisions)
        approved = [rule for rule in reviewed if rule.status == "approved"]
        self.assertEqual(1, len(approved))

        index = PolicySearchIndex(approved)
        results = index.search(code="Map.of(userId, nullableName)", file_path="Foo.java")
        self.assertTrue(results)
        self.assertEqual(approved[0].id, results[0].rule_id)

    def test_sqlite_round_trip(self) -> None:
        rule = extract_markdown(SAMPLE, "Java编码规范.md", scope="company")[0]
        rule.status = "approved"
        with tempfile.TemporaryDirectory() as directory:
            path = build_sqlite_index([rule], Path(directory) / "rules.db")
            results = SQLitePolicyIndex(path).search(query="Map.of 空值")
            self.assertTrue(results)
            self.assertEqual(rule.id, results[0].rule_id)

    def test_review_distinguishes_hint_from_executable_checker(self) -> None:
        rule = extract_markdown(SAMPLE, "Java编码规范.md", scope="company")[0]
        without_checker = render_review([rule])
        self.assertIn("只会进入按需检索与 AI review", without_checker)
        self.assertIn("执行候选` 只是分类提示", without_checker)

        rule.metadata["checks"] = [
            {
                "type": "regex_forbid",
                "pattern": r"new\s+Thread\s*\(",
                "include_paths": ["**/*.java"],
            }
        ]
        with_checker = render_review([rule])
        self.assertIn('"type": "regex_forbid"', with_checker)
        self.assertIn('"include_paths"', with_checker)

    def test_review_hash_rejects_candidate_changed_after_approval(self) -> None:
        rule = extract_markdown(SAMPLE, "Java编码规范.md", scope="company")[0]
        review = render_review([rule]).replace(
            "- [ ] 接受并启用 <!-- decision:approved -->",
            "- [x] 接受并启用 <!-- decision:approved -->",
            1,
        )
        decisions = parse_review_decisions(review)
        rule.metadata["checks"] = [
            {"type": "regex_forbid", "pattern": "new Thread"}
        ]
        with self.assertRaises(ReviewFormatError):
            apply_review_decisions([rule], decisions, strict=True)

        fresh_rule = extract_markdown(SAMPLE, "新规范.md", scope="company")[0]
        fresh_review = render_review([fresh_rule])
        fresh_decisions = parse_review_decisions(fresh_review)
        unreviewed = extract_markdown(SAMPLE, "另一份规范.md", scope="department")[1]
        unreviewed.status = "approved"
        with self.assertRaisesRegex(ReviewFormatError, "没有对应审阅块"):
            apply_review_decisions(
                [fresh_rule, unreviewed], fresh_decisions, strict=True
            )


if __name__ == "__main__":
    unittest.main()
