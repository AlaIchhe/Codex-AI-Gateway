"""核心实体与枚举。

遵循 specs/001-codex-ai-gateway/data-model.md 的字段规则。所有时间戳使用 UTC
ISO-8601；金额使用整数最小货币单位。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class WireProtocol(str, Enum):
    """上游 wire protocol。"""

    responses = "responses"
    chat_completions = "chat_completions"
    unconfirmed = "unconfirmed"


class UpstreamStatus(str, Enum):
    enabled = "enabled"
    disabled = "disabled"


class UpstreamKind(str, Enum):
    custom = "custom"
    preset = "preset"


class OfferingStatus(str, Enum):
    staged = "staged"
    approved = "approved"
    disabled = "disabled"


class TokenStatus(str, Enum):
    active = "active"
    grace = "grace"
    revoked = "revoked"


class RoutingScope(str, Enum):
    global_preference = "global"
    canonical_model = "canonical_model"


class MatchMode(str, Enum):
    exact = "exact"
    normalized = "normalized"
    date_version = "date_version"
    suffix_ignored = "suffix_ignored"
    ambiguous = "ambiguous"
    missing = "missing"
    upstream_fallback = "upstream_fallback"


class Outcome(str, Enum):
    completed = "completed"
    cancelled = "cancelled"
    failed = "failed"
    timed_out = "timed_out"
    interrupted = "interrupted"


class ReportingBasis(str, Enum):
    provider_reported = "provider_reported"
    estimated = "estimated"
    mixed = "mixed"
    none = "none"


class MappingStatus(str, Enum):
    automatic_confirmed = "automatic_confirmed"
    automatic_normalized = "automatic_normalized"
    date_matched = "date_matched"
    operator_confirmed = "operator_confirmed"
    ambiguous = "ambiguous"
    missing = "missing"


class SelectionResult(str, Enum):
    accepted = "accepted"
    rejected = "rejected"
    expired = "expired"


class VerificationStatus(str, Enum):
    complete = "complete"
    conflict = "conflict"
    ambiguous = "ambiguous"
    missing = "missing"
    untrusted = "untrusted"


class SourceKind(str, Enum):
    openrouter = "openrouter"
    upstream_native = "upstream_native"
    codex_schema_baseline = "codex_schema_baseline"


class SnapshotStatus(str, Enum):
    current = "current"
    expired = "expired"
    refresh_failed = "refresh_failed"


class CatalogRevisionStatus(str, Enum):
    published = "published"
    rolled_back = "rolled_back"


class IntegrationState(str, Enum):
    draft = "draft"
    preview_ready = "preview_ready"
    approved = "approved"
    applying = "applying"
    applied_validated = "applied_validated"
    rollback_required = "rollback_required"
    rolled_back = "rolled_back"
    drift_detected = "drift_detected"


class ProviderErrorType(str, Enum):
    authentication = "authentication"
    quota_budget = "quota_budget"
    rate_limit = "rate_limit"
    model_permission = "model_permission"
    upstream_fault = "upstream_fault"


class Upstream(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=80)
    status: UpstreamStatus = UpstreamStatus.enabled
    kind: UpstreamKind = UpstreamKind.custom
    preset_id: str | None = None
    preset_version: str | None = None
    base_url: str
    auth_credential_ref: str
    default_headers: dict[str, str] = Field(default_factory=dict)
    namespace_prefixes: set[str] = Field(default_factory=set)
    last_health_at: str | None = None
    last_health_result: str | None = None
    model_protocol_probe: dict[str, list[str]] = Field(default_factory=dict)
    cooldown_until: str | None = None
    created_at: str
    updated_at: str


class Offering(BaseModel):
    id: str
    upstream_id: str
    provider_model_id: str
    provider_version: str | None = None
    native_metadata_json: dict[str, Any] | None = None
    wire_protocol: WireProtocol
    display_name: str
    identity_evidence: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    status: OfferingStatus = OfferingStatus.staged
    canonical_model_id: str | None = None
    discovered_at: str
    updated_at: str


class CanonicalModel(BaseModel):
    id: str
    openrouter_model_id: str | None = None
    display_name: str
    slug: str
    capability_baseline: dict[str, Any] = Field(default_factory=dict)
    status: str = "unavailable"
    first_matched_at: str
    updated_at: str


class ModelIdentityMapping(BaseModel):
    id: str
    offering_id: str
    openrouter_model_id: str | None = None
    match_mode: MatchMode
    normalized_key: str
    family_key: str | None = None
    provider_date: str | None = None
    openrouter_date: str | None = None
    ignored_suffix: str | None = None
    evidence_json: dict[str, Any] = Field(default_factory=dict)
    matched_at: str


class RoutingPreference(BaseModel):
    id: str
    scope: RoutingScope
    canonical_model_id: str | None = None
    ordered_upstream_ids: list[str] = Field(default_factory=list)
    updated_at: str


class GatewayToken(BaseModel):
    id: str
    lookup_hash: str
    prefix: str
    last4: str
    status: TokenStatus = TokenStatus.active
    issued_at: str
    grace_until: str | None = None
    successor_id: str | None = None
    predecessor_id: str | None = None
    last_used_at: str | None = None


class ExternalMetadataSnapshot(BaseModel):
    models_json: list[dict[str, Any]] = Field(default_factory=list)
    id: str
    source_url: str
    response_version: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    body_sha256: str
    headers_digest: str
    fetched_at: str
    expires_at: str
    filter_rule_version: str = "identity-lexical-v1"
    status: SnapshotStatus = SnapshotStatus.current


class CatalogCandidate(BaseModel):
    id: str
    offering_id: str
    upstream_id: str
    proposed_alias_slug: str
    openrouter_model_id: str | None = None
    openrouter_version_id: str | None = None
    openrouter_snapshot_id: str | None = None
    mapping_status: MappingStatus = MappingStatus.missing
    selection_result: SelectionResult = SelectionResult.rejected
    rejection_reason: str | None = None
    public_snapshot_url: str | None = None
    public_snapshot_version: str | None = None
    public_snapshot_time: str | None = None
    native_upstream_metadata_time: str | None = None
    lineage_hash: str | None = None
    created_at: str
    updated_at: str


class CatalogFieldEvidence(BaseModel):
    candidate_id: str
    id: str
    field_path: str
    source_rank: int = 0
    source_kind: SourceKind
    source_reference: dict[str, Any] = Field(default_factory=dict)
    observed_value: Any = None
    verification_status: VerificationStatus
    resolution_reason: str | None = None
    advice: str | None = None
    observed_at: str


class CatalogEvidenceSet(BaseModel):
    candidate_id: str
    fields: list[CatalogFieldEvidence] = Field(default_factory=list)


class PublishedCatalogEntry(BaseModel):
    id: str
    offering_id: str
    revision: int
    version_hash: str
    model_info_json: dict[str, Any]
    field_sources_json: dict[str, Any] = Field(default_factory=dict)
    accepted_at: str
    valid_until: str | None = None
    approval_evidence: dict[str, Any] = Field(default_factory=dict)
    generation_context: dict[str, Any] = Field(default_factory=dict)


class CatalogRevision(BaseModel):
    id: str
    parent_id: str | None = None
    trigger: str
    openrouter_snapshot_id: str | None = None
    entry_ids: list[str] = Field(default_factory=list)
    models_response_hash: str
    diff_summary: dict[str, Any] = Field(default_factory=dict)
    recovery_point_id: str | None = None
    status: CatalogRevisionStatus = CatalogRevisionStatus.published
    created_at: str
    published_at: str


class IntegrationProfile(BaseModel):
    id: str
    display_name: str
    codex_home: str
    config_path: str | None = None
    profile_path: str | None = None
    catalog_path: str | None = None
    expected_provider_id: str | None = None
    expected_base_url: str | None = None
    selected_group_id: str | None = None
    default_catalog_strategy: str = "published"
    auto_maintenance: bool = True
    schema_contract_version: str | None = None
    docs_refs: dict[str, Any] = Field(default_factory=dict)
    drift_events: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str
    updated_at: str


class IntegrationFailures(BaseModel):
    schema_validation: str | None = None
    connectivity_check: str | None = None
    startup_config_check: str | None = None
    rollback_result: str | None = None


class IntegrationRevision(BaseModel):
    id: str
    profile_id: str
    state: IntegrationState = IntegrationState.draft
    preview_diff_json: dict[str, Any] = Field(default_factory=dict)
    pre_fingerprint: dict[str, Any] = Field(default_factory=dict)
    post_fingerprint: dict[str, Any] = Field(default_factory=dict)
    recovery_point_path: str | None = None
    validation_events: list[dict[str, Any]] = Field(default_factory=list)
    failures: IntegrationFailures | None = None
    completed_at: str | None = None
    created_at: str
    updated_at: str


class ManagementStateRevision(BaseModel):
    id: str
    trigger: str
    state_hash: str
    changed_paths: list[str] = Field(default_factory=list)
    accepted_at: str


class ProtocolFixtureExecution(BaseModel):
    id: str
    canonical_model_id: str
    offering_id: str
    upstream_id: str
    fixture_id: str
    protocol: WireProtocol
    client_request_id: str | None = None
    attempt_id: str | None = None
    outcome: Outcome
    error_mapping_code: str | None = None
    started_at: str
    duration_ms: int | None = None
    usage_reporting_basis: ReportingBasis = ReportingBasis.none


class UsageEvent(BaseModel):
    id: str
    client_request_id: str
    started_at: str
    duration_ms: int | None = None
    first_byte_ms: int | None = None
    cancelled_at: str | None = None
    upstream_id: str | None = None
    upstream_label: str | None = None
    offering_id: str | None = None
    canonical_model_id: str | None = None
    canonical_model_label: str | None = None
    provider_model_id: str | None = None
    inbound_protocol: WireProtocol | None = None
    outbound_protocol: WireProtocol | None = None
    outcome: Outcome
    http_upstream_status: int | None = None
    provider_error_type: ProviderErrorType | None = None
    error_mapping_code: str | None = None
    attempt_ordinal: int = 1
    fallback_trigger: str | None = None
    reporting_basis: ReportingBasis = ReportingBasis.estimated
    token_usage_by_category: dict[str, Any] = Field(default_factory=dict)
    price_snapshot_id: str | None = None
    cost_minor_units: int | None = None
    currency: str | None = None
    output_summary_count: int | None = None


class PriceSnapshot(BaseModel):
    id: str
    offering_id: str
    openrouter_version: str | None = None
    pricing_units: dict[str, Any] = Field(default_factory=dict)
    source_url: str | None = None
    fetched_at: str
    effective_from: str | None = None
    is_estimated_basis: bool = True


class PresetDocumentSnapshot(BaseModel):
    id: str
    preset_id: str
    upstream_id: str
    source_url: str
    http_status: int | None = None
    content_type: str | None = None
    final_url: str | None = None
    body_sha256: str
    body_size: int
    extractor_key: str
    extractor_version: str
    model_ids: list[str] = Field(default_factory=list)
    evidence_json: dict[str, Any] = Field(default_factory=dict)
    fetched_at: str


class PresetDiscoveryFailure(BaseModel):
    id: str
    preset_id: str
    upstream_id: str
    source_url: str
    extractor_key: str
    extractor_version: str
    failure_code: str
    failure_message: str
    http_status: int | None = None
    content_type: str | None = None
    final_url: str | None = None
    body_sha256: str | None = None
    body_size: int | None = None
    evidence_json: dict[str, Any] = Field(default_factory=dict)
    occurred_at: str


class PresetDiscoveryState(BaseModel):
    id: str
    preset_id: str
    upstream_id: str
    status: Literal["never", "succeeded", "failed"] = "never"
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None
    latest_snapshot_id: str | None = None
    latest_failure_id: str | None = None
    current_model_count: int = 0


class GatewaySettings(BaseModel):
    codex_auto_integration_enabled: bool = True
    usage_retention_days: int = Field(default=30, ge=1)
    debug_capture_enabled: bool = False
    debug_capture_limited: bool = False
    listen_hint: str = "0.0.0.0:8787"
