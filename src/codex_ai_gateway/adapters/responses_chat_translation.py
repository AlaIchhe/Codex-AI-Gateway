"""Responses 与 Chat Completions 双向翻译器。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from codex_ai_gateway.adapters.protocol_normal_form import NormalRequest

# Responses 里表达「模型要调工具」与「工具结果」的 item 类型。
_CALL_ITEM_TYPES = frozenset({"function_call", "custom_tool_call"})
_OUTPUT_ITEM_TYPES = frozenset({"function_call_output", "custom_tool_call_output"})


@dataclass
class HistoryHygiene:
    """记录历史在翻译阶段被修复/丢弃的东西。

    Codex 的历史里出现「有 tool_call 没有工具结果」时，网关必须丢弃这个
    tool_call（见 ``_merge_and_prune_tool_messages``），否则严格上游直接 400。
    但静默丢弃会让用户完全看不到是哪一环断了——MCP 工具、skill、插件在 Codex
    侧看起来都只是「模型突然不提这件事了」。所以丢弃必须留痕：服务端 warning
    日志 + UsageEvent 字段 + 响应里给用户看的告警文本。
    """

    # [{"call_id": "call_x", "name": "mcp__context7__query_docs"}]，按出现顺序。
    dropped_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # 有工具结果却没有对应 tool_call 的孤立结果，通常是历史被裁剪过。
    orphan_tool_outputs: list[str] = field(default_factory=list)
    # 被搬到调用正后方的工具结果数量（Responses 邻接归一化）。
    relocated_tool_outputs: int = 0
    # 既无文本也无 tool_calls 的空 assistant item（Codex 纯工具回合的常见形态）。
    blank_assistant_messages: int = 0

    @property
    def has_dropped_tool_calls(self) -> bool:
        return bool(self.dropped_tool_calls or self.orphan_tool_outputs)

    @property
    def is_empty(self) -> bool:
        return not (
            self.dropped_tool_calls
            or self.orphan_tool_outputs
            or self.relocated_tool_outputs
            or self.blank_assistant_messages
        )

    def digest(self) -> dict[str, Any]:
        return {
            "dropped_tool_calls": self.dropped_tool_calls,
            "orphan_tool_outputs": self.orphan_tool_outputs,
            "relocated_tool_outputs": self.relocated_tool_outputs,
            "blank_assistant_messages": self.blank_assistant_messages,
        }

    def notice_text(self) -> str | None:
        """给 Codex 用户看的告警文本；没有丢弃时返回 None。"""
        if not self.has_dropped_tool_calls:
            return None
        lines = [
            "⚠️ [codex-ai-gateway] 历史里有工具调用没有配对的工具结果，已从本次"
            "发往上游的请求中丢弃。常见原因是 MCP / skill / 插件执行失败或被中断，"
            "也可能是上下文压缩丢掉了结果：",
        ]
        for call in self.dropped_tool_calls:
            name = str(call.get("name") or "").strip() or "(未命名工具)"
            call_id = str(call.get("call_id") or "").strip() or "未知"
            lines.append(f"- {name}（call_id={call_id}）")
        for call_id in self.orphan_tool_outputs:
            lines.append(f"- 孤立工具结果（call_id={call_id}）没有对应的调用")
        lines.append("这些调用不会出现在发给上游的请求里；需要继续请重新发起该工具调用。")
        return "\n".join(lines)


def _call_name(tool_call: dict[str, Any]) -> str | None:
    function = tool_call.get("function") or {}
    name = function.get("name") if isinstance(function, dict) else None
    return str(name) if name else None


def chat_request_from_normal(
    normal: NormalRequest,
    *,
    target_model: str,
    hygiene: HistoryHygiene | None = None,
    reasoning_cache: Any = None,
) -> dict[str, Any]:
    """NormalRequest -> Chat Completions request body。"""
    raw_messages: list[dict[str, Any]] = []
    # 入站 Responses 的 instructions 就是 Codex 的系统提示词。它必须进 raw_messages，
    # 否则后面 messages = _merge_and_prune_tool_messages(raw_messages) 会把它整个丢掉，
    # chat 上游只剩历史里的 developer 片段。
    if normal.extra.get("instructions"):
        raw_messages.append(
            {"role": "system", "content": str(normal.extra["instructions"])}
        )
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
            assistant: dict[str, Any] = {
                "role": msg.role,
                "content": _concat_parts(msg.content) or None,
                "tool_calls": msg.tool_calls,
            }
            if msg.role == "assistant" and msg.reasoning_content:
                assistant["reasoning_content"] = msg.reasoning_content
            raw_messages.append(assistant)
        else:
            chat_role = "system" if msg.role == "developer" else msg.role
            assistant_or_user = {"role": chat_role, "content": _concat_parts(msg.content)}
            if chat_role == "assistant" and msg.reasoning_content:
                assistant_or_user["reasoning_content"] = msg.reasoning_content
            raw_messages.append(assistant_or_user)
    messages: list[dict[str, Any]] = _merge_and_prune_tool_messages(
        raw_messages, hygiene=hygiene, reasoning_cache=reasoning_cache
    )
    # 全空历史（例如整段对话只剩被丢弃的空 user 消息，或只有 instructions）会让
    # 上游报 "messages must not be empty"，补一条最小占位用户消息。
    if not any(m.get("role") in {"user", "assistant", "tool"} for m in messages):
        messages = [*messages, {"role": "user", "content": "(empty message)"}]
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


def _ensure_tool_call_reasoning_content(
    messages: list[dict[str, Any]], *, reasoning_cache: Any = None
) -> None:
    """DeepSeek thinking 模式要求带 tool_calls 的 assistant 消息回传 reasoning_content。

    当网关从 Responses 历史转换出的 tool_call 消息 content 和 reasoning_content
    都为空时，补一个占位 reasoning，避免上游 400：
    The `reasoning_content` in the thinking mode must be passed back to the API.

    有回放缓存时优先用**真实** reasoning（上一轮网关亲自回传给 Codex 的那段），
    占位符只是缓存未命中时的兜底：占位符能过格式校验，但还原不了思考内容。
    """
    for message in messages:
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        has_content = bool(str(message.get("content") or "").strip())
        has_reasoning = bool(str(message.get("reasoning_content") or "").strip())
        if has_content or has_reasoning:
            continue
        message["reasoning_content"] = _replayed_reasoning(
            message.get("tool_calls"), reasoning_cache
        ) or "Calling the requested tool."


def _replayed_reasoning(tool_calls: list[dict[str, Any]], reasoning_cache: Any) -> str | None:
    """按 call_id 从回放缓存里取回真实 reasoning。"""
    if reasoning_cache is None:
        return None
    get = getattr(reasoning_cache, "get", None)
    if get is None:
        return None
    for tool_call in tool_calls:
        call_id = tool_call.get("id") if isinstance(tool_call, dict) else None
        if not call_id:
            continue
        try:
            text = get(str(call_id))
        except Exception:  # noqa: BLE001 - 缓存只是尽力而为，坏了不能影响请求
            return None
        if isinstance(text, str) and text.strip():
            return text
    return None


def strip_reasoning_content(body: dict[str, Any]) -> bool:
    """剥掉出站 Chat 请求里的全部 reasoning_content，返回是否真的改动了。

    用于上游对 reasoning 回放过敏时的 fail-soft 重试：参考 bifrost
    ``shouldStripReasoningAfterClientError`` / ``stripUnverifiableReasoning``，
    只有**真的剥掉了东西**才值得重发一次，否则重试只是把同一个 400 再打一遍。
    """
    changed = False
    for message in body.get("messages") or []:
        if isinstance(message, dict) and message.pop("reasoning_content", None) is not None:
            changed = True
    return changed


def _merge_and_prune_tool_messages(
    messages: list[dict[str, Any]],
    *,
    hygiene: HistoryHygiene | None = None,
    reasoning_cache: Any = None,
) -> list[dict[str, Any]]:
    """规范化 Chat 消息列表，保证 assistant tool_calls 与 tool 响应相邻。

    Responses 输入可能把文本和 tool call 拆成两个 assistant item，
    或者在带 tool_calls 的 assistant 消息后没有对应的 tool 响应。Chat
    Completions 要求 assistant tool_calls 消息必须紧随同轮 tool 响应，
    这里做两步处理：
    1. 合并相邻 assistant 纯文本和 assistant tool_calls 为单条消息，
       丢弃既无文本也无 tool_calls 的空 assistant 消息
    2. 以 tool_calls 消息为锚点向前扫描，把夹在中间的 assistant 文本
       并入锚点消息、收集 tool 响应，按 call_id 重建合法序列

    被丢弃的 tool_call / 孤立 tool 响应会记进 ``hygiene``：一次静默丢弃在
    Codex 侧表现为「模型不理那个工具了」，用户无从知道是 MCP / skill /
    插件的哪一次调用断了。
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
                if prev.get("reasoning_content") is None and msg.get("reasoning_content"):
                    merged[-1]["reasoning_content"] = msg.get("reasoning_content")
                continue
        if content and not calls and merged and merged[-1].get("role") == "assistant":
            prev = merged[-1]
            if prev.get("tool_calls") and not prev.get("content"):
                merged[-1] = {**prev, "content": content}
                if prev.get("reasoning_content") is None and msg.get("reasoning_content"):
                    merged[-1]["reasoning_content"] = msg.get("reasoning_content")
                continue
        if not calls and not content:
            if hygiene is not None:
                hygiene.blank_assistant_messages += 1
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
                if hygiene is not None and msg.get("tool_call_id"):
                    hygiene.orphan_tool_outputs.append(str(msg["tool_call_id"]))
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
                if msg.get("reasoning_content") is None and nxt.get("reasoning_content"):
                    msg["reasoning_content"] = nxt["reasoning_content"]
                j += 1
                continue
            tool_block.append(nxt)
            if nxt.get("tool_call_id"):
                responded_ids.add(nxt["tool_call_id"])
            j += 1

        kept_calls = [tc for tc in calls if tc.get("id") and tc.get("id") in responded_ids]
        if hygiene is not None:
            for tool_call in calls:
                call_id = tool_call.get("id")
                if call_id and call_id in responded_ids:
                    continue
                hygiene.dropped_tool_calls.append(
                    {
                        "call_id": str(call_id) if call_id else None,
                        "name": _call_name(tool_call) if isinstance(tool_call, dict) else None,
                    }
                )

        if keps := kept_calls:
            kept_msg = {"role": "assistant", "content": content, "tool_calls": keps}
            if msg.get("reasoning_content"):
                kept_msg["reasoning_content"] = msg["reasoning_content"]
            result.append(kept_msg)
        elif msg.get("content"):
            kept_text = {"role": "assistant", "content": content}
            if msg.get("reasoning_content"):
                kept_text["reasoning_content"] = msg["reasoning_content"]
            result.append(kept_text)

        kept_ids = {tc.get("id") for tc in kept_calls}
        result.extend(tm for tm in tool_block if tm.get("tool_call_id") in kept_ids)
        i = j

    _ensure_tool_call_reasoning_content(result, reasoning_cache=reasoning_cache)
    _fold_system_messages(result)
    return result


def _fold_system_messages(messages: list[dict[str, Any]]) -> None:
    """把非开头的 system/developer 消息折进开头那条 system，并丢掉空消息。

    历史跨上游/跨模型复用时（典型场景：在一个对话里换过 provider），
    Codex 会在**历史中段**重新带上 ``<app-context>`` / ``<skills_instructions>``
    这类 developer 片段。Responses 允许中段 developer item，翻译成 Chat 后却是
    中段 ``role=system``——DeepSeek / 方舟 / Gemini-OpenAI 兼容层等严格实现会
    直接 400（``Invalid input`` / ``param: messages.N.content``），把整段对话判死。
    这里统一保留一条开头 system，其余 system 文本按原顺序并入它。
    """
    system_indexes = [i for i, m in enumerate(messages) if m.get("role") == "system"]
    if system_indexes:
        texts = [
            str(messages[i].get("content") or "").strip()
            for i in system_indexes
        ]
        merged_text = "\n\n".join(text for text in texts if text)
        for i in reversed(system_indexes):
            messages.pop(i)
        if merged_text:
            messages.insert(0, {"role": "system", "content": merged_text})

    # 空 content 的非 assistant 消息同样是严格的 4xx 来源（messages.N.content）。
    for index in reversed(range(len(messages))):
        message = messages[index]
        role = message.get("role")
        if role == "tool" and not str(message.get("content") or "").strip():
            message["content"] = "(tool returned no output)"
            continue
        if role in {"user", "system"} and not str(message.get("content") or "").strip():
            messages.pop(index)


def responses_request_from_normal(
    normal: NormalRequest,
    *,
    target_model: str,
    hygiene: HistoryHygiene | None = None,
) -> dict[str, Any]:
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
        "input": relocate_tool_outputs(items, hygiene=hygiene),
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


def relocate_tool_outputs(
    items: list[Any], *, hygiene: HistoryHygiene | None = None
) -> list[Any]:
    """把工具结果搬到对应工具调用的正后方（Responses 邻接归一化）。

    上游（opencodex#1292 / #4726、litellm#32992）会把「call 与 result 之间夹了
    别的 item」当成非法请求直接 400。典型来源是 Codex 的 hook 在 call 与 result
    之间插入 developer 消息，或者历史拼接时多插了一段文本。

    实现参考 opencodex ``normalizeResponsesToolResultAdjacency`` 的两条纪律：

    * **批量归一**，不是逐对重排：同一轮的并行调用必须整体保持在一起，逐对
      重排会把一轮并行调用拆成两个 assistant 轮（opencodex#1477 踩过）。
    * **只在无歧义时动手**：同一个 call_id 出现多次、结果缺失、结果出现在调用
      之前、或中间夹了另一组调用时，原样返回，不做猜测性修补
      （gpustack#6210：错误的修补比不修补更糟）。
    """
    if not isinstance(items, list) or not items:
        return items

    call_positions: dict[str, list[int]] = {}
    output_positions: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        if item_type in _CALL_ITEM_TYPES:
            call_positions.setdefault(call_id, []).append(index)
        elif item_type in _OUTPUT_ITEM_TYPES:
            output_positions.setdefault(call_id, []).append(index)

    if not call_positions:
        return items
    if any(len(indexes) > 1 for indexes in call_positions.values()) or any(
        len(indexes) > 1 for indexes in output_positions.values()
    ):
        return items

    result = list(items)
    index = 0
    while index < len(result):
        if not _is_call_item(result[index]):
            index += 1
            continue
        group_start = index
        group_end = index
        while group_end < len(result) and _is_call_item(result[group_end]):
            group_end += 1
        call_ids = [result[pos].get("call_id") for pos in range(group_start, group_end)]
        if not all(isinstance(call_id, str) and call_id in output_positions for call_id in call_ids):
            index = group_end
            continue
        positions = [output_positions[call_id][0] for call_id in call_ids]
        if positions != list(range(group_end, group_end + len(positions))):
            span = positions[-1]
            if span < group_end or any(
                _is_call_item(result[pos]) for pos in range(group_end, span + 1)
            ):
                # 结果出现在调用之前，或中间夹了另一组调用：无歧义前提不成立。
                index = group_end
                continue
            moved = set(positions)
            outputs = [result[pos] for pos in positions]
            displaced = [
                result[pos] for pos in range(group_end, span + 1) if pos not in moved
            ]
            result = result[:group_end] + outputs + displaced + result[span + 1 :]
            if hygiene is not None:
                hygiene.relocated_tool_outputs += len(outputs)
        index = group_end
    return result


def _is_call_item(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") in _CALL_ITEM_TYPES


def relocate_tool_outputs_in_body(
    body: dict[str, Any], *, hygiene: HistoryHygiene | None = None
) -> dict[str, Any]:
    """Responses 透传路径上的邻接归一化：只重排 input，不改其它字段。"""
    items = body.get("input")
    if not isinstance(items, list):
        return body
    relocated = relocate_tool_outputs(items, hygiene=hygiene)
    if relocated is items:
        return body
    return {**body, "input": relocated}


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


def response_hygiene_message_item(item_id: str, text: str) -> dict[str, Any]:
    """网关自查告警在最终 output 里的形态（一条独立 assistant message）。"""
    return {
        "id": item_id,
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def response_hygiene_message_events(
    item_id: str, output_index: int, text: str
) -> list[dict[str, Any]]:
    """把网关自查告警作为完整 item 生命周期事件推给客户端。

    走的是和其它消息一样的 added/delta/done 序列，客户端不需要认识任何自定义
    事件类型；只是 item_id 和 output_index 独立，不打扰模型真正产出的 item。
    """
    return [
        {
            "type": "response.output_item.added",
            "output_index": output_index,
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
            "output_index": output_index,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
        {
            "type": "response.output_text.delta",
            "item_id": item_id,
            "output_index": output_index,
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.output_text.done",
            "item_id": item_id,
            "output_index": output_index,
            "content_index": 0,
            "text": text,
        },
        {
            "type": "response.content_part.done",
            "item_id": item_id,
            "output_index": output_index,
            "content_index": 0,
            "part": {"type": "output_text", "text": text, "annotations": []},
        },
        {
            "type": "response.output_item.done",
            "output_index": output_index,
            "item": response_hygiene_message_item(item_id, text),
        },
    ]


HYGIENE_ITEM_ID = "msg_gateway_hygiene"


def response_failed_event(
    model: str, error_message: str, *, code: str = "upstream_error"
) -> dict[str, Any]:
    """mid-stream 错误时通知客户端。

    ``code`` 必须是 Codex 认识的官方错误码：客户端只在
    ``response.failed`` 且 ``code == "context_length_exceeded"`` 时压缩历史，
    其它码一律按不可恢复失败处理。
    """
    return {
        "type": "response.failed",
        "response": {
            "id": "resp_placeholder",
            "object": "response",
            "model": model,
            "status": "failed",
            "error": {
                "code": code,
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
