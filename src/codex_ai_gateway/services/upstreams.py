"""上游服务：模型列表发现与 offering 构建。

协议不在创建时探测。没有协议记录的模型会得到一条 ``unconfirmed``
offering，模型因此仍然可路由；第一个真实请求按两种协议依次尝试，成功后
由数据面把命中的协议写回 ``model_protocol_probe``。
"""

from __future__ import annotations

from typing import Any

import httpx

from codex_ai_gateway.models.entities import (
    Offering,
    OfferingStatus,
    Upstream,
    WireProtocol,
)
from codex_ai_gateway.util import utc_now, uuid7

# 模型列表端点通常比推理端点慢，且部分聚合站会间歇性超时；给足读超时并区分失败原因。
MODELS_DISCOVERY_CONNECT_TIMEOUT_SECONDS = 10.0
MODELS_DISCOVERY_TIMEOUT_SECONDS = 30.0


def _auth_header(api_credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_credential}"}


def _headers(upstream: Upstream, api_credential: str) -> dict[str, str]:
    headers = dict(upstream.default_headers)
    headers.update(_auth_header(api_credential))
    return headers


def offering_protocols(protocols: list[WireProtocol] | None) -> list[WireProtocol]:
    """返回模型的 offering 协议：已确认协议，或一个 ``unconfirmed`` 占位。

    未确认协议的模型仍然建立 offering，从而保持可路由；真实请求会依次尝试
    两种协议，命中后由数据面提升为具体协议并落盘。
    """
    return list(protocols) if protocols else [WireProtocol.unconfirmed]


def _identity_source(protocol: WireProtocol) -> str:
    if protocol is WireProtocol.unconfirmed:
        return "unconfirmed/models"
    return f"{protocol.value}/models"


async def fetch_upstream_models(
    upstream: Upstream,
    api_credential: str,
) -> list[dict[str, Any]]:
    """获取上游 ``/models`` 原始条目。

    超时按模型列表端点单独放宽，失败时抛出 ``httpx.HTTPError`` / ``ValueError``，
    由调用方决定如何记录，避免把网络失败误报成"未发现可用模型"。
    """
    base = upstream.base_url.rstrip("/")
    headers = _headers(upstream, api_credential)
    timeout = httpx.Timeout(
        MODELS_DISCOVERY_TIMEOUT_SECONDS,
        connect=MODELS_DISCOVERY_CONNECT_TIMEOUT_SECONDS,
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(f"{base}/models", headers=headers)
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        return []
    items = data.get("data", [])
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict) and item.get("id")]


async def discover_offerings(
    upstream: Upstream,
    api_credential: str,
    *,
    protocol_map: dict[str, list[WireProtocol]] | None = None,
    models: list[dict[str, Any]] | None = None,
) -> list[Offering]:
    """为每个模型构建 offering。

    ``protocol_map`` 里已有记录的模型按已确认协议各建一条；没有记录的模型建
    一条 ``unconfirmed`` 占位，等首次真实请求确认协议。

    ``models`` 可由调用方传入已获取的模型列表，避免重复请求 ``/models``；
    未传入时自行拉取，拉取失败会向上抛出而不是静默返回空列表。
    """
    if models is None:
        models = await fetch_upstream_models(upstream, api_credential)
    now = utc_now()
    result: list[Offering] = []
    for item in models:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        model_id = str(item.get("id"))
        for protocol in offering_protocols((protocol_map or {}).get(model_id)):
            result.append(
                Offering(
                    id=uuid7(),
                    upstream_id=upstream.id,
                    provider_model_id=model_id,
                    provider_version=str(item["version"]) if item.get("version") is not None else None,
                    native_metadata_json=item,
                    wire_protocol=protocol,
                    display_name=item.get("display_name") or str(item.get("name") or item.get("id")),
                    identity_evidence={"source": _identity_source(protocol)},
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
        for protocol in offering_protocols(protocol_map.get(model_id)):
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
    capabilities = item.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    return {
        "modalities": capabilities.get("modalities") or [],
        "tools": capabilities.get("tools") or [],
        "description": item.get("description"),
    }