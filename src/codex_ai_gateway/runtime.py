"""应用运行依赖容器。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codex_ai_gateway.domain.upstream_client import UpstreamClient
from codex_ai_gateway.integrations.secret_store import SecretStore
from codex_ai_gateway.persistence.file_store import StateStore
from codex_ai_gateway.persistence.usage_log import UsageLog
from codex_ai_gateway.services.gateway_token import create_gateway_token, get_or_create_signing_key


@dataclass
class Runtime:
    data_dir: Path
    state_store: StateStore
    usage_log: UsageLog
    secret_store: SecretStore
    upstream_client: UpstreamClient
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
        return self.usage_log.run_recovery()
