from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from policykit.ai import (
    AISettings,
    build_embeddings_cached,
    embed_runtime_query,
    enrich_rules_cached,
)
from policykit.database import PolicyDatabaseError, sync_database_bundle
from policykit.model import PolicyRule, SourceRef


def make_rules() -> list[PolicyRule]:
    return [
        PolicyRule(
            id="RULE-ONE",
            title="规则一",
            statement="变量必须采用小驼峰命名",
            source=SourceRef("coding.md", "命名"),
        ),
        PolicyRule(
            id="RULE-TWO",
            title="规则二",
            statement="禁止使用外部格式化字符串",
            source=SourceRef("security.md", "格式化"),
        ),
    ]


class FakeProvider:
    def __init__(self, settings: AISettings) -> None:
        self.settings = settings
        self.enriched_ids: list[str] = []
        self.embedded_batches: list[list[str]] = []

    def enrich(self, rules: list[PolicyRule]) -> dict[str, dict[str, object]]:
        self.enriched_ids.extend(rule.id for rule in rules)
        return {
            rule.id: {
                "retrieval_intent": f"触发 {rule.title}",
                "aliases": [f"{rule.title}别名"],
                "code_signals": [f"{rule.id}.signal"],
                "trigger_terms": [f"{rule.id}.trigger"],
            }
            for rule in rules
        }

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embedded_batches.append(list(texts))
        return [[float((len(text) % 7) + 1), 1.0] for text in texts]


class AIAndDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = AISettings(
            provider="openai",
            llm_enabled=True,
            llm_model="test-llm",
            enrichment_batch_size=10,
            embedding_enabled=True,
            embedding_model="test-embedding",
            embedding_batch_size=10,
        )

    def test_ai_enrichment_and_embeddings_are_incremental_by_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            enrichment_cache = root / "enrichment.json"
            embedding_cache = root / "embeddings.json"
            provider = FakeProvider(self.settings)

            first = make_rules()
            first_stats = enrich_rules_cached(
                first, self.settings, enrichment_cache, provider=provider
            )
            self.assertEqual(2, first_stats["generated"])
            self.assertEqual(["RULE-ONE", "RULE-TWO"], provider.enriched_ids)
            self.assertEqual("触发 规则一", first[0].metadata["retrieval_intent"])
            self.assertIn("RULE-ONE.trigger", first[0].trigger_terms)

            second = make_rules()
            second_stats = enrich_rules_cached(
                second, self.settings, enrichment_cache, provider=provider
            )
            self.assertEqual(2, second_stats["cached"])
            self.assertEqual(0, second_stats["generated"])
            self.assertEqual(["RULE-ONE", "RULE-TWO"], provider.enriched_ids)

            vectors, vector_stats = build_embeddings_cached(
                first, self.settings, embedding_cache, provider=provider
            )
            self.assertEqual({"RULE-ONE", "RULE-TWO"}, set(vectors))
            self.assertEqual(2, vector_stats["generated"])
            repeated_vectors, repeated_stats = build_embeddings_cached(
                first, self.settings, embedding_cache, provider=provider
            )
            self.assertEqual(vectors, repeated_vectors)
            self.assertEqual(2, repeated_stats["cached"])
            self.assertEqual(1, len(provider.embedded_batches))

            query_vector, warning = embed_runtime_query(
                self.settings,
                query="创建变量",
                file_path="Example.java",
                code="String userName;",
                provider=provider,
            )
            self.assertEqual("", warning)
            self.assertEqual(2, len(query_vector or []))

            batches_before = len(provider.embedded_batches)
            unavailable, warning = embed_runtime_query(
                self.settings,
                query="创建变量",
                file_path="Example.java",
                code="String userName;",
                index_metadata={"embedding_count": "0"},
                provider=provider,
            )
            self.assertIsNone(unavailable)
            self.assertIn("尚无规则向量", warning)
            self.assertEqual(batches_before, len(provider.embedded_batches))

            cache = json.loads(embedding_cache.read_text(encoding="utf-8"))
            self.assertEqual(1, cache["schema_version"])

    def test_sqlite_database_port_mirrors_activated_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            rules = make_rules()
            for rule in rules:
                rule.status = "approved"
            config = {
                "database": {
                    "enabled": True,
                    "adapter": "sqlite",
                    "url": "sqlite:///.policy-work/policies.db",
                    "required": True,
                }
            }
            status = sync_database_bundle(
                config,
                home,
                rules,
                policy_version="db-test-v1",
                bundle_id="a" * 64,
                embeddings={"RULE-ONE": [1.0, 0.0]},
            )
            self.assertTrue(status["synced"])
            database_path = home / ".policy-work" / "policies.db"
            connection = sqlite3.connect(database_path)
            try:
                rows = connection.execute(
                    "SELECT rule_id, embedding_json FROM policykit_rules ORDER BY rule_id"
                ).fetchall()
                metadata = dict(
                    connection.execute(
                        "SELECT key, value FROM policykit_metadata"
                    ).fetchall()
                )
            finally:
                connection.close()
            self.assertEqual(["RULE-ONE", "RULE-TWO"], [row[0] for row in rows])
            self.assertIsNotNone(rows[0][1])
            self.assertIsNone(rows[1][1])
            self.assertEqual("db-test-v1", metadata["policy_version"])

    def test_required_database_without_url_fails_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PolicyDatabaseError):
                sync_database_bundle(
                    {
                        "database": {
                            "enabled": True,
                            "adapter": "sqlite",
                            "url": "",
                            "url_env": "POLICYKIT_TEST_DATABASE_URL_UNSET",
                            "required": True,
                        }
                    },
                    directory,
                    [],
                    policy_version="v1",
                    bundle_id="b" * 64,
                )


if __name__ == "__main__":
    unittest.main()
