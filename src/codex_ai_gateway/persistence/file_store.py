"""admin-state 文件存储：schema 校验 + 进程写锁 + 进程间 flock。"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codex_ai_gateway.models.entities import ManagementStateRevision
from codex_ai_gateway.models.state import SCHEMA_VERSION, AdminState
from codex_ai_gateway.persistence.atomic_writer import (
    atomic_write_json,
    cleanup_crash_residue,
    ensure_secure_dir,
)
from codex_ai_gateway.persistence.locks import FileLock


class SchemaMismatchError(RuntimeError):
    pass


def _normalize_legacy_wire_protocols(raw: dict[str, Any]) -> dict[str, Any]:
    """规范化历史状态中已废弃的 adaptive 协议。"""
    for upstream in raw.get("upstreams", []):
        if not isinstance(upstream, dict):
            continue
        confirmed = upstream.get("confirmed_protocols")
        if isinstance(confirmed, list):
            upstream["confirmed_protocols"] = [
                "unconfirmed" if protocol == "adaptive" else protocol
                for protocol in confirmed
            ]
    for offering in raw.get("offerings", []):
        if isinstance(offering, dict) and offering.get("wire_protocol") == "adaptive":
            offering["wire_protocol"] = "unconfirmed"
    return raw


def _migrate_schema_v1_to_v2(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("schema_version") != 1:
        return raw
    migrated = _normalize_legacy_wire_protocols(raw)
    preset_upstream_ids = {
        str(item.get("id"))
        for item in migrated.get("upstreams", [])
        if isinstance(item, dict) and item.get("kind") == "preset"
    }
    if preset_upstream_ids:
        removed_offering_ids = {
            str(item.get("id"))
            for item in migrated.get("offerings", [])
            if isinstance(item, dict) and item.get("upstream_id") in preset_upstream_ids
        }
        removed_canonical_ids = {
            str(item.get("canonical_model_id"))
            for item in migrated.get("offerings", [])
            if isinstance(item, dict)
            and item.get("upstream_id") in preset_upstream_ids
            and item.get("canonical_model_id")
        }
        removed_canonical_openrouter_ids = {
            str(item.get("openrouter_model_id"))
            for item in migrated.get("model_mappings", [])
            if isinstance(item, dict)
            and item.get("offering_id") in removed_offering_ids
            and item.get("openrouter_model_id")
        }
        for model in migrated.get("canonical_models", []):
            if (
                isinstance(model, dict)
                and (
                    model.get("id") in removed_canonical_ids
                    or model.get("openrouter_model_id") in removed_canonical_openrouter_ids
                )
            ):
                model["status"] = "unavailable"
        migrated["offerings"] = [
            item
            for item in migrated.get("offerings", [])
            if not isinstance(item, dict) or item.get("upstream_id") not in preset_upstream_ids
        ]
        migrated["model_mappings"] = [
            item
            for item in migrated.get("model_mappings", [])
            if not isinstance(item, dict) or item.get("offering_id") not in removed_offering_ids
        ]
    migrated.setdefault("preset_snapshots", [])
    migrated.setdefault("preset_discovery_failures", [])
    migrated.setdefault("preset_discovery_states", [])
    migrated["schema_version"] = 2
    return migrated


class StateStore:
    """管理 admin-state.json 的读写。

    每次修改都持有：一个进程内 threading.RLock（保护同一进程并发），以及一个
    跨进程 FileLock（防止命令行维护或异常重入）。写入前读校验 schema 版本，
    写入后原子替换并 fsync。
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.state_path = self.data_dir / "admin-state.json"
        self.lock_path = self.data_dir / ".admin-state.lock"
        self._local = threading.RLock()
        self._cache: AdminState | None = None

    def _load_unlocked(self) -> AdminState:
        if not self.state_path.exists():
            state = AdminState()
            atomic_write_json(self.state_path, state.model_dump(mode="json"))
            return state
        cleanup_crash_residue(self.state_path)
        try:
            with self.state_path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except json.JSONDecodeError as exc:
            raise SchemaMismatchError("admin-state.json 损坏") from exc
        raw = _normalize_legacy_wire_protocols(raw)
        version = raw.get("schema_version")
        if version == 1 and SCHEMA_VERSION == 2:
            raw = _migrate_schema_v1_to_v2(raw)
            atomic_write_json(self.state_path, raw)
            version = raw.get("schema_version")
        if version != SCHEMA_VERSION:
            raise SchemaMismatchError(
                f"admin-state schema 版本不匹配: 期望 {SCHEMA_VERSION}, 实际 {version}"
            )
        try:
            return AdminState.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            raise SchemaMismatchError(f"admin-state 校验失败: {exc}") from exc

    def load(self) -> AdminState:
        with self._local:
            return self._load_unlocked()

    def mutate(self, fn: Callable[[AdminState], None], *, trigger: str = "management_change") -> AdminState:
        """在锁内加载、修改、生成修订快照并写回，返回新状态。"""
        with self._local:
            with FileLock(self.lock_path):
                state = self._load_unlocked()
                before = state.model_dump(mode="json")
                fn(state)
                changed_paths = sorted(
                    key for key, value in state.model_dump(mode="json").items()
                    if key != "management_revisions" and before.get(key) != value
                )
                revision_payload = state.model_dump(mode="json", exclude={"management_revisions"})
                state.management_revisions.append(
                    ManagementStateRevision(
                        id=hashlib.sha256(
                            (revision_payload.__repr__() + str(datetime.now(UTC))).encode()
                        ).hexdigest()[:32],
                        trigger=trigger,
                        state_hash=hashlib.sha256(
                            str(revision_payload).encode()
                        ).hexdigest(),
                        changed_paths=changed_paths,
                        accepted_at=datetime.now(UTC).isoformat(),
                    )
                )
                state.management_revisions = state.management_revisions[-500:]
                atomic_write_json(
                    self.state_path,
                    state.model_dump(mode="json"),
                )
                self._cache = state
                return state

    def save(self, state: AdminState) -> AdminState:
        """在不调用回调的情况下直接写回整个状态。"""
        with self._local:
            with FileLock(self.lock_path):
                atomic_write_json(
                    self.state_path,
                    state.model_dump(mode="json"),
                )
                self._cache = state
                return state

    def read_state(self) -> AdminState:
        """返回当前状态（优先缓存，无缓存则读盘）。"""
        with self._local:
            if self._cache is not None:
                return self._cache
            return self._load_unlocked()

    def from_state(self, state: AdminState) -> StateStore:
        self._cache = state
        return self


def default_data_dir() -> Path:
    """默认数据目录：XDG_STATE_HOME 或 ~/.local/state 下。"""
    import os

    home = os.path.expanduser("~")
    state_home = os.environ.get("XDG_STATE_HOME") or f"{home}/.local/state"
    override = os.environ.get("CODEX_AI_GATEWAY_DATA_DIR")
    if override:
        return Path(override)
    return Path(state_home) / "codex-ai-gateway"


def init_data_dir(data_dir: Path) -> None:
    """初始化数据目录结构与权限。"""
    data_dir = Path(data_dir)
    ensure_secure_dir(data_dir)
    for sub in ("catalog/publications", "integration/revisions", "usage/pending", "preset-snapshots"):
        ensure_secure_dir(data_dir / sub)
    (data_dir / "usage/events").mkdir(parents=True, exist_ok=True)
    (data_dir / "reports/cache").mkdir(parents=True, exist_ok=True)
    try:
        (data_dir / "usage/events").chmod(0o700)
        (data_dir / "reports/cache").chmod(0o700)
    except OSError:
        pass
