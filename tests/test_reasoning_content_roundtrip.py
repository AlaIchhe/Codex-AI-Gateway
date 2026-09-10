"""reasoning_content 回传回归测试（DeepSeek/TokenDance thinking 模式）。"""

from __future__ import annotations

from codex_ai_gateway.adapters.protocol_normal_form import normalize_request
from codex_ai_gateway.adapters.responses_chat_translation import (
    _ensure_tool_call_reasoning_content,
    _merge_and_prune_tool_messages,
    chat_request_from_normal,
)


def test_responses_reasoning_item_attaches_to_function_call():
    normal = normalize_request(
        inbound_protocol="responses",
        body={
            "model": "deepseek-v4",
            "input": [
                {"role": "user", "content": "ping"},
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "Need to run echo."}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "shell_command",
                    "arguments": '{"command":"echo hi"}',
                },
                {"type": "function_call_output", "call_id": "call_1", "output": "hi"},
            ],
            "tools": [{"type": "function", "name": "shell_command", "parameters": {}}],
        },
    )
    body = chat_request_from_normal(normal, target_model="up")
    assistant = next(m for m in body["messages"] if m.get("tool_calls"))
    assert assistant["reasoning_content"] == "Need to run echo."


def test_chat_content_reasoning_preserved():
    normal = normalize_request(
        inbound_protocol="chat_completions",
        body={
            "model": "deepseek-v4",
            "messages": [
                {"role": "user", "content": "ping"},
                {"role": "assistant", "content": "pong", "reasoning_content": "thought"},
                {"role": "user", "content": "next"},
            ],
        },
    )
    body = chat_request_from_normal(normal, target_model="up")
    assert body["messages"][1]["reasoning_content"] == "thought"


def test_empty_tool_call_assistant_gets_placeholder_reasoning():
    messages = [{"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]}]
    _ensure_tool_call_reasoning_content(messages)
    assert messages[0]["reasoning_content"] == "Calling the requested tool."


def test_existing_reasoning_is_not_overwritten():
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}], "reasoning_content": "real"}
    ]
    _ensure_tool_call_reasoning_content(messages)
    assert messages[0]["reasoning_content"] == "real"


def test_merge_preserves_reasoning_for_kept_tool_call():
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1"}],
            "reasoning_content": "kept",
        },
        {"role": "tool", "tool_call_id": "c1", "content": "done"},
    ]
    merged = _merge_and_prune_tool_messages(messages)
    assert merged[0]["reasoning_content"] == "kept"
