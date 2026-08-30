"""原子 JSON 写入器。

在目标同目录创建临时文件，写入后 fsync，再通过 os.replace 原子替换目标。
目标文件权限限制为 0600，数据目录权限为 0700。启动时清理崩溃残留临时文件。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def ensure_secure_dir(path: Path) -> None:
    """确保数据目录存在且权限 0700。"""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        # 某些平台（如 Windows）不支持 chmod，忽略。
        pass


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    mode: int = 0o600,
) -> None:
    """原子写入 JSON 文档。"""
    path = Path(path)
    ensure_secure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp.",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
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
    """原子写入字节内容。"""
    path = Path(path)
    ensure_secure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp.",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
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


def cleanup_crash_residue(path: Path) -> None:
    """清理目录内残留的临时文件。"""
    directory = Path(path).parent
    if not directory.exists():
        return
    for candidate in directory.glob(f".{Path(path).name}.tmp.*"):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass
