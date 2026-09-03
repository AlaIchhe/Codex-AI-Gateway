""" "规范模型路由与备用上游顺序。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from codex_ai_gateway.models.entities import (
    CanonicalModel,
    Offering,
    OfferingStatus,
    RoutingScope,
    Upstream,
    UpstreamStatus,
    WireProtocol,
)
from codex_ai_gateway.services.model_identity import family_key_of
from codex_ai_gateway.services.upstreams import is_cooling_down


class RoutingError(Exception):
    def __init__(self, *, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass
class ResolvedRoute:
    canonical_model: CanonicalModel
    offering: Offering
    upstream: Upstream
    protocol: WireProtocol


def _enabled_upstreams(state: Any) -> list[Upstream]:
    return [u for u in state.upstreams if u.status == UpstreamStatus.enabled]


def resolve_canonical_model(state: Any, model: str) -> CanonicalModel:
    normalized = model.strip().lower()
    family_key = family_key_of(normalized)
    for m in state.canonical_models:
        if m.status != "available":
            continue
        slug = m.slug.lower()
        # 家族键精确匹配覆盖日期/滚动别名变体；openrouter_model_id 精确匹配作为补充。
        openrouter_id = m.openrouter_model_id.lower() if m.openrouter_model_id else None
        if (
            slug == normalized
            or slug == family_key
            or (openrouter_id is not None and openrouter_id == normalized)
        ):
            return m
    raise RoutingError(
        code="unknown_model",
        message=f"模型 '{model}' 不在可路由目录中。",
        status_code=404,
    )


def route_candidates(
    state: Any, canonical: CanonicalModel, *, prefer_chat: bool = False
) -> list[tuple[Offering, Upstream, WireProtocol]]:
    """按生效优先级返回已确认 offering/upstream 候选。"""
    enabled = {u.id: u for u in _enabled_upstreams(state)}
    global_pref = next(
        (r for r in state.routing_preferences if r.scope == RoutingScope.global_preference),
        None,
    )
    model_pref = next(
        (
            r
            for r in state.routing_preferences
            if r.scope == RoutingScope.canonical_model and r.canonical_model_id == canonical.id
        ),
        None,
    )
    order = (model_pref or global_pref).ordered_upstream_ids if (model_pref or global_pref) else []
    ordered = [enabled[i] for i in order if i in enabled]
    ordered.extend(u for u in enabled.values() if u not in ordered)
    responses_first: list[tuple[Offering, Upstream, WireProtocol]] = []
    for upstream in ordered:
        if is_cooling_down(upstream):
            continue
        candidates = [
            o
            for o in state.offerings
            if o.canonical_model_id == canonical.id
            and o.status == OfferingStatus.approved
            and o.upstream_id == upstream.id
        ]
        if any(o.wire_protocol == WireProtocol.responses for o in candidates):
            responses_offering = next(
                (o for o in candidates if o.wire_protocol == WireProtocol.responses), None
            )
            chat_offering = next(
                (o for o in candidates if o.wire_protocol == WireProtocol.chat_completions), None
            )
            if prefer_chat and chat_offering is not None:
                responses_first.append((chat_offering, upstream, WireProtocol.chat_completions))
            elif responses_offering is not None:
                responses_first.append((responses_offering, upstream, WireProtocol.responses))
            continue
        if any(o.wire_protocol == WireProtocol.chat_completions for o in candidates):
            offering = next(
                o for o in candidates if o.wire_protocol == WireProtocol.chat_completions
            )
            responses_first.append((offering, upstream, WireProtocol.chat_completions))
    return responses_first
