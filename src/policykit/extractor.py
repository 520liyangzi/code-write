"""Conservative Markdown-to-policy candidate extraction.

This is intentionally a *candidate* extractor.  It finds normative sentences,
adds useful routing metadata, and leaves every result in ``pending_review``.
No extracted sentence becomes an active company rule without a human decision.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
import unicodedata

from .model import PolicyRule, SourceRef, make_rule_id


_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_RULE_HEADING_RE = re.compile(
    r"^\s{0,3}(#{1,6})\s+"
    r"(?:(\d+(?:\.\d+)*)\s+)?"
    r"([A-Z][A-Z0-9_-]*(?:\.[A-Z0-9_-]+){1,})\s+"
    r"(.+?)\s*#*\s*$"
)
_STRUCTURED_FIELD_RE = re.compile(
    r"^\s*(?:\*\*)?\s*【\s*(级别|描述|反例|正例)\s*】\s*"
    r"(?:\*\*)?\s*[:：]?\s*(.*?)\s*$"
)
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

_MAX_QUALIFIED_RULE_ID_CHARS = 240


@dataclass(slots=True)
class _Block:
    text: str
    raw: str
    line_start: int
    line_end: int
    section: str
    list_item: bool = False


@dataclass(slots=True)
class _StructuredRuleBlock:
    rule_code: str
    number: str
    title: str
    parent_section: str
    raw: str
    fields: dict[str, str]
    line_start: int
    line_end: int


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


def _strip_field_markdown(value: str) -> str:
    """Clean prose while preserving useful line and code boundaries."""

    lines: list[str] = []
    in_fence = False
    for raw_line in value.strip().splitlines():
        if _FENCE_RE.match(raw_line):
            in_fence = not in_fence
            continue
        cleaned = raw_line.rstrip() if in_fence else _strip_markdown(raw_line)
        if cleaned or (lines and lines[-1]):
            lines.append(cleaned)
    return "\n".join(lines).strip()


def _iter_structured_rule_blocks(markdown: str) -> Iterable[_StructuredRuleBlock]:
    """Yield common Chinese standard blocks as one complete policy unit.

    Documents commonly encode a rule in the heading and put its rationale and
    examples in labelled fields.  Treating the heading as an independent
    sentence loses most of the rule and was the main cause of one-word/one-line
    candidates.
    """

    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    starts: list[tuple[int, re.Match[str], str]] = []
    headings: list[tuple[int, int]] = []
    heading_stack: list[tuple[int, str]] = []
    in_fence = False
    fence_marker = ""

    for index, line in enumerate(lines):
        fence_match = _FENCE_RE.match(line)
        if fence_match:
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
        if not heading:
            continue
        level = len(heading.group(1))
        headings.append((index, level))
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        rule_heading = _RULE_HEADING_RE.match(line)
        parent = _section_text(heading_stack)
        if rule_heading:
            starts.append((index, rule_heading, parent))
        heading_stack.append((level, _strip_markdown(heading.group(2))))

    for position, (start, match, parent) in enumerate(starts):
        next_rule = (
            starts[position + 1][0]
            if position + 1 < len(starts)
            else len(lines)
        )
        heading_level = len(match.group(1))
        next_section = next(
            (
                index
                for index, level in headings
                if index > start and level <= heading_level
            ),
            len(lines),
        )
        end = min(next_rule, next_section)
        field_values: dict[str, list[str]] = {
            "级别": [],
            "描述": [],
            "反例": [],
            "正例": [],
        }
        current = "描述"
        for line in lines[start + 1 : end]:
            field = _STRUCTURED_FIELD_RE.match(line)
            if field:
                current = field.group(1)
                inline = field.group(2).strip()
                if inline:
                    field_values[current].append(inline)
                continue
            field_values[current].append(line)
        fields = {
            key: "\n".join(value).strip()
            for key, value in field_values.items()
        }
        raw = "\n".join(lines[start:end]).strip()
        yield _StructuredRuleBlock(
            rule_code=match.group(3).strip(),
            number=(match.group(2) or "").strip(),
            title=_strip_markdown(match.group(4)),
            parent_section=parent,
            raw=raw,
            fields=fields,
            line_start=start + 1,
            line_end=end,
        )


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

    if len(text) <= 800:
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
    # Strip fenced-code delimiters before parsing inline code. Otherwise a
    # closing/opening pair of triple backticks can become one giant trigger.
    trigger_source = re.sub(
        r"^\s*(?:```+|~~~+)[^\n]*$",
        "",
        raw,
        flags=re.MULTILINE,
    )
    terms.extend(
        re.findall(r"(?<!`)`([^`\n]{1,100})`(?!`)", trigger_source)
    )
    terms.extend(
        re.findall(
            r"@[A-Z][A-Za-z0-9_]*|\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+\b|"
            r"\b[A-Za-z_$][\w$]*(?:\.java|\.xml|\.yml|\.yaml|\.properties)\b",
            trigger_source,
        )
    )
    terms.extend(
        value.rstrip("(")
        for value in re.findall(
            r"\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+\s*\(",
            trigger_source,
        )
    )
    cleaned = (_strip_markdown(term) for term in terms)
    return tuple(dict.fromkeys(term for term in cleaned if 1 < len(term) <= 100))


def _retrieval_hints(text: str) -> tuple[str, ...]:
    """Add stable Java intent aliases for lexical fallback retrieval."""

    groups: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
        (("小驼峰", "驼峰命名"), ("camelCase", "变量命名", "字段命名", "局部变量")),
        (("javadoc", "顶层public类", "顶层 public 类"),
         ("public class", "public interface", "public enum", "public record", "类注释")),
        (("反序列化",),
         ("ObjectInputStream", "readObject", "ObjectMapper.readValue", "JSON.parseObject", "外部数据")),
        (("格式化字符串", "string.format"),
         ("String.format", "Formatter", "format", "占位符", "外部输入", "请求参数")),
        (("外部数据", "外部输入", "用户输入"),
         ("request.getParameter", "不可信数据", "请求参数", "用户可控")),
    )
    folded = unicodedata.normalize("NFKC", text).casefold().replace(" ", "")
    hints: list[str] = []
    for needles, values in groups:
        if any(needle.casefold().replace(" ", "") in folded for needle in needles):
            hints.extend(values)
    return tuple(dict.fromkeys(hints))


def _direct_triggers(text: str) -> tuple[str, ...]:
    """Return only high-precision code signals safe for direct injection."""

    folded = unicodedata.normalize("NFKC", text).casefold().replace(" ", "")
    triggers: list[str] = []
    if "javadoc" in folded and any(
        value in folded for value in ("顶层public类", "publicclass")
    ):
        triggers.extend(
            ("public class", "public interface", "public enum", "public record")
        )
    if "反序列化" in folded:
        triggers.extend(
            (
                "ObjectInputStream",
                "readObject",
                "ObjectMapper.readValue",
                "JSON.parseObject",
            )
        )
    if "格式化字符串" in folded or "string.format" in folded:
        triggers.extend(("String.format", "Formatter"))
    return tuple(dict.fromkeys(triggers))


def _structured_severity(level: str, title: str, category: str) -> str:
    normalized = _strip_markdown(level).casefold()
    if any(value in normalized for value in ("建议", "推荐", "参考", "advisory")):
        return "advisory"
    return _suggest_severity(title, category)


def _structured_rule(
    block: _StructuredRuleBlock,
    *,
    source_name: str,
    scope: str,
) -> PolicyRule:
    description = _strip_field_markdown(block.fields.get("描述", ""))
    negative_example = _strip_field_markdown(block.fields.get("反例", ""))
    positive_example = _strip_field_markdown(block.fields.get("正例", ""))
    level = _strip_markdown(block.fields.get("级别", ""))
    complete_text = "\n".join(
        value
        for value in (
            block.title,
            description,
            negative_example,
            positive_example,
        )
        if value
    )
    category = _classify_category(source_name, block.parent_section, complete_text)
    hints = _retrieval_hints(complete_text)
    direct_triggers = _direct_triggers(complete_text)
    triggers = tuple(
        dict.fromkeys((*_trigger_terms(block.raw, complete_text), *hints))
    )
    heading = " ".join(
        value for value in (block.number, block.rule_code, block.title) if value
    )
    section = " / ".join(
        value for value in (block.parent_section, heading) if value
    )
    return PolicyRule(
        id=block.rule_code,
        title=block.title,
        statement=block.title,
        source=SourceRef(
            document=source_name,
            section=section,
            line_start=block.line_start,
            line_end=block.line_end,
            quote=block.raw,
        ),
        scope=scope,
        category=category,
        severity=_structured_severity(level, block.title, category),
        enforcement_candidates=_enforcement_candidates(complete_text),
        trigger_terms=triggers,
        tags=tuple(dict.fromkeys((category, "structured-rule", block.rule_code))),
        status="pending_review",
        confidence=0.96 if description else 0.9,
        metadata={
            "extractor": "markdown-structured-v2",
            "structured_format": True,
            "rule_number": block.number,
            "level": level,
            "description": description,
            "negative_example": negative_example,
            "positive_example": positive_example,
            "retrieval_hints": list(hints),
            "code_signals": list(_trigger_terms(block.raw, complete_text)),
            "direct_triggers": list(direct_triggers),
        },
    )


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

        structured = list(_iter_structured_rule_blocks(markdown))
        covered_lines: set[int] = set()
        seen_ids: set[str] = set()
        for block in structured:
            rule = _structured_rule(
                block,
                source_name=source_name,
                scope=effective_scope,
            )
            if rule.id in seen_ids:
                rule.id = make_rule_id(
                    source_name,
                    rule.source.section,
                    rule.statement,
                    prefix=rule.id,
                )
            seen_ids.add(rule.id)
            rules.append(rule)
            covered_lines.update(range(block.line_start, block.line_end + 1))

        for block in _iter_blocks(markdown):
            if any(
                line in covered_lines
                for line in range(block.line_start, block.line_end + 1)
            ):
                continue
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
                if rule.id in seen_ids:
                    continue
                seen_ids.add(rule.id)
                rules.append(rule)
        return rules


def _document_id_namespace(document: str) -> str:
    """Return a readable, stable namespace derived from a source filename."""

    normalized = unicodedata.normalize("NFKC", str(document or "document"))
    filename = normalized.replace("\\", "/").rsplit("/", 1)[-1]
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    namespace = re.sub(r"[^\w.-]+", "-", stem, flags=re.UNICODE).strip("-._")
    return (namespace or "document")[:96]


def _qualified_rule_id(namespace: str, original_id: str) -> str:
    candidate = f"{namespace}::{original_id}"
    if len(candidate) <= _MAX_QUALIFIED_RULE_ID_CHARS:
        return candidate
    digest = sha256(candidate.encode("utf-8")).hexdigest()[:10]
    original = original_id[:120]
    available = max(
        12,
        _MAX_QUALIFIED_RULE_ID_CHARS - len(original) - len(digest) - 3,
    )
    return f"{namespace[:available]}-{digest}::{original}"


def qualify_duplicate_rule_ids(rules: Iterable[PolicyRule]) -> list[PolicyRule]:
    """Namespace only cross-document ID collisions by source filename.

    Published standards frequently restart IDs in separate documents. The
    original ID remains searchable metadata, while the internal ID becomes a
    stable ``filename::original-id`` key suitable for review maps and SQLite.
    Rules whose IDs are already unique remain unchanged, preserving existing
    approvals and external references.
    """

    selected = list(rules)
    groups: dict[str, list[PolicyRule]] = {}
    for rule in selected:
        groups.setdefault(
            unicodedata.normalize("NFKC", rule.id).casefold(), []
        ).append(rule)

    occupied = {
        unicodedata.normalize("NFKC", group[0].id).casefold()
        for group in groups.values()
        if len(group) == 1
    }
    collision_groups = sorted(
        (group for group in groups.values() if len(group) > 1),
        key=lambda group: unicodedata.normalize("NFKC", group[0].id).casefold(),
    )
    for group in collision_groups:
        entries: list[tuple[PolicyRule, str, str]] = []
        candidate_counts: dict[str, int] = {}
        for rule in group:
            namespace = _document_id_namespace(rule.source.document)
            candidate = _qualified_rule_id(namespace, rule.id)
            folded = unicodedata.normalize("NFKC", candidate).casefold()
            candidate_counts[folded] = candidate_counts.get(folded, 0) + 1
            entries.append((rule, namespace, candidate))

        entries.sort(
            key=lambda item: (
                unicodedata.normalize("NFKC", item[0].source.document).casefold(),
                item[0].source.line_start,
                item[0].source.section.casefold(),
                item[0].statement.casefold(),
            )
        )
        for rule, namespace, candidate in entries:
            original_id = rule.id
            folded = unicodedata.normalize("NFKC", candidate).casefold()
            if candidate_counts[folded] > 1 or folded in occupied:
                document_key = unicodedata.normalize(
                    "NFKC", rule.source.document
                ).casefold()
                document_digest = sha256(document_key.encode("utf-8")).hexdigest()[:8]
                namespace = f"{namespace}-{document_digest}"
                candidate = _qualified_rule_id(namespace, original_id)
                folded = unicodedata.normalize("NFKC", candidate).casefold()
            if folded in occupied:
                rule_key = "\0".join(
                    (
                        rule.source.document,
                        rule.source.section,
                        str(rule.source.line_start),
                        rule.statement,
                    )
                )
                rule_digest = sha256(rule_key.encode("utf-8")).hexdigest()[:10]
                namespace = (
                    f"{_document_id_namespace(rule.source.document)}-{rule_digest}"
                )
                candidate = _qualified_rule_id(namespace, original_id)
                folded = unicodedata.normalize("NFKC", candidate).casefold()
            suffix = 2
            unique_candidate = candidate
            while folded in occupied:
                unique_candidate = _qualified_rule_id(
                    f"{namespace}-{suffix}", original_id
                )
                folded = unicodedata.normalize("NFKC", unique_candidate).casefold()
                suffix += 1

            metadata = dict(rule.metadata or {})
            metadata.update(
                {
                    "original_rule_id": original_id,
                    "id_namespace": namespace,
                    "id_collision_resolved": True,
                }
            )
            rule.id = unique_candidate
            rule.metadata = metadata
            rule.trigger_terms = tuple(
                dict.fromkeys((*rule.trigger_terms, original_id))
            )
            rule.tags = tuple(
                dict.fromkeys(
                    (*rule.tags, original_id, namespace, "document-namespaced-id")
                )
            )
            occupied.add(folded)
    return selected


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
    return qualify_duplicate_rule_ids(rules)


__all__ = [
    "MarkdownPolicyExtractor",
    "extract_file",
    "extract_files",
    "extract_markdown",
    "qualify_duplicate_rule_ids",
    "read_markdown",
]
