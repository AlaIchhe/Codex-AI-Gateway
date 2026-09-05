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
    raw_messages: list[dict[str, Any]] = []
    for msg in normal.messages:
        if msg.role == "tool":
            raw_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "content": _concat_parts(msg.content),
                }
            )
        elif msg.tool_calls:
            raw_messages.append(
                {
                    "role": msg.role,
                    "content": _concat_parts(msg.content) or None,
                    "tool_calls": msg.tool_calls,
                }
            )
        else:
            chat_role = "system" if msg.role == "developer" else msg.role
            raw_messages.append({"role": chat_role, "content": _concat_parts(msg.content)})
    messages = _merge_and_prune_tool_messages(raw_messages)
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
    if normal.stream:
        # Codex 只认 response.completed 里的 usage 作为计费/终止依据，
        # 上游不带 stream_options.include_usage 时流末尾不会回传 usage。
        stream_options = dict(body.get("stream_options") or {})
        stream_options["include_usage"] = True
        body["stream_options"] = stream_options
    return body


def _merge_and_prune_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """规范化 Chat 消息列表，保证 assistant tool_calls 与 tool 响应相邻。

    Responses 输入可能把文本和 tool call 拆成两个 assistant item，
    或者在带 tool_calls 的 assistant 消息后没有对应的 tool 响应。Chat
    Completions 要求 assistant tool_calls 消息必须紧随同轮 tool 响应，
    这里做两步处理：
    1. 合并相邻 assistant 纯文本和 assistant tool_calls 为单条消息，
       丢弃既无文本也无 tool_calls 的空 assistant 消息
    2. 以 tool_calls 消息为锚点向前扫描，把夹在中间的 assistant 文本
       并入锚点消息、收集 tool 响应，按 call_id 重建合法序列
    """
    # 1) 把相邻的 assistant 纯文本与 tool_calls 合并成单条
    merged: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            merged.append(msg)
            continue
        content = msg.get("content") or None
        calls = msg.get("tool_calls")
        if calls and not content and merged and merged[-1].get("role") == "assistant":
            prev = merged[-1]
            if prev.get("content") and not prev.get("tool_calls"):
                merged[-1] = {**prev, "tool_calls": calls}
                continue
        if content and not calls and merged and merged[-1].get("role") == "assistant":
            prev = merged[-1]
            if prev.get("tool_calls") and not prev.get("content"):
                merged[-1] = {**prev, "content": content}
                continue
        if not calls and not content:
            continue
        merged.append(msg)

    # 以 tool_calls 为锚点重建序列：锚点之间夹带的 assistant 文本并入锚点，
    # tool 响应只保留与保留下的 tool_calls 匹配的部分
    result: list[dict[str, Any]] = []
    i, n = 0, len(merged)
    while i < n:
        msg = merged[i]
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            if msg.get("role") == "tool":
                # 前面没有任何 tool_calls 锚点可承接的孤立 tool 响应
                i += 1
                continue
            result.append(msg)
            i += 1
            continue

        content = msg.get("content")
        calls = list(msg["tool_calls"])
        j = i + 1
        tool_block: list[dict[str, Any]] = []
        responded_ids: set[str] = set()
        while j < n and merged[j].get("role") in ("assistant", "tool"):
            nxt = merged[j]
            if nxt.get("role") == "assistant":
                if nxt.get("tool_calls"):
                    calls.extend(nxt["tool_calls"])
                if nxt.get("content"):
                    content = (content or "") + str(nxt["content"])
                j += 1
                continue
            tool_block.append(nxt)
            if nxt.get("tool_call_id"):
                responded_ids.add(nxt["tool_call_id"])
            j += 1

        kept_calls = [tc for tc in calls if tc.get("id") and tc.get("id") in responded_ids]

        if keps := kept_calls:
            result.append({"role": "assistant", "content": content, "tool_calls": keps})
        elif msg.get("content"):
            result.append({"role": "assistant", "content": content})

        kept_ids = {tc.get("id") for tc in kept_calls}
        result.extend(tm for tm in tool_block if tm.get("tool_call_id") in kept_ids)
        i = j

    return result


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
                if fn.get("name") in normal.custom_tool_names:
                    try:
                        arguments = json.loads(fn.get("arguments") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        arguments = {}
                    input_text = arguments.get("input") if isinstance(arguments, dict) else ""
                    items.append(
                        {
                            "type": "custom_tool_call",
                            "call_id": tc.get("id"),
                            "name": fn.get("name"),
                            "input": input_text if isinstance(input_text, str) else "",
                        }
                    )
                else:
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
        body["tools"] = []
        for tool in normal.tools:
            fn = tool["function"]
            if fn.get("name") in normal.custom_tool_names:
                body["tools"].append(
                    {"type": "custom", "name": fn.get("name"), "description": fn.get("description")}
                )
            else:
                body["tools"].append(
                    {
                        "type": "function",
                        "name": fn.get("name"),
                        "description": fn.get("description"),
                        "parameters": fn.get("parameters"),
                        "strict": fn.get("strict"),
                    }
                )
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


def restore_namespace_tool_name(
    name: str, aliases: dict[str, dict[str, str]] | None
) -> tuple[str, str | None]:
    """把上游平铺的 ns__tool 名称还原为 (子工具名, namespace)。"""
    alias = (aliases or {}).get(name)
    if not alias:
        return name, None
    return str(alias.get("name") or name), str(alias.get("namespace") or "") or None


def response_tool_call_started_event(
    tc_index: int,
    call_id: str,
    name: str,
    *,
    is_custom: bool,
    namespace_tool_aliases: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """流式期间 tool call 首次出现时的 output_item.added 事件。"""
    restored_name, namespace = restore_namespace_tool_name(name, namespace_tool_aliases)
    item: dict[str, Any] = {
        "id": call_id,
        "type": "custom_tool_call" if is_custom else "function_call",
        "call_id": call_id,
        "name": restored_name,
        "status": "in_progress",
    }
    if namespace is not None:
        item["namespace"] = namespace
    if not is_custom:
        item["arguments"] = ""
    return {
        "type": "response.output_item.added",
        "output_index": tc_index + 1,
        "item": item,
    }


def response_content_part_done_events(text: str) -> list[dict[str, Any]]:
    """消息流结束时的 output_text.done + content_part.done 事件对。"""
    return [
        {
            "type": "response.output_text.done",
            "item_id": "msg_placeholder",
            "output_index": 0,
            "content_index": 0,
            "text": text,
        },
        {
            "type": "response.content_part.done",
            "item_id": "msg_placeholder",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": text, "annotations": []},
        },
    ]


def response_function_call_done_events(
    tool_calls: list[dict[str, Any]],
    *,
    custom_tool_names: set[str] | None = None,
    namespace_tool_aliases: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """把累积的 Chat tool_calls 完成态转为 Responses function_call done 事件。"""
    events: list[dict[str, Any]] = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        call_id = tc.get("id") or f"call_{tc.get('index', 0)}"
        name = fn.get("name") or ""
        if name in (custom_tool_names or set()):
            item = _custom_tool_call_item(call_id, name, fn.get("arguments") or "")
            restored_name, namespace = restore_namespace_tool_name(name, namespace_tool_aliases)
            item["name"] = restored_name
            if namespace is not None:
                item["namespace"] = namespace
        else:
            restored_name, namespace = restore_namespace_tool_name(name, namespace_tool_aliases)
            item = {
                "type": "function_call",
                "call_id": call_id,
                "name": restored_name,
                "arguments": fn.get("arguments") or "",
            }
            if namespace is not None:
                item["namespace"] = namespace
        events.append(
            {
                "type": "response.output_item.done",
                "output_index": (tc.get("index") or 0) + 1,
                "item": item,
            }
        )
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


def _coerce_token_count(value: Any) -> int:
    """Codex 将 usage 数值字段解析为 i64，null/字符串必须兜底为 0。"""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def responses_usage_from_chat_usage(provider_usage: dict[str, Any] | None) -> dict[str, Any]:
    """Chat usage -> Responses usage，缺省数值与 details 子对象一律补零。

    Codex 的 ResponseCompleted 解析把数值字段和
    input_tokens_details.cached_tokens / output_tokens_details.reasoning_tokens
    当作必填，透传 null 或缺 details 会导致 invalid type 崩溃。
    """
    usage = provider_usage if isinstance(provider_usage, dict) else {}

    def first_present(*keys: str) -> Any:
        for key in keys:
            value = usage.get(key)
            if value is not None:
                return value
        return None

    prompt_tokens = first_present("prompt_tokens", "input_tokens", "promptTokenCount")
    completion_tokens = first_present(
        "completion_tokens", "output_tokens", "completionTokenCount"
    )
    total_tokens = first_present("total_tokens", "totalTokenCount")
    if total_tokens is None:
        total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)
    prompt_details = (
        usage.get("prompt_tokens_details")
        or usage.get("input_tokens_details")
        or {}
    )
    completion_details = (
        usage.get("completion_tokens_details")
        or usage.get("output_tokens_details")
        or {}
    )
    return {
        "input_tokens": _coerce_token_count(prompt_tokens),
        "output_tokens": _coerce_token_count(completion_tokens),
        "total_tokens": _coerce_token_count(total_tokens),
        "input_tokens_details": {
            "cached_tokens": _coerce_token_count(prompt_details.get("cached_tokens")),
        },
        "output_tokens_details": {
            "reasoning_tokens": _coerce_token_count(
                completion_details.get("reasoning_tokens")
            ),
        },
    }


def response_completed_event(
    *,
    model: str,
    completion_id: str | None = None,
    finish_reason: str | None = None,
    output: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = "incomplete" if finish_reason == "length" else "completed"
    return {
        "type": "response.completed",
        "response": {
            "id": completion_id or "resp_placeholder",
            "object": "response",
            "output": output or [],
            "model": model,
            "status": status,
            "error": None,
            "incomplete_details": (
                {"reason": "max_output_tokens"} if status == "incomplete" else None
            ),
            "usage": responses_usage_from_chat_usage(usage),
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
    custom_tool_names: set[str] | None = None,
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
        if function.get("name") in (custom_tool_names or set()):
            output_items.append(
                _custom_tool_call_item(
                    tool_call.get("id"), function.get("name"), function.get("arguments") or ""
                )
            )
        else:
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
        "usage": responses_usage_from_chat_usage(chat_completion.get("usage")),
    }
    return envelope


def _custom_tool_call_item(call_id: Any, name: str, arguments: str) -> dict[str, Any]:
    """把降级层使用的 {input: string} 调用还原为 Responses custom item。"""
    try:
        parsed = json.loads(arguments)
    except (TypeError, json.JSONDecodeError):
        parsed = {}
    input_value = parsed.get("input") if isinstance(parsed, dict) else ""
    return {
        "id": call_id,
        "type": "custom_tool_call",
        "call_id": call_id,
        "name": name,
        "input": input_value if isinstance(input_value, str) else "",
        "status": "completed",
    }
