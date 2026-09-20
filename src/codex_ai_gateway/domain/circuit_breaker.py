"""按 (upstream, provider_model) 粒度的运行时失败避让窗口。

替代旧的 ``Upstream.cooldown_until`` 全局冻结：失败只在正确的粒度上记录，
优先采用上游 ``Retry-After`` / 配额重置时间，带抖动与上限，并对“请求本身
有问题”的错误完全不记录。

方案 A：此窗口**只用于排序降权**——``route_candidates`` 把避让中的目标排到
候选列表末尾，但永不屏蔽目标，因此不存在“全部上游均在冷却中 → 503”这种
不可路由状态（fail-open，客户端每次都拿到真实的上游错误）。

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
# 「该端点不支持此模型」是端点属性，不会在几分钟内自愈；用长窗口记住它，
# 否则每次请求都会先打一遍这个必死的端点（浪费额度、还会把真正的 429/5xx
# 错误盖成「上游不支持该模型或已下线」）。
MODEL_UNAVAILABLE_COOLDOWN_SECONDS = 6 * 3600.0
MIN_COOLDOWN_SECONDS = 1.0
JITTER_RATIO = 0.15

_REQUEST_SHAPE_CODES = {
    "context_length_exceeded",
    "provider_context_length_exceeded",
    "tool_catalog_too_large",
    "input_admission_refused",
    "target_incompatible",
}
_REQUEST_SHAPE_HINTS = (
    "context length",
    "context_length",
    "context window",
    "maximum context",
    "prompt is too long",
    "input is too long",
    "too many tokens",
    "maximum number of tokens",
    "reduce the length",
    "tool catalog",
    "too many tools",
    "上下文长度",
    "上下文超",
    "超出最大",
    "请求体过大",
)
# 内容审查拒收：对整包上下文的判定，与请求格式无关。上游本身没病（不进避让），
# 但换一个过滤词表不同的上游可能直接通过，所以必须 hop 而不是 stop。
CONTENT_POLICY_REASON = "content_policy"
_CONTENT_POLICY_CODES = {
    "provider_content_policy_blocked",
    "content_policy_violation",
}
_CONTENT_POLICY_HINTS = (
    "content exists risk",
    "datainspectionfailed",
    "content policy",
    "内容审查",
    "内容审核",
    "敏感词",
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
    cap_seconds: float = MAX_COOLDOWN_SECONDS


@dataclass
class CooldownEntry:
    upstream_id: str
    provider_model_id: str | None
    scope: CooldownScope
    until: float
    reason: str
    status_code: int | None = None
    code: str | None = None
    wire_protocol: str | None = None

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
            "wire_protocol": self.wire_protocol,
            "remaining_seconds": round(self.remaining(now), 1),
            "until": datetime.fromtimestamp(self.until, tz=UTC).isoformat(),
        }


def _protocol_key(wire_protocol: Any) -> str | None:
    """把 WireProtocol 枚举或字符串规范化为键用的字符串。"""
    if wire_protocol is None:
        return None
    value = getattr(wire_protocol, "value", wire_protocol)
    text = str(value).strip()
    return text or None


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

    # 内容审查拒收：换一个过滤词表不同的上游可能直接通过，且上游本身没病，
    # 因此 hop 但不冷却（对齐 LiteLLM：内容策略错误既不重试同一目标、也不冷却）。
    if (
        error_type == ProviderErrorType.content_policy.value
        or normalized_code in _CONTENT_POLICY_CODES
        or any(hint in text for hint in _CONTENT_POLICY_HINTS)
    ):
        return FailureClassification(
            FailureDecision.hop, CooldownScope.none, 0.0, CONTENT_POLICY_REASON
        )

    # 「不支持该模型 / 不在套餐内」常以 403/400 返回，但它是模型级事实而不是
    # 账号级鉴权失败：必须先于 401/403 分支判定，否则会被降级成 provider 级
    # 的 60 秒冷却，于是每个请求都重打一遍这个必然失败的模型。
    if error_type == ProviderErrorType.model_permission.value or status_code == 404:
        return FailureClassification(
            FailureDecision.hop,
            CooldownScope.target,
            MODEL_UNAVAILABLE_COOLDOWN_SECONDS,
            "model_unavailable",
            MODEL_UNAVAILABLE_COOLDOWN_SECONDS,
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
    """进程内熔断表。键为 (upstream_id, provider_model_id, wire_protocol)。

    provider 级用 (upstream_id, None, None)；target 级带协议，因为同一个模型在
    上游的 /responses 与 /chat/completions 是两个能力面，一个端点不支持不代表
    另一个不支持（反之亦然）。协议为 None 表示「不区分协议」的通用条目。
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._clock = clock
        self._jitter = jitter
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str | None, str | None], CooldownEntry] = {}

    def _prune(self, now: float) -> None:
        for key in [key for key, entry in self._entries.items() if entry.until <= now]:
            self._entries.pop(key, None)

    def _targets(
        self,
        upstream_id: str,
        provider_model_id: str | None,
        wire_protocol: str | None = None,
    ) -> tuple[tuple[str, str | None, str | None], ...]:
        provider_key = (upstream_id, None, None)
        if provider_model_id is None:
            return (provider_key,)
        keys = [provider_key, (upstream_id, provider_model_id, wire_protocol)]
        if wire_protocol is not None:
            keys.append((upstream_id, provider_model_id, None))
        else:
            # 未指定协议时覆盖该模型的全部协议条目，避免漏判避让。
            keys.extend(
                key
                for key in self._entries
                if key[0] == upstream_id and key[1] == provider_model_id
            )
        return tuple(keys)

    def remaining(
        self,
        upstream_id: str,
        provider_model_id: str | None,
        *,
        wire_protocol: Any = None,
        now: float | None = None,
    ) -> float | None:
        protocol = _protocol_key(wire_protocol)
        current = self._clock() if now is None else now
        with self._lock:
            self._prune(current)
            values = [
                entry.until - current
                for key in self._targets(upstream_id, provider_model_id, protocol)
                if (entry := self._entries.get(key)) is not None and entry.until > current
            ]
            return min(values) if values else None

    def is_open(
        self,
        upstream_id: str,
        provider_model_id: str | None,
        *,
        wire_protocol: Any = None,
        now: float | None = None,
    ) -> bool:
        return (
            self.remaining(
                upstream_id, provider_model_id, wire_protocol=wire_protocol, now=now
            )
            is not None
        )

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
        wire_protocol: Any = None,
        now: float | None = None,
    ) -> FailureClassification:
        protocol = _protocol_key(wire_protocol)
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
        seconds = min(
            max(seconds, MIN_COOLDOWN_SECONDS), classification.cap_seconds
        )
        is_target = classification.scope is CooldownScope.target
        target_model = provider_model_id if is_target else None
        key = (upstream_id, target_model, protocol if is_target else None)
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
                wire_protocol=key[2],
            )
        return classification

    def record_success(
        self,
        upstream_id: str,
        provider_model_id: str | None,
        *,
        wire_protocol: Any = None,
        now: float | None = None,
    ) -> None:
        """清掉本次成功的路径。

        只清本协议（以及不区分协议的通用条目）：某个端点的成功不能抹掉另一个端点
        「不支持该模型」的记忆，否则必死端点会被无限重试。"""
        protocol = _protocol_key(wire_protocol)
        current = self._clock() if now is None else now
        with self._lock:
            self._prune(current)
            self._entries.pop((upstream_id, provider_model_id, protocol), None)
            if protocol is not None:
                self._entries.pop((upstream_id, provider_model_id, None), None)

    def clear(self, upstream_id: str | None = None) -> None:
        with self._lock:
            if upstream_id is None:
                self._entries.clear()
                return
            for key in [key for key in self._entries if key[0] == upstream_id]:
                self._entries.pop(key, None)

    def earliest_remaining(
        self,
        targets: Iterable[tuple[Any, ...]],
        *,
        now: float | None = None,
    ) -> float | None:
        current = self._clock() if now is None else now
        with self._lock:
            self._prune(current)
            values: list[float] = []
            for target in targets:
                upstream_id = target[0]
                provider_model_id = target[1] if len(target) > 1 else None
                protocol = target[2] if len(target) > 2 else None
                for key in self._targets(upstream_id, provider_model_id, protocol):
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
