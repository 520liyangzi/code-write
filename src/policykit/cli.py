from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .ai import (
    AISettings,
    PolicyAIError,
    build_embeddings_cached,
    embed_runtime_query,
    enrich_rules_cached,
)
from .audit import AuditTrail
from .checkers import PolicyChecker, validate_checker_rules
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
from .database import PolicyDatabaseError, sync_database_bundle
from .extractor import extract_file, qualify_duplicate_rule_ids
from .hooks import main_hook, prepare_receipt
from .io_utils import utc_now
from .model import PolicyRule
from .review import (
    apply_review_decisions,
    bundle_fingerprint,
    export_approved_rules,
    load_rules_json,
    read_review_decisions,
    reconcile_review_decisions,
    write_review,
    write_rules_json,
)
from .search import SQLitePolicyIndex, build_sqlite_index, retrieve_runtime_rules


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

    rules: list[PolicyRule] = []
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
    rules = qualify_duplicate_rule_ids(rules)
    candidates_path = resolve_path(home, config, "candidates")
    review_path = resolve_path(home, config, "review")
    previous_candidates = (
        load_rules_json(candidates_path) if candidates_path.is_file() else []
    )
    previous_decisions = {
        decision.rule_id: decision
        for decision in (
            read_review_decisions(review_path, strict=False)
            if review_path.is_file()
            else []
        )
    }
    ai_settings = AISettings.from_config(config)
    ai_warning = ""
    try:
        ai_stats = enrich_rules_cached(
            rules,
            ai_settings,
            resolve_path(home, config, "ai_cache"),
        )
    except (PolicyAIError, OSError, ValueError) as error:
        if ai_settings.required:
            raise
        ai_stats = {"enabled": 1, "cached": 0, "generated": 0}
        ai_warning = str(error)

    preserved, resettable = reconcile_review_decisions(
        previous_candidates,
        previous_decisions,
        rules,
    )
    if resettable and not args.reset_decisions:
        counts: dict[str, int] = {}
        for decision in resettable:
            counts[decision.decision] = counts.get(decision.decision, 0) + 1
        summary = "、".join(
            f"{name}={count}" for name, count in sorted(counts.items())
        )
        raise ValueError(
            "检测到已修改或已删除规则上的审阅决定（"
            f"{summary}）。确认丢弃这些决定时请加 --reset-decisions；"
            f"另外 {len(preserved)} 条未变化规则的决定会继续保留。"
        )
    write_rules_json(rules, candidates_path, approved_only=False)
    write_review(rules, review_path, decisions=preserved)
    _print(f"已扫描 {len(files)} 个 Markdown 文件。")
    _print(
        f"提取到 {len(rules)} 条候选规则；保留 {len(preserved)} 条既有决定，"
        f"新增待审 {len(rules) - len(preserved)} 条。"
    )
    if ai_stats.get("enabled"):
        _print(
            "大模型检索增强："
            f"缓存 {ai_stats.get('cached', 0)}，"
            f"新生成 {ai_stats.get('generated', 0)}。"
        )
    if ai_warning:
        _print(f"提示：大模型增强失败，已使用本地解析结果：{ai_warning}")
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
    previous_decisions = {
        decision.rule_id: decision
        for decision in (
            read_review_decisions(review_path, strict=False)
            if review_path.is_file()
            else []
        )
    }
    checker_errors = validate_checker_rules(rules)
    if checker_errors:
        raise ValueError(
            "checker 草案校验失败：\n- " + "\n- ".join(checker_errors[:20])
        )
    preserved, resettable = reconcile_review_decisions(
        rules,
        previous_decisions,
        rules,
    )
    if resettable and not args.reset_decisions:
        raise ValueError(
            "checker 草案或候选内容已变化，部分既有决定已过期。"
            "确认丢弃过期决定时请加 --reset-decisions。"
        )
    write_review(rules, review_path, decisions=preserved)
    configured = sum(
        1
        for rule in rules
        if any(
            key in (rule.metadata or {})
            for key in ("checks", "checkers", "check", "checker", "enforcement")
        )
    )
    _print(f"已重新生成 {len(rules)} 条候选规则的审阅文件。")
    _print(f"已保留 {len(preserved)} 条未变化规则的既有决定。")
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
    if not approved:
        raise ValueError(
            "没有任何已批准规则；为防止误清空现有策略，activate 已拒绝。"
            "请先在 REVIEW_ME.md 中批准至少一条规则。"
        )
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
    limit = int(config.get("review", {}).get("global_core_limit", 40))
    bundle_id = bundle_fingerprint(approved, policy_version)

    ai_settings = AISettings.from_config(config)
    embedding_warning = ""
    try:
        embeddings, embedding_stats = build_embeddings_cached(
            approved,
            ai_settings,
            resolve_path(home, config, "embedding_cache"),
        )
    except (PolicyAIError, OSError, ValueError) as error:
        if ai_settings.required:
            raise
        embeddings = {}
        embedding_stats = {"enabled": 1, "cached": 0, "generated": 0}
        embedding_warning = str(error)

    write_rules_json(
        reviewed,
        reviewed_path,
        approved_only=False,
        policy_version=policy_version,
        bundle_id=bundle_id,
    )
    export_approved_rules(
        reviewed,
        approved_path,
        policy_version=policy_version,
        bundle_id=bundle_id,
    )
    build_sqlite_index(
        approved,
        index_path,
        approved_only=True,
        policy_version=policy_version,
        bundle_id=bundle_id,
        embeddings=embeddings,
        embedding_model=ai_settings.embedding_model if embeddings else None,
    )
    write_global_block(approved, global_path, limit=limit)
    database = sync_database_bundle(
        config,
        home,
        approved,
        policy_version=policy_version,
        bundle_id=bundle_id,
        embeddings=embeddings,
    )

    _print(f"候选规则：{len(candidates)}")
    _print(f"已批准并激活：{len(approved)}")
    _print(f"仍待处理或已拒绝：{len(reviewed) - len(approved)}")
    _print(f"正式规则库：{approved_path}")
    _print(f"检索索引：{index_path}")
    _print(f"全局 MD 复制块：{global_path}")
    if embedding_stats.get("enabled"):
        _print(
            "向量索引："
            f"缓存 {embedding_stats.get('cached', 0)}，"
            f"新生成 {embedding_stats.get('generated', 0)}。"
        )
    if embedding_warning:
        _print(f"提示：向量生成失败，已使用纯 BM25 索引：{embedding_warning}")
    if database.get("enabled"):
        if database.get("synced"):
            _print(f"数据库镜像：已同步 {database.get('rule_count', 0)} 条规则。")
        else:
            _print(f"提示：数据库镜像未同步：{database.get('error') or database.get('reason')}")
    return 0


def _read_optional_code(args: argparse.Namespace) -> str:
    if args.code_file:
        return Path(args.code_file).expanduser().read_text(encoding="utf-8-sig")
    return args.code or ""


def command_search(args: argparse.Namespace) -> int:
    home, config = _load_context(args)
    approved_path = resolve_path(home, config, "approved_rules")
    index_path = resolve_path(home, config, "search_index")
    approved_payload = json.loads(approved_path.read_text(encoding="utf-8-sig"))
    if isinstance(approved_payload, list):
        approved_rules = approved_payload
        policy_version = "1"
        bundle_id = ""
    elif isinstance(approved_payload, dict):
        approved_rules = approved_payload.get("rules", [])
        policy_version = str(
            approved_payload.get("policy_version")
            or approved_payload.get("schema_version")
            or "1"
        )
        bundle_id = str(approved_payload.get("bundle_id") or "")
    else:
        raise ValueError("已批准规则文件必须是 JSON 对象或数组")
    if not isinstance(approved_rules, list):
        raise ValueError("已批准规则的 rules 字段必须是数组")
    if any(not isinstance(rule, dict) for rule in approved_rules):
        raise ValueError("已批准规则的 rules 数组包含非对象条目")
    normalized_bundle_id = bundle_id.casefold()
    parsed_rules = [PolicyRule.from_dict(rule) for rule in approved_rules]
    if not parsed_rules:
        raise ValueError("approved-rules.json 不包含已批准规则；请重新激活")
    if any(not rule.active for rule in parsed_rules):
        raise ValueError("approved-rules.json 包含未批准规则；请重新激活")
    calculated_bundle_id = bundle_fingerprint(parsed_rules, policy_version)
    if (
        len(normalized_bundle_id) != 64
        or any(character not in "0123456789abcdef" for character in normalized_bundle_id)
        or normalized_bundle_id != calculated_bundle_id
    ):
        raise ValueError(
            "approved-rules.json 的 bundle_id 缺失或与规则内容不一致；请重新激活"
        )
    configured_block_severities = config.get("runtime", {}).get(
        "block_severities", ("blocker", "major")
    )
    if isinstance(configured_block_severities, str):
        configured_block_severities = (configured_block_severities,)
    checker = PolicyChecker(
        approved_rules,
        fail_closed=bool(config.get("runtime", {}).get("fail_closed", True)),
        block_severities=configured_block_severities,
    )
    code = _read_optional_code(args)
    ai_settings = AISettings.from_config(config)
    index_metadata = SQLitePolicyIndex(index_path).validate_metadata(
        expected_policy_version=policy_version,
        expected_bundle_id=normalized_bundle_id,
    )
    query_embedding, semantic_error = embed_runtime_query(
        ai_settings,
        query=args.query or "",
        file_path=args.file or "",
        code=code,
        index_metadata=index_metadata,
    )
    results = retrieve_runtime_rules(
        index_path,
        checker,
        query=args.query or "",
        file_path=args.file or "",
        code=code,
        limit=args.limit,
        scopes=args.scope,
        categories=args.category,
        expected_policy_version=policy_version,
        expected_bundle_id=normalized_bundle_id,
        query_embedding=query_embedding,
        semantic_weight=ai_settings.semantic_weight,
        min_similarity=ai_settings.min_similarity,
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
            code=code,
            config=config,
            home=home,
            cwd=os.getcwd(),
        )
    ranked_results = results
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
            "index_backend": (
                "sqlite-hybrid" if query_embedding is not None else "sqlite"
            ),
            "semantic_error": semantic_error,
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
        rule = result.get("rule") if isinstance(result.get("rule"), dict) else result
        reason = "；".join(str(value) for value in result.get("reasons", ()))
        source = rule.get("source") if isinstance(rule.get("source"), dict) else {}
        _print(
            f"- [{rule.get('id', 'UNKNOWN-RULE')}] "
            f"[{rule.get('severity', 'major')}] {rule.get('statement', '')}"
        )
        _print(
            f"  来源：{source.get('document', '')} / "
            f"{source.get('section') or '未标章节'}"
        )
        metadata = rule.get("metadata") if isinstance(rule.get("metadata"), dict) else {}
        for key, label in (
            ("original_rule_id", "原始规则 ID"),
            ("level", "级别"),
            ("description", "描述"),
            ("negative_example", "反例"),
            ("positive_example", "正例"),
        ):
            value = str(metadata.get(key) or "").strip()
            if value:
                _print(f"  {label}：{value}")
        _print(f"  命中依据：{reason}")
    if semantic_error:
        _print(f"提示：语义检索不可用，已自动回退到 BM25：{semantic_error}")
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


def command_ui(args: argparse.Namespace) -> int:
    home, config = _load_context(args)
    from .studio import run_server

    run_server(
        home,
        config,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
    )
    return 0


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
    prepare.add_argument(
        "--reset-decisions",
        action="store_true",
        help="确认丢弃已修改或已删除规则上的既有审阅决定",
    )
    prepare.set_defaults(handler=command_prepare)

    review = subparsers.add_parser(
        "review", help="从候选 JSON 重新生成包含 checker 草案的审阅文件"
    )
    review.add_argument("--candidates")
    review.add_argument("--output")
    review.add_argument(
        "--reset-decisions",
        action="store_true",
        help="确认丢弃候选或 checker 已变化规则上的既有决定",
    )
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

    ui = subparsers.add_parser("ui", help="启动本机 Policy Studio")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    ui.set_defaults(handler=command_ui)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args) or 0)
    except (
        FileNotFoundError,
        ValueError,
        KeyError,
        OSError,
        PolicyAIError,
        PolicyDatabaseError,
    ) as error:
        sys.stderr.write(f"policykit: {error}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
