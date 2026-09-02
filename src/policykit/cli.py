from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .audit import AuditTrail
from .checkers import validate_checker_rules
from .compiler import write_global_block
from .config import (
    CONFIG_NAME,
    ensure_layout,
    find_home,
    load_config,
    resolve_path,
    write_default_config,
)
from .diagnostics import render_doctor, run_doctor
from .extractor import extract_file
from .hooks import main_hook, prepare_receipt
from .io_utils import utc_now
from .review import (
    apply_review_decisions,
    export_approved_rules,
    load_rules_json,
    read_review_decisions,
    write_review,
    write_rules_json,
)
from .search import PolicySearchIndex, build_sqlite_index


def _print(value: str = "") -> None:
    sys.stdout.write(value + ("" if value.endswith("\n") else "\n"))


def _infer_scope(path: Path, source_root: Path) -> str:
    try:
        parts = [part.casefold() for part in path.relative_to(source_root).parts[:-1]]
    except ValueError:
        parts = [part.casefold() for part in path.parts[:-1]]
    joined = "/".join(parts)
    if any(value in joined for value in ("project", "项目")):
        return "project"
    if any(value in joined for value in ("department", "部门", "team", "组内")):
        return "department"
    if any(value in joined for value in ("company", "公司")):
        return "company"
    return "unknown"


def _markdown_files(source: Path) -> list[Path]:
    if source.is_file():
        return [source] if source.suffix.casefold() in {".md", ".markdown"} else []
    if not source.exists():
        return []
    return sorted(
        [
            path
            for path in source.rglob("*")
            if path.is_file() and path.suffix.casefold() in {".md", ".markdown"}
        ],
        key=lambda path: str(path).casefold(),
    )


def _load_context(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    home, config = load_config(args.home, getattr(args, "config", None))
    ensure_layout(home, config)
    return home, config


def command_init(args: argparse.Namespace) -> int:
    home = (
        Path(args.home).expanduser().resolve()
        if args.home
        else find_home()
    )
    config_path = write_default_config(home, force=args.force)
    _, config = load_config(home, config_path)
    ensure_layout(home, config)
    source = resolve_path(home, config, "source_dir")
    for name in ("company", "department", "project"):
        (source / name).mkdir(parents=True, exist_ok=True)
    _print(f"已初始化：{home}")
    _print(f"配置文件：{config_path}")
    _print(f"请把规范 Markdown 放入：{source}")
    return 0


def command_prepare(args: argparse.Namespace) -> int:
    home, config = _load_context(args)
    source_root = (
        Path(args.source).expanduser().resolve()
        if args.source
        else resolve_path(home, config, "source_dir")
    )
    files = _markdown_files(source_root)
    if not files:
        raise FileNotFoundError(f"没有找到 Markdown 规范文件：{source_root}")

    rules = []
    for path in files:
        scope = args.scope or _infer_scope(path, source_root)
        try:
            source_name = path.relative_to(source_root).as_posix()
        except ValueError:
            source_name = path.name
        rules.extend(
            extract_file(
                path,
                scope=scope,
                id_prefix=args.id_prefix,
                source_name=source_name,
            )
        )
    candidates_path = resolve_path(home, config, "candidates")
    review_path = resolve_path(home, config, "review")
    write_rules_json(rules, candidates_path, approved_only=False)
    write_review(rules, review_path)
    _print(f"已扫描 {len(files)} 个 Markdown 文件。")
    _print(f"提取到 {len(rules)} 条待审阅候选规则；当前没有任何规则被激活。")
    _print(f"候选数据：{candidates_path}")
    _print(f"请审阅：{review_path}")
    return 0


def command_review(args: argparse.Namespace) -> int:
    """Regenerate the human review file after checker-draft enrichment."""

    home, config = _load_context(args)
    candidates_path = (
        Path(args.candidates).expanduser().resolve()
        if args.candidates
        else resolve_path(home, config, "candidates")
    )
    review_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else resolve_path(home, config, "review")
    )
    rules = load_rules_json(candidates_path)
    checker_errors = validate_checker_rules(rules)
    if checker_errors:
        raise ValueError(
            "checker 草案校验失败：\n- " + "\n- ".join(checker_errors[:20])
        )
    write_review(rules, review_path)
    configured = sum(
        1
        for rule in rules
        if any(
            key in (rule.metadata or {})
            for key in ("checks", "checkers", "check", "checker", "enforcement")
        )
    )
    _print(f"已重新生成 {len(rules)} 条候选规则的审阅文件。")
    _print(f"其中 {configured} 条包含可执行 checker 草案；其余规则按 AI review 处理。")
    _print(f"请审阅：{review_path}")
    return 0


def command_activate(args: argparse.Namespace) -> int:
    home, config = _load_context(args)
    candidates_path = (
        Path(args.candidates).expanduser().resolve()
        if args.candidates
        else resolve_path(home, config, "candidates")
    )
    review_path = (
        Path(args.review).expanduser().resolve()
        if args.review
        else resolve_path(home, config, "review")
    )
    candidates = load_rules_json(candidates_path)
    decisions = read_review_decisions(review_path, strict=True)
    reviewed = apply_review_decisions(candidates, decisions, strict=True)
    approved = [rule for rule in reviewed if rule.status == "approved"]
    checker_errors = validate_checker_rules(approved)
    if checker_errors:
        raise ValueError(
            "已批准 checker 校验失败，未激活任何新规则：\n- "
            + "\n- ".join(checker_errors[:20])
        )
    reviewed_path = resolve_path(home, config, "work_dir") / "reviewed-rules.json"
    approved_path = resolve_path(home, config, "approved_rules")
    index_path = resolve_path(home, config, "search_index")
    global_path = resolve_path(home, config, "global_block")
    policy_version = args.policy_version or utc_now()

    write_rules_json(
        reviewed,
        reviewed_path,
        approved_only=False,
        policy_version=policy_version,
    )
    export_approved_rules(
        reviewed,
        approved_path,
        policy_version=policy_version,
    )
    build_sqlite_index(approved, index_path, approved_only=True)
    limit = int(config.get("review", {}).get("global_core_limit", 40))
    write_global_block(approved, global_path, limit=limit)

    _print(f"候选规则：{len(candidates)}")
    _print(f"已批准并激活：{len(approved)}")
    _print(f"仍待处理或已拒绝：{len(reviewed) - len(approved)}")
    _print(f"正式规则库：{approved_path}")
    _print(f"检索索引：{index_path}")
    _print(f"全局 MD 复制块：{global_path}")
    return 0


def _read_optional_code(args: argparse.Namespace) -> str:
    if args.code_file:
        return Path(args.code_file).expanduser().read_text(encoding="utf-8-sig")
    return args.code or ""


def command_search(args: argparse.Namespace) -> int:
    home, config = _load_context(args)
    approved_path = resolve_path(home, config, "approved_rules")
    index = PolicySearchIndex.from_json(approved_path)
    results = index.search(
        query=args.query or "",
        file_path=args.file or "",
        code=_read_optional_code(args),
        limit=args.limit,
        scopes=args.scope,
        categories=args.category,
    )
    receipt_payload = None
    if args.receipt:
        if not args.session:
            raise ValueError("--receipt 必须同时提供 --session")
        if not args.file:
            raise ValueError("--receipt 必须同时提供 --file")
        receipt_payload = prepare_receipt(
            args.file,
            args.session,
            query=args.query or "",
            code=_read_optional_code(args),
            config=config,
            home=home,
            cwd=os.getcwd(),
        )
    ranked_results = [result.to_dict() for result in results]
    if receipt_payload is not None:
        compact_receipt = {
            key: receipt_payload.get(key)
            for key in (
                "status",
                "receipt_issued",
                "blocking",
                "policy_version",
                "matched_rule_ids",
                "context",
                "error",
            )
        }
        payload = {
            "status": str(receipt_payload.get("status") or "unavailable"),
            "searched": True,
            "file": args.file or "",
            "result_count": len(receipt_payload.get("matched_rule_ids") or ()),
            "receipt": compact_receipt,
        }
    else:
        payload = {
            "status": "matched" if results else "no_applicable_rule",
            "searched": True,
            "query": args.query or "",
            "file": args.file or "",
            "result_count": len(ranked_results),
            "results": ranked_results,
        }
    if args.json:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        if receipt_payload is not None:
            output_limit = max(
                2000,
                int(
                    config.get("runtime", {}).get("max_cli_output_chars", 8000)
                    or 8000
                ),
            )
            if len(encoded) > output_limit:
                context = str(compact_receipt.get("context") or "")
                overflow = len(encoded) - output_limit
                keep = max(200, len(context) - overflow - 80)
                compact_receipt["context"] = (
                    context[:keep].rstrip() + "\n…CLI 回执已按配置截断。"
                )
                encoded = json.dumps(payload, ensure_ascii=False, indent=2)
            if len(encoded) > output_limit:
                compact_receipt["context"] = (
                    "…CLI 回执上下文已省略；请按 matched_rule_ids 查询。"
                )
                encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        _print(encoded)
        return 0
    if receipt_payload is not None:
        _print(receipt_payload.get("context", ""))
        return 0
    if not results:
        _print("[规范查询] 已完成；没有找到当前场景的专门规则。")
        return 0
    _print(f"[规范查询] 命中 {len(results)} 条已审核规则：")
    for result in results:
        rule = result.rule
        reason = "；".join(result.reasons)
        _print(f"- [{rule.id}] [{rule.severity}] {rule.statement}")
        _print(f"  来源：{rule.source.document} / {rule.source.section or '未标章节'}")
        _print(f"  命中依据：{reason}")
    return 0


def command_hook(args: argparse.Namespace) -> int:
    home, config = _load_context(args)
    return main_hook(args.event, config=config, home=home)


def command_report(args: argparse.Namespace) -> int:
    home, config = _load_context(args)
    audit_dir = resolve_path(home, config, "audit_dir")
    if args.session:
        trail = AuditTrail(audit_dir, args.session)
        report = trail.write_report()
        _print(str(report))
        return 0
    reports = sorted(
        (audit_dir / "reports").glob("*.md"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not reports:
        _print("尚无会话审计报告。")
        return 0
    for path in reports[: args.limit]:
        _print(str(path))
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    home, config = _load_context(args)
    results = run_doctor(home, config)
    if args.json:
        _print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        _print(render_doctor(results))
    return 1 if any(result["status"] == "fail" for result in results) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="policykit",
        description="Codagent Java 规范导入、检索、检查与追踪工具。",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--home", help=f"Policy Kit 根目录（默认向上查找 {CONFIG_NAME}）")
    parser.add_argument("--config", help="显式指定 policykit.json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="初始化本地目录")
    init_parser.add_argument("--force", action="store_true", help="重写默认配置")
    init_parser.set_defaults(handler=command_init)

    prepare = subparsers.add_parser("prepare", help="从 Markdown 提取待审阅规则")
    prepare.add_argument("--source", help="规范文件或目录")
    prepare.add_argument(
        "--scope", choices=("company", "department", "project", "unknown")
    )
    prepare.add_argument("--id-prefix", default="AUTO")
    prepare.set_defaults(handler=command_prepare)

    review = subparsers.add_parser(
        "review", help="从候选 JSON 重新生成包含 checker 草案的审阅文件"
    )
    review.add_argument("--candidates")
    review.add_argument("--output")
    review.set_defaults(handler=command_review)

    activate = subparsers.add_parser("activate", help="激活审阅通过的规则")
    activate.add_argument("--candidates")
    activate.add_argument("--review")
    activate.add_argument("--policy-version")
    activate.set_defaults(handler=command_activate)

    search = subparsers.add_parser("search", help="查询已审核规范")
    search.add_argument("--query", default="")
    search.add_argument("--file", default="")
    search.add_argument("--code", default="")
    search.add_argument("--code-file")
    search.add_argument("--scope", action="append")
    search.add_argument("--category", action="append")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--session", help="会话 ID；准备写入凭据时必填")
    search.add_argument(
        "--receipt", action="store_true", help="为目标文件准备一次写入前规范凭据"
    )
    search.add_argument("--json", action="store_true")
    search.set_defaults(handler=command_search)

    hook = subparsers.add_parser("hook", help="处理 Codagent/Claude Hook 事件")
    hook.add_argument(
        "event", choices=("pre-edit", "pre-shell", "post-edit", "stop")
    )
    hook.set_defaults(handler=command_hook)

    report = subparsers.add_parser("report", help="生成或列出会话审计报告")
    report.add_argument("--session")
    report.add_argument("--limit", type=int, default=10)
    report.set_defaults(handler=command_report)

    doctor = subparsers.add_parser("doctor", help="检查本地安装")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=command_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args) or 0)
    except (FileNotFoundError, ValueError, KeyError, OSError) as error:
        sys.stderr.write(f"policykit: {error}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
