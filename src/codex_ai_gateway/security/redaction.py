"""日志与导出脱敏工具。

过滤 bearer token、虚拟 key、Authorization header 与 prompt/响应正文。
"""

from __future__ import annotations

import re
from typing import Any

SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "x-api-key", "api-key"}

_BEARER_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_AUTH_RE = re.compile(
    r"(?i)(authorization|auth|x-api-key|api-key)\s*[:=]\s*[^\s,;]+"
)
_GENERIC_SECRET_RE = re.compile(r"(?:gwk_[a-f0-9]{48}|gwg_[A-Za-z0-9_-]{30,})")
_PROMPT_KEYS = {"prompt", "messages", "content", "input", "output", "response"}


def redact_authorization_header(value: str) -> str:
    return _BEARER_RE.sub("Bearer [REDACTED]", value)


def redact_gateway_token(value: str) -> str:
    return _GENERIC_SECRET_RE.sub("[REDACTED]", value)


def redact_text(text: str) -> str:
    """脱敏一段文本中的认证信息与虚拟 key。"""
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _AUTH_RE.sub(r"\1=[REDACTED]", text)
    text = _GENERIC_SECRET_RE.sub("[REDACTED]", text)
    return text


def redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """返回不含敏感头的 header 副本。"""
    if not headers:
        return {}
    return {
        k: v for k, v in headers.items() if k.lower() not in SENSITIVE_HEADERS
    }


def redact_dict(data: Any, *, depth: int = 0) -> Any:
    """递归脱敏字典：移除 prompt/response 正文，脱敏认证值。"""
    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(data, dict):
        result: dict[str, Any] = {}
        for key, value in data.items():
            lkey = str(key).lower()
            if lkey in {"token", "gateway_token", "env_value", "experimental_bearer_token"} or lkey in SENSITIVE_HEADERS:
                result[key] = "[REDACTED]"
                continue
            if lkey in _PROMPT_KEYS:
                result[key] = "[REDACTED]"
                continue
            result[key] = redact_dict(value, depth=depth + 1)
        return result
    if isinstance(data, list):
        return [redact_dict(item, depth=depth + 1) for item in data]
    if isinstance(data, str):
        return redact_text(data)
    return data


def redact_url(url: str) -> str:
    if not url:
        return url
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(url)
        if parts.username:
            host = parts.hostname or ""
            netloc = f"{host}"
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
        return url
    except Exception:  # noqa: BLE001
        return url
