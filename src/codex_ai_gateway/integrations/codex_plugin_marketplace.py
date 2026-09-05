"""Codex 本地插件市场读写、插件启用/禁用与只读诊断。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import tomlkit

from codex_ai_gateway.persistence.atomic_writer import atomic_write_bytes

MANIFEST_RELATIVE_PATHS = (
    Path(".agents") / "plugins" / "marketplace.json",
    Path(".claude-plugin") / "marketplace.json",
)


class CodexPluginMarketplaceError(RuntimeError):
    """插件市场配置错误。"""


def _parse_config(path: Path) -> tomlkit.TOMLDocument:
    if not path.exists():
        return tomlkit.document()
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, tomlkit.exceptions.ParseError) as exc:
        raise CodexPluginMarketplaceError(f"config.toml 读取失败: {exc}") from exc


def _write_config(path: Path, doc: tomlkit.TOMLDocument) -> None:
    text = tomlkit.dumps(doc)
    if not text.endswith("\n"):
        text += "\n"
    atomic_write_bytes(path, text.encode("utf-8"))


def _manifest_path(source: Path) -> Path | None:
    for candidate in MANIFEST_RELATIVE_PATHS:
        path = source / candidate
        if path.is_file():
            return path
    return None


def _load_manifest(source: Path) -> dict[str, Any]:
    path = _manifest_path(source)
    if path is None:
        joined = " 或 ".join(str(item) for item in MANIFEST_RELATIVE_PATHS)
        raise CodexPluginMarketplaceError(f"marketplace source 无效：缺少 {joined}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexPluginMarketplaceError(f"marketplace.json 读取失败: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("name"), str):
        raise CodexPluginMarketplaceError("marketplace.json 必须是包含 name 的对象")
    return manifest


def _validate_marketplace_name(name: str) -> str:
    value = name.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value):
        raise CodexPluginMarketplaceError(
            "marketplace 名称只能包含小写字母、数字、连字符和下划线"
        )
    if value.startswith("openai-"):
        raise CodexPluginMarketplaceError("openai-* 是 Codex 保留名，不能注册为本地市场")
    return value


def _configured_marketplaces(doc: tomlkit.TOMLDocument, config_path: Path) -> list[dict[str, Any]]:
    marketplaces: list[dict[str, Any]] = []
    table = doc.get("marketplaces")
    if not isinstance(table, dict):
        return marketplaces
    for name, item in table.items():
        source = item.get("source") if isinstance(item, dict) else None
        source_path = Path(str(source)) if source else None
        manifest = _manifest_path(source_path) if source_path else None
        marketplaces.append(
            {
                "name": str(name),
                "source_type": str(item.get("source_type", "")) if isinstance(item, dict) else "",
                "source": str(source) if source else None,
                "manifest_path": str(manifest) if manifest else None,
                "manifest_valid": manifest is not None,
                "plugin_count": 0,
                "plugins": [],
            }
        )
    del config_path
    return marketplaces


def _plugin_rows(doc: tomlkit.TOMLDocument) -> list[dict[str, Any]]:
    plugins_table = doc.get("plugins")
    if not isinstance(plugins_table, dict):
        return []
    rows = []
    for plugin_id, item in plugins_table.items():
        enabled = item.get("enabled", False) if isinstance(item, dict) else False
        rows.append(
            {
                "plugin_id": str(plugin_id),
                "enabled": bool(enabled),
                "config": dict(item) if isinstance(item, dict) else {},
            }
        )
    return rows


def _marketplace_by_name(
    doc: tomlkit.TOMLDocument, marketplace_name: str
) -> tuple[dict[Any, Any], Path]:
    marketplaces = doc.get("marketplaces")
    if not isinstance(marketplaces, dict) or marketplace_name not in marketplaces:
        raise CodexPluginMarketplaceError(f"marketplace 不存在: {marketplace_name}")
    item = marketplaces[marketplace_name]
    if not isinstance(item, dict) or item.get("source_type") != "local" or not item.get("source"):
        raise CodexPluginMarketplaceError("只支持 source_type = 'local' 的插件市场")
    return item, Path(str(item["source"]))


def list_plugin_marketplaces(config_path: str | Path) -> dict[str, Any]:
    """读取 config.toml 与本地 manifest，输出市场/插件诊断视图。"""
    path = Path(config_path)
    doc = _parse_config(path)
    marketplaces = _configured_marketplaces(doc, path)
    for marketplace in marketplaces:
        if marketplace["manifest_valid"] and marketplace["source"]:
            try:
                manifest = _load_manifest(Path(marketplace["source"]))
                plugins = manifest.get("plugins", [])
                marketplace["name"] = manifest["name"]
                marketplace["plugin_count"] = len(plugins) if isinstance(plugins, list) else 0
                marketplace["plugins"] = plugins if isinstance(plugins, list) else []
            except CodexPluginMarketplaceError:
                marketplace["manifest_valid"] = False
                marketplace["plugin_count"] = 0
                marketplace["plugins"] = []
    return {
        "config_path": str(path),
        "exists": path.exists(),
        "marketplaces": marketplaces,
        "plugins": _plugin_rows(doc),
    }


def register_local_marketplace(
    config_path: str | Path,
    *,
    name: str,
    source: str,
    default_enabled: bool = False,
) -> dict[str, Any]:
    """注册一个本地 marketplace，并可启用其全部插件。"""
    path = Path(config_path)
    marketplace_name = _validate_marketplace_name(name)
    source_path = Path(source).expanduser().resolve()
    manifest = _load_manifest(source_path)
    if manifest.get("name") != marketplace_name:
        raise CodexPluginMarketplaceError("manifest.name 与注册名称不一致")
    doc = _parse_config(path)
    marketplaces = doc.get("marketplaces")
    if not isinstance(marketplaces, dict):
        marketplaces = tomlkit.table()
        doc["marketplaces"] = marketplaces
    table = marketplaces.get(marketplace_name)
    if not isinstance(table, dict):
        table = tomlkit.table()
        marketplaces[marketplace_name] = table
    table["source_type"] = "local"
    table["source"] = str(source_path)
    if default_enabled:
        plugins_table = doc.get("plugins")
        if not isinstance(plugins_table, dict):
            plugins_table = tomlkit.table()
            doc["plugins"] = plugins_table
        for plugin in manifest.get("plugins", []):
            plugin_name = plugin.get("name") if isinstance(plugin, dict) else None
            if not plugin_name:
                continue
            plugin_id = f"{plugin_name}@{marketplace_name}"
            row = plugins_table.get(plugin_id)
            if not isinstance(row, dict):
                row = tomlkit.table()
                plugins_table[plugin_id] = row
            if "enabled" not in row:
                row["enabled"] = True
    _write_config(path, doc)
    return list_plugin_marketplaces(path)


def set_plugin_enabled(config_path: str | Path, *, plugin_id: str, enabled: bool) -> dict[str, Any]:
    """启用或禁用 plugin@marketplace。"""
    path = Path(config_path)
    value = plugin_id.strip()
    if value.count("@") != 1:
        raise CodexPluginMarketplaceError("plugin_id 必须是 plugin-name@marketplace-name")
    plugin_name, marketplace_name = value.split("@", 1)
    _validate_marketplace_name(marketplace_name)
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", plugin_name):
        raise CodexPluginMarketplaceError("plugin 名称包含非法字符")
    doc = _parse_config(path)
    _marketplace_item, marketplace_source = _marketplace_by_name(doc, marketplace_name)
    manifest = _load_manifest(marketplace_source)
    available = {
        plugin.get("name")
        for plugin in manifest.get("plugins", [])
        if isinstance(plugin, dict) and plugin.get("name")
    }
    if plugin_name not in available:
        raise CodexPluginMarketplaceError(f"plugin 不存在: {plugin_name}")
    plugins_table = doc.get("plugins")
    if not isinstance(plugins_table, dict):
        plugins_table = tomlkit.table()
        doc["plugins"] = plugins_table
    row = plugins_table.get(value)
    if not isinstance(row, dict):
        row = tomlkit.table()
        plugins_table[value] = row
    row["enabled"] = enabled
    _write_config(path, doc)
    return list_plugin_marketplaces(path)


def remove_marketplace(config_path: str | Path, *, name: str) -> dict[str, Any]:
    """移除本地 marketplace 注册，但不删除磁盘文件。"""
    path = Path(config_path)
    marketplace_name = _validate_marketplace_name(name)
    doc = _parse_config(path)
    marketplaces = doc.get("marketplaces")
    if not isinstance(marketplaces, dict) or marketplace_name not in marketplaces:
        raise CodexPluginMarketplaceError(f"marketplace 不存在: {marketplace_name}")
    del marketplaces[marketplace_name]
    if not marketplaces:
        del doc["marketplaces"]
    plugins_table = doc.get("plugins")
    if isinstance(plugins_table, dict):
        suffix = f"@{marketplace_name}"
        for plugin_id in [key for key in plugins_table.keys() if str(key).endswith(suffix)]:
            del plugins_table[plugin_id]
        if not plugins_table:
            del doc["plugins"]
    _write_config(path, doc)
    return list_plugin_marketplaces(path)
