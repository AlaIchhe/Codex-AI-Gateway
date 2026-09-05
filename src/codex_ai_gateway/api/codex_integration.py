"""Local Codex 集成端点。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from codex_ai_gateway.integrations.codex_context import (
    CodexContextError,
    create_skill,
    delete_mcp_server,
    delete_skill,
    list_mcp_servers,
    list_skills,
    upsert_mcp_server,
)
from codex_ai_gateway.integrations.codex_plugin_marketplace import (
    CodexPluginMarketplaceError,
    list_plugin_marketplaces,
    register_local_marketplace,
    remove_marketplace,
    resolve_plugin_icon,
    set_plugin_enabled,
)
from codex_ai_gateway.integrations.codex_writer import resolve_config_path, resolve_profile_path
from codex_ai_gateway.models.entities import (
    IntegrationProfile,
    IntegrationRevision,
    IntegrationState,
)
from codex_ai_gateway.models.schemas import (
    ApplyRequest,
    MarketplaceRegisterRequest,
    McpServerUpsertRequest,
    PluginToggleRequest,
    ProfileCreate,
    ProfilePreviewRequest,
    SettingsPatch,
    SettingsView,
    SkillCreateRequest,
)
from codex_ai_gateway.runtime import Runtime
from codex_ai_gateway.services.local_codex import (
    CodexDriftError,
    LocalCodexAutomationService,
    LocalCodexService,
)
from codex_ai_gateway.util import utc_now, uuid7

router = APIRouter(prefix="/admin/codex")
rev_router = APIRouter(prefix="/admin/integration-revisions")


def _runtime(request: Request) -> Runtime:
    return request.app.state.runtime


@router.get("/integration/status")
def integration_status(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    return LocalCodexAutomationService().status(runtime)


@router.patch("/settings/local-integration", response_model=SettingsView)
def patch_local_integration(request: Request, patch: SettingsPatch) -> SettingsView:
    """自动维护开关；请求体只允许该开关，避免管理设置接口耦合集成编排。"""
    if patch.codex_auto_integration_enabled is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="codex_auto_integration_enabled 必填")
    patch.codex_auto_integration_enabled = patch.codex_auto_integration_enabled
    patch.debug_capture_enabled = None
    patch.usage_retention_days = None
    runtime = _runtime(request)
    runtime.state_store.mutate(lambda state: setattr(state.settings, "codex_auto_integration_enabled", patch.codex_auto_integration_enabled))
    state = runtime.state_store.read_state()
    secret_ok = True
    try:
        runtime.secret_store.verify_usable()
    except Exception:  # noqa: BLE001
        secret_ok = False
    return SettingsView(
        listen_hint=state.settings.listen_hint,
        debug_capture_enabled=state.settings.debug_capture_enabled,
        debug_capture_limited=state.settings.debug_capture_limited,
        usage_retention_days=state.settings.usage_retention_days,
        codex_auto_integration_enabled=state.settings.codex_auto_integration_enabled,
        secret_backend_available=secret_ok,
    )


@router.get("/plugin-marketplaces")
def get_plugin_marketplaces(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    profile = next((p for p in state.integration_profiles if p.config_path), None)
    if profile is None:
        raise HTTPException(status_code=404, detail="本地 Codex profile 不存在")
    return list_plugin_marketplaces(profile.config_path)


@router.post("/plugin-marketplaces")
def post_register_marketplace(
    request: Request,
    payload: MarketplaceRegisterRequest,
) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    profile = next((p for p in state.integration_profiles if p.config_path), None)
    if profile is None:
        raise HTTPException(status_code=404, detail="本地 Codex profile 不存在")
    try:
        return register_local_marketplace(
            profile.config_path,
            name=payload.name,
            source=payload.source,
            default_enabled=payload.default_enabled,
        )
    except CodexPluginMarketplaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/plugins/toggle")
def post_toggle_plugin(request: Request, payload: PluginToggleRequest) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    profile = next((p for p in state.integration_profiles if p.config_path), None)
    if profile is None:
        raise HTTPException(status_code=404, detail="本地 Codex profile 不存在")
    try:
        return set_plugin_enabled(
            profile.config_path,
            plugin_id=payload.plugin_id,
            enabled=payload.enabled,
        )
    except CodexPluginMarketplaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/plugin-marketplaces/{name}/plugins/{plugin_name}/icon")
def get_plugin_icon(request: Request, name: str, plugin_name: str) -> FileResponse:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    profile = next((p for p in state.integration_profiles if p.config_path), None)
    if profile is None:
        raise HTTPException(status_code=404, detail="本地 Codex profile 不存在")
    try:
        icon_path, media_type = resolve_plugin_icon(
            profile.config_path,
            marketplace_name=name,
            plugin_name=plugin_name,
        )
    except CodexPluginMarketplaceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(icon_path, media_type=media_type)


@router.delete("/plugin-marketplaces/{name}")
def delete_marketplace(request: Request, name: str) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    profile = next((p for p in state.integration_profiles if p.config_path), None)
    if profile is None:
        raise HTTPException(status_code=404, detail="本地 Codex profile 不存在")
    try:
        return remove_marketplace(profile.config_path, name=name)
    except CodexPluginMarketplaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/mcp-servers")
def get_mcp_servers(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    profile = next((p for p in state.integration_profiles if p.config_path), None)
    if profile is None:
        raise HTTPException(status_code=404, detail="本地 Codex profile 不存在")
    return list_mcp_servers(profile.config_path)


@router.post("/mcp-servers")
def post_mcp_server(request: Request, payload: McpServerUpsertRequest) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    profile = next((p for p in state.integration_profiles if p.config_path), None)
    if profile is None:
        raise HTTPException(status_code=404, detail="本地 Codex profile 不存在")
    try:
        return upsert_mcp_server(
            profile.config_path,
            name=payload.name,
            command=payload.command,
            args=payload.args,
            url=payload.url,
            env=payload.env,
        )
    except CodexContextError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/mcp-servers/{name}")
def delete_mcp(name: str, request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    profile = next((p for p in state.integration_profiles if p.config_path), None)
    if profile is None:
        raise HTTPException(status_code=404, detail="本地 Codex profile 不存在")
    try:
        return delete_mcp_server(profile.config_path, name=name)
    except CodexContextError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/skills")
def get_skills(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    profile = next((p for p in state.integration_profiles if p.codex_home), None)
    if profile is None:
        raise HTTPException(status_code=404, detail="本地 Codex profile 不存在")
    return list_skills(profile.codex_home)


@router.post("/skills")
def post_skill(request: Request, payload: SkillCreateRequest) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    profile = next((p for p in state.integration_profiles if p.codex_home), None)
    if profile is None:
        raise HTTPException(status_code=404, detail="本地 Codex profile 不存在")
    try:
        return create_skill(
            profile.codex_home,
            skill_id=payload.skill_id,
            name=payload.name,
            description=payload.description,
        )
    except CodexContextError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/skills/{skill_id}")
def delete_skill_route(request: Request, skill_id: str) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    profile = next((p for p in state.integration_profiles if p.codex_home), None)
    if profile is None:
        raise HTTPException(status_code=404, detail="本地 Codex profile 不存在")
    try:
        return delete_skill(profile.codex_home, skill_id=skill_id)
    except CodexContextError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/profiles")
def list_profiles(request: Request) -> list[dict[str, Any]]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    return [p.model_dump(mode="json") for p in state.integration_profiles]


@router.post("/profiles")
def create_profile(request: Request, payload: ProfileCreate) -> dict[str, Any]:
    runtime = _runtime(request)
    profile = IntegrationProfile(
        id=uuid7(),
        display_name=payload.display_name,
        codex_home=payload.codex_home,
        config_path=resolve_config_path(payload.codex_home),
        profile_path=resolve_profile_path(payload.codex_home, "codex-ai-gateway"),
        expected_provider_id=payload.expected_provider_id or "codex-ai-gateway",
        expected_base_url=payload.expected_base_url or "http://127.0.0.1:8787/v1",
        schema_contract_version="1",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    runtime.state_store.mutate(lambda s: s.integration_profiles.append(profile))
    return profile.model_dump(mode="json")


@router.post("/profiles/{profile_id}/preview")
def preview_profile(request: Request, profile_id: str, payload: ProfilePreviewRequest) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    profile = next((p for p in state.integration_profiles if p.id == profile_id), None)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile 不存在")
    service = LocalCodexService(runtime.data_dir)
    preview, new_text = service.create_preview(profile)
    revision_id = payload.revision_id or uuid7()
    revision = IntegrationRevision(
        id=revision_id,
        profile_id=profile_id,
        state=IntegrationState.preview_ready,
        preview_diff_json=preview,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    runtime.state_store.mutate(lambda s: _upsert_revision(s, revision))
    return {"revision_id": revision_id, "preview": preview, "new_config": new_text}


@router.post("/profiles/{profile_id}/apply")
def apply_profile(request: Request, profile_id: str, payload: ApplyRequest) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    profile = next((p for p in state.integration_profiles if p.id == profile_id), None)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile 不存在")
    revision = next((r for r in state.integration_revisions if r.id == payload.revision_id), None)
    if revision is None:
        raise HTTPException(status_code=404, detail="revision 不存在")
    service = LocalCodexService(runtime.data_dir)
    gateway_token = runtime.secret_store.get_secret("gateway:token")
    try:
        updated = service.apply_revision(
            profile=profile,
            revision=revision,
            submitted_fingerprint=payload.fingerprint,
            gateway_token=gateway_token,
        )
    except CodexDriftError as exc:
        runtime.state_store.mutate(lambda s: _mark_drift(s, payload.revision_id))
        raise HTTPException(status_code=409, detail="codex_config_drift") from exc
    runtime.state_store.mutate(lambda s: _upsert_revision(s, updated))
    return updated.model_dump(mode="json")


@rev_router.get("/{revision_id}")
def get_revision(request: Request, revision_id: str) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    revision = next((r for r in state.integration_revisions if r.id == revision_id), None)
    if revision is None:
        raise HTTPException(status_code=404, detail="revision 不存在")
    return revision.model_dump(mode="json")


@rev_router.post("/{revision_id}/rollback")
def rollback(request: Request, revision_id: str) -> dict[str, Any]:
    runtime = _runtime(request)
    state = runtime.state_store.read_state()
    revision = next((r for r in state.integration_revisions if r.id == revision_id), None)
    if revision is None:
        raise HTTPException(status_code=404, detail="revision 不存在")
    profile = next((p for p in state.integration_profiles if p.id == revision.profile_id), None)
    if revision.state == IntegrationState.applied_validated and profile is not None:
        service = LocalCodexService(runtime.data_dir)
        rp = runtime.data_dir / (revision.recovery_point_path or "")
        config_path = profile.config_path or ""
        catalog_path = profile.catalog_path or str(Path(profile.codex_home) / "model_catalog.json")
        if rp.exists():
            service._rollback(rp, [config_path, catalog_path])
        revision.state = IntegrationState.rolled_back
        revision.updated_at = utc_now()
        runtime.state_store.mutate(lambda s: _upsert_revision(s, revision))
    return revision.model_dump(mode="json")


def _upsert_revision(state: Any, revision: IntegrationRevision) -> None:
    for i, r in enumerate(state.integration_revisions):
        if r.id == revision.id:
            state.integration_revisions[i] = revision
            return
    state.integration_revisions.append(revision)


def _mark_drift(state: Any, revision_id: str) -> None:
    for r in state.integration_revisions:
        if r.id == revision_id:
            r.state = IntegrationState.drift_detected
            r.updated_at = utc_now()
