"""Database extension port for activated rule bundles.

JSON plus the local search index remain the fail-closed runtime source.  This
module mirrors a successfully activated bundle to a configurable database so
teams can connect a local service now and replace it with MySQL/PostgreSQL or
a vector-aware store later without changing extraction and review code.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Protocol, Sequence

from .model import PolicyRule


class PolicyDatabaseError(RuntimeError):
    """Raised when a configured database adapter cannot synchronize."""


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() not in {"", "0", "false", "no", "off"}


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    enabled: bool = False
    adapter: str = "sqlite"
    url: str = ""
    required: bool = False
    custom_factory: str = ""
    options: Mapping[str, Any] | None = None

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "DatabaseSettings":
        raw = config.get("database")
        database = dict(raw) if isinstance(raw, Mapping) else {}
        url_env = str(database.get("url_env") or "POLICYKIT_DATABASE_URL").strip()
        options = database.get("options")
        return cls(
            enabled=_bool(database.get("enabled"), False),
            adapter=str(database.get("adapter") or "sqlite").strip().casefold(),
            url=str(os.environ.get(url_env) or database.get("url") or "").strip(),
            required=_bool(database.get("required"), False),
            custom_factory=str(database.get("custom_factory") or "").strip(),
            options=dict(options) if isinstance(options, Mapping) else {},
        )


class RuleDatabasePort(Protocol):
    def sync_bundle(
        self,
        rules: Sequence[PolicyRule],
        *,
        policy_version: str,
        bundle_id: str,
        embeddings: Mapping[str, Sequence[float]],
    ) -> None:
        ...


class SQLiteRuleDatabase:
    """Built-in local database implementation of :class:`RuleDatabasePort`."""

    def __init__(self, url: str, *, home: Path) -> None:
        prefix = "sqlite:///"
        if not url.startswith(prefix):
            raise PolicyDatabaseError(
                "sqlite adapter 的 URL 必须使用 sqlite:///path/to/policy.db"
            )
        raw_path = url[len(prefix) :]
        if not raw_path:
            raise PolicyDatabaseError("sqlite database URL 缺少文件路径")
        path = Path(raw_path).expanduser()
        self.path = path.resolve() if path.is_absolute() else (home / path).resolve()

    def sync_bundle(
        self,
        rules: Sequence[PolicyRule],
        *,
        policy_version: str,
        bundle_id: str,
        embeddings: Mapping[str, Sequence[float]],
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS policykit_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS policykit_rules (
                    rule_id TEXT PRIMARY KEY,
                    policy_version TEXT NOT NULL,
                    bundle_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    embedding_json TEXT
                );
                """
            )
            with connection:
                connection.execute("DELETE FROM policykit_rules")
                connection.executemany(
                    "INSERT INTO policykit_rules("
                    "rule_id, policy_version, bundle_id, payload_json, embedding_json"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        (
                            rule.id,
                            policy_version,
                            bundle_id,
                            json.dumps(rule.to_dict(), ensure_ascii=False),
                            (
                                json.dumps(list(embeddings[rule.id]))
                                if rule.id in embeddings
                                else None
                            ),
                        )
                        for rule in rules
                    ),
                )
                connection.executemany(
                    "INSERT INTO policykit_metadata(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (
                        ("schema_version", "1"),
                        ("policy_version", policy_version),
                        ("bundle_id", bundle_id),
                        ("rule_count", str(len(rules))),
                        ("embedding_count", str(len(embeddings))),
                    ),
                )
        except sqlite3.DatabaseError as error:
            raise PolicyDatabaseError(f"本地 SQLite 规则库同步失败：{error}") from error
        finally:
            connection.close()


def _custom_port(settings: DatabaseSettings) -> RuleDatabasePort:
    target = settings.custom_factory
    if ":" not in target:
        raise PolicyDatabaseError(
            "custom adapter 必须配置 database.custom_factory=module:function"
        )
    module_name, function_name = target.split(":", 1)
    try:
        factory = getattr(import_module(module_name), function_name)
        port = factory(url=settings.url, options=dict(settings.options or {}))
    except (ImportError, AttributeError, TypeError) as error:
        raise PolicyDatabaseError(f"无法加载数据库 custom_factory：{target}") from error
    if not callable(getattr(port, "sync_bundle", None)):
        raise PolicyDatabaseError("custom_factory 返回对象缺少 sync_bundle 方法")
    return port


def database_status(config: Mapping[str, Any]) -> dict[str, Any]:
    settings = DatabaseSettings.from_config(config)
    return {
        "enabled": settings.enabled,
        "adapter": settings.adapter,
        "configured": bool(settings.url),
        "required": settings.required,
    }


def sync_database_bundle(
    config: Mapping[str, Any],
    home: str | Path,
    rules: Sequence[PolicyRule],
    *,
    policy_version: str,
    bundle_id: str,
    embeddings: Mapping[str, Sequence[float]] | None = None,
) -> dict[str, Any]:
    """Synchronize the optional database and return a secret-free status."""

    settings = DatabaseSettings.from_config(config)
    status = database_status(config)
    if not settings.enabled:
        return {**status, "synced": False, "reason": "disabled"}
    if not settings.url:
        error = PolicyDatabaseError(
            "database.enabled=true，但数据库 URL 未配置；请设置指定的 url_env"
        )
        if settings.required:
            raise error
        return {**status, "synced": False, "error": str(error)}
    try:
        if settings.adapter == "sqlite":
            port: RuleDatabasePort = SQLiteRuleDatabase(
                settings.url, home=Path(home).expanduser().resolve()
            )
        elif settings.adapter == "custom":
            port = _custom_port(settings)
        else:
            raise PolicyDatabaseError(
                f"未知 database.adapter：{settings.adapter}；支持 sqlite、custom"
            )
        port.sync_bundle(
            rules,
            policy_version=policy_version,
            bundle_id=bundle_id,
            embeddings=embeddings or {},
        )
    except (PolicyDatabaseError, OSError, ValueError) as error:
        if settings.required:
            raise
        return {**status, "synced": False, "error": str(error)}
    return {
        **status,
        "synced": True,
        "rule_count": len(rules),
        "embedding_count": len(embeddings or {}),
    }


__all__ = [
    "DatabaseSettings",
    "PolicyDatabaseError",
    "RuleDatabasePort",
    "SQLiteRuleDatabase",
    "database_status",
    "sync_database_bundle",
]
