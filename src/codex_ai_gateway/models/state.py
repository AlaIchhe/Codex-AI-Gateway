"""admin-state.json 文档模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from codex_ai_gateway.models.entities import (
    CanonicalModel,
    CatalogCandidate,
    CatalogEvidenceSet,
    CatalogRevision,
    ExternalMetadataSnapshot,
    GatewaySettings,
    GatewayToken,
    IntegrationProfile,
    IntegrationRevision,
    ManagementStateRevision,
    ModelIdentityMapping,
    Offering,
    PresetDiscoveryFailure,
    PresetDiscoveryState,
    PresetDocumentSnapshot,
    PriceSnapshot,
    ProtocolFixtureExecution,
    PublishedCatalogEntry,
    RoutingPreference,
    Upstream,
)

SCHEMA_VERSION = 2


class AdminState(BaseModel):
    schema_version: int = SCHEMA_VERSION
    settings: GatewaySettings = Field(default_factory=GatewaySettings)
    upstreams: list[Upstream] = Field(default_factory=list)
    offerings: list[Offering] = Field(default_factory=list)
    canonical_models: list[CanonicalModel] = Field(default_factory=list)
    model_mappings: list[ModelIdentityMapping] = Field(default_factory=list)
    routing_preferences: list[RoutingPreference] = Field(default_factory=list)
    gateway_tokens: list[GatewayToken] = Field(default_factory=list)
    catalog_candidates: list[CatalogCandidate] = Field(default_factory=list)
    catalog_evidence: list[CatalogEvidenceSet] = Field(default_factory=list)
    publications: list[PublishedCatalogEntry] = Field(default_factory=list)
    catalog_revisions: list[CatalogRevision] = Field(default_factory=list)
    openrouter_snapshots: list[ExternalMetadataSnapshot] = Field(default_factory=list)
    management_revisions: list[ManagementStateRevision] = Field(default_factory=list)
    protocol_fixture_executions: list[ProtocolFixtureExecution] = Field(default_factory=list)
    integration_profiles: list[IntegrationProfile] = Field(default_factory=list)
    integration_revisions: list[IntegrationRevision] = Field(default_factory=list)
    price_snapshots: list[PriceSnapshot] = Field(default_factory=list)
    preset_snapshots: list[PresetDocumentSnapshot] = Field(default_factory=list)
    preset_discovery_failures: list[PresetDiscoveryFailure] = Field(default_factory=list)
    preset_discovery_states: list[PresetDiscoveryState] = Field(default_factory=list)
