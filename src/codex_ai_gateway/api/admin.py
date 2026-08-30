""""管理 API：设置、上游、模型、路由与网关 token。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from codex_ai_gateway.models.entities import (
    GatewayToken,
    Offering,
    PresetDiscoveryState,
    RoutingPreference,
    RoutingScope,
    TokenStatus,
    Upstream,
    UpstreamKind,
    UpstreamStatus,
    WireProtocol,
)
from codex_ai_gateway.models.schemas import (
    GatewayTokenView,
    PresetDiscoveryView,
    PresetView,
    RoutingPreferencePut,
    SettingsPatch,
    SettingsView,
    UpstreamCreate,
    UpstreamUpdate,
)
from codex_ai_gateway.services.catalog_publishing import (
    diff_catalog_revisions,
    get_catalog_revision,
    list_catalog_revisions,
    rollback_catalog_revision,
    run_catalog_automation,
)
from codex_ai_gateway.services.gateway_token import (
    apply_revoke,
    apply_rotation,
    create_gateway_token,
    rotate_gateway_token,
)
from codex_ai_gateway.services.local_codex import LocalCodexAutomationService
from codex_ai_gateway.services.model_aggregation import aggregate_models
from codex_ai_gateway.services.preset_discovery import (
    PresetDiscoveryResult,
    discover_preset,
    make_failure,
    make_snapshot,
)
from codex_ai_gateway.services.presets import (
    get_preset_provider,
    load_preset_catalog,
    preset_discovery_view,
    preset_view,
)
from codex_ai_gateway.services.upstreams import (
    build_preset_offerings,
    discover_offerings,
    probe_model_protocols,
)
from codex_ai_gateway.util import utc_now, uuid7

router = APIRouter(prefix="/admin")


def _runtime(request: Request):
    return request.app.state.runtime


def _not_found(message: str) -> HTTPException:
    raise HTTPException(status_code=404, detail=message)


@router.get("/settings", response_model=SettingsView)
def get_settings(request: Request) -> SettingsView:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    secret_ok = True
    try:
        runtime.secret_store.verify_usable()
    except Exception:
        secret_ok = False
    return SettingsView(
        listen_hint=state.settings.listen_hint,
        debug_capture_enabled=state.settings.debug_capture_enabled,
        debug_capture_limited=state.settings.debug_capture_limited,
        usage_retention_days=state.settings.usage_retention_days,
        codex_auto_integration_enabled=state.settings.codex_auto_integration_enabled,
        secret_backend_available=secret_ok,
    )


@router.patch("/settings")
async def patch_settings(request: Request, patch: SettingsPatch) -> SettingsView:
    runtime = _runtime(request)

    def apply(state: Any) -> None:
        if patch.usage_retention_days is not None:
            state.settings.usage_retention_days = patch.usage_retention_days
        if patch.debug_capture_enabled is not None:
            state.settings.debug_capture_enabled = patch.debug_capture_enabled
        if patch.codex_auto_integration_enabled is not None:
            state.settings.codex_auto_integration_enabled = patch.codex_auto_integration_enabled

    reopen = patch.codex_auto_integration_enabled is True
    runtime.state_store.mutate(apply)
    if reopen:
        LocalCodexAutomationService().run_auto_maintenance(runtime, trigger="auto_maintenance.reenabled")
    return get_settings(request)


def _upstream_view(upstream: Upstream, state: Any | None = None) -> dict[str, Any]:
    data = upstream.model_dump(mode="json")
    data["model_protocol_probe"] = upstream.model_protocol_probe
    if state is not None and upstream.kind == UpstreamKind.preset and upstream.preset_id:
        data["preset_discovery"] = preset_discovery_view(
            state,
            preset_id=upstream.preset_id,
            upstream_id=upstream.id,
        )
    return data


def _preset_discovery_payload(state: Any, upstream: Upstream) -> dict[str, Any]:
    if upstream.kind != UpstreamKind.preset or not upstream.preset_id:
        return {}
    current = preset_discovery_view(
        state,
        preset_id=upstream.preset_id,
        upstream_id=upstream.id,
    )
    current["upstream_id"] = upstream.id
    current["preset_id"] = upstream.preset_id
    snapshots = [
        item.model_dump(mode="json")
        for item in state.preset_snapshots
        if item.preset_id == upstream.preset_id and item.upstream_id == upstream.id
    ][-100:]
    failures = [
        item.model_dump(mode="json")
        for item in state.preset_discovery_failures
        if item.preset_id == upstream.preset_id and item.upstream_id == upstream.id
    ][-100:]
    current["snapshots"] = [
        {
            "id": item["id"],
            "model_count": len(item["model_ids"]),
            "body_sha256": item["body_sha256"],
            "fetched_at": item["fetched_at"],
            "extractor_key": item["extractor_key"],
            "extractor_version": item["extractor_version"],
        }
        for item in snapshots
    ]
    current["failures"] = [
        {
            "id": item["id"],
            "failure_code": item["failure_code"],
            "failure_message": item["failure_message"],
            "http_status": item["http_status"],
            "body_sha256": item["body_sha256"],
            "occurred_at": item["occurred_at"],
        }
        for item in failures
    ]
    return current


def _apply_preset_discovery(
    runtime: Any,
    upstream: Upstream,
    result: PresetDiscoveryResult,
    discovered: list[Offering],
) -> None:
    snapshot = make_snapshot(result, upstream_id=upstream.id) if result.status == "succeeded" else None
    failure = make_failure(result, upstream_id=upstream.id) if result.status == "failed" else None
    now = result.attempted_at

    def apply(state: Any) -> None:
        old_offering_ids = {
            item.id for item in state.offerings if item.upstream_id == upstream.id
        }
        state.offerings = [
            item for item in state.offerings if item.upstream_id != upstream.id
        ]
        state.model_mappings = [
            item for item in state.model_mappings if item.offering_id not in old_offering_ids
        ]
        if snapshot is not None:
            state.preset_snapshots.append(snapshot)
            state.preset_snapshots = state.preset_snapshots[-100:]
            state.offerings.extend(discovered)
        if failure is not None:
            state.preset_discovery_failures.append(failure)
            state.preset_discovery_failures = state.preset_discovery_failures[-100:]
        existing = next(
            (
                item
                for item in state.preset_discovery_states
                if item.preset_id == result.preset_id and item.upstream_id == upstream.id
            ),
            None,
        )
        if existing is None:
            existing = PresetDiscoveryState(
                id=uuid7(),
                preset_id=result.preset_id,
                upstream_id=upstream.id,
            )
            state.preset_discovery_states.append(existing)
        existing.last_attempt_at = now
        existing.current_model_count = len({
            item.provider_model_id for item in discovered
        }) if snapshot is not None else 0
        if snapshot is not None:
            existing.status = "succeeded"
            existing.last_success_at = now
            existing.latest_snapshot_id = snapshot.id
        if failure is not None:
            existing.status = "failed"
            existing.last_failure_at = now
            existing.latest_failure_id = failure.id

    runtime.state_store.mutate(apply, trigger="preset.discovery")



def _credential_ref(runtime: Any, upstream_id: str) -> str:
    return f"upstream:{upstream_id}:api_credential"


async def _run_upstream_pipeline(runtime: Any, upstream: Upstream) -> Upstream:
    """Run full pipeline: discover models -> probe protocols -> build offerings."""
    credential = runtime.secret_store.get_secret(upstream.auth_credential_ref) or ""

    # Phase 1: discover model IDs
    if upstream.kind == UpstreamKind.preset:
        preset = get_preset_provider(upstream.preset_id or "")
        discovery = await discover_preset(preset)
        if discovery.status != "succeeded":
            return _update_upstream_health(runtime, upstream, "探测失败：" + (discovery.failure_message or "未知错误"))
        model_ids = discovery.model_ids
    else:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{upstream.base_url.rstrip('/')}/models",
                    headers={"Authorization": f"Bearer {credential}"},
                )
                resp.raise_for_status()
                data = resp.json()
            model_ids = [item["id"] for item in data.get("data", []) if isinstance(item, dict) and item.get("id")]
        except Exception:
            model_ids = []

    if not model_ids:
        return _update_upstream_health(runtime, upstream, "未发现可用模型")

    # Phase 2: incremental diff - only probe new models
    existing_probe = dict(upstream.model_protocol_probe or {})
    to_probe = [mid for mid in model_ids if mid not in existing_probe]
    removed = [mid for mid in existing_probe if mid not in set(model_ids)]

    # Phase 3: probe model protocols (batched)
    if to_probe:
        new_results = await probe_model_protocols(upstream, credential, to_probe)
        for mid in removed:
            existing_probe.pop(mid, None)
        for mid, protocols in new_results.items():
            existing_probe[mid] = [p.value for p in protocols]
    else:
        for mid in removed:
            existing_probe.pop(mid, None)

    # Phase 4: build offerings
    protocol_map = {
        mid: [WireProtocol(p) for p in protocols]
        for mid, protocols in existing_probe.items()
    }
    if upstream.kind == UpstreamKind.preset:
        snapshot_id = uuid7()
        discovery.snapshot_id = snapshot_id
        discovered = build_preset_offerings(
            upstream, discovery, snapshot_id=snapshot_id, protocol_map=protocol_map,
        )
        _apply_preset_discovery(runtime, upstream, discovery, discovered)
    else:
        discovered = await discover_offerings(upstream, credential, protocol_map=protocol_map)
        def apply(state: Any) -> None:
            old_ids = {o.id for o in state.offerings if o.upstream_id == upstream.id}
            state.offerings = [o for o in state.offerings if o.upstream_id != upstream.id]
            state.model_mappings = [m for m in state.model_mappings if m.offering_id not in old_ids]
            state.offerings.extend(discovered)
        runtime.state_store.mutate(apply, trigger="upstream.offerings.refresh")

    confirmed_count = sum(1 for ps in existing_probe.values() if ps)
    result_text = f"探测完成：{len(model_ids)} 个模型，{confirmed_count} 个有可用协议"
    refreshed = _update_upstream_health(runtime, upstream, result_text, model_protocol_probe=existing_probe)
    return refreshed


def _update_upstream_health(
    runtime: Any,
    upstream: Upstream,
    result_text: str,
    *,
    model_protocol_probe: dict[str, list[str]] | None = None,
) -> Upstream:
    update = {
        "last_health_at": utc_now(),
        "last_health_result": result_text,
        "cooldown_until": None,
        "updated_at": utc_now(),
    }
    if model_protocol_probe is not None:
        update["model_protocol_probe"] = model_protocol_probe
    refreshed = upstream.model_copy(update=update)

    def apply(state: Any) -> None:
        for index, item in enumerate(state.upstreams):
            if item.id == upstream.id:
                state.upstreams[index] = refreshed

    runtime.state_store.mutate(apply)
    return refreshed


@router.get("/upstreams")
async def list_upstreams(request: Request) -> list[dict[str, Any]]:
    state = _runtime(request).state_store.read_state()
    return [_upstream_view(u, state) for u in state.upstreams]



@router.get("/presets", response_model=list[PresetView])
def list_presets(request: Request) -> list[dict[str, Any]]:
    catalog = load_preset_catalog()
    state = _runtime(request).state_store.read_state()
    return [preset_view(item, state) for item in catalog.presets]


@router.get("/upstreams/{upstream_id}/discovery", response_model=PresetDiscoveryView)
async def get_preset_discovery(request: Request, upstream_id: str) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    upstream = next((item for item in state.upstreams if item.id == upstream_id), None)
    if upstream is None:
        _not_found("上游不存在")
    if upstream.kind != UpstreamKind.preset or not upstream.preset_id:
        raise HTTPException(status_code=422, detail="只有预设 Provider 支持文档模型发现")
    return _preset_discovery_payload(state, upstream)


@router.post("/upstreams")
async def create_upstream(request: Request, payload: UpstreamCreate) -> dict[str, Any]:
    runtime = _runtime(request)
    now = utc_now()
    upstream_id = uuid7()
    if payload.kind == "preset":
        preset = get_preset_provider(payload.preset_id or "")
        name = preset.name
        base_url = preset.base_url
        default_headers = preset.default_headers
        kind = UpstreamKind.preset
        preset_version = load_preset_catalog().version
    else:
        name = payload.name or ""
        base_url = (payload.base_url or "").rstrip("/")
        default_headers = payload.default_headers
        kind = UpstreamKind.custom
        preset_version = None
    upstream = Upstream(
        id=upstream_id,
        name=name,
        kind=kind,
        preset_id=payload.preset_id,
        preset_version=preset_version,
        base_url=base_url,
        auth_credential_ref=_credential_ref(runtime, upstream_id),
        default_headers=default_headers,
        created_at=now,
        updated_at=now,
    )

    def apply(state: Any) -> None:
        if any(u.name == upstream.name for u in state.upstreams):
            raise HTTPException(status_code=409, detail="上游名称已存在")
        state.upstreams.append(upstream)

    runtime.state_store.mutate(apply)
    if payload.api_credential:
        runtime.secret_store.set_secret(upstream.auth_credential_ref, payload.api_credential)
    refreshed = await _run_upstream_pipeline(runtime, upstream)
    await _offerings(runtime, refreshed)
    await _maybe_aggregate(runtime)
    return _upstream_view(refreshed, runtime.state_store.read_state())

@router.post("/debug/probe/{upstream_id}")
async def debug_probe(request: Request, upstream_id: str) -> dict[str, Any]:
    """Backend-only debug probe. Not exposed in frontend UI."""
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    existing = next((u for u in state.upstreams if u.id == upstream_id), None)
    if existing is None:
        _not_found('上游不存在')
    refreshed = await _run_upstream_pipeline(runtime, existing)
    await _maybe_aggregate(runtime)
    new_state = runtime.state_store.read_state()
    offerings = [o.model_dump(mode='json') for o in new_state.offerings if o.upstream_id == upstream_id]
    return {
        'upstream_id': upstream_id,
        'model_protocol_probe': refreshed.model_protocol_probe,
        'last_health_result': refreshed.last_health_result,
        'offerings_count': len(offerings),
    }



@router.put("/upstreams/{upstream_id}")
async def update_upstream(request: Request, upstream_id: str, payload: UpstreamUpdate) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    existing = next((u for u in state.upstreams if u.id == upstream_id), None)
    if existing is None:
        _not_found("上游不存在")
    if existing.kind == UpstreamKind.preset and (payload.name is not None or payload.base_url is not None):
        raise HTTPException(status_code=422, detail="预设 Provider 不允许手工修改 name/base_url")
    update: dict[str, Any] = {"updated_at": utc_now()}
    if payload.name is not None:
        update["name"] = payload.name
    if payload.base_url is not None:
        update["base_url"] = payload.base_url.rstrip("/")
    if payload.status is not None:
        update["status"] = UpstreamStatus(payload.status)
    if payload.default_headers is not None:
        update["default_headers"] = payload.default_headers
    updated = existing.model_copy(update=update)

    def apply(state: Any) -> None:
        for index, item in enumerate(state.upstreams):
            if item.id == upstream_id:
                state.upstreams[index] = updated

    runtime.state_store.mutate(apply)
    if payload.api_credential:
        runtime.secret_store.set_secret(existing.auth_credential_ref, payload.api_credential)
    refreshed = await _run_upstream_pipeline(runtime, updated)
    await _offerings(runtime, refreshed)
    await _maybe_aggregate(runtime)
    return _upstream_view(refreshed, runtime.state_store.read_state())


@router.delete("/upstreams/{upstream_id}")
async def delete_upstream(request: Request, upstream_id: str) -> dict[str, Any]:
    runtime = _runtime(request)
    runtime.state_store.mutate(lambda s: setattr(
        s,
        "upstreams",
        [u for u in s.upstreams if u.id != upstream_id],
    ))
    runtime.state_store.mutate(
        lambda s: setattr(s, "offerings", [o for o in s.offerings if o.upstream_id != upstream_id])
    )
    await _maybe_aggregate(runtime)
    return {"id": upstream_id, "deleted": True}


@router.post("/upstreams/{upstream_id}/probe")
async def probe(request: Request, upstream_id: str) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    existing = next((u for u in state.upstreams if u.id == upstream_id), None)
    if existing is None:
        _not_found("上游不存在")
    refreshed = await _run_upstream_pipeline(runtime, existing)
    await _offerings(runtime, refreshed)
    await _maybe_aggregate(runtime)
    return _upstream_view(refreshed, runtime.state_store.read_state())


async def _offerings(runtime: Any, upstream: Upstream) -> list[Offering]:
    """Legacy entry point - delegates to pipeline."""
    await _run_upstream_pipeline(runtime, upstream)
    await run_catalog_automation(runtime)
    LocalCodexAutomationService().run_auto_maintenance(runtime, trigger='catalog.changed')
    return [o for o in runtime.state_store.read_state().offerings if o.upstream_id == upstream.id]

async def _maybe_aggregate(runtime: Any) -> dict[str, int]:
    state = runtime.state_store.read_state()
    return await aggregate_models(runtime, offerings=state.offerings, upstreams=state.upstreams)


@router.get("/models")
async def list_models(request: Request) -> list[dict[str, Any]]:
    state = _runtime(request).state_store.read_state()
    result = []
    for model in state.canonical_models:
        if model.status != "available":
            continue
        offerings = [o for o in state.offerings if o.canonical_model_id == model.id]
        upstreams = []
        for offering in offerings:
            upstream = next((u for u in state.upstreams if u.id == offering.upstream_id), None)
            if upstream and upstream.name not in upstreams:
                upstreams.append(upstream.name)
        override = next(
            (r for r in state.routing_preferences if r.canonical_model_id == model.id), None
        )
        result.append({
            "id": model.id,
            "openrouter_model_id": model.openrouter_model_id,
            "display_name": model.display_name,
            "slug": model.slug,
            "metadata_status": "complete" if model.capability_baseline else "missing",
            "upstream_count": len(upstreams),
            "upstream_names": upstreams,
            "priority_summary": "自定义" if override else "继承全局",
        })
    return result


@router.get("/models/{model_id}")
async def get_model(request: Request, model_id: str) -> dict[str, Any]:
    state = _runtime(request).state_store.read_state()
    model = next((m for m in state.canonical_models if m.id == model_id), None)
    if model is None:
        _not_found("模型不存在")
    evidence = [m for m in state.model_mappings if m.openrouter_model_id == model.openrouter_model_id]
    offering_ids = {o.id for o in state.offerings if o.canonical_model_id == model.id}
    candidates = [c for c in state.catalog_candidates if c.offering_id in offering_ids]
    candidate_ids = {c.id for c in candidates}
    catalog_evidence = [
        item.model_dump(mode="json")
        for item in state.catalog_evidence
        if item.candidate_id in candidate_ids
    ]
    return {
        "model": model.model_dump(mode="json"),
        "identity_evidence": [m.model_dump(mode="json") for m in evidence],
        "catalog_candidates": [c.model_dump(mode="json") for c in candidates],
        "catalog_evidence": catalog_evidence,
    }


@router.get("/upstreams/{upstream_id}/offerings")
async def list_upstream_offerings(request: Request, upstream_id: str) -> list[dict[str, Any]]:
    state = _runtime(request).state_store.read_state()
    if not any(u.id == upstream_id for u in state.upstreams):
        _not_found("上游不存在")
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for o in state.offerings:
        if o.upstream_id != upstream_id:
            continue
        key = (o.provider_model_id, o.wire_protocol.value)
        unique[key] = {
            "id": o.id,
            "provider_model_id": o.provider_model_id,
            "provider_version": o.provider_version,
            "display_name": o.display_name,
            "status": o.status.value,
            "canonical_model_id": o.canonical_model_id,
        }
    return list(unique.values())


@router.get("/catalog/revisions")
async def catalog_revisions(request: Request) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in list_catalog_revisions(_runtime(request).data_dir)]


@router.get("/catalog/revisions/{revision_id}")
async def catalog_revision_detail(request: Request, revision_id: str) -> dict[str, Any]:
    try:
        return get_catalog_revision(_runtime(request).data_dir, revision_id).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/catalog/revisions/{revision_id}/diff")
async def catalog_revision_diff(request: Request, revision_id: str, target_id: str | None = None) -> dict[str, Any]:
    try:
        return diff_catalog_revisions(_runtime(request).data_dir, revision_id, target_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/catalog/revisions/{revision_id}/rollback")
async def rollback_revision(request: Request, revision_id: str) -> dict[str, Any]:
    try:
        revision = await rollback_catalog_revision(_runtime(request).data_dir, revision_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    def apply(state):
        if not any(item.id == revision.id for item in state.catalog_revisions):
            state.catalog_revisions.append(revision)

    _runtime(request).state_store.mutate(apply)
    return revision.model_dump(mode="json")


@router.get("/routing")
async def get_routing(request: Request) -> list[dict[str, Any]]:
    state = _runtime(request).state_store.read_state()
    return [r.model_dump(mode="json") for r in state.routing_preferences]


@router.put("/routing/global")
async def put_global_routing(request: Request, payload: RoutingPreferencePut) -> dict[str, Any]:
    return await _put_routing(request, None, payload)


@router.put("/routing/models/{model_id}")
async def put_model_routing(request: Request, model_id: str, payload: RoutingPreferencePut) -> dict[str, Any]:
    return await _put_routing(request, model_id, payload)


async def _put_routing(request: Request, model_id: str | None, payload: RoutingPreferencePut) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    upstream_ids = {u.id for u in state.upstreams}
    if model_id is not None and not any(m.id == model_id for m in state.canonical_models):
        _not_found("模型不存在")
    if not payload.ordered_upstream_ids or any(i not in upstream_ids for i in payload.ordered_upstream_ids):
        raise HTTPException(status_code=422, detail="上游优先级必须覆盖有效上游")
    now = utc_now()

    def apply(state: Any) -> None:
        if model_id is None:
            scope = RoutingScope.global_preference
            state.routing_preferences = [r for r in state.routing_preferences if r.scope != scope]
        else:
            scope = RoutingScope.canonical_model
            state.routing_preferences = [
                r for r in state.routing_preferences if not (r.scope == scope and r.canonical_model_id == model_id)
            ]
        state.routing_preferences.append(
            RoutingPreference(
                id=uuid7(), scope=scope, canonical_model_id=model_id,
                ordered_upstream_ids=payload.ordered_upstream_ids, updated_at=now,
            )
        )

    runtime.state_store.mutate(apply)
    return {"scope": "global" if model_id is None else "canonical_model", "canonical_model_id": model_id, "ordered_upstream_ids": payload.ordered_upstream_ids}


@router.get("/gateway-token")
async def get_gateway_token(request: Request) -> GatewayTokenView:
    state = _runtime(request).state_store.read_state()
    token = next((t for t in state.gateway_tokens if t.status == TokenStatus.active), None)
    if token is None:
        raise HTTPException(status_code=404, detail="尚未生成网关 token")
    return _token_view(token)


@router.post("/gateway-token/rotate")
async def rotate_gateway_token_api(request: Request) -> GatewayTokenView:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    active = next((t for t in state.gateway_tokens if t.status == TokenStatus.active), None)
    new_record, raw = (
        rotate_gateway_token(active, runtime.signing_key)
        if active else create_gateway_token(runtime.signing_key)
    )
    runtime.secret_store.set_secret("gateway:token", raw)
    runtime.state_store.mutate(lambda s: apply_rotation(s, active, new_record) if active else s.gateway_tokens.append(new_record))
    await _trigger_codex_if_configured(runtime)
    return _token_view(new_record, secret=raw)


@router.post("/gateway-token/revoke")
async def revoke_gateway_token(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    active = next((t for t in state.gateway_tokens if t.status == TokenStatus.active), None)
    if active is None:
        _not_found("尚未生成网关 token")
    runtime.state_store.mutate(lambda s: apply_revoke(s, active))
    return {"id": active.id, "status": "revoked"}


def _token_view(token: GatewayToken, *, secret: str | None = None) -> GatewayTokenView:
    return GatewayTokenView(
        id=token.id, status=token.status.value, prefix=token.prefix, last4=token.last4,
        issued_at=token.issued_at, successor_id=token.successor_id,
        predecessor_id=token.predecessor_id, last_used_at=token.last_used_at, token=secret,
    )


async def _trigger_codex_if_configured(runtime: Any) -> None:
    if runtime.state_store.read_state().integration_profiles:
        LocalCodexAutomationService().run_auto_maintenance(runtime, trigger="gateway_token.change")
