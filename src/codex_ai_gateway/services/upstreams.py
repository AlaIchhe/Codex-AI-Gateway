"""上游服务：双端点协议确认与 offering 发现。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from codex_ai_gateway.models.entities import (
    Offering,
    OfferingStatus,
    Upstream,
    UpstreamKind,
    WireProtocol,
)
from codex_ai_gateway.util import utc_now, uuid7

PROBE_CONNECT_TIMEOUT_SECONDS = 3
PROBE_TOTAL_TIMEOUT_SECONDS = 8
PROBE_ATTEMPTS = 1
PROBE_TIMEOUT_SECONDS = 8


def _auth_header(api_credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_credential}"}


def _headers(upstream: Upstream, api_credential: str) -> dict[str, str]:
    headers = dict(upstream.default_headers)
    headers.update(_auth_header(api_credential))
    return headers


def _protocol_evidence(
    *,
    http_status: int | None = None,
    content_type: str | None = None,
    reason: str | None = None,
    error: str | None = None,
    body_shape: str | None = None,
) -> dict[str, Any]:
    confirmed = (
        http_status is not None
        and body_shape in {"object", "array"}
        and (http_status < 400 or http_status in {400, 409, 422})
    )
    evidence: dict[str, Any] = {
        "method": "POST",
        "confirmed": confirmed,
        "http_status": http_status,
        "content_type": content_type,
        "reason": reason,
        "error": error,
        "body_shape": body_shape,
        "checked_at": utc_now(),
    }
    if confirmed and http_status >= 400:
        evidence["confirmation"] = "endpoint_validation_error"
    elif confirmed:
        evidence["confirmation"] = "endpoint_accepted_probe"
    return evidence


async def _probe_endpoint(
    client: httpx.AsyncClient,
    *,
    upstream: Upstream,
    api_credential: str,
    path: str,
) -> dict[str, Any]:
    """发送不会生成模型输出的空 JSON 请求。

    200 表示端点可用；400/409/422 且 JSON 响应表示端点存在但请求缺少必要字段。
    401、403、404、405、429 与网络错误都不能安全确认协议。
    """
    url = f"{upstream.base_url.rstrip('/')}{path}"
    last_error: Exception | None = None
    for _attempt in range(PROBE_ATTEMPTS):
        try:
            response = await client.post(
                url,
                json={},
                headers=_headers(upstream, api_credential),
            )
            content_type = response.headers.get("content-type")
            try:
                parsed = response.json()
                body_shape = "object" if isinstance(parsed, dict) else "array" if isinstance(parsed, list) else "other"
            except ValueError:
                parsed = None
                body_shape = "non_json"
            json_validation = body_shape in {"object", "array"}
            # 部分网关对"模型不存在/参数校验错误"返回 401/403，但 body 是明确的
            # 模型校验错误（如 ModelError: "Model ... not supported"）。此时认证实际
            # 已通过（否则会返回 authentication/unauthorized 类错误），端点存在，
            # 应视为 provider_validation_error 而非 authentication_required。
            effective_status = response.status_code
            is_model_error = (
                json_validation
                and isinstance(parsed, dict)
                and (
                    isinstance(parsed.get("error"), dict)
                    and str(parsed["error"].get("type", "")).lower() in {"modelerror", "invalidrequest", "invalid_request_error"}
                    or isinstance(parsed.get("error"), dict)
                    and str(parsed["error"].get("message", "")).lower().startswith("model")
                )
            )
            if response.status_code in {401, 403} and is_model_error:
                effective_status = 400
            reason = (
                "endpoint_not_found"
                if response.status_code in {404, 405}
                else "provider_validation_error"
                if effective_status in {400, 409, 422} and json_validation
                else "authentication_required"
                if response.status_code in {401, 403}
                else "rate_limited_probe_rejected"
                if response.status_code == 429
                else "non_json_response"
                if not json_validation
                else "unexpected_validation_response"
                if response.status_code >= 400
                else None
            )
            return _protocol_evidence(
                http_status=effective_status,
                content_type=content_type,
                reason=reason,
                body_shape=body_shape,
            )
        except httpx.HTTPError as exc:
            last_error = exc
    return _protocol_evidence(error=str(last_error), reason="network_error")


async def _probe_account(
    client: httpx.AsyncClient,
    *,
    upstream: Upstream,
    api_credential: str,
) -> dict[str, Any]:
    """Validate credentials through the non-billing model catalog endpoint."""
    try:
        response = await client.get(
            f"{upstream.base_url.rstrip('/')}/models",
            headers=_headers(upstream, api_credential),
        )
        return {
            "confirmed": response.status_code == 200,
            "http_status": response.status_code,
            "checked_at": utc_now(),
        }
    except httpx.HTTPError as exc:
        return {"confirmed": False, "error": str(exc), "checked_at": utc_now()}


async def probe_upstream(upstream: Upstream, api_credential: str) -> dict[str, Any]:
    """确认上游支持的 Responses 或 Chat Completions 接口。

    两个探测相互独立；任一失败不阻塞另一个，也不阻塞上游创建。
    双端点均确认时优先 Responses，单端点确认时使用该协议，否则保持未确认。
    """
    is_preset = upstream.kind == UpstreamKind.preset
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
        if is_preset:
            # 预设 Provider MUST NOT 调用 /models；连通性由出站端点探针判定。
            account_probe = {
                "confirmed": True,
                "reason": "preset_skips_models_interface",
                "checked_at": utc_now(),
            }
        else:
            account_probe = await _probe_account(
                client,
                upstream=upstream,
                api_credential=api_credential,
            )
        responses = await _probe_endpoint(
            client,
            upstream=upstream,
            api_credential=api_credential,
            path="/responses",
        )
        chat_completions = await _probe_endpoint(
            client,
            upstream=upstream,
            api_credential=api_credential,
            path="/chat/completions",
        )
    confirmed: list[WireProtocol] = []
    if responses["confirmed"] and account_probe["confirmed"]:
        responses["confirmation"] = (
            "endpoint_validation" if is_preset else "account_and_endpoint_validation"
        )
        confirmed.append(WireProtocol.responses)
    if chat_completions["confirmed"] and account_probe["confirmed"]:
        chat_completions["confirmation"] = (
            "endpoint_validation" if is_preset else "account_and_endpoint_validation"
        )
        confirmed.append(WireProtocol.chat_completions)
    selection_reason = (
        "both_confirmed"
        if len(confirmed) == 2
        else confirmed[0].value if confirmed
        else "no_endpoint_safely_confirmed"
    )
    return {
        "confirmed": bool(confirmed),
        "confirmed_protocols": [p.value for p in confirmed],
        "selection_reason": selection_reason,
        "endpoints": {
            "responses": responses,
            "chat_completions": chat_completions,
        },
        "account_probe": account_probe,
        "checked_at": utc_now(),
    }


async def discover_offerings(
    upstream: Upstream,
    api_credential: str,
) -> list[Offering]:
    """Discover offerings for each confirmed protocol."""
    base = upstream.base_url.rstrip("/")
    headers = _headers(upstream, api_credential)
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{base}/models", headers=headers)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    models = data.get("data", []) if isinstance(data, dict) else []
    now = utc_now()
    result: list[Offering] = []
    for item in models:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        for protocol in upstream.confirmed_protocols:
            result.append(
                Offering(
                    id=uuid7(),
                    upstream_id=upstream.id,
                    provider_model_id=str(item.get("id")),
                    provider_version=str(item["version"]) if item.get("version") is not None else None,
                    native_metadata_json=item,
                    wire_protocol=protocol,
                    display_name=item.get("display_name") or str(item.get("name") or item.get("id")),
                    identity_evidence={"source": f"{protocol.value}/models"},
                    capabilities=_capabilities_from_metadata(item),
                    status=OfferingStatus.approved,
                    discovered_at=now,
                    updated_at=now,
                )
            )
    return result



def build_preset_offerings(
    upstream: Upstream,
    discovery: Any,
    *,
    snapshot_id: str,
) -> list[Offering]:
    """根据成功的官方文档发现结果构建 offerings，不调用 /models。"""
    now = utc_now()
    result: list[Offering] = []
    for model_id in discovery.model_ids:
        for protocol in upstream.confirmed_protocols:
            evidence = {
                "source": "preset_official_doc",
                "source_url": discovery.source_url,
                "snapshot_id": snapshot_id,
                "extractor_key": discovery.extractor_key,
                "extractor_version": discovery.extractor_version,
            }
            result.append(
                Offering(
                    id=uuid7(),
                    upstream_id=upstream.id,
                    provider_model_id=str(model_id),
                    native_metadata_json={
                        "source": "official_doc",
                        "preset_id": upstream.preset_id,
                        "snapshot_id": snapshot_id,
                        "extractor_key": discovery.extractor_key,
                        "extractor_version": discovery.extractor_version,
                    },
                    wire_protocol=protocol,
                    display_name=str(model_id),
                    identity_evidence=evidence,
                    status=OfferingStatus.approved,
                    discovered_at=now,
                    updated_at=now,
                )
            )
    return result


def _capabilities_from_metadata(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "modalities": item.get("capabilities", {}).get("modalities") or [],
        "tools": item.get("capabilities", {}).get("tools") or [],
        "description": item.get("description"),
    }


def request_failure_fields(status_code: int | None = None) -> dict[str, Any]:
    """计算请求失败后的 30 秒健康冷却字段。"""
    should_cooldown = (
        status_code is None
        or status_code in {429, 502, 503, 529}
    )
    now = datetime.now(UTC)
    return {
        "last_health_result": "请求失败，冷却中" if should_cooldown else "请求失败",
        "cooldown_until": (
            (now + timedelta(seconds=30)).isoformat() if should_cooldown else None
        ),
        "updated_at": now.isoformat(),
    }


def is_cooling_down(upstream: Any, *, now: str | None = None) -> bool:
    value = getattr(upstream, "cooldown_until", None)
    if not value:
        return False
    current = now or datetime.now(UTC).isoformat()
    return value > current
