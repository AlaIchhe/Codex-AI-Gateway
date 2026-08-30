"""config.toml/model catalog 读取、指纹、schema 校验与 tomlkit 保序编辑。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import tomlkit


class CodexConfigReadError(RuntimeError):
    pass


class CodexSchemaError(RuntimeError):
    pass


def resolve_config_path(codex_home: str) -> str:
    return str(Path(codex_home) / "config.toml")


def resolve_profile_path(codex_home: str, profile_name: str) -> str:
    return str(Path(codex_home) / f"{profile_name}.config.toml")


def resolve_catalog_path(codex_home: str, catalog_name: str = "model_catalog.json") -> str:
    return str(Path(codex_home) / catalog_name)


def read_config(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def file_fingerprint(path: str) -> dict[str, Any]:
    """mtime/size/content digest 与权限语义摘要。"""
    p = Path(path)
    if not p.exists():
        return {"exists": False, "mtime": None, "size": 0, "digest": None}
    stat = p.stat()
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    return {
        "exists": True,
        "mtime": stat.st_mtime,
        "size": stat.st_size,
        "digest": digest,
        "mode": oct(stat.st_mode & 0o777),
    }


def parse_toml(text: str) -> dict[str, Any]:
    try:
        doc = tomlkit.parse(text)
    except tomlkit.exceptions.ParseError as exc:
        raise CodexConfigReadError(f"config.toml 解析失败: {exc}") from exc
    return doc


def validate_codex_schema(config_text: str) -> list[dict[str, Any]]:
    """执行 schema 契约校验。返回校验事件列表（空=通过）。"""
    events: list[dict[str, Any]] = []
    try:
        doc = tomlkit.parse(config_text)
    except tomlkit.exceptions.ParseError as exc:
        return [{"kind": "schema_validation", "ok": False, "detail": str(exc)}]
    provider = doc.get("model_providers")
    if provider is not None and not isinstance(provider, dict):
        return [{"kind": "schema_validation", "ok": False, "detail": "model_providers 必须是表"}]
    if not config_text.strip():
        return [{"kind": "schema_validation", "ok": False, "detail": "config.toml 为空"}]
    return events


def apply_managed_config(
    *,
    raw_config: str,
    provider_id: str,
    base_url: str,
    env_key: str,
    model_catalog_path: str,
    gateway_token: str | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """用 atom 编辑生成受管 config.toml，返回 (新文本, diff, unchanged)。"""
    doc = tomlkit.parse(raw_config) if raw_config.strip() else tomlkit.document()
    providers = doc.get("model_providers")
    if providers is None:
        providers = tomlkit.table()
        doc["model_providers"] = providers
    if provider_id not in providers:
        providers[provider_id] = tomlkit.table()
    provider_table = providers[provider_id]
    added = []
    modified = []
    deleted = []
    if gateway_token:
        managed_values = (
            ("name", provider_id),
            ("base_url", base_url),
            ("experimental_bearer_token", gateway_token),
            ("wire_api", "responses"),
        )
    else:
        managed_values = (
            ("name", provider_id),
            ("base_url", base_url),
            ("env_key", env_key),
            ("wire_api", "responses"),
        )
    for key, value in managed_values:
        current = provider_table.get(key)
        if current is None:
            provider_table[key] = value
            added.append(f"model_providers.{provider_id}.{key}")
        elif str(current) != str(value):
            provider_table[key] = value
            modified.append(f"model_providers.{provider_id}.{key}")
    if gateway_token:
        # `env_value` 是本项目旧版遗留；官方 Codex 使用 experimental_bearer_token。
        for legacy_key in ("env_key", "env_value"):
            if provider_table.get(legacy_key) is not None:
                del provider_table[legacy_key]
                deleted.append(f"model_providers.{provider_id}.{legacy_key}")
    # FR-043: 受管默认 provider —— 应用时指向网关，漂移由自动维护调和
    current_provider = doc.get("model_provider")
    if current_provider is None:
        added.append("model_provider")
    elif str(current_provider) != provider_id:
        modified.append("model_provider")
    doc["model_provider"] = provider_id
    # FR-024/FR-044: 受管 catalog 路径引用
    current_catalog_ref = doc.get("model_catalog_json")
    if current_catalog_ref is None:
        added.append("model_catalog_json")
    elif str(current_catalog_ref) != str(model_catalog_path):
        modified.append("model_catalog_json")
    doc["model_catalog_json"] = model_catalog_path
    diff = {
        "added": added,
        "modified": modified,
        "deleted": [],
    }
    unchanged = _unchanged_summary(doc, provider_id)
    return tomlkit.dumps(doc), diff, unchanged


def _unchanged_summary(doc: Any, provider_id: str) -> dict[str, Any]:
    return {"unchanged_keys": list(doc.keys())}
