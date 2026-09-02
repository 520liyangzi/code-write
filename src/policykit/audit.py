"""Append-only audit trails and human-readable session reports.

The runtime deliberately keeps audit data outside the repository being
edited.  Callers choose the audit root (normally ``paths.audit_dir`` from the
kit configuration); this module never guesses a location below the current
project.

Only metadata passed by the caller is persisted.  Hook callers should record
paths, rule ids and outcomes instead of source code or complete tool payloads.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Iterable, Mapping
from uuid import uuid4


AUDIT_SCHEMA_VERSION = 1
_PROCESS_LOCK = threading.RLock()


def utc_now() -> str:
    """Return an RFC-3339 UTC timestamp with second precision."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def safe_session_id(value: Any) -> str:
    """Convert an untrusted hook session id into a safe file stem."""

    text = str(value or "session").strip()
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip(".-")
    return (text[:120] or "session")


def _json_safe(value: Any) -> Any:
    """Convert common runtime objects to deterministic JSON values."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_safe(to_dict())
    return str(value)


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read valid JSON objects from *path*.

    A partially written final line must not make the whole audit unreadable,
    so malformed or non-object lines are ignored.
    """

    source = Path(path)
    if not source.is_file():
        return []
    events: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                events.append(value)
    return events


def _markdown_cell(value: Any, *, limit: int = 220) -> str:
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value or "")
    text = re.sub(r"\s+", " ", text).strip().replace("|", "\\|")
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text or "—"


def summarize_events(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Build a stable summary used by both hooks and Markdown reports."""

    materialized = list(events)
    event_counts = Counter(str(item.get("event", "unknown")) for item in materialized)
    policy_counts = Counter(
        str(item.get("policy_status"))
        for item in materialized
        if item.get("policy_status")
    )
    rule_ids: set[str] = set()
    failed_rules: set[str] = set()
    changed_files: set[str] = set()
    blocking_events = 0
    latest_results: dict[tuple[str, str, str], dict[str, Any]] = {}
    ever_failed: set[tuple[str, str, str]] = set()
    pending_ai: dict[tuple[str, str], dict[str, Any]] = {}
    ai_self_attested: set[str] = set()
    for event in materialized:
        event_name = str(event.get("event", ""))
        for rule_id in event.get("matched_rule_ids", ()) or ():
            rule_ids.add(str(rule_id))
        for result in event.get("results", ()) or ():
            if not isinstance(result, Mapping):
                continue
            rule_id = str(result.get("rule_id", "")).strip()
            if rule_id:
                rule_ids.add(rule_id)
            if result.get("status") in {"fail", "error"} and rule_id:
                failed_rules.add(rule_id)
            if result.get("blocking") and result.get("status") in {"fail", "error"}:
                blocking_events += 1
            checker = str(result.get("checker", "")).strip()
            result_path = str(result.get("path", "")).strip()
            key = (rule_id or "UNKNOWN-RULE", result_path, checker)
            compact = {
                "rule_id": rule_id or "UNKNOWN-RULE",
                "path": result_path,
                "checker": checker,
                "status": str(result.get("status", "unknown")),
                "severity": str(result.get("severity", "")),
                "message": str(result.get("message", "")),
                "blocking": bool(result.get("blocking")),
            }
            latest_results[key] = compact
            if compact["status"] in {"fail", "error"}:
                ever_failed.add(key)
            if compact["status"] == "review" and (
                event_name == "post_write_check"
                or (
                    event_name == "stop_summary"
                    and str(event.get("outcome", "")) != "completed"
                )
            ):
                pending_ai[(compact["rule_id"], result_path)] = compact
        if event_name == "ai_review_self_attested":
            attested = {
                str(rule_id)
                for rule_id in event.get("matched_rule_ids", ()) or ()
                if str(rule_id)
            }
            ai_self_attested.update(attested)
            pending_ai = {
                key: value
                for key, value in pending_ai.items()
                if key[0] not in attested
            }
        path = str(event.get("path", "")).strip()
        if path and event.get("event") in {
            "post_write_check",
            "write_observed",
            "pre_write_authorized",
        }:
            changed_files.add(path)

    unresolved = [
        value
        for key, value in latest_results.items()
        if value["status"] in {"fail", "error"}
    ]
    resolved = [
        value
        for key, value in latest_results.items()
        if key in ever_failed and value["status"] in {"pass", "skip"}
    ]
    sort_key = lambda item: (item["rule_id"], item["path"], item["checker"])

    return {
        "events": len(materialized),
        "event_counts": dict(sorted(event_counts.items())),
        "policy_counts": dict(sorted(policy_counts.items())),
        "matched_rule_ids": sorted(rule_ids),
        "failed_rule_ids": sorted(failed_rules),
        "changed_files": sorted(changed_files),
        "blocking_failures": blocking_events,
        "resolved_issues": sorted(resolved, key=sort_key),
        "unresolved_issues": sorted(unresolved, key=sort_key),
        "pending_ai_reviews": sorted(pending_ai.values(), key=sort_key),
        "ai_self_attested_rule_ids": sorted(ai_self_attested),
    }


def render_markdown_report(
    session_id: str,
    events: Iterable[Mapping[str, Any]],
    *,
    generated_at: str | None = None,
) -> str:
    """Render an audit trail as a concise, evidence-oriented Markdown report."""

    materialized = list(events)
    summary = summarize_events(materialized)
    generated = generated_at or utc_now()
    if summary["unresolved_issues"]:
        status = f"最终仍有 {len(summary['unresolved_issues'])} 个确定性问题"
    elif summary["pending_ai_reviews"]:
        status = f"仍有 {len(summary['pending_ai_reviews'])} 项等待 AI 审查证据"
    elif summary["resolved_issues"]:
        status = "历史确定性问题已在后续检查中通过"
    elif summary["blocking_failures"]:
        status = "历史中记录过阻断检查；最终状态无对应失败项"
    else:
        status = "未记录未解决的确定性问题"

    lines = [
        "# Java Policy Kit 会话报告",
        "",
        f"- 会话：`{_markdown_cell(session_id)}`",
        f"- 生成时间：`{generated}`",
        f"- 状态：{status}",
        f"- 已记录事件：{summary['events']}",
        f"- 涉及文件：{len(summary['changed_files'])}",
        f"- 命中规范：{len(summary['matched_rule_ids'])}",
        "",
        "## 规范使用结论",
        "",
    ]
    policy_counts = summary["policy_counts"]
    if policy_counts:
        labels = {
            "matched": "规范已命中",
            "none": "已检索但无专门规范",
            "unavailable": "规范检索不可用",
            "not_required": "无需规范凭据",
        }
        for key, count in policy_counts.items():
            lines.append(f"- {labels.get(key, key)}：{count}")
    else:
        lines.append("- 本会话没有规范查询记录。")

    lines.extend(["", "## 修改文件", ""])
    if summary["changed_files"]:
        lines.extend(f"- `{path}`" for path in summary["changed_files"])
    else:
        lines.append("- 未记录代码写入。")

    lines.extend(["", "## 命中规则", ""])
    if summary["matched_rule_ids"]:
        lines.extend(f"- `{rule_id}`" for rule_id in summary["matched_rule_ids"])
    else:
        lines.append("- 无。")

    lines.extend(["", "## 最终检查状态", ""])
    if summary["unresolved_issues"]:
        lines.append("### 最终未解决")
        lines.append("")
        for item in summary["unresolved_issues"]:
            lines.append(
                f"- `{_markdown_cell(item['rule_id'])}` / "
                f"`{_markdown_cell(item['path'])}` / "
                f"`{_markdown_cell(item['checker'])}`：{_markdown_cell(item['message'])}"
            )
    else:
        lines.append("- 没有最终状态为 fail/error 的确定性检查。")

    if summary["resolved_issues"]:
        lines.extend(["", "### 已修复的历史问题", ""])
        for item in summary["resolved_issues"]:
            lines.append(
                f"- `{_markdown_cell(item['rule_id'])}` / "
                f"`{_markdown_cell(item['path'])}` / `{_markdown_cell(item['checker'])}`"
            )

    if summary["pending_ai_reviews"]:
        lines.extend(["", "### 等待 AI 审查证据", ""])
        for item in summary["pending_ai_reviews"]:
            lines.append(
                f"- `{_markdown_cell(item['rule_id'])}` / `{_markdown_cell(item['path'])}`"
            )

    if summary["ai_self_attested_rule_ids"]:
        lines.extend(["", "### AI 自述已审查（非程序化验证）", ""])
        lines.extend(
            f"- `{_markdown_cell(rule_id)}`"
            for rule_id in summary["ai_self_attested_rule_ids"]
        )

    lines.extend(
        [
            "",
            "## 时间线",
            "",
            "| 时间 | 事件 | 文件/目标 | 规范状态 | 结果 |",
            "|---|---|---|---|---|",
        ]
    )
    for event in materialized:
        results = event.get("results") or ()
        if isinstance(results, list) and results:
            failed = sum(
                1
                for result in results
                if isinstance(result, Mapping)
                and result.get("status") in {"fail", "error"}
            )
            reviews = sum(
                1
                for result in results
                if isinstance(result, Mapping) and result.get("status") == "review"
            )
            outcome = f"失败 {failed}，待 AI 审查 {reviews}" if failed or reviews else "通过"
        else:
            outcome = event.get("outcome") or event.get("message") or "—"
        lines.append(
            "| {time} | {event} | {path} | {policy} | {outcome} |".format(
                time=_markdown_cell(event.get("timestamp"), limit=32),
                event=_markdown_cell(event.get("event"), limit=50),
                path=_markdown_cell(event.get("path") or event.get("target")),
                policy=_markdown_cell(event.get("policy_status"), limit=50),
                outcome=_markdown_cell(outcome),
            )
        )

    lines.extend(
        [
            "",
            "> 本报告证明运行时实际记录到的查询与检查；“命中规范”不等于所有规则均可程序化验证。",
            "",
        ]
    )
    return "\n".join(lines)


class AuditTrail:
    """Append-only JSONL audit plus a regenerable Markdown session report."""

    def __init__(self, root: str | Path, session_id: str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.session_id = safe_session_id(session_id)
        self.jsonl_path = self.root / "sessions" / f"{self.session_id}.jsonl"
        self.report_path = self.root / "reports" / f"{self.session_id}.md"

    def events(self) -> list[dict[str, Any]]:
        return read_jsonl(self.jsonl_path)

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        """Append one event and return the exact persisted object."""

        event_name = str(event or "unknown").strip() or "unknown"
        with _PROCESS_LOCK:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            sequence = len(read_jsonl(self.jsonl_path)) + 1
            item = {
                "schema_version": AUDIT_SCHEMA_VERSION,
                "event_id": uuid4().hex,
                "sequence": sequence,
                "timestamp": utc_now(),
                "session_id": self.session_id,
                "event": event_name,
            }
            item.update(_json_safe(fields))
            encoded = (json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n").encode(
                "utf-8"
            )
            descriptor = os.open(
                self.jsonl_path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                0o600,
            )
            try:
                os.write(descriptor, encoded)
            finally:
                os.close(descriptor)
        return item

    def summary(self) -> dict[str, Any]:
        return summarize_events(self.events())

    def write_report(self, path: str | Path | None = None) -> Path:
        destination = Path(path) if path is not None else self.report_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = render_markdown_report(self.session_id, self.events())
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, destination)
        return destination


def append_audit_event(
    root: str | Path, session_id: str, event: str, **fields: Any
) -> dict[str, Any]:
    """Functional wrapper for integrations that do not keep an AuditTrail."""

    return AuditTrail(root, session_id).record(event, **fields)


__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "AuditTrail",
    "append_audit_event",
    "read_jsonl",
    "render_markdown_report",
    "safe_session_id",
    "summarize_events",
    "utc_now",
]
