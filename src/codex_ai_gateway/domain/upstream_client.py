"""单次上游传输客户端：httpx 流式读取、无自动重试、取消传播、首字节计时。"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from codex_ai_gateway.integrations.secret_store import SecretStore
from codex_ai_gateway.models.entities import Upstream
from codex_ai_gateway.security.redaction import SENSITIVE_HEADERS

logger = logging.getLogger("codex_ai_gateway.upstream_client")


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


# 上游超时：任何路径都不能无限等待（timeout=None 曾让请求永久挂起，客户端只看到
# 一个永不结束的 SSE 流）。流式读超时是兜底，网关自身 120s 无数据看门狗负责先中止。
CONNECT_TIMEOUT_SECONDS = 15.0
POOL_TIMEOUT_SECONDS = 15.0
WRITE_TIMEOUT_SECONDS = 60.0
NON_STREAM_READ_TIMEOUT_SECONDS = 600.0
STREAM_READ_TIMEOUT_SECONDS = 180.0


def upstream_http_timeout(read_timeout: float) -> httpx.Timeout:
    """构造有限的上游超时；None 表示不限制，禁止使用。"""
    return httpx.Timeout(
        connect=CONNECT_TIMEOUT_SECONDS,
        read=read_timeout,
        write=WRITE_TIMEOUT_SECONDS,
        pool=POOL_TIMEOUT_SECONDS,
    )


@dataclass
class UpstreamResult:
    status_code: int
    headers: dict[str, str]
    body: bytes
    first_byte_ms: int | None


@dataclass
class UpstreamStream:
    """已发出的流式上游响应：先读 status 再决定转发或降级。"""

    status_code: int
    headers: dict[str, str]
    first_byte_ms: int | None
    _response: Any
    _client: Any

    def __aiter__(self) -> Any:
        return self._response.aiter_bytes()

    async def read_error_body(self) -> bytes:
        return await self._response.aread()

    async def aclose(self) -> None:
        await self._response.aclose()
        await self._client.aclose()


class UpstreamClient:
    """对指定上游发起单次请求，禁止自动重试。"""

    def __init__(self, secret_store: SecretStore) -> None:
        self.secret_store = secret_store

    def _upstream_url(self, upstream: Upstream, path: str) -> str:
        base = upstream.base_url.rstrip("/")
        if path.startswith("/"):
            return f"{base}{path}"
        return f"{base}/{path}"

    def _inject_headers(
        self,
        upstream: Upstream,
        incoming_headers: dict[str, str],
    ) -> dict[str, str]:
        headers = {
            k: v
            for k, v in incoming_headers.items()
            if k.lower() not in HOP_BY_HOP_HEADERS
            and k.lower() not in SENSITIVE_HEADERS
        }
        headers.update(upstream.default_headers)
        headers.pop("authorization", None)
        cred_ref = upstream.auth_credential_ref
        api_key = self.secret_store.get_secret(cred_ref)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            logger.warning("upstream credential missing: %s", cred_ref)
        return headers

    async def request(
        self,
        upstream: Upstream,
        *,
        path: str,
        method: str,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
        stream: bool = False,
    ) -> UpstreamResult:
        url = self._upstream_url(upstream, path)
        out_headers = self._inject_headers(upstream, headers or {})
        async with httpx.AsyncClient(
            timeout=upstream_http_timeout(NON_STREAM_READ_TIMEOUT_SECONDS),
            follow_redirects=False,
        ) as client:
            start = time.perf_counter()
            req = client.build_request(
                method,
                url,
                json=json_body,
                headers=out_headers,
            )
            resp = await client.send(req, stream=stream)
            first_byte_ms = int((time.perf_counter() - start) * 1000)
            if stream:
                chunks = bytearray()
                async for chunk in resp.aiter_bytes():
                    chunks.extend(chunk)
                body = bytes(chunks)
                await resp.aclose()
            else:
                body = await resp.aread()
            return UpstreamResult(
                status_code=resp.status_code,
                headers=dict(resp.headers),
                body=body,
                first_byte_ms=first_byte_ms,
            )

    async def stream(
        self,
        upstream: Upstream,
        *,
        path: str,
        method: str,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> AsyncIterator[bytes]:
        """流式读取上游响应字节。用于 SSE 直通。"""
        url = self._upstream_url(upstream, path)
        out_headers = self._inject_headers(upstream, headers or {})
        async with httpx.AsyncClient(
            timeout=upstream_http_timeout(STREAM_READ_TIMEOUT_SECONDS),
            follow_redirects=False,
        ) as client:
            req = client.build_request(
                method,
                url,
                json=json_body,
                headers=out_headers,
            )
            resp = await client.send(req, stream=True)
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()

    async def open_stream(
        self,
        upstream: Upstream,
        *,
        path: str,
        method: str,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> UpstreamStream:
        """发送流式请求并返回带 status 的响应对象，供降级决策使用。"""
        url = self._upstream_url(upstream, path)
        out_headers = self._inject_headers(upstream, headers or {})
        client = httpx.AsyncClient(
            timeout=upstream_http_timeout(STREAM_READ_TIMEOUT_SECONDS),
            follow_redirects=False,
        )
        try:
            req = client.build_request(
                method,
                url,
                json=json_body,
                headers=out_headers,
            )
            start = time.perf_counter()
            resp = await client.send(req, stream=True)
            first_byte_ms = int((time.perf_counter() - start) * 1000)
        except BaseException:
            await client.aclose()
            raise
        return UpstreamStream(
            status_code=resp.status_code,
            headers=dict(resp.headers),
            first_byte_ms=first_byte_ms,
            _response=resp,
            _client=client,
        )
