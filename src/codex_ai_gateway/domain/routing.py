""""规范模型路由与备用上游顺序。"""

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
    canonical = next(
        (
            m
            for m in state.canonical_models
            if m.status == "available"
            and (m.slug.lower() == normalized or m.openrouter_model_id.lower() == normalized)
        ),
        None,
    )
    if canonical is None:
        raise RoutingError(
            code="unknown_model",
            message=f"模型 '{model}' 不在可路由目录中。",
            status_code=404,
        )
    return canonical


def route_candidates(
    state: Any, canonical: CanonicalModel
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
            and o.wire_protocol in upstream.confirmed_protocols
        ]
        if any(o.wire_protocol == WireProtocol.responses for o in candidates):
            offering = next(o for o in candidates if o.wire_protocol == WireProtocol.responses)
            responses_first.append((offering, upstream, WireProtocol.responses))
            continue
        if any(o.wire_protocol == WireProtocol.chat_completions for o in candidates):
            offering = next(o for o in candidates if o.wire_protocol == WireProtocol.chat_completions)
            responses_first.append((offering, upstream, WireProtocol.chat_completions))
    return responses_first
