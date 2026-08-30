"""冒烟：聚合主路径（canonical 复用 + 回退 + 状态关联）用模拟 state store 验证。"""

from __future__ import annotations

import asyncio
from typing import Any

from codex_ai_gateway.models.entities import (
    CanonicalModel,
    Offering,
    OfferingStatus,
    Upstream,
    UpstreamKind,
    UpstreamStatus,
    WireProtocol,
)
from codex_ai_gateway.services.model_aggregation import aggregate_models


class FakeStateStore:
    def __init__(self, state: Any) -> None:
        self._state = state

    def read_state(self) -> Any:
        self._state.openrouter_snapshots = []
        return self._state

    def mutate(self, fn: Any) -> None:
        fn(self._state)


class FakeState:
    def __init__(self, offerings: list[Offering], upstreams: list[Upstream]) -> None:
        self.offerings = offerings
        self.upstreams = upstreams
        self.canonical_models: list[CanonicalModel] = []
        self.model_mappings: list[Any] = []
        self.openrouter_snapshots: list[Any] = []
        self.routing_preferences: list[Any] = []


class FakeRuntime:
    def __init__(self, state: Any) -> None:
        self.state_store = FakeStateStore(state)


def _upstream() -> Upstream:
    return Upstream(
        id="upstream-1",
        name="u1",
        status=UpstreamStatus.enabled,
        kind=UpstreamKind.custom,
        base_url="http://localhost:9999",
        auth_credential_ref="k",
        created_at="2026-08-30T00:00:00+00:00",
        updated_at="2026-08-30T00:00:00+00:00",
    )


def _offering(provider_model_id: str, **extra: Any) -> Offering:
    base: dict[str, Any] = {
        "id": f"offering-{provider_model_id}".replace("/", "-"),
        "upstream_id": "upstream-1",
        "provider_model_id": provider_model_id,
        "wire_protocol": WireProtocol.responses,
        "display_name": provider_model_id,
        "status": OfferingStatus.approved,
        "discovered_at": "2026-08-30T00:00:00+00:00",
        "updated_at": "2026-08-30T00:00:00+00:00",
    }
    base.update(extra)
    return Offering(**base)


def test_aggregate_hit_and_fallback(monkeypatch: Any) -> None:
    models = [
        {"id": "deepseek/deepseek-v4-flash-0731", "created": 200, "name": "flash"},
    ]
    # 用本地快照模拟（避免网络）；将 _snapshot 换成返回固定数据。
    import codex_ai_gateway.services.model_aggregation as agg

    async def fake_snapshot(runtime: Any) -> dict[str, Any]:
        return {"models": models, "snapshot_id": "snap-1"}

    monkeypatch.setattr(agg, "_snapshot", fake_snapshot)

    offerings = [
        _offering("deepseek-v4-flash"),
        _offering("vendor/custom-model", native_metadata_json={"context_window": 64000}),
    ]
    state = FakeState(offerings, [_upstream()])
    runtime = FakeRuntime(state)
    result = asyncio.run(aggregate_models(runtime, offerings=offerings, upstreams=state.upstreams))
    assert result["matched"] == 1
    by_slug = {m.slug: m for m in state.canonical_models}
    assert "deepseek-v4-flash" in by_slug
    assert by_slug["deepseek-v4-flash"].openrouter_model_id == "deepseek/deepseek-v4-flash-0731"
    # 回退模型也能建 canonical
    assert "custom-model" in by_slug
    assert by_slug["custom-model"].openrouter_model_id is None
    # offering 关联到 canonical
    flash_offering = next(o for o in state.offerings if o.provider_model_id == "deepseek-v4-flash")
    assert flash_offering.canonical_model_id == by_slug["deepseek-v4-flash"].id
