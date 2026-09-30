"""Codex CLI 探测与更新测试（全程离线，注入 prober / HTTP 替身）。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from codex_ai_gateway.services import codex_cli
from codex_ai_gateway.services.codex_cli import (
    CodexCliError,
    CodexCliService,
    LocalProbe,
    compare_versions,
    detect_install_kind,
    extract_version,
    platform_suffix,
)


def test_extract_version_from_cli_output() -> None:
    assert extract_version("codex-cli 0.149.0") == "0.149.0"
    assert extract_version("codex-cli 0.159.2") == "0.159.2"
    assert extract_version("codex 1.2") is None
    assert extract_version(None) is None


def test_compare_versions() -> None:
    assert compare_versions("0.149.0", "0.159.2") == -1
    assert compare_versions("0.159.2", "0.159.2") == 0
    assert compare_versions("0.160.0", "0.159.2") == 1
    assert compare_versions("local-build", "0.159.2") is None
    assert compare_versions(None, "0.159.2") is None


def test_platform_suffix_supported_and_unknown(monkeypatch) -> None:
    monkeypatch.setattr(codex_cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(codex_cli.platform, "machine", lambda: "x86_64")
    assert platform_suffix() == "linux-x64"
    monkeypatch.setattr(codex_cli.platform, "machine", lambda: "aarch64")
    assert platform_suffix() == "linux-arm64"
    monkeypatch.setattr(codex_cli.platform, "system", lambda: "Plan9")
    assert platform_suffix() is None


def test_detect_install_kind(tmp_path: Path) -> None:
    npm_entry = tmp_path / "bin/codex"
    npm_entry.parent.mkdir(parents=True)
    npm_entry.write_text("#!/usr/bin/env node", encoding="utf-8")
    package_dir = tmp_path / "lib/node_modules/@openai/codex"
    package_dir.mkdir(parents=True)
    (package_dir / "bin").mkdir()
    (package_dir / "bin/codex.js").write_text("//", encoding="utf-8")
    try:
        npm_entry.unlink()
        npm_entry.symlink_to(package_dir / "bin/codex.js")
    except (OSError, NotImplementedError):  # pragma: no cover - Windows 需开发者模式
        pytest.skip("当前平台不支持创建符号链接")
    assert detect_install_kind(str(npm_entry)) == "npm"

    plain = tmp_path / "codex"
    plain.write_text("binary", encoding="utf-8")
    assert detect_install_kind(str(plain)) == "standalone"
    assert detect_install_kind(None) == "unknown"


def test_probe_local_reports_not_installed(monkeypatch) -> None:
    monkeypatch.setattr(codex_cli, "resolve_codex_path", lambda: None)
    probe = codex_cli.probe_local()
    assert probe.installed is False
    assert probe.installed_but_broken is False
    assert probe.error


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> object:
    class _Result:
        pass

    result = _Result()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def test_probe_local_distinguishes_missing_and_broken(monkeypatch, tmp_path: Path) -> None:
    binary = tmp_path / "codex"
    binary.write_text("x", encoding="utf-8")

    monkeypatch.setattr(
        codex_cli.subprocess,
        "run",
        lambda *a, **k: _completed(0, stdout="codex-cli 0.159.2\n"),
    )
    probe = codex_cli.probe_local(str(binary))
    assert probe.version == "0.159.2"
    assert probe.installed_but_broken is False

    monkeypatch.setattr(
        codex_cli.subprocess,
        "run",
        lambda *a, **k: _completed(1, stderr="Missing optional dependency @openai/codex-linux-x64"),
    )
    broken = codex_cli.probe_local(str(binary))
    assert broken.version is None
    assert broken.installed_but_broken is True
    assert "退出码 1" in (broken.error or "")

    monkeypatch.setattr(codex_cli.subprocess, "run", lambda *a, **k: _completed(0, stdout="hi"))
    unparsable = codex_cli.probe_local(str(binary))
    assert unparsable.installed_but_broken is True
    assert unparsable.version is None


class _FakeResponse:
    def __init__(self, payload: object = None, *, content: bytes = b"", status_code: int = 200):
        self._payload = payload
        self._content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self._payload

    def iter_bytes(self, chunk_size: int | None = None) -> object:
        size = chunk_size or 8
        for start in range(0, len(self._content), size):
            yield self._content[start : start + size]


class _FakeAsyncClient:
    """只按 URL 派发响应的异步替身；未知 URL 直接报错，避免掩盖真实请求。"""

    def __init__(self, responses: dict[str, _FakeResponse], **kwargs: object) -> None:
        self._responses = responses

    async def get(self, url: str, **kwargs: object) -> _FakeResponse:
        if url not in self._responses:
            raise RuntimeError(f"unexpected url: {url}")
        return self._responses[url]


class _FakeStream:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def __enter__(self) -> _FakeResponse:
        return self._response

    def __exit__(self, *args: object) -> bool:
        return False


class _FakeSyncClient:
    def __init__(
        self,
        *,
        metadata: _FakeResponse | None = None,
        archive: _FakeResponse | None = None,
        **kwargs: object,
    ) -> None:
        self._metadata = metadata
        self._archive = archive

    def __enter__(self) -> _FakeSyncClient:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def get(self, url: str) -> _FakeResponse:
        assert self._metadata is not None, "测试未提供元数据响应"
        return self._metadata

    def stream(self, method: str, url: str) -> _FakeStream:
        assert self._archive is not None, "测试未提供归档响应"
        return _FakeStream(self._archive)


def test_fetch_latest_prefers_npm_dist_tags() -> None:
    service = CodexCliService(Path("."), prober=lambda: LocalProbe(None, None, False, None))
    url = "https://registry.npmjs.org/-/package/@openai%2fcodex/dist-tags"
    client = _FakeAsyncClient({url: _FakeResponse({"latest": "0.159.2"})})
    result = asyncio.run(service.fetch_latest(client))
    assert result == {"version": "0.159.2", "source": "npm-dist-tags", "error": None}


def test_fetch_latest_falls_back_to_github() -> None:
    service = CodexCliService(Path("."), prober=lambda: LocalProbe(None, None, False, None))
    url = "https://api.github.com/repos/openai/codex/releases/latest"
    client = _FakeAsyncClient({url: _FakeResponse({"tag_name": "rust-v0.160.0"})})
    result = asyncio.run(service.fetch_latest(client))
    assert result["version"] == "0.160.0"
    assert result["source"] == "github-releases"


def test_fetch_latest_reports_error_when_both_sources_fail() -> None:
    service = CodexCliService(Path("."), prober=lambda: LocalProbe(None, None, False, None))
    client = _FakeAsyncClient({})  # 两个来源都会抛错
    result = asyncio.run(service.fetch_latest(client))
    assert result["version"] is None
    assert result["source"] is None
    assert "npm registry 查询失败" in (result["error"] or "")
    assert "GitHub Releases 查询失败" in (result["error"] or "")

def _make_tarball(path: Path, payload: bytes) -> bytes:
    """构造与 npm 平台包同构的归档：package/vendor/<triple>/bin/<name>。"""
    triple = "x86_64-unknown-linux-musl"
    with tarfile.open(path, "w:gz") as tar:
        for name in ("codex", "codex-code-mode-host"):
            data = payload if name == "codex" else payload + b"-host"
            info = tarfile.TarInfo(f"package/vendor/{triple}/bin/{name}")
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
        noise = tarfile.TarInfo("package/package.json")
        noise.size = 2
        tar.addfile(noise, io.BytesIO(b"{}"))
    raw = path.read_bytes()
    return "sha512-" + base64.b64encode(hashlib.sha512(raw).digest()).decode("ascii")


def _standalone_fixture(tmp_path: Path, *, payload: bytes = b"NEW-BINARY") -> dict:
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    binary = install_dir / "codex"
    binary.write_bytes(b"OLD-BINARY")
    sibling = install_dir / "codex-code-mode-host"
    sibling.write_bytes(b"OLD-HOST")
    archive_path = tmp_path / "platform.tgz"
    integrity = _make_tarball(archive_path, payload)
    archive_bytes = archive_path.read_bytes()

    def prober() -> LocalProbe:
        if binary.read_bytes() == payload:
            return LocalProbe(str(binary), "0.159.2", False, None)
        return LocalProbe(str(binary), "0.149.0", False, None)

    client_factory = lambda **kwargs: _FakeSyncClient(  # noqa: E731
        metadata=_FakeResponse({"dist": {"integrity": integrity}}),
        archive=_FakeResponse(content=archive_bytes),
    )
    service = CodexCliService(tmp_path / "data", prober=prober, client_factory=client_factory)
    return {
        "service": service,
        "binary": binary,
        "sibling": sibling,
        "payload": payload,
        "install_dir": install_dir,
    }


def test_update_standalone_replaces_binary_and_verifies(tmp_path: Path) -> None:
    fixture = _standalone_fixture(tmp_path)
    service: CodexCliService = fixture["service"]
    binary: Path = fixture["binary"]
    service.patch_state(latest_version="0.159.2")

    result = service.request_update()
    assert result["started"] is True
    assert service._thread is not None
    service._thread.join(timeout=30)

    state = service.read_state()
    assert state["update_status"] == "succeeded", state.get("update_error")
    assert binary.read_bytes() == fixture["payload"]
    backup = fixture["install_dir"] / "codex.bak-0.149.0"
    assert backup.read_bytes() == b"OLD-BINARY"
    assert (fixture["install_dir"] / "codex-code-mode-host").read_bytes() == (
        fixture["payload"] + b"-host"
    )
    # 临时文件与暂存目录都已清理
    leftovers = [
        p.name for p in fixture["install_dir"].iterdir() if p.name.startswith(".codex-cli")
    ]
    assert leftovers == []
    assert service.status()["update_available"] is False


def test_update_standalone_fails_on_integrity_mismatch(tmp_path: Path) -> None:
    fixture = _standalone_fixture(tmp_path)
    service: CodexCliService = fixture["service"]
    service.patch_state(latest_version="0.159.2")
    service._client_factory = lambda **kwargs: _FakeSyncClient(  # noqa: E731
        metadata=_FakeResponse({"dist": {"integrity": "sha512-" + base64.b64encode(b"x" * 64).decode()}}),
        archive=_FakeResponse(content=(tmp_path / "platform.tgz").read_bytes()),
    )

    service.request_update()
    assert service._thread is not None
    service._thread.join(timeout=30)

    state = service.read_state()
    assert state["update_status"] == "failed"
    assert "校验失败" in (state["update_error"] or "")
    assert fixture["binary"].read_bytes() == b"OLD-BINARY"


def test_update_refuses_when_not_installed(monkeypatch, tmp_path: Path) -> None:
    service = CodexCliService(
        tmp_path,
        prober=lambda: LocalProbe(None, None, False, "未找到 codex 命令。"),
    )
    result = service.request_update()
    assert result["started"] is False
    assert "未检测到 codex 命令" in result["message"]


def test_update_refuses_when_already_latest(tmp_path: Path) -> None:
    service = CodexCliService(
        tmp_path,
        prober=lambda: LocalProbe("/usr/local/bin/codex", "0.159.2", False, None),
    )
    service.patch_state(latest_version="0.159.2")
    result = service.request_update()
    assert result["started"] is False
    assert result["update_available"] is False
    assert "最新版本" in result["message"]


def test_status_marks_update_available_and_broken_state(tmp_path: Path) -> None:
    service = CodexCliService(
        tmp_path,
        prober=lambda: LocalProbe("/usr/local/bin/codex", None, True, "--version 退出码 1"),
    )
    service.patch_state(latest_version="0.159.2")
    status = service.status()
    assert status["installed"] is True
    assert status["runnable"] is False
    assert status["installed_but_broken"] is True
    assert status["update_available"] is False

    ready = CodexCliService(
        tmp_path,
        prober=lambda: LocalProbe("/usr/local/bin/codex", "0.149.0", False, None),
    )
    ready.patch_state(latest_version="0.159.2")
    assert ready.status()["update_available"] is True


def test_api_returns_codex_cli_status(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from codex_ai_gateway.app import create_app
    from codex_ai_gateway.integrations.secret_store import InMemorySecretStore

    app = create_app(
        data_dir=tmp_path,
        secret_store=InMemorySecretStore(),
        frontend_dist=tmp_path / "missing-dist",
    )
    client = TestClient(app)
    payload = client.get("/admin/codex-cli/status").json()
    assert set(payload) >= {"installed", "runnable", "install_kind", "update_status"}
    assert payload["update_status"] == "idle"

def test_fetch_integrity_requires_checksum(tmp_path: Path) -> None:
    service = CodexCliService(tmp_path)
    client = _FakeSyncClient(metadata=_FakeResponse({"dist": {}}))
    with pytest.raises(CodexCliError):
        service._fetch_integrity(client, "0.159.2", "linux-x64")
