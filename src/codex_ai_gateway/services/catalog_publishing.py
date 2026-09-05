"""目录元数据维护：逐字段证据评估、自动维护与发布构建器。"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from codex_ai_gateway.integrations.openrouter_metadata import (
    extract_model_metadata,
    find_identity_candidate,
    search_models,
)
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
    VerificationStatus,
    WireProtocol,
)
from codex_ai_gateway.persistence.atomic_writer import atomic_write_json, ensure_secure_dir
from codex_ai_gateway.util import utc_now, uuid7


def _safe_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _text_modality(value: Any) -> list[str]:
    return [str(item) for item in value if isinstance(item, str | int)] if isinstance(value, list) else []


def _reasoning_levels(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, dict):
        # 空 dict 无证据（按既有契约交付为缺失）；非空 dict 但缺少等级信息时，
        # 只要未明确禁用推理，按 FR-018 补全语义默认给出 medium。
        if not value:
            return []
        if value.get("enabled") is False or value.get("supported") is False:
            return []
        efforts = value.get("supported_efforts")
        if isinstance(efforts, list) and efforts:
            return [str(item) for item in efforts]
        effort = value.get("default_effort") or value.get("reasoning")
        return [str(effort)] if (effort is not None and str(effort)) else ["medium"]
    if isinstance(value, str) and value:
        return [value]
    return []


def reconcile_metadata(openrouter: dict[str, Any], native: dict[str, Any] | None) -> dict[str, Any]:
    """按 FR-016 对双源元数据做保守合并；冲突输出为可能被剔除的值。"""
    merged = dict(openrouter)
    if not isinstance(native, dict):
        return merged
    contexts = [v for v in (openrouter.get("context_window"), native.get("context_window")) if _safe_positive_int(v) is not None]
    if contexts:
        merged["context_window"] = min(_safe_positive_int(v) for v in contexts)
    for key in ("input_modality", "output_modality"):
        values = [_text_modality(item.get(key)) for item in (openrouter, native) if isinstance(item, dict) and item.get(key) is not None]
        if values:
            intersection = set(values[0]).intersection(*values[1:]) if len(values) > 1 else set(values[0])
            merged[key] = sorted(intersection)
    param_sets = [[str(v) for v in item.get("supported_parameters") or []] for item in (openrouter, native) if isinstance(item, dict) and item.get("supported_parameters") is not None]
    if param_sets:
        intersection = set(param_sets[0]).intersection(*param_sets[1:]) if len(param_sets) > 1 else set(param_sets[0])
        merged["supported_parameters"] = sorted(intersection)
    native_reasoning = native.get("reasoning", native.get("reasoning_levels"))
    if native_reasoning is not None:
        openrouter_levels = set(_reasoning_levels(openrouter.get("reasoning")))
        native_levels = set(_reasoning_levels(native_reasoning))
        merged["reasoning"] = {"supported_efforts": sorted(openrouter_levels & native_levels)}
    return merged

REQUIRED_FIELDS = [
    "context_window",
    "input_modality",
    "output_modality",
    "tools_supported",
    "tool_choice_supported",
    "structured_output",
    "reasoning_levels",
]


class CatalogPublishingService:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.publications_dir = self.data_dir / "catalog/publications"

    async def run_maintenance(
        self,
        candidates: list[CatalogCandidate],
        offerings: list[Offering],
        upstreams: list[Upstream],
        *,
        existing_evidence: list[CatalogEvidenceSet] | None = None,
    ) -> list[CatalogEvidenceSet]:
        """对每个候选执行字段级评估；已有完整证据作为无网络维护输入。"""
        evidence_by_candidate = {
            item.candidate_id: item for item in (existing_evidence or [])
        }
        evidence_sets: list[CatalogEvidenceSet] = []
        for candidate in candidates:
            offering = next(
                (o for o in offerings if o.id == candidate.offering_id), None
            )
            if offering is None:
                continue
            old_evidence = evidence_by_candidate.get(candidate.id)
            metadata = _metadata_from_evidence(old_evidence.fields) if old_evidence else {}
            fields = evaluate_fields(candidate, metadata)
            # structured_output 是可选能力（缺失仍可正常使用模型），
            # 不作为拒绝条件；其余字段缺失仍拒绝（FR-018 补全语义）。
            accepted = all(
                field.verification_status == VerificationStatus.complete
                for field in fields
                if field.field_path != "structured_output"
            )
            candidate.selection_result = (
                SelectionResult.accepted if accepted else SelectionResult.rejected
            )
            candidate.mapping_status = (
                MappingStatus.automatic_confirmed
                if accepted
                or any(
                    field.verification_status == VerificationStatus.complete
                    for field in fields
                )
                else MappingStatus.missing
            )
            candidate.rejection_reason = _rejection_reason(fields)
            candidate.updated_at = utc_now()
            evidence_sets.append(
                CatalogEvidenceSet(candidate_id=candidate.id, fields=fields)
            )
        return evidence_sets

    async def build_publication(
        self,
        candidates: list[CatalogCandidate],
        evidence_sets: list[CatalogEvidenceSet],
        offering: Offering,
    ) -> PublishedCatalogEntry:
        """从 accepted candidates 构建版本，schema 校验后写独立 JSON 资产。"""
        accepted = [
            candidate
            for candidate in candidates
            if candidate.selection_result == SelectionResult.accepted
            and candidate.offering_id == offering.id
        ]
        if not accepted:
            raise ValueError("没有可发布的 accepted 候选")
        accepted_ids = {candidate.id for candidate in accepted}
        field_sources: dict[str, Any] = {}
        normalized_sets: list[CatalogEvidenceSet] = []
        for evidence_item in evidence_sets:
            normalized_sets.append(
                evidence_item
                if isinstance(evidence_item, CatalogEvidenceSet)
                else CatalogEvidenceSet(candidate_id=accepted[0].id, fields=evidence_item)
            )
        evidence_sets = normalized_sets
        for evidence_set in evidence_sets:
            candidate_id = (
                evidence_set.candidate_id
                if isinstance(evidence_set, CatalogEvidenceSet)
                else accepted[0].id
            )
            if candidate_id not in accepted_ids:
                continue
            fields = (
                evidence_set.fields
                if isinstance(evidence_set, CatalogEvidenceSet)
                else evidence_set
            )
            for field in fields:
                field_sources[field.field_path] = field.source_kind.value
        evidence_set = next((item for item in evidence_sets if item.candidate_id == accepted[0].id), None)
        model_info = _build_model_info(accepted[0], evidence_set.fields if evidence_set else [])
        version_hash = _version_hash(model_info, field_sources)
        entry = PublishedCatalogEntry(
            id=uuid7(),
            offering_id=offering.id,
            revision=1,
            version_hash=version_hash,
            model_info_json=model_info,
            field_sources_json=field_sources,
            accepted_at=utc_now(),
            approval_evidence={"approval": "automatic_policy", "manual_review": False},
            generation_context={"schema": "codex-model-catalog-v1"},
        )
        ensure_secure_dir(self.publications_dir)
        atomic_write_json(
            self.publications_dir / f"{entry.id}.json",
            entry.model_dump(mode="json"),
        )
        revisions_dir = self.data_dir / "catalog/revisions"
        ensure_secure_dir(revisions_dir)
        previous: list[CatalogRevision] = []
        for path in revisions_dir.glob("*.json"):
            try:
                previous.append(CatalogRevision.model_validate_json(path.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001 - 损坏历史不影响新版本
                continue
        parent_id = max(previous, key=lambda item: item.created_at).id if previous else None
        revision = CatalogRevision(
            id=uuid7(),
            parent_id=parent_id,
            trigger="model_change",
            entry_ids=[entry.id],
            models_response_hash=version_hash,
            diff_summary={"added": [entry.model_info_json.get("slug")], "updated": [], "removed": []},
            status=CatalogRevisionStatus.published,
            created_at=entry.accepted_at,
            published_at=entry.accepted_at,
        )
        atomic_write_json(revisions_dir / f"{revision.id}.json", revision.model_dump(mode="json"))
        for stale in set(item.id for item in previous) - set(item.id for item in _retained_catalog_revisions(self.data_dir)):
            (revisions_dir / f"{stale}.json").unlink(missing_ok=True)
        return entry


def _catalog_revisions(data_dir: Path) -> list[CatalogRevision]:
    revisions_dir = Path(data_dir) / "catalog/revisions"
    if not revisions_dir.exists():
        return []
    revisions: list[CatalogRevision] = []
    for path in revisions_dir.glob("*.json"):
        try:
            revisions.append(CatalogRevision.model_validate_json(path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 - 损坏历史不阻断查询
            continue
    return sorted(revisions, key=lambda item: item.created_at)


def _retained_catalog_revisions(data_dir: Path) -> list[CatalogRevision]:
    revisions = _catalog_revisions(data_dir)
    cutoff = datetime.now(UTC) - timedelta(days=90)
    by_age = [item for item in revisions if datetime.fromisoformat(item.created_at) >= cutoff]
    return by_age if len(by_age) >= 100 else revisions[-100:]


def list_catalog_revisions(data_dir: Path) -> list[CatalogRevision]:
    return _catalog_revisions(data_dir)


def get_catalog_revision(data_dir: Path, revision_id: str) -> CatalogRevision:
    revision = next((item for item in _catalog_revisions(data_dir) if item.id == revision_id), None)
    if revision is None:
        raise ValueError("目录版本不存在")
    return revision


def _revision_entries(data_dir: Path, revision: CatalogRevision) -> list[PublishedCatalogEntry]:
    entries: list[PublishedCatalogEntry] = []
    for entry_id in revision.entry_ids:
        path = Path(data_dir) / "catalog/publications" / f"{entry_id}.json"
        if path.exists():
            entries.append(PublishedCatalogEntry.model_validate_json(path.read_text(encoding="utf-8")))
    return entries


def _model_info_slug(entry: PublishedCatalogEntry) -> str:
    return str(entry.model_info_json.get("slug") or "")


def diff_catalog_revisions(data_dir: Path, left_id: str, right_id: str | None = None) -> dict[str, Any]:
    revisions = _catalog_revisions(data_dir)
    left = next((item for item in revisions if item.id == left_id), None)
    if left is None:
        raise ValueError("左侧目录版本不存在")
    if right_id is None:
        right = revisions[-1] if revisions else None
    else:
        right = next((item for item in revisions if item.id == right_id), None)
    if right is None:
        raise ValueError("右侧目录版本不存在")
    left_entries = _revision_entries(data_dir, left)
    right_entries = _revision_entries(data_dir, right)
    left_by_slug = {_model_info_slug(item): item.model_info_json for item in left_entries}
    right_by_slug = {_model_info_slug(item): item.model_info_json for item in right_entries}
    return {
        "left_revision_id": left.id,
        "right_revision_id": right.id,
        "added": [{"slug": slug, "model_info": right_by_slug[slug]} for slug in sorted(right_by_slug.keys() - left_by_slug.keys())],
        "removed": [{"slug": slug, "model_info": left_by_slug[slug]} for slug in sorted(left_by_slug.keys() - right_by_slug.keys())],
        "updated": [
            {
                "slug": slug,
                "before": left_by_slug[slug],
                "after": right_by_slug[slug],
            }
            for slug in sorted(left_by_slug.keys() & right_by_slug.keys())
            if left_by_slug[slug] != right_by_slug[slug]
        ],
    }


def _rollback_offering(entry: PublishedCatalogEntry) -> Offering:
    return Offering(
        id=entry.offering_id,
        upstream_id="rollback",
        provider_model_id=str(entry.model_info_json.get("slug") or "rollback"),
        wire_protocol=WireProtocol.responses,
        display_name=str(entry.model_info_json.get("name") or "rollback"),
        status=OfferingStatus.approved,
        discovered_at=utc_now(),
        updated_at=utc_now(),
    )


async def rollback_catalog_revision(data_dir: Path, target_revision_id: str) -> CatalogRevision:
    """以目标版本内容创建新版本；历史不可篡改。"""
    target = get_catalog_revision(data_dir, target_revision_id)
    target_entries = _revision_entries(data_dir, target)
    if not target_entries:
        raise ValueError("目标目录版本没有可回退的发布资产")
    recovery_dir = Path(data_dir) / "catalog/recovery-points"
    ensure_secure_dir(recovery_dir)
    recovery_point_id = uuid7()
    atomic_write_json(
        recovery_dir / f"{recovery_point_id}.json",
        {"entries": [item.model_dump(mode="json") for item in load_published_model_infos(data_dir)]},
    )
    service = CatalogPublishingService(data_dir)
    entry = await service.build_publication(
        [_rollback_candidate(target_entries[0])],
        [CatalogEvidenceSet(candidate_id=target_entries[0].id, fields=[])],
        _rollback_offering(target_entries[0]),
    )
    revisions = _catalog_revisions(data_dir)
    latest = next(item for item in reversed(revisions) if item.id != target.id)
    recovery_revision = latest.model_copy(update={
        "id": uuid7(),
        "parent_id": latest.id,
        "trigger": "catalog_rollback",
        "entry_ids": [entry.id],
        "models_response_hash": entry.version_hash,
        "diff_summary": {"rollback_to": target.id, "recovery_point_id": recovery_point_id},
        "status": CatalogRevisionStatus.rolled_back,
        "created_at": entry.accepted_at,
        "published_at": entry.accepted_at,
    })
    atomic_write_json(Path(data_dir) / "catalog/revisions" / f"{recovery_revision.id}.json", recovery_revision.model_dump(mode="json"))
    return recovery_revision


def _rollback_candidate(entry: PublishedCatalogEntry) -> CatalogCandidate:
    now = utc_now()
    return CatalogCandidate(
        id=entry.id,
        offering_id=entry.offering_id,
        upstream_id="rollback",
        proposed_alias_slug=str(entry.model_info_json.get("slug") or ""),
        openrouter_model_id=None,
        mapping_status=MappingStatus.automatic_confirmed,
        selection_result=SelectionResult.accepted,
        created_at=now,
        updated_at=now,
    )


async def run_catalog_automation(runtime: Any) -> dict[str, Any]:
    """offering 注册或探测后的全自动目录维护：筛选通过即发布。"""
    state = runtime.state_store.read_state()
    service = CatalogPublishingService(runtime.data_dir)
    offerings = list(state.offerings)
    offering_ids = {item.id for item in offerings}
    grouped_candidates: dict[str, list[CatalogCandidate]] = {}
    for candidate in state.catalog_candidates:
        if candidate.offering_id in offering_ids:
            grouped_candidates.setdefault(candidate.offering_id, []).append(candidate)

    # 同一 offering 保留最新且优先带公开身份的候选，避免历史探针累积重复项。
    candidates: list[CatalogCandidate] = []
    for group in grouped_candidates.values():
        candidates.append(
            max(
                group,
                key=lambda item: (
                    item.openrouter_model_id is not None,
                    item.created_at,
                ),
            )
        )
    kept_candidate_ids = {item.id for item in candidates}
    runtime.state_store.mutate(
        lambda state: (
            setattr(state, "catalog_candidates", candidates),
            setattr(
                state,
                "catalog_evidence",
                [
                    item
                    for item in state.catalog_evidence
                    if item.candidate_id in kept_candidate_ids
                ],
            ),
        )[-1]
    )

    # 自动为 approved offering 建立候选；失败原因在维护作业中逐字段补全。
    existing_offerings = {candidate.offering_id for candidate in candidates}
    now = utc_now()
    for offering in offerings:
        if offering.status != OfferingStatus.approved:
            continue
        if offering.id in existing_offerings:
            continue
        candidates.append(
            CatalogCandidate(
                id=uuid7(),
                offering_id=offering.id,
                upstream_id=offering.upstream_id,
                proposed_alias_slug=offering.provider_model_id.lower().replace(".", "-"),
                mapping_status=MappingStatus.missing,
                selection_result=SelectionResult.rejected,
                rejection_reason="缺少公开元数据",
                created_at=now,
                updated_at=now,
            )
        )

    snapshot_id = next(
        (item.id for item in reversed(getattr(state, "openrouter_snapshots", []) ) if item.status.value == "current"),
        None,
    )
    # 为缺少公开身份的候选自动获取 OpenRouter 元数据；相同 provider model 只查一次。
    offering_by_id = {item.id: item for item in offerings}
    provider_ids = {
        offering_by_id[candidate.offering_id].provider_model_id
        for candidate in candidates
        if candidate.openrouter_model_id is None and candidate.offering_id in offering_by_id
    }
    identities: dict[str, dict[str, Any]] = {}
    metadata_by_candidate: dict[str, dict[str, Any]] = {}
    # 并发查询全部待映射模型，避免 N 个 20 秒超时串行放大探测延迟。
    search_results = await asyncio.gather(
        *[search_models(query=provider_model_id) for provider_model_id in sorted(provider_ids)]
    )
    for provider_model_id, models in zip(sorted(provider_ids), search_results, strict=True):
        identity = find_identity_candidate(
            models,
            provider_model_id=provider_model_id,
        )
        if identity is not None:
            identities[provider_model_id] = identity

    # 搜索未命中时回退到聚合身份匹配（含预设别名与变体过滤），
    # 使 doubao-seed 等私有命名的模型也能获得公开身份。
    # 使用最新本地快照，避免重复网络拉取；快照为空时跳过回退。
    unfound = [pid for pid in provider_ids if pid not in identities]
    if unfound:
        from codex_ai_gateway.services.model_identity import match_offering

        snapshots = sorted(getattr(state, "openrouter_snapshots", []), key=lambda item: item.fetched_at)
        if snapshots:
            snapshot_models = snapshots[-1].models_json or []
            upstream_by_id = {item.id: item for item in getattr(state, "upstreams", [])}
            for candidate in candidates:
                offering = offering_by_id.get(candidate.offering_id)
                if offering is None or offering.provider_model_id not in unfound:
                    continue
                upstream = upstream_by_id.get(offering.upstream_id)
                aliases: dict[str, str] = {}
                if upstream is not None and getattr(upstream, "kind", None) == "preset" and upstream.preset_id:
                    try:
                        from codex_ai_gateway.services.presets import get_preset_provider

                        aliases = get_preset_provider(upstream.preset_id).identity_aliases
                    except KeyError:
                        pass
                result = match_offering(
                    offering,
                    snapshot_models,
                    namespace_prefixes=set(getattr(upstream, "namespace_prefixes", set()) or set()),
                    snapshot_id=snapshot_id,
                    aliases=aliases or None,
                )
                if result.candidate is not None:
                    identities[offering.provider_model_id] = result.candidate

    for candidate in candidates:
        offering = offering_by_id.get(candidate.offering_id)
        if offering is None:
            continue
        identity = identities.get(offering.provider_model_id)
        if identity is None:
            continue
        metadata = reconcile_metadata(
            extract_model_metadata(identity),
            offering.native_metadata_json,
        )
        metadata_by_candidate[candidate.id] = metadata
        candidate.openrouter_model_id = identity.get("id")
        candidate.openrouter_snapshot_id = snapshot_id
        canonical_slug = _official_slug(identity)
        if canonical_slug:
            candidate.proposed_alias_slug = canonical_slug
        candidate.openrouter_version_id = metadata.get("version_id")
        candidate.mapping_status = MappingStatus.automatic_confirmed
        candidate.public_snapshot_url = "https://openrouter.ai/api/v1/models"
        candidate.public_snapshot_version = metadata.get("version_id")
        candidate.public_snapshot_time = utc_now()

    evidence_input = list(state.catalog_evidence)
    for candidate_id, metadata in metadata_by_candidate.items():
        candidate = next(item for item in candidates if item.id == candidate_id)
        evidence_input.append(
            CatalogEvidenceSet(
                candidate_id=candidate_id,
                fields=evaluate_fields(candidate, metadata),
            )
        )

    evidence_sets = await service.run_maintenance(
        candidates,
        offerings,
        list(state.upstreams),
        existing_evidence=evidence_input,
    )
    accepted_ids = [
        candidate.id
        for candidate in candidates
        if candidate.selection_result == SelectionResult.accepted
    ]

    def apply_results(state: Any) -> None:
        for candidate in candidates:
            for index, existing in enumerate(state.catalog_candidates):
                if existing.id == candidate.id:
                    state.catalog_candidates[index] = candidate
                    break
            else:
                state.catalog_candidates.append(candidate)
        for evidence_set in evidence_sets:
            state.catalog_evidence = [
                item
                for item in state.catalog_evidence
                if item.candidate_id != evidence_set.candidate_id
            ]
            state.catalog_evidence.append(evidence_set)

    runtime.state_store.mutate(apply_results)

    published_ids: list[str] = []
    if accepted_ids:
        refreshed = runtime.state_store.read_state()
        offering_ids = {
            candidate.offering_id
            for candidate in refreshed.catalog_candidates
            if candidate.id in accepted_ids
        }
        for offering_id in offering_ids:
            group_candidates = [
                candidate
                for candidate in refreshed.catalog_candidates
                if candidate.offering_id == offering_id
                and candidate.selection_result == SelectionResult.accepted
            ]
            group_evidence = [
                evidence
                for evidence in refreshed.catalog_evidence
                if evidence.candidate_id in {candidate.id for candidate in group_candidates}
            ]
            offering = next(
                (item for item in refreshed.offerings if item.id == offering_id), None
            )
            if offering is None:
                continue
            entry = await service.build_publication(
                group_candidates, group_evidence, offering
            )
            def publish(state, entry=entry, entry_id=None):
                state.publications.append(entry)
                revision = next((item for item in list_catalog_revisions(runtime.data_dir) if entry.id in item.entry_ids), None)
                if revision and not any(item.id == revision.id for item in getattr(state, "catalog_revisions", [])):
                    if not hasattr(state, "catalog_revisions"):
                        state.catalog_revisions = []
                    state.catalog_revisions.append(revision)
            runtime.state_store.mutate(publish)
            published_ids.append(entry.id)

    return {
        "status": "complete",
        "candidates": len(candidates),
        "accepted": len(accepted_ids),
        "published": published_ids,
    }


def _official_slug(identity: dict[str, Any]) -> str | None:
    """从 OpenRouter canonical_slug 提取去除日期后缀的官方 slug。

    支持 YYYYMMDD（8 位）与 MM-DD（5 位，如 qwen3.6-plus-04-02）两类日期后缀；
    MM-DD 需通过月份/日期范围校验，避免误伤真实版本号。
    """
    import re

    value = identity.get("canonical_slug")
    if not isinstance(value, str) or "/" not in value:
        return None
    slug = value.rsplit("/", 1)[-1]
    # YYYYMMDD 或 MM-DD 结尾
    date_full = re.compile(r"^(?P<base>.+)-(?P<date>\d{8})$")
    date_short = re.compile(r"^(?P<base>.+)-(?P<mm>\d{2})-(?P<dd>\d{2})$")
    match = date_full.match(slug)
    if match:
        return match.group("base") or None
    match = date_short.match(slug)
    if match:
        mm, dd = int(match.group("mm")), int(match.group("dd"))
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            return match.group("base") or None
    return slug or None


def _metadata_from_evidence(fields: list[CatalogFieldEvidence]) -> dict[str, Any]:
    values = {
        field.field_path: field.observed_value
        for field in fields
        if field.verification_status == VerificationStatus.complete
    }
    return {
        "context_window": values.get("context_window"),
        "input_modality": values.get("input_modality") or [],
        "output_modality": values.get("output_modality") or [],
        "supported_parameters": [
            name
            for name, value in (
                ("tools", values.get("tools_supported")),
                ("tool_choice", values.get("tool_choice_supported")),
                ("structured_outputs", values.get("structured_output")),
            )
            if value is True
        ],
        "reasoning": values.get("reasoning_levels"),
    }


def _rejection_reason(fields: list[CatalogFieldEvidence]) -> str | None:
    failed = [
        field
        for field in fields
        if field.verification_status != VerificationStatus.complete
    ]
    if not failed:
        return None
    return "; ".join(
        f"{field.field_path}: {field.advice or '证据不完整'}" for field in failed
    )


def evaluate_fields(
    candidate: CatalogCandidate, metadata: dict[str, Any]
) -> list[CatalogFieldEvidence]:
    """逐字段评估必要属性，生成 evidence。"""
    now = utc_now()
    fields: list[CatalogFieldEvidence] = []

    def add(
        field_path: str,
        value: Any,
        status: VerificationStatus,
        advice: str | None = None,
        source: SourceKind = SourceKind.openrouter,
    ) -> None:
        fields.append(
            CatalogFieldEvidence(
                candidate_id=candidate.id,
                id=uuid7(),
                field_path=field_path,
                source_kind=source,
                observed_value=value,
                verification_status=status,
                advice=advice,
                observed_at=now,
            )
        )

    context = metadata.get("context_window")
    add(
        "context_window",
        context,
        VerificationStatus.complete
        if isinstance(context, int) and context > 0
        else VerificationStatus.missing,
        "上下文窗口必须是正整数",
    )
    input_modality = metadata.get("input_modality") or []
    output_modality = metadata.get("output_modality") or []
    add(
        "input_modality",
        input_modality,
        VerificationStatus.complete if "text" in input_modality else VerificationStatus.missing,
        "输入模态必须包含 text",
    )
    add(
        "output_modality",
        output_modality,
        VerificationStatus.complete if "text" in output_modality else VerificationStatus.missing,
        "输出模态必须包含 text",
    )
    supported = metadata.get("supported_parameters") or []
    tools = "tools" in supported
    tool_choice = "tool_choice" in supported
    add("tools_supported", tools, VerificationStatus.complete if tools else VerificationStatus.missing, "必须支持 tools")
    add("tool_choice_supported", tool_choice, VerificationStatus.complete if tool_choice else VerificationStatus.missing, "必须支持 tool_choice")
    structured = "structured_outputs" in supported
    add("structured_output", structured, VerificationStatus.complete if structured else VerificationStatus.missing, "缺少 structured output 证据")
    reasoning = metadata.get("reasoning")
    # 非空 dict 视为存在推理上下文（含 mandatory=False 等可选推理声明），
    # 按 FR-018 补全语义给出默认等级；空 dict / None / 空值才视为证据缺失。
    has_reasoning = isinstance(reasoning, dict) and bool(reasoning) and bool(
        _reasoning_levels(reasoning)
    )
    add(
        "reasoning_levels",
        reasoning if has_reasoning else reasoning,
        VerificationStatus.complete if has_reasoning else VerificationStatus.missing,
        "思考等级必须给出有效证据，不能把空值解释为不支持推理",
    )
    return fields


def _build_model_info(
    candidate: CatalogCandidate, evidence: list[CatalogFieldEvidence] | None = None
) -> dict[str, Any]:
    if candidate.openrouter_model_id is None:
        raise ValueError("候选缺少 openrouter_model_id")
    metadata = _metadata_from_evidence(evidence or [])
    context_window = _safe_positive_int(metadata.get("context_window"))
    if context_window is None:
        raise ValueError(f"候选 {candidate.id} 缺少有效的 context_window")
    reasoning_levels = _reasoning_levels(metadata.get("reasoning")) or ["medium"]
    # 默认等级优先取上游(default_effort)，缺失时用列表首个；补全场景无 default_effort 时用列表首个。
    reasoning_raw = metadata.get("reasoning")
    default_effort = None
    if isinstance(reasoning_raw, dict):
        default_effort = reasoning_raw.get("default_effort")
    reasoning_effort = (
        str(default_effort) if default_effort is not None and str(default_effort)
        else reasoning_levels[0]
    )
    return {
        "slug": candidate.proposed_alias_slug,
        "name": candidate.proposed_alias_slug,
        "model_id": candidate.openrouter_model_id,
        "context_window": context_window,
        "reasoning_levels": reasoning_levels,
        "reasoning_effort": reasoning_effort,
        "input_modalities": _codex_supported_modalities(metadata.get("input_modality") or ["text"]),
        "output_modalities": _codex_supported_modalities(metadata.get("output_modality") or ["text"]),
        "tools": bool(metadata.get("tools_supported")),
        "tool_choice": bool(metadata.get("tool_choice_supported")),
        "structured_output": bool(metadata.get("structured_output")),
    }


def _version_hash(model_info: dict[str, Any], field_sources: dict[str, Any]) -> str:
    blob = f"{model_info}|{field_sources}".encode()
    return hashlib.sha256(blob).hexdigest()


async def fetch_upstream_metadata(
    upstream: Upstream, api_key: str, model_id: str
) -> dict[str, Any] | None:
    """查询上游原生元数据（不发送付费生成请求）。"""
    base = upstream.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{base}/models/{model_id}", headers=headers)
            if resp.status_code >= 400:
                return None
            return resp.json()
    except (httpx.HTTPError, ValueError):
        return None


# ---------------------------------------------------------------------------
# FR-044: 官方 Codex `model_catalog_json` 目录内容生成器
#
# 官方契约出处（openai/codex 仓库）：
# - codex-rs/protocol/src/openai_models.rs: ModelInfo/ModelsResponse schema
# - codex-rs/core/src/config/mod.rs: load_catalog_json（models 非空校验）
# - codex-rs/models-manager/models.json: 内置目录字段参考
# 官方 ModelInfo 无 provider/base_url/env_key 字段；端点与认证由
# config.toml 受管 model_provider 承担，目录条目不承载路由或凭据。
# ---------------------------------------------------------------------------

OFFICIAL_MODEL_INFO_REQUIRED = [
    "slug",
    "display_name",
    "supported_reasoning_levels",
    "shell_type",
    "visibility",
    "supported_in_api",
    "priority",
    "support_verbosity",
    "truncation_policy",
    "additional_speed_tiers",
    "availability_nux",
    "default_reasoning_summary",
    "effective_context_window_percent",
    "max_context_window",
    "service_tiers",
    "supports_image_detail_original",
    "supports_parallel_tool_calls",
    "supports_reasoning_summaries",
    "supports_search_tool",
    "upgrade",
]

OFFICIAL_MODEL_INFO_FORBIDDEN = {"provider", "base_url", "env_key", "id", "name", "model_id"}

DEFAULT_BASE_INSTRUCTIONS = "You are a helpful assistant."


def load_published_model_infos(data_dir: Path) -> list[dict[str, Any]]:
    """读取全部发布资产，按官方 slug 去重取最新，返回发布 model_info 列表。"""
    publications = Path(data_dir) / "catalog/publications"
    if not publications.exists():
        return []
    latest: dict[str, tuple[str, dict[str, Any]]] = {}
    for path in sorted(publications.glob("*.json")):
        try:
            entry = PublishedCatalogEntry.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 损坏资产跳过，不阻断目录生成
            continue
        slug = str(entry.model_info_json.get("slug") or "").strip()
        if not slug:
            continue
        seen = latest.get(slug)
        if seen is None or entry.accepted_at >= seen[0]:
            latest[slug] = (entry.accepted_at, entry.model_info_json)
    return [item[1] for item in latest.values()]


def build_catalog_response(model_infos: list[dict[str, Any]]) -> dict[str, Any]:
    """将发布 model_info 映射为官方 ModelsResponse 文档（仅官方字段）。"""
    return {"models": [_official_model_info(item) for item in model_infos]}


def _codex_supported_modalities(values: Any) -> list[str]:
    """Codex CLI ModelInfo 的 input_modalities 枚举：text|image|audio。

    OpenRouter 元数据可能包含 video 等 Codex CLI 不支持的模态，
    官方 load_catalog_json 会因 unknown variant 拒绝解析整个目录。
    """
    allowed = {"text", "image", "audio"}
    kept = [str(item) for item in values if str(item) in allowed]
    return kept or ["text"]


# Codex 推理等级从低到高的自然顺序；未知等级置于末尾。
_REASONING_EFFORT_ORDER = {
    "none": 0,
    "minimal": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
    "xhigh": 5,
    "max": 6,
}


def _sort_reasoning_levels(levels: list[str]) -> list[str]:
    """按低→高排序推理等级（已知等级按自然序，未知等级保持相对顺序置于末尾）。"""
    known = [item for item in levels if item in _REASONING_EFFORT_ORDER]
    unknown = [item for item in levels if item not in _REASONING_EFFORT_ORDER]
    known_sorted = sorted(known, key=lambda item: _REASONING_EFFORT_ORDER[item])
    return known_sorted + unknown


def _official_model_info(info: dict[str, Any]) -> dict[str, Any]:
    slug = str(info.get("slug") or "").strip()
    if not slug:
        raise ValueError("发布条目缺少 slug，无法生成官方目录条目")
    # 优先使用完整等级列表；缺失时以 reasoning_effort 兜底为单一等级。
    levels = [str(item) for item in (info.get("reasoning_levels") or []) if str(item)]
    if not levels:
        levels = [str(info.get("reasoning_effort") or "medium")]
    # 按低→高排序（Codex 目录惯例），未知等级置末尾。
    levels = _sort_reasoning_levels(levels)
    effort = str(info.get("reasoning_effort") or levels[0] or "medium")
    context_window = int(info.get("context_window") or 128000)
    if context_window <= 0:
        raise ValueError(f"发布条目 {slug} 的 context_window 必须为正整数")
    # Codex CLI 0.147+ 的 ModelInfo serde 要求以下布尔/标量字段必须存在，
    # 缺失会导致 load_catalog_json 直接拒绝整个目录（E2E 实测）。
    supports_reasoning_summaries = any(item != "none" for item in levels)
    return {
        "slug": slug,
        "display_name": str(info.get("name") or slug),
        "description": str(info.get("description") or f"Via codex-ai-gateway: {slug}"),
        "default_reasoning_level": effort,
        "supported_reasoning_levels": [
            {"effort": item, "description": f"Reasoning effort: {item}"}
            for item in levels
        ],
        "shell_type": "unified_exec",
        "visibility": "list",
        "supported_in_api": True,
        "priority": 0,
        "support_verbosity": False,
        "apply_patch_tool_type": "freeform",
        "web_search_tool_type": "text",
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "context_window": context_window,
        "max_context_window": context_window,
        "effective_context_window_percent": 95,
        "input_modalities": _codex_supported_modalities(info.get("input_modalities") or ["text"]),
        "experimental_supported_tools": [],
        "base_instructions": str(info.get("base_instructions") or DEFAULT_BASE_INSTRUCTIONS),
        "additional_speed_tiers": [],
        "service_tiers": [],
        "availability_nux": None,
        "upgrade": None,
        "default_reasoning_summary": "none",
        "supports_reasoning_summaries": supports_reasoning_summaries,
        "supports_parallel_tool_calls": False,
        "supports_image_detail_original": False,
        "supports_search_tool": False,
    }


def validate_catalog_response(doc: Any) -> None:
    """校验目录文档符合官方 schema：非空 models、必填字段齐全、无非官方字段。"""
    models = doc.get("models") if isinstance(doc, dict) else None
    if not isinstance(models, list) or not models:
        raise ValueError("model catalog 必须包含非空 models 数组（官方 load_catalog_json 契约）")
    for model in models:
        if not isinstance(model, dict):
            raise ValueError("目录条目必须是 JSON 对象")
        missing = [field for field in OFFICIAL_MODEL_INFO_REQUIRED if field not in model]
        if missing:
            raise ValueError(f"目录条目缺少官方必填字段: {missing}")
        forbidden = OFFICIAL_MODEL_INFO_FORBIDDEN & set(model)
        if forbidden:
            raise ValueError(f"目录条目包含非官方字段（会被官方 serde 忽略）: {sorted(forbidden)}")