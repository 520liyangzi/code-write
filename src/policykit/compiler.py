from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .io_utils import write_text
from .model import PolicyRule


_SEVERITY_ORDER = {"blocker": 0, "major": 1, "advisory": 2}
_SCOPE_ORDER = {"company": 0, "department": 1, "project": 2, "unknown": 3}


def _as_rule(value: PolicyRule | Mapping[str, Any]) -> PolicyRule:
    return value if isinstance(value, PolicyRule) else PolicyRule.from_dict(value)


def select_global_core_rules(
    rules: Iterable[PolicyRule | Mapping[str, Any]], *, limit: int = 40
) -> list[PolicyRule]:
    """Select a compact global subset from already-approved rules.

    An explicit ``metadata.global_core`` decision wins.  Without one, only
    blocker rules outside project scope are selected automatically.  This
    intentionally prefers a small global file over silently copying the full
    policy corpus into every agent context.
    """

    approved = [_as_rule(value) for value in rules]
    approved = [rule for rule in approved if rule.status == "approved"]
    explicit = [rule for rule in approved if rule.metadata.get("global_core") is True]
    implicit = [
        rule
        for rule in approved
        if rule.metadata.get("global_core") is not False
        and rule.severity == "blocker"
        and rule.scope != "project"
        and rule not in explicit
    ]
    selected = explicit + implicit
    selected.sort(
        key=lambda rule: (
            0 if rule.metadata.get("global_core") is True else 1,
            _SEVERITY_ORDER.get(rule.severity, 9),
            _SCOPE_ORDER.get(rule.scope, 9),
            rule.id,
        )
    )
    return selected[: max(0, limit)]


def render_global_block(
    rules: Iterable[PolicyRule | Mapping[str, Any]], *, limit: int = 40
) -> str:
    selected = select_global_core_rules(rules, limit=limit)
    lines = [
        "<!-- CODAGENT-JAVA-POLICY:START -->",
        "## Java 规范执行流程",
        "",
        "任何创建或修改 Java 代码的任务，都必须使用 `java-policy` Skill。",
        "",
        "- 第一次修改前，查询当前文件和改动场景适用的已审核规则。",
        "- 每个逻辑改动单元开始前，向用户显示规范追踪状态和命中的规则 ID。",
        "- 已检索但没有专门规则时，必须明确说明，不得声称已严格遵循公司专门规则。",
        "- 规范查询失败或 Hook 阻断时，不得绕过并继续写入。",
        "- 不得通过 Shell、Python/Node 写文件脚本绕过受管文件的 Edit/Write Hook。",
        "- 修改完成后执行硬规则检查；结束前执行 `java-review` Skill。",
        "- CodeGraph 仅在当前项目已配置且可用时用于代码检索，不是强制依赖。",
        "",
        "### 核心红线",
        "",
    ]
    if selected:
        lines.extend(f"- `{rule.id}`：{rule.statement}" for rule in selected)
    else:
        lines.append("- 当前没有经审核并被选入全局上下文的核心规则。")
    lines.extend(
        [
            "",
            "完整规则不写入本文件，由本地 Policy Kit 按需检索。",
            "<!-- CODAGENT-JAVA-POLICY:END -->",
            "",
        ]
    )
    return "\n".join(lines)


def write_global_block(
    rules: Iterable[PolicyRule | Mapping[str, Any]],
    output_path: str | Path,
    *,
    limit: int = 40,
) -> Path:
    return write_text(output_path, render_global_block(rules, limit=limit))
