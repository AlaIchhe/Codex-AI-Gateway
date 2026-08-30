"""统一错误处理与简体中文 problem+json 响应工厂。"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class GatewayError(Exception):
    """网关业务错误，携带稳定 type/code 与中文消息。"""

    def __init__(
        self,
        *,
        error_type: str,
        code: str,
        message: str,
        status_code: int,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        self.headers = headers


def problem_json(
    *,
    error_type: str,
    code: str,
    title: str,
    detail: str,
    status_code: int,
    instance: str | None = None,
    details: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    """构造 RFC 9457 problem+json 响应。"""
    body: dict[str, Any] = {
        "type": error_type,
        "code": code,
        "title": title,
        "detail": detail,
        "status": status_code,
    }
    if instance:
        body["instance"] = instance
    if details:
        body["details"] = details
    if extra:
        body.update(extra)
    return JSONResponse(
        status_code=status_code,
        content=body,
        media_type="application/problem+json",
    )


def problem_for_error(error: GatewayError, request: Request | None = None) -> JSONResponse:
    return problem_json(
        error_type=error.error_type,
        code=error.code,
        title=error.message,
        detail=error.details.get("detail") or error.message,
        status_code=error.status_code,
        instance=str(request.url) if request else None,
        details=error.details,
        headers=error.headers,
    )


def gateway_error_response(
    *,
    error_type: str,
    code: str,
    message: str,
    status_code: int,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """面向网关客户端的统一错误体（OpenAI 风格 error 对象）。"""
    from fastapi.responses import JSONResponse

    body = {
        "error": {
            "type": error_type,
            "code": code,
            "message": message,
            "details": details or {},
        }
    }
    return JSONResponse(
        status_code=status_code,
        content=body,
        media_type="application/json",
        headers=headers,
    )


def make_invalid_request(code: str, message: str, **kw: Any) -> GatewayError:
    return GatewayError(
        error_type="invalid_request",
        code=code,
        message=message,
        status_code=400,
        **kw,
    )


def make_auth_error(code: str, message: str, **kw: Any) -> GatewayError:
    return GatewayError(
        error_type="authentication_error",
        code=code,
        message=message,
        status_code=401,
        **kw,
    )


def make_permission_error(code: str, message: str, **kw: Any) -> GatewayError:
    return GatewayError(
        error_type="permission_denied",
        code=code,
        message=message,
        status_code=403,
        **kw,
    )


def make_provider_error(code: str, message: str, **kw: Any) -> GatewayError:
    return GatewayError(
        error_type="provider_error",
        code=code,
        message=message,
        status_code=502,
        **kw,
    )


def make_untranslatable(message: str, capability: str, **kw: Any) -> GatewayError:
    return GatewayError(
        error_type="untranslatable_capability",
        code="untranslatable_capability",
        message=message,
        status_code=422,
        details={"capability": capability, **kw},
    )
