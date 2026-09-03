from pathlib import Path
import unittest


UI_ROOT = Path(__file__).resolve().parents[1] / "src" / "policykit" / "ui"


class PolicyStudioUIAssetTests(unittest.TestCase):
    def test_approved_rules_are_hidden_by_default_but_remain_reachable(self) -> None:
        html = (UI_ROOT / "index.html").read_text(encoding="utf-8")
        script = (UI_ROOT / "app.js").read_text(encoding="utf-8")

        self.assertIn(
            '<option value="pending_review" selected>待处理</option>', html
        )
        self.assertIn('id="approvedRulesToggle"', html)
        self.assertIn('<option value="accepted">已批准（含修改后接受）</option>', html)
        self.assertIn('dom.decisionFilter.value === "accepted"', script)
        self.assertIn('["approved", "modified"].includes(draft.decision)', script)
        self.assertGreaterEqual(
            script.count('dom.decisionFilter.value = "pending_review"'), 2
        )
        self.assertIn("待处理规则已全部完成", script)


if __name__ == "__main__":
    unittest.main()
