from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from policykit.checkers import PolicyChecker
from policykit.extractor import extract_markdown
from policykit.model import PolicyRule, SourceRef
from policykit.review import (
    ReviewFormatError,
    apply_review_decisions,
    bundle_fingerprint,
    parse_review_decisions,
    render_review,
)
from policykit.search import (
    PolicyIndexMetadataError,
    PolicySearchIndex,
    SQLitePolicyIndex,
    build_sqlite_index,
    retrieve_runtime_rules,
)


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

    def test_sqlite_and_memory_use_identical_full_corpus_scores(self) -> None:
        rules = [
            PolicyRule(
                id="MAP-NULL",
                title="Map 空值",
                statement="Map.of 的 key 和 value 不得为空",
                source=SourceRef("company.md", "集合"),
                trigger_terms=("Map.of",),
                status="approved",
            ),
            PolicyRule(
                id="MAP-COPY",
                title="不可变 Map",
                statement="Map.of 创建的集合不可修改",
                source=SourceRef("department.md", "集合"),
                status="approved",
            ),
            PolicyRule(
                id="THREAD-POOL",
                title="线程池",
                statement="异步任务必须使用统一线程池",
                source=SourceRef("performance.md", "并发"),
                status="approved",
            ),
        ]
        version = "score-v1"
        bundle_id = bundle_fingerprint(rules, version)
        memory = PolicySearchIndex(
            rules,
            policy_version=version,
            bundle_id=bundle_id,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = build_sqlite_index(
                rules,
                Path(directory) / "rules.db",
                policy_version=version,
                bundle_id=bundle_id,
            )
            sqlite_index = SQLitePolicyIndex(path)
            metadata = sqlite_index.read_metadata()
            self.assertEqual(version, metadata["policy_version"])
            self.assertEqual(bundle_id, metadata["bundle_id"])
            sqlite_index.validate_metadata(
                expected_policy_version=version,
                expected_bundle_id=bundle_id,
            )
            with self.assertRaises(PolicyIndexMetadataError):
                sqlite_index.validate_metadata(expected_policy_version="stale")

            expected = memory.search(
                query="Map.of 集合",
                file_path="src/main/java/Foo.java",
                code="return Map.of(\"key\", value);",
            )
            actual = sqlite_index.search(
                query="Map.of 集合",
                file_path="src/main/java/Foo.java",
                code="return Map.of(\"key\", value);",
                expected_policy_version=version,
                expected_bundle_id=bundle_id,
            )
            self.assertEqual(
                [(item.rule_id, item.score, item.reasons) for item in expected],
                [(item.rule_id, item.score, item.reasons) for item in actual],
            )

    def test_runtime_retrieval_never_drops_direct_path_rule(self) -> None:
        ranked_rule = PolicyRule(
            id="RANKED-MAP",
            title="Map 空值",
            statement="Map.of 的 value 必须预防空值",
            source=SourceRef("coding.md", "集合", 10, 12),
            scope="company",
            trigger_terms=("Map.of",),
            status="approved",
        )
        path_rule = PolicyRule(
            id="SERVICE-PATH",
            title="服务文件落位",
            statement="Service 实现必须放在 service 目录",
            source=SourceRef("project.md", "目录", 21, 23),
            scope="project",
            status="approved",
            metadata={
                "checks": [
                    {
                        "type": "ai_review",
                        "include_paths": ["**/service/**/*.java"],
                    }
                ]
            },
        )
        rules = [ranked_rule, path_rule]
        cards = retrieve_runtime_rules(
            PolicySearchIndex(rules),
            PolicyChecker(rules),
            query="Map.of 空值",
            file_path="src/main/java/com/acme/service/order/OrderService.java",
            limit=1,
        )
        by_id = {card["id"]: card for card in cards}
        self.assertIn("SERVICE-PATH", by_id)
        self.assertGreater(len(cards), 1)  # Direct rules take precedence over limit.
        self.assertTrue(by_id["SERVICE-PATH"]["applicable"])
        self.assertEqual(
            21, by_id["SERVICE-PATH"]["rule"]["source"]["line_start"]
        )
        company_only = retrieve_runtime_rules(
            PolicySearchIndex(rules),
            PolicyChecker(rules),
            query="Map.of 空值",
            file_path="src/main/java/com/acme/service/order/OrderService.java",
            scopes=["company"],
            limit=10,
        )
        self.assertEqual(["RANKED-MAP"], [card["id"] for card in company_only])

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
