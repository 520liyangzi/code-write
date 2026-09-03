"""Core data structures for the policy kit.

The objects in this module deliberately contain no company rules.  Imported
rules always start in ``pending_review`` and only become active after an
explicit review decision.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
from pathlib import Path
import re
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1

VALID_STATUSES = frozenset(
    {"pending_review", "approved", "rejected", "needs_edit"}
)
VALID_SCOPES = frozenset({"company", "department", "project", "unknown"})
VALID_SEVERITIES = frozenset({"blocker", "major", "advisory"})


def _clean_string(value: Any) -> str:
    return str(value or "").strip()


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values: Iterable[Any] = (value,)
    else:
        values = value
    return tuple(item for item in (_clean_string(v) for v in values) if item)


@dataclass(slots=True)
class SourceRef:
    """Traceability information for a rule extracted from a source document."""

    document: str
    section: str = ""
    line_start: int = 0
    line_end: int = 0
    quote: str = ""

    def __post_init__(self) -> None:
        self.document = _clean_string(self.document)
        self.section = _clean_string(self.section)
        self.quote = _clean_string(self.quote)
        self.line_start = max(0, int(self.line_start or 0))
        self.line_end = max(self.line_start, int(self.line_end or self.line_start))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourceRef":
        return cls(
            document=data.get("document", ""),
            section=data.get("section", ""),
            line_start=data.get("line_start", 0),
            line_end=data.get("line_end", 0),
            quote=data.get("quote", ""),
        )


@dataclass(slots=True)
class PolicyRule:
    """A reviewable policy rule.

    ``severity`` and ``enforcement_candidates`` are suggestions made by the
    importer, not declarations of truth.  ``status`` is always
    ``pending_review`` for newly extracted rules.
    """

    id: str
    title: str
    statement: str
    source: SourceRef
    scope: str = "unknown"
    category: str = "coding"
    severity: str = "major"
    enforcement_candidates: tuple[str, ...] = ("ai_review",)
    trigger_terms: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    status: str = "pending_review"
    confidence: float = 0.5
    reviewer_notes: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.id = _clean_string(self.id)
        self.title = _clean_string(self.title)
        self.statement = _clean_string(self.statement)
        if not isinstance(self.source, SourceRef):
            self.source = SourceRef.from_dict(self.source)
        self.scope = _clean_string(self.scope).lower() or "unknown"
        self.category = _clean_string(self.category).lower() or "coding"
        self.severity = _clean_string(self.severity).lower() or "major"
        self.status = _clean_string(self.status).lower() or "pending_review"
        self.enforcement_candidates = _string_tuple(self.enforcement_candidates)
        self.trigger_terms = _string_tuple(self.trigger_terms)
        self.tags = _string_tuple(self.tags)
        self.reviewer_notes = _clean_string(self.reviewer_notes)
        self.confidence = min(1.0, max(0.0, float(self.confidence)))
        self.metadata = dict(self.metadata or {})

        if not self.id:
            raise ValueError("rule id must not be empty")
        if not self.statement:
            raise ValueError(f"rule {self.id}: statement must not be empty")
        if self.status not in VALID_STATUSES:
            raise ValueError(
                f"rule {self.id}: unknown status {self.status!r}; "
                f"expected one of {sorted(VALID_STATUSES)}"
            )
        if self.severity not in VALID_SEVERITIES:
            raise ValueError(
                f"rule {self.id}: unknown severity {self.severity!r}; "
                f"expected one of {sorted(VALID_SEVERITIES)}"
            )

    @property
    def active(self) -> bool:
        return self.status == "approved"

    def searchable_text(self) -> str:
        """Return the complete, runtime-relevant text for retrieval.

        Structured policy documents keep explanations and positive/negative
        examples in ``metadata``.  Those fields are deliberately searchable:
        an edit often contains an API such as ``String.format`` or
        ``ObjectInputStream.readObject`` that appears only in an example, not
        in the short normative title.
        """

        metadata = self.metadata or {}
        metadata_parts: list[str] = []
        for key in (
            "original_rule_id",
            "id_namespace",
            "level",
            "description",
            "negative_example",
            "positive_example",
            "retrieval_intent",
            "retrieval_hints",
            "aliases",
            "code_signals",
        ):
            value = metadata.get(key)
            if isinstance(value, str):
                metadata_parts.append(value)
            elif isinstance(value, (list, tuple, set)):
                metadata_parts.extend(str(item) for item in value if item)

        return "\n".join(
            part
            for part in (
                # Repetition supplies lightweight field weighting to both the
                # in-memory and SQLite BM25 indexes without a schema-specific
                # tokenizer dependency.
                "\n".join((self.id,) * 5),
                "\n".join((self.title,) * 4),
                "\n".join((self.statement,) * 3),
                self.source.section,
                "\n".join((" ".join(self.trigger_terms),) * 5),
                " ".join(self.tags),
                self.category,
                self.scope,
                "\n".join(metadata_parts),
            )
            if part
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "statement": self.statement,
            "source": self.source.to_dict(),
            "scope": self.scope,
            "category": self.category,
            "severity": self.severity,
            "enforcement_candidates": list(self.enforcement_candidates),
            "trigger_terms": list(self.trigger_terms),
            "tags": list(self.tags),
            "status": self.status,
            "confidence": self.confidence,
            "reviewer_notes": self.reviewer_notes,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PolicyRule":
        return cls(
            id=data.get("id", ""),
            title=data.get("title", ""),
            statement=data.get("statement", ""),
            source=SourceRef.from_dict(data.get("source", {})),
            scope=data.get("scope", "unknown"),
            category=data.get("category", "coding"),
            severity=data.get("severity", "major"),
            enforcement_candidates=data.get(
                "enforcement_candidates", ("ai_review",)
            ),
            trigger_terms=data.get("trigger_terms", ()),
            tags=data.get("tags", ()),
            status=data.get("status", "pending_review"),
            confidence=data.get("confidence", 0.5),
            reviewer_notes=data.get("reviewer_notes", ""),
            metadata=data.get("metadata", {}),
        )


@dataclass(slots=True)
class ReviewDecision:
    """A decision parsed from ``REVIEW_ME.md``."""

    rule_id: str
    decision: str = "pending_review"
    review_hash: str = ""
    edited_statement: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        self.rule_id = _clean_string(self.rule_id)
        self.decision = _clean_string(self.decision).lower() or "pending_review"
        self.review_hash = _clean_string(self.review_hash).lower()
        self.edited_statement = _clean_string(self.edited_statement)
        self.notes = _clean_string(self.notes)
        if self.decision not in {
            "approved",
            "rejected",
            "modified",
            "pending_review",
            "needs_edit",
        }:
            raise ValueError(f"unknown review decision: {self.decision!r}")


def make_rule_id(
    source_document: str,
    section: str,
    statement: str,
    *,
    prefix: str = "AUTO",
) -> str:
    """Create a stable, human-readable id for an extracted candidate rule."""

    source_stem = Path(source_document).stem.upper()
    source_slug = re.sub(r"[^A-Z0-9]+", "-", source_stem).strip("-")[:18]
    source_slug = source_slug or "POLICY"
    fingerprint_input = "\x1f".join(
        (source_document.strip(), section.strip(), statement.strip())
    )
    digest = sha256(fingerprint_input.encode("utf-8")).hexdigest()[:10].upper()
    safe_prefix = re.sub(r"[^A-Z0-9]+", "-", prefix.upper()).strip("-") or "AUTO"
    return f"{safe_prefix}-{source_slug}-{digest}"


__all__ = [
    "PolicyRule",
    "ReviewDecision",
    "SCHEMA_VERSION",
    "SourceRef",
    "VALID_SCOPES",
    "VALID_SEVERITIES",
    "VALID_STATUSES",
    "make_rule_id",
]
