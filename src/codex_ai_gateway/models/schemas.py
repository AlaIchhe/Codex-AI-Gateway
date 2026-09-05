""""管理 API 请求/响应模型与 RFC 9457 problem+json 结构。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class SettingsPatch(BaseModel):
    usage_retention_days: int | None = Field(default=None, ge=1)
    debug_capture_enabled: bool | None = None
    codex_auto_integration_enabled: bool | None = None


class SettingsView(BaseModel):
    listen_hint: str
    debug_capture_enabled: bool
    debug_capture_limited: bool
    usage_retention_days: int
    codex_auto_integration_enabled: bool
    secret_backend_available: bool
    recent_recovery_point: str | None = None


class UpstreamCreate(BaseModel):
    kind: str = "custom"
    preset_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=80)
    base_url: str | None = None
    api_credential: str | None = None
    default_headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, v: str) -> str:
        if v not in {"custom", "preset"}:
            raise ValueError("kind 只能是 custom 或 preset")
        return v

    @model_validator(mode="after")
    def validate_form(self) -> UpstreamCreate:
        if self.kind == "preset":
            if not self.preset_id:
                raise ValueError("preset 创建必须提供 preset_id")
            if self.name or self.base_url:
                raise ValueError("preset 创建不能手填 name/base_url")
            return self
        if not self.name or not self.base_url:
            raise ValueError("custom 创建必须提供 name/base_url")
        self.base_url = self.base_url.strip().rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return self

    @field_validator("default_headers")
    @classmethod
    def reject_auth_headers(cls, v: dict[str, str]) -> dict[str, str]:
        for key in v:
            if key.lower() in {"authorization", "proxy-authorization", "x-api-key", "api-key"}:
                raise ValueError(f"不允许在 default_headers 中设置 {key}")
        return v


class PresetView(BaseModel):
    preset_id: str
    name: str
    icon: str
    base_url: str
    doc_url: str
    model_source: str
    extractor_key: str
    extractor_version: str
    model_count: int | None = None
    current_model_count: int = 0
    discovery_status: str = "never"
    source: dict[str, Any]


class PresetDiscoveryView(BaseModel):
    upstream_id: str
    preset_id: str
    status: str
    current_model_count: int
    latest_snapshot_id: str | None = None
    latest_failure_id: str | None = None
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None
    snapshots: list[dict[str, Any]] = Field(default_factory=list)
    failures: list[dict[str, Any]] = Field(default_factory=list)


class UpstreamUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_credential: str | None = None
    default_headers: dict[str, str] | None = None
    status: str | None = None


class RoutingPreferencePut(BaseModel):
    ordered_upstream_ids: list[str]


class GatewayTokenView(BaseModel):
    id: str
    status: str
    prefix: str
    last4: str
    issued_at: str
    successor_id: str | None = None
    predecessor_id: str | None = None
    last_used_at: str | None = None
    token: str | None = None


class CandidateSearchRequest(BaseModel):
    offering_id: str
    proposed_slug: str


class MaintenanceJobRequest(BaseModel):
    candidate_ids: list[str]


class PublicationRequest(BaseModel):
    candidate_ids: list[str]


class ProfileCreate(BaseModel):
    display_name: str
    codex_home: str
    expected_provider_id: str | None = None
    expected_base_url: str | None = None


class ProfilePreviewRequest(BaseModel):
    revision_id: str | None = None


class MarketplaceRegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    source: str
    default_enabled: bool = False


class PluginToggleRequest(BaseModel):
    plugin_id: str = Field(min_length=1, max_length=160)
    enabled: bool


class McpServerUpsertRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    command: str | None = None
    args: list[str] = Field(default_factory=list, max_length=64)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict, max_length=32)


class SkillCreateRequest(BaseModel):
    skill_id: str = Field(min_length=1, max_length=80)
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class ApplyRequest(BaseModel):
    revision_id: str
    fingerprint: dict[str, Any]


class ProblemDocument(BaseModel):
    type: str
    code: str
    title: str
    detail: str
    status: int
    instance: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
