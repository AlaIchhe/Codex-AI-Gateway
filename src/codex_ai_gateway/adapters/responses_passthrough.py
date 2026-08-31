"""Responses 协议透传 + 孤儿 delta 修补器。

解决的问题：部分上游（如火山方舟 Coding Plan）的 Responses API 实现不完整，
发送 reasoning_summary_delta / output_text_delta 等事件时缺少前置的
response.output_item.added 生命周期事件，导致 Codex 客户端报
"ReasoningSummaryDelta without active item" / "OutputTextDelta without active item"。

本模块在透传路径上解析 SSE 事件，跟踪 item 生命周期状态，
检测到孤儿 delta 时自动注入缺失的前置事件后再转发给客户端。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from codex_ai_gateway.adapters.sse_stream import SSEFrameBuffer

logger = logging.getLogger(__name__)

# 需要关联到特定 item 类型的 delta 事件前缀
_REASONING_PREFIX = "response.reasoning_summary_"
_MESSAGE_PREFIX = "response.output_text."
_FUNCTION_PREFIX = "response.function_call_arguments."

# 不需要修补的事件类型（直接透传）
_PASSTHROUGH_TYPES = {
    "response.created",
    "response.in_progress",
    "response.completed",
    "response.failed",
    "response.queued",
    "response.output_item.added",
    "response.output_item.done",
    "response.content_part.added",
    "response.content_part.done",
    "response.reasoning_summary_part.added",
    "response.reasoning_summary_part.done",
}


def _encode_sse(event: dict[str, Any]) -> bytes:
    """把事件 dict 编码为标准 SSE 帧。"""
    payload = json.dumps(event, ensure_ascii=False)
    return f"event: {event['type']}\ndata: {payload}\n\n".encode()


class ResponsesPassthrough:
    """Responses 协议透传器：解析 SSE → 跟踪 item 状态 → 修补孤儿 delta → 重编码。"""

    def __init__(self) -> None:
        self.sse_buffer = SSEFrameBuffer()
        # item_id -> item_type ("reasoning" | "message" | "function_call" | ...)
        self._active_items: dict[str, str] = {}
        self._injected_count = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        """喂入上游 bytes chunk，返回修补后的 SSE 帧列表。"""
        events = self.sse_buffer.feed(chunk)
        frames: list[bytes] = []
        for event in events:
            frames.extend(self._process(event))
        return frames

    def flush(self) -> list[bytes]:
        """流结束时冲刷残帧。"""
        events = self.sse_buffer.flush()
        frames: list[bytes] = []
        for event in events:
            frames.extend(self._process(event))
        return frames

    def _process(self, event: dict[str, Any]) -> list[bytes]:
        """处理单个事件，返回（可能含注入前置事件的）SSE 帧列表。"""
        etype = event.get("type", "")

        # Item 生命周期：跟踪 active state
        if etype == "response.output_item.added":
            item = event.get("item") or {}
            item_id = item.get("id", "")
            item_type = item.get("type", "")
            if item_id:
                self._active_items[item_id] = item_type
            return [_encode_sse(event)]

        if etype == "response.output_item.done":
            item = event.get("item") or {}
            item_id = item.get("id", "")
            self._active_items.pop(item_id, None)
            return [_encode_sse(event)]

        # 检测孤儿 delta
        source_item_id = event.get("item_id", "")
        prefix_events = self._check_orphan(etype, source_item_id)
        return [_encode_sse(e) for e in prefix_events] + [_encode_sse(event)]

    def _check_orphan(self, etype: str, source_item_id: str = "") -> list[dict[str, Any]]:
        """检测孤儿 delta 事件，返回需要注入的前置事件列表。"""
        has_reasoning = any(t == "reasoning" for t in self._active_items.values())
        has_message = any(t == "message" for t in self._active_items.values())
        has_function = any(t == "function_call" for t in self._active_items.values())

        if etype.startswith(_REASONING_PREFIX) and not has_reasoning:
            # reasoning_summary_part.added 也需要 reasoning item
            # reasoning_summary_part.done 也是
            return self._inject_item("reasoning", source_item_id=source_item_id, summary_part=True)
        if etype.startswith(_MESSAGE_PREFIX) and not has_message:
            return self._inject_item("message", source_item_id=source_item_id, summary_part=False)
        if etype.startswith(_FUNCTION_PREFIX) and not has_function:
            return self._inject_item("function_call", source_item_id=source_item_id, summary_part=False)
        return []

    def _inject_item(self, item_type: str, *, source_item_id: str = "", summary_part: bool = False) -> list[dict[str, Any]]:
        """生成缺失的 output_item.added（+ content_part.added）事件。

        使用源 delta 事件的 item_id（如果有），否则生成一个新 ID。
        """
        self._injected_count += 1
        item_id = source_item_id if source_item_id else f"injected-{item_type}-{self._injected_count}"
        logger.debug("注入缺失的 output_item.added: type=%s id=%s", item_type, item_id)

        item: dict[str, Any] = {"id": item_id, "type": item_type, "status": "in_progress"}
        if item_type == "message":
            item["role"] = "assistant"
            item["content"] = []
        elif item_type == "reasoning":
            item["summary"] = []
        elif item_type == "function_call":
            item["call_id"] = ""
            item["name"] = ""
            item["arguments"] = ""

        events: list[dict[str, Any]] = [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": item,
            }
        ]

        # 对于 reasoning 类型，还需要注入 reasoning_summary_part.added
        # 因为上游可能直接发 reasoning_summary_delta 而不先发 part.added
        if item_type == "reasoning":
            events.append({
                "type": "response.reasoning_summary_part.added",
                "item_id": item_id,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": ""},
            })

        # 对于 message 类型，注入 content_part.added
        if item_type == "message":
            events.append({
                "type": "response.content_part.added",
                "item_id": item_id,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            })

        # 注册为 active
        self._active_items[item_id] = item_type
        return events
