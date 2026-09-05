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

PLUGIN_MANIFEST_RELATIVE_PATH = Path(".codex-plugin") / "plugin.json"

IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

_PLUGIN_NAME_RE = re.compile(r"[a-zA-Z0-9._-]+")


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
    return value


def _validate_new_marketplace_name(name: str) -> str:
    value = _validate_marketplace_name(name)
    if value.startswith("openai-"):
        # 保留名校验只约束新建注册；Codex 官方内置市场（如 openai-bundled）
        # 会自行出现在 config.toml 中，对其插件的启用/禁用必须放行。
        raise CodexPluginMarketplaceError("openai-* 是 Codex 保留名，不能注册为本地市场")
    return value


def _marketplace_clone_dir(config_path: Path, marketplace_name: str) -> Path:
    """Codex 对 git 市场的克隆约定位置：$CODEX_HOME/.tmp/marketplaces/<市场名>。"""
    return config_path.parent / ".tmp" / "marketplaces" / marketplace_name


def _resolve_marketplace_source(
    item: Any, marketplace_name: str, codex_home: Path
) -> Path | None:
    """把 config 条目解析为磁盘上的市场根目录；解析失败返回 None。

    - local：source 即路径；
    - git：Codex 克隆约定目录（Codex 桌面端/CLI 负责拉取）；
    - 其他来源暂不支持。
    """
    if not isinstance(item, dict):
        return None
    source_type = str(item.get("source_type", ""))
    source = item.get("source")
    if source_type == "local" and source:
        return Path(str(source))
    if source_type == "git":
        clone_dir = codex_home / ".tmp" / "marketplaces" / marketplace_name
        return clone_dir if clone_dir.is_dir() else None
    return None


def _configured_marketplaces(doc: tomlkit.TOMLDocument, config_path: Path) -> list[dict[str, Any]]:
    marketplaces: list[dict[str, Any]] = []
    table = doc.get("marketplaces")
    if not isinstance(table, dict):
        return marketplaces
    codex_home = config_path.parent
    for name, item in table.items():
        resolved = _resolve_marketplace_source(item, str(name), codex_home)
        manifest = _manifest_path(resolved) if resolved else None
        source = item.get("source") if isinstance(item, dict) else None
        marketplaces.append(
            {
                "name": str(name),
                "source_type": str(item.get("source_type", "")) if isinstance(item, dict) else "",
                "source": str(source) if source else None,
                "resolved_source": str(resolved) if resolved else None,
                "manifest_path": str(manifest) if manifest else None,
                "manifest_valid": manifest is not None,
                "plugin_count": 0,
                "plugins": [],
                "catalog": [],
            }
        )
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
    doc: tomlkit.TOMLDocument, marketplace_name: str, codex_home: Path | None = None
) -> tuple[dict[Any, Any], Path]:
    marketplaces = doc.get("marketplaces")
    if not isinstance(marketplaces, dict) or marketplace_name not in marketplaces:
        raise CodexPluginMarketplaceError(f"marketplace 不存在: {marketplace_name}")
    item = marketplaces[marketplace_name]
    if not isinstance(item, dict):
        raise CodexPluginMarketplaceError("只支持 local / git 插件市场")
    home = codex_home if codex_home is not None else Path.cwd()
    resolved = _resolve_marketplace_source(item, marketplace_name, home)
    if resolved is None:
        source_type = str(item.get("source_type", ""))
        if source_type == "git":
            raise CodexPluginMarketplaceError(
                "git 市场尚未被 Codex 拉取到本地缓存（.tmp/marketplaces），无法操作"
            )
        raise CodexPluginMarketplaceError(
            f"不支持的市场来源（source_type = {source_type or '未知'}）"
        )
    return item, resolved


def _plugin_dir_for(root: Path, entry: dict[str, Any]) -> Path | None:
    """解析 manifest 条目指向的插件目录；目录必须落在市场根目录内。"""
    source = entry.get("source") if isinstance(entry, dict) else None
    relative = source.get("path") if isinstance(source, dict) else None
    name = entry.get("name")
    candidates: list[str] = []
    if isinstance(relative, str) and relative.strip():
        candidates.append(relative.strip())
    if isinstance(name, str) and name.strip():
        candidates.append(f"plugins/{name.strip()}")
    try:
        root_resolved = root.resolve()
    except OSError:
        return None
    for candidate in candidates:
        path = (root / candidate).resolve()
        if path.is_relative_to(root_resolved) and path.is_dir():
            return path
    return None


def _read_plugin_metadata(plugin_dir: Path) -> dict[str, Any] | None:
    path = plugin_dir / PLUGIN_MANIFEST_RELATIVE_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _resolve_icon_file(plugin_dir: Path, interface: dict[str, Any]) -> Path | None:
    """从 interface.logo / composerIcon 解析本地图标文件，拒绝越界与非图片路径。"""
    try:
        plugin_root = plugin_dir.resolve()
    except OSError:
        return None
    for key in ("logo", "composerIcon"):
        raw = interface.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        value = raw.strip()
        if value.lower().startswith(("http://", "https://", "data:")):
            continue
        candidate = (plugin_dir / value).resolve()
        if not candidate.is_relative_to(plugin_root):
            continue
        media_type = IMAGE_MEDIA_TYPES.get(candidate.suffix.lower())
        if media_type and candidate.is_file():
            return candidate
    return None


def _icon_url(marketplace_name: str, plugin_name: str, icon_file: Path | None) -> str | None:
    if icon_file is None:
        return None
    return (
        f"/admin/codex/plugin-marketplaces/{marketplace_name}/plugins/{plugin_name}/icon"
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str | int | float)]


def _catalog_entry(
    root: Path,
    entry: dict[str, Any],
    rows_by_id: dict[str, dict[str, Any]],
    marketplace_name: str,
) -> dict[str, Any]:
    name = str(entry.get("name") or "").strip()
    plugin_id = f"{name}@{marketplace_name}"
    row = rows_by_id.get(plugin_id)
    plugin_dir = _plugin_dir_for(root, entry)
    meta = _read_plugin_metadata(plugin_dir) if plugin_dir is not None else None
    interface = meta.get("interface") if isinstance(meta, dict) else None
    interface = interface if isinstance(interface, dict) else {}
    author = meta.get("author") if isinstance(meta, dict) else None
    author = author if isinstance(author, dict) else {}
    short_description = interface.get("shortDescription")
    if not isinstance(short_description, str) or not short_description:
        short_description = meta.get("description") if isinstance(meta, dict) else None
    icon_file = (
        _resolve_icon_file(plugin_dir, interface)
        if plugin_dir is not None and interface
        else None
    )
    category = entry.get("category")
    if not isinstance(category, str) or not category:
        category = interface.get("category")
    return {
        "plugin_id": plugin_id,
        "name": name,
        "configured": row is not None,
        "enabled": bool(row.get("enabled")) if row else False,
        "stale": False,
        "has_metadata": meta is not None,
        "display_name": interface.get("displayName") if isinstance(interface.get("displayName"), str) else None,
        "description": short_description if isinstance(short_description, str) else None,
        "long_description": interface.get("longDescription")
        if isinstance(interface.get("longDescription"), str)
        else None,
        "category": category if isinstance(category, str) else None,
        "version": meta.get("version") if isinstance(meta, dict) and isinstance(meta.get("version"), str) else None,
        "author": author.get("name") if isinstance(author.get("name"), str) else None,
        "developer_name": interface.get("developerName")
        if isinstance(interface.get("developerName"), str)
        else None,
        "keywords": _string_list(meta.get("keywords") if isinstance(meta, dict) else None),
        "capabilities": _string_list(interface.get("capabilities")),
        "homepage": meta.get("homepage") if isinstance(meta, dict) and isinstance(meta.get("homepage"), str) else None,
        "repository": meta.get("repository") if isinstance(meta, dict) and isinstance(meta.get("repository"), str) else None,
        "brand_color": interface.get("brandColor") if isinstance(interface.get("brandColor"), str) else None,
        "icon_url": _icon_url(marketplace_name, name, icon_file),
    }


def _stale_catalog_entries(
    rows_by_id: dict[str, dict[str, Any]],
    marketplace_names: set[str],
    manifest_names: set[str],
) -> list[dict[str, Any]]:
    """config.toml 中存在、但已不在市场 manifest 里的插件行。"""
    entries: list[dict[str, Any]] = []
    for plugin_id, row in rows_by_id.items():
        text = str(plugin_id)
        if text.count("@") < 1:
            continue
        plugin_name, _, marketplace = text.rpartition("@")
        if marketplace not in marketplace_names or plugin_name in manifest_names:
            continue
        entries.append(
            {
                "plugin_id": text,
                "name": plugin_name,
                "configured": True,
                "enabled": bool(row.get("enabled")),
                "stale": True,
                "has_metadata": False,
                "display_name": None,
                "description": None,
                "long_description": None,
                "category": None,
                "version": None,
                "author": None,
                "developer_name": None,
                "keywords": [],
                "capabilities": [],
                "homepage": None,
                "repository": None,
                "brand_color": None,
                "icon_url": None,
            }
        )
    return entries


def list_plugin_marketplaces(config_path: str | Path) -> dict[str, Any]:
    """读取 config.toml 与本地 manifest，输出市场/插件目录与启用状态诊断视图。"""
    path = Path(config_path)
    doc = _parse_config(path)
    marketplaces = _configured_marketplaces(doc, path)
    registered_names = {str(item["name"]) for item in marketplaces}
    rows = _plugin_rows(doc)
    rows_by_id = {str(row["plugin_id"]): row for row in rows}
    for marketplace in marketplaces:
        table_key = str(marketplace["name"])
        marketplace_names = {table_key}
        manifest_names: set[str] = set()
        if marketplace["manifest_valid"] and marketplace["resolved_source"]:
            try:
                manifest = _load_manifest(Path(marketplace["resolved_source"]))
                plugins = manifest.get("plugins", [])
                manifest_name = str(manifest["name"])
                marketplace_names.add(manifest_name)
                marketplace["name"] = manifest_name
                marketplace["plugin_count"] = len(plugins) if isinstance(plugins, list) else 0
                marketplace["plugins"] = plugins if isinstance(plugins, list) else []
            except CodexPluginMarketplaceError:
                marketplace["manifest_valid"] = False
                marketplace["plugin_count"] = 0
                marketplace["plugins"] = []
        manifest_names = {
            str(item.get("name"))
            for item in marketplace["plugins"]
            if isinstance(item, dict) and item.get("name")
        }
        catalog: list[dict[str, Any]] = []
        if marketplace["manifest_valid"] and marketplace["resolved_source"]:
            root = Path(str(marketplace["resolved_source"]))
            catalog = [
                _catalog_entry(root, entry, rows_by_id, str(marketplace["name"]))
                for entry in marketplace["plugins"]
                if isinstance(entry, dict) and entry.get("name")
            ]
        catalog.extend(_stale_catalog_entries(rows_by_id, marketplace_names, manifest_names))
        marketplace["catalog"] = catalog
    for row in rows:
        marketplace_part = str(row["plugin_id"]).rpartition("@")[2]
        row["marketplace_registered"] = marketplace_part in registered_names
    return {
        "config_path": str(path),
        "exists": path.exists(),
        "marketplaces": marketplaces,
        "plugins": rows,
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
    marketplace_name = _validate_new_marketplace_name(name)
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
    """启用或禁用 plugin@marketplace。

    启用要求插件仍存在于市场 manifest；禁用对 config.toml 中已存在的行放行，
    这样 manifest 变更后遗留的插件配置也能从 WebUI 摘除。
    """
    path = Path(config_path)
    value = plugin_id.strip()
    if value.count("@") != 1:
        raise CodexPluginMarketplaceError("plugin_id 必须是 plugin-name@marketplace-name")
    plugin_name, marketplace_name = value.split("@", 1)
    _validate_marketplace_name(marketplace_name)
    if not _PLUGIN_NAME_RE.fullmatch(plugin_name):
        raise CodexPluginMarketplaceError("plugin 名称包含非法字符")
    doc = _parse_config(path)
    _marketplace_item, marketplace_source = _marketplace_by_name(
        doc, marketplace_name, codex_home=path.parent
    )
    manifest = _load_manifest(marketplace_source)
    available = {
        plugin.get("name")
        for plugin in manifest.get("plugins", [])
        if isinstance(plugin, dict) and plugin.get("name")
    }
    plugins_table = doc.get("plugins")
    existing_row = plugins_table.get(value) if isinstance(plugins_table, dict) else None
    if plugin_name not in available and (enabled or not isinstance(existing_row, dict)):
        raise CodexPluginMarketplaceError(f"plugin 不存在: {plugin_name}")
    if not isinstance(plugins_table, dict):
        plugins_table = tomlkit.table()
        doc["plugins"] = plugins_table
    row = existing_row if isinstance(existing_row, dict) else tomlkit.table()
    if not isinstance(existing_row, dict):
        plugins_table[value] = row
    row["enabled"] = enabled
    _write_config(path, doc)
    return list_plugin_marketplaces(path)


def resolve_plugin_icon(
    config_path: str | Path, *, marketplace_name: str, plugin_name: str
) -> tuple[Path, str]:
    """解析插件的本地图标文件，返回 (路径, media_type)。"""
    value = plugin_name.strip()
    if not _PLUGIN_NAME_RE.fullmatch(value):
        raise CodexPluginMarketplaceError("plugin 名称包含非法字符")
    path = Path(config_path)
    doc = _parse_config(path)
    _marketplace_item, marketplace_source = _marketplace_by_name(
        doc, marketplace_name, codex_home=path.parent
    )
    root = marketplace_source
    manifest = _load_manifest(root)
    entry = next(
        (
            plugin
            for plugin in manifest.get("plugins", [])
            if isinstance(plugin, dict) and plugin.get("name") == value
        ),
        None,
    )
    if entry is None:
        raise CodexPluginMarketplaceError(f"plugin 不存在: {value}")
    plugin_dir = _plugin_dir_for(root, entry)
    if plugin_dir is None:
        raise CodexPluginMarketplaceError("插件目录不存在")
    meta = _read_plugin_metadata(plugin_dir)
    interface = meta.get("interface") if isinstance(meta, dict) else None
    interface = interface if isinstance(interface, dict) else {}
    icon_file = _resolve_icon_file(plugin_dir, interface)
    if icon_file is None:
        raise CodexPluginMarketplaceError("插件未提供本地图标")
    return icon_file, IMAGE_MEDIA_TYPES[icon_file.suffix.lower()]


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
