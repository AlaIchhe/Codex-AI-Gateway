"""提供商错误映射与 sanitized fingerprint。"""

from __future__ import annotations

import hashlib
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
    if status_code == 401 or status_code == 403:
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
    elif status_code == 404:
        error_type = ProviderErrorType.model_permission
        code = "provider_model_unavailable"
        message = f"{prefix}上游不支持该模型或请求。"
    elif status_code == 408 or status_code in (502, 504):
        error_type = ProviderErrorType.upstream_fault
        code = "provider_upstream_fault"
        message = f"{prefix}上游连接或响应异常，请稍后重试。"
    elif status_code == 503:
        error_type = ProviderErrorType.upstream_fault
        code = "provider_upstream_fault"
        message = f"{prefix}上游暂不可用，请稍后重试。"
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
    }
