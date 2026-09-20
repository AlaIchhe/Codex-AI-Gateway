"""状态落盘并发回归：残留清理不得误删在写的临时文件，token 使用时间落盘节流。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codex_ai_gateway.api import gateway
from codex_ai_gateway.persistence import atomic_writer
from codex_ai_gateway.persistence.file_store import StateStore
from codex_ai_gateway.services.gateway_token import create_gateway_token

SIGNING_KEY = b"0123456789abcdef0123456789abcdef"


def test_cleanup_crash_residue_keeps_fresh_tmp_files(tmp_path: Path) -> None:
    state_path = tmp_path / "admin-state.json"
    fresh = tmp_path / ".admin-state.json.tmp.fresh"
    fresh.write_text("{}", encoding="utf-8")
    stale = tmp_path / ".admin-state.json.tmp.stale"
    stale.write_text("{}", encoding="utf-8")
    old = time.time() - 3600.0
    os.utime(stale, (old, old))

    atomic_writer.cleanup_crash_residue(state_path)

    # 新临时文件可能属于正在写入的进程，必须保留；旧的崩溃残留照常清理。
    assert fresh.exists()
    assert not stale.exists()


def test_atomic_write_json_retries_when_residue_cleanup_removes_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "admin-state.json"
    real_replace = os.replace
    calls: list[str] = []

    def replace_after_residue_cleanup(src: Any, dst: Any) -> None:
        calls.append("replace")
        if len(calls) == 1:
            # 模拟另一进程的残留清理把正在写的临时文件删掉。
            atomic_writer.cleanup_crash_residue(target, min_age_seconds=0.0)
        real_replace(src, dst)

    monkeypatch.setattr(atomic_writer.os, "replace", replace_after_residue_cleanup)

    atomic_writer.atomic_write_json(target, {"schema_version": 2})

    assert len(calls) == 2
    assert json.loads(target.read_text(encoding="utf-8")) == {"schema_version": 2}
    assert not list(tmp_path.glob(".admin-state.json.tmp.*"))


def test_state_mutate_survives_concurrent_state_load(tmp_path: Path) -> None:
    """复现线上 500：另一实例 load() 触发残留清理，不能打断本次写盘。"""
    writer = StateStore(tmp_path)
    reader = StateStore(tmp_path)
    writer.mutate(lambda s: s.gateway_tokens.append(_new_token()))

    real_replace = os.replace

    def replace_with_concurrent_load(src: Any, dst: Any) -> None:
        reader.load()
        real_replace(src, dst)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(atomic_writer.os, "replace", replace_with_concurrent_load)
        writer.mutate(lambda s: s.gateway_tokens.append(_new_token()))

    assert len(StateStore(tmp_path).load().gateway_tokens) == 2


def test_gateway_token_touch_is_throttled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = StateStore(tmp_path)
    token, raw = create_gateway_token(SIGNING_KEY)
    store.mutate(lambda s: s.gateway_tokens.append(token))
    runtime = SimpleNamespace(state_store=store, signing_key=SIGNING_KEY)
    request = SimpleNamespace(headers={"authorization": f"Bearer {raw}"})

    gateway._token_persisted_at.clear()
    writes = _counting_mutate(monkeypatch, store)

    for _ in range(5):
        assert gateway._authenticate(request, runtime).id == token.id

    assert writes() == 1
    persisted = StateStore(tmp_path).load().gateway_tokens[0]
    assert persisted.last_used_at is not None


def test_gateway_token_touch_persists_after_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateStore(tmp_path)
    token, raw = create_gateway_token(SIGNING_KEY)
    store.mutate(lambda s: s.gateway_tokens.append(token))
    runtime = SimpleNamespace(state_store=store, signing_key=SIGNING_KEY)
    request = SimpleNamespace(headers={"authorization": f"Bearer {raw}"})

    gateway._token_persisted_at.clear()
    gateway._token_persisted_at[token.id] = time.monotonic() - 61.0
    writes = _counting_mutate(monkeypatch, store)

    assert gateway._authenticate(request, runtime).id == token.id
    assert writes() == 1


def test_authenticate_survives_token_touch_persistence_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateStore(tmp_path)
    token, raw = create_gateway_token(SIGNING_KEY)
    store.mutate(lambda s: s.gateway_tokens.append(token))
    runtime = SimpleNamespace(state_store=store, signing_key=SIGNING_KEY)
    request = SimpleNamespace(headers={"authorization": f"Bearer {raw}"})

    gateway._token_persisted_at.clear()

    def boom(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("admin-state.json 临时文件消失")

    monkeypatch.setattr(store, "mutate", boom)

    # 使用时间属于遥测，落盘失败不能把请求打成 500。
    assert gateway._authenticate(request, runtime).id == token.id


def _new_token() -> Any:
    from codex_ai_gateway.models.entities import GatewayToken

    return GatewayToken(
        id=f"tok-{time.time_ns()}",
        lookup_hash="hash",
        prefix="gwg_test",
        last4="test",
        issued_at="2026-09-20T00:00:00+00:00",
    )


def _counting_mutate(monkeypatch: pytest.MonkeyPatch, store: StateStore) -> Any:
    counter = {"n": 0}
    original = store.mutate

    def counting_mutate(fn: Any, **kwargs: Any) -> Any:
        counter["n"] += 1
        return original(fn, **kwargs)

    monkeypatch.setattr(store, "mutate", counting_mutate)
    return lambda: counter["n"]
