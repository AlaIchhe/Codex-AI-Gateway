"""error_mapping 与协议翻译的回归测试（400 透传 / 简写 input item）。"""

from __future__ import annotations

import json

import pytest

from codex_ai_gateway.adapters.protocol_normal_form import (
    UntranslatableCapabilityError,
    ensure_prefill_continuation,
    normalize_request,
    validate_translatable,
)
from codex_ai_gateway.domain.error_mapping import map_provider_error


def test_400_passes_through_upstream_body_with_name():
    body = json.dumps({"error": {"message": "Input cannot be empty"}})
    mapped = map_provider_error(
        400,
        body=body,
        upstream_name="OpenCode Go",
    )
    assert mapped["error_mapping_code"] == "provider_invalid_request"
    assert mapped["provider_error_type"] == "invalid_request"
    assert "[OpenCode Go]" in mapped["message"]
    assert "Input cannot be empty" in mapped["message"]


def test_400_without_body_still_names_upstream():
    mapped = map_provider_error(400, body=b"", upstream_name="火山方舟")
    assert mapped["error_mapping_code"] == "provider_invalid_request"
    assert "[火山方舟]" in mapped["message"]
    assert "400" in mapped["message"]


MODEL_NOT_IN_PLAN_BODY = json.dumps(
    {
        "error": {
            "message": (
                "MODEL_NOT_IN_PLAN: Gemini 3.5 Flash Lite available in Pro and above "
                "plans or extra on demand usage"
            ),
            "type": "permission_error",
            "code": "FORBIDDEN",
        }
    }
)


def test_model_not_in_plan_beats_generic_403_authentication():
    """403 + MODEL_NOT_IN_PLAN 是模型级事实，不能报成「上游认证失败」。"""
    mapped = map_provider_error(403, body=MODEL_NOT_IN_PLAN_BODY, upstream_name="command ai")
    assert mapped["error_mapping_code"] == "provider_model_not_in_plan"
    assert mapped["provider_error_type"] == "model_permission"
    assert "[command ai]" in mapped["message"]
    assert "套餐" in mapped["message"]


def test_plain_403_still_maps_to_authentication():
    mapped = map_provider_error(
        403,
        body=json.dumps({"error": {"code": "FORBIDDEN", "message": "invalid api key"}}),
        upstream_name="A",
    )
    assert mapped["error_mapping_code"] == "provider_authentication_failed"


def test_402_keeps_quota_category():
    mapped = map_provider_error(402, body=b"{}", upstream_name="A")
    assert mapped["error_mapping_code"] == "provider_quota_budget"
    assert "[A]" in mapped["message"]


def test_404_is_model_permission_not_quota():
    mapped = map_provider_error(404, body=b"{}", upstream_name="A")
    assert mapped["error_mapping_code"] == "provider_model_unavailable"


def test_502_is_upstream_fault():
    mapped = map_provider_error(502, body=b"", upstream_name="A")
    assert mapped["error_mapping_code"] == "provider_upstream_fault"


def test_400_unsupported_model_is_model_unavailable_not_invalid_request():
    """上游 responses 端点不支持该模型（code=unsupported_model）要能回落，而不是硬 400。"""
    body = json.dumps(
        {
            "error": {
                "message": 'Model "deepseek-v4.1-flash" is not supported on this endpoint.',
                "type": "invalid_request_error",
                "param": "model",
                "code": "unsupported_model",
            }
        }
    )
    mapped = map_provider_error(400, body=body, upstream_name="command ai")
    assert mapped["error_mapping_code"] == "provider_model_unavailable"
    assert mapped["provider_error_type"] == "model_permission"


def test_400_unsupported_endpoint_message_without_code_is_model_unavailable():
    body = json.dumps({"error": {"message": "Model x is not supported on this endpoint."}})
    mapped = map_provider_error(400, body=body, upstream_name="A")
    assert mapped["error_mapping_code"] == "provider_model_unavailable"


def test_500_is_upstream_fault_not_client_error():
    """裸 500 必须归为上游故障：否则会被当作 invalid_request 直接 stop、不切换备用上游。"""
    mapped = map_provider_error(500, body=b"{}", upstream_name="A")
    assert mapped["error_mapping_code"] == "provider_upstream_fault"
    assert mapped["provider_error_type"] == "upstream_fault"


def test_shorthand_input_item_treated_as_message():
    normal = normalize_request(
        inbound_protocol="responses",
        body={
            "model": "glm-5.3-flash",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        },
    )
    assert len(normal.messages) == 1
    assert normal.messages[0].role == "user"
    assert normal.messages[0].content[0]["text"] == "hi"


def test_unknown_input_item_raises_instead_of_silent_drop():
    with pytest.raises(UntranslatableCapabilityError):
        normalize_request(
            inbound_protocol="responses",
            body={
                "model": "m",
                "input": [{"type": "item_reference", "id": "abc"}],
            },
        )


def test_non_dict_input_item_raises():
    with pytest.raises(UntranslatableCapabilityError):
        normalize_request(
            inbound_protocol="responses",
            body={"model": "m", "input": [42]},
        )


def test_prefill_continuation_appends_user_after_assistant_last():
    body = {
        "model": "glm-5.3-flash",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "partial"}]},
        ],
    }
    patched = ensure_prefill_continuation(body)
    assert patched is not body
    assert patched["input"][-1]["role"] == "user"
    assert patched["input"][-1]["content"][0]["text"] == "Continue."
    assert len(body["input"]) == 2  # 原请求不被修改


def test_prefill_continuation_noop_for_user_last():
    body = {
        "model": "m",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
    }
    assert ensure_prefill_continuation(body) is body


def test_prefill_continuation_noop_for_string_input():
    body = {"model": "m", "input": "hi"}
    assert ensure_prefill_continuation(body) is body


def test_prefill_continuation_noop_without_input():
    body = {"model": "m"}
    assert ensure_prefill_continuation(body) is body


def test_503_model_not_found_maps_to_model_unavailable():
    body = json.dumps(
        {
            "error": {
                "code": "model_not_found",
                "message": "No available channel for model gpt-5.4-mini",
            }
        }
    )
    mapped = map_provider_error(503, body=body, upstream_name="MaoLao")
    assert mapped["error_mapping_code"] == "provider_model_unavailable"
    assert mapped["provider_error_type"] == "model_permission"
    assert "[MaoLao]" in mapped["message"]


def test_503_without_model_code_stays_upstream_fault():
    body = json.dumps({"error": {"code": "server_overloaded"}})
    mapped = map_provider_error(503, body=body, upstream_name="A")
    assert mapped["error_mapping_code"] == "provider_upstream_fault"


def test_503_no_available_channel_code_maps_to_model_unavailable():
    body = json.dumps({"error": {"type": "no_available_channel"}})
    mapped = map_provider_error(503, body=body, upstream_name="A")
    assert mapped["error_mapping_code"] == "provider_model_unavailable"


def test_history_image_degrades_to_text_placeholder():
    """历史轮次里的图片降级为文本占位：一张老图片不应让会话永久不可用。"""
    normal = normalize_request(
        inbound_protocol="responses",
        body={
            "model": "glm-5.3-flash",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "看下这张图"},
                        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                    ],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "看到了"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "现在这轮是纯文本"}],
                },
            ],
        },
    )
    parts = normal.messages[0].content
    assert parts[1]["type"] == "text"
    assert parts[1]["text"] == "[image attachment omitted from older history]"
    assert normal.messages[2].content[0]["text"] == "现在这轮是纯文本"
    validate_translatable(normal)  # 不再抛错


def test_current_turn_image_still_fails_closed():
    """当前轮图片仍然 fail-closed：立刻 422，而不是被当成上游故障。"""
    normal = normalize_request(
        inbound_protocol="responses",
        body={
            "model": "glm-5.3-flash",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "旧轮"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "这轮带图"},
                        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                    ],
                },
            ],
        },
    )
    assert normal.messages[0].content[0]["type"] == "text"
    with pytest.raises(UntranslatableCapabilityError) as excinfo:
        validate_translatable(normal)
    assert excinfo.value.capability == "multimodal_input"
