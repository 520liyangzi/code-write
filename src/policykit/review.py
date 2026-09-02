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

from .io_utils import write_json, write_text
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


def _structured_details(rule: PolicyRule) -> list[str]:
    metadata = rule.metadata or {}
    if not metadata.get("structured_format"):
        return []
    lines = ["### 结构化规则详情", ""]
    level = str(metadata.get("level") or "未标注").strip()
    lines.extend([f"- 级别：`{level}`", ""])
    for key, label in (
        ("description", "描述"),
        ("negative_example", "反例"),
        ("positive_example", "正例"),
    ):
        value = str(metadata.get(key) or "").strip()
        lines.extend([f"#### {label}", "", value or "（未提供）", ""])
    return lines


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


def reconcile_review_decisions(
    previous_rules: Iterable[PolicyRule],
    previous_decisions: Mapping[str, ReviewDecision],
    new_rules: Iterable[PolicyRule],
) -> tuple[dict[str, ReviewDecision], list[ReviewDecision]]:
    """Carry decisions forward only for byte-equivalent candidate meaning.

    New rules remain pending.  Changed or removed rules are returned as
    ``dropped`` when their old decision contains user work, allowing callers
    to request one explicit reset confirmation instead of asking the user to
    re-approve every unchanged rule in every batch.
    """

    old_by_id = {rule.id: rule for rule in previous_rules}
    new_by_id = {rule.id: rule for rule in new_rules}
    preserved: dict[str, ReviewDecision] = {}
    preserved_ids: set[str] = set()
    for rule_id, rule in new_by_id.items():
        old_rule = old_by_id.get(rule_id)
        decision = previous_decisions.get(rule_id)
        if old_rule is None or decision is None:
            continue
        if (
            decision.decision == "pending_review"
            and not decision.edited_statement
            and not decision.notes
        ):
            # An untouched review block is the default state, not user work.
            # Re-render it as unchecked rather than turning "暂不处理" into
            # an explicit checked decision on every prepare/review run.
            continue
        old_fingerprint = review_fingerprint(old_rule)
        if (
            decision.review_hash != old_fingerprint
            or old_fingerprint != review_fingerprint(rule)
        ):
            continue
        preserved[rule_id] = ReviewDecision(
            rule_id=rule_id,
            decision=decision.decision,
            review_hash=review_fingerprint(rule),
            edited_statement=decision.edited_statement,
            notes=decision.notes,
        )
        preserved_ids.add(rule_id)

    dropped = [
        decision
        for rule_id, decision in previous_decisions.items()
        if rule_id not in preserved_ids
        and (
            decision.decision != "pending_review"
            or bool(decision.edited_statement)
            or bool(decision.notes)
        )
    ]
    return preserved, dropped


def _review_decision_map(
    decisions: Iterable[ReviewDecision] | Mapping[str, ReviewDecision] | None,
) -> dict[str, ReviewDecision]:
    if decisions is None:
        return {}
    if isinstance(decisions, Mapping):
        values = dict(decisions)
    else:
        values: dict[str, ReviewDecision] = {}
        for decision in decisions:
            if decision.rule_id in values:
                raise ReviewFormatError(f"规则决定重复: {decision.rule_id}")
            values[decision.rule_id] = decision
    for rule_id, decision in values.items():
        if not isinstance(decision, ReviewDecision):
            raise TypeError(f"规则 {rule_id} 的审阅决定类型无效")
        if rule_id != decision.rule_id:
            raise ReviewFormatError(
                f"审阅决定键 {rule_id} 与规则 ID {decision.rule_id} 不一致"
            )
    return values


def _safe_review_text(value: str, *, field: str, rule_id: str) -> str:
    if re.search(
        r"<!--\s*(?:/?POLICYKIT-|decision\s*:)", value, re.IGNORECASE
    ):
        raise ReviewFormatError(
            f"规则 {rule_id} 的{field}包含 Policy Kit 保留标记"
        )
    return value


def render_review(
    rules: Iterable[PolicyRule],
    decisions: Iterable[ReviewDecision] | Mapping[str, ReviewDecision] | None = None,
) -> str:
    """Render a stable, checkbox-based ``REVIEW_ME.md`` document."""

    sorted_rules = sorted(
        rules,
        key=lambda rule: (
            rule.source.document.casefold(),
            rule.source.line_start,
            rule.id,
        ),
    )
    decision_map = _review_decision_map(decisions)
    known_ids = {rule.id for rule in sorted_rules}
    unknown_ids = set(decision_map) - known_ids
    if unknown_ids:
        raise ReviewFormatError(
            "审阅决定包含未知规则 ID: " + ", ".join(sorted(unknown_ids))
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
        fingerprint = review_fingerprint(rule)
        decision = decision_map.get(rule.id)
        decision_name = decision.decision if decision else ""
        if decision_name == "needs_edit":
            decision_name = "pending_review"
        if decision and decision.review_hash and decision.review_hash != fingerprint:
            raise ReviewFormatError(
                f"规则 {rule.id} 的审阅决定已过期；请重新加载候选规则"
            )
        edited_statement = decision.edited_statement if decision else ""
        notes = decision.notes if decision else ""
        if decision_name == "modified" and not edited_statement.strip():
            raise ReviewFormatError(
                f"规则 {rule.id} 选择了“修改后接受”，但没有填写修改后的规则正文"
            )
        edited_statement = _safe_review_text(
            edited_statement, field="修改正文", rule_id=rule.id
        )
        notes = _safe_review_text(notes, field="审阅备注", rule_id=rule.id)
        edited_block = (
            edited_statement
            if edited_statement
            else "<!-- 在这里填写修改后的完整规则正文 -->"
        )
        notes_block = notes if notes else "<!-- 可在这里填写原因、适用条件或后续事项 -->"

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
                f'<!-- POLICYKIT-RULE id="{rule.id}" review_hash="{fingerprint}" -->',
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
                *_structured_details(rule),
                "### 文档原文",
                "",
                _blockquote(rule.source.quote or rule.statement),
                "",
                "### 审阅决定（只能勾选一个）",
                "",
                f"- [{'x' if decision_name == 'approved' else ' '}] 接受并启用 <!-- decision:approved -->",
                f"- [{'x' if decision_name == 'modified' else ' '}] 修改后接受 <!-- decision:modified -->",
                f"- [{'x' if decision_name == 'rejected' else ' '}] 拒绝 <!-- decision:rejected -->",
                f"- [{'x' if decision_name == 'pending_review' else ' '}] 暂不处理 <!-- decision:pending_review -->",
                "",
                "### 修改后的规则正文",
                "",
                "仅在勾选“修改后接受”时填写；请保留下面两个标记。",
                "",
                "<!-- POLICYKIT-EDITED:start -->",
                edited_block,
                "<!-- POLICYKIT-EDITED:end -->",
                "",
                "### 审阅备注（可选）",
                "",
                "<!-- POLICYKIT-NOTES:start -->",
                notes_block,
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
    rules: Iterable[PolicyRule],
    output_path: str | Path = "REVIEW_ME.md",
    decisions: Iterable[ReviewDecision] | Mapping[str, ReviewDecision] | None = None,
) -> Path:
    """Atomically write the review document and return its resolved path."""

    return write_text(output_path, render_review(rules, decisions)).resolve()


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
    bundle_id: str | None = None,
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
    if bundle_id:
        payload["bundle_id"] = bundle_id
    return payload


def bundle_fingerprint(
    rules: Iterable[PolicyRule],
    policy_version: str,
) -> str:
    """Return a stable identity for one activated rule set and version."""

    canonical_rules = sorted(
        (rule.to_dict() for rule in rules if rule.status == "approved"),
        key=lambda value: str(value.get("id") or ""),
    )
    canonical = json.dumps(
        {
            "policy_version": str(policy_version or ""),
            "rules": canonical_rules,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def write_rules_json(
    rules: Iterable[PolicyRule],
    output_path: str | Path,
    *,
    approved_only: bool = False,
    generated_at: str | None = None,
    policy_version: str | None = None,
    bundle_id: str | None = None,
) -> Path:
    payload = rules_payload(
        rules,
        approved_only=approved_only,
        generated_at=generated_at,
        policy_version=policy_version,
        bundle_id=bundle_id,
    )
    return write_json(output_path, payload).resolve()


def export_approved_rules(
    rules: Iterable[PolicyRule],
    output_path: str | Path = "approved-rules.json",
    *,
    generated_at: str | None = None,
    policy_version: str | None = None,
    bundle_id: str | None = None,
) -> Path:
    """Export only explicitly approved rules as the authoritative JSON."""

    return write_rules_json(
        rules,
        output_path,
        approved_only=True,
        generated_at=generated_at,
        policy_version=policy_version,
        bundle_id=bundle_id,
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
    "bundle_fingerprint",
    "export_approved_rules",
    "load_rules_json",
    "parse_review_decisions",
    "read_review_decisions",
    "reconcile_review_decisions",
    "render_review",
    "review_fingerprint",
    "rules_payload",
    "write_review",
    "write_rules_json",
]
