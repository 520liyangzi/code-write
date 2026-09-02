"""Local, dependency-free Policy Studio HTTP service.

The server intentionally binds to ``127.0.0.1`` by default, does not emit
CORS headers, and requires an explicit application header for every mutation.
It reuses the same policy extraction, review, checker, index, and compiler
functions as the command-line workflow.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from hashlib import sha256
from hmac import compare_digest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import mimetypes
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import threading
from typing import Any
from urllib.parse import unquote, urlsplit
import webbrowser

from .ai import (
    AISettings,
    PolicyAIError,
    build_embeddings_cached,
    embed_runtime_query,
    enrich_rules_cached,
)
from .checkers import PolicyChecker, validate_checker_rules
from .compiler import write_global_block
from .config import ensure_layout, resolve_path
from .database import PolicyDatabaseError, database_status, sync_database_bundle
from .extractor import extract_file
from .io_utils import utc_now, write_text
from .model import PolicyRule, ReviewDecision
from .review import (
    ReviewFormatError,
    apply_review_decisions,
    bundle_fingerprint,
    export_approved_rules,
    load_rules_json,
    read_review_decisions,
    reconcile_review_decisions,
    render_review,
    review_fingerprint,
    write_rules_json,
)
from .search import (
    PolicyIndexMetadataError,
    SQLitePolicyIndex,
    build_sqlite_index,
    retrieve_runtime_rules,
)


ALLOWED_SCOPES = frozenset({"company", "department", "project"})
ALLOWED_DOCUMENT_SUFFIXES = frozenset({".md", ".markdown"})
ALLOWED_DECISIONS = frozenset(
    {"approved", "modified", "rejected", "pending_review"}
)
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_IMPORT_FILES = 32
MAX_FILENAME_CHARS = 200
MAX_EDITED_STATEMENT_CHARS = 200_000
MAX_NOTES_CHARS = 40_000
MAX_BULK_DECISIONS = 2_000
MAX_QUERY_CHARS = 20_000
MAX_FILE_FIELD_CHARS = 8_000
MAX_CODE_CHARS = 2_000_000
_REVIEW_HASH_RE = re.compile(r"^[a-fA-F0-9]{64}$")


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"JSON constant is not allowed: {value}")


class StudioError(Exception):
    """Expected API error with a stable HTTP status and machine code."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        self.details = details

    def payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            error["details"] = self.details
        return {"ok": False, "error": error}


def _require_string(
    payload: Mapping[str, Any],
    key: str,
    *,
    maximum: int,
    required: bool = False,
) -> str:
    value = payload.get(key, "")
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise StudioError(400, "invalid_request", f"{key} 必须是字符串")
    if required and not value.strip():
        raise StudioError(400, "invalid_request", f"{key} 不能为空")
    if len(value) > maximum:
        raise StudioError(
            413,
            "field_too_large",
            f"{key} 超过允许长度 {maximum}",
        )
    return value


def _checker_value(rule: PolicyRule) -> Any:
    metadata = rule.metadata or {}
    for key in ("checks", "checkers", "check", "checker", "enforcement"):
        if key in metadata:
            return metadata[key]
    return None


def _decision_fingerprint(decision: ReviewDecision) -> str:
    canonical = json.dumps(
        {
            "rule_id": decision.rule_id,
            "decision": decision.decision,
            "review_hash": decision.review_hash,
            "edited_statement": decision.edited_statement,
            "notes": decision.notes,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _safe_document_name(value: Any) -> str:
    if not isinstance(value, str):
        raise StudioError(400, "invalid_document_name", "文件名必须是字符串")
    name = value.strip()
    if name != value:
        raise StudioError(
            400,
            "invalid_document_name",
            "文件名首尾不能包含空白字符",
        )
    if not name or len(name) > MAX_FILENAME_CHARS or "\x00" in name:
        raise StudioError(400, "invalid_document_name", "文件名为空或过长")
    windows = PureWindowsPath(name)
    posix = PurePosixPath(name)
    if (
        name in {".", ".."}
        or "/" in name
        or "\\" in name
        or windows.is_absolute()
        or bool(windows.drive)
        or posix.is_absolute()
        or windows.name != name
        or posix.name != name
        or ":" in name
        or name.endswith((".", " "))
    ):
        raise StudioError(
            400,
            "invalid_document_name",
            "文件名必须是不含路径的相对 Markdown 文件名",
        )
    if Path(name).suffix.casefold() not in ALLOWED_DOCUMENT_SUFFIXES:
        raise StudioError(
            400,
            "invalid_document_type",
            "只允许 .md 或 .markdown 文件",
        )
    reserved_stem = name.split(".", 1)[0].rstrip(" .").upper()
    reserved_names = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
    if reserved_stem in reserved_names:
        raise StudioError(
            400,
            "invalid_document_name",
            "文件名是 Windows 保留设备名",
        )
    return name


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StudioError(
            409,
            "invalid_local_state",
            f"本地 JSON 无法读取: {path}",
        ) from error
    if not isinstance(value, dict):
        raise StudioError(
            409,
            "invalid_local_state",
            f"本地 JSON 必须是对象: {path}",
        )
    return value


class PolicyStudio:
    """Application service shared by the HTTP handler and unit tests."""

    def __init__(
        self,
        home: str | Path,
        config: Mapping[str, Any],
        *,
        static_root: str | Path | None = None,
    ) -> None:
        self.home = Path(home).expanduser().resolve()
        self.config = dict(config)
        ensure_layout(self.home, self.config)
        self.static_root = (
            Path(static_root).expanduser().resolve()
            if static_root is not None
            else Path(__file__).with_name("ui").resolve()
        )
        self.mutation_lock = threading.RLock()

    def path(self, key: str) -> Path:
        return resolve_path(self.home, self.config, key)

    def _candidate_rules(self, *, required: bool = False) -> list[PolicyRule]:
        path = self.path("candidates")
        if not path.is_file():
            if required:
                raise StudioError(
                    409,
                    "not_prepared",
                    "尚未生成候选规则，请先执行 prepare",
                )
            return []
        try:
            return load_rules_json(path)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise StudioError(
                409,
                "invalid_candidates",
                f"候选规则文件无效: {path}",
            ) from error

    def _review_decisions(self, *, required: bool = False) -> dict[str, ReviewDecision]:
        path = self.path("review")
        if not path.is_file():
            if required:
                raise StudioError(
                    409,
                    "review_missing",
                    "审阅文件不存在，请先执行 prepare",
                )
            return {}
        try:
            decisions = read_review_decisions(path, strict=True)
        except (OSError, UnicodeError, ReviewFormatError, ValueError) as error:
            raise StudioError(
                409,
                "review_invalid",
                f"审阅文件无效: {error}",
            ) from error
        return {decision.rule_id: decision for decision in decisions}

    def _documents(self) -> list[dict[str, Any]]:
        source_root = self.path("source_dir")
        documents: list[dict[str, Any]] = []
        if not source_root.is_dir():
            return documents
        for path in sorted(source_root.rglob("*"), key=lambda item: str(item).casefold()):
            if not path.is_file() or path.suffix.casefold() not in ALLOWED_DOCUMENT_SUFFIXES:
                continue
            try:
                relative = path.resolve().relative_to(source_root.resolve())
            except ValueError:
                continue
            if not relative.parts or relative.parts[0].casefold() not in ALLOWED_SCOPES:
                continue
            documents.append(
                {
                    "scope": relative.parts[0].casefold(),
                    "name": relative.name,
                    "relative_path": relative.as_posix(),
                    "path": str(path.resolve()),
                    "size": path.stat().st_size,
                }
            )
        return documents

    def _rule_view(
        self,
        rule: PolicyRule,
        decision: ReviewDecision | None,
    ) -> dict[str, Any]:
        fingerprint = review_fingerprint(rule)
        effective_decision = decision or ReviewDecision(
            rule.id,
            review_hash=fingerprint,
        )
        value = rule.to_dict()
        value.update(
            {
                "checker": _checker_value(rule),
                "decision": effective_decision.decision,
                "edited_statement": effective_decision.edited_statement,
                "notes": effective_decision.notes,
                "review_hash": fingerprint,
                "decision_hash": _decision_fingerprint(effective_decision),
            }
        )
        return value

    def status(self) -> dict[str, Any]:
        with self.mutation_lock:
            return self._status_unlocked()

    def _status_unlocked(self) -> dict[str, Any]:
        candidates = self._candidate_rules()
        decisions = self._review_decisions()
        counts = Counter(
            decisions.get(rule.id, ReviewDecision(rule.id)).decision
            for rule in candidates
        )
        approved_path = self.path("approved_rules")
        active_rule_count = 0
        policy_version = ""
        bundle_id = ""
        bundle_error = ""
        if approved_path.is_file():
            approved_payload = _json_object(approved_path)
            rules = approved_payload.get("rules", [])
            if not isinstance(rules, list):
                raise StudioError(
                    409,
                    "invalid_local_state",
                    f"已激活规则格式无效: {approved_path}",
                )
            active_rule_count = sum(
                1
                for rule in rules
                if isinstance(rule, dict) and rule.get("status") == "approved"
            )
            policy_version = str(approved_payload.get("policy_version") or "")
            bundle_id = str(approved_payload.get("bundle_id") or "").casefold()
            try:
                if any(not isinstance(rule, Mapping) for rule in rules):
                    raise ValueError("rules 数组包含非对象条目")
                parsed_rules = [
                    PolicyRule.from_dict(rule)
                    for rule in rules
                ]
                if not parsed_rules:
                    raise ValueError("正式规则包不包含已批准规则")
                if any(not rule.active for rule in parsed_rules):
                    raise ValueError("正式规则包包含未批准规则")
                calculated_bundle_id = bundle_fingerprint(
                    parsed_rules,
                    policy_version,
                )
                if (
                    not _REVIEW_HASH_RE.fullmatch(bundle_id)
                    or not compare_digest(bundle_id, calculated_bundle_id)
                ):
                    bundle_error = "approved-rules.json 的 bundle_id 缺失或与规则内容不一致"
            except (TypeError, ValueError) as error:
                bundle_error = f"approved-rules.json 规则内容无效: {error}"
        index_path = self.path("search_index")
        global_path = self.path("global_block")
        index_ready = index_path.is_file()
        index_error = bundle_error
        index_metadata: dict[str, str] = {}
        if bundle_error:
            index_ready = False
        if approved_path.is_file() and index_ready:
            try:
                index_metadata = SQLitePolicyIndex(index_path).validate_metadata(
                    expected_policy_version=policy_version,
                    expected_bundle_id=(
                        bundle_id or None
                    ),
                )
            except (OSError, ValueError, sqlite3.DatabaseError) as error:
                index_ready = False
                index_error = str(error)
        paths = {
            key: str(self.path(key))
            for key in (
                "source_dir",
                "work_dir",
                "candidates",
                "review",
                "approved_rules",
                "search_index",
                "global_block",
            )
        }
        ai_settings = AISettings.from_config(self.config)
        return {
            "ok": True,
            "home": str(self.home),
            "paths": paths,
            "documents_count": len(self._documents()),
            "candidate_count": len(candidates),
            "decision_counts": dict(sorted(counts.items())),
            "pending_count": counts.get("pending_review", 0),
            "approved_count": counts.get("approved", 0) + counts.get("modified", 0),
            "active_rule_count": active_rule_count,
            "policy_version": policy_version,
            "bundle_id": bundle_id,
            "index_ready": index_ready,
            "search_index_ready": index_ready,
            "index_error": index_error,
            "embedding_count": int(index_metadata.get("embedding_count", "0") or 0),
            "embedding_model": index_metadata.get("embedding_model", ""),
            "ai": {
                "provider": ai_settings.provider,
                "llm_enabled": ai_settings.enrichment_active,
                "embedding_enabled": ai_settings.embedding_active,
            },
            "database": database_status(self.config),
            "activated": approved_path.is_file() and index_ready and global_path.is_file(),
        }

    def review(self) -> dict[str, Any]:
        candidates = self._candidate_rules()
        decisions = self._review_decisions()
        rules = [self._rule_view(rule, decisions.get(rule.id)) for rule in candidates]
        counts = Counter(rule["decision"] for rule in rules)
        return {
            "ok": True,
            "path": str(self.path("review")),
            "candidate_count": len(rules),
            "decision_counts": dict(sorted(counts.items())),
            "rules": rules,
        }

    def review_raw(self) -> dict[str, Any]:
        path = self.path("review")
        if not path.is_file():
            return {"ok": True, "path": str(path), "content": "", "exists": False}
        try:
            content = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as error:
            raise StudioError(409, "review_invalid", "审阅文件无法读取") from error
        return {"ok": True, "path": str(path), "content": content, "exists": True}

    def import_documents(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        scope = payload.get("scope")
        if not isinstance(scope, str) or scope.casefold() not in ALLOWED_SCOPES:
            raise StudioError(
                400,
                "invalid_scope",
                "scope 只能是 company、department 或 project",
            )
        scope = scope.casefold()
        files = payload.get("files")
        if not isinstance(files, list) or not files:
            raise StudioError(400, "invalid_files", "files 必须是非空数组")
        if len(files) > MAX_IMPORT_FILES:
            raise StudioError(
                413,
                "too_many_files",
                f"单次最多导入 {MAX_IMPORT_FILES} 个文件",
            )

        validated: list[tuple[str, str]] = []
        seen: set[str] = set()
        for index, item in enumerate(files):
            if not isinstance(item, Mapping):
                raise StudioError(
                    400,
                    "invalid_file",
                    f"files[{index}] 必须是对象",
                )
            name = _safe_document_name(item.get("name"))
            folded = name.casefold()
            if folded in seen:
                raise StudioError(409, "duplicate_document", f"文件名重复: {name}")
            seen.add(folded)
            content = item.get("content")
            if not isinstance(content, str):
                raise StudioError(
                    400,
                    "invalid_file_content",
                    f"文件 {name} 的 content 必须是字符串",
                )
            if "\x00" in content:
                raise StudioError(
                    400,
                    "invalid_file_content",
                    f"文件 {name} 包含 NUL 字符",
                )
            if re.search(
                r"<!--\s*(?:/?POLICYKIT-|decision\s*:)",
                content,
                re.IGNORECASE,
            ):
                raise StudioError(
                    400,
                    "reserved_review_marker",
                    f"文件 {name} 包含 Policy Kit 保留审阅标记",
                )
            size = len(content.encode("utf-8"))
            if size > MAX_FILE_BYTES:
                raise StudioError(
                    413,
                    "file_too_large",
                    f"文件 {name} 超过 {MAX_FILE_BYTES} 字节",
                )
            validated.append((name, content))

        with self.mutation_lock:
            source_root = self.path("source_dir")
            scope_root = (source_root / scope).resolve()
            try:
                scope_root.relative_to(source_root.resolve())
            except ValueError as error:
                raise StudioError(400, "invalid_scope_path", "scope 路径无效") from error
            targets = [(scope_root / name).resolve() for name, _ in validated]
            for target in targets:
                try:
                    target.relative_to(scope_root)
                except ValueError as error:
                    raise StudioError(
                        400, "invalid_document_name", "文件路径越界"
                    ) from error
                if target.exists():
                    raise StudioError(
                        409,
                        "document_exists",
                        f"同名文件已存在，不会覆盖: {target.name}",
                    )
            scope_root.mkdir(parents=True, exist_ok=True)
            imported: list[dict[str, Any]] = []
            for (name, content), target in zip(validated, targets):
                write_text(target, content)
                imported.append(
                    {
                        "scope": scope,
                        "name": name,
                        "path": str(target),
                        "size": target.stat().st_size,
                    }
                )
        return {"ok": True, "imported_count": len(imported), "documents": imported}

    def prepare(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        confirm_reset = payload.get("confirm_reset", False)
        if not isinstance(confirm_reset, bool):
            raise StudioError(
                400, "invalid_confirm_reset", "confirm_reset 必须是布尔值"
        )
        with self.mutation_lock:
            previous_candidates = self._candidate_rules()
            previous = (
                self._review_decisions() if self.path("review").is_file() else {}
            )
            documents = self._documents()
            if not documents:
                raise StudioError(
                    400,
                    "no_documents",
                    "没有可处理的 Markdown 规范文件",
                )
            rules: list[PolicyRule] = []
            for document in documents:
                path = Path(document["path"])
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise StudioError(
                        413,
                        "file_too_large",
                        f"规范文件超过 {MAX_FILE_BYTES} 字节: {document['relative_path']}",
                    )
                try:
                    source_text = path.read_text(encoding="utf-8-sig")
                except (OSError, UnicodeError) as error:
                    raise StudioError(
                        400,
                        "invalid_document",
                        f"规范文件无法按 UTF-8 读取: {document['relative_path']}",
                    ) from error
                if re.search(
                    r"<!--\s*(?:/?POLICYKIT-|decision\s*:)",
                    source_text,
                    re.IGNORECASE,
                ):
                    raise StudioError(
                        400,
                        "reserved_review_marker",
                        f"规范文件包含 Policy Kit 保留审阅标记: {document['relative_path']}",
                    )
                rules.extend(
                    extract_file(
                        path,
                        scope=document["scope"],
                        id_prefix="AUTO",
                        source_name=document["relative_path"],
                    )
                )

            warnings: list[str] = []
            ai_settings = AISettings.from_config(self.config)
            ai_stats = {"enabled": 0, "cached": 0, "generated": 0}
            try:
                ai_stats = enrich_rules_cached(
                    rules,
                    ai_settings,
                    self.path("ai_cache"),
                )
            except (PolicyAIError, OSError, ValueError) as error:
                if ai_settings.required:
                    raise StudioError(
                        503,
                        "ai_enrichment_failed",
                        f"大模型规则增强失败：{error}",
                    ) from error
                warnings.append(f"大模型规则增强失败，已使用本地解析结果：{error}")

            preserved, resettable_decisions = reconcile_review_decisions(
                previous_candidates,
                previous,
                rules,
            )
            reset_decision_counts = Counter(
                decision.decision for decision in resettable_decisions
            )
            if reset_decision_counts and not confirm_reset:
                raise StudioError(
                    409,
                    "review_decisions_exist",
                    "部分已决定规则已修改或删除；确认丢弃这些旧决定后重试",
                    details={
                        "decision_counts": dict(
                            sorted(reset_decision_counts.items())
                        ),
                        "preserved_count": len(preserved),
                    },
                )

            review_text = render_review(rules, preserved)
            candidates_path = self.path("candidates")
            review_path = self.path("review")
            write_rules_json(rules, candidates_path, approved_only=False)
            write_text(review_path, review_text)
        return {
            "ok": True,
            "documents_count": len(documents),
            "candidate_count": len(rules),
            "reset_decision_count": len(resettable_decisions),
            "preserved_decision_count": len(preserved),
            "new_pending_count": len(rules) - len(preserved),
            "ai_enrichment": ai_stats,
            "warnings": warnings,
            "paths": {
                "candidates": str(candidates_path),
                "review": str(review_path),
            },
        }

    def save_decision(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        rule_id = _require_string(payload, "rule_id", maximum=256, required=True).strip()
        decision_name = _require_string(
            payload, "decision", maximum=32, required=True
        ).casefold()
        if decision_name not in ALLOWED_DECISIONS:
            raise StudioError(
                400,
                "invalid_decision",
                "decision 只能是 approved、modified、rejected 或 pending_review",
            )
        submitted_hash = _require_string(
            payload, "review_hash", maximum=64, required=True
        ).casefold()
        if not _REVIEW_HASH_RE.fullmatch(submitted_hash):
            raise StudioError(400, "invalid_review_hash", "review_hash 格式无效")
        submitted_decision_hash = _require_string(
            payload, "decision_hash", maximum=64, required=True
        ).casefold()
        if not _REVIEW_HASH_RE.fullmatch(submitted_decision_hash):
            raise StudioError(
                400,
                "invalid_decision_hash",
                "decision_hash 格式无效",
            )
        edited_statement = _require_string(
            payload,
            "edited_statement",
            maximum=MAX_EDITED_STATEMENT_CHARS,
        )
        notes = _require_string(payload, "notes", maximum=MAX_NOTES_CHARS)
        if decision_name == "modified" and not edited_statement.strip():
            raise StudioError(
                400,
                "modified_statement_required",
                "modified 决定必须填写 edited_statement",
            )

        with self.mutation_lock:
            candidates = self._candidate_rules(required=True)
            candidates_by_id = {rule.id: rule for rule in candidates}
            rule = candidates_by_id.get(rule_id)
            if rule is None:
                raise StudioError(404, "rule_not_found", f"候选规则不存在: {rule_id}")
            current_hash = review_fingerprint(rule)
            if not compare_digest(submitted_hash, current_hash):
                raise StudioError(
                    409,
                    "stale_review",
                    "候选规则已变化，请刷新页面后重新审阅",
                    details={"current_review_hash": current_hash},
                )
            decisions = self._review_decisions()
            current_decision = decisions.get(
                rule_id,
                ReviewDecision(rule_id, review_hash=current_hash),
            )
            current_decision_hash = _decision_fingerprint(current_decision)
            if not compare_digest(submitted_decision_hash, current_decision_hash):
                raise StudioError(
                    409,
                    "stale_decision",
                    "该规则的审批决定已被其他页面更新，请刷新后重试",
                    details={"current_decision_hash": current_decision_hash},
                )
            for candidate in candidates:
                existing = decisions.get(candidate.id)
                if (
                    existing is not None
                    and existing.review_hash
                    and not compare_digest(
                        existing.review_hash, review_fingerprint(candidate)
                    )
                ):
                    raise StudioError(
                        409,
                        "stale_review",
                        f"规则 {candidate.id} 的候选内容已变化，请刷新后重试",
                    )
            decisions[rule_id] = ReviewDecision(
                rule_id=rule_id,
                decision=decision_name,
                review_hash=current_hash,
                edited_statement=edited_statement,
                notes=notes,
            )
            try:
                review_text = render_review(candidates, decisions)
            except ReviewFormatError as error:
                raise StudioError(400, "invalid_decision", str(error)) from error
            review_path = self.path("review")
            write_text(review_path, review_text)
            persisted = self._review_decisions(required=True).get(rule_id)
            if persisted is None or persisted.decision != decision_name:
                raise StudioError(
                    500,
                    "decision_persistence_failed",
                    "审阅决定写入后校验失败",
                )
        return {
            "ok": True,
            "path": str(review_path),
            "rule": self._rule_view(rule, persisted),
        }

    def approve_rules(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Atomically approve a reviewed set with optimistic-lock hashes."""

        items = payload.get("rules")
        if not isinstance(items, list) or not items:
            raise StudioError(400, "invalid_rules", "rules 必须是非空数组")
        if len(items) > MAX_BULK_DECISIONS:
            raise StudioError(
                413,
                "too_many_rules",
                f"单次最多批量批准 {MAX_BULK_DECISIONS} 条规则",
            )
        submitted: dict[str, tuple[str, str]] = {}
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                raise StudioError(
                    400, "invalid_rule", f"rules[{index}] 必须是对象"
                )
            rule_id = _require_string(
                item, "rule_id", maximum=256, required=True
            ).strip()
            review_hash = _require_string(
                item, "review_hash", maximum=64, required=True
            ).casefold()
            decision_hash = _require_string(
                item, "decision_hash", maximum=64, required=True
            ).casefold()
            if not _REVIEW_HASH_RE.fullmatch(review_hash) or not _REVIEW_HASH_RE.fullmatch(
                decision_hash
            ):
                raise StudioError(
                    400, "invalid_hash", f"规则 {rule_id} 的审批哈希格式无效"
                )
            if rule_id in submitted:
                raise StudioError(
                    409, "duplicate_rule", f"批量审批规则重复: {rule_id}"
                )
            submitted[rule_id] = (review_hash, decision_hash)

        with self.mutation_lock:
            candidates = self._candidate_rules(required=True)
            candidates_by_id = {rule.id: rule for rule in candidates}
            decisions = self._review_decisions(required=True)
            for rule_id, (submitted_review, submitted_decision) in submitted.items():
                rule = candidates_by_id.get(rule_id)
                if rule is None:
                    raise StudioError(
                        404, "rule_not_found", f"候选规则不存在: {rule_id}"
                    )
                current_review = review_fingerprint(rule)
                if not compare_digest(submitted_review, current_review):
                    raise StudioError(
                        409,
                        "stale_review",
                        f"规则 {rule_id} 已变化，请刷新后重试",
                    )
                current = decisions.get(
                    rule_id,
                    ReviewDecision(rule_id, review_hash=current_review),
                )
                if current.decision != "pending_review":
                    raise StudioError(
                        409,
                        "decision_not_pending",
                        f"规则 {rule_id} 已有决定，不会被批量覆盖",
                    )
                if not compare_digest(
                    submitted_decision, _decision_fingerprint(current)
                ):
                    raise StudioError(
                        409,
                        "stale_decision",
                        f"规则 {rule_id} 已在其他页面更新，请刷新后重试",
                    )
            for rule_id in submitted:
                rule = candidates_by_id[rule_id]
                current = decisions.get(rule_id)
                decisions[rule_id] = ReviewDecision(
                    rule_id=rule_id,
                    decision="approved",
                    review_hash=review_fingerprint(rule),
                    notes=current.notes if current else "",
                )
            review_text = render_review(candidates, decisions)
            review_path = self.path("review")
            write_text(review_path, review_text)
            persisted = self._review_decisions(required=True)
        return {
            "ok": True,
            "path": str(review_path),
            "approved_count": len(submitted),
            "rules": [
                self._rule_view(candidates_by_id[rule_id], persisted[rule_id])
                for rule_id in submitted
            ],
        }

    def activate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        policy_version = payload.get("policy_version")
        if policy_version is not None:
            if not isinstance(policy_version, str) or not policy_version.strip():
                raise StudioError(
                    400, "invalid_policy_version", "policy_version 必须是非空字符串"
                )
            if len(policy_version) > 200:
                raise StudioError(
                    413, "field_too_large", "policy_version 超过允许长度 200"
                )
            policy_version = policy_version.strip()
        else:
            policy_version = utc_now()

        with self.mutation_lock:
            candidates = self._candidate_rules(required=True)
            decisions = self._review_decisions(required=True)
            try:
                reviewed = apply_review_decisions(candidates, decisions, strict=True)
            except ReviewFormatError as error:
                raise StudioError(409, "review_invalid", str(error)) from error
            approved = [rule for rule in reviewed if rule.status == "approved"]
            if not approved:
                raise StudioError(
                    400,
                    "no_approved_rules",
                    "至少批准一条规则后才能激活",
                )
            checker_errors = validate_checker_rules(approved)
            if checker_errors:
                raise StudioError(
                    400,
                    "invalid_checker",
                    "已批准规则的 checker 校验失败",
                    details=checker_errors[:20],
                )

            work_path = self.path("work_dir")
            reviewed_path = work_path / "reviewed-rules.json"
            approved_path = self.path("approved_rules")
            index_path = self.path("search_index")
            global_path = self.path("global_block")
            limit = int(self.config.get("review", {}).get("global_core_limit", 40))
            bundle_id = bundle_fingerprint(approved, policy_version)
            warnings: list[str] = []
            ai_settings = AISettings.from_config(self.config)
            embeddings: dict[str, list[float]] = {}
            embedding_stats = {"enabled": 0, "cached": 0, "generated": 0}
            try:
                embeddings, embedding_stats = build_embeddings_cached(
                    approved,
                    ai_settings,
                    self.path("embedding_cache"),
                )
            except (PolicyAIError, OSError, ValueError) as error:
                if ai_settings.required:
                    raise StudioError(
                        503,
                        "embedding_failed",
                        f"规则向量生成失败：{error}",
                    ) from error
                warnings.append(f"规则向量生成失败，已生成纯 BM25 索引：{error}")
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
                embedding_model=(
                    ai_settings.embedding_model if embeddings else None
                ),
            )
            write_global_block(approved, global_path, limit=limit)
            try:
                database = sync_database_bundle(
                    self.config,
                    self.home,
                    approved,
                    policy_version=policy_version,
                    bundle_id=bundle_id,
                    embeddings=embeddings,
                )
            except (PolicyDatabaseError, OSError, ValueError) as error:
                raise StudioError(
                    503,
                    "database_sync_failed",
                    f"数据库同步失败：{error}",
                ) from error
            if database.get("error"):
                warnings.append(str(database["error"]))
        return {
            "ok": True,
            "policy_version": policy_version,
            "counts": {
                "candidates": len(candidates),
                "approved": len(approved),
                "not_approved": len(reviewed) - len(approved),
            },
            "embedding": embedding_stats,
            "database": database,
            "warnings": warnings,
            "paths": {
                "reviewed_rules": str(reviewed_path),
                "approved_rules": str(approved_path),
                "search_index": str(index_path),
                "global_block": str(global_path),
            },
        }

    def search(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self.mutation_lock:
            return self._search_unlocked(payload)

    def _search_unlocked(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        query = _require_string(payload, "query", maximum=MAX_QUERY_CHARS)
        file_path = _require_string(payload, "file", maximum=MAX_FILE_FIELD_CHARS)
        code = _require_string(payload, "code", maximum=MAX_CODE_CHARS)
        limit_value = payload.get("limit", 20)
        if isinstance(limit_value, bool) or not isinstance(limit_value, int):
            raise StudioError(400, "invalid_limit", "limit 必须是整数")
        if limit_value < 1 or limit_value > 100:
            raise StudioError(400, "invalid_limit", "limit 必须在 1 到 100 之间")

        scopes = payload.get("scopes")
        if scopes is not None:
            if not isinstance(scopes, list) or any(
                not isinstance(value, str) or value.casefold() not in ALLOWED_SCOPES
                for value in scopes
            ):
                raise StudioError(
                    400,
                    "invalid_scopes",
                    "scopes 必须是 company、department、project 组成的数组",
                )
            scopes = [value.casefold() for value in scopes]

        categories = payload.get("categories")
        if categories is not None:
            if (
                not isinstance(categories, list)
                or len(categories) > 32
                or any(
                    not isinstance(value, str)
                    or not value.strip()
                    or len(value) > 80
                    for value in categories
                )
            ):
                raise StudioError(
                    400,
                    "invalid_categories",
                    "categories 必须是非空短字符串组成的数组",
                )
            categories = [value.strip().casefold() for value in categories]

        approved_path = self.path("approved_rules")
        index_path = self.path("search_index")
        if not approved_path.is_file():
            raise StudioError(
                409,
                "not_activated",
                "尚无已激活规则，请先完成审批和激活",
            )
        if not index_path.is_file():
            raise StudioError(
                409,
                "search_index_missing",
                "已激活检索索引不存在，请重新激活规则",
            )
        approved_payload = _json_object(approved_path)
        approved_rules = approved_payload.get("rules", [])
        if not isinstance(approved_rules, list):
            raise StudioError(
                409,
                "search_bundle_invalid",
                "已激活规则包格式无效，请重新激活规则",
            )
        policy_version = str(approved_payload.get("policy_version") or "")
        bundle_id = str(approved_payload.get("bundle_id") or "").casefold()
        try:
            if any(not isinstance(rule, Mapping) for rule in approved_rules):
                raise ValueError("rules 数组包含非对象条目")
            parsed_rules = [
                PolicyRule.from_dict(rule)
                for rule in approved_rules
            ]
            if not parsed_rules:
                raise ValueError("正式规则包不包含已批准规则")
            if any(not rule.active for rule in parsed_rules):
                raise ValueError("正式规则包包含未批准规则")
            calculated_bundle_id = bundle_fingerprint(parsed_rules, policy_version)
        except (TypeError, ValueError) as error:
            raise StudioError(
                409,
                "search_bundle_invalid",
                "已激活规则包内容无效，请重新激活规则",
            ) from error
        if (
            not _REVIEW_HASH_RE.fullmatch(bundle_id)
            or not compare_digest(bundle_id, calculated_bundle_id)
        ):
            raise StudioError(
                409,
                "search_bundle_invalid",
                "已激活规则包 bundle_id 与内容不一致，请重新激活规则",
            )
        checker = PolicyChecker(
            approved_rules,
            fail_closed=True,
            block_severities=self.config.get("runtime", {}).get(
                "block_severities", ("blocker", "major")
            ),
        )
        try:
            index_metadata = SQLitePolicyIndex(index_path).validate_metadata(
                expected_policy_version=policy_version,
                expected_bundle_id=bundle_id or None,
            )
        except (OSError, ValueError, sqlite3.DatabaseError) as error:
            raise StudioError(
                409,
                "search_index_invalid",
                "已激活检索索引损坏或与规则包不一致，请重新激活规则",
                details={"reason": str(error)},
            ) from error
        ai_settings = AISettings.from_config(self.config)
        try:
            query_embedding, semantic_error = embed_runtime_query(
                ai_settings,
                query=query,
                file_path=file_path,
                code=code,
                index_metadata=index_metadata,
            )
        except (PolicyAIError, OSError, ValueError) as error:
            raise StudioError(
                503,
                "semantic_search_failed",
                f"语义检索查询向量生成失败：{error}",
            ) from error
        try:
            results = retrieve_runtime_rules(
                index_path,
                checker,
                query=query,
                file_path=file_path,
                code=code,
                limit=limit_value,
                scopes=scopes,
                categories=categories,
                expected_policy_version=policy_version,
                expected_bundle_id=bundle_id or None,
                query_embedding=query_embedding,
                semantic_weight=ai_settings.semantic_weight,
                min_similarity=ai_settings.min_similarity,
            )
        except (
            OSError,
            ValueError,
            sqlite3.DatabaseError,
            PolicyIndexMetadataError,
        ) as error:
            raise StudioError(
                409,
                "search_index_invalid",
                "已激活检索索引损坏或与规则包不一致，请重新激活规则",
                details={"reason": str(error)},
            ) from error
        return {
            "ok": True,
            "status": "matched" if results else "no_applicable_rule",
            "policy_version": policy_version,
            "bundle_id": bundle_id,
            "index_backend": (
                "sqlite-hybrid" if query_embedding is not None else "sqlite"
            ),
            "index_path": str(index_path),
            "semantic_used": query_embedding is not None,
            "semantic_error": semantic_error,
            "result_count": len(results),
            "results": results,
        }


class PolicyStudioHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        studio: PolicyStudio,
    ) -> None:
        self.studio = studio
        super().__init__(server_address, PolicyStudioHandler)


class PolicyStudioHandler(BaseHTTPRequestHandler):
    server: PolicyStudioHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "PolicyKitStudio/1"

    def _security_headers(self, *, static: bool = False) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.send_header("Cache-Control", "no-store" if not static else "no-cache")

    def _validate_host(self) -> None:
        values = self.headers.get_all("Host") or []
        if len(values) != 1:
            raise StudioError(400, "invalid_host", "请求必须包含一个 Host 头")
        try:
            parsed = urlsplit("http://" + values[0])
            hostname = (parsed.hostname or "").casefold()
            requested_port = parsed.port
        except ValueError as error:
            raise StudioError(400, "invalid_host", "Host 头无效") from error
        bound_host = str(self.server.server_address[0]).casefold()
        allowed = {"127.0.0.1", "localhost", "::1", bound_host}
        if hostname not in allowed:
            raise StudioError(403, "host_not_allowed", "Host 不属于本机 Studio")
        if requested_port is not None and requested_port != self.server.server_port:
            raise StudioError(403, "host_not_allowed", "Host 端口与 Studio 不一致")

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(encoded)

    def _send_error(self, error: StudioError) -> None:
        self._send_json(error.status, error.payload())

    def _path(self) -> str:
        raw = urlsplit(self.path).path
        try:
            return unquote(raw, errors="strict")
        except (UnicodeError, ValueError) as error:
            raise StudioError(400, "invalid_path", "请求路径编码无效") from error

    def _read_json(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().casefold() != "application/json":
            self.close_connection = True
            raise StudioError(
                415,
                "unsupported_media_type",
                "POST 必须使用 Content-Type: application/json",
            )
        if self.headers.get("X-PolicyKit-Studio") != "1":
            self.close_connection = True
            raise StudioError(
                403,
                "studio_header_required",
                "缺少 X-PolicyKit-Studio: 1 请求头",
            )
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise StudioError(400, "invalid_request", "不支持 Transfer-Encoding")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self.close_connection = True
            raise StudioError(411, "length_required", "缺少 Content-Length")
        try:
            length = int(raw_length)
        except ValueError as error:
            self.close_connection = True
            raise StudioError(400, "invalid_content_length", "Content-Length 无效") from error
        if length < 0:
            self.close_connection = True
            raise StudioError(400, "invalid_content_length", "Content-Length 无效")
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            raise StudioError(
                413,
                "body_too_large",
                f"请求体超过 {MAX_BODY_BYTES} 字节",
            )
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise StudioError(400, "incomplete_body", "请求体不完整")
        try:
            value = json.loads(
                raw.decode("utf-8"), parse_constant=_reject_json_constant
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise StudioError(400, "invalid_json", "请求体不是有效 UTF-8 JSON") from error
        if not isinstance(value, dict):
            raise StudioError(400, "invalid_json", "JSON 根节点必须是对象")
        return value

    def _serve_static(self, request_path: str) -> None:
        root = self.server.studio.static_root.resolve()
        if "\x00" in request_path:
            raise StudioError(400, "invalid_path", "静态路径无效")
        relative = request_path.lstrip("/") or "index.html"
        windows = PureWindowsPath(relative)
        parts = [part for part in re.split(r"[/\\]+", relative) if part]
        if (
            windows.is_absolute()
            or bool(windows.drive)
            or any(part in {".", ".."} for part in parts)
        ):
            raise StudioError(404, "not_found", "静态资源不存在")
        candidate = root.joinpath(*parts).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise StudioError(404, "not_found", "静态资源不存在") from error
        if candidate.is_dir():
            candidate = (candidate / "index.html").resolve()
            try:
                candidate.relative_to(root)
            except ValueError as error:
                raise StudioError(404, "not_found", "静态资源不存在") from error
        if not candidate.is_file():
            raise StudioError(404, "not_found", "静态资源不存在")
        try:
            content = candidate.read_bytes()
        except OSError as error:
            raise StudioError(404, "not_found", "静态资源无法读取") from error
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
        }:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self._security_headers(static=True)
        self.end_headers()
        self.wfile.write(content)

    def _dispatch_get(self) -> None:
        path = self._path()
        studio = self.server.studio
        routes = {
            "/api/status": studio.status,
            "/api/review": studio.review,
            "/api/review/raw": studio.review_raw,
        }
        handler = routes.get(path)
        if handler is not None:
            self._send_json(HTTPStatus.OK, handler())
            return
        if path.startswith("/api/"):
            raise StudioError(404, "not_found", "API 不存在")
        self._serve_static(path)

    def _dispatch_post(self) -> None:
        path = self._path()
        payload = self._read_json()
        studio = self.server.studio
        routes = {
            "/api/documents/import": studio.import_documents,
            "/api/prepare": studio.prepare,
            "/api/review/decision": studio.save_decision,
            "/api/review/approve": studio.approve_rules,
            "/api/activate": studio.activate,
            "/api/search": studio.search,
        }
        handler = routes.get(path)
        if handler is None:
            raise StudioError(404, "not_found", "API 不存在")
        self._send_json(HTTPStatus.OK, handler(payload))

    def _run(self, callback: Any) -> None:
        try:
            self._validate_host()
            callback()
        except StudioError as error:
            self._send_error(error)
        except (ReviewFormatError, ValueError, KeyError) as error:
            self._send_error(StudioError(400, "invalid_request", str(error)))
        except FileNotFoundError as error:
            self._send_error(StudioError(404, "not_found", str(error)))
        except Exception as error:  # pragma: no cover - defensive HTTP boundary
            self.log_error("Unhandled Policy Studio error: %r", error)
            self._send_error(
                StudioError(500, "internal_error", "Policy Studio 内部错误")
            )

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self._run(self._dispatch_get)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self._run(self._dispatch_post)

    def _method_not_allowed(self) -> None:
        self._send_error(StudioError(405, "method_not_allowed", "请求方法不允许"))

    do_DELETE = _method_not_allowed
    do_HEAD = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_PUT = _method_not_allowed

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the standard, local-only request log while giving it a stable tag.
        super().log_message("[Policy Studio] " + format, *args)


def create_server(
    home: str | Path,
    config: Mapping[str, Any],
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    static_root: str | Path | None = None,
) -> PolicyStudioHTTPServer:
    resolved_host = str(host or "").strip()
    if not resolved_host or any(character.isspace() for character in resolved_host):
        raise ValueError("host 无效")
    if resolved_host.casefold() != "localhost":
        try:
            address = ipaddress.ip_address(resolved_host)
        except ValueError as error:
            raise ValueError(
                "Policy Studio 只允许绑定 localhost 或 IPv4 回环地址"
            ) from error
        if address.version != 4 or not address.is_loopback:
            raise ValueError(
                "Policy Studio 只允许绑定 localhost 或 IPv4 回环地址"
            )
    resolved_port = int(port)
    if resolved_port < 0 or resolved_port > 65535:
        raise ValueError("port 必须在 0 到 65535 之间")
    studio = PolicyStudio(home, config, static_root=static_root)
    return PolicyStudioHTTPServer((resolved_host, resolved_port), studio)


def run_server(
    home: str | Path,
    config: Mapping[str, Any],
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    static_root: str | Path | None = None,
) -> None:
    server = create_server(
        home,
        config,
        host=host,
        port=port,
        static_root=static_root,
    )
    actual_host, actual_port = server.server_address[:2]
    display_host = actual_host
    url_host = f"[{display_host}]" if ":" in display_host else display_host
    url = f"http://{url_host}:{actual_port}/"
    print(f"Policy Studio: {url}")
    if open_browser:
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


__all__ = [
    "ALLOWED_SCOPES",
    "MAX_BODY_BYTES",
    "MAX_FILE_BYTES",
    "PolicyStudio",
    "PolicyStudioHTTPServer",
    "StudioError",
    "create_server",
    "run_server",
]
