from __future__ import annotations

import unittest

from policykit.audit import render_markdown_report, summarize_events


def result(rule_id: str, status: str, checker: str = "regex_forbid") -> dict:
    return {
        "rule_id": rule_id,
        "path": "src/Foo.java",
        "checker": checker,
        "status": status,
        "severity": "major",
        "message": f"{rule_id}-{status}",
        "blocking": status == "fail",
    }


class AuditTests(unittest.TestCase):
    def test_summary_distinguishes_resolved_unresolved_and_ai_evidence(self) -> None:
        events = [
            {"event": "post_write_check", "results": [result("R1", "fail")]},
            {"event": "post_write_check", "results": [result("R1", "pass")]},
            {"event": "post_write_check", "results": [result("R2", "fail")]},
            {
                "event": "post_write_check",
                "results": [result("AI1", "review", "ai_review")],
            },
            {
                "event": "ai_review_self_attested",
                "matched_rule_ids": ["AI1"],
            },
        ]
        summary = summarize_events(events)
        self.assertEqual(["R1"], [item["rule_id"] for item in summary["resolved_issues"]])
        self.assertEqual(
            ["R2"], [item["rule_id"] for item in summary["unresolved_issues"]]
        )
        self.assertEqual([], summary["pending_ai_reviews"])
        self.assertEqual(["AI1"], summary["ai_self_attested_rule_ids"])

        report = render_markdown_report("test", events, generated_at="now")
        self.assertIn("最终未解决", report)
        self.assertIn("已修复的历史问题", report)
        self.assertIn("AI 自述已审查", report)

    def test_later_ai_review_becomes_pending_again(self) -> None:
        review = result("AI1", "review", "ai_review")
        events = [
            {"event": "post_write_check", "results": [review]},
            {"event": "ai_review_self_attested", "matched_rule_ids": ["AI1"]},
            {"event": "post_write_check", "results": [review]},
        ]
        summary = summarize_events(events)
        self.assertEqual(
            ["AI1"], [item["rule_id"] for item in summary["pending_ai_reviews"]]
        )


if __name__ == "__main__":
    unittest.main()
