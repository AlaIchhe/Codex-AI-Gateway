"""Responses custom tool 降级到 Chat function 的双向映射测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from codex_ai_gateway.adapters.protocol_normal_form import (
    UntranslatableCapabilityError,
    disable_unsupported_web_search,
    normalize_request,
)
from codex_ai_gateway.adapters.responses_chat_translation import (
    chat_request_from_normal,
    response_completed_event,
    response_content_part_done_events,
    response_function_call_done_events,
    response_tool_call_started_event,
    responses_request_from_normal,
    translate_chat_chunk_to_response_event,
)
from codex_ai_gateway.domain.routing import route_candidates
from codex_ai_gateway.models.entities import (
    CanonicalModel,
    Offering,
    OfferingStatus,
    RoutingPreference,
    RoutingScope,
    Upstream,
    UpstreamStatus,
    WireProtocol,
)


def test_custom_tool_lowers_to_input_function():
    normal = normalize_request(
        inbound_protocol="responses",
        body={
            "model": "mimo-v2.5-pro",
            "input": "hi",
            "tools": [
                {"type": "function", "name": "shell", "parameters": {"type": "object"}},
                {"type": "custom", "name": "apply_patch", "description": "Patch files."},
            ],
        },
    )
    assert normal.custom_tool_names == {"apply_patch"}
    assert normal.tools[0]["type"] == "function"
    lowered = normal.tools[1]
    assert lowered["type"] == "function"
    assert lowered["function"]["name"] == "apply_patch"
    assert lowered["function"]["parameters"]["required"] == ["input"]


def test_custom_tool_missing_name_fails_closed():
    with pytest.raises(UntranslatableCapabilityError):
        normalize_request(
            inbound_protocol="responses",
            body={"model": "m", "tools": [{"type": "custom"}]},
        )


def test_web_search_tool_is_disabled_for_chat_degradation():
    normal = normalize_request(
        inbound_protocol="responses",
        body={
            "model": "mimo-v2.5-pro",
            "input": "hi",
            "tools": [
                {"type": "web_search"},
                {"type": "function", "name": "shell", "parameters": {"type": "object"}},
                {"type": "custom", "name": "apply_patch"},
            ],
        },
    )
    assert [tool["function"]["name"] for tool in normal.tools] == ["shell", "apply_patch"]
    chat = chat_request_from_normal(normal, target_model="upstream-mimo")
    assert chat["tools"] == normal.tools


def test_web_search_tool_is_disabled_for_responses_passthrough():
    body = {
        "model": "mimo-v2.5-pro",
        "input": "hi",
        "tools": [{"type": "web_search"}, {"type": "function", "name": "shell"}],
    }
    assert disable_unsupported_web_search(body)["tools"] == [{"type": "function", "name": "shell"}]


def test_other_hosted_tools_still_fail_closed():
    with pytest.raises(UntranslatableCapabilityError):
        normalize_request(
            inbound_protocol="responses",
            body={"model": "m", "input": "hi", "tools": [{"type": "code_execution"}]},
        )


def test_custom_call_history_round_trips_through_chat_arguments():
    normal = normalize_request(
        inbound_protocol="responses",
        body={
            "model": "m",
            "input": [
                {
                    "type": "custom_tool_call",
                    "call_id": "call_1",
                    "name": "apply_patch",
                    "input": "*** Begin Patch\n*** End Patch",
                },
                {"type": "custom_tool_call_output", "call_id": "call_1", "output": "done"},
            ],
            "tools": [{"type": "custom", "name": "apply_patch"}],
        },
    )
    assert normal.messages[0].tool_calls[0]["function"]["name"] == "apply_patch"
    chat = chat_request_from_normal(normal, target_model="upstream-mimo")
    assert '"input"' in chat["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert chat["messages"][1]["role"] == "tool"
    assert chat["messages"][1]["tool_call_id"] == "call_1"

    responses = responses_request_from_normal(normal, target_model="client-mimo")
    assert responses["input"][0]["type"] == "custom_tool_call"
    assert responses["input"][0]["input"] == "*** Begin Patch\n*** End Patch"
    assert responses["tools"][0]["type"] == "custom"


def test_chat_tool_call_done_event_is_restored_as_custom_tool_call():
    events = response_function_call_done_events(
        [
            {
                "index": 0,
                "id": "call_1",
                "function": {
                    "name": "apply_patch",
                    "arguments": '{"input":"*** Begin Patch\\n*** End Patch"}',
                },
            }
        ],
        custom_tool_names={"apply_patch"},
    )
    item = events[0]["item"]
    assert item["type"] == "custom_tool_call"
    assert item["name"] == "apply_patch"


def test_tool_call_chunk_without_content_translates_to_no_events():
    assert translate_chat_chunk_to_response_event(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "call_1", "function": {"name": "apply_patch"}}
                        ]
                    }
                }
            ]
        }
    ) is None


def test_content_chunk_translates_to_single_delta_event():
    event = translate_chat_chunk_to_response_event(
        {"choices": [{"delta": {"content": "hello"}}]}
    )
    assert event is not None
    assert event["type"] == "response.output_text.delta"
    assert event["delta"] == "hello"


def test_tool_call_started_event_emitted_for_new_tool_call():
    event = response_tool_call_started_event(0, "call_1", "apply_patch", is_custom=True)
    assert event["type"] == "response.output_item.added"
    assert event["output_index"] == 1
    assert event["item"]["type"] == "custom_tool_call"
    assert event["item"]["status"] == "in_progress"

    event_fn = response_tool_call_started_event(0, "call_2", "shell", is_custom=False)
    assert event_fn["item"]["type"] == "function_call"
    assert event_fn["item"]["arguments"] == ""


def test_content_part_done_events_include_text_done_and_part_done():
    events = response_content_part_done_events("hello world")
    assert events[0]["type"] == "response.output_text.done"
    assert events[0]["text"] == "hello world"
    assert events[1]["type"] == "response.content_part.done"
    assert events[1]["part"]["text"] == "hello world"


def test_response_completed_event_includes_output_and_finish_reason():
    output = [{"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{}"}]
    event = response_completed_event(
        model="mimo",
        finish_reason="tool_calls",
        output=output,
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    assert event["response"]["output"] == output
    assert event["response"]["status"] == "completed"

    event_incomplete = response_completed_event(
        model="mimo", finish_reason="length", output=[], usage={}
    )
    assert event_incomplete["response"]["status"] == "incomplete"


def _routing_state():
    now = datetime.now(UTC).isoformat()
    upstream = Upstream(
        id="upstream-1",
        name="TokenDance",
        base_url="https://example.test/v1",
        auth_credential_ref="test",
        status=UpstreamStatus.enabled,
        created_at=now,
        updated_at=now,
    )
    canonical = CanonicalModel(
        id="canonical-1",
        display_name="MiMo",
        slug="mimo",
        status="available",
        first_matched_at=now,
        updated_at=now,
    )
    responses_offering = Offering(
        id="offering-responses",
        upstream_id=upstream.id,
        provider_model_id="mimo-v2.5-pro",
        wire_protocol=WireProtocol.responses,
        display_name="MiMo Responses",
        status=OfferingStatus.approved,
        canonical_model_id=canonical.id,
        discovered_at=now,
        updated_at=now,
    )
    chat_offering = Offering(
        id="offering-chat",
        upstream_id=upstream.id,
        provider_model_id="mimo-v2.5-pro",
        wire_protocol=WireProtocol.chat_completions,
        display_name="MiMo Chat",
        status=OfferingStatus.approved,
        canonical_model_id=canonical.id,
        discovered_at=now,
        updated_at=now,
    )
    state = SimpleNamespace(
        upstreams=[upstream],
        canonical_models=[canonical],
        offerings=[responses_offering, chat_offering],
        routing_preferences=[
            RoutingPreference(
                id="pref-1",
                scope=RoutingScope.global_preference,
                ordered_upstream_ids=[upstream.id],
                updated_at=now,
            )
        ],
    )
    return state, canonical


def test_route_candidates_prefers_chat_when_request_has_custom_tool():
    state, canonical = _routing_state()
    assert route_candidates(state, canonical)[0][2] == WireProtocol.responses
    assert route_candidates(state, canonical, prefer_chat=True)[0][2] == (
        WireProtocol.chat_completions
    )
