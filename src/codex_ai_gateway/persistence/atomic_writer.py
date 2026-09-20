"""原子 JSON 写入器。

在目标同目录创建临时文件，写入后 fsync，再通过 os.replace 原子替换目标。
目标文件权限限制为 0600，数据目录权限为 0700。启动时清理崩溃残留临时文件。

并发安全要点：残留清理只删除「足够旧」的临时文件（默认 300s），否则另一个
进程/线程正在写入的新临时文件会被误删，随后的 os.replace 抛 FileNotFoundError
（线上表现为 500）。同时 os.replace 遇到临时文件消失时重建临时文件重试。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

# 残留临时文件的最短年龄：比它新的临时文件视为「正在写入」，不清理。
CRASH_RESIDUE_MIN_AGE_SECONDS = 300.0
# os.replace 因临时文件消失而失败时的重试次数。
WRITE_ATTEMPTS = 3


def ensure_secure_dir(path: Path) -> None:
    """确保数据目录存在且权限 0700。"""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        # 某些平台（如 Windows）不支持 chmod，忽略。
        pass


def _write_once(path: Path, payload: bytes, mode: int) -> None:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp.",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp_path, mode)
        except OSError:
            pass
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """原子写入字节内容；临时文件被并发清理时自动重试。"""
    path = Path(path)
    ensure_secure_dir(path.parent)
    for attempt in range(1, WRITE_ATTEMPTS + 1):
        try:
            _write_once(path, data, mode)
            return
        except FileNotFoundError:
            if attempt >= WRITE_ATTEMPTS:
                raise


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    mode: int = 0o600,
) -> None:
    """原子写入 JSON 文档。"""
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    atomic_write_bytes(path, payload, mode=mode)


def cleanup_crash_residue(
    path: Path,
    *,
    min_age_seconds: float = CRASH_RESIDUE_MIN_AGE_SECONDS,
) -> None:
    """清理目录内残留的临时文件（只清理足够旧的）。"""
    directory = Path(path).parent
    if not directory.exists():
        return
    deadline = time.time() - max(min_age_seconds, 0.0)
    for candidate in directory.glob(f".{Path(path).name}.tmp.*"):
        try:
            if candidate.stat().st_mtime > deadline:
                # 可能是其他进程正在写入的临时文件，保留。
                continue
            candidate.unlink(missing_ok=True)
        except OSError:
            pass
