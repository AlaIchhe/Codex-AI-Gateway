"""方案 A 回归：请求侧错误返回 4xx 且不冷却；上游失败只降权、永不屏蔽。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from codex_ai_gateway.api.gateway import _attempt_with_fallback
from codex_ai_gateway.domain.circuit_breaker import CircuitBreaker
from codex_ai_gateway.domain.routing import route_candidates
from codex_ai_gateway.models.entities import (
    CanonicalModel,
    Offering,
    OfferingStatus,
    Upstream,
    WireProtocol,
)

CURRENT_IMAGE_BODY: dict[str, Any] = {
    "model": "m",
    "input": [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "看一下这张图"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            ],
        }
    ],
}


class _UsageLog:
    def __init__(self) -> None:
        self.pending: list[Any] = []
        self.finalized: list[Any] = []

    def create_pending(self, event: Any) -> None:
        self.pending.append(event)

    def record_finalized(self, event: Any) -> None:
        self.finalized.append(event)


class _FailingUpstreamClient:
    """返回 500 的桩客户端：模拟上游故障。"""

    async def request(self, upstream: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(status_code=500, headers={}, body=b"{}")


def _upstream() -> Upstream:
    return Upstream(
        id="u1",
        name="u1",
        base_url="https://u1.example.com/v1",
        auth_credential_ref="upstream:u1:api_credential",
        created_at="2026-08-30T00:00:00+00:00",
        updated_at="2026-08-30T00:00:00+00:00",
    )


def _offering(protocol: WireProtocol) -> Offering:
    return Offering(
        id="u1-model-a",
        upstream_id="u1",
        provider_model_id="model-a",
        wire_protocol=protocol,
        display_name="model-a",
        status=OfferingStatus.approved,
        canonical_model_id="canon-1",
        discovered_at="2026-08-30T00:00:00+00:00",
        updated_at="2026-08-30T00:00:00+00:00",
    )


def _canonical() -> CanonicalModel:
    return CanonicalModel(
        id="canon-1",
        display_name="model-a",
        slug="model-a",
        status="available",
        first_matched_at="2026-08-30T00:00:00+00:00",
        updated_at="2026-08-30T00:00:00+00:00",
    )


def _runtime(client: Any) -> SimpleNamespace:
    canonical = _canonical()
    return SimpleNamespace(
        state_store=SimpleNamespace(
            read_state=lambda: SimpleNamespace(canonical_models=[canonical])
        ),
        usage_log=_UsageLog(),
        circuit_breaker=CircuitBreaker(clock=lambda: 1000.0, jitter=lambda: 0.0),
        upstream_client=client,
    )


def test_untranslatable_capability_returns_422_without_avoidance() -> None:
    runtime = _runtime(_FailingUpstreamClient())
    offering = _offering(WireProtocol.chat_completions)
    upstream = _upstream()

    response = asyncio.run(
        _attempt_with_fallback(
            request=SimpleNamespace(headers={}),
            runtime=runtime,
            canonical_id="canon-1",
            candidates=[(offering, upstream, WireProtocol.chat_completions)],
            body=CURRENT_IMAGE_BODY,
        )
    )

    assert response.status_code == 422
    payload = json.loads(bytes(response.body))
    assert payload["error"]["code"] == "untranslatable_capability"
    assert payload["error"]["details"]["capability"] == "multimodal_input"
    # 关键：请求侧错误不产生任何避让记录，也不会污染上游健康。
    assert runtime.circuit_breaker.snapshot() == []


def test_upstream_fault_records_avoidance_but_still_routes_afterwards() -> None:
    runtime = _runtime(_FailingUpstreamClient())
    offering = _offering(WireProtocol.responses)
    upstream = _upstream()
    state = SimpleNamespace(
        upstreams=[upstream],
        offerings=[offering],
        routing_preferences=[],
    )

    response = asyncio.run(
        _attempt_with_fallback(
            request=SimpleNamespace(headers={}),
            runtime=runtime,
            canonical_id="canon-1",
            candidates=[(offering, upstream, WireProtocol.responses)],
            body={"model": "m", "input": "hi"},
        )
    )

    assert response.status_code == 500
    # 失败目标被记入避让窗口……
    assert runtime.circuit_breaker.remaining("u1", "model-a") is not None
    # ……但下一次请求仍然能路由到它，而不是拿到「所有上游均在冷却中」。
    candidates = route_candidates(state, _canonical(), circuit_breaker=runtime.circuit_breaker)
    assert len(candidates) == 1
    assert candidates[0][1].id == "u1"
