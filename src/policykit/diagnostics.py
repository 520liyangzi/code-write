from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .config import resolve_path


def _result(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def run_doctor(home: Path, config: Mapping[str, Any]) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    results.append(
        _result(
            "Python",
            "pass" if sys.version_info >= (3, 10) else "fail",
            sys.version.split()[0],
        )
    )

    for command, label in (("java", "JDK"), ("mvn", "Maven")):
        executable = shutil.which(command)
        results.append(
            _result(
                label,
                "pass" if executable else "warn",
                executable or f"未在 PATH 中找到 {command}",
            )
        )

    work_dir = resolve_path(home, config, "work_dir")
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        probe = work_dir / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        results.append(_result("运行目录", "pass", str(work_dir)))
    except OSError as error:
        results.append(_result("运行目录", "fail", str(error)))

    approved = resolve_path(home, config, "approved_rules")
    if approved.exists():
        try:
            payload = json.loads(approved.read_text(encoding="utf-8-sig"))
            count = len(payload.get("rules", [])) if isinstance(payload, dict) else 0
            results.append(_result("已审核规则库", "pass", f"{count} 条规则"))
        except (OSError, ValueError) as error:
            results.append(_result("已审核规则库", "fail", str(error)))
    else:
        results.append(_result("已审核规则库", "warn", "尚未运行 activate"))

    index_path = resolve_path(home, config, "search_index")
    results.append(
        _result(
            "检索索引",
            "pass" if index_path.exists() else "warn",
            str(index_path) if index_path.exists() else "尚未生成",
        )
    )

    codegraph = config.get("codegraph", {})
    if codegraph.get("enabled"):
        command = str(codegraph.get("command", "")).strip()
        status = "pass" if command else "warn"
        detail = command or "已启用但未配置命令；MCP 模式可忽略此提示"
    else:
        status, detail = "skip", "未启用（可选能力）"
    results.append(_result("CodeGraph", status, detail))
    return results


def render_doctor(results: list[Mapping[str, str]]) -> str:
    symbols = {"pass": "通过", "warn": "提醒", "fail": "失败", "skip": "跳过"}
    return "\n".join(
        f"[{symbols.get(item.get('status', ''), item.get('status', '未知'))}] "
        f"{item.get('name', '')}：{item.get('detail', '')}"
        for item in results
    )

