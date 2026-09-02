"""Conservative Markdown-to-policy candidate extraction.

This is intentionally a *candidate* extractor.  It finds normative sentences,
adds useful routing metadata, and leaves every result in ``pending_review``.
No extracted sentence becomes an active company rule without a human decision.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata

from .model import PolicyRule, SourceRef, make_rule_id


_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_LIST_RE = re.compile(
    r"^\s*(?:[-+*]|\d+[.)、]|[一二三四五六七八九十]+[、.)])\s+(.*)$"
)
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
_NORMATIVE_RE = re.compile(
    r"(?:"
    r"严禁|禁止|不得|不可(?!变)|不允许|不能|必须|务必|应当|应该|不应当|不应该|"
    r"需要|需(?!求)(?:要)?|须(?!知)|只允许|只能|仅限|统一(?:使用|采用|放置|配置|通过)|"
    r"建议|推荐|宜|不宜|尽量|避免|请勿|确保|"
    r"must\s+not|must|shall\s+not|shall|should\s+not|should|"
    r"may\s+not|required|prohibited|recommended"
    r")",
    re.IGNORECASE,
)
_STRONG_NEGATIVE_RE = re.compile(
    r"严禁|禁止|不得|不可(?!变)|不允许|不能|只允许|只能|仅限|must\s+not|"
    r"shall\s+not|prohibited",
    re.IGNORECASE,
)
_REQUIRED_RE = re.compile(
    r"必须|务必|应当|需要|须|确保|must|shall|required", re.IGNORECASE
)
_ADVISORY_RE = re.compile(
    r"建议|推荐|宜|不宜|尽量|避免|should|recommended", re.IGNORECASE
)

_CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "security": (
        "安全",
        "漏洞",
        "注入",
        "鉴权",
        "权限",
        "认证",
        "密码",
        "口令",
        "加密",
        "解密",
        "敏感",
        "脱敏",
        "csrf",
        "xss",
        "反序列化",
        "sql injection",
        "路径穿越",
    ),
    "performance": (
        "高性能",
        "性能",
        "并发",
        "线程",
        "线程池",
        "缓存",
        "内存",
        "吞吐",
        "延迟",
        "批量",
        "n+1",
        "复杂度",
        "锁",
        "连接池",
    ),
    "project": (
        "项目规范",
        "项目结构",
        "模块",
        "目录",
        "路径",
        "包路径",
        "工程结构",
        "文件位置",
        "配套文件",
        "同步修改",
        "pom.xml",
    ),
    "testing": ("测试", "单元测试", "集成测试", "mock", "覆盖率"),
    "logging": ("日志", "logger", "log.", "slf4j", "traceid", "链路"),
    "exception": ("异常", "exception", "throw", "catch", "错误码"),
    "database": (
        "数据库",
        "sql",
        "mybatis",
        "mapper",
        "事务",
        "索引",
        "查询",
    ),
    "api": (
        "接口",
        "controller",
        "request",
        "response",
        "spring mvc",
        "参数校验",
    ),
}

_CODE_SIGNAL_RE = re.compile(
    r"`[^`]+`|@[A-Z][A-Za-z0-9_]*|\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+\b|"
    r"\b(?:new|catch|throw|return)\s+[A-Za-z_$][\w$]*",
    re.IGNORECASE,
)
_PATH_SIGNAL_RE = re.compile(
    r"(?:路径|目录|模块|包(?:路径)?|package|module|src/main|[/\\]|\*\*/)",
    re.IGNORECASE,
)
_CHANGE_SET_RE = re.compile(
    r"(?:同时|同步|一并|配套|对应).{0,16}(?:新增|创建|修改|更新|删除|提供)|"
    r"(?:新增|创建|修改|更新|删除).{0,16}(?:同时|同步|一并|配套|对应)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class _Block:
    text: str
    raw: str
    line_start: int
    line_end: int
    section: str
    list_item: bool = False


def _strip_markdown(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"<!--.*?-->", " ", value)
    value = re.sub(r"!\[([^]]*)]\([^)]+\)", r"\1", value)
    value = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", value)
    value = re.sub(r"<https?://[^>]+>", " ", value)
    value = re.sub(r"[`*_~]", "", value)
    value = re.sub(r"^\s*>+\s?", "", value)
    value = re.sub(r"\s*\|\s*", "；", value).strip("； ")
    return re.sub(r"\s+", " ", value).strip()


def _section_text(stack: list[tuple[int, str]]) -> str:
    return " / ".join(title for _, title in stack)


def _iter_blocks(markdown: str) -> Iterable[_Block]:
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    section_stack: list[tuple[int, str]] = []
    in_fence = False
    fence_marker = ""
    in_frontmatter = bool(lines and lines[0].strip() == "---")
    paragraph: list[str] = []
    paragraph_start = 0
    paragraph_section = ""
    list_lines: list[str] = []
    list_start = 0
    list_section = ""

    def flush(end_line: int) -> _Block | None:
        nonlocal paragraph, paragraph_start, paragraph_section
        if not paragraph:
            return None
        raw = "\n".join(paragraph)
        text = _strip_markdown(" ".join(part.strip() for part in paragraph))
        block = _Block(
            text=text,
            raw=raw,
            line_start=paragraph_start,
            line_end=end_line,
            section=paragraph_section,
        )
        paragraph = []
        paragraph_start = 0
        paragraph_section = ""
        return block if text else None

    def flush_list(end_line: int) -> _Block | None:
        nonlocal list_lines, list_start, list_section
        if not list_lines:
            return None
        raw = "\n".join(list_lines)
        text = _strip_markdown(" ".join(part.strip() for part in list_lines))
        block = _Block(
            text=text,
            raw=raw,
            line_start=list_start,
            line_end=end_line,
            section=list_section,
            list_item=True,
        )
        list_lines = []
        list_start = 0
        list_section = ""
        return block if text else None

    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if in_frontmatter:
            if line_number > 1 and stripped == "---":
                in_frontmatter = False
            continue

        fence_match = _FENCE_RE.match(line)
        if fence_match:
            pending_list = flush_list(line_number - 1)
            if pending_list:
                yield pending_list
            pending = flush(line_number - 1)
            if pending:
                yield pending
            marker = fence_match.group(1)[0]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            continue
        if in_fence:
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            pending_list = flush_list(line_number - 1)
            if pending_list:
                yield pending_list
            pending = flush(line_number - 1)
            if pending:
                yield pending
            level = len(heading.group(1))
            title = _strip_markdown(heading.group(2))
            while section_stack and section_stack[-1][0] >= level:
                section_stack.pop()
            section_stack.append((level, title))
            # A heading can itself be a complete normative sentence.
            if _NORMATIVE_RE.search(title) and len(title) >= 6:
                yield _Block(
                    text=title,
                    raw=heading.group(2),
                    line_start=line_number,
                    line_end=line_number,
                    section=_section_text(section_stack[:-1]),
                )
            continue

        if not stripped or _TABLE_SEPARATOR_RE.match(line):
            pending_list = flush_list(line_number - 1)
            if pending_list:
                yield pending_list
            pending = flush(line_number - 1)
            if pending:
                yield pending
            continue

        list_match = _LIST_RE.match(line)
        if list_match:
            pending_list = flush_list(line_number - 1)
            if pending_list:
                yield pending_list
            pending = flush(line_number - 1)
            if pending:
                yield pending
            list_lines = [list_match.group(1).strip()]
            list_start = line_number
            list_section = _section_text(section_stack)
            continue

        # Treat table rows as independent candidates rather than merging an
        # entire Markdown table into one oversized paragraph.
        if stripped.count("|") >= 2 and (stripped.startswith("|") or stripped.endswith("|")):
            pending_list = flush_list(line_number - 1)
            if pending_list:
                yield pending_list
            pending = flush(line_number - 1)
            if pending:
                yield pending
            table_text = _strip_markdown(stripped)
            if table_text:
                yield _Block(
                    text=table_text,
                    raw=stripped,
                    line_start=line_number,
                    line_end=line_number,
                    section=_section_text(section_stack),
                )
            continue

        # Markdown allows indented and "lazy" continuation lines in list
        # items. Keeping them together prevents half-rules from being emitted.
        if list_lines:
            list_lines.append(line.strip())
            continue

        if not paragraph:
            paragraph_start = line_number
            paragraph_section = _section_text(section_stack)
        paragraph.append(line)

    pending_list = flush_list(len(lines))
    if pending_list:
        yield pending_list
    pending = flush(len(lines))
    if pending:
        yield pending


def _normative_parts(text: str) -> list[str]:
    """Keep coherent candidates while splitting very long prose paragraphs."""

    if len(text) <= 240:
        return [text] if _NORMATIVE_RE.search(text) else []
    parts = re.split(r"(?<=[。！？!?；;])\s*", text)
    return [part.strip() for part in parts if _NORMATIVE_RE.search(part)]


def _classify_category(document: str, section: str, statement: str) -> str:
    document_hint = Path(document).stem
    # Sample/disclaimer suffixes describe the fixture, not the rule domain.
    document_hint = re.sub(
        r"(?:仅供测试|仅测试|测试样例|示例文件|test[-_ ]?only)",
        "",
        document_hint,
        flags=re.IGNORECASE,
    )
    doc_context = f"{document_hint} {section}".lower()
    doc_context = re.sub(r"(?:仅供测试|仅测试|测试样例|test[-_ ]?only)", "", doc_context)
    full_context = f"{doc_context} {statement}".lower()
    scores: dict[str, int] = {}
    for category, terms in _CATEGORY_TERMS.items():
        doc_score = sum(3 for term in terms if term.lower() in doc_context)
        statement_score = sum(1 for term in terms if term.lower() in full_context)
        scores[category] = doc_score + statement_score

    if not scores or max(scores.values()) == 0:
        return "coding"
    # The insertion order gives security/performance/project priority on ties.
    return max(scores, key=scores.__getitem__)


def _suggest_severity(statement: str, category: str) -> str:
    if _STRONG_NEGATIVE_RE.search(statement):
        return "blocker"
    if _REQUIRED_RE.search(statement):
        return "major"
    if category == "security" and not _ADVISORY_RE.search(statement):
        return "major"
    return "advisory"


def _enforcement_candidates(statement: str) -> tuple[str, ...]:
    candidates: list[str] = ["coding_context"]
    if _CHANGE_SET_RE.search(statement):
        candidates.append("change_set_check")
    if _PATH_SIGNAL_RE.search(statement):
        candidates.append("path_check")
    if _CODE_SIGNAL_RE.search(statement) or re.search(
        r"(?:禁止|不得|必须).{0,24}(?:调用|使用|捕获|继承|实现|注解|日志)",
        statement,
        re.IGNORECASE,
    ):
        candidates.append("static_check")
    candidates.append("ai_review")
    return tuple(dict.fromkeys(candidates))


def _trigger_terms(raw: str, statement: str) -> tuple[str, ...]:
    terms: list[str] = []
    terms.extend(re.findall(r"`([^`]{1,100})`", raw))
    terms.extend(
        re.findall(
            r"@[A-Z][A-Za-z0-9_]*|\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+\b|"
            r"\b[A-Za-z_$][\w$]*(?:\.java|\.xml|\.yml|\.yaml|\.properties)\b",
            raw,
        )
    )
    for category, keywords in _CATEGORY_TERMS.items():
        del category
        terms.extend(term for term in keywords if term.lower() in statement.lower())
    cleaned = (_strip_markdown(term) for term in terms)
    return tuple(dict.fromkeys(term for term in cleaned if 1 < len(term) <= 100))


def _title(section: str, statement: str) -> str:
    leaf = section.split(" / ")[-1] if section else "规范候选"
    summary = re.split(r"[。；;！？!?]", statement, maxsplit=1)[0].strip()
    if len(summary) > 42:
        summary = summary[:41].rstrip() + "…"
    if leaf and leaf not in summary:
        return f"{leaf}：{summary}"[:80]
    return (summary or leaf)[:80]


def _confidence(block: _Block, statement: str) -> float:
    score = 0.55
    if block.list_item:
        score += 0.08
    if block.section:
        score += 0.05
    if _STRONG_NEGATIVE_RE.search(statement) or _REQUIRED_RE.search(statement):
        score += 0.12
    if _CODE_SIGNAL_RE.search(block.raw):
        score += 0.08
    if len(statement) < 12 or len(statement) > 400:
        score -= 0.08
    return round(min(0.95, max(0.25, score)), 2)


class MarkdownPolicyExtractor:
    """Extract reviewable rule candidates from Markdown text."""

    def __init__(self, *, scope: str = "unknown", id_prefix: str = "AUTO") -> None:
        self.scope = scope
        self.id_prefix = id_prefix

    def extract(
        self,
        markdown: str,
        *,
        source_name: str = "policy.md",
        scope: str | None = None,
    ) -> list[PolicyRule]:
        rules: list[PolicyRule] = []
        seen_statements: set[str] = set()
        effective_scope = scope or self.scope

        for block in _iter_blocks(markdown):
            for statement in _normative_parts(block.text):
                normalized = re.sub(r"\s+", "", statement).casefold()
                if len(normalized) < 5 or normalized in seen_statements:
                    continue
                seen_statements.add(normalized)
                category = _classify_category(
                    source_name, block.section, statement
                )
                severity = _suggest_severity(statement, category)
                triggers = _trigger_terms(block.raw, statement)
                rule = PolicyRule(
                    id=make_rule_id(
                        source_name,
                        block.section,
                        statement,
                        prefix=self.id_prefix,
                    ),
                    title=_title(block.section, statement),
                    statement=statement,
                    source=SourceRef(
                        document=source_name,
                        section=block.section,
                        line_start=block.line_start,
                        line_end=block.line_end,
                        quote=block.raw,
                    ),
                    scope=effective_scope,
                    category=category,
                    severity=severity,
                    enforcement_candidates=_enforcement_candidates(statement),
                    trigger_terms=triggers,
                    tags=tuple(
                        dict.fromkeys(
                            (
                                category,
                                severity,
                                "extracted-candidate",
                            )
                        )
                    ),
                    status="pending_review",
                    confidence=_confidence(block, statement),
                    metadata={"extractor": "markdown-v1"},
                )
                rules.append(rule)
        return rules


def extract_markdown(
    markdown: str,
    source_name: str = "policy.md",
    *,
    scope: str = "unknown",
    id_prefix: str = "AUTO",
) -> list[PolicyRule]:
    """Functional wrapper around :class:`MarkdownPolicyExtractor`."""

    return MarkdownPolicyExtractor(scope=scope, id_prefix=id_prefix).extract(
        markdown, source_name=source_name
    )


def read_markdown(path: str | Path) -> str:
    """Read common Chinese Markdown encodings without external libraries."""

    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeError(f"无法识别 Markdown 文件编码: {path}")


def extract_file(
    path: str | Path,
    *,
    scope: str = "unknown",
    id_prefix: str = "AUTO",
    source_name: str | None = None,
) -> list[PolicyRule]:
    path = Path(path)
    return extract_markdown(
        read_markdown(path),
        source_name=source_name or path.name,
        scope=scope,
        id_prefix=id_prefix,
    )


def extract_files(
    paths: Iterable[str | Path],
    *,
    scope: str = "unknown",
    scopes: Mapping[str, str] | None = None,
    id_prefix: str = "AUTO",
) -> list[PolicyRule]:
    """Extract all Markdown files in ``paths`` (directories are recursive).

    ``scopes`` can map an absolute path, a file name, or a directory name to a
    scope.  This keeps company/department/project selection in caller-owned
    configuration instead of guessing it from confidential file contents.
    """

    candidates: list[Path] = []
    for value in paths:
        path = Path(value)
        if path.is_dir():
            candidates.extend(path.rglob("*.md"))
            candidates.extend(path.rglob("*.markdown"))
        elif path.suffix.lower() in {".md", ".markdown"}:
            candidates.append(path)

    rules: list[PolicyRule] = []
    for path in sorted(set(candidates), key=lambda item: str(item).casefold()):
        file_scope = scope
        if scopes:
            possible_keys = [str(path.resolve()), str(path), path.name]
            possible_keys.extend(parent.name for parent in path.parents)
            for key in possible_keys:
                if key in scopes:
                    file_scope = scopes[key]
                    break
        rules.extend(extract_file(path, scope=file_scope, id_prefix=id_prefix))
    return rules


__all__ = [
    "MarkdownPolicyExtractor",
    "extract_file",
    "extract_files",
    "extract_markdown",
    "read_markdown",
]
