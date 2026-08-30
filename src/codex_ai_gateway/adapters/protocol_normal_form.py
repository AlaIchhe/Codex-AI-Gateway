"""协议规范化表示与显式能力允许清单。

无法表达的字段在请求前引发 untranslatable_capability。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


class UntranslatableCapabilityError(Exception):
    def __init__(self, capability: str, message: str | None = None) -> None:
        super().__init__(message or f"无法翻译能力: {capability}")
        self.capability = capability
        self.message = message or f"无法翻译能力: {capability}"


# 允许清单：能够安全双向映射的字段。
ALLOWED_SAMPLING_KEYS = {"temperature", "top_p", "max_output_tokens", "max_tokens"}
ALLOWED_TOP_KEYS = {"model", "stream", "tool_choice", "tools"}


@dataclass
class NormalMessage:
    role: str
    content: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    name: str | None = None


@dataclass
class NormalRequest:
    model: str
    messages: list[NormalMessage]
    stream: bool = False
    sampling: dict[str, Any] = field(default_factory=dict)
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_choice: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


def _reject_hosted_tools(body: dict[str, Any]) -> None:
    """拒绝 server-side host tools 与不受支持扩展。"""
    capabilities = []
    tools = body.get("tools") or []
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and tool.get("type") in {
                "web_search",
                "computer_use",
                "file_search",
                "code_execution",
            }:
                capabilities.append(f"hosted_tool:{tool.get('type')}")
    if "voice" in body or "audio" in body:
        capabilities.append("voice_audio")
    if "background" in body:
        capabilities.append("background")
    if capabilities:
        raise UntranslatableCapabilityError(capabilities[0], ", ".join(capabilities))


def _normalize_content(content: Any) -> list[dict[str, Any]]:
    """将 string 或 content parts 归一化为 parts 列表。"""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        parts: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in {"text", "input_text", "output_text"}:
                parts.append({"type": "text", "text": part.get("text", "")})
            elif ptype in {"image", "input_image", "image_url"}:
                parts.append({"type": "image", "image_url": part.get("image_url")})
            elif ptype in {"refusal"}:
                parts.append({"type": "refusal", "refusal": part.get("refusal")})
        return parts
    return []


def normalize_request(*, inbound_protocol: str, body: dict[str, Any]) -> NormalRequest:
    """把入站 request 转成 NormalRequest。

    inbound_protocol 为 'responses' 或 'chat_completions'。
    """
    _reject_hosted_tools(body)
    model = body.get("model") or ""
    stream = bool(body.get("stream"))

    if inbound_protocol == "responses":
        return _normalize_responses(body, model, stream)
    return _normalize_chat(body, model, stream)


def _normalize_responses(body: dict[str, Any], model: str, stream: bool) -> NormalRequest:
    messages: list[NormalMessage] = []
    raw_input = body.get("input") or []
    # input 可以是 string 或 list of items
    if isinstance(raw_input, str):
        items: list[Any] = [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": raw_input}]}
        ]
    elif isinstance(raw_input, list):
        items = raw_input
    else:
        items = []
    for item in items:
        if isinstance(item, str):
            messages.append(NormalMessage(role="user", content=[{"type": "text", "text": item}]))
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            role = item.get("role", "user")
            content = _normalize_content(item.get("content"))
            messages.append(NormalMessage(role=role, content=content))
        elif item_type == "function_call":
            messages.append(
                NormalMessage(
                    role="assistant",
                    name=item.get("name"),
                    tool_calls=[
                        {
                            "id": item.get("call_id") or item.get("id"),
                            "type": "function",
                            "function": {
                                "name": item.get("name"),
                                "arguments": item.get("arguments") or "{}",
                            },
                        }
                    ],
                )
            )
        elif item_type == "function_call_output":
            messages.append(
                NormalMessage(
                    role="tool",
                    tool_call_id=item.get("call_id") or item.get("id"),
                    content=[{"type": "text", "text": str(item.get("output", ""))}],
                )
            )
        elif item_type == "reasoning":
            # 普通对话子集不支持 reasoning item 到 chat 的安全映射
            raise UntranslatableCapabilityError("reasoning_item")

    tools = _norm_tools(body.get("tools"))
    tool_choice = body.get("tool_choice")
    sampling = _extract_sampling(body, responses=True)
    return NormalRequest(
        model=model,
        messages=messages,
        stream=stream,
        sampling=sampling,
        tools=tools,
        tool_choice=tool_choice,
        extra={"instructions": body.get("instructions")},
    )


def _normalize_chat(body: dict[str, Any], model: str, stream: bool) -> NormalRequest:
    messages: list[NormalMessage] = []
    for msg in body.get("messages", []):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        content = _normalize_content(msg.get("content"))
        tool_calls = msg.get("tool_calls") or []
        messages.append(
            NormalMessage(
                role=role,
                content=content,
                tool_call_id=msg.get("tool_call_id"),
                tool_calls=tool_calls,
                name=msg.get("name"),
            )
        )
    tools = _norm_tools(body.get("tools"))
    sampling = _extract_sampling(body, responses=False)
    return NormalRequest(
        model=model,
        messages=messages,
        stream=stream,
        sampling=sampling,
        tools=tools,
        tool_choice=body.get("tool_choice"),
        extra={},
    )


def _norm_tools(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    result: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if "function" in tool:
            result.append(deepcopy(tool))
        elif tool.get("type") == "function":
            # Responses 风格：name/parameters 直接位于顶层
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.get("name"),
                        "description": tool.get("description"),
                        "parameters": tool.get("parameters"),
                        "strict": tool.get("strict"),
                    },
                }
            )
    return result


def _extract_sampling(body: dict[str, Any], *, responses: bool) -> dict[str, Any]:
    sampling: dict[str, Any] = {}
    for key in ALLOWED_SAMPLING_KEYS:
        if key in body:
            sampling[key] = body[key]
    if responses and "max_output_tokens" in body:
        sampling["max_tokens"] = body["max_output_tokens"]
    return sampling


def validate_translatable(normal: NormalRequest) -> None:
    """对协议级别能力做预检，无法翻译则失败关闭。"""
    for msg in normal.messages:
        for part in msg.content:
            if part.get("type") in {"image", "input_image"}:
                raise UntranslatableCapabilityError("multimodal_input")
    if normal.extra.get("instructions") is not None:
        # instructions 需要一个 system 前缀，由翻译器处理
        pass
