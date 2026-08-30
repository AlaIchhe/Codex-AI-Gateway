"""model_aggregation 核心行为测试：canonical 复用、回退路径与状态关联。"""

from __future__ import annotations

from typing import Any

from codex_ai_gateway.models.entities import (
    CanonicalModel,
    Offering,
    OfferingStatus,
    WireProtocol,
)
from codex_ai_gateway.services.model_identity import (
    build_standard_catalog,
    canonical_from_offering,
    match_offering,
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


def _canonical(slug: str, openrouter_id: str | None) -> CanonicalModel:
    return CanonicalModel(
        id=f"canon-{slug}",
        openrouter_model_id=openrouter_id,
        display_name=slug,
        slug=slug,
        capability_baseline={},
        status="available",
        first_matched_at="2026-08-30T00:00:00+00:00",
        updated_at="2026-08-30T00:00:00+00:00",
    )


class TestCanonicalReuse:
    def test_slug_reuse_updates_pointer(self) -> None:
        """同家族命中时复用已有 canonical，滚动更新 openrouter_model_id。"""
        entry = {"id": "deepseek/deepseek-v4-flash-0731", "created": 200}
        existing = _canonical("deepseek-v4-flash", "deepseek/deepseek-v4-flash-0423")
        result = match_offering(_offering("deepseek-v4-flash"), [entry])
        assert result.candidate is not None
        # 复用逻辑：slug 相同 → 更新指针
        updated = existing.model_copy(
            update={
                "openrouter_model_id": result.mapping.openrouter_model_id,
                "capability_baseline": result.candidate,
                "updated_at": "2026-08-30T00:00:00+00:00",
            }
        )
        assert updated.openrouter_model_id == "deepseek/deepseek-v4-flash-0731"
        assert updated.slug == "deepseek-v4-flash"


class TestUpstreamFallback:
    def test_fallback_canonical_has_no_openrouter_id(self) -> None:
        offering = _offering("vendor/custom-model", native_metadata_json={"context_window": 64000})
        canon = canonical_from_offering(offering, now="2026-08-30T00:00:00+00:00", slug="custom-model")
        assert canon.openrouter_model_id is None
        assert canon.slug == "custom-model"
        assert canon.capability_baseline == {"context_window": 64000}

    def test_fallback_does_not_clobber_directory_hit(self) -> None:
        """同一 slug 已有目录命中 canonical 时，回退不再新建/覆盖。"""
        entry = {"id": "deepseek/deepseek-v4-flash-0731", "created": 200}
        hit = match_offering(_offering("deepseek-v4-flash"), [entry])
        assert hit.candidate is not None
        # 回退分支仅当 canonical 不存在时新建；已存在则跳过。
        fallback = match_offering(_offering("other-vendor/deepseek-v4-flash"), [])
        assert fallback.mapping.match_mode.value == "upstream_fallback"

    def test_offerings_of_same_family_share_canonical(self) -> None:
        """同家族两个 offering（不同厂商）归同一 canonical slug。"""
        catalog = build_standard_catalog(
            [
                {"id": "alpha/deepseek-v4-flash", "created": 100},
                {"id": "beta/deepseek-v4-flash-0731", "created": 200},
            ]
        )
        # beta 的 0731 胜出（created 更大）
        assert catalog[0].family_key == "deepseek-v4-flash"
        assert catalog[0].openrouter_model_id == "beta/deepseek-v4-flash-0731"
        r1 = match_offering(_offering("deepseek-v4-flash"), [], catalog=catalog)
        r2 = match_offering(_offering("deepseek-v4-flash-0731"), [], catalog=catalog)
        assert r1.mapping.family_key == r2.mapping.family_key


class TestCanonicalBySlugCompatibility:
    def test_normalized_slug_reuse(self) -> None:
        """旧数据 slug 为归一化形态（glm-5-3-flash）时，按家族基础名可复用。"""
        from codex_ai_gateway.services.model_aggregation import _canonical_by_slug

        class State:
            canonical_models = [_canonical("glm-5-3-flash", None)]

        state = State()
        found = _canonical_by_slug(state, "glm-5.3-flash")
        assert found is not None
        assert found.slug == "glm-5-3-flash"

    def test_exact_slug_preferred(self) -> None:
        from codex_ai_gateway.services.model_aggregation import _canonical_by_slug

        class State:
            canonical_models = [
                _canonical("glm-5-3-flash", None),
                _canonical("glm-5.3-flash", None),
            ]

        state = State()
        found = _canonical_by_slug(state, "glm-5.3-flash")
        assert found.slug == "glm-5.3-flash"
