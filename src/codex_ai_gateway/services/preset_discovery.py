"""通过预设官方文档发现模型列表。"""

from __future__ import annotations

import hashlib
from typing import Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from codex_ai_gateway.models.entities import (
    PresetDiscoveryFailure,
    PresetDocumentSnapshot,
)
from codex_ai_gateway.services.preset_extractors import (
    ExtractionResult,
    PresetExtractionError,
    get_extractor,
)
from codex_ai_gateway.services.presets import PresetProvider
from codex_ai_gateway.util import utc_now, uuid7

PRESET_DOCUMENT_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
PRESET_DOCUMENT_MAX_BYTES = 5 * 1024 * 1024
_ALLOWED_CONTENT_TYPES = {"text/html", "application/xhtml+xml", "application/json"}


class PresetDiscoveryResult(BaseModel):
    status: Literal["succeeded", "failed"]
    preset_id: str
    source_url: str
    extractor_key: str
    extractor_version: str
    model_ids: list[str] = Field(default_factory=list)
    snapshot_id: str | None = None
    evidence: dict[str, object] = Field(default_factory=dict)
    http_status: int | None = None
    content_type: str | None = None
    final_url: str | None = None
    body_sha256: str | None = None
    body_size: int = 0
    attempted_at: str
    failure_code: str | None = None
    failure_message: str | None = None


def _failure(
    preset: PresetProvider,
    *,
    extractor_version: str,
    attempted_at: str,
    code: str,
    message: str,
    http_status: int | None = None,
    content_type: str | None = None,
    final_url: str | None = None,
    body: bytes = b"",
    body_sha256: str | None = None,
    body_size: int | None = None,
    evidence: dict[str, object] | None = None,
) -> PresetDiscoveryResult:
    return PresetDiscoveryResult(
        status="failed",
        preset_id=preset.preset_id,
        source_url=preset.doc_url,
        extractor_key=preset.extractor_key,
        extractor_version=extractor_version,
        http_status=http_status,
        content_type=content_type,
        final_url=final_url,
        body_sha256=body_sha256 or (hashlib.sha256(body).hexdigest() if body else None),
        body_size=body_size if body_size is not None else len(body),
        attempted_at=attempted_at,
        failure_code=code,
        failure_message=message,
        evidence=evidence or {},
    )



def _is_same_registrable_domain(source_host: str | None, final_host: str | None) -> bool:
    """Check that final host shares the same registrable domain (last two labels)."""
    if not source_host or not final_host:
        return False
    source_host = source_host.lower().rstrip(".")
    final_host = final_host.lower().rstrip(".")
    if source_host == final_host:
        return True
    source_parts = source_host.rsplit(".", 2)
    final_parts = final_host.rsplit(".", 2)
    if len(source_parts) < 2 or len(final_parts) < 2:
        return False
    return source_parts[-2:] == final_parts[-2:]


async def discover_preset(preset: PresetProvider) -> PresetDiscoveryResult:
    attempted_at = utc_now()
    try:
        extractor = get_extractor(preset.extractor_key)
        if extractor.version != preset.extractor_version:
            raise PresetExtractionError(
                "extractor_version_mismatch",
                "预设提取器版本与目录声明不一致",
            )
    except PresetExtractionError as exc:
        return _failure(
            preset,
            extractor_version=preset.extractor_version,
            attempted_at=attempted_at,
            code=exc.code,
            message=exc.message,
            evidence=exc.evidence,
        )

    try:
        source_url = urlparse(preset.doc_url)
        async with httpx.AsyncClient(
            timeout=PRESET_DOCUMENT_TIMEOUT,
            follow_redirects=True,
            headers={"Accept": "text/html,application/xhtml+xml,application/json"},
        ) as client:
            async with client.stream("GET", preset.doc_url) as response:
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower() or None
                final_url = str(response.url)
                parsed_final_url = urlparse(final_url)
                if (
                    parsed_final_url.scheme != "https"
                    or not parsed_final_url.netloc
                    or parsed_final_url.hostname != source_url.hostname
                ):
                    return _failure(
                        preset,
                        extractor_version=extractor.version,
                        attempted_at=attempted_at,
                        code="document_unsafe_redirect",
                        message="官方文档网页重定向到不安全地址",
                        http_status=response.status_code,
                        content_type=content_type,
                        final_url=final_url,
                        evidence={"final_url": final_url},
                    )
                content_length = response.headers.get("content-length")
                if content_length and content_length.isdigit() and int(content_length) > PRESET_DOCUMENT_MAX_BYTES:
                    return _failure(
                        preset,
                        extractor_version=extractor.version,
                        attempted_at=attempted_at,
                        code="document_too_large",
                        message="官方文档网页响应超过大小限制",
                        http_status=response.status_code,
                        content_type=content_type,
                        final_url=final_url,
                        body_size=int(content_length),
                        evidence={"content_length": int(content_length)},
                    )
                chunks: list[bytes] = []
                body_size = 0
                async for chunk in response.aiter_bytes():
                    body_size += len(chunk)
                    if body_size > PRESET_DOCUMENT_MAX_BYTES:
                        return _failure(
                            preset,
                            extractor_version=extractor.version,
                            attempted_at=attempted_at,
                            code="document_too_large",
                            message="官方文档网页响应超过大小限制",
                            http_status=response.status_code,
                            content_type=content_type,
                            final_url=final_url,
                            body_sha256=hashlib.sha256(b"".join(chunks)).hexdigest() if chunks else None,
                            body_size=body_size,
                            evidence={"content_length": content_length},
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)
    except httpx.HTTPError as exc:
        return _failure(
            preset,
            extractor_version=extractor.version,
            attempted_at=attempted_at,
            code="document_http_error",
            message="官方文档网页获取失败",
            evidence={"error_type": type(exc).__name__},
        )

    body_hash = hashlib.sha256(body).hexdigest()
    response_context = {
        "http_status": response.status_code,
        "content_type": content_type,
        "final_url": final_url,
        "body_sha256": body_hash,
        "body_size": len(body),
    }
    if response.status_code < 200 or response.status_code >= 300:
        return _failure(
            preset,
            extractor_version=extractor.version,
            attempted_at=attempted_at,
            code="document_http_status",
            message="官方文档网页返回非成功状态",
            http_status=response.status_code,
            content_type=content_type,
            final_url=final_url,
            body=body,
            evidence=response_context,
        )
    if len(body) > PRESET_DOCUMENT_MAX_BYTES:
        return _failure(
            preset,
            extractor_version=extractor.version,
            attempted_at=attempted_at,
            code="document_too_large",
            message="官方文档网页响应超过大小限制",
            http_status=response.status_code,
            content_type=content_type,
            final_url=final_url,
            body=body,
            evidence=response_context,
        )
    if content_type not in _ALLOWED_CONTENT_TYPES:
        return _failure(
            preset,
            extractor_version=extractor.version,
            attempted_at=attempted_at,
            code="document_not_html",
            message="官方文档网页不是 HTML 内容",
            http_status=response.status_code,
            content_type=content_type,
            final_url=final_url,
            body=body,
            evidence=response_context,
        )
    try:
        document = body.decode("utf-8")
    except UnicodeDecodeError:
        return _failure(
            preset,
            extractor_version=extractor.version,
            attempted_at=attempted_at,
            code="document_invalid_encoding",
            message="官方文档网页不是有效的 UTF-8 内容",
            http_status=response.status_code,
            content_type=content_type,
            final_url=final_url,
            body=body,
            evidence=response_context,
        )
    try:
        extracted: ExtractionResult = extractor.extract(document)
    except PresetExtractionError as exc:
        return _failure(
            preset,
            extractor_version=extractor.version,
            attempted_at=attempted_at,
            code=exc.code,
            message=exc.message,
            http_status=response.status_code,
            content_type=content_type,
            final_url=final_url,
            body=body,
            evidence={**response_context, **exc.evidence},
        )
    return PresetDiscoveryResult(
        status="succeeded",
        preset_id=preset.preset_id,
        source_url=preset.doc_url,
        extractor_key=extractor.key,
        extractor_version=extractor.version,
        model_ids=extracted.model_ids,
        evidence={**response_context, **extracted.evidence},
        http_status=response.status_code,
        content_type=content_type,
        final_url=final_url,
        body_sha256=body_hash,
        body_size=len(body),
        attempted_at=attempted_at,
    )


def make_snapshot(result: PresetDiscoveryResult, *, upstream_id: str) -> PresetDocumentSnapshot:
    if result.status != "succeeded" or not result.body_sha256:
        raise ValueError("只有成功的预设发现结果可以生成快照")
    snapshot_id = result.snapshot_id or uuid7()
    result.snapshot_id = snapshot_id
    return PresetDocumentSnapshot(
        id=snapshot_id,
        preset_id=result.preset_id,
        upstream_id=upstream_id,
        source_url=result.source_url,
        http_status=result.http_status,
        content_type=result.content_type,
        final_url=result.final_url,
        body_sha256=result.body_sha256,
        body_size=result.body_size,
        extractor_key=result.extractor_key,
        extractor_version=result.extractor_version,
        model_ids=result.model_ids,
        evidence_json=result.evidence,
        fetched_at=result.attempted_at,
    )


def make_failure(result: PresetDiscoveryResult, *, upstream_id: str) -> PresetDiscoveryFailure:
    if result.status != "failed" or not result.failure_code or not result.failure_message:
        raise ValueError("只有失败的预设发现结果可以生成失败记录")
    return PresetDiscoveryFailure(
        id=uuid7(),
        preset_id=result.preset_id,
        upstream_id=upstream_id,
        source_url=result.source_url,
        extractor_key=result.extractor_key,
        extractor_version=result.extractor_version,
        failure_code=result.failure_code,
        failure_message=result.failure_message,
        http_status=result.http_status,
        content_type=result.content_type,
        final_url=result.final_url,
        body_sha256=result.body_sha256,
        body_size=result.body_size or None,
        evidence_json=result.evidence,
        occurred_at=result.attempted_at,
    )
