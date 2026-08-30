"""token usage 解析与归类、reporting_basis 标记。"""

from __future__ import annotations

from typing import Any

from codex_ai_gateway.models.entities import ReportingBasis


def parse_provider_usage(body: Any) -> dict[str, int] | None:
    """从上游 OpenAI 风格 usage 解析 token 类别。返回 None 表示缺失。"""
    if not isinstance(body, dict):
        return None
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    result: dict[str, int] = {}
    prompt_tokens = _as_int(usage.get("prompt_tokens"))
    completion_tokens = _as_int(usage.get("completion_tokens"))
    # Responses 协议使用 input_tokens / output_tokens 命名
    if prompt_tokens is None:
        prompt_tokens = _as_int(usage.get("input_tokens"))
    if completion_tokens is None:
        completion_tokens = _as_int(usage.get("output_tokens"))
    if prompt_tokens is not None:
        result["input"] = prompt_tokens
    if completion_tokens is not None:
        result["output"] = completion_tokens
    input_details = usage.get("input_tokens_details") or {}
    if isinstance(input_details, dict):
        cached = _as_int(input_details.get("cached_tokens"))
        if cached is not None:
            result["cache"] = cached
    output_details = usage.get("output_tokens_details") or {}
    if isinstance(output_details, dict):
        reasoning = _as_int(output_details.get("reasoning_tokens"))
        if reasoning is not None:
            result["reasoning"] = reasoning
    total = _as_int(usage.get("total_tokens"))
    if total is not None:
        result["total"] = total
    if "input" not in result and "output" not in result:
        return None
    return result


def estimate_usage_from_text(text: str) -> dict[str, int]:
    """本地估算 token（粗略），用于无 provider usage 时标记为 estimated。"""
    chars = len(text)
    est = max(1, chars // 4)
    return {
        "estimated_input": 0,
        "estimated_output": est,
    }


def reporting_basis_for(provider_usage: dict[str, int] | None) -> ReportingBasis:
    if provider_usage is None:
        return ReportingBasis.estimated
    if "estimated_input" in provider_usage or "estimated_output" in provider_usage:
        return ReportingBasis.mixed
    return ReportingBasis.provider_reported


def merge_usage_categories(
    provider_usage: dict[str, int] | None,
    estimates: dict[str, int] | None,
) -> dict[str, int]:
    merged: dict[str, int] = dict(provider_usage or {})
    if provider_usage is None:
        merged.update(estimates or {})
    return merged


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
