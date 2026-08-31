"""error_mapping 与协议翻译的回归测试（400 透传 / 简写 input item）。"""

from __future__ import annotations

import json

import pytest

from codex_ai_gateway.adapters.protocol_normal_form import (
    UntranslatableCapabilityError,
    normalize_request,
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
