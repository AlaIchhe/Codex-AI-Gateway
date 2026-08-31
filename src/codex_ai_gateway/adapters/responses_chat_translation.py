"""Responses 与 Chat Completions 双向翻译器。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from codex_ai_gateway.adapters.protocol_normal_form import NormalRequest


def chat_request_from_normal(normal: NormalRequest, *, target_model: str) -> dict[str, Any]:
    """NormalRequest -> Chat Completions request body。"""
    messages = []
    if normal.extra.get("instructions"):
        messages.append({"role": "system", "content": str(normal.extra["instructions"])})
    for msg in normal.messages:
        if msg.role == "tool":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "content": _concat_parts(msg.content),
                }
            )
        elif msg.tool_calls:
            messages.append(
                {
                    "role": msg.role,
                    "content": _concat_parts(msg.content) or None,
                    "tool_calls": msg.tool_calls,
                }
            )
        else:
            chat_role = "system" if msg.role == "developer" else msg.role
            messages.append({"role": chat_role, "content": _concat_parts(msg.content)})
    body: dict[str, Any] = {
        "model": target_model,
        "messages": messages,
        "stream": normal.stream,
    }
    for key, value in normal.sampling.items():
        if key == "max_tokens":
            body["max_tokens"] = value
        else:
            body[key] = value
    if normal.tools:
        body["tools"] = normal.tools
    if normal.tool_choice is not None:
        body["tool_choice"] = normal.tool_choice
    return body


def responses_request_from_normal(normal: NormalRequest, *, target_model: str) -> dict[str, Any]:
    """NormalRequest -> Responses request body。"""
    items: list[Any] = []
    for msg in normal.messages:
        if msg.role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": msg.tool_call_id,
                    "output": _concat_parts(msg.content),
                }
            )
        elif msg.tool_calls:
            for tc in msg.tool_calls:
                fn = tc.get("function", {})
                items.append(
                    {
                        "type": "function_call",
                        "call_id": tc.get("id"),
                        "name": fn.get("name"),
                        "arguments": fn.get("arguments"),
                    }
                )
        else:
            items.append(
                {
                    "type": "message",
                    "role": msg.role,
                    "content": [{"type": "input_text", "text": _concat_parts(msg.content)}],
                }
            )
    body: dict[str, Any] = {
        "model": target_model,
        "input": items,
        "stream": normal.stream,
    }
    for key, value in normal.sampling.items():
        if key == "max_tokens":
            body["max_output_tokens"] = value
        else:
            body[key] = value
    if normal.tools:
        body["tools"] = [
            {
                "type": "function",
                "name": tool["function"].get("name"),
                "description": tool["function"].get("description"),
                "parameters": tool["function"].get("parameters"),
                "strict": tool["function"].get("strict"),
            }
            for tool in normal.tools
        ]
    if normal.tool_choice is not None:
        body["tool_choice"] = normal.tool_choice
    return body


def _concat_parts(parts: list[dict[str, Any]]) -> str:
    return "".join(str(p.get("text", "")) for p in parts)


def iter_response_events(normal: NormalRequest, body: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """构造 Responses lifecycle 事件骨架（固定五段，中间段可为空）。"""
    model = normal.model
    event_id = body.get("id") or "resp_placeholder"
    yield {
        "type": "response.created",
        "response": {
            "id": event_id,
            "object": "response",
            "model": model,
            "status": "in_progress",
        },
    }
    yield {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {
            "id": "msg_placeholder",
            "type": "message",
            "status": "in_progress",
            "role": "assistant",
            "content": [],
        },
    }
    yield {
        "type": "response.content_part.added",
        "item_id": "msg_placeholder",
        "part_index": 0,
        "part": {"type": "output_text", "text": "", "annotations": []},
    }
    # text delta 由调用方注入，这里返回空骨架
    yield {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {
            "id": "msg_placeholder",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "", "annotations": []}],
        },
    }
    yield {
        "type": "response.completed",
        "response": {
            "id": event_id,
            "object": "response",
            "model": model,
            "status": "completed",
        },
    }


def translate_chat_chunk_to_response_event(chunk: dict[str, Any]) -> dict[str, Any] | None:
    """将一个 Chat Completions streaming delta 转换为 Responses 文本 delta 事件。"""
    choices = chunk.get("choices") or []
    if not choices:
        return None
    choice = choices[0]
    delta = choice.get("delta") or {}
    content = delta.get("content")
    if content:
        return {
            "type": "response.output_text.delta",
            "item_id": "msg_placeholder",
            "output_index": 0,
            "content_index": 0,
            "delta": content,
        }
    return None


def response_sse(event: dict[str, Any]) -> bytes:
    """把 Responses 事件编码为标准 SSE 帧。"""
    payload = json.dumps(event, ensure_ascii=False)
    return f"event: {event['type']}\ndata: {payload}\n\n".encode()


def response_created_event(model: str) -> dict[str, Any]:
    return {
        "type": "response.created",
        "response": {
            "id": "resp_placeholder",
            "object": "response",
            "model": model,
            "status": "in_progress",
        },
    }


def response_message_started_events() -> list[dict[str, Any]]:
    item_id = "msg_placeholder"
    return [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": item_id,
                "type": "message",
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            },
        },
        {
            "type": "response.content_part.added",
            "item_id": item_id,
            "part_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
    ]


def response_function_call_done_events(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把累积的 Chat tool_calls 完成态转为 Responses function_call done 事件。"""
    events: list[dict[str, Any]] = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        events.append({
            "type": "response.output_item.done",
            "output_index": (tc.get("index") or 0) + 1,
            "item": {
                "id": tc.get("id") or f"call_{tc.get('index', 0)}",
                "type": "function_call",
                "call_id": tc.get("id") or f"call_{tc.get('index', 0)}",
                "name": fn.get("name") or "",
                "arguments": fn.get("arguments") or "",
                "status": "completed",
            },
        })
    return events


def response_message_done_event(text: str) -> dict[str, Any]:
    return {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {
            "id": "msg_placeholder",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        },
    }


def response_failed_event(model: str, error_message: str) -> dict[str, Any]:
    """mid-stream 错误时通知客户端。"""
    return {
        "type": "response.failed",
        "response": {
            "id": "resp_placeholder",
            "object": "response",
            "model": model,
            "status": "failed",
            "error": {
                "code": "upstream_error",
                "message": error_message,
            },
        },
    }


def response_completed_event(
    *,
    model: str,
    completion_id: str | None = None,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider_usage = usage or {}
    status = "incomplete" if finish_reason == "length" else "completed"
    return {
        "type": "response.completed",
        "response": {
            "id": completion_id or "resp_placeholder",
            "object": "response",
            "model": model,
            "status": status,
            "error": None,
            "incomplete_details": (
                {"reason": "max_output_tokens"} if status == "incomplete" else None
            ),
            "usage": {
                "input_tokens": provider_usage.get("prompt_tokens"),
                "output_tokens": provider_usage.get("completion_tokens"),
                "total_tokens": provider_usage.get("total_tokens"),
            },
        },
    }


def parse_chat_sse_frame(frame: str) -> dict[str, Any] | None:
    """解析一个 Chat Completions SSE 帧；注释、空帧和 DONE 返回 None。"""
    data_lines = []
    for line in frame.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if not data_lines:
        return None
    payload = "\n".join(data_lines)
    if not payload or payload == "[DONE]":
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def response_envelope_from_chat_completion(
    chat_completion: dict[str, Any],
    *,
    requested_model: str,
) -> dict[str, Any]:
    """把非流式 Chat Completions 信封转换回 Responses 信封。"""
    choices = chat_completion.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason")

    output_items: list[dict[str, Any]] = []
    tool_calls = message.get("tool_calls") or []
    for tool_call in tool_calls:
        function = tool_call.get("function") or {}
        output_items.append(
            {
                "type": "function_call",
                "id": tool_call.get("id"),
                "call_id": tool_call.get("id"),
                "name": function.get("name"),
                "arguments": function.get("arguments"),
                "status": "completed",
            }
        )
    content_text = message.get("content")
    if content_text:
        output_items.append(
            {
                "id": f"msg_{chat_completion.get('id')}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": content_text,
                        "annotations": [],
                    }
                ],
            }
        )

    usage = chat_completion.get("usage") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    status = "incomplete" if finish_reason == "length" else "completed"
    envelope: dict[str, Any] = {
        "id": chat_completion.get("id"),
        "object": "response",
        "created_at": chat_completion.get("created"),
        "status": status,
        "model": requested_model,
        "output": output_items,
        "parallel_tool_calls": True,
        "tool_choice": chat_completion.get("tool_choice", "auto"),
        "tools": chat_completion.get("tools", []),
        "error": None,
        "incomplete_details": ({"reason": "max_output_tokens"} if status == "incomplete" else None),
        "usage": {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "input_tokens_details": {
                "cached_tokens": prompt_details.get("cached_tokens", 0),
            },
            "output_tokens_details": {
                "reasoning_tokens": completion_details.get("reasoning_tokens"),
            },
        },
    }
    return envelope
