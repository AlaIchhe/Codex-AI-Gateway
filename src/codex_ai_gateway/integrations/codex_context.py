"""Codex 本机上下文扩展诊断：MCP 服务器管理与技能只读目录。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import tomlkit

from codex_ai_gateway.persistence.atomic_writer import atomic_write_bytes

_SKILL_FRONTMATTER_KEYS = ("name", "description")


class CodexContextError(RuntimeError):
    """MCP 服务器配置错误。"""


def _parse_config(path: Path) -> tomlkit.TOMLDocument:
    if not path.exists():
        return tomlkit.document()
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, tomlkit.exceptions.ParseError) as exc:
        raise CodexContextError(f"config.toml 读取失败: {exc}") from exc


def _write_config(path: Path, doc: tomlkit.TOMLDocument) -> None:
    text = tomlkit.dumps(doc)
    if not text.endswith("\n"):
        text += "\n"
    atomic_write_bytes(path, text.encode("utf-8"))


def _validate_mcp_name(name: str) -> str:
    value = name.strip()
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", value):
        raise CodexContextError("MCP 名称只能包含字母、数字、点、连字符和下划线")
    return value


def _mcp_transport(item: dict[str, Any]) -> str:
    return "url" if item.get("url") else "stdio"


def _mcp_row(name: str, item: dict[str, Any]) -> dict[str, Any]:
    env = item.get("env") if isinstance(item.get("env"), dict) else {}
    args = item.get("args")
    return {
        "name": str(name),
        "transport": _mcp_transport(item),
        "command": item.get("command") if isinstance(item.get("command"), str) else None,
        "args": [str(arg) for arg in args] if isinstance(args, list) else [],
        "url": item.get("url") if isinstance(item.get("url"), str) else None,
        "env_keys": sorted(str(key) for key in env),
    }


def _config_mcp_rows(doc: tomlkit.TOMLDocument) -> list[dict[str, Any]]:
    table = doc.get("mcp_servers")
    if not isinstance(table, dict):
        return []
    return [
        _mcp_row(str(name), item)
        for name, item in table.items()
        if isinstance(item, dict)
    ]


def _load_plugin_mcp_servers(marketplace_source: Path, entry: dict[str, Any]) -> list[dict[str, Any]]:
    """读取插件目录中 mcpServers 字段指向的 .mcp.json。"""
    from codex_ai_gateway.integrations.codex_plugin_marketplace import (
        _plugin_dir_for,
        _read_plugin_metadata,
    )

    plugin_dir = _plugin_dir_for(marketplace_source, entry)
    if plugin_dir is None:
        return []
    meta = _read_plugin_metadata(plugin_dir)
    if not meta:
        return []
    raw = meta.get("mcpServers")
    if not isinstance(raw, str) or not raw.strip():
        return []
    mcp_path = (plugin_dir / raw.strip()).resolve()
    if not mcp_path.is_relative_to(plugin_dir.resolve()) or not mcp_path.is_file():
        return []
    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return []
    return [
        {**_mcp_row(str(name), item), "name": f"{name}"}
        for name, item in servers.items()
        if isinstance(item, dict)
    ]


def list_mcp_servers(config_path: str | Path) -> dict[str, Any]:
    """config.toml 的 [mcp_servers] 与已启用插件捆绑的 MCP 一并输出。"""
    path = Path(config_path)
    doc = _parse_config(path)

    from codex_ai_gateway.integrations.codex_plugin_marketplace import (
        CodexPluginMarketplaceError,
        _configured_marketplaces,
        _load_manifest,
    )

    plugin_servers: list[dict[str, Any]] = []
    for marketplace in _configured_marketplaces(doc, path):
        if not (marketplace["manifest_valid"] and marketplace["resolved_source"]):
            continue
        try:
            manifest = _load_manifest(Path(marketplace["resolved_source"]))
        except CodexPluginMarketplaceError:
            continue
        marketplace_name = str(manifest["name"])
        root = Path(str(marketplace["resolved_source"]))
        for entry in manifest.get("plugins", []):
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            plugin_id = f"{entry['name']}@{marketplace_name}"
            row = doc.get("plugins")
            enabled = (
                isinstance(row, dict)
                and isinstance(row.get(plugin_id), dict)
                and bool(row[plugin_id].get("enabled"))
            )
            if not enabled:
                continue
            servers = _load_plugin_mcp_servers(root, entry)
            if servers:
                plugin_servers.append(
                    {"plugin_id": plugin_id, "plugin_enabled": True, "servers": servers}
                )
    return {
        "config_path": str(path),
        "exists": path.exists(),
        "servers": _config_mcp_rows(doc),
        "plugin_servers": plugin_servers,
    }


def upsert_mcp_server(
    config_path: str | Path,
    *,
    name: str,
    command: str | None = None,
    args: list[str] | None = None,
    url: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """新增或更新一个 [mcp_servers.<name>] 条目。"""
    path = Path(config_path)
    value = _validate_mcp_name(name)
    clean_url = (url or "").strip() or None
    clean_command = (command or "").strip() or None
    if not clean_url and not clean_command:
        raise CodexContextError("url 与 command 至少提供一个")
    if clean_url and clean_command:
        raise CodexContextError("url 与 command 只能提供一个")
    clean_args = [str(arg) for arg in (args or [])]
    if clean_url and clean_args:
        raise CodexContextError("url 型 MCP 不支持 args")
    doc = _parse_config(path)
    table = doc.get("mcp_servers")
    if not isinstance(table, dict):
        table = tomlkit.table()
        doc["mcp_servers"] = table
    row = table.get(value)
    if not isinstance(row, dict):
        row = tomlkit.table()
        table[value] = row
    for key in ("command", "args", "url", "env"):
        if key in row:
            del row[key]
    if clean_url:
        row["url"] = clean_url
    else:
        row["command"] = clean_command
        if clean_args:
            row["args"] = clean_args
    if env:
        row["env"] = {str(key): str(item) for key, item in env.items()}
    _write_config(path, doc)
    return list_mcp_servers(path)


def delete_mcp_server(config_path: str | Path, *, name: str) -> dict[str, Any]:
    """删除一个 [mcp_servers.<name>] 条目。"""
    path = Path(config_path)
    value = _validate_mcp_name(name)
    doc = _parse_config(path)
    table = doc.get("mcp_servers")
    if not isinstance(table, dict) or value not in table:
        raise CodexContextError(f"mcp server 不存在: {value}")
    del table[value]
    if not table:
        del doc["mcp_servers"]
    _write_config(path, doc)
    return list_mcp_servers(path)


def _parse_skill_frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in _SKILL_FRONTMATTER_KEYS and key not in meta:
            meta[key] = value
    return meta


def list_skills(codex_home: str | Path) -> dict[str, Any]:
    """扫描 $CODEX_HOME/skills 下的技能目录，解析 SKILL.md frontmatter。"""
    home = Path(codex_home)
    skills_dir = home / "skills"
    skills: list[dict[str, Any]] = []
    if skills_dir.is_dir():
        for entry in sorted(skills_dir.iterdir()):
            skill_md = entry / "SKILL.md"
            if not entry.is_dir() or not skill_md.is_file():
                continue
            try:
                meta = _parse_skill_frontmatter(
                    skill_md.read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                meta = {}
            skills.append(
                {
                    "id": entry.name,
                    "name": meta.get("name") or entry.name,
                    "description": meta.get("description"),
                    "is_symlink": entry.is_symlink(),
                    "path": str(entry),
                }
            )
    return {
        "codex_home": str(home),
        "skills_dir": str(skills_dir),
        "exists": skills_dir.is_dir(),
        "skills": skills,
    }


def _validate_skill_id(skill_id: str) -> str:
    value = skill_id.strip()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", value):
        raise CodexContextError("技能 ID 只能包含字母、数字、点、连字符和下划线")
    return value


def create_skill(
    codex_home: str | Path,
    *,
    skill_id: str,
    name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """新建技能目录与 SKILL.md（frontmatter 最小骨架）。"""
    value = _validate_skill_id(skill_id)
    skills_dir = Path(codex_home) / "skills"
    skill_dir = skills_dir / value
    if skill_dir.exists() or skill_dir.is_symlink():
        raise CodexContextError(f"技能已存在: {value}")
    clean_name = " ".join((name or "").split()) or value
    clean_description = " ".join((description or "").split())
    frontmatter = f"---\nname: {clean_name}\n"
    if clean_description:
        frontmatter += f"description: {clean_description}\n"
    frontmatter += "---\n"
    try:
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"{frontmatter}\n# {clean_name}\n", encoding="utf-8"
        )
    except OSError as exc:
        raise CodexContextError(f"技能目录创建失败: {exc}") from exc
    return list_skills(codex_home)


def delete_skill(codex_home: str | Path, *, skill_id: str) -> dict[str, Any]:
    """删除技能目录；符号链接只摘链不删目标。"""
    import shutil

    value = _validate_skill_id(skill_id)
    skills_dir = (Path(codex_home) / "skills").resolve()
    skill_dir = skills_dir / value
    if not skill_dir.exists() and not skill_dir.is_symlink():
        raise CodexContextError(f"技能不存在: {value}")
    try:
        if skill_dir.is_symlink() or skill_dir.is_junction():
            skill_dir.unlink()
        else:
            if not (skill_dir / "SKILL.md").is_file():
                raise CodexContextError(f"目录不是技能（缺少 SKILL.md）: {value}")
            shutil.rmtree(skill_dir)
    except OSError as exc:
        raise CodexContextError(f"技能删除失败: {exc}") from exc
    return list_skills(codex_home)
