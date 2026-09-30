"""Codex CLI（``codex`` 命令）版本探测与更新。

探测只认显式证据，不做任何推测：

* 本地版本：直接执行 ``codex --version``，输出形如 ``codex-cli 0.149.0``。
  明确区分「未安装」（PATH 上找不到可执行文件）与「装了但跑不起来」
  （可执行文件存在、``--version`` 非零退出，例如缺平台二进制），后者如实上报，
  绝不去别处捞一份旧版把真实故障盖住。
* 最新版本：npm registry 的 dist-tags 端点取 ``latest``；该来源不可用时退回
  GitHub Releases（tag ``rust-v<version>``）。实测两处同源同值。

更新按探测到的安装形态选择，并在结束后重新执行 ``codex --version`` 复核，
版本未变即判失败（不产生「升级成功但版本号不变」的假成功）：

* npm 全局安装（入口真身位于 ``node_modules``）→ ``npm i -g @openai/codex@latest``。
* 独立二进制（入口本身即可执行文件）→ 从 registry.npmjs.org 下载平台包
  ``@openai/codex@<version>-<platform>``，用 registry 的 ``dist.integrity``
  校验后解出 ``package/vendor/*/bin/`` 下的可执行文件，原子替换入口同目录的
  同名文件（先备份为 ``<name>.bak-<旧版本>``）。

  为什么用 npm registry 而不是 GitHub Release 资产：实测部署机上 GitHub 资产
  直连被限速到约 0.5 KB/s（108 MB 需数十小时），而 registry.npmjs.org 直连
  10.5 MB/s；两者是同一份 vendor 构建。

更新在后台线程执行，状态落 ``data/codex-cli-state.json``，供管理页轮询。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from codex_ai_gateway.persistence.atomic_writer import atomic_write_json, ensure_secure_dir
from codex_ai_gateway.services.updater import parse_version
from codex_ai_gateway.util import utc_now

CODEX_PACKAGE = "@openai/codex"
NPM_REGISTRY = "https://registry.npmjs.org"
GITHUB_REPO = "openai/codex"

STATE_FILE_NAME = "codex-cli-state.json"
PROBE_TIMEOUT_SECONDS = 20.0
DOWNLOAD_TIMEOUT_SECONDS = 900.0

# 状态里保留的日志行数上限。
LOG_LIMIT = 200

_VERSION_IN_TEXT = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?")


class CodexCliError(Exception):
    """Codex CLI 更新过程中的可预期错误。"""


@dataclass(frozen=True)
class LocalProbe:
    """``codex --version`` 探测结果。

    ``installed_but_broken`` 用于区分「未安装」与「装了但跑不起来」，
    前端据此展示不同提示，无需靠错误文案反推语义。
    """

    path: str | None
    version: str | None
    installed_but_broken: bool
    error: str | None

    @property
    def installed(self) -> bool:
        return self.path is not None

    @property
    def runnable(self) -> bool:
        return self.path is not None and self.version is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "version": self.version,
            "installed_but_broken": self.installed_but_broken,
            "error": self.error,
        }

def extract_version(text: str | None) -> str | None:
    """从 ``codex-cli 0.149.0`` 这类输出里取出语义版本号。"""
    if not text:
        return None
    match = _VERSION_IN_TEXT.search(text)
    return match.group(0) if match else None


def compare_versions(left: str | None, right: str | None) -> int | None:
    """比较两个版本号；任一侧无法解析时返回 None（调用方自行决定策略）。"""
    if not left or not right:
        return None
    parsed_left = parse_version(left)
    parsed_right = parse_version(right)
    if parsed_left is None or parsed_right is None:
        return None
    if parsed_left == parsed_right:
        return 0
    return 1 if parsed_left > parsed_right else -1


def platform_suffix() -> str | None:
    """返回 npm 平台包后缀（如 ``linux-x64``）；不支持的平台返回 None。"""
    system = platform.system().lower()
    machine = platform.machine().lower()
    os_name = {"linux": "linux", "darwin": "darwin", "windows": "win32"}.get(system)
    arch = {
        "x86_64": "x64",
        "amd64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine)
    if not os_name or not arch:
        return None
    return f"{os_name}-{arch}"


def resolve_codex_path() -> str | None:
    """定位 codex 可执行文件：显式配置 > PATH > 常见安装目录。"""
    configured = os.environ.get("CODEX_AI_GATEWAY_CODEX_CLI_PATH")
    if configured:
        return configured if os.path.exists(configured) else None
    found = shutil.which("codex")
    if found:
        return found
    for candidate in (Path.home() / ".local/bin/codex", Path("/usr/local/bin/codex")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def detect_install_kind(path: str | None) -> str:
    """判定安装形态：``npm`` / ``standalone`` / ``unknown``。

    判据是入口解析后的真身路径是否位于 ``node_modules`` 内——npm 全局安装的
    ``bin/codex`` 只是指向包内 launcher 的 shim；独立安装的真身就是可执行文件本身。
    """
    if not path:
        return "unknown"
    try:
        real = Path(os.path.realpath(path))
    except OSError:
        return "unknown"
    if "node_modules" in real.parts:
        return "npm"
    if real.is_file() and os.access(real, os.X_OK):
        return "standalone"
    return "unknown"


def _tail(text: str, limit: int = 500) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return "…" + cleaned[-limit:]


def probe_local(path: str | None = None) -> LocalProbe:
    """执行 ``codex --version`` 并解析结果。"""
    target = path or resolve_codex_path()
    if not target:
        return LocalProbe(
            path=None,
            version=None,
            installed_but_broken=False,
            error="未在 PATH 与环境配置中找到 codex 命令。",
        )
    try:
        completed = subprocess.run(
            [target, "--version"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return LocalProbe(
            path=target,
            version=None,
            installed_but_broken=True,
            error=f"`{target} --version` 超时（>{PROBE_TIMEOUT_SECONDS:.0f}s）。",
        )
    except OSError as exc:
        return LocalProbe(
            path=target,
            version=None,
            installed_but_broken=True,
            error=f"无法执行 `{target} --version`：{exc}",
        )

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if completed.returncode == 0:
        version = extract_version(stdout) or extract_version(stderr)
        if version:
            return LocalProbe(path=target, version=version, installed_but_broken=False, error=None)
        return LocalProbe(
            path=target,
            version=None,
            installed_but_broken=True,
            error=f"`{target} --version` 未输出可识别的版本号：{_tail(stdout or stderr)}",
        )
    diagnostic = stderr.strip() or stdout.strip()
    return LocalProbe(
        path=target,
        version=None,
        installed_but_broken=True,
        error=(
            f"`{target} --version` 以退出码 {completed.returncode} 结束："
            f"{_tail(diagnostic) or '无输出'}"
        ),
    )


def _npm_tarball_url(version: str, suffix: str) -> str:
    return f"{NPM_REGISTRY}/{CODEX_PACKAGE}/-/{Path(CODEX_PACKAGE).name}-{version}-{suffix}.tgz"


def _npm_version_metadata_url(version: str, suffix: str) -> str:
    return f"{NPM_REGISTRY}/{CODEX_PACKAGE}/{version}-{suffix}"


def target_label(version: str, suffix: str) -> str:
    return f"{CODEX_PACKAGE}@{version}-{suffix}"

class CodexCliService:
    """Codex CLI 探测与更新服务。

    ``data_dir`` 用于落状态文件；``prober`` / ``client_factory`` 供测试注入。
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        prober: Any = None,
        client_factory: Any = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.state_path = self.data_dir / STATE_FILE_NAME
        self._prober = prober or probe_local
        self._client_factory = client_factory or (lambda **kwargs: httpx.Client(**kwargs))
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # -- 状态文件 ---------------------------------------------------------
    def read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def write_state(self, state: dict[str, Any]) -> dict[str, Any]:
        ensure_secure_dir(self.data_dir)
        atomic_write_json(self.state_path, state)
        return state

    def patch_state(self, **updates: Any) -> dict[str, Any]:
        state = self.read_state()
        state.update(updates)
        return self.write_state(state)

    def _log(self, message: str) -> None:
        state = self.read_state()
        lines = list(state.get("update_log") or [])
        lines.append(f"{utc_now()} {message}")
        self.patch_state(update_log=lines[-LOG_LIMIT:])

    # -- 探测 -------------------------------------------------------------
    def local(self) -> LocalProbe:
        return self._prober()

    async def fetch_latest(self, client: httpx.AsyncClient) -> dict[str, Any]:
        """取远程最新版本，返回 ``{version, source, error}``。"""
        npm_error: str | None = None
        try:
            response = await client.get(
                f"{NPM_REGISTRY}/-/package/{CODEX_PACKAGE.replace('/', '%2f')}/dist-tags"
            )
            response.raise_for_status()
            tags = response.json()
            latest = tags.get("latest") if isinstance(tags, dict) else None
            if isinstance(latest, str) and latest.strip():
                return {"version": latest.strip(), "source": "npm-dist-tags", "error": None}
            npm_error = "npm dist-tags 中没有 latest 字段。"
        except Exception as exc:  # noqa: BLE001 - 网络失败如实上报并尝试下一来源
            npm_error = f"npm registry 查询失败：{exc}"
        try:
            response = await client.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "codex-ai-gateway",
                },
            )
            response.raise_for_status()
            payload = response.json()
            tag = payload.get("tag_name") if isinstance(payload, dict) else None
            version = extract_version(tag if isinstance(tag, str) else None)
            if version:
                return {"version": version, "source": "github-releases", "error": None}
            return {
                "version": None,
                "source": "github-releases",
                "error": "GitHub Release tag 无法解析出语义版本。",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "version": None,
                "source": None,
                "error": f"{npm_error}；GitHub Releases 查询失败：{exc}",
            }

    # -- 状态视图 ---------------------------------------------------------
    def status(self) -> dict[str, Any]:
        state = self.read_state()
        probe = self.local()
        latest_version = state.get("latest_version")
        comparison = compare_versions(latest_version, probe.version)
        update_available = bool(probe.version and latest_version and comparison == 1)
        return {
            **probe.to_dict(),
            "installed": probe.installed,
            "runnable": probe.runnable,
            "install_kind": detect_install_kind(probe.path),
            "latest_version": latest_version,
            "latest_source": state.get("latest_source"),
            "last_check_at": state.get("last_check_at"),
            "last_check_error": state.get("last_check_error"),
            "update_available": update_available,
            "update_status": state.get("update_status", "idle"),
            "update_target": state.get("update_target"),
            "update_started_at": state.get("update_started_at"),
            "update_finished_at": state.get("update_finished_at"),
            "update_error": state.get("update_error"),
            "update_log": list(state.get("update_log") or [])[-LOG_LIMIT:],
        }

    async def check(self) -> dict[str, Any]:
        """刷新远程最新版本并落盘。"""
        async with httpx.AsyncClient(
            timeout=PROBE_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            result = await self.fetch_latest(client)
        self.patch_state(
            latest_version=result["version"],
            latest_source=result["source"],
            last_check_at=utc_now(),
            last_check_error=result["error"],
        )
        return self.status()
    # -- 更新 -------------------------------------------------------------
    def request_update(self, *, force: bool = False) -> dict[str, Any]:
        """后台线程执行更新；进行中的任务会挡下重复请求。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return {**self.status(), "started": False, "message": "更新正在进行中。"}
            current = self.status()
            target = current.get("latest_version") or current.get("version")
            if not current["installed"]:
                return {**current, "started": False, "message": "未检测到 codex 命令，无法更新。"}
            if not target:
                return {
                    **current,
                    "started": False,
                    "message": "尚无已知的目标版本，请先执行检查更新。",
                }
            if not force and not current["update_available"]:
                return {**current, "started": False, "message": "当前已是最新版本。"}
            self.patch_state(
                update_status="running",
                update_target=target,
                update_started_at=utc_now(),
                update_finished_at=None,
                update_error=None,
                update_log=[],
            )
            thread = threading.Thread(
                target=self._run_update,
                args=(target,),
                name="codex-cli-update",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        return {**self.status(), "started": True, "message": f"已开始更新到 {target}。"}

    def _finish(self, *, status: str, error: str | None = None) -> None:
        self.patch_state(
            update_status=status,
            update_error=error,
            update_finished_at=utc_now(),
        )

    def _run_update(self, target: str) -> None:
        probe = self.local()
        path = probe.path
        kind = detect_install_kind(path)
        try:
            if path is None:
                raise CodexCliError("未找到 codex 可执行文件。")
            self._log(f"检测到安装形态：{kind}，入口：{path}，当前版本：{probe.version or '未知'}")
            if kind == "npm":
                self._update_via_npm(target)
            elif kind == "standalone":
                self._update_standalone(target, path, probe.version)
            else:
                raise CodexCliError(
                    f"无法判定的安装形态（入口 {path}）：既不是 npm 全局安装，也不是独立可执行文件。"
                )
        except Exception as exc:  # noqa: BLE001 - 失败必须落盘为明确状态
            self._log(f"更新失败：{exc}")
            self._finish(status="failed", error=str(exc))
            return

        after = self.local()
        if after.version != target:
            self._log(
                f"更新后复核 `codex --version` 得到 {after.version or '无法解析'}，"
                f"与目标 {target} 不一致。"
            )
            detail = f"；{after.error}" if after.error else ""
            self._finish(
                status="failed",
                error=(
                    f"更新后版本仍为 {after.version or '未知'}，未达到目标 {target}{detail}。"
                ),
            )
            return
        self._log(f"更新完成，当前版本 {after.version}。")
        self._finish(status="succeeded")

    def _update_via_npm(self, target: str) -> None:
        npm = shutil.which("npm")
        if not npm:
            raise CodexCliError("npm 全局安装形态，但 PATH 上找不到 npm。")
        self._log(f"执行 npm 全局升级到 {target}。")
        completed = subprocess.run(
            [npm, "install", "-g", f"{CODEX_PACKAGE}@{target}"],
            capture_output=True,
            text=True,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode != 0:
            raise CodexCliError(
                f"npm install 退出码 {completed.returncode}："
                f"{_tail(completed.stderr or completed.stdout or '无输出')}"
            )
    def _update_standalone(self, target: str, path: str, current_version: str | None) -> None:
        suffix = platform_suffix()
        if not suffix:
            raise CodexCliError(
                f"不支持的平台：{platform.system()} {platform.machine()}，无法确定对应平台包。"
            )
        install_dir = Path(path).parent
        with self._client_factory(
            timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            expected = self._fetch_integrity(client, target, suffix)
            archive = self._download(client, target, suffix, install_dir)
            staged = install_dir / f".codex-cli-stage-{os.getpid()}"
            try:
                self._verify_integrity(archive, expected)
                self._log(f"平台包校验通过：{archive.name}")
                shutil.rmtree(staged, ignore_errors=True)
                staged.mkdir(parents=True, exist_ok=True)
                try:
                    binaries = self._extract_binaries(archive, staged)
                    self._install_binaries(binaries, install_dir, current_version)
                finally:
                    shutil.rmtree(staged, ignore_errors=True)
            finally:
                archive.unlink(missing_ok=True)

    def _fetch_integrity(self, client: httpx.Client, version: str, suffix: str) -> str:
        response = client.get(_npm_version_metadata_url(version, suffix))
        response.raise_for_status()
        payload = response.json()
        dist = payload.get("dist") if isinstance(payload, dict) else None
        if isinstance(dist, dict):
            integrity = dist.get("integrity")
            if isinstance(integrity, str) and integrity.strip():
                return integrity.strip()
            shasum = dist.get("shasum")
            if isinstance(shasum, str) and shasum.strip():
                return f"sha1-{shasum.strip()}"
        raise CodexCliError(f"registry 未提供 {target_label(version, suffix)} 的校验值。")

    def _download(
        self, client: httpx.Client, version: str, suffix: str, install_dir: Path
    ) -> Path:
        url = _npm_tarball_url(version, suffix)
        self._log(f"下载 {url}")
        archive = install_dir / f".codex-cli-{version}-{suffix}.tgz"
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with archive.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
        self._log(f"下载完成：{archive.stat().st_size} 字节")
        return archive

    def _verify_integrity(self, archive: Path, expected: str) -> None:
        algorithm, _, encoded = expected.partition("-")
        if algorithm not in {"sha512", "sha256", "sha1"} or not encoded:
            raise CodexCliError(f"不支持的校验算法：{expected}")
        digest = hashlib.new(algorithm)
        with archive.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = base64.b64encode(digest.digest()).decode("ascii")
        if actual != encoded:
            raise CodexCliError(f"平台包校验失败：期望 {algorithm} {encoded}，实际 {actual}")

    @staticmethod
    def _extract_binaries(archive: Path, dest: Path) -> dict[str, Path]:
        """解出 ``package/vendor/<triple>/bin/<name>`` 下的可执行文件。"""
        extracted: dict[str, Path] = {}
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                parts = PurePosixPath(member.name).parts
                if len(parts) != 5 or parts[:2] != ("package", "vendor") or parts[3] != "bin":
                    continue
                name = parts[4]
                if not name or name in extracted:
                    continue
                source = tar.extractfile(member)
                if source is None:
                    continue
                target = dest / name
                with source, target.open("wb") as handle:
                    shutil.copyfileobj(source, handle, length=1024 * 1024)
                target.chmod(0o755)
                extracted[name] = target
        if not extracted:
            raise CodexCliError("平台包内未找到 vendor/*/bin/ 下的可执行文件。")
        return extracted

    def _install_binaries(
        self, binaries: dict[str, Path], install_dir: Path, current_version: str | None
    ) -> None:
        """原子替换入口目录下的同名文件；只动原本就存在的兄弟可执行文件。"""
        replaced: list[str] = []
        for name, staged in sorted(binaries.items()):
            existing = install_dir / name
            if name != "codex" and not existing.exists():
                self._log(f"跳过 {name}：入口目录中原本没有该文件。")
                continue
            if existing.exists():
                backup = install_dir / f"{name}.bak-{current_version or 'unknown'}"
                if not backup.exists():
                    shutil.copy2(existing, backup, follow_symlinks=False)
                    self._log(f"已备份 {existing} → {backup.name}")
            temporary = install_dir / f".{name}.new-{os.getpid()}"
            shutil.copy2(staged, temporary)
            temporary.chmod(0o755)
            os.replace(temporary, existing)
            replaced.append(name)
        if "codex" not in replaced:
            raise CodexCliError("未替换 codex 主程序，安装中止。")
        self._log(f"已替换：{', '.join(replaced)}")
