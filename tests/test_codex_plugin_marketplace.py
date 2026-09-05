from __future__ import annotations

import json
from pathlib import Path

import tomlkit

from codex_ai_gateway.integrations.codex_plugin_marketplace import (
    CodexPluginMarketplaceError,
    list_plugin_marketplaces,
    register_local_marketplace,
    remove_marketplace,
    resolve_plugin_icon,
    set_plugin_enabled,
)


def _make_marketplace(tmp_path: Path) -> Path:
    root = tmp_path / "marketplace"
    plugins = root / ".agents" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "team-curated",
                "plugins": [
                    {"name": "demo-plugin", "version": "0.1.0"},
                ],
            }
        ),
        encoding="utf-8",
    )
    return root


def _make_rich_marketplace(tmp_path: Path) -> Path:
    root = _make_marketplace(tmp_path)
    plugin_dir = root / "plugins" / "demo-plugin"
    manifest_dir = plugin_dir / ".codex-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "demo-plugin",
                "version": "0.1.0",
                "description": "顶层描述",
                "author": {"name": "Ada"},
                "homepage": "https://example.com",
                "keywords": ["demo", "test"],
                "interface": {
                    "displayName": "Demo Plugin",
                    "shortDescription": "一句话简介",
                    "longDescription": "很长的介绍。",
                    "developerName": "Ada",
                    "category": "Developer Tools",
                    "capabilities": ["MCP", "Read"],
                    "brandColor": "#226DB4",
                    "logo": "./assets/logo.png",
                },
            }
        ),
        encoding="utf-8",
    )
    assets = plugin_dir / "assets"
    assets.mkdir()
    (assets / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return root


def test_register_enable_and_list_marketplace(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    root = _make_marketplace(tmp_path)

    result = register_local_marketplace(
        config_path,
        name="team-curated",
        source=str(root),
        default_enabled=True,
    )

    assert result["marketplaces"][0]["name"] == "team-curated"
    assert result["marketplaces"][0]["manifest_valid"] is True
    assert result["marketplaces"][0]["plugin_count"] == 1
    assert result["plugins"][0]["plugin_id"] == "demo-plugin@team-curated"
    assert result["plugins"][0]["enabled"] is True

    text = config_path.read_text(encoding="utf-8")
    assert "[marketplaces.team-curated]" in text
    assert '[plugins."demo-plugin@team-curated"]' in text


def test_toggle_plugin_requires_registered_marketplace(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"

    try:
        set_plugin_enabled(
            config_path,
            plugin_id="demo-plugin@missing",
            enabled=True,
        )
    except CodexPluginMarketplaceError as exc:
        assert "marketplace 不存在" in str(exc)
    else:
        raise AssertionError("expected CodexPluginMarketplaceError")


def test_toggle_allows_official_openai_marketplace(tmp_path: Path) -> None:
    """Codex 官方内置市场（openai-*）不由网关注册，但其插件必须可以开关。"""
    config_path = tmp_path / "config.toml"
    root = _make_rich_marketplace(tmp_path)
    doc = tomlkit.document()
    market = tomlkit.table()
    market["source_type"] = "local"
    market["source"] = str(root)
    doc["marketplaces"] = tomlkit.table()
    doc["marketplaces"]["openai-bundled"] = market
    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    # 手工改写 manifest 名称以匹配官方市场名
    manifest_path = root / ".agents" / "plugins" / "marketplace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["name"] = "openai-bundled"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = set_plugin_enabled(
        config_path,
        plugin_id="demo-plugin@openai-bundled",
        enabled=True,
    )

    assert result["plugins"][0]["plugin_id"] == "demo-plugin@openai-bundled"
    assert result["plugins"][0]["enabled"] is True

    try:
        register_local_marketplace(
            config_path,
            name="openai-copy",
            source=str(root),
        )
    except CodexPluginMarketplaceError as exc:
        assert "保留名" in str(exc)
    else:
        raise AssertionError("expected CodexPluginMarketplaceError")


def test_remove_marketplace_removes_only_matching_plugins(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    root = _make_marketplace(tmp_path)
    register_local_marketplace(config_path, name="team-curated", source=str(root))

    doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    doc["plugins"] = tomlkit.table()
    doc["plugins"]["other@team-curated"] = {"enabled": True}
    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    result = remove_marketplace(config_path, name="team-curated")

    assert result["marketplaces"] == []
    assert result["plugins"] == []


def test_list_preserves_existing_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[model_providers.gateway]\nname = "gateway"\n',
        encoding="utf-8",
    )
    _make_marketplace(tmp_path)

    view = list_plugin_marketplaces(config_path)

    assert "model_providers" in tomlkit.parse(config_path.read_text(encoding="utf-8"))
    assert view["exists"] is True


def test_catalog_merges_manifest_and_config_state(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    root = _make_rich_marketplace(tmp_path)

    view = register_local_marketplace(config_path, name="team-curated", source=str(root))

    catalog = view["marketplaces"][0]["catalog"]
    assert len(catalog) == 1
    entry = catalog[0]
    assert entry["plugin_id"] == "demo-plugin@team-curated"
    assert entry["configured"] is False
    assert entry["enabled"] is False
    assert entry["stale"] is False
    assert entry["has_metadata"] is True
    assert entry["display_name"] == "Demo Plugin"
    assert entry["description"] == "一句话简介"
    assert entry["long_description"] == "很长的介绍。"
    assert entry["category"] == "Developer Tools"
    assert entry["version"] == "0.1.0"
    assert entry["author"] == "Ada"
    assert entry["keywords"] == ["demo", "test"]
    assert entry["capabilities"] == ["MCP", "Read"]
    assert entry["icon_url"] == (
        "/admin/codex/plugin-marketplaces/team-curated/plugins/demo-plugin/icon"
    )


def test_enable_unconfigured_plugin_from_catalog(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    root = _make_rich_marketplace(tmp_path)
    register_local_marketplace(config_path, name="team-curated", source=str(root))

    set_plugin_enabled(
        config_path,
        plugin_id="demo-plugin@team-curated",
        enabled=True,
    )

    view = list_plugin_marketplaces(config_path)
    entry = view["marketplaces"][0]["catalog"][0]
    assert entry["configured"] is True
    assert entry["enabled"] is True


def test_stale_plugin_shown_and_disable_only(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    root = _make_rich_marketplace(tmp_path)
    register_local_marketplace(config_path, name="team-curated", source=str(root))
    doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    doc["plugins"] = tomlkit.table()
    doc["plugins"]["ghost-plugin@team-curated"] = {"enabled": True}
    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    view = list_plugin_marketplaces(config_path)
    catalog = {entry["name"]: entry for entry in view["marketplaces"][0]["catalog"]}
    assert catalog["ghost-plugin"]["stale"] is True
    assert catalog["ghost-plugin"]["enabled"] is True
    assert catalog["ghost-plugin"]["has_metadata"] is False

    set_plugin_enabled(
        config_path,
        plugin_id="ghost-plugin@team-curated",
        enabled=False,
    )
    view = list_plugin_marketplaces(config_path)
    ghost = {
        entry["name"]: entry for entry in view["marketplaces"][0]["catalog"]
    }["ghost-plugin"]
    assert ghost["enabled"] is False

    try:
        set_plugin_enabled(
            config_path,
            plugin_id="ghost-plugin@team-curated",
            enabled=True,
        )
    except CodexPluginMarketplaceError as exc:
        assert "plugin 不存在" in str(exc)
    else:
        raise AssertionError("expected CodexPluginMarketplaceError")


def test_resolve_plugin_icon_returns_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    root = _make_rich_marketplace(tmp_path)
    register_local_marketplace(config_path, name="team-curated", source=str(root))

    icon_path, media_type = resolve_plugin_icon(
        config_path,
        marketplace_name="team-curated",
        plugin_name="demo-plugin",
    )

    assert icon_path.name == "logo.png"
    assert media_type == "image/png"


def test_resolve_plugin_icon_rejects_escape_and_missing(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    root = _make_marketplace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    manifest_path = root / ".agents" / "plugins" / "marketplace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["plugins"][0]["source"] = {"source": "local", "path": "../../outside"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    register_local_marketplace(config_path, name="team-curated", source=str(root))

    try:
        resolve_plugin_icon(
            config_path,
            marketplace_name="team-curated",
            plugin_name="demo-plugin",
        )
    except CodexPluginMarketplaceError as exc:
        assert "图标" in str(exc) or "不存在" in str(exc)
    else:
        raise AssertionError("expected CodexPluginMarketplaceError")


def test_git_marketplace_resolves_from_codex_clone(tmp_path: Path) -> None:
    import shutil

    codex_home = tmp_path / ".codex"
    config_path = codex_home / "config.toml"
    root = _make_rich_marketplace(tmp_path)
    clone = codex_home / ".tmp" / "marketplaces" / "ars"
    shutil.copytree(root, clone)
    manifest_path = clone / ".agents" / "plugins" / "marketplace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["name"] = "ars"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    doc = tomlkit.document()
    market = tomlkit.table()
    market["source_type"] = "git"
    market["source"] = "https://github.com/example/ars.git"
    doc["marketplaces"] = tomlkit.table()
    doc["marketplaces"]["ars"] = market
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    view = list_plugin_marketplaces(config_path)
    market_view = view["marketplaces"][0]
    assert market_view["manifest_valid"] is True
    assert market_view["resolved_source"] == str(clone)
    assert market_view["catalog"][0]["display_name"] == "Demo Plugin"
    assert market_view["catalog"][0]["icon_url"] is not None

    set_plugin_enabled(config_path, plugin_id="demo-plugin@ars", enabled=True)
    view = list_plugin_marketplaces(config_path)
    assert view["marketplaces"][0]["catalog"][0]["configured"] is True
    assert view["marketplaces"][0]["catalog"][0]["enabled"] is True

    icon_path, media_type = resolve_plugin_icon(
        config_path, marketplace_name="ars", plugin_name="demo-plugin"
    )
    assert icon_path.name == "logo.png"
    assert media_type == "image/png"


def test_git_marketplace_without_clone_reports_unresolved(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    doc = tomlkit.document()
    market = tomlkit.table()
    market["source_type"] = "git"
    market["source"] = "https://github.com/example/y.git"
    doc["marketplaces"] = tomlkit.table()
    doc["marketplaces"]["y"] = market
    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    view = list_plugin_marketplaces(config_path)

    market_view = view["marketplaces"][0]
    assert market_view["manifest_valid"] is False
    assert market_view["resolved_source"] is None
    assert market_view["catalog"] == []
