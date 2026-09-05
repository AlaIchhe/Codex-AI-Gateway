"""Codex namespace tools（MCP / 子代理）双向降级回归测试。"""

from __future__ import annotations

import pytest

from codex_ai_gateway.adapters.protocol_normal_form import (
    UntranslatableCapabilityError,
    normalize_request,
)
from codex_ai_gateway.adapters.responses_chat_translation import (
    chat_request_from_normal,
    response_function_call_done_events,
    response_tool_call_started_event,
)


def _responses_body(tools, tool_choice=None):
    body = {
        "model": "m",
        "input": "hi",
        "tools": tools,
    }
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    return body


NAMESPACE_TOOL = {
    "type": "namespace",
    "name": "mcp__context7",
    "description": "Context7 docs lookup.",
    "tools": [
        {
            "type": "function",
            "name": "resolve_library_id",
            "parameters": {"type": "object"},
        },
        {
            "type": "function",
            "name": "query_docs",
            "description": "Query docs.",
            "parameters": {"type": "object"},
        },
    ],
}


def test_namespace_tools_flatten_to_chat_functions():
    normal = normalize_request(inbound_protocol="responses", body=_responses_body([NAMESPACE_TOOL]))
    names = [t["function"]["name"] for t in normal.tools]
    assert names == ["mcp__context7__resolve_library_id", "mcp__context7__query_docs"]
    first = normal.tools[0]["function"]
    assert first["description"] == "Context7 docs lookup."
    second = normal.tools[1]["function"]
    assert "Query docs." in (second["description"] or "")
    assert normal.namespace_tool_aliases["mcp__context7__query_docs"] == {
        "namespace": "mcp__context7",
        "name": "query_docs",
        "kind": "function",
    }


def test_namespace_tool_choice_selector_is_lowered():
    normal = normalize_request(
        inbound_protocol="responses",
        body=_responses_body(
            [NAMESPACE_TOOL],
            tool_choice={"type": "function", "namespace": "mcp__context7", "name": "query_docs"},
        ),
    )
    assert normal.tool_choice == {
        "type": "function",
        "function": {"name": "mcp__context7__query_docs"},
    }


def test_namespace_tool_choice_unknown_tool_fails_closed():
    with pytest.raises(UntranslatableCapabilityError):
        normalize_request(
            inbound_protocol="responses",
            body=_responses_body(
                [NAMESPACE_TOOL],
                tool_choice={"type": "function", "namespace": "mcp__context7", "name": "missing"},
            ),
        )


def test_namespace_wire_name_collision_fails_closed():
    colliding = {
        "type": "namespace",
        "name": "mcp__context7",
        "tools": [
            {"type": "function", "name": "resolve_library_id", "parameters": {}},
            {"type": "function", "name": "resolve_library_id", "parameters": {}},
        ],
    }
    with pytest.raises(UntranslatableCapabilityError):
        normalize_request(inbound_protocol="responses", body=_responses_body([colliding]))


def test_namespace_custom_child_keeps_freeform_semantics():
    tool = {
        "type": "namespace",
        "name": "builtin",
        "tools": [{"type": "custom", "name": "apply_patch", "description": "Patch."}],
    }
    normal = normalize_request(inbound_protocol="responses", body=_responses_body([tool]))
    lowered = normal.tools[0]["function"]
    assert lowered["name"] == "builtin__apply_patch"
    assert lowered["parameters"]["required"] == ["input"]
    assert "builtin__apply_patch" in normal.custom_tool_names
    assert normal.namespace_tool_aliases["builtin__apply_patch"]["kind"] == "custom"


def test_chat_request_from_normal_keeps_flat_namespace_tools():
    normal = normalize_request(inbound_protocol="responses", body=_responses_body([NAMESPACE_TOOL]))
    chat_body = chat_request_from_normal(normal, target_model="up")
    assert [t["function"]["name"] for t in chat_body["tools"]] == [
        "mcp__context7__resolve_library_id",
        "mcp__context7__query_docs",
    ]


def test_done_events_restore_namespace_on_function_call():
    normal = normalize_request(inbound_protocol="responses", body=_responses_body([NAMESPACE_TOOL]))
    events = response_function_call_done_events(
        [{"id": "c1", "index": 0, "function": {"name": "mcp__context7__query_docs", "arguments": "{}"}}],
        namespace_tool_aliases=normal.namespace_tool_aliases,
    )
    item = events[0]["item"]
    assert item["name"] == "query_docs"
    assert item["namespace"] == "mcp__context7"


def test_started_event_restores_namespace():
    normal = normalize_request(inbound_protocol="responses", body=_responses_body([NAMESPACE_TOOL]))
    event = response_tool_call_started_event(
        0,
        "c1",
        "mcp__context7__resolve_library_id",
        is_custom=False,
        namespace_tool_aliases=normal.namespace_tool_aliases,
    )
    assert event["item"]["name"] == "resolve_library_id"
    assert event["item"]["namespace"] == "mcp__context7"
