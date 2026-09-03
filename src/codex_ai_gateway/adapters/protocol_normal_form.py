"""协议规范化表示与显式能力允许清单。

无法表达的字段在请求前引发 untranslatable_capability。
"""

from __future__ import annotations

import json
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
    custom_tool_names: set[str] = field(default_factory=set)


def _reject_hosted_tools(body: dict[str, Any]) -> None:
    """拒绝 server-side host tools 与不受支持扩展。"""
    capabilities = []
    tools = body.get("tools") or []
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and tool.get("type") in {
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
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": raw_input}],
            }
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
            raise UntranslatableCapabilityError(
                f"input_item:{type(item).__name__}",
                f"responses input item 必须是对象，收到 {type(item).__name__}",
            )
        item_type = item.get("type")
        if item_type is None and ("role" in item or "content" in item):
            # 无 type 的 role/content 简写按 message 处理
            item_type = "message"
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
        elif item_type == "custom_tool_call":
            input_text = item.get("input")
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
                                "arguments": json.dumps(
                                    {"input": input_text if isinstance(input_text, str) else ""}
                                ),
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
        elif item_type == "custom_tool_call_output":
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
        else:
            # 未识别的 item 类型显式报错，绝不静默丢弃（丢弃会导致空 messages 上游 400）
            raise UntranslatableCapabilityError(
                f"input_item:{item_type or 'unknown'}",
                f"无法翻译 responses input item: type={item_type!r}",
            )

    tools, custom_tool_names = _norm_tools(body.get("tools"))
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
        custom_tool_names=custom_tool_names,
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
    tools, custom_tool_names = _norm_tools(body.get("tools"))
    sampling = _extract_sampling(body, responses=False)
    return NormalRequest(
        model=model,
        messages=messages,
        stream=stream,
        sampling=sampling,
        tools=tools,
        tool_choice=body.get("tool_choice"),
        extra={},
        custom_tool_names=custom_tool_names,
    )


def _norm_tools(tools: Any) -> tuple[list[dict[str, Any]], set[str]]:
    if not isinstance(tools, list):
        return [], set()
    result: list[dict[str, Any]] = []
    custom_names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if "function" in tool:
            result.append(deepcopy(tool))
        elif tool.get("type") == "web_search":
            # Chat Completions 无法承载 Responses hosted web_search；
            # 对降级上游显式禁用该工具，避免上游 400。
            continue
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
        elif tool.get("type") == "custom":
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                raise UntranslatableCapabilityError("custom_tool", "custom tool 缺少 name。")
            custom_names.add(name)
            description = str(tool.get("description") or "").strip()
            input_description = (
                "Raw apply_patch input. Begin exactly with `*** Begin Patch` and use the standard patch envelope."
                if name == "apply_patch"
                else "Raw freeform input for this custom tool."
            )
            if description:
                description = f"{description}\n\nThis is a FREEFORM tool. Put only the raw tool input in `input`; do not wrap it in JSON or markdown."
            else:
                description = f"FREEFORM custom tool: {name}. Put only the raw tool input in `input`; do not wrap it in JSON or markdown."
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "input": {"type": "string", "description": input_description}
                            },
                            "required": ["input"],
                        },
                    },
                }
            )
    return result, custom_names


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


def ensure_prefill_continuation(body: dict[str, Any]) -> dict[str, Any]:
    """responses 透传前的 prefill 规范化。

    input 以 assistant 消息结尾时，部分上游（如火山方舟对不支持 prefill
    的模型）会拒绝整个请求。此时追加一条 user 续写消息，语义等价于
    "继续"，对支持 prefill 的上游无实质影响。
    """
    raw_input = body.get("input")
    if not isinstance(raw_input, list) or not raw_input:
        return body
    last = raw_input[-1]
    if not isinstance(last, dict) or last.get("role") != "assistant":
        return body
    patched = dict(body)
    patched["input"] = [
        *raw_input,
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Continue."}],
        },
    ]
    return patched


def disable_unsupported_web_search(body: dict[str, Any]) -> dict[str, Any]:
    """移除 Responses 透传请求中的 hosted web_search。

    Codex 会依据 fallback model metadata 自动注入 web_search。若当前 Responses
    上游尚未支持该 hosted tool，继续透传只会把请求推入必然失败的状态。
    """
    tools = body.get("tools")
    if not isinstance(tools, list) or not any(
        isinstance(tool, dict) and tool.get("type") == "web_search" for tool in tools
    ):
        return body
    return {
        **body,
        "tools": [
            tool
            for tool in tools
            if not (isinstance(tool, dict) and tool.get("type") == "web_search")
        ],
    }
