"""目录维护与发布管理端点。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from codex_ai_gateway.integrations.openrouter_metadata import (
    extract_model_metadata,
    find_identity_candidate,
    search_models,
)
from codex_ai_gateway.models.entities import (
    CatalogCandidate,
    CatalogEvidenceSet,
    MappingStatus,
)
from codex_ai_gateway.models.schemas import (
    CandidateSearchRequest,
    MaintenanceJobRequest,
    PublicationRequest,
)
from codex_ai_gateway.runtime import Runtime
from codex_ai_gateway.services.catalog_publishing import (
    CatalogPublishingService,
    evaluate_fields,
)
from codex_ai_gateway.util import utc_now, uuid7

router = APIRouter(prefix="/admin/catalog")


def _runtime(request: Request) -> Runtime:
    return request.app.state.runtime


@router.post("/candidates/search")
async def search_candidates(request: Request, payload: CandidateSearchRequest) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    offering = next((o for o in state.offerings if o.id == payload.offering_id), None)
    if offering is None:
        raise HTTPException(status_code=404, detail="offering 不存在")
    # 用固定 offline fixture 语料，避免真实计费请求
    models = await search_models(query=payload.proposed_slug)
    identity = find_identity_candidate(models, provider_model_id=offering.provider_model_id)
    if identity is None:
        # 无法联网时接受候选身份由操作员确认
        fallback_slug = payload.proposed_slug or offering.provider_model_id
        candidate = CatalogCandidate(
            id=uuid7(),
            offering_id=offering.id,
            upstream_id=offering.upstream_id,
            proposed_alias_slug=fallback_slug,
            mapping_status=MappingStatus.operator_confirmed,
            selection_result="rejected",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        runtime.state_store.mutate(lambda s: s.catalog_candidates.append(candidate))
        fields = evaluate_fields(candidate, {})
        runtime.state_store.mutate(
            lambda s: _replace_evidence(
                s,
                CatalogEvidenceSet(candidate_id=candidate.id, fields=fields),
            )
        )
        return {"candidate": candidate.model_dump(mode="json"), "identity_candidates": []}
    meta = extract_model_metadata(identity)
    candidate = CatalogCandidate(
        id=uuid7(),
        offering_id=offering.id,
        upstream_id=offering.upstream_id,
        proposed_alias_slug=payload.proposed_slug,
        openrouter_model_id=identity.get("id"),
        openrouter_version_id=meta.get("version_id"),
        mapping_status=MappingStatus.automatic_confirmed,
        selection_result="rejected",
        public_snapshot_url="https://openrouter.ai/api/v1/models",
        public_snapshot_version=meta.get("version_id"),
        public_snapshot_time=utc_now(),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    runtime.state_store.mutate(lambda s: s.catalog_candidates.append(candidate))
    fields = evaluate_fields(candidate, meta)
    runtime.state_store.mutate(
        lambda s: _append_evidence(s, candidate.id, fields)
    )
    return {"candidate": candidate.model_dump(mode="json"), "identity_candidates": [identity]}


@router.post("/maintenance/jobs")
async def run_maintenance(request: Request, payload: MaintenanceJobRequest) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    candidates = [c for c in state.catalog_candidates if c.id in payload.candidate_ids]
    service = CatalogPublishingService(runtime.data_dir)
    evidence_sets = await service.run_maintenance(
        candidates, state.offerings, state.upstreams
    )
    for ev in evidence_sets:
        runtime.state_store.mutate(
            lambda s, ev=ev: _replace_evidence(s, ev)
        )
    return {"job_id": uuid7(), "candidates": len(candidates), "status": "complete"}


@router.get("/maintenance/jobs/{job_id}")
def get_job(request: Request, job_id: str) -> dict[str, Any]:
    return {"job_id": job_id, "status": "complete"}


@router.get("/offering-candidates")
def list_candidates(request: Request, state_filter: str | None = None) -> list[dict[str, Any]]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    result = []
    for cand in state.catalog_candidates:
        if state_filter and cand.selection_result.value != state_filter:
            continue
        item = cand.model_dump(mode="json")
        item["evidence"] = _evidence_for(state, cand.id)
        result.append(item)
    return result


@router.post("/publications")
async def publish(request: Request, payload: PublicationRequest) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    candidates = [c for c in state.catalog_candidates if c.id in payload.candidate_ids]
    if not candidates:
        raise HTTPException(status_code=404, detail="没有可发布的候选")
    offering = next((o for o in state.offerings if o.id == candidates[0].offering_id), None)
    if offering is None:
        raise HTTPException(status_code=404, detail="offering 不存在")
    evidence_sets = [e for e in state.catalog_evidence if e.candidate_id in payload.candidate_ids]
    service = CatalogPublishingService(runtime.data_dir)
    entry = await service.build_publication(candidates, evidence_sets, offering)
    runtime.state_store.mutate(lambda s: s.publications.append(entry))
    return entry.model_dump(mode="json")


@router.get("/publications/{publication_id}/model-info.json")
def download_model_info(request: Request, publication_id: str) -> JSONResponse:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    entry = next((p for p in state.publications if p.id == publication_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="publication 不存在")
    return JSONResponse(content=entry.model_info_json)


def _append_evidence(state: Any, candidate_id: str, fields: list[Any]) -> None:
    _replace_evidence(
        state,
        CatalogEvidenceSet(candidate_id=candidate_id, fields=fields),
    )


def _replace_evidence(state: Any, ev_set: Any) -> None:
    state.catalog_evidence = [
        e for e in state.catalog_evidence if e.candidate_id != ev_set.candidate_id
    ]
    state.catalog_evidence.append(ev_set)


def _evidence_for(state: Any, candidate_id: str) -> list[dict[str, Any]]:
    ev_set = next((e for e in state.catalog_evidence if e.candidate_id == candidate_id), None)
    if ev_set is None:
        return []
    return [f.model_dump(mode="json") for f in ev_set.fields]
