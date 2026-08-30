"""OpenRouter 元数据客户端、身份映射与候选搜索。"""

from __future__ import annotations

from typing import Any

import httpx

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


async def search_models(
    *,
    query: str,
    api_key: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """从 OpenRouter /models 搜索候选模型。"""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    params: dict[str, Any] = {
        "limit": limit,
        "q": query,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                OPENROUTER_MODELS_URL,
                headers=headers,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    models = data.get("data") if isinstance(data, dict) else None
    return [m for m in (models or []) if isinstance(m, dict)]


def find_identity_candidate(
    models: list[dict[str, Any]],
    *,
    provider_model_id: str,
) -> dict[str, Any] | None:
    """在 OpenRouter 结果中找到与 provider_model_id 匹配的单一可信候选。"""
    candidates = []
    for m in models:
        if m.get("id") == provider_model_id:
            candidates.append(m)
    if len(candidates) == 1:
        return candidates[0]
    # 尝试 canonical slug 后缀匹配
    matching = [m for m in models if str(m.get("id", "")).endswith(provider_model_id)]
    if len(matching) == 1:
        return matching[0]
    return None


def extract_model_metadata(model: dict[str, Any]) -> dict[str, Any]:
    """提取规范化字段，作为目录发布基线。"""
    architecture = model.get("architecture") or {}
    pricing = model.get("pricing") or {}
    return {
        "id": model.get("id"),
        "name": model.get("name"),
        "description": model.get("description"),
        "context_window": model.get("context_length"),
        "input_modality": architecture.get("input_modalities") or [],
        "output_modality": architecture.get("output_modalities") or [],
        "supported_parameters": model.get("supported_parameters") or [],
        "pricing": {
            "prompt": pricing.get("prompt"),
            "completion": pricing.get("completion"),
            "request": pricing.get("request"),
        },
        "reasoning": model.get("reasoning"),
        "version_id": model.get("version_id") if isinstance(model.get("version_id"), str) else None,
    }
