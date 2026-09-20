"""端点级「不支持该模型」记忆 + 终端错误选择回归。

线上现象：command ai 的 /responses 端点对 deepseek-v4.1-flash 恒返回
400 unsupported_model，而 /chat/completions 正常（偶尔 429/502）。旧行为下
每次请求都会先打一遍这个必死端点，且它的 400 会盖掉真正的 429/502，用户看到
的是「上游不支持该模型或已下线」——误导且浪费额度。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from codex_ai_gateway.api.gateway import _attempt_with_fallback
from codex_ai_gateway.domain.circuit_breaker import (
    CircuitBreaker,
    CooldownScope,
    classify_failure,
)
from codex_ai_gateway.domain.routing import route_candidates
from codex_ai_gateway.models.entities import (
    CanonicalModel,
    Offering,
    OfferingStatus,
    ProviderErrorType,
    Upstream,
    WireProtocol,
)

UNSUPPORTED_BODY = json.dumps(
    {
        "error": {
            "message": 'Model "model-a" is not supported on this endpoint.',
            "type": "invalid_request_error",
            "param": "model",
            "code": "unsupported_model",
        }
    }
).encode()

RATE_LIMIT_BODY = json.dumps(
    {"error": {"message": "Too many requests", "code": "rate_limit_exceeded"}}
).encode()

# command ai 真实返回：HTTP 403，但 error.code 是 FORBIDDEN，真正的信号在 message。
NOT_IN_PLAN_BODY = json.dumps(
    {
        "error": {
            "message": (
                "MODEL_NOT_IN_PLAN: Gemini 3.5 Flash Lite available in Pro and above "
                "plans or extra on demand usage"
            ),
            "type": "permission_error",
            "code": "FORBIDDEN",
        }
    }
).encode()


def _upstream() -> Upstream:
    return Upstream(
        id="u1",
        name="command ai",
        base_url="https://u1.example.com/v1",
        auth_credential_ref="upstream:u1:api_credential",
        created_at="2026-08-30T00:00:00+00:00",
        updated_at="2026-08-30T00:00:00+00:00",
    )


def _offering(protocol: WireProtocol) -> Offering:
    return Offering(
        id=f"u1-model-a-{protocol.value}",
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


class _UsageLog:
    def create_pending(self, event: Any) -> None:
        return None

    def record_finalized(self, event: Any) -> None:
        return None


class _Client:
    """按顺序返回预置响应，记录实际请求的路径。"""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    async def request(self, upstream: Any, **kwargs: Any) -> Any:
        self.calls.append(str(kwargs.get("path")))
        if self._responses:
            return self._responses.pop(0)
        return SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            body=b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}',
        )


def _runtime(client: Any, breaker: CircuitBreaker | None = None) -> SimpleNamespace:
    canonical = _canonical()
    return SimpleNamespace(
        state_store=SimpleNamespace(
            read_state=lambda: SimpleNamespace(canonical_models=[canonical])
        ),
        usage_log=_UsageLog(),
        circuit_breaker=breaker or CircuitBreaker(clock=lambda: 1000.0, jitter=lambda: 0.0),
        upstream_client=client,
    )


def _breaker() -> CircuitBreaker:
    return CircuitBreaker(clock=lambda: 1000.0, jitter=lambda: 0.0)


def _record_endpoint_unsupported(breaker: CircuitBreaker) -> None:
    breaker.record_failure(
        "u1",
        "model-a",
        status_code=400,
        error_type=ProviderErrorType.model_permission.value,
        code="provider_model_unavailable",
        message="上游不支持该模型或已下线。",
        wire_protocol=WireProtocol.responses,
    )


def test_model_unavailable_is_protocol_scoped_and_long_lived() -> None:
    breaker = _breaker()
    _record_endpoint_unsupported(breaker)

    remaining = breaker.remaining("u1", "model-a", wire_protocol=WireProtocol.responses)
    assert remaining is not None
    # 端点属性不会几分钟自愈：窗口按小时计，而不是原来的 60s。
    assert remaining >= 3600.0
    # 另一个协议面（chat）没有被牵连。
    assert (
        breaker.remaining("u1", "model-a", wire_protocol=WireProtocol.chat_completions) is None
    )


def test_success_on_other_protocol_keeps_endpoint_memory() -> None:
    breaker = _breaker()
    _record_endpoint_unsupported(breaker)

    breaker.record_success("u1", "model-a", wire_protocol=WireProtocol.chat_completions)

    assert (
        breaker.remaining("u1", "model-a", wire_protocol=WireProtocol.responses) is not None
    )
    breaker.record_success("u1", "model-a", wire_protocol=WireProtocol.responses)
    assert (
        breaker.remaining("u1", "model-a", wire_protocol=WireProtocol.responses) is None
    )


def test_route_candidates_puts_dead_endpoint_last() -> None:
    breaker = _breaker()
    _record_endpoint_unsupported(breaker)
    state = SimpleNamespace(
        upstreams=[_upstream()],
        offerings=[_offering(WireProtocol.responses), _offering(WireProtocol.chat_completions)],
        routing_preferences=[],
    )

    candidates = route_candidates(
        state, _canonical(), prefer_chat=False, circuit_breaker=breaker
    )

    assert [item[2] for item in candidates] == [
        WireProtocol.chat_completions,
        WireProtocol.responses,
    ]


def test_terminal_error_prefers_rate_limit_over_model_unavailable() -> None:
    client = _Client(
        [
            SimpleNamespace(status_code=429, headers={"retry-after": "3"}, body=RATE_LIMIT_BODY),
            SimpleNamespace(status_code=400, headers={}, body=UNSUPPORTED_BODY),
        ]
    )
    runtime = _runtime(client)
    upstream = _upstream()

    response = asyncio.run(
        _attempt_with_fallback(
            request=SimpleNamespace(headers={}),
            runtime=runtime,
            canonical_id="canon-1",
            candidates=[
                (_offering(WireProtocol.chat_completions), upstream, WireProtocol.chat_completions),
                (_offering(WireProtocol.responses), upstream, WireProtocol.responses),
            ],
            body={"model": "model-a", "input": "hi"},
        )
    )

    assert client.calls == ["/chat/completions", "/responses"]
    payload = json.loads(bytes(response.body))
    # 真实原因是限流，不能报成「上游不支持该模型或已下线」。
    assert response.status_code == 429
    assert payload["error"]["code"] == "provider_rate_limited"


def test_terminal_error_keeps_model_unavailable_when_all_endpoints_agree() -> None:
    client = _Client(
        [
            SimpleNamespace(status_code=400, headers={}, body=UNSUPPORTED_BODY),
            SimpleNamespace(status_code=400, headers={}, body=UNSUPPORTED_BODY),
        ]
    )
    runtime = _runtime(client)
    upstream = _upstream()

    response = asyncio.run(
        _attempt_with_fallback(
            request=SimpleNamespace(headers={}),
            runtime=runtime,
            canonical_id="canon-1",
            candidates=[
                (_offering(WireProtocol.chat_completions), upstream, WireProtocol.chat_completions),
                (_offering(WireProtocol.responses), upstream, WireProtocol.responses),
            ],
            body={"model": "model-a", "input": "hi"},
        )
    )

    payload = json.loads(bytes(response.body))
    assert response.status_code == 400
    assert payload["error"]["code"] == "provider_model_unavailable"
    assert "不支持该模型" in payload["error"]["message"]


# ---------------------------------------------------------------------------
# MODEL_NOT_IN_PLAN：不在套餐内的模型直接从该上游剔除
# ---------------------------------------------------------------------------


class _PruneStore:
    """最小可写状态存储：记录 mutate 调用并在内存里生效。"""

    def __init__(self, state: Any) -> None:
        self._state = state
        self.mutate_calls = 0

    def read_state(self) -> Any:
        return self._state

    def mutate(self, fn: Any, **_kwargs: Any) -> None:
        self.mutate_calls += 1
        fn(self._state)


def _prune_runtime(client: Any, state: Any) -> SimpleNamespace:
    return SimpleNamespace(
        state_store=_PruneStore(state),
        usage_log=_UsageLog(),
        circuit_breaker=CircuitBreaker(clock=lambda: 1000.0, jitter=lambda: 0.0),
        upstream_client=client,
    )


def test_not_in_plan_is_target_scoped_even_though_status_is_403() -> None:
    """403 不等于账号级鉴权失败：套餐缺模型必须按 target 记住，且窗口按小时计。"""
    classification = classify_failure(
        status_code=403,
        error_type=ProviderErrorType.model_permission.value,
        code="provider_model_not_in_plan",
    )
    assert classification.scope is CooldownScope.target
    assert classification.base_cooldown_seconds >= 3600.0

    breaker = _breaker()
    breaker.record_failure(
        "u1",
        "model-a",
        status_code=403,
        error_type=ProviderErrorType.model_permission.value,
        code="provider_model_not_in_plan",
        wire_protocol=WireProtocol.responses,
    )
    assert breaker.remaining("u1", "model-a", wire_protocol=WireProtocol.responses) is not None
    # 没有污染 provider 级：同一上游的其它模型不受影响。
    assert [entry["scope"] for entry in breaker.snapshot()] == ["target"]


def test_model_not_in_plan_prunes_only_the_failing_protocol() -> None:
    """已确认协议的 offering 只剔除报错的那一个协议面。"""
    client = _Client(
        [
            SimpleNamespace(status_code=403, headers={}, body=NOT_IN_PLAN_BODY),
            SimpleNamespace(
                status_code=200,
                headers={"content-type": "application/json"},
                body=b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}',
            ),
        ]
    )
    state = SimpleNamespace(
        canonical_models=[_canonical()],
        offerings=[
            _offering(WireProtocol.responses),
            _offering(WireProtocol.chat_completions),
        ],
        model_mappings=[],
    )
    runtime = _prune_runtime(client, state)
    upstream = _upstream()

    response = asyncio.run(
        _attempt_with_fallback(
            request=SimpleNamespace(headers={}),
            runtime=runtime,
            canonical_id="canon-1",
            candidates=[
                (_offering(WireProtocol.responses), upstream, WireProtocol.responses),
                (
                    _offering(WireProtocol.chat_completions),
                    upstream,
                    WireProtocol.chat_completions,
                ),
            ],
            body={"model": "model-a", "input": "hi"},
        )
    )

    # responses 被套餐拦住，但 chat/completions 照常可用，不能连坐剔除。
    assert client.calls == ["/responses", "/chat/completions"]
    assert response.status_code == 200
    assert [item.id for item in state.offerings] == ["u1-model-a-chat_completions"]
    assert runtime.state_store.mutate_calls == 1


def test_model_not_in_plan_prunes_unconfirmed_placeholder_and_falls_back() -> None:
    other_offering = _offering(WireProtocol.chat_completions).model_copy(
        update={"id": "u2-model-a-chat", "upstream_id": "u2"}
    )
    client = _Client(
        [
            SimpleNamespace(status_code=403, headers={}, body=NOT_IN_PLAN_BODY),
            SimpleNamespace(status_code=403, headers={}, body=NOT_IN_PLAN_BODY),
            SimpleNamespace(
                status_code=200,
                headers={"content-type": "application/json"},
                body=b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}',
            ),
        ]
    )
    state = SimpleNamespace(
        canonical_models=[_canonical()],
        offerings=[_offering(WireProtocol.unconfirmed), other_offering],
        model_mappings=[],
    )
    runtime = _prune_runtime(client, state)
    upstream = _upstream()
    other_upstream = _upstream().model_copy(update={"id": "u2", "name": "MaoLao"})

    response = asyncio.run(
        _attempt_with_fallback(
            request=SimpleNamespace(headers={}),
            runtime=runtime,
            canonical_id="canon-1",
            candidates=[
                (_offering(WireProtocol.unconfirmed), upstream, WireProtocol.responses),
                (_offering(WireProtocol.unconfirmed), upstream, WireProtocol.chat_completions),
                (other_offering, other_upstream, WireProtocol.chat_completions),
            ],
            body={"model": "model-a", "input": "hi"},
        )
    )

    # 两个协议都被套餐拦住 → 占位整个剔除，落到备用上游正常返回。
    assert client.calls == ["/responses", "/chat/completions", "/chat/completions"]
    assert response.status_code == 200
    assert [item.id for item in state.offerings] == ["u2-model-a-chat"]
    assert runtime.state_store.mutate_calls == 1


def test_not_in_plan_on_single_protocol_keeps_unconfirmed_placeholder() -> None:
    """只在 responses 被套餐拦住时不能判死：chat 试通了就正常学习协议。"""
    client = _Client(
        [
            SimpleNamespace(status_code=403, headers={}, body=NOT_IN_PLAN_BODY),
            SimpleNamespace(
                status_code=200,
                headers={"content-type": "application/json"},
                body=b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}',
            ),
        ]
    )
    state = SimpleNamespace(
        canonical_models=[_canonical()],
        upstreams=[_upstream()],
        offerings=[_offering(WireProtocol.unconfirmed)],
        model_mappings=[],
    )
    runtime = _prune_runtime(client, state)
    upstream = _upstream()

    response = asyncio.run(
        _attempt_with_fallback(
            request=SimpleNamespace(headers={}),
            runtime=runtime,
            canonical_id="canon-1",
            candidates=[
                (_offering(WireProtocol.unconfirmed), upstream, WireProtocol.responses),
                (_offering(WireProtocol.unconfirmed), upstream, WireProtocol.chat_completions),
            ],
            body={"model": "model-a", "input": "hi"},
        )
    )

    assert client.calls == ["/responses", "/chat/completions"]
    assert response.status_code == 200
    assert runtime.state_store.mutate_calls == 1
    assert [item.wire_protocol for item in state.offerings] == [WireProtocol.chat_completions]
    assert state.offerings[0].identity_evidence["source"] == "live_request"


def test_model_not_in_plan_prune_cleans_dangling_model_mappings() -> None:
    client = _Client([SimpleNamespace(status_code=403, headers={}, body=NOT_IN_PLAN_BODY)])
    offering = _offering(WireProtocol.responses)
    state = SimpleNamespace(
        canonical_models=[_canonical()],
        offerings=[offering],
        model_mappings=[SimpleNamespace(offering_id=offering.id)],
    )
    runtime = _prune_runtime(client, state)

    asyncio.run(
        _attempt_with_fallback(
            request=SimpleNamespace(headers={}),
            runtime=runtime,
            canonical_id="canon-1",
            candidates=[(offering, _upstream(), WireProtocol.responses)],
            body={"model": "model-a", "input": "hi"},
        )
    )

    assert state.offerings == []
    assert state.model_mappings == []


def test_not_in_plan_prune_is_skipped_for_readonly_state_store() -> None:
    """最小运行时（无 mutate）不应因为剔除逻辑而报错。"""
    client = _Client([SimpleNamespace(status_code=403, headers={}, body=NOT_IN_PLAN_BODY)])
    runtime = _runtime(client)

    response = asyncio.run(
        _attempt_with_fallback(
            request=SimpleNamespace(headers={}),
            runtime=runtime,
            canonical_id="canon-1",
            candidates=[(_offering(WireProtocol.responses), _upstream(), WireProtocol.responses)],
            body={"model": "model-a", "input": "hi"},
        )
    )

    assert response.status_code == 403
