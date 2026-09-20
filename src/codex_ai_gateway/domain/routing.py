""" "规范模型路由与备用上游顺序。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from codex_ai_gateway.domain.circuit_breaker import CircuitBreaker
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


def _protocol_order(prefer_chat: bool) -> tuple[WireProtocol, WireProtocol]:
    """候选协议顺序。

    协议未确认时这个顺序就是试错顺序：``responses`` 优先以保留上游直通的
    保真度（走 chat 必须过翻译层）。
    """
    if prefer_chat:
        return (WireProtocol.chat_completions, WireProtocol.responses)
    return (WireProtocol.responses, WireProtocol.chat_completions)


def route_candidates(
    state: Any,
    canonical: CanonicalModel,
    *,
    prefer_chat: bool = False,
    circuit_breaker: CircuitBreaker | None = None,
) -> list[tuple[Offering, Upstream, WireProtocol]]:
    """按生效优先级返回 (offering, upstream, 协议) 候选。

    协议已确认的 offering 直接成为候选；某 upstream 下完全没有协议记录时，
    用 ``unconfirmed`` 占位展开成两个协议候选，让第一个真实请求充当探针。
    已确认协议存在时不展开兜底——只学一个协议即可。

    ``circuit_breaker`` 只提供 (upstream, provider_model, 协议) 粒度的失败避让
    窗口，用于把近期失败的目标排到候选列表最后；它不再屏蔽目标，也不存在
    “全部目标冷却”这种不可路由状态（fail-open）。
    """
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
    responses_first: list[tuple[Offering, Upstream, WireProtocol, float]] = []
    for upstream in ordered:
        candidates = [
            o
            for o in state.offerings
            if o.canonical_model_id == canonical.id
            and o.status == OfferingStatus.approved
            and o.upstream_id == upstream.id
        ]
        if not candidates:
            continue
        # 同一 upstream 的两种协议各自成为候选：某个协议端点不支持该模型时
        # （例如上游 responses 端点返回 400 unsupported_model），还能回落到
        # 另一个协议，而不是把整个模型判死。已确认协议优先；完全没有协议记录
        # 时才拿 unconfirmed 占位展开两种协议，让真实请求充当探针。
        pairs: list[tuple[Offering, WireProtocol]] = []
        for protocol in _protocol_order(prefer_chat):
            offering = next((o for o in candidates if o.wire_protocol == protocol), None)
            if offering is not None:
                pairs.append((offering, protocol))
        if not pairs:
            placeholder = next(
                (o for o in candidates if o.wire_protocol == WireProtocol.unconfirmed), None
            )
            if placeholder is None:
                continue
            pairs = [(placeholder, protocol) for protocol in _protocol_order(prefer_chat)]
        for offering, protocol in pairs:
            # 方案 A：失败目标只降权、不屏蔽。避让中的目标排在健康目标之后，
            # 全部目标都在避让时仍然 fail-open（尝试最优目标），绝不返回
            # “所有上游均在冷却中”。避让窗口按展开后的具体协议计算，
            # 因此猜错某个协议后能立刻把另一个协议排到前面。
            avoid_seconds = 0.0
            if circuit_breaker is not None:
                avoid_seconds = (
                    circuit_breaker.remaining(
                        upstream.id,
                        offering.provider_model_id,
                        wire_protocol=protocol,
                    )
                    or 0.0
                )
            responses_first.append((offering, upstream, protocol, avoid_seconds))
    # 稳定排序：健康目标保持配置顺序，避让中的目标排到最后（最早恢复的优先）。
    responses_first.sort(key=lambda item: (item[3] > 0, item[3]))
    return [(offering, upstream, protocol) for offering, upstream, protocol, _ in responses_first]


def earliest_cooldown_seconds(
    state: Any,
    canonical: CanonicalModel,
    circuit_breaker: CircuitBreaker,
    *,
    now: float | None = None,
) -> float | None:
    """该模型所有已批准 target 中的最早避让剩余秒数（仅供观测/提示）。

    方案 A 下这不构成门禁：``route_candidates`` 即使在所有 target 都处于
    避让窗口时也照常返回候选，因此该值只用于展示与 ``Retry-After`` 提示。
    """
    targets = [
        (offering.upstream_id, offering.provider_model_id, offering.wire_protocol)
        for offering in state.offerings
        if offering.canonical_model_id == canonical.id
        and offering.status == OfferingStatus.approved
    ]
    return circuit_breaker.earliest_remaining(targets, now=now)
