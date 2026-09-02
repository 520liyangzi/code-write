"""Human review workflow for extracted policy candidates."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from .model import PolicyRule, ReviewDecision, SCHEMA_VERSION


REVIEW_FORMAT_VERSION = 1

_RULE_BLOCK_RE = re.compile(
    r'<!--\s*POLICYKIT-RULE\s+id="([^"]+)"'
    r'(?:\s+review_hash="([a-fA-F0-9]{64})")?\s*-->(.*?)'
    r"<!--\s*/POLICYKIT-RULE\s*-->",
    re.DOTALL | re.IGNORECASE,
)
_CHECKED_DECISION_RE = re.compile(
    r"^\s*-\s*\[[xX✓√]\].*?<!--\s*decision:([a-z_]+)\s*-->",
    re.MULTILINE | re.IGNORECASE,
)
_EDITED_RE = re.compile(
    r"<!--\s*POLICYKIT-EDITED:start\s*-->(.*?)"
    r"<!--\s*POLICYKIT-EDITED:end\s*-->",
    re.DOTALL | re.IGNORECASE,
)
_NOTES_RE = re.compile(
    r"<!--\s*POLICYKIT-NOTES:start\s*-->(.*?)"
    r"<!--\s*POLICYKIT-NOTES:end\s*-->",
    re.DOTALL | re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(r"^\s*<!--.*?-->\s*$", re.DOTALL)


class ReviewFormatError(ValueError):
    """Raised when review choices are ambiguous or malformed."""


def _blockquote(value: str) -> str:
    if not value:
        return "> （原文为空）"
    return "\n".join("> " + line if line else ">" for line in value.splitlines())


def _clean_editable_block(value: str) -> str:
    value = value.strip()
    if _PLACEHOLDER_RE.match(value):
        return ""
    return value


def _checker_draft(rule: PolicyRule) -> list[str]:
    """Render executable checker metadata without implying that a hint is a check."""

    metadata = rule.metadata or {}
    configured = next(
        (
            metadata[key]
            for key in ("checks", "checkers", "check", "checker", "enforcement")
            if key in metadata
        ),
        None,
    )
    if configured is None:
        return [
            "未生成可执行 checker 配置；若批准，本规则只会进入按需检索与 AI review。",
            "`执行候选` 只是分类提示，不等于已经实现检查器。",
        ]
    return [
        "以下是将随规则一同激活的 checker 草案；请连同规则正文一起审阅：",
        "",
        "```json",
        json.dumps(configured, ensure_ascii=False, indent=2),
        "```",
    ]


def review_fingerprint(rule: PolicyRule) -> str:
    """Bind a review decision to the exact candidate and checker draft shown."""

    payload = rule.to_dict()
    payload.pop("status", None)
    payload.pop("reviewer_notes", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def render_review(rules: Iterable[PolicyRule]) -> str:
    """Render a stable, checkbox-based ``REVIEW_ME.md`` document."""

    sorted_rules = sorted(
        rules,
        key=lambda rule: (
            rule.source.document.casefold(),
            rule.source.line_start,
            rule.id,
        ),
    )
    lines = [
        "# Java 规范候选规则审阅",
        "",
        f"<!-- policykit-review-format: {REVIEW_FORMAT_VERSION} -->",
        "",
        "> 本文件中的内容都是自动提取的候选规则，默认不会生效。",
        "> 每条规则最多勾选一个决定；只有“接受”或填写后的“修改后接受”会进入正式规则库。",
        "",
        f"候选规则数量：**{len(sorted_rules)}**",
        "",
    ]

    for rule in sorted_rules:
        location = rule.source.document
        if rule.source.section:
            location += f" / {rule.source.section}"
        if rule.source.line_start:
            location += f" / 第 {rule.source.line_start}"
            if rule.source.line_end > rule.source.line_start:
                location += f"-{rule.source.line_end}"
            location += " 行"

        lines.extend(
            [
                f'<!-- POLICYKIT-RULE id="{rule.id}" review_hash="{review_fingerprint(rule)}" -->',
                f"## {rule.id} — {rule.title}",
                "",
                f"- 来源：`{location}`",
                f"- 范围建议：`{rule.scope}`",
                f"- 分类建议：`{rule.category}`",
                f"- 严重度建议：`{rule.severity}`",
                f"- 执行候选：`{', '.join(rule.enforcement_candidates)}`",
                f"- 抽取置信度：`{rule.confidence:.2f}`",
                "",
                "### 可执行检查器草案",
                "",
                *_checker_draft(rule),
                "",
                "### 提取后的规则",
                "",
                rule.statement,
                "",
                "### 文档原文",
                "",
                _blockquote(rule.source.quote or rule.statement),
                "",
                "### 审阅决定（只能勾选一个）",
                "",
                "- [ ] 接受并启用 <!-- decision:approved -->",
                "- [ ] 修改后接受 <!-- decision:modified -->",
                "- [ ] 拒绝 <!-- decision:rejected -->",
                "- [ ] 暂不处理 <!-- decision:pending_review -->",
                "",
                "### 修改后的规则正文",
                "",
                "仅在勾选“修改后接受”时填写；请保留下面两个标记。",
                "",
                "<!-- POLICYKIT-EDITED:start -->",
                "<!-- 在这里填写修改后的完整规则正文 -->",
                "<!-- POLICYKIT-EDITED:end -->",
                "",
                "### 审阅备注（可选）",
                "",
                "<!-- POLICYKIT-NOTES:start -->",
                "<!-- 可在这里填写原因、适用条件或后续事项 -->",
                "<!-- POLICYKIT-NOTES:end -->",
                "",
                "<!-- /POLICYKIT-RULE -->",
                "",
                "---",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def write_review(
    rules: Iterable[PolicyRule], output_path: str | Path = "REVIEW_ME.md"
) -> Path:
    """Write the review document and return its resolved path."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_review(rules), encoding="utf-8")
    return path.resolve()


def parse_review_decisions(
    text: str, *, strict: bool = True
) -> list[ReviewDecision]:
    """Parse checked decisions from a rendered review document.

    With ``strict=True`` (the default), multiple checked choices and a
    ``modified`` decision without replacement text raise
    :class:`ReviewFormatError` instead of silently activating a rule.
    """

    decisions: list[ReviewDecision] = []
    seen: set[str] = set()
    for match in _RULE_BLOCK_RE.finditer(text):
        rule_id = match.group(1).strip()
        review_hash = (match.group(2) or "").strip().lower()
        block = match.group(3)
        if rule_id in seen:
            raise ReviewFormatError(f"审阅文件中规则 ID 重复: {rule_id}")
        seen.add(rule_id)

        checked = [item.lower() for item in _CHECKED_DECISION_RE.findall(block)]
        if len(checked) > 1:
            if strict:
                raise ReviewFormatError(
                    f"规则 {rule_id} 勾选了多个决定: {', '.join(checked)}"
                )
            decision = "needs_edit"
        elif checked:
            decision = checked[0]
        else:
            decision = "pending_review"

        edited_match = _EDITED_RE.search(block)
        notes_match = _NOTES_RE.search(block)
        edited = _clean_editable_block(edited_match.group(1)) if edited_match else ""
        notes = _clean_editable_block(notes_match.group(1)) if notes_match else ""
        if decision == "modified" and not edited:
            if strict:
                raise ReviewFormatError(
                    f"规则 {rule_id} 选择了“修改后接受”，但没有填写修改后的规则正文"
                )
            decision = "needs_edit"

        decisions.append(
            ReviewDecision(
                rule_id=rule_id,
                decision=decision,
                review_hash=review_hash,
                edited_statement=edited,
                notes=notes,
            )
        )
    if strict and not decisions and "POLICYKIT-RULE" in text:
        raise ReviewFormatError("无法解析审阅文件中的规则块")
    return decisions


def read_review_decisions(
    path: str | Path, *, strict: bool = True
) -> list[ReviewDecision]:
    return parse_review_decisions(Path(path).read_text(encoding="utf-8-sig"), strict=strict)


def apply_review_decisions(
    rules: Iterable[PolicyRule],
    decisions: Iterable[ReviewDecision] | Mapping[str, ReviewDecision],
    *,
    strict: bool = True,
) -> list[PolicyRule]:
    """Apply review decisions without mutating the extracted rule objects."""

    if isinstance(decisions, Mapping):
        decision_map = dict(decisions)
    else:
        decision_map: dict[str, ReviewDecision] = {}
        for decision in decisions:
            if decision.rule_id in decision_map:
                raise ReviewFormatError(f"规则决定重复: {decision.rule_id}")
            decision_map[decision.rule_id] = decision

    output: list[PolicyRule] = []
    known_ids: set[str] = set()
    for rule in rules:
        if rule.id in known_ids:
            raise ReviewFormatError(f"候选规则 ID 重复: {rule.id}")
        known_ids.add(rule.id)
        decision = decision_map.get(rule.id)
        if decision is None:
            if strict:
                raise ReviewFormatError(
                    f"规则 {rule.id} 没有对应审阅块；请重新生成 REVIEW_ME.md"
                )
            output.append(replace(rule, status="pending_review"))
            continue

        expected_hash = review_fingerprint(rule)
        if strict and not decision.review_hash:
            raise ReviewFormatError(
                f"规则 {rule.id} 的审阅块缺少 review_hash；请重新生成 REVIEW_ME.md"
            )
        if strict and decision.review_hash != expected_hash:
            raise ReviewFormatError(
                f"规则 {rule.id} 在审阅后已发生变化；请重新运行 review 并重新勾选"
            )

        metadata = dict(rule.metadata)
        metadata["review_decision"] = decision.decision
        if decision.decision == "modified":
            metadata["statement_before_review"] = rule.statement
            output.append(
                replace(
                    rule,
                    statement=decision.edited_statement,
                    status="approved",
                    reviewer_notes=decision.notes,
                    metadata=metadata,
                )
            )
        elif decision.decision == "approved":
            output.append(
                replace(
                    rule,
                    status="approved",
                    reviewer_notes=decision.notes,
                    metadata=metadata,
                )
            )
        elif decision.decision == "rejected":
            output.append(
                replace(
                    rule,
                    status="rejected",
                    reviewer_notes=decision.notes,
                    metadata=metadata,
                )
            )
        elif decision.decision == "needs_edit":
            output.append(
                replace(
                    rule,
                    status="needs_edit",
                    reviewer_notes=decision.notes,
                    metadata=metadata,
                )
            )
        else:
            output.append(
                replace(
                    rule,
                    status="pending_review",
                    reviewer_notes=decision.notes,
                    metadata=metadata,
                )
            )

    unknown = set(decision_map) - known_ids
    if strict and unknown:
        raise ReviewFormatError(
            "审阅文件包含未知规则 ID: " + ", ".join(sorted(unknown))
        )
    return output


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def rules_payload(
    rules: Iterable[PolicyRule],
    *,
    approved_only: bool = False,
    generated_at: str | None = None,
    policy_version: str | None = None,
) -> dict[str, Any]:
    selected = [rule for rule in rules if not approved_only or rule.active]
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "rule_count": len(selected),
        "rules": [rule.to_dict() for rule in selected],
    }
    if policy_version:
        payload["policy_version"] = policy_version
    return payload


def write_rules_json(
    rules: Iterable[PolicyRule],
    output_path: str | Path,
    *,
    approved_only: bool = False,
    generated_at: str | None = None,
    policy_version: str | None = None,
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = rules_payload(
        rules,
        approved_only=approved_only,
        generated_at=generated_at,
        policy_version=policy_version,
    )
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path.resolve()


def export_approved_rules(
    rules: Iterable[PolicyRule],
    output_path: str | Path = "approved-rules.json",
    *,
    generated_at: str | None = None,
    policy_version: str | None = None,
) -> Path:
    """Export only explicitly approved rules as the authoritative JSON."""

    return write_rules_json(
        rules,
        output_path,
        approved_only=True,
        generated_at=generated_at,
        policy_version=policy_version,
    )


def load_rules_json(path: str | Path) -> list[PolicyRule]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    values = payload.get("rules", payload) if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        raise ValueError("规则 JSON 必须是数组或包含 rules 数组的对象")
    return [PolicyRule.from_dict(value) for value in values]


__all__ = [
    "REVIEW_FORMAT_VERSION",
    "ReviewFormatError",
    "apply_review_decisions",
    "export_approved_rules",
    "load_rules_json",
    "parse_review_decisions",
    "read_review_decisions",
    "render_review",
    "review_fingerprint",
    "rules_payload",
    "write_review",
    "write_rules_json",
]
