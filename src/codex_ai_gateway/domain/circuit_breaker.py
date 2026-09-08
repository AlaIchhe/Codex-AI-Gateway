"""按 (upstream, provider_model) 粒度的运行时熔断/冷却。

替代旧的 ``Upstream.cooldown_until`` 全局冻结：失败只在正确的粒度上冷却，
优先采用上游 ``Retry-After`` / 配额重置时间，带抖动与上限，并对“请求本身
有问题”的错误完全不冷却。

参考 opencodex ``src/combos/failover.ts`` 的两条原则：

* 冷却作用域显式区分 ``none`` / ``target`` / ``provider``；
* 失败决策显式区分 ``hop``（换上游）与 ``stop``（直接失败）。
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any

from codex_ai_gateway.models.entities import ProviderErrorType

DEFAULT_COOLDOWN_SECONDS = 60.0
TRANSIENT_RATE_LIMIT_SECONDS = 5.0
QUOTA_COOLDOWN_SECONDS = 300.0
MAX_COOLDOWN_SECONDS = 600.0
MIN_COOLDOWN_SECONDS = 1.0
JITTER_RATIO = 0.15

_REQUEST_SHAPE_CODES = {
    "context_length_exceeded",
    "tool_catalog_too_large",
    "input_admission_refused",
    "target_incompatible",
}
_REQUEST_SHAPE_HINTS = (
    "context length",
    "context_length",
    "maximum context",
    "prompt is too long",
    "too many tokens",
    "tool catalog",
    "too many tools",
)
_ACCOUNT_QUOTA_HINTS = (
    "insufficient_quota",
    "quota exhausted",
    "usage limit reached",
    "monthly usage limit",
    "insufficient balance",
    "billing",
)


class CooldownScope(str, Enum):
    """冷却作用域。"""

    none = "none"
    target = "target"
    provider = "provider"


class FailureDecision(str, Enum):
    """失败后是否继续尝试备用上游。"""

    hop = "hop"
    stop = "stop"


@dataclass(frozen=True)
class FailureClassification:
    decision: FailureDecision
    scope: CooldownScope
    base_cooldown_seconds: float
    reason: str


@dataclass
class CooldownEntry:
    upstream_id: str
    provider_model_id: str | None
    scope: CooldownScope
    until: float
    reason: str
    status_code: int | None = None
    code: str | None = None

    def remaining(self, now: float) -> float:
        return max(0.0, self.until - now)

    def to_dict(self, now: float) -> dict[str, Any]:
        return {
            "upstream_id": self.upstream_id,
            "provider_model_id": self.provider_model_id,
            "scope": self.scope.value,
            "reason": self.reason,
            "status_code": self.status_code,
            "code": self.code,
            "remaining_seconds": round(self.remaining(now), 1),
            "until": datetime.fromtimestamp(self.until, tz=UTC).isoformat(),
        }


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """解析上游 ``Retry-After``（秒数或 HTTP-date），上限 MAX_COOLDOWN_SECONDS。"""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    current = time.time() if now is None else now
    if text.isdigit():
        return min(float(text), MAX_COOLDOWN_SECONDS)
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delay = parsed.timestamp() - current
    if delay <= 0:
        return 0.0
    return min(delay, MAX_COOLDOWN_SECONDS)


def classify_failure(
    *,
    status_code: int | None,
    error_type: str | None = None,
    code: str | None = None,
    message: str | None = None,
) -> FailureClassification:
    """把一次失败归类为 (hop|stop, none|target|provider, 默认冷却时长)。"""
    normalized_code = (code or "").strip().lower().replace("-", "_")
    text = (message or "").lower()

    if status_code == 499 or normalized_code == "origin_rejected":
        return FailureClassification(
            FailureDecision.stop, CooldownScope.none, 0.0, "origin_rejected"
        )

    # 请求形状问题：换一个上下文窗口更大的上游可能成功，但目标本身没病，不冷却。
    if normalized_code in _REQUEST_SHAPE_CODES or any(
        hint in text for hint in _REQUEST_SHAPE_HINTS
    ):
        return FailureClassification(
            FailureDecision.hop, CooldownScope.none, 0.0, "request_shape"
        )

    if error_type == ProviderErrorType.authentication.value or status_code in {401, 403}:
        return FailureClassification(
            FailureDecision.hop,
            CooldownScope.provider,
            DEFAULT_COOLDOWN_SECONDS,
            "authentication",
        )
    if error_type == ProviderErrorType.quota_budget.value or status_code == 402:
        return FailureClassification(
            FailureDecision.hop, CooldownScope.provider, QUOTA_COOLDOWN_SECONDS, "quota_budget"
        )
    if error_type == ProviderErrorType.rate_limit.value or status_code == 429:
        if any(hint in text for hint in _ACCOUNT_QUOTA_HINTS):
            return FailureClassification(
                FailureDecision.hop,
                CooldownScope.provider,
                QUOTA_COOLDOWN_SECONDS,
                "rate_limit_account",
            )
        return FailureClassification(
            FailureDecision.hop,
            CooldownScope.target,
            TRANSIENT_RATE_LIMIT_SECONDS,
            "rate_limit",
        )
    if error_type == ProviderErrorType.model_permission.value or status_code == 404:
        return FailureClassification(
            FailureDecision.hop,
            CooldownScope.target,
            DEFAULT_COOLDOWN_SECONDS,
            "model_unavailable",
        )
    if error_type == ProviderErrorType.invalid_request.value or (
        status_code is not None and 400 <= status_code < 500
    ):
        return FailureClassification(
            FailureDecision.stop, CooldownScope.none, 0.0, "invalid_request"
        )
    return FailureClassification(
        FailureDecision.hop, CooldownScope.target, DEFAULT_COOLDOWN_SECONDS, "upstream_fault"
    )


class CircuitBreaker:
    """进程内熔断表。键为 (upstream_id, provider_model_id)，provider 级用 None。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._clock = clock
        self._jitter = jitter
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str | None], CooldownEntry] = {}

    def _prune(self, now: float) -> None:
        for key in [key for key, entry in self._entries.items() if entry.until <= now]:
            self._entries.pop(key, None)

    def _targets(
        self, upstream_id: str, provider_model_id: str | None
    ) -> tuple[tuple[str, str | None], ...]:
        provider_key = (upstream_id, None)
        if provider_model_id is None:
            return (provider_key,)
        return (provider_key, (upstream_id, provider_model_id))

    def remaining(
        self, upstream_id: str, provider_model_id: str | None, *, now: float | None = None
    ) -> float | None:
        current = self._clock() if now is None else now
        with self._lock:
            self._prune(current)
            values = [
                entry.until - current
                for key in self._targets(upstream_id, provider_model_id)
                if (entry := self._entries.get(key)) is not None and entry.until > current
            ]
            return min(values) if values else None

    def is_open(
        self, upstream_id: str, provider_model_id: str | None, *, now: float | None = None
    ) -> bool:
        return self.remaining(upstream_id, provider_model_id, now=now) is not None

    def record_failure(
        self,
        upstream_id: str,
        provider_model_id: str | None,
        *,
        status_code: int | None = None,
        error_type: str | None = None,
        retry_after: str | None = None,
        code: str | None = None,
        message: str | None = None,
        now: float | None = None,
    ) -> FailureClassification:
        classification = classify_failure(
            status_code=status_code, error_type=error_type, code=code, message=message
        )
        if classification.scope is CooldownScope.none:
            return classification
        current = self._clock() if now is None else now
        requested = parse_retry_after(retry_after, now=current)
        if requested is not None:
            seconds = requested
        else:
            seconds = classification.base_cooldown_seconds
            if seconds > 0:
                seconds *= 1.0 + self._jitter() * JITTER_RATIO
        seconds = min(max(seconds, MIN_COOLDOWN_SECONDS), MAX_COOLDOWN_SECONDS)
        target_model = (
            provider_model_id if classification.scope is CooldownScope.target else None
        )
        key = (upstream_id, target_model)
        with self._lock:
            self._prune(current)
            self._entries[key] = CooldownEntry(
                upstream_id=upstream_id,
                provider_model_id=target_model,
                scope=classification.scope,
                until=current + seconds,
                reason=classification.reason,
                status_code=status_code,
                code=code,
            )
        return classification

    def record_success(
        self, upstream_id: str, provider_model_id: str | None, *, now: float | None = None
    ) -> None:
        current = self._clock() if now is None else now
        with self._lock:
            self._prune(current)
            self._entries.pop((upstream_id, provider_model_id), None)

    def clear(self, upstream_id: str | None = None) -> None:
        with self._lock:
            if upstream_id is None:
                self._entries.clear()
                return
            for key in [key for key in self._entries if key[0] == upstream_id]:
                self._entries.pop(key, None)

    def earliest_remaining(
        self,
        targets: Iterable[tuple[str, str | None]],
        *,
        now: float | None = None,
    ) -> float | None:
        current = self._clock() if now is None else now
        with self._lock:
            self._prune(current)
            values: list[float] = []
            for upstream_id, provider_model_id in targets:
                for key in self._targets(upstream_id, provider_model_id):
                    entry = self._entries.get(key)
                    if entry is not None and entry.until > current:
                        values.append(entry.until - current)
            return min(values) if values else None

    def snapshot(self, *, now: float | None = None) -> list[dict[str, Any]]:
        current = self._clock() if now is None else now
        with self._lock:
            self._prune(current)
            return [entry.to_dict(current) for entry in self._entries.values()]

    def snapshot_for(self, upstream_id: str, *, now: float | None = None) -> list[dict[str, Any]]:
        current = self._clock() if now is None else now
        with self._lock:
            self._prune(current)
            return [
                entry.to_dict(current)
                for entry in self._entries.values()
                if entry.upstream_id == upstream_id
            ]
