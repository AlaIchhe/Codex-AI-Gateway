"""应用运行依赖容器。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_ai_gateway.domain.circuit_breaker import CircuitBreaker
from codex_ai_gateway.domain.upstream_client import UpstreamClient
from codex_ai_gateway.integrations.secret_store import SecretStore
from codex_ai_gateway.persistence.file_store import StateStore
from codex_ai_gateway.persistence.usage_log import UsageLog
from codex_ai_gateway.services.gateway_token import create_gateway_token, get_or_create_signing_key
from codex_ai_gateway.services.updater import UpdateService

LEGACY_COOLDOWN_LABEL = "请求失败，冷却中"


@dataclass
class Runtime:
    data_dir: Path
    state_store: StateStore
    usage_log: UsageLog
    secret_store: SecretStore
    upstream_client: UpstreamClient
    circuit_breaker: CircuitBreaker
    updater: UpdateService
    signing_key: bytes

    @classmethod
    def create(cls, data_dir: Path, *, secret_store: SecretStore | None = None) -> Runtime:
        runtime_secret = secret_store or SecretStore()
        runtime_secret.verify_usable()
        state_store = StateStore(data_dir)
        usage_log = UsageLog(data_dir)
        signing_key = get_or_create_signing_key(runtime_secret)
        return cls(
            data_dir=data_dir,
            state_store=state_store,
            usage_log=usage_log,
            secret_store=runtime_secret,
            upstream_client=UpstreamClient(runtime_secret),
            circuit_breaker=CircuitBreaker(),
            updater=UpdateService(data_dir),
            signing_key=signing_key,
        )

    def ensure_gateway_token(self) -> None:
        state = self.state_store.read_state()
        if state.gateway_tokens:
            return
        token, raw = create_gateway_token(self.signing_key)
        self.secret_store.set_secret("gateway:token", raw)
        self.state_store.mutate(lambda s: s.gateway_tokens.append(token))

    def run_recovery(self) -> dict[str, int]:
        result = self.usage_log.run_recovery()
        self._migrate_legacy_health_labels()
        return result

    def _migrate_legacy_health_labels(self) -> None:
        """清除旧版 upstream 级冷却遗留的 health 文案（该机制已删除）。"""
        state = self.state_store.read_state()
        if not any(
            upstream.last_health_result == LEGACY_COOLDOWN_LABEL
            for upstream in state.upstreams
        ):
            return

        def apply(current: Any) -> None:
            for index, upstream in enumerate(current.upstreams):
                if upstream.last_health_result == LEGACY_COOLDOWN_LABEL:
                    current.upstreams[index] = upstream.model_copy(
                        update={"last_health_result": "请求失败"}
                    )

        self.state_store.mutate(apply, trigger="migration.legacy_cooldown_label")
