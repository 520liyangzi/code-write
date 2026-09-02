"""Configurable, standard-library policy checks.

The check engine contains no company rules.  It executes only approved rules
loaded by the caller.  A rule may declare one or more checker specifications
under ``checks``, ``checkers``, ``checker`` or ``metadata.checks``.  For
backwards compatibility, ``enforcement_candidates`` plus checker fields in
``metadata`` is also understood.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import fnmatch
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SUPPORTED_CHECKERS = frozenset(
    {
        "regex_forbid",
        "regex_require",
        "path_allow",
        "path_forbid",
        "companion_change",
        "ai_review",
    }
)
_CHECKER_CONTAINER_KEYS = ("check", "checks", "checkers", "checker", "enforcement")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]


def _strings(value: Any) -> list[str]:
    return [text for item in _items(value) if (text := _text(item))]


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        return dict(converted) if isinstance(converted, Mapping) else {}
    return {}


def _normal_path(value: str | Path) -> str:
    text = str(value or "").replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    return text.removeprefix("./")


def _path_matches(path: str, patterns: Sequence[str]) -> bool:
    normalized = _normal_path(path)
    lowered = normalized.lower()
    name = normalized.rsplit("/", 1)[-1]
    lower_name = name.lower()
    for raw_pattern in patterns:
        pattern = _normal_path(raw_pattern)
        comparable = pattern.lower()
        # Matching both the full path and basename makes ``*.java`` useful for
        # absolute hook paths while retaining support for module-aware globs.
        if fnmatch.fnmatchcase(lowered, comparable) or fnmatch.fnmatchcase(
            lower_name, comparable
        ):
            return True
        # ``fnmatch`` does not give ``**/foo`` the intuitive zero-directory
        # match, so explicitly try without that prefix.
        if comparable.startswith("**/") and (
            fnmatch.fnmatchcase(lowered, comparable[3:])
            or fnmatch.fnmatchcase(lower_name, comparable[3:])
        ):
            return True
    return False


def _first(mapping: Mapping[str, Any], names: Sequence[str], default: Any = None) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return default


def _regex_flags(value: Any) -> int:
    if isinstance(value, int):
        return value
    flags = 0
    names = _strings(value)
    if len(names) == 1 and "," in names[0]:
        names = [part.strip() for part in names[0].split(",")]
    table = {
        "i": re.IGNORECASE,
        "ignorecase": re.IGNORECASE,
        "m": re.MULTILINE,
        "multiline": re.MULTILINE,
        "s": re.DOTALL,
        "dotall": re.DOTALL,
        "x": re.VERBOSE,
        "verbose": re.VERBOSE,
    }
    for name in names:
        flags |= table.get(name.lower(), 0)
    return flags


def _regex_safety_error(pattern: str) -> str:
    if len(pattern) > 2000:
        return "正则长度超过 2000 字符"
    nested_repeat = re.search(
        r"\((?:\?:)?(?:[^()\\]|\\.){0,1000}(?:[+*]|\{\d*,?\d*\})"
        r"(?:[^()\\]|\\.){0,1000}\)\s*(?:[+*]|\{\d*,?\d*\})",
        pattern,
    )
    if nested_repeat:
        return "检测到嵌套重复量词，可能造成灾难性回溯"
    return ""


def _line_number(content: str, offset: int) -> int:
    return content.count("\n", 0, max(0, offset)) + 1


def _evidence(text: str, *, limit: int = 180) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


@dataclass(slots=True)
class CheckResult:
    """One deterministic check or AI-review routing decision."""

    rule_id: str
    checker: str
    status: str
    severity: str
    message: str
    path: str = ""
    line: int = 0
    evidence: str = ""
    source: str = ""
    blocking: bool = False

    @property
    def passed(self) -> bool:
        return self.status in {"pass", "skip"}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _source_label(rule: Mapping[str, Any]) -> str:
    source = rule.get("source")
    if isinstance(source, Mapping):
        document = _text(source.get("document"))
        section = _text(source.get("section"))
        return " / ".join(part for part in (document, section) if part)
    return _text(source)


def _checker_specs(rule: Mapping[str, Any]) -> list[dict[str, Any]]:
    metadata = _mapping(rule.get("metadata"))
    raw: list[Any] = []
    for container in (rule, metadata):
        for key in _CHECKER_CONTAINER_KEYS:
            if key in container:
                raw.extend(_items(container.get(key)))

    specs: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            spec = {"type": item}
        elif isinstance(item, Mapping):
            spec = dict(item)
        else:
            continue
        checker_type = _text(spec.get("type") or spec.get("checker")).lower()
        if checker_type in SUPPORTED_CHECKERS:
            spec["type"] = checker_type
            specs.append(spec)

    if not specs:
        candidates = _strings(
            rule.get("enforcement_candidates") or metadata.get("enforcement_candidates")
        )
        for checker_type in candidates:
            checker_type = checker_type.lower()
            if checker_type not in SUPPORTED_CHECKERS:
                continue
            # Metadata is where the importer can place pattern/path parameters
            # without changing the core PolicyRule schema.
            spec = dict(metadata)
            spec["type"] = checker_type
            specs.append(spec)
    if not specs and not bool(metadata.get("enforcement_disabled", False)):
        # An approved natural-language rule must not silently disappear merely
        # because no deterministic checker was generated for it.
        specs.append({"type": "ai_review"})
    return specs


def validate_checker_rules(rules: Iterable[Any]) -> list[str]:
    """Validate explicit checker drafts before review or activation.

    Unsupported or malformed drafts must not silently degrade to AI review,
    because that would activate behavior different from what the reviewer saw.
    """

    errors: list[str] = []
    for value in rules:
        rule = _mapping(value)
        if not rule:
            continue
        rule_id = _text(rule.get("id")) or "UNKNOWN-RULE"
        metadata = _mapping(rule.get("metadata"))
        raw: list[Any] = []
        explicit = False
        for container in (rule, metadata):
            for key in _CHECKER_CONTAINER_KEYS:
                if key in container:
                    explicit = True
                    raw.extend(_items(container.get(key)))
        if not explicit:
            continue
        if not raw:
            errors.append(f"{rule_id}: checker 草案为空")
            continue

        for position, item in enumerate(raw, start=1):
            if isinstance(item, str):
                spec = {"type": item}
            elif isinstance(item, Mapping):
                spec = dict(item)
            else:
                errors.append(
                    f"{rule_id} checker #{position}: 必须是字符串或 JSON 对象"
                )
                continue
            checker_type = _text(spec.get("type") or spec.get("checker")).lower()
            label = f"{rule_id} checker #{position}"
            if checker_type not in SUPPORTED_CHECKERS:
                errors.append(f"{label}: 不支持的 type {checker_type or '<empty>'}")
                continue

            regex_values: list[tuple[str, str]] = []
            trigger = _text(
                _first(spec, ("when_pattern", "trigger_regex", "content_trigger"), "")
            )
            if trigger:
                regex_values.append(("when_pattern", trigger))
            if checker_type in {"regex_forbid", "regex_require"}:
                patterns = _strings(
                    _first(spec, ("patterns", "pattern", "regex", "required_regex"))
                )
                if not patterns:
                    errors.append(f"{label}: {checker_type} 缺少 pattern")
                regex_values.extend(("pattern", pattern) for pattern in patterns)
            elif checker_type == "path_allow":
                allowed = _strings(
                    _first(spec, ("allow", "allowed", "allowed_paths", "patterns", "paths"))
                )
                if not allowed:
                    errors.append(f"{label}: path_allow 缺少 allowed_paths")
            elif checker_type == "path_forbid":
                forbidden = _strings(
                    _first(
                        spec,
                        ("forbid", "forbidden", "forbidden_paths", "patterns", "paths"),
                    )
                )
                if not forbidden:
                    errors.append(f"{label}: path_forbid 缺少 forbidden_paths")
            elif checker_type == "companion_change":
                triggers = _strings(
                    _first(spec, ("trigger_paths", "trigger", "when_changed", "changed"))
                )
                required = _strings(
                    _first(spec, ("requires", "required_paths", "companion_paths"))
                )
                if not triggers or not required:
                    errors.append(
                        f"{label}: companion_change 需要 trigger_paths 和 required_paths"
                    )

            flags = _regex_flags(spec.get("flags"))
            for field, pattern in regex_values:
                safety_error = _regex_safety_error(pattern)
                if safety_error:
                    errors.append(f"{label}: {field} 不安全：{safety_error}")
                    continue
                try:
                    re.compile(pattern, flags)
                except re.error as exc:
                    errors.append(f"{label}: {field} 正则无效：{exc}")
    return errors


def _rule_paths(rule: Mapping[str, Any], spec: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    metadata = _mapping(rule.get("metadata"))
    includes = _strings(
        _first(
            spec,
            ("include_paths", "applies_to_paths", "path_globs", "path_glob"),
            _first(
                metadata,
                ("include_paths", "applies_to_paths", "path_globs", "path_glob"),
                _first(rule, ("include_paths", "applies_to_paths")),
            ),
        )
    )
    excludes = _strings(
        _first(
            spec,
            ("exclude_paths", "excluded_paths"),
            _first(metadata, ("exclude_paths", "excluded_paths")),
        )
    )
    return includes, excludes


def _content_triggered(content: str, spec: Mapping[str, Any]) -> bool:
    pattern = _text(
        _first(spec, ("when_pattern", "trigger_regex", "content_trigger"), "")
    )
    if pattern:
        if re.search(pattern, content, _regex_flags(spec.get("flags"))) is None:
            return False
    terms = _strings(spec.get("when_terms"))
    if terms and not any(term.lower() in content.lower() for term in terms):
        return False
    return True


def _applies(rule: Mapping[str, Any], spec: Mapping[str, Any], path: str, content: str) -> bool:
    includes, excludes = _rule_paths(rule, spec)
    if includes and not _path_matches(path, includes):
        return False
    if excludes and _path_matches(path, excludes):
        return False
    return _content_triggered(content, spec)


def _result(
    rule: Mapping[str, Any],
    spec: Mapping[str, Any],
    *,
    checker: str,
    status: str,
    path: str,
    message: str | None = None,
    line: int = 0,
    evidence: str = "",
) -> CheckResult:
    severity = _text(spec.get("severity") or rule.get("severity") or "major").lower()
    blocking_value = spec.get("blocking")
    blocking = (
        severity in {"blocker", "major"}
        if blocking_value is None
        else bool(blocking_value)
    )
    if status not in {"fail", "error"}:
        blocking = False
    return CheckResult(
        rule_id=_text(rule.get("id")) or "UNKNOWN-RULE",
        checker=checker,
        status=status,
        severity=severity,
        message=_text(message or spec.get("message") or rule.get("statement")),
        path=_normal_path(path),
        line=max(0, int(line or 0)),
        evidence=evidence,
        source=_source_label(rule),
        blocking=blocking,
    )


def _configuration_error(
    rule: Mapping[str, Any], spec: Mapping[str, Any], path: str, message: str
) -> CheckResult:
    result = _result(
        rule,
        spec,
        checker=_text(spec.get("type")) or "unknown",
        status="error",
        path=path,
        message=f"规则配置错误：{message}",
    )
    # Configuration errors should be visible.  Whether they block is still
    # controlled by the runtime's fail_closed policy.
    result.blocking = False
    return result


class PolicyChecker:
    """Execute checker declarations from approved policy rules."""

    def __init__(
        self,
        rules: Iterable[Any],
        *,
        fail_closed: bool = True,
        block_severities: Iterable[str] = ("blocker",),
    ) -> None:
        normalized: list[dict[str, Any]] = []
        for item in rules:
            rule = _mapping(item)
            if not rule:
                continue
            # Approved bundles sometimes omit status because approval already
            # happened at export time.  An explicit non-approved status is
            # never executed.
            status = _text(rule.get("status") or "approved").lower()
            if status != "approved":
                continue
            normalized.append(rule)
        self.rules = normalized
        self.fail_closed = bool(fail_closed)
        self.block_severities = {
            _text(severity).lower() for severity in block_severities if _text(severity)
        }

    def _effective_spec(
        self, rule: Mapping[str, Any], spec: Mapping[str, Any]
    ) -> dict[str, Any]:
        effective = dict(spec)
        severity = _text(effective.get("severity") or rule.get("severity") or "major")
        effective.setdefault("blocking", severity.lower() in self.block_severities)
        return effective

    def applicable_rules(
        self,
        path: str | Path,
        content: str = "",
        *,
        max_rules: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return ranked rule cards suitable for pre-write context."""

        target = _normal_path(path)
        haystack = f"{target}\n{content}".lower()
        ranked: list[tuple[int, str, dict[str, Any]]] = []
        for rule in self.rules:
            specs = _checker_specs(rule) or [{"type": "ai_review"}]
            applicable_specs: list[dict[str, Any]] = []
            for spec in specs:
                try:
                    if _applies(rule, spec, target, content):
                        applicable_specs.append(spec)
                except re.error:
                    # Invalid generated rules are surfaced by the post-write
                    # checker; pre-write retrieval must remain usable so the
                    # rest of the approved policy can still be shown.
                    continue
            if not applicable_specs:
                continue
            score = 1
            metadata = _mapping(rule.get("metadata"))
            if "direct_triggers" in metadata:
                terms = _strings(metadata.get("direct_triggers"))
            else:
                terms = _strings(rule.get("trigger_terms")) + _strings(
                    rule.get("tags")
                )
            matched_terms = [term for term in terms if term.lower() in haystack]
            score += 4 * len(matched_terms)
            has_path_scope = any(_rule_paths(rule, spec)[0] for spec in applicable_specs)
            has_content_scope = any(
                spec.get("when_pattern")
                or spec.get("trigger_regex")
                or spec.get("content_trigger")
                or spec.get("when_terms")
                for spec in applicable_specs
            )
            has_deterministic_checker = any(
                _text(spec.get("type")) != "ai_review" for spec in applicable_specs
            )
            if not (
                matched_terms
                or has_path_scope
                or has_content_scope
                or has_deterministic_checker
                or bool(metadata.get("global_core"))
            ):
                # A broad AI-only rule with no matching signal should stay in
                # the searchable corpus without being injected into every edit.
                continue
            if has_path_scope:
                score += 3
            if has_content_scope:
                score += 3
            if _text(rule.get("severity")).lower() == "blocker":
                score += 2
            card = {
                "id": _text(rule.get("id")) or "UNKNOWN-RULE",
                "title": _text(rule.get("title")),
                "statement": _text(rule.get("statement")),
                "severity": _text(rule.get("severity") or "major"),
                "category": _text(rule.get("category") or "coding"),
                "source": _source_label(rule),
                "checkers": sorted({_text(spec.get("type")) for spec in applicable_specs}),
            }
            ranked.append((-score, card["id"], card))
        ranked.sort(key=lambda item: (item[0], item[1]))
        cards = [item[2] for item in ranked]
        return cards if max_rules is None else cards[: max(0, int(max_rules))]

    def check_file(
        self,
        path: str | Path,
        content: str,
        *,
        phase: str = "post",
        changed_files: Iterable[str | Path] = (),
        ai_rule_ids: Iterable[str] | None = None,
    ) -> list[CheckResult]:
        """Run file-local checks.  Companion checks run in check_change_set."""

        del changed_files  # Accepted for a stable public API.
        target = _normal_path(path)
        selected_ai_rules = (
            None
            if ai_rule_ids is None
            else {_text(rule_id) for rule_id in ai_rule_ids if _text(rule_id)}
        )
        results: list[CheckResult] = []
        for rule in self.rules:
            for declared_spec in _checker_specs(rule):
                spec = self._effective_spec(rule, declared_spec)
                checker = _text(spec.get("type")).lower()
                if checker == "companion_change":
                    continue
                try:
                    if not _applies(rule, spec, target, content):
                        continue
                    if checker == "regex_forbid":
                        results.extend(self._regex_forbid(rule, spec, target, content))
                    elif checker == "regex_require":
                        results.append(self._regex_require(rule, spec, target, content))
                    elif checker == "path_allow":
                        results.append(self._path_allow(rule, spec, target))
                    elif checker == "path_forbid":
                        results.append(self._path_forbid(rule, spec, target))
                    elif checker == "ai_review":
                        if (
                            selected_ai_rules is not None
                            and _text(rule.get("id")) not in selected_ai_rules
                        ):
                            continue
                        if phase in _strings(spec.get("phases") or ("post", "stop")):
                            results.append(
                                CheckResult(
                                    rule_id=_text(rule.get("id")) or "UNKNOWN-RULE",
                                    checker=checker,
                                    status="review",
                                    severity=_text(
                                        spec.get("severity")
                                        or rule.get("severity")
                                        or "major"
                                    ),
                                    message=_text(
                                        spec.get("prompt")
                                        or spec.get("message")
                                        or rule.get("statement")
                                    ),
                                    path=target,
                                    source=_source_label(rule),
                                    blocking=False,
                                )
                            )
                except re.error as exc:
                    error = _configuration_error(rule, spec, target, str(exc))
                    error.blocking = self.fail_closed
                    results.append(error)
        if self.fail_closed:
            for result in results:
                if result.status == "error":
                    result.blocking = True
        return results

    def _patterns(self, spec: Mapping[str, Any]) -> list[str]:
        return _strings(_first(spec, ("patterns", "pattern", "regex", "required_regex")))

    def _regex_forbid(
        self,
        rule: Mapping[str, Any],
        spec: Mapping[str, Any],
        path: str,
        content: str,
    ) -> list[CheckResult]:
        patterns = self._patterns(spec)
        if not patterns:
            return [_configuration_error(rule, spec, path, "regex_forbid 缺少 pattern")]
        flags = _regex_flags(spec.get("flags"))
        limit = max(1, int(spec.get("max_findings", 3) or 3))
        findings: list[CheckResult] = []
        for pattern in patterns:
            for match in re.finditer(pattern, content, flags):
                findings.append(
                    _result(
                        rule,
                        spec,
                        checker="regex_forbid",
                        status="fail",
                        path=path,
                        line=_line_number(content, match.start()),
                        evidence=_evidence(match.group(0)),
                    )
                )
                if len(findings) >= limit:
                    return findings
        if findings:
            return findings
        return [
            _result(
                rule,
                spec,
                checker="regex_forbid",
                status="pass",
                path=path,
                message="未发现禁止模式",
            )
        ]

    def _regex_require(
        self,
        rule: Mapping[str, Any],
        spec: Mapping[str, Any],
        path: str,
        content: str,
    ) -> CheckResult:
        patterns = self._patterns(spec)
        if not patterns:
            return _configuration_error(rule, spec, path, "regex_require 缺少 pattern")
        flags = _regex_flags(spec.get("flags"))
        require_all = bool(spec.get("require_all", True))
        matches = [re.search(pattern, content, flags) for pattern in patterns]
        passed = all(matches) if require_all else any(matches)
        return _result(
            rule,
            spec,
            checker="regex_require",
            status="pass" if passed else "fail",
            path=path,
            message=("已找到要求模式" if passed else None),
        )

    def _path_allow(
        self, rule: Mapping[str, Any], spec: Mapping[str, Any], path: str
    ) -> CheckResult:
        allowed = _strings(
            _first(spec, ("allow", "allowed", "allowed_paths", "patterns", "paths"))
        )
        if not allowed:
            return _configuration_error(rule, spec, path, "path_allow 缺少 allowed_paths")
        passed = _path_matches(path, allowed)
        return _result(
            rule,
            spec,
            checker="path_allow",
            status="pass" if passed else "fail",
            path=path,
            message=("文件位置符合要求" if passed else None),
        )

    def _path_forbid(
        self, rule: Mapping[str, Any], spec: Mapping[str, Any], path: str
    ) -> CheckResult:
        forbidden = _strings(
            _first(spec, ("forbid", "forbidden", "forbidden_paths", "patterns", "paths"))
        )
        if not forbidden:
            return _configuration_error(rule, spec, path, "path_forbid 缺少 forbidden_paths")
        passed = not _path_matches(path, forbidden)
        return _result(
            rule,
            spec,
            checker="path_forbid",
            status="pass" if passed else "fail",
            path=path,
            message=("文件位置未命中禁止路径" if passed else None),
        )

    def check_change_set(
        self,
        changed_files: Iterable[str | Path],
        *,
        contents: Mapping[str, str] | None = None,
        include_file_checks: bool = True,
        phase: str = "stop",
        ai_rule_ids_by_path: Mapping[str, Iterable[str]] | None = None,
    ) -> list[CheckResult]:
        """Check a complete task change set, including A-to-B contracts."""

        files = sorted({_normal_path(path) for path in changed_files if _text(path)})
        content_map = {_normal_path(key): value for key, value in (contents or {}).items()}
        ai_map = {
            _normal_path(key): value
            for key, value in (ai_rule_ids_by_path or {}).items()
        }
        results: list[CheckResult] = []
        if include_file_checks:
            for path in files:
                results.extend(
                    self.check_file(
                        path,
                        content_map.get(path, ""),
                        phase=phase,
                        ai_rule_ids=(ai_map.get(path) if ai_rule_ids_by_path is not None else None),
                    )
                )

        for rule in self.rules:
            for declared_spec in _checker_specs(rule):
                spec = self._effective_spec(rule, declared_spec)
                if _text(spec.get("type")).lower() != "companion_change":
                    continue
                triggers = _strings(
                    _first(
                        spec,
                        ("trigger_paths", "trigger", "when_changed", "changed"),
                    )
                )
                required = _strings(
                    _first(
                        spec,
                        ("requires", "required_paths", "companion_paths"),
                    )
                )
                if not triggers or not required:
                    error = _configuration_error(
                        rule,
                        spec,
                        "",
                        "companion_change 需要 trigger_paths 和 required_paths",
                    )
                    error.blocking = self.fail_closed
                    results.append(error)
                    continue
                triggered_by = [path for path in files if _path_matches(path, triggers)]
                if not triggered_by:
                    continue
                matches = {
                    pattern: [path for path in files if _path_matches(path, [pattern])]
                    for pattern in required
                }
                require_all = bool(spec.get("require_all", True))
                passed = all(matches.values()) if require_all else any(matches.values())
                missing = [pattern for pattern, hits in matches.items() if not hits]
                message = (
                    "配套文件变更完整"
                    if passed
                    else _text(spec.get("message") or rule.get("statement"))
                    + (f"；缺少：{', '.join(missing)}" if missing else "")
                )
                results.append(
                    _result(
                        rule,
                        spec,
                        checker="companion_change",
                        status="pass" if passed else "fail",
                        path=triggered_by[0],
                        message=message,
                        evidence=", ".join(triggered_by[:5]),
                    )
                )
        return results


def check_file(
    rules: Iterable[Any],
    path: str | Path,
    content: str,
    *,
    phase: str = "post",
    fail_closed: bool = True,
    ai_rule_ids: Iterable[str] | None = None,
) -> list[CheckResult]:
    """Functional convenience wrapper."""

    return PolicyChecker(rules, fail_closed=fail_closed).check_file(
        path, content, phase=phase, ai_rule_ids=ai_rule_ids
    )


def check_change_set(
    rules: Iterable[Any],
    changed_files: Iterable[str | Path],
    *,
    contents: Mapping[str, str] | None = None,
    fail_closed: bool = True,
    ai_rule_ids_by_path: Mapping[str, Iterable[str]] | None = None,
) -> list[CheckResult]:
    """Functional convenience wrapper."""

    return PolicyChecker(rules, fail_closed=fail_closed).check_change_set(
        changed_files,
        contents=contents,
        ai_rule_ids_by_path=ai_rule_ids_by_path,
    )


__all__ = [
    "CheckResult",
    "PolicyChecker",
    "SUPPORTED_CHECKERS",
    "validate_checker_rules",
    "check_change_set",
    "check_file",
]
