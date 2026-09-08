"""网关自更新：语义化版本、清单校验、更新策略与状态。

替代旧 ``scripts/auto-update.sh`` 的“字符串相等 + 从 main 拉脚本执行”逻辑：

* 版本发现走 Release 资产 ``latest.json``（含 sha256），带 ETag 条件请求、
  落盘缓存与失败退避，不再每 5 分钟裸打 GitHub API；
* 语义化版本比较，只升不降，支持 ``auto / notify / pinned`` 三种策略；
* 安装由 ``scripts/deploy-linux.sh`` 事务化执行（校验 sha256、健康检查、失败回滚）；
* 状态写入 ``data/update-state.json``，供 ``/admin/update/*`` 与管理页读取。

参考 opencodex ``src/update/`` 与 CodexPlusPlus ``crates/codex-plus-core/src/update.rs``。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import httpx

from codex_ai_gateway.persistence.atomic_writer import atomic_write_json
from codex_ai_gateway.util import utc_now

DEFAULT_REPO = "AlaIchhe/Codex-AI-Gateway"
MANIFEST_NAME = "latest.json"
MANIFEST_CACHE_SECONDS = 6 * 3600
BACKOFF_BASE_SECONDS = 300.0
BACKOFF_MAX_SECONDS = 3600.0
DEFAULT_RETAINED_RELEASES = 5
POLICY_FILE_NAME = "update-policy.conf"
STATE_FILE_NAME = "update-state.json"

_VERSION_PATTERN = re.compile(
    r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?"
    r"(?:-([0-9A-Za-z.\-]+))?(?:\+[0-9A-Za-z.\-]+)?$"
)

_UPDATE_SERVICE = "codex-ai-gateway-update.service"


class UpdateError(Exception):
    """自更新过程中的可预期错误。"""


class UpdatePolicy(str, Enum):
    """自更新策略。"""

    auto = "auto"
    notify = "notify"
    pinned = "pinned"


class InstallStatus(str, Enum):
    idle = "idle"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    sha256: str | None = None
    size: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "url": self.url, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class ReleaseManifest:
    version: str
    notes_url: str | None = None
    published_at: str | None = None
    assets: tuple[ReleaseAsset, ...] = field(default_factory=tuple)

    def archive(self) -> ReleaseAsset | None:
        """选择安装包资产：优先 codex-ai-gateway 的 zip。"""
        zips = [asset for asset in self.assets if asset.name.endswith(".zip")]
        if not zips:
            return None
        zips.sort(key=lambda a: (0 if "codex-ai-gateway" in a.name else 1, a.name))
        return zips[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "notes_url": self.notes_url,
            "published_at": self.published_at,
            "assets": [asset.to_dict() for asset in self.assets],
        }


def parse_version(text: str | None) -> tuple[Any, ...] | None:
    """解析 ``v1.2.3[-pre]`` 为可比较元组；非法返回 None。"""
    if not text:
        return None
    match = _VERSION_PATTERN.match(text.strip())
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    patch = int(match.group(3) or 0)
    prerelease = match.group(4)
    if prerelease is None:
        return (major, minor, patch, 1, ())
    identifiers: list[tuple[int, Any]] = []
    for part in prerelease.split("."):
        if part.isdigit():
            identifiers.append((0, int(part)))
        else:
            identifiers.append((1, part))
    return (major, minor, patch, 0, tuple(identifiers))


def is_newer(candidate: str | None, current: str | None) -> bool:
    """candidate 是否严格新于 current；任一侧不可解析时返回 False（只升不降）。"""
    parsed_candidate = parse_version(candidate)
    if parsed_candidate is None:
        return False
    if not current:
        return True
    parsed_current = parse_version(current)
    if parsed_current is None:
        return False
    return parsed_candidate > parsed_current


def parse_manifest(payload: Any) -> ReleaseManifest:
    """校验并解析 ``latest.json``。"""
    if not isinstance(payload, dict):
        raise UpdateError("更新清单必须是 JSON 对象。")
    version = payload.get("version")
    if not isinstance(version, str) or not version.strip():
        raise UpdateError("更新清单缺少 version。")
    raw_assets = payload.get("assets")
    assets: list[ReleaseAsset] = []
    if isinstance(raw_assets, list):
        for item in raw_assets:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            url = item.get("url")
            if not isinstance(name, str) or not isinstance(url, str):
                continue
            sha256 = item.get("sha256")
            size = item.get("size")
            assets.append(
                ReleaseAsset(
                    name=name,
                    url=url,
                    sha256=sha256 if isinstance(sha256, str) and sha256 else None,
                    size=size if isinstance(size, int) else None,
                )
            )
    manifest = ReleaseManifest(
        version=version.strip(),
        notes_url=payload.get("notes_url") if isinstance(payload.get("notes_url"), str) else None,
        published_at=(
            payload.get("published_at")
            if isinstance(payload.get("published_at"), str)
            else None
        ),
        assets=tuple(assets),
    )
    if manifest.archive() is None:
        raise UpdateError("更新清单没有可用的 zip 资产。")
    return manifest


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _NotModified(Exception):
    """HTTP 304：沿用缓存清单。"""


class UpdateService:
    """读取策略、检查 Release 清单并请求事务化安装。"""

    def __init__(
        self,
        data_dir: Path,
        *,
        app_root: Path | None = None,
        repo: str | None = None,
        clock: Any = time.time,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.app_root = Path(app_root) if app_root else self.data_dir.parent
        self.repo = repo or os.environ.get("CODEX_AI_GATEWAY_UPDATE_REPO", DEFAULT_REPO)
        self.clock = clock
        self.policy_path = self.data_dir / POLICY_FILE_NAME
        self.state_path = self.data_dir / STATE_FILE_NAME

    # -- 构造 -------------------------------------------------------------
    @classmethod
    def from_env(cls) -> UpdateService:
        from codex_ai_gateway.persistence.file_store import default_data_dir

        raw_dir = os.environ.get("CODEX_AI_GATEWAY_DATA_DIR")
        data_dir = Path(raw_dir) if raw_dir else default_data_dir()
        raw_root = os.environ.get("CODEX_AI_GATEWAY_APP_ROOT")
        app_root = Path(raw_root) if raw_root else None
        return cls(data_dir, app_root=app_root)

    # -- 路径 -------------------------------------------------------------
    @property
    def releases_dir(self) -> Path:
        return self.app_root / "releases"

    @property
    def deployed_version_path(self) -> Path:
        return self.app_root / "deployed-version"

    @property
    def deployed_build_path(self) -> Path:
        return self.app_root / "deployed-build"

    @property
    def managed(self) -> bool:
        return self.releases_dir.is_dir() and self.deployed_version_path.exists()

    def deployed_version(self) -> str | None:
        try:
            value = self.deployed_version_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    def deployed_build(self) -> str | None:
        try:
            value = self.deployed_build_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    def retained_releases(self) -> int:
        if not self.releases_dir.is_dir():
            return 0
        return sum(1 for item in self.releases_dir.iterdir() if item.is_dir())

    # -- 策略 -------------------------------------------------------------
    def read_policy(self) -> dict[str, str]:
        values = {
            "policy": UpdatePolicy.notify.value,
            "pinned_version": "",
            "dismissed_version": "",
        }
        try:
            raw = self.policy_path.read_text(encoding="utf-8")
        except OSError:
            return values
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key in values:
                values[key] = value.strip()
        if values["policy"] not in {item.value for item in UpdatePolicy}:
            values["policy"] = UpdatePolicy.notify.value
        return values

    def write_policy(
        self,
        *,
        policy: str | None = None,
        pinned_version: str | None = None,
        dismissed_version: str | None = None,
    ) -> dict[str, str]:
        current = self.read_policy()
        if policy is not None:
            if policy not in {item.value for item in UpdatePolicy}:
                raise UpdateError(f"未知的更新策略：{policy}")
            current["policy"] = policy
        if pinned_version is not None:
            current["pinned_version"] = pinned_version.strip()
        if dismissed_version is not None:
            current["dismissed_version"] = dismissed_version.strip()
        if current["policy"] == UpdatePolicy.pinned.value and not current["pinned_version"]:
            raise UpdateError("pinned 策略必须提供 pinned_version。")
        lines = [
            "# Codex AI Gateway 自更新策略（由管理端写入，请勿手工编辑）。",
            f"policy={current['policy']}",
            f"pinned_version={current['pinned_version']}",
            f"dismissed_version={current['dismissed_version']}",
            "",
        ]
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.policy_path.write_text("\n".join(lines), encoding="utf-8")
        return self.read_policy()

    # -- 状态 -------------------------------------------------------------
    def read_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def write_state(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.state_path, state)

    def patch_state(self, **updates: Any) -> dict[str, Any]:
        state = self.read_state()
        state.update(updates)
        self.write_state(state)
        return state

    # -- 清单 -------------------------------------------------------------
    def manifest_url(self, tag: str | None) -> str:
        if tag:
            return (
                f"https://github.com/{self.repo}/releases/download/{tag}/{MANIFEST_NAME}"
            )
        return f"https://github.com/{self.repo}/releases/latest/download/{MANIFEST_NAME}"

    async def _fetch_manifest(
        self, tag: str | None, *, etag: str | None
    ) -> tuple[ReleaseManifest, str | None]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "codex-ai-gateway-updater",
        }
        if etag:
            headers["If-None-Match"] = etag
        url = self.manifest_url(tag)
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise UpdateError(f"更新清单请求失败：{exc}") from exc
        if response.status_code == 304:
            raise _NotModified()
        if response.status_code == 404:
            raise UpdateError("目标 Release 未提供 latest.json 清单（可能是不支持清单的旧版本）。")
        if response.status_code >= 400:
            raise UpdateError(f"更新清单请求失败：HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpdateError("更新清单不是合法 JSON。") from exc
        return parse_manifest(payload), response.headers.get("etag")

    def _cached_manifest(self, state: dict[str, Any], tag: str | None) -> ReleaseManifest | None:
        if state.get("manifest_tag") != (tag or "latest"):
            return None
        raw = state.get("manifest")
        if not isinstance(raw, dict):
            return None
        try:
            return parse_manifest(raw)
        except UpdateError:
            return None

    def _in_backoff(self, state: dict[str, Any], now: float) -> bool:
        next_check = state.get("next_check_at")
        return isinstance(next_check, int | float) and now < float(next_check)

    # -- 检查 -------------------------------------------------------------
    async def check(self, *, force: bool = False) -> dict[str, Any]:
        policy = self.read_policy()
        state = self.read_state()
        now = self.clock()
        tag = (
            policy["pinned_version"]
            if policy["policy"] == UpdatePolicy.pinned.value and policy["pinned_version"]
            else None
        )
        manifest = self._cached_manifest(state, tag)
        error: str | None = None
        fetched_at = state.get("fetched_at")
        fresh = (
            manifest is not None
            and isinstance(fetched_at, int | float)
            and now - float(fetched_at) < MANIFEST_CACHE_SECONDS
        )
        should_fetch = force or not fresh
        if should_fetch and not force and self._in_backoff(state, now):
            should_fetch = False
            error = state.get("last_check_error")
        if should_fetch:
            try:
                fetched, etag = await self._fetch_manifest(tag, etag=state.get("etag"))
            except _NotModified:
                fetched, etag = manifest, state.get("etag")
                if fetched is None:
                    error = "更新清单未修改但本地无缓存。"
            except UpdateError as exc:
                error = str(exc)
                failures = int(state.get("fail_count") or 0) + 1
                backoff = min(BACKOFF_BASE_SECONDS * (2 ** (failures - 1)), BACKOFF_MAX_SECONDS)
                state["fail_count"] = failures
                state["last_check_error"] = error
                state["next_check_at"] = now + backoff
            else:
                manifest = fetched
                state["fail_count"] = 0
                state["last_check_error"] = None
                state["manifest"] = fetched.to_dict()
                state["manifest_tag"] = tag or "latest"
                state["fetched_at"] = now
                state["next_check_at"] = now + MANIFEST_CACHE_SECONDS
                if etag:
                    state["etag"] = etag
        state["last_check_at"] = utc_now()
        self.write_state(state)
        return self.status(policy=policy, state=state, manifest=manifest, check_error=error)

    # -- 计划 -------------------------------------------------------------
    def _resolve_action(
        self,
        policy: dict[str, str],
        manifest: ReleaseManifest | None,
        deployed: str | None,
    ) -> tuple[str, str, str | None]:
        target = manifest.version if manifest else None
        if not self.managed:
            return "none", "not_managed", target
        if manifest is None:
            return "none", "no_manifest", target
        if target == deployed:
            return "none", "up_to_date", target
        if policy["policy"] == UpdatePolicy.notify.value:
            # notify: never auto-install, but the operator may trigger it manually.
            return "none", "policy_notify", target
        if not deployed:
            return "install", "not_installed", target
        if policy["policy"] == UpdatePolicy.pinned.value:
            return "install", "pinned_target", target
        if not is_newer(target, deployed):
            return "none", "no_newer_version", target
        if (
            policy["policy"] == UpdatePolicy.auto.value
            and policy["dismissed_version"]
            and target == policy["dismissed_version"]
        ):
            return "none", "dismissed", target
        return "install", "newer_available", target

    def _update_available(
        self,
        action: str,
        reason: str,
        target: str | None,
        deployed: str | None,
    ) -> bool:
        if action == "install":
            return True
        if reason != "policy_notify" or not target:
            return False
        if target == deployed:
            return False
        return not deployed or is_newer(target, deployed)

    def status(
        self,
        *,
        policy: dict[str, str] | None = None,
        state: dict[str, Any] | None = None,
        manifest: ReleaseManifest | None = None,
        check_error: str | None = None,
    ) -> dict[str, Any]:
        policy = policy or self.read_policy()
        state = state if state is not None else self.read_state()
        if manifest is None:
            manifest = self._cached_manifest(
                state,
                policy["pinned_version"]
                if policy["policy"] == UpdatePolicy.pinned.value
                else None,
            )
        deployed = self.deployed_version()
        action, reason, target = self._resolve_action(policy, manifest, deployed)
        update_available = self._update_available(action, reason, target, deployed)
        archive = manifest.archive() if manifest else None
        latest = manifest.version if manifest else state.get("latest_version")
        return {
            "managed": self.managed,
            "app_root": str(self.app_root),
            "current_version": deployed,
            "current_build": self.deployed_build(),
            "policy": policy["policy"],
            "pinned_version": policy["pinned_version"] or None,
            "dismissed_version": policy["dismissed_version"] or None,
            "latest_version": latest,
            "target_version": target,
            "update_available": update_available,
            "action": action,
            "action_reason": reason,
            "notes_url": manifest.notes_url if manifest else None,
            "published_at": manifest.published_at if manifest else None,
            "target_sha256": archive.sha256 if archive else None,
            "last_check_at": state.get("last_check_at"),
            "last_check_error": check_error if check_error is not None else state.get("last_check_error"),
            "next_check_at": state.get("next_check_at"),
            "install_status": state.get("install_status", InstallStatus.idle.value),
            "install_version": state.get("install_version"),
            "install_requested_at": state.get("install_requested_at"),
            "install_finished_at": state.get("install_finished_at"),
            "install_error": state.get("install_error"),
            "retained_releases": self.retained_releases(),
        }

    def plan(self, *, policy: dict[str, str] | None = None) -> dict[str, Any]:
        """给 shell 安装器使用的纯本地决策（不发起网络请求）。"""
        policy = policy or self.read_policy()
        state = self.read_state()
        tag = (
            policy["pinned_version"]
            if policy["policy"] == UpdatePolicy.pinned.value and policy["pinned_version"]
            else None
        )
        manifest = self._cached_manifest(state, tag)
        deployed = self.deployed_version()
        action, reason, target = self._resolve_action(policy, manifest, deployed)
        archive = manifest.archive() if manifest else None
        return {
            "action": action,
            "reason": reason,
            "policy": policy["policy"],
            "current_version": deployed,
            "target_version": target,
            "url": archive.url if archive else None,
            "sha256": archive.sha256 if archive else None,
            "notes_url": manifest.notes_url if manifest else None,
        }

    # -- 安装 -------------------------------------------------------------
    def request_install(self, *, force: bool = False) -> dict[str, Any]:
        current = self.status()
        target = current["target_version"] or current["latest_version"] or current["current_version"]
        if not self.managed:
            return {**current, "started": False, "message": "当前进程不是 systemd 托管部署，无法自更新。"}
        if not force and not current["update_available"]:
            return {**current, "started": False, "message": "没有可安装的新版本。"}
        if not target:
            return {**current, "started": False, "message": "没有可安装的目标版本。"}
        try:
            subprocess.run(
                ["systemctl", "start", "--no-block", _UPDATE_SERVICE],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            message = getattr(exc, "stderr", None) or str(exc)
            self.patch_state(install_status=InstallStatus.failed.value, install_error=message)
            return {**self.status(), "started": False, "message": f"无法触发更新服务：{message}"}
        self.patch_state(
            install_status=InstallStatus.running.value,
            install_version=target,
            install_requested_at=utc_now(),
            install_finished_at=None,
            install_error=None,
        )
        return {**self.status(), "started": True, "message": f"已请求安装 {target}。"}

    def record_install(
        self, *, status: str, version: str | None = None, error: str | None = None
    ) -> dict[str, Any]:
        if status not in {item.value for item in InstallStatus}:
            raise UpdateError(f"未知的安装状态：{status}")
        updates: dict[str, Any] = {
            "install_status": status,
            "install_error": error,
            "install_finished_at": utc_now() if status != InstallStatus.running.value else None,
        }
        if version:
            updates["install_version"] = version
        self.patch_state(**updates)
        return self.status()