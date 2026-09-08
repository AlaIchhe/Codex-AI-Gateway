"""运行时恢复测试：清理旧版 upstream 级冷却遗留文案。"""

from __future__ import annotations

from codex_ai_gateway.integrations.secret_store import InMemorySecretStore
from codex_ai_gateway.models.entities import Upstream
from codex_ai_gateway.runtime import LEGACY_COOLDOWN_LABEL, Runtime


def _legacy_upstream() -> Upstream:
    return Upstream(
        id="up-legacy",
        name="legacy",
        base_url="https://example.test/v1",
        auth_credential_ref="upstream:up-legacy",
        last_health_result=LEGACY_COOLDOWN_LABEL,
        created_at="2026-08-30T00:00:00+00:00",
        updated_at="2026-08-30T00:00:00+00:00",
    )


def test_run_recovery_migrates_legacy_cooldown_label(tmp_path) -> None:
    runtime = Runtime.create(tmp_path, secret_store=InMemorySecretStore())
    upstream = _legacy_upstream()
    runtime.state_store.mutate(lambda state: state.upstreams.append(upstream))

    runtime.run_recovery()
    assert runtime.state_store.read_state().upstreams[0].last_health_result == "请求失败"

    # 幂等：再次恢复不应产生变化。
    runtime.run_recovery()
    assert runtime.state_store.read_state().upstreams[0].last_health_result == "请求失败"


def test_run_recovery_keeps_other_labels(tmp_path) -> None:
    runtime = Runtime.create(tmp_path, secret_store=InMemorySecretStore())
    upstream = _legacy_upstream().model_copy(update={"last_health_result": "探测完成"})
    runtime.state_store.mutate(lambda state: state.upstreams.append(upstream))

    runtime.run_recovery()
    assert runtime.state_store.read_state().upstreams[0].last_health_result == "探测完成"