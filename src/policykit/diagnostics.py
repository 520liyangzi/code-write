from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .config import resolve_path
from .model import PolicyRule
from .review import bundle_fingerprint
from .search import SQLitePolicyIndex


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
    approved_version = ""
    approved_bundle_id = ""
    approved_bundle_valid = False
    if approved.exists():
        try:
            payload = json.loads(approved.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict) or not isinstance(payload.get("rules"), list):
                raise ValueError("规则库必须是包含 rules 数组的 JSON 对象")
            raw_rules = payload["rules"]
            if not raw_rules or any(not isinstance(rule, Mapping) for rule in raw_rules):
                raise ValueError("规则库必须包含至少一条规则对象")
            rules = [PolicyRule.from_dict(rule) for rule in raw_rules]
            if any(not rule.active for rule in rules):
                raise ValueError("正式规则包包含未批准规则")
            approved_version = str(payload.get("policy_version") or "")
            approved_bundle_id = str(payload.get("bundle_id") or "").casefold()
            if bundle_fingerprint(rules, approved_version) != approved_bundle_id:
                raise ValueError("bundle_id 缺失或与规则内容不一致")
            approved_bundle_valid = True
            results.append(
                _result(
                    "已审核规则库",
                    "pass",
                    f"{len(rules)} 条规则，版本 {approved_version}",
                )
            )
        except (OSError, TypeError, ValueError) as error:
            results.append(_result("已审核规则库", "fail", str(error)))
    else:
        results.append(_result("已审核规则库", "warn", "尚未运行 activate"))

    index_path = resolve_path(home, config, "search_index")
    if index_path.exists() and approved_bundle_valid:
        try:
            SQLitePolicyIndex(index_path).validate_metadata(
                expected_policy_version=approved_version,
                expected_bundle_id=approved_bundle_id,
            )
            results.append(_result("检索索引", "pass", str(index_path)))
        except (OSError, ValueError) as error:
            results.append(_result("检索索引", "fail", str(error)))
    elif index_path.exists():
        results.append(
            _result("检索索引", "fail", "规则包无效，无法确认索引属于同一 bundle")
        )
    else:
        results.append(
            _result(
                "检索索引",
                "fail" if approved.exists() else "warn",
                "规则库已存在但索引缺失" if approved.exists() else "尚未生成",
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
