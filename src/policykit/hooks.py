"""Claude Code / Codagent compatible policy hooks.

Public integration entry points are :func:`handle_hook` and
:func:`main_hook`.  The first pre-write attempt for a Java file is denied with
the applicable policy context.  That denial issues a short-lived, one-use
receipt; the retry is allowed through the normal host permission flow.  A
successful post-write hook consumes the receipt, checks the resulting file and
records evidence in the audit trail.

The optional CodeGraph status in an input payload is recorded when present,
but CodeGraph is never imported, invoked or required here.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, IO, Iterable, Mapping
from uuid import uuid4

from .ai import AISettings, PolicyAIError, embed_runtime_query
from .audit import AuditTrail, safe_session_id
from .checkers import CheckResult, PolicyChecker
from .model import PolicyRule
from .review import bundle_fingerprint
from .search import (
    SQLitePolicyIndex,
    retrieve_runtime_rules,
)


HOOK_SCHEMA_VERSION = 1
WRITE_TOOLS = frozenset({"edit", "write", "multiedit", "notebookedit"})
SHELL_TOOLS = frozenset(
    {"bash", "shell", "powershell", "command", "exec_command", "exec-command"}
)
DEFAULT_MANAGED_EXTENSIONS = (
    ".java",
    ".xml",
    ".yml",
    ".yaml",
    ".properties",
)
_STATE_THREAD_LOCK = threading.RLock()


@contextmanager
def _state_file_lock(state_path: Path, *, timeout_seconds: float = 15.0):
    """Serialize a session's read/modify/write state across hook processes."""

    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _STATE_THREAD_LOCK:
        handle = lock_path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            started = time.monotonic()
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() - started >= timeout_seconds:
                        raise TimeoutError(
                            f"等待会话状态锁超时：{lock_path}"
                        )
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return _text(value).lower() not in {"", "0", "false", "no", "off", "none"}


def _nested(config: Mapping[str, Any], section: str, key: str, default: Any = None) -> Any:
    group = config.get(section)
    if isinstance(group, Mapping) and key in group:
        return group[key]
    dotted = f"{section}.{key}"
    if dotted in config:
        return config[dotted]
    return default


def _default_home() -> Path:
    explicit = os.environ.get("POLICYKIT_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / ".local" / "share"
    return (base / "CodagentJavaPolicy").resolve()


def _resolve_home(home: str | Path | None) -> Path:
    return Path(home).expanduser().resolve() if home is not None else _default_home()


def _resolve_config_path(home: Path) -> Path | None:
    explicit = os.environ.get("POLICYKIT_CONFIG")
    if explicit:
        return Path(explicit).expanduser().resolve()
    for candidate in (
        home / "policykit.json",
        home / "config.json",
        home / "runtime.json",
    ):
        if candidate.is_file():
            return candidate
    return None


def load_runtime_config(home: str | Path | None = None) -> dict[str, Any]:
    """Load optional JSON runtime configuration.

    The package-level CLI normally passes a configuration mapping directly;
    this loader exists so ``python -m policykit.hooks`` is independently
    useful and easy to diagnose.
    """

    root = _resolve_home(home)
    path = _resolve_config_path(root)
    if path is None:
        return {}
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping):
        raise ValueError(f"runtime config must be a JSON object: {path}")
    return dict(value)


def _path_from_config(
    config: Mapping[str, Any],
    home: Path,
    key: str,
    default: str,
    *,
    env: str | tuple[str, ...] | None = None,
) -> Path:
    env_names = (env,) if isinstance(env, str) else (env or ())
    value = next((os.environ[name] for name in env_names if os.environ.get(name)), "")
    if not value:
        value = _text(_nested(config, "paths", key, default))
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (home / path).resolve()


def _load_rule_bundle(
    path: Path,
) -> tuple[list[dict[str, Any]], str, str, str | None]:
    """Return ``(rules, policy_version, bundle_id, error)`` for hooks."""

    if not path.is_file():
        return [], "unavailable", "", f"已审核规则文件不存在：{path}"
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        return [], "unavailable", "", f"无法读取已审核规则：{exc}"
    if isinstance(payload, list):
        raw_rules = payload
        version = "1"
        bundle_id = ""
    elif isinstance(payload, Mapping):
        raw_rules = payload.get("rules", [])
        version = _text(
            payload.get("policy_version") or payload.get("schema_version") or "1"
        )
        bundle_id = _text(payload.get("bundle_id"))
    else:
        return [], "unavailable", "", "已审核规则文件必须是 JSON 对象或数组"
    if not isinstance(raw_rules, list):
        return [], version, bundle_id, "已审核规则的 rules 字段必须是数组"
    if not re.fullmatch(r"[0-9a-fA-F]{64}", bundle_id):
        return (
            [],
            version,
            bundle_id,
            "已审核规则缺少有效的 64 位 bundle_id；请重新激活规则包",
        )
    if not all(isinstance(item, Mapping) for item in raw_rules):
        return [], version, bundle_id, "已审核规则的 rules 元素必须是 JSON 对象"
    try:
        parsed_rules = [PolicyRule.from_dict(item) for item in raw_rules]
    except (TypeError, ValueError) as exc:
        return [], version, bundle_id, f"已审核规则内容无效：{exc}"
    if not parsed_rules:
        return [], version, bundle_id, "正式规则包不包含已批准规则；请重新激活"
    if any(not rule.active for rule in parsed_rules):
        return [], version, bundle_id, "正式规则包包含未批准规则；请重新激活"
    bundle_id = bundle_id.casefold()
    if bundle_fingerprint(parsed_rules, version) != bundle_id:
        return (
            [],
            version,
            bundle_id,
            "已审核规则内容与 bundle_id 不一致；规则包可能不完整或被修改",
        )
    return (
        [dict(item) for item in raw_rules],
        version,
        bundle_id,
        None,
    )


def _search_cards(
    search_index: Path,
    checker: PolicyChecker,
    *,
    query: str,
    file_path: str,
    code: str,
    limit: int,
    expected_policy_version: str,
    expected_bundle_id: str,
    query_embedding: Iterable[float] | None = None,
    semantic_weight: float = 0.4,
    min_similarity: float = 0.28,
) -> list[dict[str, Any]]:
    """Retrieve only from the activated index and merge checker applicability."""

    return retrieve_runtime_rules(
        search_index,
        checker,
        query=query,
        file_path=file_path,
        code=code,
        limit=limit,
        expected_policy_version=expected_policy_version,
        expected_bundle_id=expected_bundle_id or None,
        query_embedding=(
            tuple(float(value) for value in query_embedding)
            if query_embedding is not None
            else None
        ),
        semantic_weight=semantic_weight,
        min_similarity=min_similarity,
    )


def _event_name(value: Any) -> str:
    text = _text(value).lower().replace("_", "-")
    aliases = {
        "pre": "pre-edit",
        "pretooluse": "pre-edit",
        "pre-tool-use": "pre-edit",
        "post": "post-edit",
        "posttooluse": "post-edit",
        "post-tool-use": "post-edit",
        "stop": "stop",
    }
    return aliases.get(text, text)


def _session_id(payload: Mapping[str, Any]) -> str:
    explicit = _text(payload.get("session_id") or payload.get("sessionId"))
    if explicit:
        return safe_session_id(explicit)
    seed = _text(payload.get("transcript_path") or payload.get("transcriptPath"))
    if not seed:
        seed = f"{payload.get('cwd', '')}|{os.getppid()}"
    return f"session-{sha256(seed.encode('utf-8')).hexdigest()[:16]}"


def _tool_name(payload: Mapping[str, Any]) -> str:
    return _text(payload.get("tool_name") or payload.get("toolName")).lower()


def _tool_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("tool_input")
    if not isinstance(value, Mapping):
        value = payload.get("toolInput")
    return _mapping(value)


def _target_path(payload: Mapping[str, Any]) -> Path | None:
    tool_input = _tool_input(payload)
    raw = _text(
        tool_input.get("file_path")
        or tool_input.get("filePath")
        or tool_input.get("path")
        or tool_input.get("notebook_path")
    )
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        cwd = Path(_text(payload.get("cwd")) or os.getcwd()).expanduser()
        path = cwd / path
    # strict=False is intentional for a proposed new Write target.
    return path.resolve(strict=False)


def _display_path(path: Path, payload: Mapping[str, Any]) -> str:
    cwd = Path(_text(payload.get("cwd")) or os.getcwd()).expanduser().resolve()
    try:
        return path.relative_to(cwd).as_posix()
    except ValueError:
        return path.as_posix()


def _file_extensions(config: Mapping[str, Any]) -> tuple[str, ...]:
    value = _nested(config, "runtime", "file_extensions", DEFAULT_MANAGED_EXTENSIONS)
    if isinstance(value, str):
        values: Iterable[Any] = (value,)
    else:
        values = value or DEFAULT_MANAGED_EXTENSIONS
    extensions = []
    for item in values:
        extension = _text(item).lower()
        if extension and not extension.startswith("."):
            extension = "." + extension
        if extension:
            extensions.append(extension)
    return tuple(extensions or DEFAULT_MANAGED_EXTENSIONS)


def _is_managed_write(payload: Mapping[str, Any], config: Mapping[str, Any]) -> bool:
    name = _tool_name(payload)
    if name and name not in WRITE_TOOLS:
        return False
    target = _target_path(payload)
    return target is not None and target.suffix.lower() in _file_extensions(config)


def _looks_like_managed_shell_write(
    payload: Mapping[str, Any], config: Mapping[str, Any]
) -> bool:
    """Catch common shell writes so managed files use observable edit tools.

    Hooks cannot prove the side effects of an arbitrary executable.  This is a
    guardrail for direct redirection and common file-mutating commands, not an
    operating-system sandbox.
    """

    if _tool_name(payload) not in SHELL_TOOLS:
        return False
    tool_input = _tool_input(payload)
    command = _text(
        tool_input.get("command")
        or tool_input.get("cmd")
        or tool_input.get("script")
        or tool_input.get("code")
    )
    if not command:
        return False
    lower = command.casefold()
    if not any(extension.casefold() in lower for extension in _file_extensions(config)):
        return False

    named_mutator = re.search(
        r"(?:^|[\s;&|])(?:set-content|add-content|out-file|copy-item|move-item|"
        r"remove-item|rename-item|tee|sed\s+-i|perl\s+-p?i|cp|mv|rm|del|erase|"
        r"copy|move|apply_patch)(?:\s|$)",
        lower,
    )
    code_mutator = any(
        marker in lower
        for marker in (
            ".write(",
            ".write_text(",
            ".write_bytes(",
            "writefile(",
            "writefilesync(",
            "set-content",
            "add-content",
            "out-file",
        )
    )
    git_mutator = bool(re.search(r"(?:^|\s)git\s+(?:checkout|restore|apply)\b", lower))
    extension_pattern = "|".join(
        re.escape(extension.casefold()) for extension in _file_extensions(config)
    )
    redirection_pattern = (
        rf">{{1,2}}\s*(?:\"[^\"]*(?:{extension_pattern})\""
        rf"|'[^']*(?:{extension_pattern})'"
        rf"|[^\s\"']*(?:{extension_pattern})(?:\s|$))"
    )
    managed_redirection = bool(re.search(redirection_pattern, lower))
    return bool(managed_redirection or named_mutator or code_mutator or git_mutator)


def _apply_edit(original: str, tool_input: Mapping[str, Any]) -> str:
    if "content" in tool_input:
        return str(tool_input.get("content") or "")
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        updated = original
        for edit in edits:
            if isinstance(edit, Mapping):
                updated = _apply_edit(updated, edit)
        return updated
    old = tool_input.get("old_string")
    new = tool_input.get("new_string")
    if old is None:
        old = tool_input.get("oldString")
    if new is None:
        new = tool_input.get("newString")
    if old is not None and new is not None:
        count = -1 if _bool(tool_input.get("replace_all"), False) else 1
        return original.replace(str(old), str(new), count)
    # For unknown Codagent variants, a snippet is more useful for retrieval
    # than an empty string, but it is not used as the post-write source of truth.
    return _text(new) or original


def _proposed_content(payload: Mapping[str, Any], target: Path) -> str:
    try:
        original = target.read_text(encoding="utf-8-sig") if target.is_file() else ""
    except OSError:
        original = ""
    return _apply_edit(original, _tool_input(payload))


def _tool_failed(payload: Mapping[str, Any]) -> bool:
    response = payload.get("tool_response")
    if not isinstance(response, Mapping):
        response = payload.get("toolResponse")
    response = _mapping(response)
    return bool(
        _bool(payload.get("is_error"), False)
        or _bool(response.get("is_error"), False)
        or _bool(response.get("isError"), False)
        or payload.get("tool_error")
    )


def _ai_review_evidence(
    payload: Mapping[str, Any], rule_ids: Iterable[str]
) -> tuple[bool, str, list[str]]:
    message = _text(
        payload.get("last_assistant_message")
        or payload.get("lastAssistantMessage")
    )
    expected = sorted({_text(rule_id) for rule_id in rule_ids if _text(rule_id)})
    positive = re.compile(
        r"(?:审查通过|结论\s*[:：]\s*通过|已修复|无未解决(?:问题|项)|"
        r"仅建议\s*[,，；;:]?\s*无阻断|\bpass(?:ed)?\b|\bcompliant\b)",
        re.IGNORECASE,
    )
    negative = re.compile(
        r"(?:未通过|不通过|未修复|仍有|尚有|(?<!不)存在(?:严重)?问题|"
        r"有阻断|不符合|无法确认|\bfail(?:ed)?\b|\bnon[- ]compliant\b|\bunresolved\b)",
        re.IGNORECASE,
    )
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    invalid: list[str] = []
    for rule_id in expected:
        matching_lines = [
            line for line in lines if rule_id.casefold() in line.casefold()
        ]
        if not any(positive.search(line) and not negative.search(line) for line in matching_lines):
            invalid.append(rule_id)
    return bool(message and not invalid), message, invalid


def _read_state(path: Path, session_id: str) -> dict[str, Any]:
    empty = {
        "schema_version": HOOK_SCHEMA_VERSION,
        "session_id": session_id,
        "receipts": {},
        "changed_files": {},
        "review_requests": {},
    }
    if not path.is_file():
        return empty
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return empty
    if not isinstance(value, Mapping):
        return empty
    state = dict(empty)
    state.update(value)
    for key in ("receipts", "changed_files", "review_requests"):
        if not isinstance(state.get(key), Mapping):
            state[key] = {}
        else:
            state[key] = dict(state[key])
    return state


def _write_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _receipt_key(path: Path) -> str:
    return path.as_posix().casefold()


def _valid_receipt(receipt: Any, ttl_seconds: int) -> bool:
    if not isinstance(receipt, Mapping):
        return False
    if receipt.get("status") not in {"ready", "authorized"}:
        return False
    issued = receipt.get("issued_at_epoch")
    try:
        age = time.time() - float(issued)
    except (TypeError, ValueError):
        return False
    return 0 <= age <= max(1, ttl_seconds)


def _format_context(
    path: str,
    cards: list[Mapping[str, Any]],
    *,
    policy_version: str,
    policy_error: str | None = None,
    retrieval_warning: str | None = None,
    codegraph_status: str = "not_used",
    max_chars: int = 12000,
) -> tuple[str, str]:
    lines = [
        f"[Java 规范凭据｜写入前] 目标：{path}",
        f"规范版本：{policy_version}",
    ]
    if policy_error:
        lines.extend(["规范检索：失败", f"原因：{policy_error}"])
        return "unavailable", "\n".join(lines)[:max_chars]
    if cards:
        lines.append(f"规范检索：已完成，命中 {len(cards)} 条")
        per_rule_budget = min(
            3200,
            max(520, (max_chars - 700) // max(1, len(cards))),
        )
        for card in cards:
            raw_rule = card.get("rule")
            rule = dict(raw_rule) if isinstance(raw_rule, Mapping) else {}
            raw_metadata = rule.get("metadata")
            metadata = (
                dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
            )
            statement = _text(card.get("statement") or rule.get("statement"))
            statement_limit = min(600, max(180, per_rule_budget // 4))
            if len(statement) > statement_limit:
                statement = statement[: statement_limit - 1].rstrip() + "…"
            source = _text(card.get("source"))
            suffix = f"（来源：{source}）" if source else ""
            severity = _text(rule.get("severity") or card.get("severity"))
            level = _text(metadata.get("level") or severity)
            prefix = f"- [{card.get('id')}]"
            if level:
                prefix += f"【级别：{level}】"
            lines.append(f"{prefix} {statement}{suffix}")

            detail_fields = (
                ("描述", metadata.get("description")),
                ("反例", metadata.get("negative_example")),
                ("正例", metadata.get("positive_example")),
            )
            remaining = max(0, per_rule_budget - len(statement) - len(suffix) - 80)
            present = [(label, _text(value)) for label, value in detail_fields if _text(value)]
            field_budget = max(100, remaining // max(1, len(present)))
            for label, value in present:
                if len(value) > field_budget:
                    value = value[: field_budget - 1].rstrip() + "…"
                lines.append(f"  【{label}】{value}")
        lines.append("实施状态：必须按上述已命中规范完成本次写入。")
        status = "matched"
    else:
        lines.extend(
            [
                "规范检索：已完成，未命中当前文件的专门规则",
                "实施状态：可按项目现有模式和 Java 21/Spring MVC 通用实践实现，",
                "但不得声称本次是“严格按已命中的公司专门规范”编写。",
            ]
        )
        status = "none"
    if retrieval_warning:
        lines.append(f"语义检索：不可用，已自动回退到本地 BM25（{retrieval_warning}）")
    if codegraph_status == "used":
        lines.append("CodeGraph：本次输入声明已查询；它只作为代码事实来源。")
    elif codegraph_status == "unavailable":
        lines.append("CodeGraph：不可用（可选能力，不影响规范流程）。")
    else:
        lines.append("CodeGraph：未使用（可选能力）。")
    context = "\n".join(lines)
    if len(context) > max_chars:
        context = context[: max(1, max_chars - 28)].rstrip() + "\n…规范上下文已按配置截断。"
    return status, context


def _codegraph_status(payload: Mapping[str, Any]) -> str:
    value = payload.get("codegraph")
    if not isinstance(value, Mapping):
        context = payload.get("policykit_context")
        if isinstance(context, Mapping):
            value = context.get("codegraph")
    if isinstance(value, Mapping):
        if _bool(value.get("queried") or value.get("used"), False):
            return "used"
        if value.get("available") is False or value.get("error"):
            return "unavailable"
    return "not_used"


def _deny_pre(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _pre_context(context: str) -> dict[str, Any]:
    # Deliberately omit permissionDecision=allow: returning an explicit allow
    # would bypass the host's normal user permission handling.
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context,
        }
    }


def _post_context(context: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        }
    }


def _blocking_output(reason: str) -> dict[str, Any]:
    return {"decision": "block", "reason": reason}


def _result_lines(results: Iterable[CheckResult], *, failures_only: bool = False) -> list[str]:
    lines: list[str] = []
    for result in results:
        if failures_only and result.status not in {"fail", "error"}:
            continue
        location = result.path + (f":{result.line}" if result.line else "")
        lines.append(
            f"- [{result.rule_id}] {location}：{result.message}"
            + (f"；证据：{result.evidence}" if result.evidence else "")
        )
    return lines


class HookRuntime:
    """Stateful implementation shared by CLI hooks and unit tests."""

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        home: str | Path | None = None,
    ) -> None:
        self.home = _resolve_home(home)
        self.config = dict(config or {})
        self.rules_path = _path_from_config(
            self.config,
            self.home,
            "approved_rules",
            ".policy-work/approved-rules.json",
            env="POLICYKIT_APPROVED_RULES",
        )
        self.search_index_path = _path_from_config(
            self.config,
            self.home,
            "search_index",
            ".policy-work/search-index.db",
            env="POLICYKIT_SEARCH_INDEX",
        )
        self.receipts_dir = _path_from_config(
            self.config,
            self.home,
            "receipts_dir",
            ".policy-work/receipts",
            env="POLICYKIT_RECEIPTS_DIR",
        )
        self.audit_dir = _path_from_config(
            self.config,
            self.home,
            "audit_dir",
            ".policy-work/audit",
            env="POLICYKIT_AUDIT_DIR",
        )
        self.require_receipt = _bool(
            _nested(self.config, "runtime", "require_receipt", True), True
        )
        self.receipt_ttl = max(
            10,
            int(_nested(self.config, "runtime", "receipt_ttl_seconds", 900) or 900),
        )
        self.fail_closed = _bool(
            _nested(self.config, "runtime", "fail_closed", True), True
        )
        self.max_rules = max(
            1,
            int(_nested(self.config, "runtime", "max_rules_per_edit", 20) or 20),
        )
        self.max_context_chars = max(
            1000,
            int(_nested(self.config, "runtime", "max_context_chars", 6000) or 6000),
        )
        configured_block_severities = _nested(
            self.config, "runtime", "block_severities", ("blocker", "major")
        )
        if isinstance(configured_block_severities, str):
            configured_block_severities = (configured_block_severities,)
        self.block_severities = tuple(
            configured_block_severities or ("blocker", "major")
        )
        self.ai_review_blocks_stop = _bool(
            _nested(self.config, "runtime", "ai_review_blocks_stop", True), True
        )
        self.ai_settings = AISettings.from_config(self.config)

    def _resources(
        self, payload: Mapping[str, Any]
    ) -> tuple[
        str,
        Path,
        dict[str, Any],
        AuditTrail,
        list[dict[str, Any]],
        str,
        str,
        str | None,
    ]:
        session_id = _session_id(payload)
        state_path = self.receipts_dir / f"{session_id}.json"
        state = _read_state(state_path, session_id)
        audit = AuditTrail(self.audit_dir, session_id)
        rules, version, bundle_id, error = _load_rule_bundle(self.rules_path)
        if error is None:
            try:
                SQLitePolicyIndex(self.search_index_path).validate_metadata(
                    expected_policy_version=version,
                    expected_bundle_id=bundle_id or None,
                )
            except (OSError, ValueError) as exc:
                error = f"正式检索索引不可用或与规则包不一致：{exc}"
        return (
            session_id,
            state_path,
            state,
            audit,
            rules,
            version,
            bundle_id,
            error,
        )

    def prepare_receipt(
        self,
        file_path: str | Path,
        session_id: str,
        *,
        query: str = "",
        code: str = "",
        cwd: str | Path | None = None,
        codegraph: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        state_path = self.receipts_dir / f"{safe_session_id(session_id)}.json"
        with _state_file_lock(state_path):
            return self._prepare_receipt_unlocked(
                file_path,
                session_id,
                query=query,
                code=code,
                cwd=cwd,
                codegraph=codegraph,
            )

    def _prepare_receipt_unlocked(
        self,
        file_path: str | Path,
        session_id: str,
        *,
        query: str = "",
        code: str = "",
        cwd: str | Path | None = None,
        codegraph: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Proactively retrieve policy context and issue a pre-write receipt.

        Skills should call this before proposing an Edit/Write.  The PreToolUse
        fallback can then validate and authorize the receipt without denying
        the first write.  If a Skill does not call it, PreToolUse itself issues
        the receipt and denies once so the model still sees the policy context.
        """

        base = Path(cwd).expanduser().resolve() if cwd is not None else Path.cwd()
        target = Path(file_path).expanduser()
        if not target.is_absolute():
            target = base / target
        target = target.resolve(strict=False)
        payload: dict[str, Any] = {
            "session_id": safe_session_id(session_id),
            "cwd": str(base),
        }
        if codegraph is not None:
            payload["codegraph"] = dict(codegraph)
        display = _display_path(target, payload)
        (
            _session,
            state_path,
            state,
            audit,
            rules,
            policy_version,
            bundle_id,
            policy_error,
        ) = self._resources(payload)
        checker = PolicyChecker(
            rules,
            fail_closed=self.fail_closed,
            block_severities=self.block_severities,
        )
        cards: list[dict[str, Any]] = []
        semantic_warning = ""
        if policy_error is None:
            try:
                query_embedding, semantic_warning = embed_runtime_query(
                    self.ai_settings,
                    query=query,
                    file_path=display,
                    code=code,
                    index_metadata=SQLitePolicyIndex(
                        self.search_index_path
                    ).read_metadata(),
                )
                cards = _search_cards(
                    self.search_index_path,
                    checker,
                    query=query,
                    file_path=display,
                    code=code,
                    limit=self.max_rules,
                    expected_policy_version=policy_version,
                    expected_bundle_id=bundle_id,
                    query_embedding=query_embedding,
                    semantic_weight=self.ai_settings.semantic_weight,
                    min_similarity=self.ai_settings.min_similarity,
                )
            except (OSError, ValueError, PolicyAIError) as exc:
                policy_error = f"正式检索索引查询失败：{exc}"
        codegraph_status = _codegraph_status(payload)
        policy_status, context = _format_context(
            display,
            cards,
            policy_version=policy_version,
            policy_error=policy_error,
            retrieval_warning=semantic_warning,
            codegraph_status=codegraph_status,
            max_chars=self.max_context_chars,
        )
        issued = not (policy_error and self.fail_closed)
        if issued:
            state["receipts"][_receipt_key(target)] = {
                "token_hash": sha256(uuid4().hex.encode("ascii")).hexdigest(),
                "status": "ready",
                "issued_at_epoch": time.time(),
                "path": display,
                "policy_version": policy_version,
                "bundle_id": bundle_id,
                "policy_status": policy_status,
                "matched_rule_ids": [card["id"] for card in cards],
                "matched_rules": cards,
                "prepared_by": "skill",
                "prepared_context": context,
            }
            _write_state(state_path, state)
        audit.record(
            "policy_context_prepared",
            path=display,
            policy_status=policy_status,
            policy_version=policy_version,
            matched_rule_ids=[card["id"] for card in cards],
            codegraph_status=codegraph_status,
            outcome="凭据已生成" if issued else "fail-closed，未生成凭据",
        )
        audit.write_report()
        return {
            "status": policy_status,
            "context": context,
            "matched_rule_ids": [card["id"] for card in cards],
            "matched_rules": cards,
            "policy_version": policy_version,
            "bundle_id": bundle_id,
            "receipt_issued": issued,
            "blocking": bool(policy_error and self.fail_closed),
            "error": policy_error,
        }

    def handle(self, event_name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        state_path = self.receipts_dir / f"{_session_id(payload)}.json"
        with _state_file_lock(state_path):
            return self._handle_unlocked(event_name, payload)

    def _handle_unlocked(
        self, event_name: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        event = _event_name(event_name or payload.get("hook_event_name"))
        if event == "pre-edit":
            return self.pre_edit(payload)
        if event == "pre-shell":
            return self.pre_shell(payload)
        if event == "post-edit":
            return self.post_edit(payload)
        if event == "stop":
            return self.stop(payload)
        return {}

    def pre_shell(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not _looks_like_managed_shell_write(payload, self.config):
            return {}
        session_id = _session_id(payload)
        audit = AuditTrail(self.audit_dir, session_id)
        audit.record(
            "shell_write_blocked",
            policy_status="not_required",
            outcome="检测到 Shell 直接修改受管文件；要求改用 Edit/Write/MultiEdit",
        )
        audit.write_report()
        return _deny_pre(
            "Java Policy Kit 已阻止通过 Shell 直接修改 Java/Maven/Spring 受管文件。"
            "请改用 Edit、Write 或 MultiEdit，使写入前检索、写后检查和审计 Hook 能够执行。"
        )

    def pre_edit(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not _is_managed_write(payload, self.config):
            return {}
        target = _target_path(payload)
        assert target is not None
        display = _display_path(target, payload)
        (
            _session,
            state_path,
            state,
            audit,
            rules,
            policy_version,
            bundle_id,
            policy_error,
        ) = self._resources(payload)
        checker = PolicyChecker(
            rules,
            fail_closed=self.fail_closed,
            block_severities=self.block_severities,
        )
        proposed = _proposed_content(payload, target)
        cards: list[dict[str, Any]] = []
        semantic_warning = ""
        if policy_error is None:
            try:
                task_query = _text(payload.get("task") or payload.get("prompt"))
                query_embedding, semantic_warning = embed_runtime_query(
                    self.ai_settings,
                    query=task_query,
                    file_path=display,
                    code=proposed,
                    index_metadata=SQLitePolicyIndex(
                        self.search_index_path
                    ).read_metadata(),
                )
                cards = _search_cards(
                    self.search_index_path,
                    checker,
                    query=task_query,
                    file_path=display,
                    code=proposed,
                    limit=self.max_rules,
                    expected_policy_version=policy_version,
                    expected_bundle_id=bundle_id,
                    query_embedding=query_embedding,
                    semantic_weight=self.ai_settings.semantic_weight,
                    min_similarity=self.ai_settings.min_similarity,
                )
            except (OSError, ValueError, PolicyAIError) as exc:
                policy_error = f"正式检索索引查询失败：{exc}"
        codegraph_status = _codegraph_status(payload)
        policy_status, context = _format_context(
            display,
            cards,
            policy_version=policy_version,
            policy_error=policy_error,
            retrieval_warning=semantic_warning,
            codegraph_status=codegraph_status,
            max_chars=self.max_context_chars,
        )

        if policy_error and self.fail_closed:
            audit.record(
                "policy_lookup_failed",
                path=display,
                policy_status="unavailable",
                message=policy_error,
                codegraph_status=codegraph_status,
            )
            audit.write_report()
            return _deny_pre(context + "\n状态：fail-closed，禁止写入。")

        key = _receipt_key(target)
        receipts = state["receipts"]
        receipt = receipts.get(key)
        receipt_valid = _valid_receipt(receipt, self.receipt_ttl) and (
            _text(receipt.get("policy_version")) == policy_version
            and _text(receipt.get("bundle_id")) == bundle_id
        )
        authorization_context = context
        if receipt_valid:
            prepared_cards = [
                dict(item)
                for item in (receipt.get("matched_rules") or ())
                if isinstance(item, Mapping)
            ]
            prepared_ids = {
                _text(item.get("id")) for item in prepared_cards if _text(item.get("id"))
            }
            combined: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            direct_cards = [card for card in cards if card.get("direct_applicable")]
            ranked_cards = [card for card in cards if not card.get("direct_applicable")]
            for card in (*direct_cards, *prepared_cards, *ranked_cards):
                rule_id = _text(card.get("id"))
                if not rule_id or rule_id in seen_ids:
                    continue
                seen_ids.add(rule_id)
                combined.append(card)
            cards = combined[: max(self.max_rules, len(direct_cards))]
            policy_status, context = _format_context(
                display,
                cards,
                policy_version=policy_version,
                policy_error=policy_error,
                retrieval_warning=semantic_warning,
                codegraph_status=codegraph_status,
                max_chars=self.max_context_chars,
            )
            new_cards = [
                card for card in cards if _text(card.get("id")) not in prepared_ids
            ]
            id_summary = ", ".join(_text(card.get("id")) for card in cards) or "无专门规范"
            authorization_context = (
                f"[Java 规范凭据] 有效；沿用已准备的规范上下文。规则：{id_summary}。"
            )
            if new_cards:
                _status, supplement = _format_context(
                    display,
                    new_cards,
                    policy_version=policy_version,
                    policy_error=policy_error,
                    retrieval_warning=semantic_warning,
                    codegraph_status=codegraph_status,
                    max_chars=self.max_context_chars,
                )
                authorization_context += "\n真实写入内容新增命中：\n" + supplement
        matched_rule_ids = [card["id"] for card in cards]
        if self.require_receipt and not receipt_valid:
            token = uuid4().hex
            receipts[key] = {
                "token_hash": sha256(token.encode("ascii")).hexdigest(),
                "status": "ready",
                "issued_at_epoch": time.time(),
                "path": display,
                "policy_version": policy_version,
                "bundle_id": bundle_id,
                "policy_status": policy_status,
                "matched_rule_ids": matched_rule_ids,
                "matched_rules": cards,
            }
            _write_state(state_path, state)
            audit.record(
                "policy_context_issued",
                path=display,
                policy_status=policy_status,
                policy_version=policy_version,
                matched_rule_ids=matched_rule_ids,
                codegraph_status=codegraph_status,
                outcome="首写已阻止，规范凭据已生成",
            )
            audit.write_report()
            return _deny_pre(
                context
                + "\n本次首写已阻止，用于确保上述上下文进入模型；请依据它重试写入。"
            )

        if self.require_receipt:
            receipt["status"] = "authorized"
            receipt["authorized_at_epoch"] = time.time()
            receipt["policy_status"] = policy_status
            receipt["matched_rule_ids"] = matched_rule_ids
            receipt["matched_rules"] = cards
            receipt["prepared_context"] = context
            receipts[key] = receipt
            _write_state(state_path, state)
        audit.record(
            "pre_write_authorized",
            path=display,
            policy_status=policy_status,
            policy_version=policy_version,
            matched_rule_ids=matched_rule_ids,
            codegraph_status=codegraph_status,
            outcome=("凭据有效，交回宿主权限流程" if self.require_receipt else "无需凭据"),
        )
        return _pre_context(
            authorization_context + "\n继续使用 Codagent 原有权限流程。"
        )

    def post_edit(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not _is_managed_write(payload, self.config):
            return {}
        target = _target_path(payload)
        assert target is not None
        display = _display_path(target, payload)
        (
            _session,
            state_path,
            state,
            audit,
            rules,
            policy_version,
            bundle_id,
            policy_error,
        ) = self._resources(payload)
        key = _receipt_key(target)
        receipt = state["receipts"].pop(key, None)
        receipt_authorized = (
            isinstance(receipt, Mapping)
            and receipt.get("status") == "authorized"
            and _text(receipt.get("policy_version")) == policy_version
            and _text(receipt.get("bundle_id")) == bundle_id
        )

        if _tool_failed(payload):
            _write_state(state_path, state)
            audit.record(
                "write_failed",
                path=display,
                policy_status=(receipt or {}).get("policy_status", "unavailable"),
                policy_version=policy_version,
                outcome="宿主工具报告写入失败",
            )
            audit.write_report()
            return {}

        try:
            content = target.read_text(encoding="utf-8-sig")
        except OSError as exc:
            message = f"写入后无法读取目标文件：{exc}"
            audit.record(
                "post_write_check_failed",
                path=display,
                policy_status=(receipt or {}).get("policy_status", "unavailable"),
                message=message,
            )
            audit.write_report()
            if self.fail_closed:
                return _blocking_output(message)
            return _post_context(message + "；当前配置为 fail-open。")

        checker = PolicyChecker(
            rules,
            fail_closed=self.fail_closed,
            block_severities=self.block_severities,
        )
        active_ai_rule_ids = (
            list(receipt.get("matched_rule_ids") or ())
            if isinstance(receipt, Mapping)
            else []
        )
        results = checker.check_file(
            display,
            content,
            phase="post",
            ai_rule_ids=active_ai_rule_ids,
        )
        blocking = [
            result
            for result in results
            if result.blocking and result.status in {"fail", "error"}
        ]
        configuration_errors = [result for result in results if result.status == "error"]
        if self.fail_closed:
            for result in configuration_errors:
                result.blocking = True
            blocking = [
                result
                for result in results
                if result.blocking and result.status in {"fail", "error"}
            ]
        state["changed_files"][key] = {
            "absolute_path": target.as_posix(),
            "display_path": display,
            "last_changed_epoch": time.time(),
            "matched_rule_ids": active_ai_rule_ids,
        }
        _write_state(state_path, state)
        policy_status = (receipt or {}).get(
            "policy_status", "matched" if results else "none"
        )
        audit.record(
            "post_write_check",
            path=display,
            policy_status=policy_status,
            policy_version=policy_version,
            receipt_present=bool(receipt),
            receipt_authorized=receipt_authorized,
            matched_rule_ids=(receipt or {}).get("matched_rule_ids", []),
            results=[result.to_dict() for result in results],
            outcome="blocked" if blocking else "checked",
        )
        audit.write_report()

        if policy_error and self.fail_closed:
            return _blocking_output(f"规范规则不可用：{policy_error}")
        if self.require_receipt and not receipt_authorized and self.fail_closed:
            return _blocking_output(
                "本次 Java 写入没有有效的写入前规范凭据。请重新编辑该文件，让 PreToolUse 完成规范查询。"
            )
        if blocking:
            lines = [
                "[Java 规范检查｜写入后] 发现阻断问题，请立即修复后重新检查：",
                *_result_lines(blocking, failures_only=True),
            ]
            return _blocking_output("\n".join(lines))
        nonblocking_failures = [
            result
            for result in results
            if not result.blocking and result.status in {"fail", "error"}
        ]
        reviews = [result for result in results if result.status == "review"]
        if nonblocking_failures or reviews:
            sections = ["[Java 规范检查｜写入后] 未发现阻断问题，但仍有未通过或待审项目："]
            if nonblocking_failures:
                sections.append(
                    "非阻断确定性问题：\n"
                    + "\n".join(_result_lines(nonblocking_failures, failures_only=True))
                )
            if reviews:
                sections.append(
                    "待 AI 语义审查：\n" + "\n".join(_result_lines(reviews))
                )
            return _post_context("\n".join(sections))
        return _post_context("[Java 规范检查｜写入后] 已执行，未发现阻断问题。")

    def stop(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        (
            _session,
            state_path,
            state,
            audit,
            rules,
            policy_version,
            _bundle_id,
            policy_error,
        ) = self._resources(payload)
        changed: dict[str, Any] = dict(state.get("changed_files", {}))
        for raw in payload.get("changed_files", ()) or ():
            path = Path(str(raw)).expanduser().resolve(strict=False)
            changed.setdefault(
                _receipt_key(path),
                {"absolute_path": path.as_posix(), "display_path": path.as_posix()},
            )

        paths: list[str] = []
        contents: dict[str, str] = {}
        ai_rule_ids_by_path: dict[str, Iterable[str]] = {}
        unreadable: list[str] = []
        for item in changed.values():
            if not isinstance(item, Mapping):
                continue
            absolute = _text(item.get("absolute_path"))
            display = _text(item.get("display_path") or absolute)
            if not absolute:
                continue
            path = Path(absolute)
            paths.append(display)
            ai_rule_ids_by_path[display] = list(item.get("matched_rule_ids") or ())
            try:
                contents[display] = path.read_text(encoding="utf-8-sig")
            except OSError:
                unreadable.append(display)
                contents[display] = ""

        checker = PolicyChecker(
            rules,
            fail_closed=self.fail_closed,
            block_severities=self.block_severities,
        )
        results = checker.check_change_set(
            paths,
            contents=contents,
            phase="stop",
            ai_rule_ids_by_path=ai_rule_ids_by_path,
        )
        if self.fail_closed:
            for result in results:
                if result.status == "error":
                    result.blocking = True
        blocking = [
            result
            for result in results
            if result.blocking and result.status in {"fail", "error"}
        ]
        reviews = [result for result in results if result.status == "review"]

        review_gate_reason = ""
        if self.ai_review_blocks_stop and reviews and not blocking:
            review_rule_ids = sorted({item.rule_id for item in reviews})
            signature = sha256(
                "\x1f".join(
                    sorted(
                        f"{item.rule_id}:{item.path}:"
                        f"{sha256(contents.get(item.path, '').encode('utf-8')).hexdigest()}"
                        for item in reviews
                    )
                ).encode("utf-8")
            ).hexdigest()
            evidence_ok, assistant_message, invalid_rule_ids = _ai_review_evidence(
                payload, review_rule_ids
            )
            if not evidence_ok:
                state["review_requests"][signature] = {
                    "requested_at_epoch": time.time(),
                    "rule_ids": review_rule_ids,
                    "invalid_rule_ids": invalid_rule_ids,
                }
                review_gate_reason = (
                    "[Java 规范最终审查] 请逐条完成 AI 语义审查，在最终回复中写出每个规则 ID，"
                    "并明确写明“审查通过”“已修复”或“仅建议，无阻断”。"
                    "Codagent 必须通过 Stop 的 last_assistant_message 提供这份证据后才能结束：\n"
                    + "\n".join(_result_lines(reviews))
                )
                if invalid_rule_ids:
                    review_gate_reason += "\n当前回复缺少逐规则正向结论：" + ", ".join(
                        invalid_rule_ids
                    )
            else:
                audit.record(
                    "ai_review_self_attested",
                    policy_status="matched",
                    matched_rule_ids=review_rule_ids,
                    assistant_message_sha256=sha256(
                        assistant_message.encode("utf-8")
                    ).hexdigest(),
                    outcome="最终回复包含逐规则 AI 自述结论；不宣称程序化通过",
                )
                state["review_requests"].pop(signature, None)

        if unreadable and self.fail_closed:
            blocking.append(
                CheckResult(
                    rule_id="RUNTIME-READ",
                    checker="runtime",
                    status="error",
                    severity="blocker",
                    message="最终检查无法读取已修改文件",
                    evidence=", ".join(unreadable),
                    blocking=True,
                )
            )
        if policy_error and self.fail_closed and paths:
            blocking.append(
                CheckResult(
                    rule_id="RUNTIME-POLICY",
                    checker="runtime",
                    status="error",
                    severity="blocker",
                    message=policy_error,
                    blocking=True,
                )
            )

        audit.record(
            "stop_summary",
            policy_status=("unavailable" if policy_error else "matched" if results else "none"),
            policy_version=policy_version,
            changed_files=paths,
            matched_rule_ids=sorted({result.rule_id for result in results}),
            results=[result.to_dict() for result in results],
            codegraph_status=_codegraph_status(payload),
            outcome="blocked" if blocking or review_gate_reason else "completed",
        )
        _write_state(state_path, state)
        report = audit.write_report()

        if blocking:
            return _blocking_output(
                "[Java 规范最终检查] 存在未解决的阻断问题：\n"
                + "\n".join(_result_lines(blocking, failures_only=True))
                + f"\n会话报告：{report}"
            )
        if review_gate_reason:
            return _blocking_output(review_gate_reason + f"\n会话报告：{report}")
        return {}


def handle_hook(
    event_name: str,
    payload: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
    home: str | Path | None = None,
) -> dict[str, Any]:
    """Handle one hook event and return the JSON object for stdout."""

    if not isinstance(payload, Mapping):
        raise TypeError("hook payload must be a mapping")
    return HookRuntime(config, home=home).handle(event_name, payload)


def prepare_receipt(
    file_path: str | Path,
    session_id: str,
    query: str = "",
    code: str = "",
    config: Mapping[str, Any] | None = None,
    home: str | Path | None = None,
    *,
    cwd: str | Path | None = None,
    codegraph: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Functional Skill-facing wrapper for :meth:`HookRuntime.prepare_receipt`."""

    return HookRuntime(config, home=home).prepare_receipt(
        file_path,
        session_id,
        query=query,
        code=code,
        cwd=cwd,
        codegraph=codegraph,
    )


def main_hook(
    event_name: str | None = None,
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    config: Mapping[str, Any] | None = None,
    home: str | Path | None = None,
) -> int:
    """Read one hook JSON payload from stdin and emit one JSON response."""

    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    resolved_event = event_name or ""
    try:
        payload = json.load(input_stream)
        if not isinstance(payload, Mapping):
            raise ValueError("hook stdin must be a JSON object")
        resolved_config = dict(config) if config is not None else load_runtime_config(home)
        resolved_event = event_name or _text(
            payload.get("hook_event_name") or payload.get("hookEventName")
        )
        output = handle_hook(resolved_event, payload, resolved_config, home)
    except Exception as exc:  # Hook processes must fail predictably, not traceback.
        reason = f"Java Policy Hook 运行失败：{type(exc).__name__}: {exc}"
        output = (
            _deny_pre(reason)
            if _event_name(resolved_event) in {"pre-edit", "pre-shell"}
            else _blocking_output(reason)
        )
    json.dump(output, output_stream, ensure_ascii=False)
    output_stream.write("\n")
    output_stream.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a Java Policy Kit hook")
    parser.add_argument(
        "event",
        nargs="?",
        choices=("pre", "pre-edit", "pre-shell", "post", "post-edit", "stop"),
        help="omit to use hook_event_name from stdin",
    )
    parser.add_argument("--home", help="installed policy-kit home")
    args = parser.parse_args(argv)
    return main_hook(args.event, home=args.home)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "HOOK_SCHEMA_VERSION",
    "HookRuntime",
    "handle_hook",
    "load_runtime_config",
    "main",
    "main_hook",
    "prepare_receipt",
]
