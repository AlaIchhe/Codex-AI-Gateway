""""规范模型聚合服务。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from codex_ai_gateway.models.entities import (
    Offering,
    OfferingStatus,
    UpstreamStatus,
)
from codex_ai_gateway.services.model_identity import (
    MatchResult,
    build_standard_catalog,
    canonical_from_candidate,
    canonical_from_offering,
    match_offering,
)
from codex_ai_gateway.util import utc_now


async def aggregate_models(
    runtime: Any, *, offerings: list[Offering], upstreams: list[Any]
) -> dict[str, int]:
    """为每个 offering 更新映射，并自动维护 CanonicalModel。

    canonical 关联键：家族键（family_key）→ canonical.slug。
    命中标准目录的映射以 openrouter_model_id 作滚动指针；上游回退模型
    openrouter_model_id=None，slug 为归一化 provider id。
    """
    runtime.state_store.read_state()
    upstream_by_id = {item.id: item for item in upstreams}
    snapshot_data = await _snapshot(runtime)
    openrouter_models = snapshot_data["models"]
    snapshot_id = snapshot_data["snapshot_id"]
    # 标准目录构建一次，全部 offering 复用同一分组与择新结果。
    catalog = build_standard_catalog(openrouter_models)
    results: list[MatchResult] = []
    for offering in offerings:
        if offering.status == OfferingStatus.disabled:
            continue
        upstream = upstream_by_id.get(offering.upstream_id)
        prefixes = getattr(upstream, "namespace_prefixes", set())
        aliases: dict[str, str] = {}
        if upstream is not None and getattr(upstream, "kind", None) == "preset" and upstream.preset_id:
            try:
                from codex_ai_gateway.services.presets import get_preset_provider

                aliases = get_preset_provider(upstream.preset_id).identity_aliases
            except KeyError:
                pass
        results.append(
            match_offering(
                offering,
                openrouter_models,
                namespace_prefixes=set(prefixes or set()),
                snapshot_id=snapshot_id,
                aliases=aliases or None,
                catalog=catalog,
            )
        )
    now = utc_now()

    def apply(state: Any) -> None:
        active_offering_ids = {offering.id for offering in state.offerings}
        state.model_mappings = [
            m
            for m in state.model_mappings
            if m.offering_id in active_offering_ids
            and m.offering_id not in {r.mapping.offering_id for r in results}
        ]
        state.model_mappings.extend(r.mapping for r in results)
        for result in results:
            mapping = result.mapping
            offering = offering_for(state, mapping.offering_id)
            if offering is None:
                continue
            slug = mapping.family_key or mapping.normalized_key.rsplit("/", 1)[-1].strip("-") or "fallback"
            canonical = _canonical_by_slug(state, slug)
            if result.candidate is not None:
                # 命中标准目录：slug 优先复用，滚动更新 openrouter_model_id 指针与基线。
                if canonical is None:
                    canonical = canonical_from_candidate(
                        result.candidate, now=now, slug=slug
                    )
                    state.canonical_models.append(canonical)
                else:
                    if canonical.slug != slug:
                        # 旧数据中 slug 为归一化形态（如 glm-5-3-flash），统一为家族基础名。
                        canonical.slug = slug
                    canonical.openrouter_model_id = mapping.openrouter_model_id
                    canonical.capability_baseline = result.candidate
                    canonical.updated_at = now
            else:
                # 上游回退：不覆盖已存在的同 slug canonical（避免换掉目录命中身份）。
                if canonical is None:
                    fallback = canonical_from_offering(offering, now=now, slug=slug)
                    state.canonical_models.append(fallback)
        for result in results:
            offering = next((o for o in state.offerings if o.id == result.mapping.offering_id), None)
            if offering is None:
                continue
            slug = (
                result.mapping.family_key
                or result.mapping.normalized_key.rsplit("/", 1)[-1].strip("-")
                or "fallback"
            )
            canonical = _canonical_by_slug(state, slug)
            offering.canonical_model_id = canonical.id if canonical else None
            offering.updated_at = now
        # 重建可用状态：任一 enabled upstream 的 approved matched offering 即可用。
        for model in state.canonical_models:
            offering_ids = {
                o.id for o in state.offerings if o.canonical_model_id == model.id
            }
            available = False
            for offering in state.offerings:
                if offering.id not in offering_ids or offering.status != OfferingStatus.approved:
                    continue
                upstream = next((u for u in state.upstreams if u.id == offering.upstream_id), None)
                if upstream and upstream.status == UpstreamStatus.enabled:
                    available = True
                    break
            model.status = "available" if available else "unavailable"
            model.updated_at = now

    runtime.state_store.mutate(apply)
    return {"offerings": len(offerings), "matched": sum(1 for r in results if r.candidate)}


def _canonical_by_slug(
    state: Any, slug: str
) -> Any | None:
    """按 slug 查找 canonical；精确未命中时按归一化形态匹配（`.`/`-` 互换），
    兼容旧版本数据中 slug 为归一化连字符形态（如 glm-5-3-flash）的情况。
    """
    canon = next((m for m in state.canonical_models if m.slug == slug), None)
    if canon is not None:
        return canon
    normalized = slug.replace(".", "-").replace("/", "-")
    return next(
        (m for m in state.canonical_models if m.slug.replace(".", "-").replace("/", "-") == normalized),
        None,
    )


def offering_for(state: Any, offering_id: str) -> Offering | None:
    return next((o for o in state.offerings if o.id == offering_id), None)


def _snapshot_current(snapshot: Any, *, now: str | None = None) -> bool:
    try:
        current_at = now or datetime.now(UTC).isoformat()
        return snapshot.status.value == "current" and snapshot.expires_at > current_at
    except Exception:
        return False


async def _snapshot(runtime: Any) -> dict[str, Any]:
    from codex_ai_gateway.services.model_identity import fetch_openrouter_snapshot

    state = runtime.state_store.read_state()
    snapshots = sorted(getattr(state, "openrouter_snapshots", []), key=lambda item: item.fetched_at)
    if snapshots and _snapshot_current(snapshots[-1]):
        snapshot = snapshots[-1]
        return {"models": snapshot.models_json, "snapshot_id": snapshot.id}
    data = await fetch_openrouter_snapshot()
    if data.snapshot is None:
        if snapshots and _snapshot_current(snapshots[-1]):
            snapshot = snapshots[-1]
            return {"models": snapshot.models_json, "snapshot_id": snapshot.id}
        return {"models": [], "snapshot_id": None}
    if snapshots:
        latest = snapshots[-1]
        data.snapshot.etag = data.snapshot.etag or latest.etag
        data.snapshot.last_modified = data.snapshot.last_modified or latest.last_modified
    runtime.state_store.mutate(
        lambda state: (
            state.openrouter_snapshots.append(data.snapshot),
            setattr(
                state,
                "openrouter_snapshots",
                sorted(state.openrouter_snapshots, key=lambda item: item.fetched_at)[-100:],
            ),
        )[-1]
    )
    return {"models": data.models, "snapshot_id": data.snapshot.id if data.snapshot else None}


async def _models(runtime: Any) -> list[dict[str, Any]]:
    return (await _snapshot(runtime))["models"]
