"""目录发布：滚动别名归并、幽灵条目过滤与上游兜底发布。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from codex_ai_gateway.models.entities import (
    CatalogCandidate,
    MappingStatus,
    Offering,
    OfferingStatus,
    PublishedCatalogEntry,
    SelectionResult,
    SourceKind,
    Upstream,
    UpstreamKind,
    UpstreamStatus,
    VerificationStatus,
    WireProtocol,
)
from codex_ai_gateway.services.catalog_publishing import (
    _build_model_info,
    _metadata_from_upstream,
    _official_slug,
    _provider_family_slug,
    evaluate_fields,
    load_published_model_infos,
    routable_slug_key,
)
from codex_ai_gateway.util import utc_now, uuid7


def _offering(model_id: str = "deepseek-v4.1-flash") -> Offering:
    now = utc_now()
    return Offering(
        id=uuid7(),
        upstream_id="up-1",
        provider_model_id=model_id,
        wire_protocol=WireProtocol.chat_completions,
        display_name=model_id,
        status=OfferingStatus.approved,
        discovered_at=now,
        updated_at=now,
    )


def _candidate(**overrides: object) -> CatalogCandidate:
    now = utc_now()
    data: dict[str, object] = {
        "id": uuid7(),
        "offering_id": "off-1",
        "upstream_id": "up-1",
        "proposed_alias_slug": "deepseek-v4.1-flash",
        "openrouter_model_id": None,
        "mapping_status": MappingStatus.missing,
        "selection_result": SelectionResult.rejected,
        "created_at": now,
        "updated_at": now,
    }
    data.update(overrides)
    return CatalogCandidate(**data)


def _publication(
    tmp_path: Path,
    *,
    slug: str,
    offering_id: str,
    accepted_at: str,
    model_id: str,
) -> None:
    entry = PublishedCatalogEntry(
        id=uuid7(),
        offering_id=offering_id,
        revision=1,
        version_hash="hash",
        model_info_json={
            "slug": slug,
            "name": slug,
            "model_id": model_id,
            "context_window": 1000000,
            "reasoning_levels": ["low", "medium", "high"],
            "reasoning_effort": "medium",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tools": True,
            "tool_choice": True,
            "structured_output": False,
        },
        accepted_at=accepted_at,
    )
    publications = tmp_path / "catalog/publications"
    publications.mkdir(parents=True, exist_ok=True)
    (publications / f"{entry.id}.json").write_text(
        entry.model_dump_json(), encoding="utf-8"
    )


def test_official_slug_merges_latest_alias() -> None:
    assert (
        _official_slug({"canonical_slug": "~deepseek/deepseek-v4-flash-latest"})
        == "deepseek-v4-flash"
    )
    assert (
        _official_slug({"canonical_slug": "deepseek/deepseek-v4-flash-20260731"})
        == "deepseek-v4-flash"
    )
    assert (
        _official_slug({"canonical_slug": "deepseek/deepseek-v4-pro-20260813"})
        == "deepseek-v4-pro"
    )
    assert routable_slug_key("deepseek-v4-flash-latest") == "deepseek-v4-flash"
    assert routable_slug_key("~deepseek-v4-flash-latest") == "deepseek-v4-flash"
    # 4 位 MMDD 版本后缀也要归并到 canonical slug，避免有效模型被过滤。
    assert routable_slug_key("deepseek-chat-v3-0324") == "deepseek-chat-v3"


def test_provider_family_slug_keeps_dotted_version() -> None:
    assert _provider_family_slug(_offering("deepseek-v4.1-flash"), None) == (
        "deepseek-v4.1-flash"
    )


def test_metadata_from_upstream_defaults_text_modalities() -> None:
    metadata = _metadata_from_upstream(
        {"id": "deepseek-v4.1-flash", "context_length": 1000000},
        {"modalities": [], "tools": []},
    )
    assert metadata["context_window"] == 1000000
    assert metadata["input_modality"] == ["text"]
    assert metadata["output_modality"] == ["text"]


def test_fallback_publication_uses_provider_model_id() -> None:
    candidate = _candidate()
    metadata = {
        "context_window": 1000000,
        "input_modality": ["text"],
        "output_modality": ["text"],
        "supported_parameters": ["tools", "tool_choice"],
        "reasoning": {
            "supported_efforts": ["low", "medium", "high"],
            "default_effort": "medium",
        },
    }
    fields = evaluate_fields(candidate, metadata)
    required = [field for field in fields if field.field_path != "structured_output"]
    assert all(
        field.verification_status == VerificationStatus.complete for field in required
    )
    assert {field.source_kind for field in fields} == {SourceKind.upstream_native}
    info = _build_model_info(
        candidate, fields, provider_model_id="deepseek-v4.1-flash"
    )
    assert info["slug"] == "deepseek-v4.1-flash"
    assert info["model_id"] == "deepseek-v4.1-flash"
    assert info["reasoning_effort"] == "medium"


def test_openrouter_candidate_evidence_source_stays_openrouter() -> None:
    candidate = _candidate(openrouter_model_id="deepseek/deepseek-v4-flash")
    fields = evaluate_fields(
        candidate,
        {
            "context_window": 1000,
            "input_modality": ["text"],
            "output_modality": ["text"],
        },
    )
    assert {field.source_kind for field in fields} == {SourceKind.openrouter}


def test_load_published_filters_ghosts_and_merges_latest_alias(
    tmp_path: Path,
) -> None:
    _publication(
        tmp_path,
        slug="deepseek-v4-flash",
        offering_id="off-real",
        accepted_at="2026-09-01T00:00:00+00:00",
        model_id="deepseek/deepseek-v4-flash",
    )
    _publication(
        tmp_path,
        slug="deepseek-v4-flash-latest",
        offering_id="off-ghost",
        accepted_at="2026-09-08T00:00:00+00:00",
        model_id="~deepseek/deepseek-v4-flash-latest",
    )
    _publication(
        tmp_path,
        slug="removed-model",
        offering_id="off-removed",
        accepted_at="2026-09-02T00:00:00+00:00",
        model_id="vendor/removed-model",
    )

    infos = load_published_model_infos(tmp_path, valid_slugs={"deepseek-v4-flash"})
    assert [item["slug"] for item in infos] == ["deepseek-v4-flash"]
    assert infos[0]["model_id"] == "deepseek/deepseek-v4-flash"
    assert load_published_model_infos(tmp_path, valid_slugs=set()) == []


def test_load_published_without_filter_merges_alias_only(tmp_path: Path) -> None:
    _publication(
        tmp_path,
        slug="deepseek-v4-flash-latest",
        offering_id="off-ghost",
        accepted_at="2026-09-08T00:00:00+00:00",
        model_id="~deepseek/deepseek-v4-flash-latest",
    )
    infos = load_published_model_infos(tmp_path)
    assert len(infos) == 1
    assert infos[0]["slug"] == "deepseek-v4-flash"
    assert infos[0]["name"] == "deepseek-v4-flash"


class _FakeStore:
    def __init__(self, state: Any) -> None:
        self._state = state

    def read_state(self) -> Any:
        return self._state

    def mutate(self, fn: Any, **_kwargs: Any) -> None:
        fn(self._state)


class _FakeSecrets:
    def get_secret(self, _ref: str) -> str:
        return "sk-test"


class _FakeRuntime:
    def __init__(self, data_dir: Path, state: Any) -> None:
        self.data_dir = data_dir
        self.state_store = _FakeStore(state)
        self.secret_store = _FakeSecrets()


class _FakeState:
    def __init__(self, offerings: list[Offering], upstreams: list[Upstream]) -> None:
        self.offerings = offerings
        self.upstreams = upstreams
        self.catalog_candidates: list[CatalogCandidate] = []
        self.catalog_evidence: list[Any] = []
        self.publications: list[PublishedCatalogEntry] = []
        self.openrouter_snapshots: list[Any] = []


def _upstream() -> Upstream:
    return Upstream(
        id="up-1",
        name="tokendance",
        status=UpstreamStatus.enabled,
        kind=UpstreamKind.custom,
        base_url="https://tokendance.space/gateway/v1",
        auth_credential_ref="tokendance",
        created_at="2026-09-01T00:00:00+00:00",
        updated_at="2026-09-01T00:00:00+00:00",
    )


def test_run_catalog_automation_publishes_upstream_fallback(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import codex_ai_gateway.services.catalog_publishing as cp

    calls = {"probe": 0}

    async def fake_search_models(**_kwargs: Any) -> list[Any]:
        return []

    async def fake_probe(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        calls["probe"] += 1
        return {
            "supported_parameters": ["tools", "tool_choice"],
            "reasoning": {
                "supported_efforts": ["low", "medium", "high"],
                "default_effort": "medium",
            },
        }

    monkeypatch.setattr(cp, "search_models", fake_search_models)
    monkeypatch.setattr(cp, "probe_model_capabilities", fake_probe)

    offering = _offering("deepseek-v4.1-flash")
    offering = offering.model_copy(
        update={"native_metadata_json": {"id": "deepseek-v4.1-flash", "context_length": 1000000}}
    )
    state = _FakeState([offering], [_upstream()])
    runtime = _FakeRuntime(tmp_path, state)

    first = asyncio.run(cp.run_catalog_automation(runtime))
    assert first["accepted"] == 1
    assert len(state.publications) == 1
    info = state.publications[0].model_info_json
    assert info["slug"] == "deepseek-v4.1-flash"
    assert info["model_id"] == "deepseek-v4.1-flash"
    assert calls["probe"] == 1

    second = asyncio.run(cp.run_catalog_automation(runtime))
    assert second["accepted"] == 1
    assert calls["probe"] == 1, "能力探测结果应在 TTL 内复用"

    infos = load_published_model_infos(tmp_path, valid_slugs={"deepseek-v4.1-flash"})
    assert [item["slug"] for item in infos] == ["deepseek-v4.1-flash"]