""""预设 Provider 服务：读取随版本发布的内置预设目录。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_PRESETS_JSON = Path(__file__).resolve().parents[1] / "presets" / "presets.json"


class PresetSource(BaseModel):
    kind: str = "official_doc"
    url: str
    extractor_key: str
    extractor_version: str
    fetched_at: str | None = None


class PresetProvider(BaseModel):
    preset_id: str
    name: str
    icon: str = "box"
    base_url: str
    default_headers: dict[str, str] = Field(default_factory=dict)
    doc_url: str
    model_source: str = "official_doc"
    extractor_key: str
    extractor_version: str
    identity_aliases: dict[str, str] = Field(default_factory=dict)
    source: PresetSource = Field(default_factory=dict)


class PresetDiscoverySummary(BaseModel):
    status: str = "never"
    current_model_count: int = 0
    latest_snapshot_id: str | None = None
    latest_failure_id: str | None = None
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None


def preset_discovery_view(state: Any, *, preset_id: str, upstream_id: str) -> dict[str, Any]:
    discovery = next(
        (
            item
            for item in state.preset_discovery_states
            if item.preset_id == preset_id and item.upstream_id == upstream_id
        ),
        None,
    )
    if discovery is None:
        return {
            "status": "never",
            "current_model_count": 0,
            "latest_snapshot_id": None,
            "latest_failure_id": None,
            "last_attempt_at": None,
            "last_success_at": None,
            "last_failure_at": None,
        }
    return discovery.model_dump(mode="json", exclude={"id", "preset_id", "upstream_id"})


class PresetCatalog(BaseModel):
    version: str
    presets: list[PresetProvider]

    def by_id(self, preset_id: str) -> PresetProvider | None:
        return next((p for p in self.presets if p.preset_id == preset_id), None)


def _load_catalog_bytes() -> bytes:
    return _PRESETS_JSON.read_bytes()


def load_preset_catalog(*, raw: bytes | None = None) -> PresetCatalog:
    if raw is None:
        raw = _load_catalog_bytes()
    return PresetCatalog.model_validate(json.loads(raw))


def get_catalog_version() -> str:
    return load_preset_catalog().version


def get_preset_provider(preset_id: str, *, raw: bytes | None = None) -> PresetProvider:
    catalog = load_preset_catalog(raw=raw)
    preset = catalog.by_id(preset_id)
    if preset is None:
        raise KeyError(preset_id)
    return preset


def preset_view(preset: PresetProvider, state: Any | None = None) -> dict[str, Any]:
    data = {
        "preset_id": preset.preset_id,
        "name": preset.name,
        "icon": preset.icon,
        "base_url": preset.base_url,
        "doc_url": preset.doc_url,
        "model_source": preset.model_source,
        "extractor_key": preset.extractor_key,
        "extractor_version": preset.extractor_version,
        "model_count": None,
        "current_model_count": 0,
        "discovery_status": "never",
        "source": preset.source.model_dump(mode="json"),
    }
    if state is None:
        return data
    discoveries = [
        item
        for item in state.preset_discovery_states
        if item.preset_id == preset.preset_id
    ]
    latest = max(
        discoveries,
        key=lambda item: item.last_attempt_at or "",
        default=None,
    )
    if latest is not None:
        data.update(
            {
                "current_model_count": latest.current_model_count,
                "discovery_status": latest.status,
            }
        )
    return data
