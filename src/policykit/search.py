"""Dependency-free lexical search with optional semantic-vector blending.

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
from typing import Any, Sequence

from .model import PolicyRule


_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_LATIN_TOKEN_RE = re.compile(
    r"@?[A-Za-z_$][A-Za-z0-9_$]*(?:[.\-/:\\][A-Za-z0-9_$*]+)*|\d+(?:\.\d+)*"
)
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_LOW_SIGNAL_TOKENS = frozenset(
    {
        "java",
        "src",
        "main",
        "test",
        "com",
        "org",
        "net",
        "io",
        "get",
        "set",
        "value",
        "values",
        "data",
        "object",
        "string",
        "request",
        "response",
        "result",
        "创建",
        "新增",
        "修改",
        "编写",
        "实现",
        "代码",
    }
)
_RELATIVE_SCORE_FLOOR = 0.22


def _ordered_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def tokenize(text: str) -> list[str]:
    """Tokenize identifiers and Chinese text without a dictionary dependency.

    Chinese runs emit phrases plus adjacent bigrams/trigrams.  Identifiers
    emit their full form plus dot/underscore/path and camel-case components.
    This makes queries such as ``线程管理`` match rules phrased as ``线程池管理``
    while retaining exact API matches such as ``Map.of``.
    """

    normalized = unicodedata.normalize("NFKC", str(text or ""))
    tokens: list[str] = []

    for match in _CJK_RUN_RE.finditer(normalized):
        run = match.group(0)
        if len(run) <= 32:
            tokens.append(run)
        if len(run) == 1:
            tokens.append(run)
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
        tokens.extend(run[index : index + 3] for index in range(len(run) - 2))

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
        if len(run) <= 32:
            values.append(run)
        if len(run) == 1:
            values.append(run)
        values.extend(run[index : index + 2] for index in range(len(run) - 1))
        values.extend(run[index : index + 3] for index in range(len(run) - 2))
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


class PolicyIndexMetadataError(ValueError):
    """Raised when a runtime index does not match its approved rule bundle."""


def _normalized_bundle_id(value: str | None, *, required: bool = False) -> str:
    normalized = str(value or "").strip().casefold()
    if not normalized and not required:
        return ""
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise PolicyIndexMetadataError("bundle_id 必须是 64 位十六进制字符串")
    return normalized


def _validate_metadata_values(
    metadata: Mapping[str, str],
    *,
    expected_policy_version: str | None = None,
    expected_bundle_id: str | None = None,
) -> dict[str, str]:
    values = {str(key): str(value) for key, value in metadata.items()}
    if values.get("schema_version") != "1":
        raise PolicyIndexMetadataError(
            f"不支持的检索索引 schema_version：{values.get('schema_version') or '缺失'}"
        )

    actual_bundle_id = values.get("bundle_id", "")
    if actual_bundle_id:
        values["bundle_id"] = _normalized_bundle_id(actual_bundle_id)

    if expected_policy_version is not None:
        expected_version = str(expected_policy_version).strip()
        actual_version = values.get("policy_version", "")
        if actual_version != expected_version:
            raise PolicyIndexMetadataError(
                "检索索引 policy_version 与已批准规则不一致："
                f"expected={expected_version!r}, actual={actual_version or '缺失'!r}"
            )
    if expected_bundle_id is not None:
        expected_bundle = _normalized_bundle_id(expected_bundle_id, required=True)
        actual_bundle = values.get("bundle_id", "")
        if actual_bundle != expected_bundle:
            raise PolicyIndexMetadataError(
                "检索索引 bundle_id 与已批准规则不一致："
                f"expected={expected_bundle}, actual={actual_bundle or '缺失'}"
            )
    return values


def _scope_set(scopes: Iterable[str] | str | None) -> set[str] | None:
    if scopes is None:
        return None
    if isinstance(scopes, str):
        return {scopes.casefold()}
    return {str(scope).casefold() for scope in scopes}


def _query_terms(
    query: str, file_path: str, code: str
) -> tuple[list[str], list[str], list[str], Counter[str]]:
    query_tokens = [
        token for token in tokenize(query) if token not in _LOW_SIGNAL_TOKENS
    ]
    path_tokens = [
        token for token in tokenize(file_path) if token not in _LOW_SIGNAL_TOKENS
    ]
    code_tokens = [
        token for token in tokenize(code) if token not in _LOW_SIGNAL_TOKENS
    ]
    weighted: Counter[str] = Counter()
    weighted.update({term: 3.0 for term in query_tokens})
    weighted.update({term: 2.0 for term in path_tokens})
    weighted.update({term: 1.0 for term in code_tokens})
    return query_tokens, path_tokens, code_tokens, weighted


def _score_results(
    rules: Mapping[str, PolicyRule],
    term_frequencies: Mapping[str, Counter[str]],
    document_frequencies: Mapping[str, int],
    document_lengths: Mapping[str, int],
    *,
    total_documents: int,
    average_length: float,
    query: str,
    file_path: str,
    code: str,
    limit: int,
    scopes: Iterable[str] | str | None,
    categories: Iterable[str] | str | None,
) -> list[SearchResult]:
    """Score candidate documents using corpus-wide BM25 statistics."""

    if limit <= 0 or not rules or total_documents <= 0:
        return []
    query_tokens, path_tokens, code_tokens, weighted_query = _query_terms(
        query, file_path, code
    )
    if not weighted_query:
        return []

    requested_scopes = _scope_set(scopes)
    requested_categories = _scope_set(categories)
    normalized_query = unicodedata.normalize("NFKC", query).casefold()
    normalized_path = unicodedata.normalize("NFKC", file_path).casefold()
    normalized_code = unicodedata.normalize("NFKC", code).casefold()
    results: list[SearchResult] = []

    for rule_id, rule in rules.items():
        if requested_scopes and rule.scope.casefold() not in requested_scopes:
            continue
        if requested_categories and rule.category.casefold() not in requested_categories:
            continue
        frequencies = term_frequencies.get(rule_id, Counter())
        matched = [term for term in weighted_query if term in frequencies]
        if not matched:
            continue

        document_length = document_lengths.get(rule_id, 1)
        score = 0.0
        for term in matched:
            document_frequency = int(document_frequencies.get(term, 0))
            inverse_frequency = math.log(
                1.0
                + (total_documents - document_frequency + 0.5)
                / (document_frequency + 0.5)
            )
            frequency = frequencies[term]
            normalization = 1.5 * (
                1.0
                - 0.75
                + 0.75 * document_length / max(average_length, 1.0)
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
            normalized_query == rule_id_folded or rule_id_folded in normalized_query
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
    if not results:
        return []
    score_floor = max(0.0, results[0].score * _RELATIVE_SCORE_FLOOR)
    return [result for result in results if result.score >= score_floor][:limit]


class PolicySearchIndex:
    """In-memory BM25 index suited to hundreds or a few thousand rules."""

    def __init__(
        self,
        rules: Iterable[PolicyRule] = (),
        *,
        approved_only: bool = True,
        policy_version: str | None = None,
        bundle_id: str | None = None,
    ) -> None:
        self.approved_only = approved_only
        self.policy_version = str(policy_version or "").strip()
        self.bundle_id = _normalized_bundle_id(bundle_id) if bundle_id else ""
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

        return _score_results(
            self._rules,
            self._term_frequencies,
            self._document_frequencies,
            self._document_lengths,
            total_documents=len(self._rules),
            average_length=self._average_length,
            query=query,
            file_path=file_path,
            code=code,
            limit=limit,
            scopes=scopes,
            categories=categories,
        )

    def read_metadata(self) -> dict[str, str]:
        metadata = {
            "schema_version": "1",
            "rule_count": str(self.rule_count),
        }
        if self.policy_version:
            metadata["policy_version"] = self.policy_version
        if self.bundle_id:
            metadata["bundle_id"] = self.bundle_id
        return metadata

    def validate_metadata(
        self,
        *,
        expected_policy_version: str | None = None,
        expected_bundle_id: str | None = None,
    ) -> dict[str, str]:
        return _validate_metadata_values(
            self.read_metadata(),
            expected_policy_version=expected_policy_version,
            expected_bundle_id=expected_bundle_id,
        )

    def to_sqlite(self, path: str | Path) -> Path:
        return build_sqlite_index(
            self._rules.values(),
            path,
            approved_only=False,
            policy_version=self.policy_version or None,
            bundle_id=self.bundle_id or None,
        )

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
            policy_version=(
                str(payload.get("policy_version") or "")
                if isinstance(payload, Mapping)
                else None
            ),
            bundle_id=(
                str(payload.get("bundle_id") or "")
                if isinstance(payload, Mapping)
                else None
            ),
        )


def build_sqlite_index(
    rules: Iterable[PolicyRule],
    path: str | Path,
    *,
    approved_only: bool = True,
    policy_version: str | None = None,
    bundle_id: str | None = None,
    embeddings: Mapping[str, Sequence[float]] | None = None,
    embedding_model: str | None = None,
) -> Path:
    """Build an atomic, disposable SQLite postings index."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".building")
    if temporary.exists():
        temporary.unlink()

    selected = [rule for rule in rules if not approved_only or rule.active]
    selected_ids = {rule.id for rule in selected}
    normalized_policy_version = str(policy_version or "").strip()
    normalized_bundle_id = _normalized_bundle_id(bundle_id) if bundle_id else ""
    normalized_embeddings: dict[str, list[float]] = {}
    embedding_dimensions: set[int] = set()
    for rule_id, vector in (embeddings or {}).items():
        if str(rule_id) not in selected_ids:
            continue
        values = [float(value) for value in vector]
        if not values or any(not math.isfinite(value) for value in values):
            raise ValueError(f"规则 {rule_id} 的 embedding 向量无效")
        normalized_embeddings[str(rule_id)] = values
        embedding_dimensions.add(len(values))
    if len(embedding_dimensions) > 1:
        raise ValueError("规则 embedding 向量维度不一致")
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
            CREATE TABLE embeddings (
                rule_id TEXT PRIMARY KEY,
                model TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                vector_json TEXT NOT NULL,
                FOREIGN KEY (rule_id) REFERENCES rules(rule_id)
            );
            """
        )
        metadata = [
            ("schema_version", "1"),
            ("rule_count", str(len(selected))),
        ]
        if normalized_policy_version:
            metadata.append(("policy_version", normalized_policy_version))
        if normalized_bundle_id:
            metadata.append(("bundle_id", normalized_bundle_id))
        metadata.append(("embedding_count", str(len(normalized_embeddings))))
        if normalized_embeddings:
            metadata.append(("embedding_model", str(embedding_model or "unknown")))
            metadata.append(
                ("embedding_dimensions", str(next(iter(embedding_dimensions))))
            )
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)", metadata
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
            vector = normalized_embeddings.get(rule.id)
            if vector is not None:
                connection.execute(
                    "INSERT INTO embeddings(rule_id, model, dimensions, vector_json) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        rule.id,
                        str(embedding_model or "unknown"),
                        len(vector),
                        json.dumps(vector, separators=(",", ":")),
                    ),
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

    def read_metadata(self) -> dict[str, str]:
        connection = sqlite3.connect(self.path)
        try:
            rows = connection.execute("SELECT key, value FROM metadata").fetchall()
        except sqlite3.DatabaseError as exc:
            raise PolicyIndexMetadataError(f"无法读取检索索引 metadata：{exc}") from exc
        finally:
            connection.close()
        return {str(key): str(value) for key, value in rows}

    def validate_metadata(
        self,
        *,
        expected_policy_version: str | None = None,
        expected_bundle_id: str | None = None,
    ) -> dict[str, str]:
        metadata = _validate_metadata_values(
            self.read_metadata(),
            expected_policy_version=expected_policy_version,
            expected_bundle_id=expected_bundle_id,
        )
        try:
            expected_count = int(metadata.get("rule_count", ""))
        except ValueError as exc:
            raise PolicyIndexMetadataError("检索索引 rule_count metadata 无效") from exc
        connection = sqlite3.connect(self.path)
        try:
            actual_count = int(
                connection.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
            )
        except sqlite3.DatabaseError as exc:
            raise PolicyIndexMetadataError(f"检索索引 rules 表无效：{exc}") from exc
        finally:
            connection.close()
        if actual_count != expected_count:
            raise PolicyIndexMetadataError(
                "检索索引 rule_count 不一致："
                f"metadata={expected_count}, actual={actual_count}"
            )
        return metadata

    def _lexical_search(
        self,
        query: str = "",
        file_path: str = "",
        code: str = "",
        limit: int = 20,
        scopes: Iterable[str] | str | None = None,
        *,
        categories: Iterable[str] | str | None = None,
        expected_policy_version: str | None = None,
        expected_bundle_id: str | None = None,
    ) -> list[SearchResult]:
        self.validate_metadata(
            expected_policy_version=expected_policy_version,
            expected_bundle_id=expected_bundle_id,
        )
        query_tokens, path_tokens, code_tokens, weighted_query = _query_terms(
            query, file_path, code
        )
        terms = _ordered_unique((*query_tokens, *path_tokens, *code_tokens))
        if not weighted_query or limit <= 0:
            return []
        requested_scopes = _scope_set(scopes)
        requested_categories = _scope_set(categories)
        connection = sqlite3.connect(self.path)
        try:
            # A TEMP table avoids SQLite bind-variable limits without changing
            # the persistent, rebuildable index.
            connection.execute(
                "CREATE TEMP TABLE runtime_query_terms (token TEXT PRIMARY KEY)"
            )
            connection.executemany(
                "INSERT INTO runtime_query_terms(token) VALUES (?)",
                ((term,) for term in terms),
            )
            rows = connection.execute(
                "SELECT DISTINCT r.rule_id, r.payload "
                "FROM rules AS r "
                "JOIN postings AS p ON p.rule_id = r.rule_id "
                "JOIN runtime_query_terms AS q ON q.token = p.token"
            ).fetchall()

            rules: dict[str, PolicyRule] = {}
            for rule_id, payload in rows:
                rule = PolicyRule.from_dict(json.loads(payload))
                if requested_scopes and rule.scope.casefold() not in requested_scopes:
                    continue
                if (
                    requested_categories
                    and rule.category.casefold() not in requested_categories
                ):
                    continue
                rules[str(rule_id)] = rule
            if not rules:
                return []

            connection.execute(
                "CREATE TEMP TABLE runtime_candidates (rule_id TEXT PRIMARY KEY)"
            )
            connection.executemany(
                "INSERT INTO runtime_candidates(rule_id) VALUES (?)",
                ((rule_id,) for rule_id in rules),
            )
            term_frequencies: dict[str, Counter[str]] = {
                rule_id: Counter() for rule_id in rules
            }
            for rule_id, token, frequency in connection.execute(
                "SELECT p.rule_id, p.token, p.term_frequency "
                "FROM postings AS p "
                "JOIN runtime_query_terms AS q ON q.token = p.token "
                "JOIN runtime_candidates AS c ON c.rule_id = p.rule_id"
            ):
                term_frequencies[str(rule_id)][str(token)] = int(frequency)

            document_frequencies = {
                str(token): int(count)
                for token, count in connection.execute(
                    "SELECT p.token, COUNT(*) "
                    "FROM postings AS p "
                    "JOIN runtime_query_terms AS q ON q.token = p.token "
                    "GROUP BY p.token"
                )
            }
            document_lengths = {
                str(rule_id): max(1, int(length or 0))
                for rule_id, length in connection.execute(
                    "SELECT p.rule_id, SUM(p.term_frequency) "
                    "FROM postings AS p "
                    "JOIN runtime_candidates AS c ON c.rule_id = p.rule_id "
                    "GROUP BY p.rule_id"
                )
            }
            total_documents = int(
                connection.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
            )
            average_row = connection.execute(
                "SELECT AVG(CASE WHEN document_length < 1 THEN 1 "
                "ELSE document_length END) "
                "FROM ("
                "SELECT r.rule_id, COALESCE(SUM(p.term_frequency), 0) "
                "AS document_length "
                "FROM rules AS r LEFT JOIN postings AS p "
                "ON p.rule_id = r.rule_id GROUP BY r.rule_id"
                ")"
            ).fetchone()
            average_length = float(average_row[0] or 0.0)
        except sqlite3.DatabaseError as exc:
            raise PolicyIndexMetadataError(f"检索索引查询失败：{exc}") from exc
        finally:
            connection.close()

        return _score_results(
            rules,
            term_frequencies,
            document_frequencies,
            document_lengths,
            total_documents=total_documents,
            average_length=average_length,
            query=query,
            file_path=file_path,
            code=code,
            limit=limit,
            scopes=scopes,
            categories=categories,
        )

    @staticmethod
    def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
        if len(left) != len(right) or not left:
            return -1.0
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if left_norm == 0.0 or right_norm == 0.0:
            return -1.0
        return dot / (left_norm * right_norm)

    def _semantic_search(
        self,
        query_embedding: Sequence[float],
        *,
        limit: int,
        scopes: Iterable[str] | str | None,
        categories: Iterable[str] | str | None,
        min_similarity: float,
    ) -> list[SearchResult]:
        vector = [float(value) for value in query_embedding]
        if not vector or any(not math.isfinite(value) for value in vector):
            raise ValueError("查询 embedding 向量无效")
        metadata = self.read_metadata()
        if int(metadata.get("embedding_count", "0") or 0) <= 0:
            return []
        expected_dimensions = int(metadata.get("embedding_dimensions", "0") or 0)
        if expected_dimensions and len(vector) != expected_dimensions:
            raise PolicyIndexMetadataError(
                "查询 embedding 维度与索引不一致："
                f"query={len(vector)}, index={expected_dimensions}"
            )
        requested_scopes = _scope_set(scopes)
        requested_categories = _scope_set(categories)
        connection = sqlite3.connect(self.path)
        try:
            rows = connection.execute(
                "SELECT r.payload, e.vector_json FROM rules AS r "
                "JOIN embeddings AS e ON e.rule_id = r.rule_id"
            ).fetchall()
        except sqlite3.DatabaseError as error:
            raise PolicyIndexMetadataError(
                f"无法读取语义检索向量：{error}"
            ) from error
        finally:
            connection.close()
        results: list[SearchResult] = []
        for payload, raw_vector in rows:
            rule = PolicyRule.from_dict(json.loads(payload))
            if requested_scopes and rule.scope.casefold() not in requested_scopes:
                continue
            if (
                requested_categories
                and rule.category.casefold() not in requested_categories
            ):
                continue
            stored = [float(value) for value in json.loads(raw_vector)]
            similarity = self._cosine(vector, stored)
            if similarity < min_similarity:
                continue
            results.append(
                SearchResult(
                    rule=rule,
                    score=round(similarity, 6),
                    reasons=(f"语义相似度：{similarity:.3f}",),
                )
            )
        results.sort(key=lambda item: (-item.score, item.rule.id))
        return results[: max(limit, 0)]

    def search(
        self,
        query: str = "",
        file_path: str = "",
        code: str = "",
        limit: int = 20,
        scopes: Iterable[str] | str | None = None,
        *,
        categories: Iterable[str] | str | None = None,
        expected_policy_version: str | None = None,
        expected_bundle_id: str | None = None,
        query_embedding: Sequence[float] | None = None,
        semantic_weight: float = 0.4,
        min_similarity: float = 0.28,
    ) -> list[SearchResult]:
        """Run lexical BM25 and optionally blend cached semantic vectors."""

        expanded_limit = max(limit * 4, 40)
        lexical = self._lexical_search(
            query=query,
            file_path=file_path,
            code=code,
            limit=expanded_limit if query_embedding is not None else limit,
            scopes=scopes,
            categories=categories,
            expected_policy_version=expected_policy_version,
            expected_bundle_id=expected_bundle_id,
        )
        if query_embedding is None or limit <= 0:
            return lexical[: max(limit, 0)]
        semantic = self._semantic_search(
            query_embedding,
            limit=expanded_limit,
            scopes=scopes,
            categories=categories,
            min_similarity=min(1.0, max(-1.0, float(min_similarity))),
        )
        weight = min(1.0, max(0.0, float(semantic_weight)))
        lexical_by_id = {result.rule_id: result for result in lexical}
        semantic_by_id = {result.rule_id: result for result in semantic}
        merged: list[SearchResult] = []
        for rule_id in set(lexical_by_id) | set(semantic_by_id):
            lexical_result = lexical_by_id.get(rule_id)
            semantic_result = semantic_by_id.get(rule_id)
            rule = (
                lexical_result.rule if lexical_result is not None else semantic_result.rule
            )
            lexical_score = lexical_result.score if lexical_result else 0.0
            semantic_score = semantic_result.score if semantic_result else 0.0
            reasons = tuple(
                dict.fromkeys(
                    (*(
                        lexical_result.reasons if lexical_result else ()
                    ), *(
                        semantic_result.reasons if semantic_result else ()
                    ))
                )
            )
            matched_terms = (
                lexical_result.matched_terms if lexical_result is not None else ()
            )
            merged.append(
                SearchResult(
                    rule=rule,
                    score=round(lexical_score + weight * 8.0 * max(0.0, semantic_score), 6),
                    reasons=reasons,
                    matched_terms=matched_terms,
                )
            )
        merged.sort(
            key=lambda item: (
                -item.score,
                {"blocker": 0, "major": 1, "advisory": 2}.get(
                    item.rule.severity, 3
                ),
                item.rule.id,
            )
        )
        if not merged:
            return []
        score_floor = max(0.0, merged[0].score * _RELATIVE_SCORE_FLOOR)
        return [result for result in merged if result.score >= score_floor][:limit]


def _search_result_card(result: SearchResult) -> dict[str, Any]:
    return {
        "rule": result.rule.to_dict(),
        "id": result.rule.id,
        "title": result.rule.title,
        "statement": result.rule.statement,
        "severity": result.rule.severity,
        "category": result.rule.category,
        "source": " / ".join(
            part
            for part in (result.rule.source.document, result.rule.source.section)
            if part
        ),
        "checkers": [],
        "score": result.score,
        "reasons": list(result.reasons),
        "applicable": False,
    }


def retrieve_runtime_rules(
    search_index: str | Path | PolicySearchIndex | SQLitePolicyIndex,
    checker: Any,
    *,
    query: str = "",
    file_path: str = "",
    code: str = "",
    limit: int = 20,
    scopes: Iterable[str] | str | None = None,
    categories: Iterable[str] | str | None = None,
    expected_policy_version: str | None = None,
    expected_bundle_id: str | None = None,
    query_embedding: Sequence[float] | None = None,
    semantic_weight: float = 0.4,
    min_similarity: float = 0.28,
) -> list[dict[str, Any]]:
    """Merge ranked retrieval with every directly applicable checker rule.

    Direct path/content/checker applicability is safety-relevant and therefore
    takes precedence over the normal ranking limit. If more directly
    applicable rules exist than ``limit``, all of them are returned.
    """

    if limit <= 0:
        return []
    index = (
        search_index
        if isinstance(search_index, (PolicySearchIndex, SQLitePolicyIndex))
        else SQLitePolicyIndex(search_index)
    )
    index.validate_metadata(
        expected_policy_version=expected_policy_version,
        expected_bundle_id=expected_bundle_id,
    )
    if isinstance(index, SQLitePolicyIndex):
        matches = index.search(
            query=query,
            file_path=file_path,
            code=code,
            limit=limit,
            scopes=scopes,
            categories=categories,
            expected_policy_version=expected_policy_version,
            expected_bundle_id=expected_bundle_id,
            query_embedding=query_embedding,
            semantic_weight=semantic_weight,
            min_similarity=min_similarity,
        )
    else:
        matches = index.search(
            query=query,
            file_path=file_path,
            code=code,
            limit=limit,
            scopes=scopes,
            categories=categories,
        )
    ranked_cards = [_search_result_card(result) for result in matches]
    ranked_by_id = {str(card["id"]): card for card in ranked_cards}
    applicable_cards = checker.applicable_rules(
        file_path, f"{query}\n{code}", max_rules=None
    )
    checker_rules = {
        str(rule.get("id") or "").strip(): dict(rule)
        for rule in (getattr(checker, "rules", ()) or ())
        if isinstance(rule, Mapping) and str(rule.get("id") or "").strip()
    }
    requested_scopes = _scope_set(scopes)
    requested_categories = _scope_set(categories)

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for applicable in applicable_cards:
        rule_id = str(applicable.get("id") or "").strip()
        if not rule_id or rule_id in seen:
            continue
        checker_rule = checker_rules.get(rule_id, {})
        if (
            requested_scopes
            and str(checker_rule.get("scope") or "").casefold()
            not in requested_scopes
        ):
            continue
        if (
            requested_categories
            and str(checker_rule.get("category") or "").casefold()
            not in requested_categories
        ):
            continue
        card = dict(ranked_by_id.get(rule_id, {}))
        card.update(dict(applicable))
        card["checkers"] = sorted(
            set((ranked_by_id.get(rule_id, {}).get("checkers") or ()))
            | set(applicable.get("checkers") or ())
        )
        card["direct_applicable"] = True
        card["applicable"] = True
        if "rule" not in card and checker_rule:
            card["rule"] = checker_rule
        if rule_id in ranked_by_id:
            card["score"] = ranked_by_id[rule_id].get("score", 0.0)
            card["reasons"] = ranked_by_id[rule_id].get("reasons", [])
        else:
            card["reasons"] = ["文件路径或 Checker 条件直接适用"]
        merged.append(card)
        seen.add(rule_id)

    for ranked in ranked_cards:
        rule_id = str(ranked.get("id") or "").strip()
        if not rule_id or rule_id in seen:
            continue
        if len(merged) >= limit:
            break
        ranked["direct_applicable"] = False
        ranked["applicable"] = False
        merged.append(ranked)
        seen.add(rule_id)
    return merged


__all__ = [
    "PolicyIndexMetadataError",
    "PolicySearchIndex",
    "SQLitePolicyIndex",
    "SearchResult",
    "build_sqlite_index",
    "retrieve_runtime_rules",
    "token_frequencies",
    "tokenize",
]
