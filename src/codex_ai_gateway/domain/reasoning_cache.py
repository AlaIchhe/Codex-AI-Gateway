"""工具调用回合的 reasoning 回放缓存（DeepSeek / TokenDance thinking 模式）。

背景：thinking 模式要求带 ``tool_calls`` 的 assistant 消息把上一轮的
``reasoning_content`` 原样回传给上游，否则上游直接 400：

    The `reasoning_content` in the thinking mode must be passed back to the API.

Codex 通常在 Responses 历史里回放 ``reasoning`` item，网关据此还原
``reasoning_content``。但这段 reasoning 会丢：``/compact`` 压缩掉它、
会话中途换了上游（上一个上游不是 thinking 模型）、或上游自己没回传
``summary``。丢掉之后网关只能补占位符——格式能过，但还原不了思考内容。

参照 opencodex ``src/responses/reasoning-replay-cache.ts``：网关亲自把真实
reasoning 交给 Codex 的那一刻，按 call_id 存一份，下一轮命中就回填真实值。

隐私：只存进程内存、绝不写日志、绝不落盘、不导出；容量按「条数 + 总字节 +
TTL」三重上限收敛，超出按 LRU 淘汰。键是上游生成的 call_id：它由上游为每次
工具调用生成，跨会话撞车只会把一个「本来就缺失」的 reasoning 换成另一个值，
不会影响请求能否被接受，所以不需要额外拼会话身份。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable

DEFAULT_MAX_ENTRIES = 64
DEFAULT_MAX_TOTAL_BYTES = 256 * 1024
DEFAULT_TTL_SECONDS = 3600.0


class ReasoningReplayCache:
    """(call_id) -> 真实 reasoning_content 的有界内存缓存。"""

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_entries = max(1, max_entries)
        self._max_total_bytes = max(1, max_total_bytes)
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, str, int]] = OrderedDict()
        self._total_bytes = 0
        self._lock = threading.Lock()

    def put(self, call_id: str | None, text: str | None) -> None:
        """存一段真实 reasoning。空值或超上限的条目不存。"""
        if not call_id or not isinstance(text, str) or not text.strip():
            return
        size = len(text.encode("utf-8"))
        if size > self._max_total_bytes:
            return
        with self._lock:
            self._prune_expired_locked()
            previous = self._entries.pop(call_id, None)
            if previous is not None:
                self._total_bytes -= previous[2]
            self._entries[call_id] = (self._clock() + self._ttl_seconds, text, size)
            self._total_bytes += size
            self._prune_overflow_locked()

    def get(self, call_id: str | None) -> str | None:
        """取回真实 reasoning；未命中或已过期返回 None。"""
        if not call_id:
            return None
        with self._lock:
            entry = self._entries.get(call_id)
            if entry is None:
                return None
            expires_at, text, size = entry
            if expires_at <= self._clock():
                self._entries.pop(call_id, None)
                self._total_bytes -= size
                return None
            self._entries.move_to_end(call_id)
            return text

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._total_bytes = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    def _prune_expired_locked(self) -> None:
        now = self._clock()
        for call_id in [
            call_id
            for call_id, (expires_at, _text, _size) in self._entries.items()
            if expires_at <= now
        ]:
            _expires_at, _text, size = self._entries.pop(call_id)
            self._total_bytes -= size

    def _prune_overflow_locked(self) -> None:
        while self._entries and (
            len(self._entries) > self._max_entries
            or self._total_bytes > self._max_total_bytes
        ):
            _call_id, (_expires_at, _text, size) = self._entries.popitem(last=False)
            self._total_bytes -= size
