"""Small, dependency-free policy search with Chinese character bigrams.

JSON remains the authoritative policy store.  The optional SQLite file is a
rebuildable acceleration artifact and contains explicit token postings rather
than relying on platform-specific FTS tokenizers.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import sqlite3
import unicodedata
from typing import Any

from .model import PolicyRule


_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_LATIN_TOKEN_RE = re.compile(
    r"@?[A-Za-z_$][A-Za-z0-9_$]*(?:[.\-/:\\][A-Za-z0-9_$*]+)*|\d+(?:\.\d+)*"
)
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _ordered_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def tokenize(text: str) -> list[str]:
    """Tokenize identifiers and Chinese text without a dictionary dependency.

    Chinese runs emit characters and adjacent-character bigrams.  Identifiers
    emit their full form plus dot/underscore/path and camel-case components.
    This makes queries such as ``线程管理`` match rules phrased as ``线程池管理``
    while retaining exact API matches such as ``Map.of``.
    """

    normalized = unicodedata.normalize("NFKC", str(text or ""))
    tokens: list[str] = []

    for match in _CJK_RUN_RE.finditer(normalized):
        run = match.group(0)
        if len(run) <= 16:
            tokens.append(run)
        tokens.extend(run)
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))

    without_cjk = _CJK_RUN_RE.sub(" ", normalized)
    for match in _LATIN_TOKEN_RE.finditer(without_cjk):
        original = match.group(0)
        full = original.casefold()
        tokens.append(full)
        pieces = re.split(r"[.\-/:\\_$@*]+", original)
        expanded: list[str] = []
        for piece in pieces:
            if not piece:
                continue
            expanded.extend(_CAMEL_BOUNDARY_RE.split(piece))
        tokens.extend(piece.casefold() for piece in expanded if piece)
    return _ordered_unique(tokens)


def token_frequencies(text: str) -> Counter[str]:
    """Frequency-preserving variant used by BM25 indexing."""

    normalized = unicodedata.normalize("NFKC", str(text or ""))
    values: list[str] = []
    for match in _CJK_RUN_RE.finditer(normalized):
        run = match.group(0)
        if len(run) <= 16:
            values.append(run)
        values.extend(run)
        values.extend(run[index : index + 2] for index in range(len(run) - 1))
    without_cjk = _CJK_RUN_RE.sub(" ", normalized)
    for match in _LATIN_TOKEN_RE.finditer(without_cjk):
        original = match.group(0)
        values.append(original.casefold())
        for piece in re.split(r"[.\-/:\\_$@*]+", original):
            if piece:
                values.extend(
                    part.casefold()
                    for part in _CAMEL_BOUNDARY_RE.split(piece)
                    if part
                )
    return Counter(value for value in values if value)


@dataclass(frozen=True, slots=True)
class SearchResult:
    rule: PolicyRule
    score: float
    reasons: tuple[str, ...]
    matched_terms: tuple[str, ...] = ()

    @property
    def rule_id(self) -> str:
        return self.rule.id

    @property
    def statement(self) -> str:
        return self.rule.statement

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule.to_dict(),
            "score": self.score,
            "reasons": list(self.reasons),
            "matched_terms": list(self.matched_terms),
        }

    def __getitem__(self, key: str) -> Any:
        """Allow lightweight dict-style use in CLI/templates."""

        if key == "rule":
            return self.rule
        if key == "score":
            return self.score
        if key == "reasons":
            return self.reasons
        if key == "matched_terms":
            return self.matched_terms
        if key == "rule_id":
            return self.rule.id
        raise KeyError(key)


def _scope_set(scopes: Iterable[str] | str | None) -> set[str] | None:
    if scopes is None:
        return None
    if isinstance(scopes, str):
        return {scopes.casefold()}
    return {str(scope).casefold() for scope in scopes}


class PolicySearchIndex:
    """In-memory BM25 index suited to hundreds or a few thousand rules."""

    def __init__(
        self,
        rules: Iterable[PolicyRule] = (),
        *,
        approved_only: bool = True,
    ) -> None:
        self.approved_only = approved_only
        self._rules: dict[str, PolicyRule] = {}
        self._term_frequencies: dict[str, Counter[str]] = {}
        self._document_frequencies: Counter[str] = Counter()
        self._document_lengths: dict[str, int] = {}
        self._average_length = 0.0
        self.build(rules)

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    @property
    def rules(self) -> tuple[PolicyRule, ...]:
        return tuple(self._rules.values())

    def build(self, rules: Iterable[PolicyRule]) -> "PolicySearchIndex":
        self._rules.clear()
        self._term_frequencies.clear()
        self._document_frequencies.clear()
        self._document_lengths.clear()

        selected: dict[str, PolicyRule] = {}
        for rule in rules:
            if self.approved_only and not rule.active:
                continue
            selected[rule.id] = rule

        self._rules.update(selected)
        for rule in selected.values():
            frequencies = token_frequencies(rule.searchable_text())
            self._term_frequencies[rule.id] = frequencies
            length = sum(frequencies.values()) or 1
            self._document_lengths[rule.id] = length
            self._document_frequencies.update(frequencies.keys())

        self._average_length = (
            sum(self._document_lengths.values()) / len(self._document_lengths)
            if self._document_lengths
            else 0.0
        )
        return self

    def add(self, rule: PolicyRule) -> None:
        """Add one rule, rebuilding small summary statistics safely."""

        rules = list(self._rules.values())
        rules = [item for item in rules if item.id != rule.id]
        rules.append(rule)
        self.build(rules)

    def search(
        self,
        query: str = "",
        file_path: str = "",
        code: str = "",
        limit: int = 20,
        scopes: Iterable[str] | str | None = None,
        *,
        categories: Iterable[str] | str | None = None,
    ) -> list[SearchResult]:
        """Search by task language, actual file path, and/or code features."""

        if limit <= 0 or not self._rules:
            return []
        query_tokens = tokenize(query)
        path_tokens = tokenize(file_path)
        code_tokens = tokenize(code)
        if not query_tokens and not path_tokens and not code_tokens:
            return []

        weighted_query: Counter[str] = Counter()
        weighted_query.update({term: 3.0 for term in query_tokens})
        weighted_query.update({term: 2.0 for term in path_tokens})
        weighted_query.update({term: 1.0 for term in code_tokens})

        requested_scopes = _scope_set(scopes)
        requested_categories = _scope_set(categories)
        total_documents = len(self._rules)
        results: list[SearchResult] = []
        normalized_query = unicodedata.normalize("NFKC", query).casefold()
        normalized_path = unicodedata.normalize("NFKC", file_path).casefold()
        normalized_code = unicodedata.normalize("NFKC", code).casefold()

        for rule_id, rule in self._rules.items():
            if requested_scopes and rule.scope.casefold() not in requested_scopes:
                continue
            if (
                requested_categories
                and rule.category.casefold() not in requested_categories
            ):
                continue
            frequencies = self._term_frequencies[rule_id]
            matched = [term for term in weighted_query if term in frequencies]
            if not matched:
                continue

            document_length = self._document_lengths[rule_id]
            score = 0.0
            for term in matched:
                document_frequency = self._document_frequencies[term]
                inverse_frequency = math.log(
                    1.0
                    + (total_documents - document_frequency + 0.5)
                    / (document_frequency + 0.5)
                )
                frequency = frequencies[term]
                normalization = 1.5 * (
                    1.0
                    - 0.75
                    + 0.75 * document_length / max(self._average_length, 1.0)
                )
                score += (
                    weighted_query[term]
                    * inverse_frequency
                    * frequency
                    * 2.5
                    / (frequency + normalization)
                )

            reasons: list[str] = []
            rule_id_folded = rule.id.casefold()
            if normalized_query and (
                normalized_query == rule_id_folded
                or rule_id_folded in normalized_query
            ):
                score += 12.0
                reasons.append(f"命中规则 ID：{rule.id}")

            query_matches = [term for term in query_tokens if term in frequencies]
            path_matches = [term for term in path_tokens if term in frequencies]
            code_matches = [term for term in code_tokens if term in frequencies]
            if query_matches:
                reasons.append("命中任务词：" + "、".join(query_matches[:8]))
            if path_matches:
                reasons.append("命中文件路径：" + "、".join(path_matches[:6]))
            if code_matches:
                reasons.append("命中代码特征：" + "、".join(code_matches[:8]))

            trigger_hits: list[str] = []
            for trigger in rule.trigger_terms:
                normalized_trigger = unicodedata.normalize("NFKC", trigger).casefold()
                if normalized_trigger and (
                    normalized_trigger in normalized_query
                    or normalized_trigger in normalized_path
                    or normalized_trigger in normalized_code
                ):
                    trigger_hits.append(trigger)
            if trigger_hits:
                score += 2.5 * len(trigger_hits)
                reasons.append("命中显式触发项：" + "、".join(trigger_hits[:6]))

            results.append(
                SearchResult(
                    rule=rule,
                    score=round(score, 6),
                    reasons=tuple(reasons or ("词元相关",)),
                    matched_terms=tuple(matched),
                )
            )

        results.sort(
            key=lambda item: (
                -item.score,
                {"blocker": 0, "major": 1, "advisory": 2}.get(
                    item.rule.severity, 3
                ),
                item.rule.id,
            )
        )
        return results[:limit]

    def to_sqlite(self, path: str | Path) -> Path:
        return build_sqlite_index(self._rules.values(), path, approved_only=False)

    @classmethod
    def from_json(
        cls, path: str | Path, *, approved_only: bool = True
    ) -> "PolicySearchIndex":
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        values = payload.get("rules", payload) if isinstance(payload, dict) else payload
        if not isinstance(values, list):
            raise ValueError("规则 JSON 必须是数组或包含 rules 数组的对象")
        return cls(
            (PolicyRule.from_dict(value) for value in values),
            approved_only=approved_only,
        )


def build_sqlite_index(
    rules: Iterable[PolicyRule],
    path: str | Path,
    *,
    approved_only: bool = True,
) -> Path:
    """Build an atomic, disposable SQLite postings index."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".building")
    if temporary.exists():
        temporary.unlink()

    selected = [rule for rule in rules if not approved_only or rule.active]
    connection = sqlite3.connect(temporary)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=DELETE;
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE rules (
                rule_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                scope TEXT NOT NULL,
                category TEXT NOT NULL
            );
            CREATE TABLE postings (
                token TEXT NOT NULL,
                rule_id TEXT NOT NULL,
                term_frequency INTEGER NOT NULL,
                PRIMARY KEY (token, rule_id),
                FOREIGN KEY (rule_id) REFERENCES rules(rule_id)
            );
            CREATE INDEX postings_rule_id_idx ON postings(rule_id);
            """
        )
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (("schema_version", "1"), ("rule_count", str(len(selected)))),
        )
        for rule in selected:
            connection.execute(
                "INSERT INTO rules(rule_id, payload, scope, category) "
                "VALUES (?, ?, ?, ?)",
                (
                    rule.id,
                    json.dumps(rule.to_dict(), ensure_ascii=False),
                    rule.scope,
                    rule.category,
                ),
            )
            frequencies = token_frequencies(rule.searchable_text())
            connection.executemany(
                "INSERT INTO postings(token, rule_id, term_frequency) "
                "VALUES (?, ?, ?)",
                ((term, rule.id, count) for term, count in frequencies.items()),
            )
        connection.commit()
    finally:
        # sqlite3.Connection's context manager commits/rolls back but does not
        # close. Explicit close is required before os.replace on Windows.
        connection.close()

    temporary.replace(destination)
    return destination.resolve()


class SQLitePolicyIndex:
    """Persistent candidate selection backed by a rebuildable SQLite index."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(self.path)

    def search(
        self,
        query: str = "",
        file_path: str = "",
        code: str = "",
        limit: int = 20,
        scopes: Iterable[str] | str | None = None,
        *,
        categories: Iterable[str] | str | None = None,
    ) -> list[SearchResult]:
        terms = _ordered_unique(
            (*tokenize(query), *tokenize(file_path), *tokenize(code))
        )
        # Stay below conservative SQLite bind-variable limits when a whole
        # source file is supplied by a hook. Task/path terms occur first and
        # therefore retain priority.
        terms = terms[:900]
        if not terms or limit <= 0:
            return []

        placeholders = ",".join("?" for _ in terms)
        requested_scopes = _scope_set(scopes)
        requested_categories = _scope_set(categories)
        connection = sqlite3.connect(self.path)
        try:
            rows = connection.execute(
                "SELECT DISTINCT r.payload "
                "FROM rules AS r JOIN postings AS p ON p.rule_id = r.rule_id "
                f"WHERE p.token IN ({placeholders})",
                terms,
            ).fetchall()
        finally:
            connection.close()

        rules = []
        for (payload,) in rows:
            rule = PolicyRule.from_dict(json.loads(payload))
            if requested_scopes and rule.scope.casefold() not in requested_scopes:
                continue
            if (
                requested_categories
                and rule.category.casefold() not in requested_categories
            ):
                continue
            rules.append(rule)

        # SQLite narrows candidates; the shared in-memory scorer preserves one
        # relevance implementation and consistent explanations.
        return PolicySearchIndex(rules, approved_only=False).search(
            query=query,
            file_path=file_path,
            code=code,
            limit=limit,
            scopes=scopes,
            categories=categories,
        )


__all__ = [
    "PolicySearchIndex",
    "SQLitePolicyIndex",
    "SearchResult",
    "build_sqlite_index",
    "token_frequencies",
    "tokenize",
]
