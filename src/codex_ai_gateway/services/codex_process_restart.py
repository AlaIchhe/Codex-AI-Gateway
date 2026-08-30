"""实现 Codex 进程强制关闭逻辑（标准库版）。

识别：cmdline 包含 codex 且非网关自身/非 pkill/pgrep/ps 工具/自身。
关闭：SIGTERM -> 等 grace_seconds -> SIGKILL。
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any

# Windows 无 SIGKILL；生产为 Linux，SIGKILL 存在。这里兼容以保证平台无关测试。
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

_GATEWAY_MARKERS = ("uvicorn codex_ai_gateway", "codex_ai_gateway.app:app")
_TOOL_MARKERS = ("pgrep", "pkill", "ps -eo", "ps aux")
# 仅重启驻留 app-server/proxy 形态的 Codex 进程（它们启动时加载配置并缓存）；
# 纯交互式 codex（无 app-server/proxy 参数）在下一次运行时会自然读取新配置，
# 且可能正处于用户会话中，不应强制中断。
_DAEMON_MARKERS = ("app-server", "proxy")


def _iter_codex_pids() -> list[int]:
    """返回需要重启的 Codex 驻留进程 PID（排除网关自身与当前进程）。"""
    current_pid = os.getpid()
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        cmd = parts[1]
        if pid == current_pid:
            continue
        if "codex" not in cmd.lower():
            continue
        if any(marker in cmd for marker in _GATEWAY_MARKERS):
            continue
        if any(marker in cmd for marker in _TOOL_MARKERS):
            continue
        # 仅命中驻留形态（app-server / proxy），跳过纯交互式 codex
        if not any(marker in cmd for marker in _DAEMON_MARKERS):
            continue
        pids.append(pid)
    return pids


def restart_codex_processes(*, grace_seconds: float = 3.0) -> dict[str, Any]:
    """终止驻留的 Codex 进程，使新配置在下次启动时生效。

    返回摘要: found、terminated（SIGTERM 后退出）、killed（SIGKILL）、survived（未能终止）。
    """
    pids = _iter_codex_pids()
    if not pids:
        return {"found": 0, "terminated": [], "killed": [], "survived": []}
    summary: dict[str, Any] = {
        "found": len(pids),
        "terminated": [],
        "killed": [],
        "survived": [],
    }
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            summary["survived"].append(pid)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        alive = [_alive(pid) for pid in pids]
        if not any(alive):
            break
        time.sleep(0.2)
    for pid in pids:
        if _alive(pid):
            try:
                os.kill(pid, _SIGKILL)
                summary["killed"].append(pid)
            except (ProcessLookupError, PermissionError):
                summary["survived"].append(pid)
        else:
            summary["terminated"].append(pid)
    return summary


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
