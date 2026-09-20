"""跨上游历史复用 + 上游错误留档的回归测试。

背景：同一个 Codex 对话会跨上游、跨模型继续。历史里的 developer 片段被翻译成
中段 ``role=system`` 后，严格的上游会直接 400（``Invalid input`` /
``param: messages.N.content``）；而网关原来既丢 instructions，又不留上游错误正文，
出事之后完全无法定位。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from codex_ai_gateway.adapters.protocol_normal_form import normalize_request
from codex_ai_gateway.adapters.responses_chat_translation import chat_request_from_normal
from codex_ai_gateway.api.gateway import _attempt_with_fallback
from codex_ai_gateway.domain.circuit_breaker import (
    CircuitBreaker,
    CooldownScope,
    FailureDecision,
    classify_failure,
)
from codex_ai_gateway.domain.error_mapping import map_provider_error
from codex_ai_gateway.models.entities import (
    CanonicalModel,
    Offering,
    OfferingStatus,
    Upstream,
    WireProtocol,
)

INVALID_INPUT_BODY = json.dumps(
    {
        "error": {
            "message": "Invalid input",
            "type": "invalid_request_error",
            "param": "messages.1.content",
        }
    }
).encode()


def _chat_body(instructions: str | None, items: list[dict[str, Any]]) -> dict[str, Any]:
    body: dict[str, Any] = {"model": "m", "stream": False, "input": items}
    if instructions is not None:
        body["instructions"] = instructions
    normal = normalize_request(inbound_protocol="responses", body=body)
    return chat_request_from_normal(normal, target_model="provider-model")


def _text_message(role: str, text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": role,
        "content": [{"type": "input_text", "text": text}],
    }


def test_chat_translation_keeps_request_instructions() -> None:
    body = _chat_body("You are Codex.", [_text_message("user", "hi")])
    assert body["messages"][0] == {"role": "system", "content": "You are Codex."}
    assert body["messages"][1] == {"role": "user", "content": "hi"}


def test_mid_history_developer_blocks_fold_into_leading_system() -> None:
    """跨上游历史里中段重复下发的 developer 片段不能变成中段 system。"""
    body = _chat_body(
        "You are Codex.",
        [
            _text_message("user", "first"),
            _text_message("assistant", "ok"),
            _text_message("developer", "<app-context>re-sent</app-context>"),
            _text_message("user", "second"),
            _text_message("developer", "<skills_instructions>more</skills_instructions>"),
            _text_message("user", "third"),
        ],
    )
    system_indexes = [
        index
        for index, message in enumerate(body["messages"])
        if message["role"] == "system"
    ]
    assert system_indexes == [0]
    system_text = body["messages"][0]["content"]
    assert system_text.startswith("You are Codex.")
    assert "<app-context>re-sent</app-context>" in system_text
    assert "<skills_instructions>more</skills_instructions>" in system_text
    assert [m["role"] for m in body["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
        "user",
    ]


def test_empty_messages_are_pruned_and_tool_output_gets_placeholder() -> None:
    body = _chat_body(
        "You are Codex.",
        [
            _text_message("user", "run it"),
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "exec_command",
                "arguments": "{}",
            },
            {"type": "function_call_output", "call_id": "call_1", "output": ""},
            _text_message("user", ""),
        ],
    )
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    tool_message = body["messages"][-1]
    assert tool_message["content"].strip()


def test_provider_error_keeps_excerpt_for_invalid_input() -> None:
    mapped = map_provider_error(
        400, body=INVALID_INPUT_BODY, upstream_name="command ai"
    )
    assert mapped["error_mapping_code"] == "provider_invalid_request"
    assert "Invalid input" in mapped["excerpt"]
    assert "messages.1.content" in mapped["excerpt"]


def test_context_length_body_is_request_shape_not_invalid_request() -> None:
    mapped = map_provider_error(
        400,
        body=b'{"error":{"message":"This model\'s maximum context length is 128000 tokens"}}',
        upstream_name="u1",
    )
    assert mapped["error_mapping_code"] == "provider_context_length_exceeded"
    classification = classify_failure(
        status_code=400,
        error_type=mapped["provider_error_type"],
        code=mapped["error_mapping_code"],
        message=mapped["message"],
    )
    # 换个窗口更大的上游可能成功，且不把上游判断为故障。
    assert classification.decision is FailureDecision.hop
    assert classification.scope is CooldownScope.none


class _UsageLog:
    def __init__(self) -> None:
        self.pending: list[Any] = []
        self.finalized: list[Any] = []

    def create_pending(self, event: Any) -> None:
        self.pending.append(event)

    def record_finalized(self, event: Any) -> None:
        self.finalized.append(event)


class _InvalidInputClient:
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    async def request(self, upstream: Any, **kwargs: Any) -> Any:
        self.bodies.append(kwargs.get("json_body") or {})
        return SimpleNamespace(
            status_code=400,
            headers={"content-type": "application/json"},
            body=INVALID_INPUT_BODY,
        )


def _runtime(client: Any) -> SimpleNamespace:
    canonical = CanonicalModel(
        id="canon-1",
        display_name="model-a",
        slug="model-a",
        status="available",
        first_matched_at="2026-08-30T00:00:00+00:00",
        updated_at="2026-08-30T00:00:00+00:00",
    )
    return SimpleNamespace(
        state_store=SimpleNamespace(
            read_state=lambda: SimpleNamespace(canonical_models=[canonical])
        ),
        usage_log=_UsageLog(),
        circuit_breaker=CircuitBreaker(clock=lambda: 1000.0, jitter=lambda: 0.0),
        upstream_client=client,
    )


def test_gateway_records_upstream_error_excerpt_and_request_digest() -> None:
    client = _InvalidInputClient()
    runtime = _runtime(client)
    upstream = Upstream(
        id="u1",
        name="command ai",
        base_url="https://u1.example.com/v1",
        auth_credential_ref="upstream:u1:api_credential",
        created_at="2026-08-30T00:00:00+00:00",
        updated_at="2026-08-30T00:00:00+00:00",
    )
    offering = Offering(
        id="u1-model-a-chat_completions",
        upstream_id="u1",
        provider_model_id="model-a",
        wire_protocol=WireProtocol.chat_completions,
        display_name="model-a",
        status=OfferingStatus.approved,
        canonical_model_id="canon-1",
        discovered_at="2026-08-30T00:00:00+00:00",
        updated_at="2026-08-30T00:00:00+00:00",
    )

    response = asyncio.run(
        _attempt_with_fallback(
            request=SimpleNamespace(headers={}),
            runtime=runtime,
            canonical_id="canon-1",
            candidates=[(offering, upstream, WireProtocol.chat_completions)],
            body={
                "model": "model-a",
                "stream": False,
                "instructions": "You are Codex.",
                "input": [
                    _text_message("user", "hi"),
                    _text_message("developer", "<app-context>x</app-context>"),
                ],
            },
        )
    )

    assert response.status_code == 400
    event = runtime.usage_log.finalized[-1]
    assert "Invalid input" in event.upstream_error_excerpt
    assert "messages.1.content" in event.upstream_error_excerpt
    digest = event.outbound_request_digest
    assert digest["message_count"] == len(client.bodies[0]["messages"])
    assert digest["mid_system_indexes"] == []
    assert digest["empty_content_indexes"] == []
    assert digest["leading_roles"][0] == "system"
