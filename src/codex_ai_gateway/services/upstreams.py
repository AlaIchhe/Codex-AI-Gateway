"""上游服务：双端点协议确认与 offering 发现。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from codex_ai_gateway.models.entities import (
    Offering,
    OfferingStatus,
    Upstream,
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


# ---------------------------------------------------------------------------
# 模型级协议探测
# ---------------------------------------------------------------------------

PROBE_MODEL_BATCH_SIZE = 5
PROBE_MODEL_BATCH_DELAY_SECONDS = 2.0


def _probe_body(protocol: WireProtocol, model_id: str) -> dict[str, Any]:
    """构造 max_output_tokens=1 的极简探测请求体。"""
    if protocol == WireProtocol.responses:
        return {"model": model_id, "input": "hi", "max_output_tokens": 1}
    return {
        "model": model_id,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }


def _interpret_probe_response(status_code: int, body: dict[str, Any] | None) -> tuple[bool, str]:
    """解析探测响应，返回 (confirmed, reason)。"""
    if status_code == 200:
        return True, "model_confirmed"
    if status_code in {400, 404, 422}:
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            error_type = str(error.get("type", "")).lower()
            error_msg = str(error.get("message", "")).lower()
            if "model" in error_type or "model" in error_msg or status_code == 404:
                return False, "model_not_supported_on_protocol"
        return False, "model_not_supported_on_protocol"
    if status_code == 429:
        return False, "rate_limited_probe_rejected"
    if status_code in {401, 403}:
        return False, "authentication_required"
    return False, f"unexpected_status_{status_code}"


async def _probe_model_endpoint(
    client: httpx.AsyncClient,
    *,
    upstream: Upstream,
    api_credential: str,
    protocol: WireProtocol,
    model_id: str,
) -> dict[str, Any]:
    """对单个模型在单个协议下发送极简请求，确认可用性。"""
    path = "/responses" if protocol == WireProtocol.responses else "/chat/completions"
    url = f"{upstream.base_url.rstrip('/')}{path}"
    body = _probe_body(protocol, model_id)
    headers = _headers(upstream, api_credential)
    try:
        response = await client.post(url, json=body, headers=headers)
        parsed = None
        try:
            parsed = response.json()
        except ValueError:
            pass
        confirmed, reason = _interpret_probe_response(response.status_code, parsed)
        return {
            "confirmed": confirmed,
            "reason": reason,
            "http_status": response.status_code,
            "model_id": model_id,
            "protocol": protocol.value,
        }
    except httpx.HTTPError as exc:
        return {
            "confirmed": False,
            "reason": "network_error",
            "error": str(exc),
            "model_id": model_id,
            "protocol": protocol.value,
        }


async def _probe_single_model(
    client: httpx.AsyncClient,
    *,
    upstream: Upstream,
    api_credential: str,
    model_id: str,
) -> tuple[str, list[WireProtocol]]:
    """并发探测一个模型的两个协议，返回 (model_id, confirmed_protocols)。"""
    responses_task = _probe_model_endpoint(
        client, upstream=upstream, api_credential=api_credential,
        protocol=WireProtocol.responses, model_id=model_id,
    )
    chat_task = _probe_model_endpoint(
        client, upstream=upstream, api_credential=api_credential,
        protocol=WireProtocol.chat_completions, model_id=model_id,
    )
    responses_result, chat_result = await asyncio.gather(responses_task, chat_task)
    confirmed: list[WireProtocol] = []
    if responses_result["confirmed"]:
        confirmed.append(WireProtocol.responses)
    if chat_result["confirmed"]:
        confirmed.append(WireProtocol.chat_completions)
    return model_id, confirmed


async def probe_model_protocols(
    upstream: Upstream,
    api_credential: str,
    model_ids: list[str],
) -> dict[str, list[WireProtocol]]:
    """分批探测每个模型的协议支持。

    每批 BATCH_SIZE 个模型并发探测（每模型 2 个协议并发），
    批间等待 BATCH_DELAY_SECONDS 秒以缓解限流。
    返回 {model_id: [confirmed protocols]}。
    """
    results: dict[str, list[WireProtocol]] = {}
    total = len(model_ids)
    for i in range(0, total, PROBE_MODEL_BATCH_SIZE):
        batch = model_ids[i:i + PROBE_MODEL_BATCH_SIZE]
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
            tasks = [
                _probe_single_model(client, upstream=upstream, api_credential=api_credential, model_id=mid)
                for mid in batch
            ]
            batch_results = await asyncio.gather(*tasks)
        for mid, protocols in batch_results:
            results[mid] = protocols
        if i + PROBE_MODEL_BATCH_SIZE < total:
            await asyncio.sleep(PROBE_MODEL_BATCH_DELAY_SECONDS)
    return results

async def discover_offerings(
    upstream: Upstream,
    api_credential: str,
    *,
    protocol_map: dict[str, list[WireProtocol]] | None = None,
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
        for protocol in (protocol_map or {}).get(str(item.get("id")), []):
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
    protocol_map: dict[str, list[WireProtocol]],
) -> list[Offering]:
    """根据成功的官方文档发现结果构建 offerings，不调用 /models。"""
    now = utc_now()
    result: list[Offering] = []
    for model_id in discovery.model_ids:
        for protocol in protocol_map.get(model_id, []):
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
