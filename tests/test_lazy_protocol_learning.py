"""延迟协议确认：创建时不探测，由第一个真实请求学出协议。

旧行为是每次同步为每个尚无协议记录的模型并发打两个协议的推理请求
（远端实测每轮约 48 次、新装/新增模型时 166 次），既消耗上游限流配额，
又因为失败结果不落盘而每轮重探。新行为下创建阶段只拉 ``/models``：
没有协议记录的模型建一条 ``unconfirmed`` offering 保持可路由，第一个真实
请求按协议顺序试错，命中后把结论落盘。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx

from codex_ai_gateway.api.gateway import _attempt_with_fallback, _learn_protocol
from codex_ai_gateway.domain.circuit_breaker import CircuitBreaker
from codex_ai_gateway.domain.routing import route_candidates
from codex_ai_gateway.models.entities import (
    CanonicalModel,
    Offering,
    OfferingStatus,
    Upstream,
    WireProtocol,
)
from codex_ai_gateway.services.upstreams import discover_offerings, offering_protocols

UNSUPPORTED_BODY = json.dumps(
    {
        "error": {
            "message": 'Model "model-a" is not supported on this endpoint.',
            "type": "invalid_request_error",
            "code": "unsupported_model",
        }
    }
).encode()

# 与模型无关的 400：映射为 invalid_request，默认会 stop 而不是 hop。
GENERIC_400_BODY = json.dumps(
    {"error": {"message": "Invalid value for 'input'.", "type": "invalid_request_error"}}
).encode()

OK_BODY = b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'


def _upstream() -> Upstream:
    return Upstream(
        id="u1",
        name="command ai",
        base_url="https://u1.example.com/v1",
        auth_credential_ref="upstream:u1:api_credential",
        created_at="2026-08-30T00:00:00+00:00",
        updated_at="2026-08-30T00:00:00+00:00",
    )


def _offering(protocol: WireProtocol, *, offering_id: str = "off-1") -> Offering:
    return Offering(
        id=offering_id,
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
    def create_pending(self, _event: Any) -> None:
        return None

    def record_finalized(self, _event: Any) -> None:
        return None


class _Client:
    """按顺序返回预置响应，记录实际请求的路径。"""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    async def request(self, _upstream: Any, **kwargs: Any) -> Any:
        self.calls.append(str(kwargs.get("path")))
        if self._responses:
            return self._responses.pop(0)
        return SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            body=OK_BODY,
        )


class _StateStore:
    def __init__(self, state: Any) -> None:
        self._state = state
        self.mutate_calls = 0

    def read_state(self) -> Any:
        return self._state

    def mutate(self, fn: Any, **_kwargs: Any) -> None:
        self.mutate_calls += 1
        fn(self._state)


class _Secrets:
    def get_secret(self, _ref: str) -> str:
        return "sk-test"


def _state(offerings: list[Offering], upstreams: list[Upstream] | None = None) -> Any:
    return SimpleNamespace(
        offerings=offerings,
        upstreams=upstreams if upstreams is not None else [_upstream()],
        canonical_models=[_canonical()],
        model_mappings=[],
        routing_preferences=[],
    )


def _runtime(client: Any, state: Any) -> Any:
    return SimpleNamespace(
        state_store=_StateStore(state),
        secret_store=_Secrets(),
        usage_log=_UsageLog(),
        circuit_breaker=CircuitBreaker(clock=lambda: 1000.0, jitter=lambda: 0.0),
        upstream_client=client,
    )


def _request() -> Any:
    return SimpleNamespace(headers={})


def _ok_response() -> Any:
    return SimpleNamespace(
        status_code=200, headers={"content-type": "application/json"}, body=OK_BODY
    )


def _error_response(status: int, body: bytes) -> Any:
    return SimpleNamespace(status_code=status, headers={}, body=body)


# ---------------------------------------------------------------------------
# 创建阶段：只建 unconfirmed 占位，不发推理请求
# ---------------------------------------------------------------------------


def test_offering_protocols_defaults_to_unconfirmed() -> None:
    assert offering_protocols(None) == [WireProtocol.unconfirmed]
    assert offering_protocols([]) == [WireProtocol.unconfirmed]
    assert offering_protocols([WireProtocol.responses]) == [WireProtocol.responses]


def test_discover_offerings_marks_unknown_protocol_as_unconfirmed() -> None:
    offerings = asyncio.run(
        discover_offerings(
            _upstream(),
            "sk-test",
            protocol_map={"model-a": [WireProtocol.chat_completions]},
            models=[{"id": "model-a"}, {"id": "model-b"}],
        )
    )

    pairs = {(item.provider_model_id, item.wire_protocol) for item in offerings}
    assert pairs == {
        ("model-a", WireProtocol.chat_completions),
        ("model-b", WireProtocol.unconfirmed),
    }


def test_upstream_pipeline_never_calls_inference_endpoints(monkeypatch: Any) -> None:
    """同步阶段只允许拉 /models；任何推理请求都是回归。"""
    from codex_ai_gateway.api import admin

    async def fake_fetch(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"id": "model-a"}, {"id": "model-b"}]

    class _ForbiddenClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("模型同步阶段不应发起任何上游推理请求")

    monkeypatch.setattr(admin, "fetch_upstream_models", fake_fetch)
    monkeypatch.setattr(httpx, "AsyncClient", _ForbiddenClient)

    state = _state([], [_upstream()])
    runtime = _runtime(_Client([]), state)

    refreshed = asyncio.run(admin._run_upstream_pipeline(runtime, _upstream()))

    assert refreshed.last_health_result is not None
    assert "已同步 2 个模型" in refreshed.last_health_result
    assert [(item.provider_model_id, item.wire_protocol) for item in state.offerings] == [
        ("model-a", WireProtocol.unconfirmed),
        ("model-b", WireProtocol.unconfirmed),
    ]


def test_upstream_pipeline_prunes_removed_models_from_protocol_memory(
    monkeypatch: Any,
) -> None:
    """上游已下线的模型不再占用协议记忆；留着会让状态文件无限膨胀。"""
    from codex_ai_gateway.api import admin

    async def fake_fetch(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"id": "model-a"}]

    monkeypatch.setattr(admin, "fetch_upstream_models", fake_fetch)

    upstream = _upstream().model_copy(
        update={
            "model_protocol_probe": {
                "model-a": ["chat_completions"],
                "model-gone": ["responses"],
            }
        }
    )
    state = _state([], [upstream])
    runtime = _runtime(_Client([]), state)

    refreshed = asyncio.run(admin._run_upstream_pipeline(runtime, upstream))

    assert refreshed.model_protocol_probe == {"model-a": ["chat_completions"]}
    assert state.upstreams[0].model_protocol_probe == {"model-a": ["chat_completions"]}
    # 已确认协议的模型不再建 unconfirmed 占位，直接按确认的协议建 offering。
    assert [(item.provider_model_id, item.wire_protocol) for item in state.offerings] == [
        ("model-a", WireProtocol.chat_completions)
    ]


def test_upstream_pipeline_preserves_offering_identity(monkeypatch: Any) -> None:
    """同步只刷新元数据，不能给仍在线的模型换 offering id。

    线上现象（2026-09-21 11:58）：模型刷新循环同步完 command ai 的 71 个模型，
    offering 全部换成新 id，catalog candidate 随之重建、capability_probe_at
    归零，于是对 11 个 OpenRouter 未收录的模型重打了一遍上游推理请求。
    """
    from codex_ai_gateway.api import admin

    models: list[dict[str, Any]] = [{"id": "model-a"}, {"id": "model-b"}]

    async def fake_fetch(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [dict(item) for item in models]

    monkeypatch.setattr(admin, "fetch_upstream_models", fake_fetch)

    upstream = _upstream().model_copy(
        update={"model_protocol_probe": {"model-a": ["chat_completions"]}}
    )
    state = _state([], [upstream])
    runtime = _runtime(_Client([]), state)

    asyncio.run(admin._run_upstream_pipeline(runtime, upstream))
    first = {item.provider_model_id: item for item in state.offerings}
    assert set(first) == {"model-a", "model-b"}

    asyncio.run(admin._run_upstream_pipeline(runtime, upstream))
    second = {item.provider_model_id: item for item in state.offerings}

    assert set(second) == {"model-a", "model-b"}
    for model_id, item in second.items():
        assert item.id == first[model_id].id, f"{model_id} 的 offering id 被换掉了"
        # 首次发现时间保持稳定，元数据本身照常刷新。
        assert item.discovered_at == first[model_id].discovered_at

    # 真下线的模型仍要被清掉，不能因为复用身份就变成僵尸条目。
    models.clear()
    models.append({"id": "model-a"})
    asyncio.run(admin._run_upstream_pipeline(runtime, upstream))
    assert [item.provider_model_id for item in state.offerings] == ["model-a"]
    assert state.offerings[0].id == first["model-a"].id


# ---------------------------------------------------------------------------
# 路由：unconfirmed 展开成两个协议候选
# ---------------------------------------------------------------------------


def test_route_candidates_expands_unconfirmed_into_both_protocols() -> None:
    state = _state([_offering(WireProtocol.unconfirmed)])

    candidates = route_candidates(
        state, _canonical(), prefer_chat=False, circuit_breaker=None
    )

    assert [(item[0].id, item[2]) for item in candidates] == [
        ("off-1", WireProtocol.responses),
        ("off-1", WireProtocol.chat_completions),
    ]


def test_route_candidates_prefers_chat_first_when_custom_tools_present() -> None:
    state = _state([_offering(WireProtocol.unconfirmed)])

    candidates = route_candidates(
        state, _canonical(), prefer_chat=True, circuit_breaker=None
    )

    assert [item[2] for item in candidates] == [
        WireProtocol.chat_completions,
        WireProtocol.responses,
    ]


def test_confirmed_protocol_wins_over_unconfirmed_placeholder() -> None:
    """已确认协议存在时不再展开 unconfirmed（只学一个协议即可）。"""
    state = _state(
        [
            _offering(WireProtocol.chat_completions, offering_id="chat"),
            _offering(WireProtocol.unconfirmed, offering_id="placeholder"),
        ]
    )

    candidates = route_candidates(state, _canonical(), circuit_breaker=None)

    assert [item[0].id for item in candidates] == ["chat"]


def test_dead_endpoint_ranks_last_for_unconfirmed_expansion() -> None:
    """猜错某个协议后，该协议被降权，另一个协议排到前面。"""
    breaker = CircuitBreaker(clock=lambda: 1000.0, jitter=lambda: 0.0)
    breaker.record_failure(
        "u1",
        "model-a",
        status_code=400,
        error_type="model_permission",
        code="provider_model_unavailable",
        message="上游不支持该模型或已下线。",
        wire_protocol=WireProtocol.responses,
    )
    state = _state([_offering(WireProtocol.unconfirmed)])

    candidates = route_candidates(state, _canonical(), circuit_breaker=breaker)

    assert [item[2] for item in candidates] == [
        WireProtocol.chat_completions,
        WireProtocol.responses,
    ]


# ---------------------------------------------------------------------------
# 请求路径：真实请求学到协议并落盘
# ---------------------------------------------------------------------------


def test_first_request_learns_protocol_and_persists_it() -> None:
    client = _Client([_error_response(400, UNSUPPORTED_BODY), _ok_response()])
    state = _state([_offering(WireProtocol.unconfirmed)])
    runtime = _runtime(client, state)
    candidates = route_candidates(
        state, _canonical(), circuit_breaker=runtime.circuit_breaker
    )

    response = asyncio.run(
        _attempt_with_fallback(
            request=_request(),
            runtime=runtime,
            canonical_id="canon-1",
            candidates=candidates,
            body={"model": "model-a", "input": "hi"},
        )
    )

    assert client.calls == ["/responses", "/chat/completions"]
    assert response.status_code == 200
    assert state.offerings[0].wire_protocol == WireProtocol.chat_completions
    assert state.upstreams[0].model_protocol_probe == {"model-a": ["chat_completions"]}
    assert state.offerings[0].identity_evidence["source"] == "live_request"


def test_unconfirmed_generic_400_still_tries_the_other_protocol() -> None:
    """协议未确认时，400 可能是「猜错端点」而不是请求有问题，必须继续试。"""
    client = _Client([_error_response(400, GENERIC_400_BODY), _ok_response()])
    state = _state([_offering(WireProtocol.unconfirmed)])
    runtime = _runtime(client, state)
    candidates = route_candidates(
        state, _canonical(), circuit_breaker=runtime.circuit_breaker
    )

    response = asyncio.run(
        _attempt_with_fallback(
            request=_request(),
            runtime=runtime,
            canonical_id="canon-1",
            candidates=candidates,
            body={"model": "model-a", "input": "hi"},
        )
    )

    assert client.calls == ["/responses", "/chat/completions"]
    assert response.status_code == 200


def test_confirmed_protocol_generic_400_is_not_retried_elsewhere() -> None:
    """协议已确认时 400 是请求本身的问题：照旧直接返回，不做额外试探。"""
    client = _Client([_error_response(400, GENERIC_400_BODY), _ok_response()])
    state = _state(
        [
            _offering(WireProtocol.responses, offering_id="responses"),
            _offering(WireProtocol.chat_completions, offering_id="chat"),
        ]
    )
    runtime = _runtime(client, state)
    candidates = route_candidates(
        state, _canonical(), circuit_breaker=runtime.circuit_breaker
    )

    response = asyncio.run(
        _attempt_with_fallback(
            request=_request(),
            runtime=runtime,
            canonical_id="canon-1",
            candidates=candidates,
            body={"model": "model-a", "input": "hi"},
        )
    )

    assert client.calls == ["/responses"]
    assert response.status_code == 400
    assert state.offerings[0].wire_protocol == WireProtocol.responses


def test_learn_protocol_is_noop_for_confirmed_offering() -> None:
    state = _state([_offering(WireProtocol.chat_completions)])
    runtime = _runtime(_Client([]), state)

    _learn_protocol(runtime, _upstream(), state.offerings[0], WireProtocol.responses)

    assert runtime.state_store.mutate_calls == 0
    assert state.upstreams[0].model_protocol_probe == {}


def test_learn_protocol_survives_missing_state_store_mutate() -> None:
    """最小运行时（无 mutate）不应因为学习落盘而报错。"""

    class _ReadOnlyStore:
        def read_state(self) -> Any:
            return _state([_offering(WireProtocol.unconfirmed)])

    runtime = SimpleNamespace(state_store=_ReadOnlyStore())
    state = _state([_offering(WireProtocol.unconfirmed)])

    _learn_protocol(runtime, _upstream(), state.offerings[0], WireProtocol.chat_completions)


# ---------------------------------------------------------------------------
# 启动同步节流
# ---------------------------------------------------------------------------


def test_sync_is_due_skips_recently_synced_upstream() -> None:
    from codex_ai_gateway.app import MODEL_REFRESH_MIN_INTERVAL_SECONDS, _sync_is_due

    assert _sync_is_due(SimpleNamespace(last_health_at=None)) is True
    assert _sync_is_due(SimpleNamespace(last_health_at="")) is True

    fresh = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    assert _sync_is_due(SimpleNamespace(last_health_at=fresh)) is False

    stale = (
        datetime.now(UTC) - timedelta(seconds=MODEL_REFRESH_MIN_INTERVAL_SECONDS + 60)
    ).isoformat()
    assert _sync_is_due(SimpleNamespace(last_health_at=stale)) is True


def test_sync_is_due_tolerates_unparseable_timestamp() -> None:
    from codex_ai_gateway.app import _sync_is_due

    assert _sync_is_due(SimpleNamespace(last_health_at="not-a-timestamp")) is True