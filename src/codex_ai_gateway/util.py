"""通用工具：UUID v7、UTC 时间、HMAC 与常量比较。"""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from datetime import UTC, datetime


def utc_now() -> str:
    """返回 UTC ISO-8601 时间戳，带微秒。"""
    return datetime.now(UTC).isoformat()


def uuid7() -> str:
    """生成 UUID v7 字符串（毫秒级有序，Python 3.12 无内置实现）。"""
    import random
    import time

    ts_ms = int(time.time() * 1000)
    rnd_a = random.getrandbits(12)
    rnd_b = random.getrandbits(62)
    raw = (
        (ts_ms & 0xFFFFFFFFFFFF) << 80
        | 0x7 << 76
        | (rnd_a & 0xFFF) << 64
        | 0x2 << 62
        | (rnd_b & 0x3FFFFFFFFFFFFFFF)
    )
    return str(uuid.UUID(int=raw))


def utc_timestamp_to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def hmac_sha256_hex(key: bytes, message: str) -> str:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


def constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def generate_virtual_key() -> str:
    """生成形如 gwk_ 开头的随机虚拟 key。"""
    raw = os.urandom(24)
    return "gwk_" + raw.hex()


def sha256_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint_text(text: str) -> str:
    return sha256_digest(text.encode("utf-8"))
