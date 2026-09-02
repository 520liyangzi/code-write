"""Optional OpenAI-compatible enrichment and embedding integration.

The deterministic Markdown parser remains authoritative.  AI is used only to
add retrieval hints and semantic vectors, both of which are cached by content
hash so an unchanged policy corpus is never regenerated unnecessarily.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .io_utils import read_json, write_json
from .model import PolicyRule


class PolicyAIError(RuntimeError):
    """Raised for invalid AI configuration or provider responses."""


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() not in {"", "0", "false", "no", "off"}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True, slots=True)
class AISettings:
    provider: str = "disabled"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    timeout_seconds: float = 30.0
    required: bool = False
    llm_enabled: bool = False
    llm_model: str = ""
    enrichment_batch_size: int = 12
    embedding_enabled: bool = False
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int | None = None
    embedding_batch_size: int = 64
    semantic_weight: float = 0.4
    min_similarity: float = 0.28
    max_input_chars: int = 16_000

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "AISettings":
        ai = _mapping(config.get("ai"))
        llm = _mapping(ai.get("llm"))
        embedding = _mapping(ai.get("embedding"))
        dimensions_value = embedding.get("dimensions")
        dimensions = (
            int(dimensions_value)
            if dimensions_value not in (None, "", 0, "0")
            else None
        )
        provider = str(
            os.environ.get("POLICYKIT_AI_PROVIDER")
            or ai.get("provider")
            or "disabled"
        ).strip().casefold()
        return cls(
            provider=provider,
            base_url=str(
                os.environ.get("POLICYKIT_OPENAI_BASE_URL")
                or ai.get("base_url")
                or "https://api.openai.com/v1"
            ).strip().rstrip("/"),
            api_key_env=str(ai.get("api_key_env") or "OPENAI_API_KEY").strip(),
            timeout_seconds=max(1.0, float(ai.get("timeout_seconds") or 30)),
            required=_bool(ai.get("required"), False),
            llm_enabled=_bool(llm.get("enabled"), False),
            llm_model=str(
                os.environ.get("POLICYKIT_LLM_MODEL") or llm.get("model") or ""
            ).strip(),
            enrichment_batch_size=max(
                1, min(50, int(llm.get("batch_size") or 12))
            ),
            embedding_enabled=_bool(embedding.get("enabled"), False),
            embedding_model=str(
                os.environ.get("POLICYKIT_EMBEDDING_MODEL")
                or embedding.get("model")
                or "text-embedding-3-small"
            ).strip(),
            embedding_dimensions=dimensions,
            embedding_batch_size=max(
                1, min(2048, int(embedding.get("batch_size") or 64))
            ),
            semantic_weight=min(
                1.0, max(0.0, float(embedding.get("semantic_weight") or 0.4))
            ),
            min_similarity=min(
                1.0, max(-1.0, float(embedding.get("min_similarity") or 0.28))
            ),
            max_input_chars=max(
                1000, min(200_000, int(ai.get("max_input_chars") or 16_000))
            ),
        )

    @property
    def enrichment_active(self) -> bool:
        return self.provider != "disabled" and self.llm_enabled

    @property
    def embedding_active(self) -> bool:
        return self.provider != "disabled" and self.embedding_enabled


class PolicyAIProvider(Protocol):
    settings: AISettings

    def enrich(self, rules: Sequence[PolicyRule]) -> dict[str, dict[str, Any]]:
        ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...


class OpenAIProvider:
    """Small standard-library client for OpenAI's Responses/Embeddings APIs."""

    def __init__(self, settings: AISettings) -> None:
        if settings.provider not in {"openai", "openai-compatible"}:
            raise PolicyAIError(f"不支持的 AI provider：{settings.provider}")
        self.settings = settings

    def _api_key(self) -> str:
        key = os.environ.get(self.settings.api_key_env, "").strip()
        if not key:
            raise PolicyAIError(
                f"AI 已启用，但环境变量 {self.settings.api_key_env} 未配置"
            )
        return key

    def _post(self, resource: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = Request(
            f"{self.settings.base_url}/{resource.lstrip('/')}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "PolicyKit/0.2",
            },
        )
        try:
            with urlopen(request, timeout=self.settings.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as error:
            detail = error.read(1200).decode("utf-8", errors="replace")
            raise PolicyAIError(
                f"AI API HTTP {error.code}: {detail or error.reason}"
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise PolicyAIError(f"AI API 连接失败：{error}") from error
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise PolicyAIError("AI API 返回的不是有效 UTF-8 JSON") from error
        if not isinstance(value, dict):
            raise PolicyAIError("AI API 返回的 JSON 根节点不是对象")
        return value

    @staticmethod
    def _response_text(payload: Mapping[str, Any]) -> str:
        direct = payload.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        values: list[str] = []
        output = payload.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, Mapping):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, Mapping):
                        continue
                    text = part.get("text")
                    if isinstance(text, str) and text.strip():
                        values.append(text.strip())
        if not values:
            raise PolicyAIError("Responses API 未返回 output_text")
        return "\n".join(values)

    def enrich(self, rules: Sequence[PolicyRule]) -> dict[str, dict[str, Any]]:
        if not self.settings.llm_model:
            raise PolicyAIError("ai.llm.enabled=true 时必须配置 ai.llm.model")
        compact = []
        for rule in rules:
            metadata = rule.metadata or {}
            compact.append(
                {
                    "id": rule.id,
                    "title": rule.title,
                    "statement": rule.statement,
                    "description": metadata.get("description", ""),
                    "negative_example": metadata.get("negative_example", ""),
                    "positive_example": metadata.get("positive_example", ""),
                }
            )
        instructions = (
            "你是 Java 规范检索索引生成器。不要改写规则，也不要判断是否批准。"
            "只为每条规则生成用于代码场景召回的信息：retrieval_intent 是一句完整的触发场景；"
            "aliases 是中文近义表达；code_signals 是可能出现在 Java 代码中的类、方法、注解或语法；"
            "trigger_terms 是最精确的触发短语。严格返回 JSON 对象，格式为 "
            '{"rules":[{"id":"原ID","retrieval_intent":"...",'
            '"aliases":[],"code_signals":[],"trigger_terms":[]}]}。'
            "不要返回 Markdown，不要添加输入中不存在的规则。"
        )
        response = self._post(
            "responses",
            {
                "model": self.settings.llm_model,
                "instructions": instructions,
                "input": json.dumps(compact, ensure_ascii=False),
            },
        )
        text = self._response_text(response)
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as error:
            raise PolicyAIError("大模型检索增强结果不是有效 JSON") from error
        items = parsed.get("rules") if isinstance(parsed, Mapping) else None
        if not isinstance(items, list):
            raise PolicyAIError("大模型检索增强结果缺少 rules 数组")
        expected = {rule.id for rule in rules}
        result: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, Mapping):
                continue
            rule_id = str(item.get("id") or "").strip()
            if rule_id not in expected or rule_id in result:
                continue
            normalized: dict[str, Any] = {}
            intent = str(item.get("retrieval_intent") or "").strip()
            if intent:
                normalized["retrieval_intent"] = intent[:500]
            for key in ("aliases", "code_signals", "trigger_terms"):
                values = item.get(key)
                if isinstance(values, list):
                    normalized[key] = list(
                        dict.fromkeys(
                            str(value).strip()[:120]
                            for value in values[:40]
                            if str(value).strip()
                        )
                    )
            result[rule_id] = normalized
        missing = sorted(expected - set(result))
        if missing:
            raise PolicyAIError(
                "大模型检索增强结果缺少规则：" + "、".join(missing[:20])
            )
        return result

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not self.settings.embedding_model:
            raise PolicyAIError(
                "ai.embedding.enabled=true 时必须配置 ai.embedding.model"
            )
        if not texts:
            return []
        payload: dict[str, Any] = {
            "model": self.settings.embedding_model,
            "input": list(texts),
            "encoding_format": "float",
        }
        if self.settings.embedding_dimensions is not None:
            payload["dimensions"] = self.settings.embedding_dimensions
        response = self._post("embeddings", payload)
        data = response.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise PolicyAIError("Embedding API 返回数量与输入数量不一致")
        ordered: list[list[float] | None] = [None] * len(texts)
        for fallback_index, item in enumerate(data):
            if not isinstance(item, Mapping):
                raise PolicyAIError("Embedding API data 元素不是对象")
            index = item.get("index", fallback_index)
            vector = item.get("embedding")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or index < 0
                or index >= len(texts)
                or not isinstance(vector, list)
                or not vector
            ):
                raise PolicyAIError("Embedding API 返回的索引或向量无效")
            converted = [float(value) for value in vector]
            if any(not math.isfinite(value) for value in converted):
                raise PolicyAIError("Embedding API 返回了非有限数值")
            ordered[index] = converted
        if any(vector is None for vector in ordered):
            raise PolicyAIError("Embedding API 返回缺少部分输入")
        dimensions = {len(vector or ()) for vector in ordered}
        if len(dimensions) != 1:
            raise PolicyAIError("Embedding API 返回的向量维度不一致")
        return [vector for vector in ordered if vector is not None]


def create_provider(settings: AISettings) -> PolicyAIProvider:
    if settings.provider in {"openai", "openai-compatible"}:
        return OpenAIProvider(settings)
    raise PolicyAIError(f"不支持的 AI provider：{settings.provider}")


def _cache_payload(path: Path) -> dict[str, Any]:
    try:
        value = read_json(path, default={})
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"schema_version": 1, "entries": {}}
    if not isinstance(value, Mapping) or value.get("schema_version") != 1:
        return {"schema_version": 1, "entries": {}}
    entries = value.get("entries")
    return {
        "schema_version": 1,
        "entries": dict(entries) if isinstance(entries, Mapping) else {},
    }


def _rule_cache_key(rule: PolicyRule, model: str, purpose: str) -> str:
    payload = json.dumps(
        {
            "purpose": purpose,
            "model": model,
            "rule": rule.to_dict(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def enrich_rules_cached(
    rules: Iterable[PolicyRule],
    settings: AISettings,
    cache_path: str | Path,
    *,
    provider: PolicyAIProvider | None = None,
) -> dict[str, int]:
    """Enrich only changed/new rules and merge cached hints in place."""

    selected = list(rules)
    if not settings.enrichment_active:
        return {"enabled": 0, "cached": 0, "generated": 0}
    client = provider or create_provider(settings)
    path = Path(cache_path)
    cache = _cache_payload(path)
    entries = cache["entries"]
    keys = {
        rule.id: _rule_cache_key(rule, settings.llm_model, "retrieval-enrichment-v1")
        for rule in selected
    }
    resolved: dict[str, dict[str, Any]] = {}
    missing: list[PolicyRule] = []
    for rule in selected:
        cached = entries.get(keys[rule.id])
        if isinstance(cached, Mapping):
            resolved[rule.id] = dict(cached)
        else:
            missing.append(rule)

    for start in range(0, len(missing), settings.enrichment_batch_size):
        batch = missing[start : start + settings.enrichment_batch_size]
        generated = client.enrich(batch)
        for rule in batch:
            value = dict(generated.get(rule.id) or {})
            resolved[rule.id] = value
            entries[keys[rule.id]] = value

    for rule in selected:
        value = resolved.get(rule.id, {})
        metadata = dict(rule.metadata or {})
        for key in ("retrieval_intent", "aliases", "code_signals"):
            if value.get(key):
                metadata[key] = value[key]
        metadata["ai_enrichment_model"] = settings.llm_model
        rule.metadata = metadata
        extra = value.get("trigger_terms")
        if isinstance(extra, list):
            rule.trigger_terms = tuple(
                dict.fromkeys((*rule.trigger_terms, *(str(item) for item in extra)))
            )
    write_json(path, cache)
    return {
        "enabled": 1,
        "cached": len(selected) - len(missing),
        "generated": len(missing),
    }


def build_embeddings_cached(
    rules: Iterable[PolicyRule],
    settings: AISettings,
    cache_path: str | Path,
    *,
    provider: PolicyAIProvider | None = None,
) -> tuple[dict[str, list[float]], dict[str, int]]:
    """Return vectors for the corpus, embedding only cache misses."""

    selected = list(rules)
    if not settings.embedding_active:
        return {}, {"enabled": 0, "cached": 0, "generated": 0}
    client = provider or create_provider(settings)
    path = Path(cache_path)
    cache = _cache_payload(path)
    entries = cache["entries"]
    keys: dict[str, str] = {}
    inputs: dict[str, str] = {}
    vectors: dict[str, list[float]] = {}
    missing: list[PolicyRule] = []
    for rule in selected:
        text = rule.searchable_text()[: settings.max_input_chars]
        inputs[rule.id] = text
        key = sha256(
            json.dumps(
                {
                    "purpose": "embedding-v1",
                    "model": settings.embedding_model,
                    "dimensions": settings.embedding_dimensions,
                    "text": text,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        keys[rule.id] = key
        cached = entries.get(key)
        vector = cached.get("vector") if isinstance(cached, Mapping) else None
        if isinstance(vector, list) and vector:
            try:
                converted = [float(value) for value in vector]
            except (TypeError, ValueError):
                missing.append(rule)
            else:
                vectors[rule.id] = converted
        else:
            missing.append(rule)

    for start in range(0, len(missing), settings.embedding_batch_size):
        batch = missing[start : start + settings.embedding_batch_size]
        generated = client.embed([inputs[rule.id] for rule in batch])
        if len(generated) != len(batch):
            raise PolicyAIError("Embedding provider 返回数量不一致")
        for rule, vector in zip(batch, generated):
            vectors[rule.id] = vector
            entries[keys[rule.id]] = {
                "model": settings.embedding_model,
                "dimensions": len(vector),
                "vector": vector,
            }
    write_json(path, cache)
    return vectors, {
        "enabled": 1,
        "cached": len(selected) - len(missing),
        "generated": len(missing),
    }


def embed_runtime_query(
    settings: AISettings,
    *,
    query: str,
    file_path: str,
    code: str,
    index_metadata: Mapping[str, Any] | None = None,
    provider: PolicyAIProvider | None = None,
) -> tuple[list[float] | None, str]:
    """Embed one edit/search context; optional failures fall back to BM25."""

    if not settings.embedding_active:
        return None, ""
    expected_dimensions = 0
    if index_metadata is not None:
        try:
            embedding_count = int(index_metadata.get("embedding_count", "0") or 0)
            expected_dimensions = int(
                index_metadata.get("embedding_dimensions", "0") or 0
            )
        except (TypeError, ValueError) as error:
            raise PolicyAIError("正式索引的 embedding 元数据无效") from error
        index_model = str(index_metadata.get("embedding_model") or "").strip()
        if embedding_count <= 0:
            message = "正式索引尚无规则向量，请重新激活规则库"
            if settings.required:
                raise PolicyAIError(message)
            return None, message
        if index_model and index_model != settings.embedding_model:
            message = (
                "当前 embedding 模型与正式索引不一致："
                f"config={settings.embedding_model}, index={index_model}；请重新激活"
            )
            if settings.required:
                raise PolicyAIError(message)
            return None, message
    text = "\n".join(
        value
        for value in (
            f"开发任务：{query}" if query else "",
            f"目标文件：{file_path}" if file_path else "",
            f"代码上下文：\n{code}" if code else "",
        )
        if value
    )[: settings.max_input_chars]
    if not text:
        return None, ""
    try:
        vectors = (provider or create_provider(settings)).embed([text])
        vector = vectors[0]
        if expected_dimensions and len(vector) != expected_dimensions:
            raise PolicyAIError(
                "查询向量维度与正式索引不一致："
                f"query={len(vector)}, index={expected_dimensions}；请重新激活"
            )
        return vector, ""
    except (PolicyAIError, ValueError, OSError) as error:
        if settings.required:
            raise
        return None, str(error)


__all__ = [
    "AISettings",
    "OpenAIProvider",
    "PolicyAIError",
    "PolicyAIProvider",
    "build_embeddings_cached",
    "create_provider",
    "embed_runtime_query",
    "enrich_rules_cached",
]
