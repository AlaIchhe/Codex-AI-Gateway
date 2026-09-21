"""目录发布：滚动别名归并、幽灵条目过滤与上游兜底发布。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from codex_ai_gateway.models.entities import (
    CatalogCandidate,
    CatalogEvidenceSet,
    CatalogFieldEvidence,
    CatalogRevision,
    CatalogRevisionStatus,
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
    _fallback_metadata,
    _metadata_from_upstream,
    _official_slug,
    _provider_family_slug,
    compact_catalog_history,
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
) -> PublishedCatalogEntry:
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
    return entry


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


def test_metadata_from_upstream_reads_declared_capabilities() -> None:
    """上游把能力写在 capabilities 子对象里时同样要读出来（不发任何请求）。"""
    metadata = _metadata_from_upstream(
        {
            "id": "vendor/model",
            "context_length": 200000,
            "capabilities": {
                "modalities": ["text", "image"],
                "tools": ["function"],
                "tool_choice": True,
                "reasoning": {"supported_efforts": ["low", "high"]},
            },
        }
    )

    assert metadata["context_window"] == 200000
    assert metadata["input_modality"] == ["text", "image"]
    assert metadata["supported_parameters"] == ["tool_choice", "tools"]
    assert metadata["reasoning"] == {"supported_efforts": ["low", "high"]}


def test_fallback_metadata_merges_declaration_and_existing_evidence() -> None:
    """兜底元数据 = 上游声明 + 既有证据；两路都不需要网络请求。"""
    offering = _offering("vendor/model").model_copy(
        update={
            "native_metadata_json": {
                "id": "vendor/model",
                "context_length": 200000,
                "capabilities": {"tools": ["function"], "tool_choice": True},
            }
        }
    )

    metadata = _fallback_metadata(offering, None)
    assert metadata["context_window"] == 200000
    assert metadata["supported_parameters"] == ["tool_choice", "tools"]
    assert "reasoning" not in metadata

    evidence = CatalogEvidenceSet(
        candidate_id="cand-1",
        fields=[
            CatalogFieldEvidence(
                candidate_id="cand-1",
                id=uuid7(),
                field_path="reasoning_levels",
                source_kind=SourceKind.upstream_native,
                observed_value={"supported_efforts": ["low", "medium"]},
                verification_status=VerificationStatus.complete,
                observed_at=utc_now(),
            )
        ],
    )

    merged = _fallback_metadata(offering, evidence)
    assert merged["reasoning"] == {"supported_efforts": ["low", "medium"]}
    assert merged["supported_parameters"] == ["tool_choice", "tools"]



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
        self.mutate_calls = 0

    def read_state(self) -> Any:
        return self._state

    def mutate(self, fn: Any, **_kwargs: Any) -> None:
        self.mutate_calls += 1
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
        self.model_mappings: list[Any] = []


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


def _forbid_inference_requests(monkeypatch: Any) -> None:
    """把 httpx 客户端换成「一创建就炸」的哨兵。

    能力探测已被整体删除：目录维护只读上游 /models 声明与 OpenRouter 目录，
    任何在这里创建 HTTP 客户端的行为都是回归（旧实现会真打上游并计费）。
    """
    import codex_ai_gateway.services.catalog_publishing as cp

    class _ForbiddenClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("目录维护不得发起 HTTP 请求（能力探测已删除）")

    monkeypatch.setattr(cp.httpx, "AsyncClient", _ForbiddenClient)


def test_run_catalog_automation_publishes_upstream_fallback(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """上游 /models 声明了能力时，不借助任何推理请求即可发布。"""
    import codex_ai_gateway.services.catalog_publishing as cp

    async def fake_search_models(**_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(cp, "search_models", fake_search_models)
    _forbid_inference_requests(monkeypatch)

    offering = _offering("deepseek-v4.1-flash").model_copy(
        update={
            "native_metadata_json": {
                "id": "deepseek-v4.1-flash",
                "context_length": 1000000,
                "supported_parameters": ["tools", "tool_choice"],
                "reasoning": {"supported_efforts": ["low", "medium", "high"]},
            }
        }
    )
    state = _FakeState([offering], [_upstream()])
    runtime = _FakeRuntime(tmp_path, state)

    first = asyncio.run(cp.run_catalog_automation(runtime))
    assert first["accepted"] == 1
    assert len(state.publications) == 1
    info = state.publications[0].model_info_json
    assert info["slug"] == "deepseek-v4.1-flash"
    assert info["model_id"] == "deepseek-v4.1-flash"

    second = asyncio.run(cp.run_catalog_automation(runtime))
    assert second["accepted"] == 1
    assert len(state.publications) == 1, "内容未变化的候选不该重复发布"

    infos = load_published_model_infos(tmp_path, valid_slugs={"deepseek-v4.1-flash"})
    assert [item["slug"] for item in infos] == ["deepseek-v4.1-flash"]


def test_catalog_automation_never_touches_upstream_inference(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """回归：上游没声明能力时宁可少发，也不能真打上游推理接口。

    旧实现的「能力探测」会发一条真实请求，上游照常计费——线上 2026-09-21
    用户在账单里看到的就是它。这里用「一创建 HTTP 客户端就炸」的哨兵证明
    目录维护不再发任何请求。
    """
    import codex_ai_gateway.services.catalog_publishing as cp

    async def fake_search_models(**_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(cp, "search_models", fake_search_models)
    _forbid_inference_requests(monkeypatch)

    offering = _offering("mystery-model").model_copy(
        update={
            "native_metadata_json": {"id": "mystery-model", "context_length": 128000}
        }
    )
    state = _FakeState([offering], [_upstream()])
    runtime = _FakeRuntime(tmp_path, state)

    result = asyncio.run(cp.run_catalog_automation(runtime))

    assert result["accepted"] == 0
    assert state.publications == []
    candidate = next(item for item in state.catalog_candidates if item.offering_id == offering.id)
    assert candidate.selection_result == SelectionResult.rejected
    assert "tools_supported" in (candidate.rejection_reason or "")


def test_resync_keeps_offering_and_candidate_identity(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """模型同步复用 offering 身份，候选与既有目录证据不会每轮从零重建。"""
    import codex_ai_gateway.api.admin as admin
    import codex_ai_gateway.services.catalog_publishing as cp

    async def fake_fetch(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"id": "deepseek-v4.1-flash"}]

    async def fake_search_models(**_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(admin, "fetch_upstream_models", fake_fetch)
    monkeypatch.setattr(cp, "search_models", fake_search_models)
    _forbid_inference_requests(monkeypatch)

    upstream = _upstream().model_copy(
        update={"model_protocol_probe": {"deepseek-v4.1-flash": ["chat_completions"]}}
    )
    state = _FakeState([], [upstream])
    runtime = _FakeRuntime(tmp_path, state)

    asyncio.run(admin._run_upstream_pipeline(runtime, upstream))
    offering_ids = [offering.id for offering in state.offerings]
    asyncio.run(cp.run_catalog_automation(runtime))
    candidate_ids = [candidate.offering_id for candidate in state.catalog_candidates]

    asyncio.run(admin._run_upstream_pipeline(runtime, upstream))

    assert [offering.id for offering in state.offerings] == offering_ids
    assert [candidate.offering_id for candidate in state.catalog_candidates] == candidate_ids



def test_catalog_automation_publishes_all_declared_models(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """一次同步带来 12 个只有上游声明的模型：全部发布，且不发推理请求。"""
    import codex_ai_gateway.services.catalog_publishing as cp

    async def fake_search_models(**_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(cp, "search_models", fake_search_models)
    _forbid_inference_requests(monkeypatch)

    offerings = [
        _offering(f"upstream-model-{index}").model_copy(
            update={
                "native_metadata_json": {
                    "id": f"upstream-model-{index}",
                    "context_length": 128000,
                    "supported_parameters": ["tools", "tool_choice"],
                    "reasoning": {"supported_efforts": ["low", "medium"]},
                }
            }
        )
        for index in range(12)
    ]
    state = _FakeState(offerings, [_upstream()])
    runtime = _FakeRuntime(tmp_path, state)

    result = asyncio.run(cp.run_catalog_automation(runtime))

    assert result["accepted"] == len(offerings)



def test_run_catalog_automation_publishes_once_and_compacts(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import codex_ai_gateway.services.catalog_publishing as cp

    async def fake_search_models(**_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(cp, "search_models", fake_search_models)
    _forbid_inference_requests(monkeypatch)

    offering = _offering("deepseek-v4.1-flash").model_copy(
        update={
            "native_metadata_json": {
                "id": "deepseek-v4.1-flash",
                "context_length": 1000000,
                "supported_parameters": ["tools", "tool_choice"],
                "reasoning": {"supported_efforts": ["low"], "default_effort": "low"},
            }
        }
    )
    state = _FakeState([offering], [_upstream()])
    runtime = _FakeRuntime(tmp_path, state)

    first = asyncio.run(cp.run_catalog_automation(runtime))
    assert len(first["published"]) == 1
    assert len(state.publications) == 1
    mutates_after_first = runtime.state_store.mutate_calls

    second = asyncio.run(cp.run_catalog_automation(runtime))
    assert second["published"] == []
    assert len(state.publications) == 1, "内容未变化的模型不应重复发布"
    assert runtime.state_store.mutate_calls == mutates_after_first + 3



def _write_revision(
    tmp_path: Path, *, revision_id: str, entry_id: str, created_at: str
) -> None:
    revision = CatalogRevision(
        id=revision_id,
        parent_id=None,
        trigger="model_change",
        entry_ids=[entry_id],
        models_response_hash="hash",
        diff_summary={},
        status=CatalogRevisionStatus.published,
        created_at=created_at,
        published_at=created_at,
    )
    revisions_dir = tmp_path / "catalog/revisions"
    revisions_dir.mkdir(parents=True, exist_ok=True)
    (revisions_dir / f"{revision_id}.json").write_text(
        revision.model_dump_json(), encoding="utf-8"
    )


def test_compact_catalog_history_prunes_unreferenced(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import codex_ai_gateway.services.catalog_publishing as cp

    monkeypatch.setattr(cp, "CATALOG_REVISION_RETENTION", 2)
    entries = []
    for index in range(3):
        entry = _publication(
            tmp_path,
            slug="model",
            offering_id="off-1",
            accepted_at=f"2026-09-0{index + 1}T00:00:00+00:00",
            model_id=f"vendor/model-{index}",
        )
        _write_revision(
            tmp_path,
            revision_id=f"rev-{index}",
            entry_id=entry.id,
            created_at=f"2026-09-0{index + 1}T00:00:00+00:00",
        )
        entries.append(entry)

    state = _FakeState([], [])
    state.publications = list(entries)
    removed = compact_catalog_history(tmp_path, state)

    assert removed == {"publications": 1, "revisions": 1}
    assert [item.id for item in state.publications] == [entries[1].id, entries[2].id]
    assert not (tmp_path / "catalog/publications" / f"{entries[0].id}.json").exists()


def test_compact_catalog_history_drops_non_routable_publications(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import codex_ai_gateway.services.catalog_publishing as cp

    monkeypatch.setattr(cp, "CATALOG_REVISION_RETENTION", 1)
    ghost = _publication(
        tmp_path,
        slug="ghost-model",
        offering_id="off-ghost",
        accepted_at="2026-09-01T00:00:00+00:00",
        model_id="vendor/ghost-model",
    )
    routable = _publication(
        tmp_path,
        slug="keep-me",
        offering_id="off-keep",
        accepted_at="2026-09-02T00:00:00+00:00",
        model_id="vendor/keep-me",
    )
    _write_revision(
        tmp_path,
        revision_id="rev-ghost",
        entry_id=ghost.id,
        created_at="2026-09-01T00:00:00+00:00",
    )
    _write_revision(
        tmp_path,
        revision_id="rev-keep",
        entry_id=routable.id,
        created_at="2026-09-02T00:00:00+00:00",
    )

    state = _FakeState([], [])
    state.publications = [ghost, routable]
    state.canonical_models = [
        type("Model", (), {"slug": "keep-me", "status": "available"})()
    ]
    removed = compact_catalog_history(tmp_path, state)

    assert removed["publications"] == 1
    assert [item.id for item in state.publications] == [routable.id]
    assert not (tmp_path / "catalog/publications" / f"{ghost.id}.json").exists()
