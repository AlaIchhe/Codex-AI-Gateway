from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_ai_gateway.integrations.codex_context import (
    CodexContextError,
    create_skill,
    delete_mcp_server,
    delete_skill,
    list_mcp_servers,
    list_skills,
    upsert_mcp_server,
)


def test_mcp_upsert_list_delete_roundtrip(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[model_providers.gateway]\nname = "gateway"\n',
        encoding="utf-8",
    )

    result = upsert_mcp_server(
        config_path,
        name="playwright",
        command="npx",
        args=["-y", "@playwright/mcp@latest"],
    )
    assert result["servers"][0]["name"] == "playwright"
    assert result["servers"][0]["transport"] == "stdio"
    assert result["servers"][0]["args"] == ["-y", "@playwright/mcp@latest"]

    upsert_mcp_server(config_path, name="firecrawl", url="https://mcp.firecrawl.dev/v2/mcp")

    result = upsert_mcp_server(
        config_path,
        name="firecrawl",
        url="https://mcp.firecrawl.dev/v3/mcp",
        env={"FIRECRAWL_KEY": "sk-1"},
    )
    rows = {row["name"]: row for row in result["servers"]}
    assert rows["firecrawl"]["url"] == "https://mcp.firecrawl.dev/v3/mcp"
    assert rows["firecrawl"]["env_keys"] == ["FIRECRAWL_KEY"]

    text = config_path.read_text(encoding="utf-8")
    assert "[mcp_servers.playwright]" in text
    assert "model_providers.gateway" in text

    result = delete_mcp_server(config_path, name="playwright")
    assert [row["name"] for row in result["servers"]] == ["firecrawl"]

    delete_mcp_server(config_path, name="firecrawl")
    assert "mcp_servers" not in config_path.read_text(encoding="utf-8")


def test_mcp_upsert_validation(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"

    with pytest.raises(CodexContextError, match="至少提供一个"):
        upsert_mcp_server(config_path, name="blank")
    with pytest.raises(CodexContextError, match="只能提供一个"):
        upsert_mcp_server(config_path, name="both", command="npx", url="https://x")
    with pytest.raises(CodexContextError, match="不支持 args"):
        upsert_mcp_server(config_path, name="u", url="https://x", args=["a"])
    with pytest.raises(CodexContextError, match="名称"):
        upsert_mcp_server(config_path, name="bad name", command="npx")


def test_mcp_delete_missing(tmp_path: Path) -> None:
    with pytest.raises(CodexContextError, match="不存在"):
        delete_mcp_server(tmp_path / "config.toml", name="ghost")


def test_plugin_bundled_mcp_listed_for_enabled_plugins(tmp_path: Path) -> None:
    from codex_ai_gateway.integrations.codex_plugin_marketplace import (
        register_local_marketplace,
    )

    config_path = tmp_path / "config.toml"
    root = tmp_path / "marketplace"
    plugins = root / ".agents" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "marketplace.json").write_text(
        json.dumps({"name": "team-curated", "plugins": [{"name": "demo-plugin"}]}),
        encoding="utf-8",
    )
    plugin_dir = root / "plugins" / "demo-plugin"
    manifest_dir = plugin_dir / ".codex-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps({"name": "demo-plugin", "mcpServers": "./.mcp.json"}),
        encoding="utf-8",
    )
    (plugin_dir / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"arxiv": {"type": "stdio", "command": "uvx", "args": ["arxiv-mcp-server"]}}}
        ),
        encoding="utf-8",
    )
    register_local_marketplace(
        config_path, name="team-curated", source=str(root), default_enabled=True
    )

    view = list_mcp_servers(config_path)

    assert view["servers"] == []
    assert len(view["plugin_servers"]) == 1
    bundled = view["plugin_servers"][0]
    assert bundled["plugin_id"] == "demo-plugin@team-curated"
    assert bundled["servers"][0]["name"] == "arxiv"
    assert bundled["servers"][0]["command"] == "uvx"


def test_list_skills_parses_frontmatter(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    demo = skills_dir / "demo-skill"
    demo.mkdir(parents=True)
    (demo / "SKILL.md").write_text(
        "---\nname: Demo Skill\ndescription: 做演示用的技能。\n---\n\n# body\n",
        encoding="utf-8",
    )
    linked = skills_dir / "linked-skill"
    linked.mkdir()
    (linked / "SKILL.md").write_text("no frontmatter\n", encoding="utf-8")
    (skills_dir / "not-a-skill").mkdir()

    view = list_skills(tmp_path)

    assert view["exists"] is True
    assert [skill["id"] for skill in view["skills"]] == ["demo-skill", "linked-skill"]
    assert view["skills"][0]["name"] == "Demo Skill"
    assert view["skills"][0]["description"] == "做演示用的技能。"
    assert view["skills"][0]["is_symlink"] is False
    assert view["skills"][1]["description"] is None


def test_list_skills_missing_dir(tmp_path: Path) -> None:
    view = list_skills(tmp_path)
    assert view["exists"] is False
    assert view["skills"] == []


def test_skill_create_and_delete_roundtrip(tmp_path: Path) -> None:
    view = create_skill(
        tmp_path, skill_id="demo-skill", name="Demo", description="演示技能"
    )
    assert [skill["id"] for skill in view["skills"]] == ["demo-skill"]
    assert view["skills"][0]["name"] == "Demo"
    assert view["skills"][0]["description"] == "演示技能"

    skill_md = tmp_path / "skills" / "demo-skill" / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    assert text.startswith("---\nname: Demo\ndescription: 演示技能\n---\n")

    with pytest.raises(CodexContextError, match="已存在"):
        create_skill(tmp_path, skill_id="demo-skill")

    view = delete_skill(tmp_path, skill_id="demo-skill")
    assert view["skills"] == []

    with pytest.raises(CodexContextError, match="不存在"):
        delete_skill(tmp_path, skill_id="demo-skill")


def test_skill_delete_rejects_non_skill_dir(tmp_path: Path) -> None:
    plain = tmp_path / "skills" / "plain"
    plain.mkdir(parents=True)

    with pytest.raises(CodexContextError, match="缺少 SKILL.md"):
        delete_skill(tmp_path, skill_id="plain")

    with pytest.raises(CodexContextError, match="技能 ID"):
        create_skill(tmp_path, skill_id="bad id")
