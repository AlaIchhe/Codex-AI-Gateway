"""熔断/冷却重构测试：作用域、决策、Retry-After 与路由集成。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from codex_ai_gateway.domain.circuit_breaker import (
    CircuitBreaker,
    CooldownScope,
    FailureDecision,
    classify_failure,
    parse_retry_after,
)
from codex_ai_gateway.domain.routing import earliest_cooldown_seconds, route_candidates
from codex_ai_gateway.models.entities import (
    CanonicalModel,
    Offering,
    OfferingStatus,
    Upstream,
    WireProtocol,
)


def _upstream(uid: str) -> Upstream:
    return Upstream(
        id=uid,
        name=uid,
        base_url=f"https://{uid}.example.com/v1",
        auth_credential_ref=f"upstream:{uid}:api_credential",
        created_at="2026-08-30T00:00:00+00:00",
        updated_at="2026-08-30T00:00:00+00:00",
    )


def _offering(uid: str, model: str, canonical_id: str) -> Offering:
    return Offering(
        id=f"{uid}-{model}",
        upstream_id=uid,
        provider_model_id=model,
        wire_protocol=WireProtocol.responses,
        display_name=model,
        status=OfferingStatus.approved,
        canonical_model_id=canonical_id,
        discovered_at="2026-08-30T00:00:00+00:00",
        updated_at="2026-08-30T00:00:00+00:00",
    )


def _canonical(canonical_id: str, slug: str) -> CanonicalModel:
    return CanonicalModel(
        id=canonical_id,
        display_name=slug,
        slug=slug,
        status="available",
        first_matched_at="2026-08-30T00:00:00+00:00",
        updated_at="2026-08-30T00:00:00+00:00",
    )


def _state(upstreams: list[Upstream], offerings: list[Offering]) -> Any:
    return SimpleNamespace(
        upstreams=upstreams,
        offerings=offerings,
        routing_preferences=[],
    )


class TestClassifyFailure:
    def test_invalid_request_stops_without_cooling(self) -> None:
        result = classify_failure(status_code=400, error_type="invalid_request")
        assert result.decision is FailureDecision.stop
        assert result.scope is CooldownScope.none

    def test_context_length_hops_without_cooling(self) -> None:
        result = classify_failure(
            status_code=400,
            error_type="invalid_request",
            message="This model's maximum context length is 128000 tokens",
        )
        assert result.decision is FailureDecision.hop
        assert result.scope is CooldownScope.none

    def test_authentication_cools_provider(self) -> None:
        result = classify_failure(status_code=401, error_type="authentication")
        assert result.decision is FailureDecision.hop
        assert result.scope is CooldownScope.provider

    def test_quota_cools_provider(self) -> None:
        result = classify_failure(status_code=402, error_type="quota_budget")
        assert result.decision is FailureDecision.hop
        assert result.scope is CooldownScope.provider

    def test_rate_limit_cools_target_shortly(self) -> None:
        result = classify_failure(status_code=429, error_type="rate_limit")
        assert result.decision is FailureDecision.hop
        assert result.scope is CooldownScope.target
        assert result.base_cooldown_seconds == 5.0

    def test_account_quota_rate_limit_cools_provider(self) -> None:
        result = classify_failure(
            status_code=429,
            error_type="rate_limit",
            message="monthly usage limit reached",
        )
        assert result.scope is CooldownScope.provider

    def test_model_permission_cools_target(self) -> None:
        result = classify_failure(status_code=404, error_type="model_permission")
        assert result.decision is FailureDecision.hop
        assert result.scope is CooldownScope.target

    def test_network_error_cools_target(self) -> None:
        result = classify_failure(status_code=None)
        assert result.decision is FailureDecision.hop
        assert result.scope is CooldownScope.target


class TestParseRetryAfter:
    def test_numeric_seconds(self) -> None:
        assert parse_retry_after("120") == 120.0

    def test_caps_at_max(self) -> None:
        assert parse_retry_after("99999") == 600.0

    def test_http_date(self) -> None:
        target = datetime(2026, 10, 21, 7, 28, 0, tzinfo=UTC)
        now = target.timestamp() - 60
        assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT", now=now) == 60.0

    def test_invalid_returns_none(self) -> None:
        assert parse_retry_after("not-a-date") is None
        assert parse_retry_after(None) is None


class TestCircuitBreaker:
    def test_target_scope_does_not_block_other_models(self) -> None:
        breaker = CircuitBreaker(jitter=lambda: 0.0)
        breaker.record_failure(
            "u1", "model-a", status_code=429, error_type="rate_limit", now=1000.0
        )
        assert breaker.is_open("u1", "model-a", now=1000.0)
        assert not breaker.is_open("u1", "model-b", now=1000.0)

    def test_provider_scope_blocks_all_models(self) -> None:
        breaker = CircuitBreaker(jitter=lambda: 0.0)
        breaker.record_failure(
            "u1", "model-a", status_code=401, error_type="authentication", now=1000.0
        )
        assert breaker.is_open("u1", "model-a", now=1000.0)
        assert breaker.is_open("u1", "model-b", now=1000.0)
        assert not breaker.is_open("u2", "model-a", now=1000.0)

    def test_retry_after_overrides_default(self) -> None:
        breaker = CircuitBreaker(jitter=lambda: 0.0)
        breaker.record_failure(
            "u1",
            "model-a",
            status_code=429,
            error_type="rate_limit",
            retry_after="42",
            now=1000.0,
        )
        assert breaker.remaining("u1", "model-a", now=1000.0) == 42.0

    def test_entry_expires(self) -> None:
        breaker = CircuitBreaker(jitter=lambda: 0.0)
        breaker.record_failure(
            "u1", "model-a", status_code=429, error_type="rate_limit", now=1000.0
        )
        assert not breaker.is_open("u1", "model-a", now=1006.0)

    def test_earliest_remaining_and_clear(self) -> None:
        breaker = CircuitBreaker(jitter=lambda: 0.0)
        breaker.record_failure(
            "u1", "model-a", status_code=429, error_type="rate_limit", now=1000.0
        )
        breaker.record_failure(
            "u2", "model-a", status_code=429, error_type="rate_limit", retry_after="30", now=1000.0
        )
        assert breaker.earliest_remaining(
            [("u1", "model-a"), ("u2", "model-a")], now=1000.0
        ) == 5.0
        breaker.clear("u1")
        assert breaker.remaining("u1", "model-a", now=1000.0) is None
        assert breaker.remaining("u2", "model-a", now=1000.0) == 30.0

    def test_no_cooldown_for_request_shape(self) -> None:
        breaker = CircuitBreaker(jitter=lambda: 0.0)
        breaker.record_failure(
            "u1",
            "model-a",
            status_code=400,
            error_type="invalid_request",
            message="maximum context length exceeded",
            now=1000.0,
        )
        assert breaker.remaining("u1", "model-a", now=1000.0) is None


class TestRoutingIntegration:
    def test_cooling_one_target_keeps_siblings_routable(self) -> None:
        upstream = _upstream("u1")
        offerings = [_offering("u1", "model-a", "canon-1"), _offering("u1", "model-b", "canon-2")]
        state = _state([upstream], offerings)
        breaker = CircuitBreaker(clock=lambda: 1000.0, jitter=lambda: 0.0)
        breaker.record_failure("u1", "model-a", status_code=429, error_type="rate_limit")

        assert route_candidates(
            state, _canonical("canon-1", "model-a"), circuit_breaker=breaker
        ) == []
        siblings = route_candidates(
            state, _canonical("canon-2", "model-b"), circuit_breaker=breaker
        )
        assert len(siblings) == 1
        assert siblings[0][0].provider_model_id == "model-b"

    def test_all_cooling_reports_earliest_retry(self) -> None:
        upstream = _upstream("u1")
        state = _state([upstream], [_offering("u1", "model-a", "canon-1")])
        breaker = CircuitBreaker(clock=lambda: 1000.0, jitter=lambda: 0.0)
        breaker.record_failure("u1", "model-a", status_code=429, error_type="rate_limit")
        canonical = _canonical("canon-1", "model-a")
        assert route_candidates(state, canonical, circuit_breaker=breaker) == []
        assert earliest_cooldown_seconds(state, canonical, breaker, now=1000.0) == 5.0
