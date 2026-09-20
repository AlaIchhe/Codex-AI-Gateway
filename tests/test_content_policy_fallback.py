"""内容审查拒收（Content Exists Risk / DataInspectionFailed）的 fallback 回归。

线上现象：command ai 的 deepseek-v4.1-flash 对一份带毒历史恒返回 HTTP 400
``Content Exists Risk``。旧行为把它归成 ``provider_invalid_request``，于是
``classify_failure`` 判 stop：既不换上游、也不给可读提示，用户看到的是
「上游返回 400」，还以为是自己请求格式写错了，于是反复重试同一份内容。

参考 LiteLLM 的 ``ContentPolicyViolationError`` + ``content_policy_fallbacks``：
内容策略拒收要独立成类，走「换上游」而不是当作格式错误直接失败；同时因为
问题出在请求内容而不是上游健康，绝不能写入避让窗口（否则会误伤该上游的其它
会话与其它模型）。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from codex_ai_gateway.api.gateway import _attempt_with_fallback
from codex_ai_gateway.domain.circuit_breaker import CircuitBreaker
from codex_ai_gateway.models.entities import (
    CanonicalModel,
    Offering,
    OfferingStatus,
    Upstream,
    WireProtocol,
)

# command ai / DeepSeek 的真实返回：JSON-in-JSON，Content Exists Risk 在第二层。
CONTENT_EXISTS_RISK_BODY = json.dumps(
    {
        "error": {
            "message": json.dumps(
                {
                    "error": {
                        "message": "Content Exists Risk",
                        "type": "AI_APICallError",
                        "isRetryable": False,
                    }
                }
            )
        }
    }
).encode()

RATE_LIMIT_BODY = json.dumps(
    {"error": {"message": "Too many requests", "code": "rate_limit_exceeded"}}
).encode()

OK_BODY = b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'


def _upstream(uid: str, name: str) -> Upstream:
    return Upstream(
        id=uid,
        name=name,
        base_url=f"https://{uid}.example.com/v1",
        auth_credential_ref=f"upstream:{uid}:api_credential",
        created_at="2026-08-30T00:00:00+00:00",
        updated_at="2026-08-30T00:00:00+00:00",
    )


def _offering(uid: str, protocol: WireProtocol) -> Offering:
    return Offering(
        id=f"{uid}-model-a-{protocol.value}",
        upstream_id=uid,
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
    """按顺序返回预置响应，并记录每次请求打到了哪个上游的哪个路径。"""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    async def request(self, upstream: Any, **kwargs: Any) -> Any:
        self.calls.append((upstream.id, str(kwargs.get("path"))))
        if self._responses:
            return self._responses.pop(0)
        return SimpleNamespace(status_code=200, headers={}, body=OK_BODY)


def _breaker() -> CircuitBreaker:
    return CircuitBreaker(clock=lambda: 1000.0, jitter=lambda: 0.0)


def _run(client: Any, breaker: CircuitBreaker, candidates: list[Any]) -> Any:
    canonical = _canonical()
    runtime = SimpleNamespace(
        state_store=SimpleNamespace(
            read_state=lambda: SimpleNamespace(canonical_models=[canonical])
        ),
        usage_log=_UsageLog(),
        circuit_breaker=breaker,
        upstream_client=client,
    )
    return asyncio.run(
        _attempt_with_fallback(
            request=SimpleNamespace(headers={}),
            runtime=runtime,
            canonical_id="canon-1",
            candidates=candidates,
            body={"model": "model-a", "input": "hi"},
        )
    )


def _two_upstreams() -> list[Any]:
    return [
        (
            _offering("u1", WireProtocol.chat_completions),
            _upstream("u1", "command ai"),
            WireProtocol.chat_completions,
        ),
        (
            _offering("u2", WireProtocol.chat_completions),
            _upstream("u2", "backup"),
            WireProtocol.chat_completions,
        ),
    ]


def test_content_policy_falls_through_to_next_upstream() -> None:
    """第一上游内容审查拒收 -> 继续打第二个上游，而不是直接把 400 抛回去。"""
    breaker = _breaker()
    client = _Client(
        [
            SimpleNamespace(status_code=400, headers={}, body=CONTENT_EXISTS_RISK_BODY),
            SimpleNamespace(
                status_code=200,
                headers={"content-type": "application/json"},
                body=OK_BODY,
            ),
        ]
    )

    response = _run(client, breaker, _two_upstreams())

    assert client.calls == [("u1", "/chat/completions"), ("u2", "/chat/completions")]
    assert response.status_code == 200
    # 问题在请求内容而不是上游健康：不能落进避让窗口。
    assert breaker.snapshot() == []


def test_all_upstreams_blocked_surfaces_content_policy_message() -> None:
    """全部上游都拒收时，用户必须看到内容审查，而不是「上游返回 400」。"""
    breaker = _breaker()
    client = _Client(
        [
            SimpleNamespace(status_code=400, headers={}, body=CONTENT_EXISTS_RISK_BODY),
            SimpleNamespace(status_code=400, headers={}, body=CONTENT_EXISTS_RISK_BODY),
        ]
    )

    response = _run(client, breaker, _two_upstreams())

    payload = json.loads(bytes(response.body))
    assert response.status_code == 400
    assert payload["error"]["code"] == "provider_content_policy_blocked"
    assert "内容审查" in payload["error"]["message"]
    assert breaker.snapshot() == []


def test_content_policy_outranks_rate_limit_in_terminal_error() -> None:
    """内容审查要比限流更该被看到：限流等一会儿能好，带毒上下文不会自己变好。"""
    breaker = _breaker()
    client = _Client(
        [
            SimpleNamespace(status_code=429, headers={"retry-after": "3"}, body=RATE_LIMIT_BODY),
            SimpleNamespace(status_code=400, headers={}, body=CONTENT_EXISTS_RISK_BODY),
        ]
    )

    response = _run(client, breaker, _two_upstreams())

    payload = json.loads(bytes(response.body))
    assert payload["error"]["code"] == "provider_content_policy_blocked"


def test_same_upstream_other_protocol_is_not_retried() -> None:
    """同一上游的另一个协议面必然被同样拒收：换协议没意义，不该再打一次。

    线上这一次浪费很贵：command ai 的 /responses 对同一模型本来就恒 400，
    再打一次只会把内容审查错误盖成「上游不支持该模型或已下线」，同时又多花
    一次往返。
    """
    breaker = _breaker()
    client = _Client(
        [
            SimpleNamespace(status_code=400, headers={}, body=CONTENT_EXISTS_RISK_BODY),
            SimpleNamespace(status_code=400, headers={}, body=CONTENT_EXISTS_RISK_BODY),
        ]
    )
    candidates = [
        (
            _offering("u1", WireProtocol.chat_completions),
            _upstream("u1", "command ai"),
            WireProtocol.chat_completions,
        ),
        (
            _offering("u1", WireProtocol.responses),
            _upstream("u1", "command ai"),
            WireProtocol.responses,
        ),
    ]

    response = _run(client, breaker, candidates)

    assert client.calls == [("u1", "/chat/completions")]
    payload = json.loads(bytes(response.body))
    assert payload["error"]["code"] == "provider_content_policy_blocked"
    assert breaker.snapshot() == []
