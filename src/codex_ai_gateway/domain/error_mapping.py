"""提供商错误映射与 sanitized fingerprint。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from codex_ai_gateway.models.entities import ProviderErrorType


def sanitize_fingerprint(body: bytes | str | None) -> str | None:
    """从上游响应构造一个短指纹，不泄露正文。"""
    if body is None:
        return None
    data = body if isinstance(body, bytes) else body.encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


def _body_text(body: bytes | str | None) -> str:
    if body is None:
        return ""
    return (body.decode("utf-8", errors="ignore") if isinstance(body, bytes) else body).strip()


# 上游错误正文摘要长度：够定位 param/错误码，又不会把 21MB 的 admin-state 撑爆。
ERROR_EXCERPT_LIMIT = 500


def error_excerpt(body: bytes | str | None, *, limit: int = ERROR_EXCERPT_LIMIT) -> str | None:
    """把上游错误正文压成单行摘要，用于故障留档。

    之前 UsageEvent 只存 ``error_mapping_code``：一旦上游返回
    ``{"error":{"message":"Invalid input","param":"messages.1.content"}}``，
    事后完全看不出它抱怨的是我们发出去的哪一段请求。
    """
    text = " ".join(_body_text(body).split())
    if not text:
        return None
    return text[:limit]


_MODEL_UNAVAILABLE_CODES = {
    "model_not_found",
    "model_not_available",
    "model_unavailable",
    "no_available_channel",
    "no_available_channels",
    "unsupported_model",
    "model_unsupported",
    "model_not_supported",
    "unsupported_provider_model",
}
_MODEL_UNAVAILABLE_HINTS = (
    "not supported on this endpoint",
    "model is not supported",
    "unsupported model",
    "does not support the model",
)
# 「上下文超限」是请求形状问题：换一个窗口更大的上游可能成功，上游本身没病。
# 各家措辞差异很大（OpenAI 用 maximum context length，方舟/DeepSeek 用
# 「请求体过大」「超出最大长度」，网关自己的 cc 层只回 Invalid input），
# 这里尽量覆盖，识别不出来就会退化成 invalid_request 并直接失败。
_CONTEXT_LENGTH_CODES = {
    "context_length_exceeded",
    "context_window_exceeded",
    "maximum_context_length_exceeded",
    "input_too_long",
    "prompt_too_long",
    "string_above_max_length",
    "max_tokens_exceeded",
}
_CONTEXT_LENGTH_HINTS = (
    "context length",
    "context_length",
    "context window",
    "maximum context",
    "prompt is too long",
    "input is too long",
    "too many tokens",
    "maximum number of tokens",
    "reduce the length",
    "token limit",
    "上下文长度",
    "上下文超",
    "超出最大",
    "请求体过大",
)
# 「模型不在套餐内」：上游常见写法是 HTTP 403 + error.code=FORBIDDEN，
# 真正的信号在 message 里的 MODEL_NOT_IN_PLAN（或 error.code 里）。
# 它是模型级事实（套餐没这个模型），不是账号级鉴权失败，必须单独识别，
# 否则会被 403 分支吞掉成 provider 级 authentication。
_NOT_IN_PLAN_MARKERS = ("model_not_in_plan", "not_in_plan")
# 「上游内容审查拒收」：与请求格式无关，是上游对整包上下文做的内容判定。
# 各家措辞完全不统一（DeepSeek / command ai 用 Content Exists Risk，DashScope
# 用 DataInspectionFailed，Azure 用 ResponsibleAIPolicyViolation），这里尽量覆盖。
# 参考 LiteLLM 的 ContentPolicyViolationError + content_policy_fallbacks 的做法：
# 必须独立成类，才能「换一个上游再试」而不是当成 invalid_request 直接失败。
_CONTENT_POLICY_CODE_MARKERS = (
    "content_policy_violation",
    "content_policy_blocked",
    "datainspectionfailed",
    "data_inspection_failed",
    "contentexistsrisk",
)
_CONTENT_POLICY_HINTS = (
    "content exists risk",
    "content_exists_risk",
    "contentexistsrisk",
    "datainspectionfailed",
    "data inspection failed",
    "content policy",
    "content_policy",
    "responsibleaipolicyviolation",
    "safety system",
    "content filter",
    "flagged as potentially",
    "sensitive content",
    "input is sensitive",
    "内容审核",
    "内容风控",
    "敏感词",
)


def _provider_error_code(body: bytes | str | None) -> str | None:
    """从上游响应体中提取 error.code / error.type。"""
    text = _body_text(body)
    if not text:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("code", "type"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
    return None


def map_provider_error(
    status_code: int,
    *,
    body: bytes | str | None = None,
    error_text: str | None = None,
    upstream_name: str | None = None,
) -> dict[str, Any]:
    """将上游 HTTP 状态映射为稳定 provider 错误类别。

    上游名会附加到所有错误消息前；400/其他未分类状态透传上游原始
    错误正文，不做二次解读。
    """
    text = _body_text(body)
    prefix = f"[{upstream_name}] " if upstream_name else ""
    provider_code = _provider_error_code(body)
    lowered_text = text.lower()
    model_unavailable = (
        provider_code in _MODEL_UNAVAILABLE_CODES
        or (
            provider_code is not None
            and "model" in provider_code
            and any(
                token in provider_code
                for token in ("not_found", "unavailable", "unsupported")
            )
        )
        or any(hint in lowered_text for hint in _MODEL_UNAVAILABLE_HINTS)
    )
    not_in_plan = (
        provider_code is not None and any(mark in provider_code for mark in _NOT_IN_PLAN_MARKERS)
    ) or any(mark in lowered_text for mark in _NOT_IN_PLAN_MARKERS)
    context_length_exceeded = status_code in {400, 413, 422} and (
        provider_code in _CONTEXT_LENGTH_CODES
        or any(hint in lowered_text for hint in _CONTEXT_LENGTH_HINTS)
    )
    # 只在 4xx 上判定：5xx 正文里偶然出现「content policy」是上游自身故障，
    # 不该被当成用户请求的内容问题。
    content_policy_blocked = 400 <= status_code < 500 and (
        (
            provider_code is not None
            and any(mark in provider_code for mark in _CONTENT_POLICY_CODE_MARKERS)
        )
        or any(hint in lowered_text for hint in _CONTENT_POLICY_HINTS)
    )
    if not_in_plan:
        error_type = ProviderErrorType.model_permission
        code = "provider_model_not_in_plan"
        message = f"{prefix}该模型不在上游套餐内（MODEL_NOT_IN_PLAN），已从该上游剔除。"
    elif content_policy_blocked:
        # 必须先于 401/403：部分上游用 403 表达内容拦截，否则会误报成
        # 「上游认证失败」，把用户引向完全错误的方向。
        error_type = ProviderErrorType.content_policy
        code = "provider_content_policy_blocked"
        detail = text or error_text or ""
        hint = (
            "该判定针对整包上下文，重试或压缩后仍含该内容时无效；"
            "可改走其它上游，或新开会话。"
        )
        if detail:
            message = f"{prefix}上游内容审查拦截了本次请求：{detail} {hint}"
        else:
            message = f"{prefix}上游内容审查拦截了本次请求。{hint}"
    elif status_code == 401 or status_code == 403:
        error_type = ProviderErrorType.authentication
        code = "provider_authentication_failed"
        message = f"{prefix}上游认证失败，请检查上游凭据。"
    elif status_code == 402:
        error_type = ProviderErrorType.quota_budget
        code = "provider_quota_budget"
        message = f"{prefix}上游额度或预算不足。"
    elif status_code == 429:
        error_type = ProviderErrorType.rate_limit
        code = "provider_rate_limited"
        message = f"{prefix}上游已限流，请稍后重试。"
    elif model_unavailable:
        error_type = ProviderErrorType.model_permission
        code = "provider_model_unavailable"
        message = f"{prefix}上游不支持该模型或已下线。"
    elif status_code == 404:
        error_type = ProviderErrorType.model_permission
        code = "provider_model_unavailable"
        message = f"{prefix}上游不支持该模型或请求。"
    elif context_length_exceeded:
        # 上下文超限按「请求形状」处理：可以继续换窗口更大的上游，且上游不进入避让。
        error_type = ProviderErrorType.invalid_request
        code = "provider_context_length_exceeded"
        detail = text or error_text or ""
        if detail:
            message = f"{prefix}上游判定上下文超限（{status_code}）：{detail}"
        else:
            message = f"{prefix}上游判定上下文超限（{status_code}），请压缩历史后重试。"
    elif status_code == 503:
        error_type = ProviderErrorType.upstream_fault
        code = "provider_upstream_fault"
        message = f"{prefix}上游暂不可用，请稍后重试。"
    elif status_code == 408 or 500 <= status_code < 600:
        # 任何 5xx 都是上游侧故障：必须继续切换备用上游并进入避让，
        # 不能落进下面的 invalid_request 分支被当成客户端请求错误。
        error_type = ProviderErrorType.upstream_fault
        code = "provider_upstream_fault"
        message = f"{prefix}上游连接或响应异常，请稍后重试。"
    else:
        # 400/405/413/422 等请求类错误：完整透传上游原始错误。
        error_type = ProviderErrorType.invalid_request
        code = "provider_invalid_request"
        if text or error_text:
            detail = text or error_text or ""
            message = f"{prefix}上游返回 {status_code}：{detail}"
        else:
            message = f"{prefix}上游返回 {status_code}，且未提供错误详情。"

    return {
        "provider_error_type": error_type.value,
        "error_mapping_code": code,
        "http_upstream_status": status_code,
        "upstream_error_type": code,
        "fingerprint": sanitize_fingerprint(body),
        "message": message,
        "excerpt": error_excerpt(body),
    }
