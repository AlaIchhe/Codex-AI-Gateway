"""上游超时回归：任何上游路径都必须有有限超时，禁止 timeout=None 永久挂起。"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from codex_ai_gateway.domain import upstream_client
from codex_ai_gateway.domain.upstream_client import UpstreamClient
from codex_ai_gateway.integrations.secret_store import InMemorySecretStore
from codex_ai_gateway.models.entities import Upstream


def _upstream(base_url: str) -> Upstream:
    return Upstream(
        id="up-timeout",
        name="timeout-upstream",
        base_url=base_url,
        auth_credential_ref="upstream:up-timeout",
        created_at="2026-09-20T00:00:00+00:00",
        updated_at="2026-09-20T00:00:00+00:00",
    )


class _RecordingClient:
    """记录 AsyncClient 的 timeout，随后立即失败以终止调用。"""

    recorded: list[httpx.Timeout] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        _RecordingClient.recorded.append(kwargs.get("timeout"))

    async def __aenter__(self) -> _RecordingClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def build_request(self, *args: Any, **kwargs: Any) -> Any:
        raise _Stop()

    async def aclose(self) -> None:
        return None


class _Stop(Exception):
    pass


def test_all_upstream_paths_use_finite_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingClient.recorded = []
    monkeypatch.setattr(upstream_client.httpx, "AsyncClient", _RecordingClient)
    client = UpstreamClient(InMemorySecretStore())
    upstream = _upstream("https://upstream.test/v1")

    async def call_all() -> None:
        with pytest.raises(_Stop):
            await client.request(upstream, path="/v1/chat/completions", method="POST", json_body={})
        with pytest.raises(_Stop):
            async for _ in client.stream(
                upstream, path="/v1/chat/completions", method="POST", json_body={}
            ):
                pass
        with pytest.raises(_Stop):
            await client.open_stream(
                upstream, path="/v1/chat/completions", method="POST", json_body={}
            )

    asyncio.run(call_all())

    assert len(_RecordingClient.recorded) == 3
    for timeout in _RecordingClient.recorded:
        assert isinstance(timeout, httpx.Timeout)
        # 任何一项为 None 都代表无限等待，正是线上挂死的成因。
        assert timeout.connect is not None
        assert timeout.read is not None
        assert timeout.write is not None
        assert timeout.pool is not None
        assert timeout.connect == upstream_client.CONNECT_TIMEOUT_SECONDS


def test_open_stream_times_out_when_upstream_never_responds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(upstream_client, "STREAM_READ_TIMEOUT_SECONDS", 0.4)
    accepted = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.set()
        try:
            await asyncio.sleep(5)
        finally:
            writer.close()

    async def main() -> None:
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = UpstreamClient(InMemorySecretStore())
        upstream = _upstream(f"http://127.0.0.1:{port}")
        try:
            with pytest.raises(httpx.TimeoutException):
                await client.open_stream(
                    upstream, path="/v1/responses", method="POST", json_body={}
                )
        finally:
            # 3.12 起 wait_closed() 会等待已有连接结束，这里只关闭监听。
            server.close()

    asyncio.run(asyncio.wait_for(main(), timeout=15))
    assert accepted.is_set()
