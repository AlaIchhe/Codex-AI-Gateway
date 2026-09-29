"""孤儿 tool_call 的丢弃告警 + reasoning 回放缓存回归测试。

背景（用户要求）：网关在翻译 Codex 历史时若发现「有 tool_call 却没有工具结果」，
必须丢弃该 tool_call（严格上游否则直接 400）。但静默丢弃会让 MCP / skill / 插件
的失败在 Codex 侧完全不可见——用户只看到「模型突然不提这件事了」。所以丢弃必须
留痕：history_hygiene 字段 + 日志 + 响应里一条给用户看的告警消息。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from codex_ai_gateway.adapters.protocol_normal_form import normalize_request
from codex_ai_gateway.adapters.responses_chat_translation import (
    HistoryHygiene,
    chat_request_from_normal,
    relocate_tool_outputs,
    response_hygiene_message_events,
    response_hygiene_message_item,
    strip_reasoning_content,
)
from codex_ai_gateway.api.gateway import (
    _reasoning_replay_error,
    _remember_reasoning_from_chat_completion,
    _stream_response,
)
from codex_ai_gateway.domain.circuit_breaker import CircuitBreaker
from codex_ai_gateway.domain.error_mapping import _provider_error_code, map_provider_error
from codex_ai_gateway.domain.reasoning_cache import ReasoningReplayCache
from codex_ai_gateway.models.entities import (
    CanonicalModel,
    Offering,
    OfferingStatus,
    Upstream,
    WireProtocol,
)


def _normal(items: list[dict[str, Any]]):
    return normalize_request(
        inbound_protocol="responses",
        body={"model": "m", "input": items},
    )


# --------------------------------------------------------------------------- #
# 1. 孤儿 tool_call 的丢弃与告警
# --------------------------------------------------------------------------- #
def test_orphan_tool_call_is_dropped_and_recorded() -> None:
    """有 call 没有对应输出：call 被丢，但 call_id / 工具名要留痕。"""
    hygiene = HistoryHygiene()
    body = chat_request_from_normal(
        _normal(
            [
                {"role": "user", "content": "go"},
                {
                    "type": "function_call",
                    "call_id": "call_boom",
                    "name": "mcp__context7__query_docs",
                    "arguments": "{}",
                },
                {"role": "user", "content": "next"},
            ]
        ),
        target_model="provider-model",
        hygiene=hygiene,
    )
    # 上游请求里不能出现这个悬空的 tool_call。
    assert not any(m.get("tool_calls") for m in body["messages"])
    assert hygiene.dropped_tool_calls == [
        {"call_id": "call_boom", "name": "mcp__context7__query_docs"}
    ]
    assert hygiene.has_dropped_tool_calls
    assert hygiene.digest()["dropped_tool_calls"] == [
        {"call_id": "call_boom", "name": "mcp__context7__query_docs"}
    ]


def test_notice_text_names_the_failed_mcp_tool() -> None:
    hygiene = HistoryHygiene(
        dropped_tool_calls=[
            {"call_id": "call_1", "name": "mcp__context7__query_docs"},
            {"call_id": "call_2", "name": None},
        ]
    )
    notice = hygiene.notice_text()
    assert notice is not None
    assert "mcp__context7__query_docs" in notice
    assert "call_1" in notice
    # 未命名工具也要能指出 call_id，而不是整条消息消失。
    assert "call_2" in notice


def test_healthy_history_produces_no_notice() -> None:
    hygiene = HistoryHygiene(
        dropped_tool_calls=[],
        orphan_tool_outputs=[],
        relocated_tool_outputs=2,
        blank_assistant_messages=1,
    )
    assert hygiene.notice_text() is None
    assert not hygiene.has_dropped_tool_calls


def test_orphan_tool_output_is_recorded() -> None:
    hygiene = HistoryHygiene()
    chat_request_from_normal(
        _normal(
            [
                {"role": "user", "content": "go"},
                {"type": "function_call_output", "call_id": "call_ghost", "output": "x"},
            ]
        ),
        target_model="provider-model",
        hygiene=hygiene,
    )
    assert hygiene.orphan_tool_outputs == ["call_ghost"]
    assert hygiene.has_dropped_tool_calls
    assert "call_ghost" in (hygiene.notice_text() or "")


# --------------------------------------------------------------------------- #
# 2. reasoning 回放缓存
# --------------------------------------------------------------------------- #
def test_reasoning_cache_put_get_and_missing() -> None:
    cache = ReasoningReplayCache()
    cache.put("call_1", "real reasoning")
    assert cache.get("call_1") == "real reasoning"
    assert cache.get("call_unknown") is None
    # 空值不入缓存。
    cache.put("call_2", "   ")
    assert cache.get("call_2") is None
    cache.put(None, "x")
    assert len(cache) == 1


def test_reasoning_cache_expires_after_ttl() -> None:
    now = [0.0]
    cache = ReasoningReplayCache(ttl_seconds=10.0, clock=lambda: now[0])
    cache.put("call_1", "value")
    assert cache.get("call_1") == "value"
    now[0] = 11.0
    assert cache.get("call_1") is None
    assert len(cache) == 0


def test_reasoning_cache_evicts_lru_by_entry_limit() -> None:
    cache = ReasoningReplayCache(max_entries=2)
    cache.put("a", "1")
    cache.put("b", "2")
    assert cache.get("a") == "1"  # 触碰 a，a 变成最近使用
    cache.put("c", "3")
    assert cache.get("b") is None
    assert cache.get("a") == "1"
    assert cache.get("c") == "3"


def test_reasoning_cache_evicts_by_total_bytes() -> None:
    cache = ReasoningReplayCache(max_entries=100, max_total_bytes=20)
    cache.put("a", "x" * 12)
    cache.put("b", "y" * 12)
    # 第二条挤掉第一条，总量始终不超过上限。
    assert cache.get("a") is None
    assert cache.get("b") == "y" * 12
    assert cache.total_bytes() <= 20
    # 单条就超上限的直接拒收。
    cache.put("c", "z" * 100)
    assert cache.get("c") is None


def test_reasoning_cache_replay_used_when_available() -> None:
    """缓存命中时，占位符要让位给真实 reasoning。"""
    cache = ReasoningReplayCache()
    cache.put("call_1", "真实的思考内容")
    hygiene = HistoryHygiene()
    body = chat_request_from_normal(
        _normal(
            [
                {"role": "user", "content": "go"},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "shell_command",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
            ]
        ),
        target_model="provider-model",
        hygiene=hygiene,
        reasoning_cache=cache,
    )
    assistant = next(m for m in body["messages"] if m.get("tool_calls"))
    assert assistant["reasoning_content"] == "真实的思考内容"


def test_reasoning_cache_miss_falls_back_to_placeholder() -> None:
    cache = ReasoningReplayCache()
    body = chat_request_from_normal(
        _normal(
            [
                {"role": "user", "content": "go"},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "shell_command",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
            ]
        ),
        target_model="provider-model",
        reasoning_cache=cache,
    )
    assistant = next(m for m in body["messages"] if m.get("tool_calls"))
    assert assistant["reasoning_content"] == "Calling the requested tool."


def test_remember_reasoning_from_chat_completion() -> None:
    cache = ReasoningReplayCache()
    runtime = SimpleNamespace(reasoning_cache=cache)
    payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "upstream thought",
                    "tool_calls": [
                        {"id": "call_9", "type": "function", "function": {"name": "f"}}
                    ],
                }
            }
        ]
    }
    _remember_reasoning_from_chat_completion(
        runtime, json.dumps(payload).encode(), WireProtocol.chat_completions
    )
    assert cache.get("call_9") == "upstream thought"
    # 非 chat 协议不缓存。
    _remember_reasoning_from_chat_completion(
        runtime, json.dumps(payload).encode(), WireProtocol.responses
    )
    assert len(cache) == 1


# --------------------------------------------------------------------------- #
# 3. reasoning 相关的 fail-soft 判定必须窄
# --------------------------------------------------------------------------- #
def test_strip_reasoning_content_only_reports_real_change() -> None:
    body: dict[str, Any] = {
        "messages": [
            {"role": "assistant", "content": "hi", "reasoning_content": "thought"},
            {"role": "user", "content": "next"},
        ]
    }
    assert strip_reasoning_content(body) is True
    assert "reasoning_content" not in body["messages"][0]
    # 已经剥干净了，第二次不该再返回 True（否则会白重试）。
    assert strip_reasoning_content(body) is False
    assert strip_reasoning_content({"messages": [{"role": "user", "content": "x"}]}) is False


def test_reasoning_replay_error_is_narrow() -> None:
    assert _reasoning_replay_error(
        400, b'{"error":{"message":"The reasoning_content must be passed back"}}'
    )
    assert _reasoning_replay_error(400, b'{"error":{"message":"encrypted content"}}')
    # 非 400 不触发。
    assert not _reasoning_replay_error(
        401, b'{"error":{"message":"reasoning not allowed"}}'
    )
    # 与 reasoning 无关的 400 不触发：不能把真正的请求错误吞掉。
    assert not _reasoning_replay_error(
        400, b'{"error":{"message":"Invalid input","param":"messages.3.content"}}'
    )
    assert not _reasoning_replay_error(400, b"")


# --------------------------------------------------------------------------- #
# 4. Responses 工具结果邻接归一化
# --------------------------------------------------------------------------- #
def _call(call_id: str, name: str = "f") -> dict[str, Any]:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": "{}"}


def _output(call_id: str) -> dict[str, Any]:
    return {"type": "function_call_output", "call_id": call_id, "output": "ok"}


def test_relocate_moves_output_next_to_its_call() -> None:
    items = [
        {"role": "user", "content": "go"},
        _call("c1"),
        {"role": "developer", "content": "injected between call and result"},
        _output("c1"),
    ]
    relocated = relocate_tool_outputs(items)
    assert [i.get("call_id") for i in relocated if "call_id" in i] == ["c1", "c1"]
    assert relocated.index(_output("c1")) == relocated.index(_call("c1")) + 1


def test_relocate_keeps_parallel_calls_together() -> None:
    items = [_call("c1"), _call("c2"), {"role": "user", "content": "x"}, _output("c1"), _output("c2")]
    relocated = relocate_tool_outputs(items)
    # 批量归一：两个 call 相邻，两个 output 相邻，且紧跟其后。
    assert [i.get("call_id") for i in relocated[:4]] == ["c1", "c2", "c1", "c2"]


def test_relocate_refuses_when_ambiguous() -> None:
    # 重复 call_id：不猜测。
    duplicated = [_call("c1"), _call("c1"), _output("c1")]
    assert relocate_tool_outputs(duplicated) == duplicated
    # 结果出现在调用之前：不搬。
    output_first = [_output("c1"), _call("c1")]
    assert relocate_tool_outputs(output_first) == output_first
    # 需要的输出与调用之间夹了另一组调用：不搬（无歧义前提不成立）。
    sandwiched = [_call("c1"), _output("c2"), _call("c2"), _output("c1")]
    assert relocate_tool_outputs(sandwiched) == sandwiched
    # 结果缺失：不搬。
    missing = [_call("c1"), _call("c2"), _output("c2")]
    assert relocate_tool_outputs(missing) == missing


def test_relocate_records_hygiene_count() -> None:
    hygiene = HistoryHygiene()
    relocate_tool_outputs(
        [_call("c1"), {"role": "user", "content": "x"}, _output("c1")],
        hygiene=hygiene,
    )
    assert hygiene.relocated_tool_outputs == 1


# --------------------------------------------------------------------------- #
# 5. 顶层错误码形状（Novita 风格）
# --------------------------------------------------------------------------- #
def test_top_level_provider_error_shape_is_captured() -> None:
    # 顶层形状（Novita / 部分兼容层）：错误码不在 error 包装里。
    assert (
        _provider_error_code(
            b'{"message":"bad request","type":"invalid_request_error","trace_id":"abc"}'
        )
        == "invalid_request_error"
    )
    assert _provider_error_code(b'{"code":"MODEL_NOT_IN_PLAN","message":"x"}') == (
        "model_not_in_plan"
    )
    # error 包装仍然优先。
    assert (
        _provider_error_code(b'{"error":{"code":"context_length_exceeded"},"code":"other"}')
        == "context_length_exceeded"
    )
    # 明文错误字符串也要能当错误码用上。
    assert _provider_error_code(b'{"error":"rate limited"}') == "rate limited"


def test_plain_text_error_body_is_still_mapped() -> None:
    mapped = map_provider_error(400, body=b"Invalid input", upstream_name="u1")
    assert mapped["error_mapping_code"] == "provider_invalid_request"


# --------------------------------------------------------------------------- #
# 6. 流式路径注入告警消息
# --------------------------------------------------------------------------- #
class _FakeStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.status_code = 200
        self.headers: dict[str, str] = {}

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> bytes:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def aclose(self) -> None:
        return None


class _UsageLog:
    def __init__(self) -> None:
        self.pending: list[Any] = []
        self.finalized: list[Any] = []

    def create_pending(self, event: Any) -> None:
        self.pending.append(event)

    def record_finalized(self, event: Any) -> None:
        self.finalized.append(event)


def _stream_runtime() -> SimpleNamespace:
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
        reasoning_cache=ReasoningReplayCache(),
    )


def _stream_offering() -> Offering:
    return Offering(
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


def _stream_upstream() -> Upstream:
    return Upstream(
        id="u1",
        name="u1",
        base_url="https://u1.example.com/v1",
        auth_credential_ref="upstream:u1:api_credential",
        created_at="2026-08-30T00:00:00+00:00",
        updated_at="2026-08-30T00:00:00+00:00",
    )


async def _collect_stream_response(hygiene: HistoryHygiene | None) -> list[bytes]:
    runtime = _stream_runtime()
    event = SimpleNamespace(
        canonical_model_label="model-a",
        provider_model_id="model-a",
        start_monotonic=0.0,
    )
    upstream_stream = _FakeStream(
        [
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            b"data: [DONE]\n\n",
        ]
    )
    response = await _stream_response(
        request=SimpleNamespace(headers={}),
        runtime=runtime,
        event=event,
        upstream=_stream_upstream(),
        upstream_stream=upstream_stream,
        protocol=WireProtocol.chat_completions,
        offering=_stream_offering(),
        hygiene=hygiene,
    )
    return [chunk async for chunk in response.body_iterator]


def test_stream_injects_hygiene_notice_when_tool_call_dropped() -> None:
    hygiene = HistoryHygiene(
        dropped_tool_calls=[
            {"call_id": "call_x", "name": "mcp__context7__query_docs"}
        ]
    )
    frames = asyncio.run(_collect_stream_response(hygiene))
    blob = b"".join(frames).decode()
    assert "msg_gateway_hygiene" in blob
    assert "mcp__context7__query_docs" in blob
    assert "call_x" in blob


def test_stream_without_hygiene_has_no_notice() -> None:
    frames = asyncio.run(_collect_stream_response(None))
    blob = b"".join(frames).decode()
    assert "msg_gateway_hygiene" not in blob


def test_response_hygiene_item_shape() -> None:
    events = response_hygiene_message_events("msg_x", 3, "notice")
    assert [e["type"] for e in events] == [
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
    ]
    assert all(e.get("output_index") == 3 for e in events)
    item = response_hygiene_message_item("msg_x", "notice")
    assert item["role"] == "assistant"
    assert item["content"][0]["text"] == "notice"
