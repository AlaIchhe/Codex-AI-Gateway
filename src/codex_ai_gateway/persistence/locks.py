"""跨平台文件锁抽象。

在 POSIX 上使用 fcntl.flock，在 Windows 上使用 msvcrt.locking。两者都提供
跨进程的 advisory lock。若两者都不可用则退化为进程内 threading.Lock。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

try:  # pragma: no cover - POSIX
    import fcntl

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - Windows
    _HAS_FCNTL = False

try:  # pragma: no cover - Windows
    import msvcrt

    _HAS_MSVCRT = True
except ImportError:  # pragma: no cover - POSIX
    _HAS_MSVCRT = False


class FileLock:
    """基于路径的跨进程排他锁。"""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = Path(lock_path)
        self._fd: int | None = None
        self._local = threading.Lock()

    def __enter__(self) -> FileLock:
        self._local.acquire()
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if _HAS_FCNTL:
                fcntl.flock(fd, fcntl.LOCK_EX)
            elif _HAS_MSVCRT:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        except OSError:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._fd is not None:
            try:
                if _HAS_FCNTL:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                elif _HAS_MSVCRT:
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(self._fd)
                self._fd = None
        self._local.release()
