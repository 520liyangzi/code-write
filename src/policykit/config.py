from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping


CONFIG_NAME = "policykit.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "paths": {
        "source_dir": "policy-sources",
        "work_dir": ".policy-work",
        "candidates": ".policy-work/candidates.json",
        "review": ".policy-work/REVIEW_ME.md",
        "approved_rules": ".policy-work/approved-rules.json",
        "search_index": ".policy-work/search-index.db",
        "global_block": ".policy-work/GLOBAL_MD_BLOCK.md",
        "receipts_dir": ".policy-work/receipts",
        "audit_dir": ".policy-work/audit",
    },
    "runtime": {
        "require_receipt": True,
        "receipt_ttl_seconds": 900,
        "fail_closed": True,
        "file_extensions": [".java", ".xml", ".yml", ".yaml", ".properties"],
        "max_rules_per_edit": 20,
        "max_context_chars": 6000,
        "max_cli_output_chars": 8000,
        "block_severities": ["blocker", "major"],
        "ai_review_blocks_stop": True,
    },
    "review": {
        "global_core_limit": 40,
        "activate_only_approved": True,
    },
    "codegraph": {
        "enabled": False,
        "mode": "disabled",
        "command": "",
        "mcp_server": "",
        "tool_map": {},
        "notes": "Optional. Existing company index is never rebuilt unless explicitly configured.",
    },
}


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def find_home(start: str | os.PathLike[str] | None = None) -> Path:
    configured = os.environ.get("POLICYKIT_HOME") or os.environ.get(
        "CODAGENT_JAVA_POLICY_HOME"
    )
    if configured:
        return Path(configured).expanduser().resolve()

    current = Path(start or os.getcwd()).expanduser().resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_NAME).is_file():
            return candidate
    return current


def load_config(
    home: str | os.PathLike[str] | None = None,
    config_path: str | os.PathLike[str] | None = None,
) -> tuple[Path, dict[str, Any]]:
    root = find_home(home)
    configured_path = config_path or os.environ.get("POLICYKIT_CONFIG")
    path = (
        Path(configured_path).expanduser().resolve()
        if configured_path
        else root / CONFIG_NAME
    )
    if not path.exists():
        config = deepcopy(DEFAULT_CONFIG)
        return root, _apply_environment(config)
    with path.open("r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Configuration must be a JSON object: {path}")
    return root, _apply_environment(_deep_merge(DEFAULT_CONFIG, data))


def _apply_environment(config: dict[str, Any]) -> dict[str, Any]:
    path_variables = {
        "POLICYKIT_SOURCE_DIR": "source_dir",
        "POLICYKIT_APPROVED_RULES": "approved_rules",
        "POLICYKIT_SEARCH_INDEX": "search_index",
        "POLICYKIT_RECEIPTS_DIR": "receipts_dir",
        "POLICYKIT_AUDIT_DIR": "audit_dir",
    }
    for variable, key in path_variables.items():
        value = os.environ.get(variable)
        if value:
            config.setdefault("paths", {})[key] = value
    return config


def resolve_path(home: Path, config: Mapping[str, Any], key: str) -> Path:
    raw = config.get("paths", {}).get(key)
    if not raw:
        raise KeyError(f"Missing paths.{key} in {CONFIG_NAME}")
    path = Path(str(raw)).expanduser()
    return path.resolve() if path.is_absolute() else (home / path).resolve()


def ensure_layout(home: Path, config: Mapping[str, Any]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    resolve_path(home, config, "source_dir").mkdir(parents=True, exist_ok=True)
    for key in ("work_dir", "receipts_dir", "audit_dir"):
        resolve_path(home, config, key).mkdir(parents=True, exist_ok=True)


def write_default_config(home: str | os.PathLike[str], *, force: bool = False) -> Path:
    root = Path(home).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / CONFIG_NAME
    if path.exists() and not force:
        return path
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(DEFAULT_CONFIG, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)
    return path
