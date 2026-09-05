from __future__ import annotations

import json
from pathlib import Path

import tomlkit

from codex_ai_gateway.integrations.codex_plugin_marketplace import (
    CodexPluginMarketplaceError,
    list_plugin_marketplaces,
    register_local_marketplace,
    remove_marketplace,
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
