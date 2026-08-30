"""model_identity 与路由家族归一的单元测试。"""

from __future__ import annotations

from typing import Any

from codex_ai_gateway.models.entities import (
    CanonicalModel,
    MatchMode,
    Offering,
    OfferingStatus,
    WireProtocol,
)
from codex_ai_gateway.services.model_identity import (
    build_standard_catalog,
    canonical_from_candidate,
    canonical_from_offering,
    family_key_of,
    match_offering,
)


def _catalog_entry(
    model_id: str, *, created: int | None = 100, **extra: Any
) -> dict[str, Any]:
    return {"id": model_id, "created": created, "name": model_id, **extra}


def _offering(provider_model_id: str, **extra: Any) -> Offering:
    base: dict[str, Any] = {
        "id": f"offering-{provider_model_id}",
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


class TestFamilyKeyOf:
    def test_strips_vendor_namespace_and_date(self) -> None:
        assert family_key_of("deepseek/deepseek-v4-flash-0731") == "deepseek-v4-flash"

    def test_strips_latest_scroll_alias(self) -> None:
        assert family_key_of("~deepseek/deepseek-v4-flash-latest") == "deepseek-v4-flash"

    def test_strips_md_date_only_when_valid(self) -> None:
        assert family_key_of("deepseek-v4-pro-0813") == "deepseek-v4-pro"
        # 12-31 是合法月份的日期，按原语义视为日期剥离；月份不合法则保留。
        assert family_key_of("some-model-1231") == "some-model"
        assert family_key_of("some-model-1331") == "some-model-1331"

    def test_strips_dashed_mmdd_date(self) -> None:
        # 保留点号（家族基础名形态），仅剥离日期后缀。
        assert family_key_of("qwen3.6-plus-04-02") == "qwen3.6-plus"

    def test_strips_full_iso_and_long_date(self) -> None:
        assert family_key_of("deepseek-v4-flash-2026-08-01") == "deepseek-v4-flash"
        assert family_key_of("deepseek-v4-flash-20260801") == "deepseek-v4-flash"

    def test_lowercases_and_normalizes_separators(self) -> None:
        # 厂商路径分隔符归一为 `-`；点号保留（家族基础名形态）。
        assert family_key_of("DeepSeek/V4-Flash") == "v4-flash"
        assert family_key_of("qwen3.6-plus") == "qwen3.6-plus"

    def test_respects_declared_namespace_prefix(self) -> None:
        assert (
            family_key_of("vendor/deepseek-v4-flash", namespace_prefixes={"vendor"})
            == "deepseek-v4-flash"
        )

    def test_applies_aliases(self) -> None:
        assert family_key_of("doubao-seed-0901", aliases={"doubao-seed": "seed"}) == "seed"


class TestBuildStandardCatalog:
    def test_filters_variant_suffixes(self) -> None:
        models = [
            _catalog_entry("deepseek/deepseek-v4-flash"),
            _catalog_entry("deepseek/deepseek-v4-flash:free", created=200),
            _catalog_entry("deepseek/deepseek-v4-flash:batch", created=300),
        ]
        catalog = build_standard_catalog(models)
        assert len(catalog) == 1
        assert catalog[0].openrouter_model_id == "deepseek/deepseek-v4-flash"

    def test_merges_cross_vendor_by_created_newest(self) -> None:
        models = [
            _catalog_entry("alpha/deepseek-v4-flash", created=100),
            _catalog_entry("beta/deepseek-v4-flash-0731", created=200),
        ]
        catalog = build_standard_catalog(models)
        assert len(catalog) == 1
        assert catalog[0].openrouter_model_id == "beta/deepseek-v4-flash-0731"

    def test_created_missing_treated_as_oldest(self) -> None:
        models = [
            _catalog_entry("deepseek/deepseek-v4-flash-latest", created=None),
            _catalog_entry("deepseek/deepseek-v4-flash-0731", created=200),
            _catalog_entry("deepseek/deepseek-v4-flash", created=100),
        ]
        catalog = build_standard_catalog(models)
        assert len(catalog) == 1
        assert catalog[0].openrouter_model_id == "deepseek/deepseek-v4-flash-0731"

    def test_same_family_groups_ignoring_vendor(self) -> None:
        models = [
            _catalog_entry("a/qwen3.6-plus-04-02", created=50),
            _catalog_entry("b/qwen3.6-plus", created=150),
        ]
        catalog = build_standard_catalog(models)
        assert len(catalog) == 1
        assert catalog[0].family_key == "qwen3.6-plus"

    def test_empty_models_returns_empty(self) -> None:
        assert build_standard_catalog([]) == []


class TestMatchOffering:
    def test_hit_returns_standard_entry(self) -> None:
        models = [
            _catalog_entry("deepseek/deepseek-v4-flash-0731", created=200),
            _catalog_entry("deepseek/deepseek-v4-flash", created=100),
        ]
        result = match_offering(_offering("deepseek-v4-flash"), models)
        assert result.mapping.match_mode == MatchMode.normalized
        assert result.mapping.openrouter_model_id == "deepseek/deepseek-v4-flash-0731"
        assert result.candidate is not None
        assert result.mapping.family_key == "deepseek-v4-flash"

    def test_date_variant_offering_hits_same_family(self) -> None:
        models = [_catalog_entry("deepseek/deepseek-v4-flash-0731", created=200)]
        result = match_offering(_offering("deepseek-v4-flash-0731"), models)
        assert result.mapping.match_mode == MatchMode.normalized
        assert result.candidate is not None

    def test_miss_falls_back_to_upstream(self) -> None:
        result = match_offering(_offering("vendor-custom-model"), [])
        assert result.mapping.match_mode == MatchMode.upstream_fallback
        assert result.mapping.openrouter_model_id is None
        assert result.candidate is None

    def test_miss_with_catalog_pass_through(self) -> None:
        catalog = build_standard_catalog([])
        result = match_offering(
            _offering("vendor-custom-model"), [], catalog=catalog
        )
        assert result.mapping.match_mode == MatchMode.upstream_fallback

    def test_latest_alias_provider_hits_family(self) -> None:
        models = [
            _catalog_entry("~deepseek/deepseek-v4-flash-latest", created=300),
        ]
        result = match_offering(_offering("deepseek-v4-flash"), models)
        assert result.mapping.openrouter_model_id == "~deepseek/deepseek-v4-flash-latest"
        assert result.candidate is not None


class TestCanonicalBuilders:
    def test_canonical_from_candidate_custom_slug(self) -> None:
        candidate = _catalog_entry("deepseek/deepseek-v4-flash-0731")
        canon = canonical_from_candidate(candidate, now="2026-08-30T00:00:00+00:00", slug="deepseek-v4-flash")
        assert canon.slug == "deepseek-v4-flash"
        assert canon.openrouter_model_id == "deepseek/deepseek-v4-flash-0731"

    def test_canonical_from_offering_no_openrouter_id(self) -> None:
        offering = _offering("vendor/custom-model")
        canon = canonical_from_offering(offering, now="2026-08-30T00:00:00+00:00", slug="custom-model")
        assert canon.openrouter_model_id is None
        assert canon.slug == "custom-model"
        assert canon.capability_baseline == {}


class TestResolveCanonicalModel:
    def _state(self, canonical: CanonicalModel) -> Any:
        class State:
            canonical_models = [canonical]
            upstreams: list[Any] = []

        return State()

    def _canonical(self, slug: str, openrouter_id: str | None) -> CanonicalModel:
        return CanonicalModel(
            id="canon-1",
            openrouter_model_id=openrouter_id,
            display_name=slug,
            slug=slug,
            capability_baseline={},
            status="available",
            first_matched_at="2026-08-30T00:00:00+00:00",
            updated_at="2026-08-30T00:00:00+00:00",
        )

    def test_base_slug_resolves(self) -> None:
        from codex_ai_gateway.domain.routing import resolve_canonical_model

        state = self._state(self._canonical("deepseek-v4-flash", "deepseek/deepseek-v4-flash-0731"))
        assert resolve_canonical_model(state, "deepseek-v4-flash").slug == "deepseek-v4-flash"

    def test_date_variant_resolves_to_family(self) -> None:
        from codex_ai_gateway.domain.routing import resolve_canonical_model

        state = self._state(self._canonical("deepseek-v4-flash", "deepseek/deepseek-v4-flash-0731"))
        assert resolve_canonical_model(state, "deepseek-v4-flash-0731").slug == "deepseek-v4-flash"

    def test_latest_alias_resolves_to_family(self) -> None:
        from codex_ai_gateway.domain.routing import resolve_canonical_model

        state = self._state(self._canonical("deepseek-v4-flash", "~deepseek/deepseek-v4-flash-latest"))
        assert resolve_canonical_model(state, "deepseek-v4-flash-latest").slug == "deepseek-v4-flash"

    def test_unavailable_model_not_resolvable(self) -> None:
        from codex_ai_gateway.domain.routing import RoutingError, resolve_canonical_model

        canon = self._canonical("deepseek-v4-flash", None)
        canon.status = "unavailable"
        state = self._state(canon)
        try:
            resolve_canonical_model(state, "deepseek-v4-flash")
        except RoutingError:
            pass
        else:
            raise AssertionError("unavailable model should not resolve")

    def test_openrouter_id_exact_match_fallback(self) -> None:
        from codex_ai_gateway.domain.routing import resolve_canonical_model

        state = self._state(self._canonical("deepseek-v4-flash", "deepseek/deepseek-v4-flash-0731"))
        assert resolve_canonical_model(state, "deepseek/deepseek-v4-flash-0731").slug == "deepseek-v4-flash"
