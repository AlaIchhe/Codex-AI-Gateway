"""价格快照、成本计算、用量汇总、attempts 查询与保留期清理。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from codex_ai_gateway.models.entities import PriceSnapshot
from codex_ai_gateway.persistence.atomic_writer import atomic_write_json
from codex_ai_gateway.util import utc_now, uuid7


class UsageReportingService:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.reports_cache_dir = self.data_dir / "reports/cache"
        self.events_dir = self.data_dir / "usage/events"

    def create_price_snapshot(
        self,
        *,
        offering_id: str,
        pricing: dict[str, Any],
        is_estimated: bool = True,
    ) -> PriceSnapshot:
        snap = PriceSnapshot(
            id=uuid7(),
            offering_id=offering_id,
            pricing_units=pricing,
            fetched_at=utc_now(),
            is_estimated_basis=is_estimated,
        )
        return snap

    def sum_usage(self, events: list[dict[str, Any]], *, group_by: str) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = {}
        for ev in events:
            key = self._group_key(ev, group_by)
            buf = buckets.setdefault(
                key,
                {
                    "bucket_start": key,
                    "attempts": 0,
                    "provider_reported_input_tokens": 0,
                    "estimated_output_tokens": 0,
                    "cost_minor_units": 0,
                    "currency": "USD",
                    "reasoning_tokens": 0,
                    "cache_read_tokens": 0,
                },
            )
            buf["attempts"] += 1
            cat = ev.get("token_usage_by_category") or {}
            basis = ev.get("reporting_basis")
            if basis in ("provider_reported", "mixed"):
                buf["provider_reported_input_tokens"] += int(cat.get("input") or 0)
            if basis in ("estimated", "mixed"):
                buf["estimated_output_tokens"] += int(cat.get("estimated_output") or cat.get("output") or 0)
            buf["cost_minor_units"] += int(ev.get("cost_minor_units") or 0)
            buf["reasoning_tokens"] += int(cat.get("reasoning") or 0)
            buf["cache_read_tokens"] += int(cat.get("cache") or 0)
        return list(buckets.values())

    def list_attempts(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "id": e.get("id"),
                "started_at": e.get("started_at"),
                "duration_ms": e.get("duration_ms"),
                "outcome": e.get("outcome"),
                "reporting_basis": e.get("reporting_basis"),
                "outbound_protocol": e.get("outbound_protocol"),
                "upstream_id": e.get("upstream_id"),
                "upstream_label": e.get("upstream_label"),
                "canonical_model_id": e.get("canonical_model_id"),
                "canonical_model_label": e.get("canonical_model_label"),
                "provider_model_id": e.get("provider_model_id"),
                "attempt_ordinal": e.get("attempt_ordinal", 1),
                "fallback_trigger": e.get("fallback_trigger"),
                "cost_minor_units": e.get("cost_minor_units"),
                "error_mapping_code": e.get("error_mapping_code"),
                "tokens": e.get("token_usage_by_category"),
            }
            for e in events
        ]

    def _group_key(self, ev: dict[str, Any], group_by: str) -> str:
        if group_by == "canonical_model":
            return ev.get("canonical_model_label") or ev.get("canonical_model_id") or "unknown"
        if group_by == "upstream":
            return ev.get("upstream_label") or ev.get("upstream_id") or "unknown"
        if group_by == "offering":
            return ev.get("offering_id") or "unknown"
        if group_by == "protocol":
            return ev.get("outbound_protocol") or "unknown"
        # period: 按天
        started = ev.get("started_at") or ""
        return started[:10]

    def write_daily_cache(self, day: str, events: list[dict[str, Any]]) -> None:
        self.reports_cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.reports_cache_dir / f"{day}.json"
        atomic_write_json(path, {"day": day, "events": events})

    def run_retention(self, retention_days: int) -> dict[str, int]:
        """删除到期每日事件与缓存。返回删除计数。"""
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        count = 0
        for path in self.events_dir.glob("*.ndjson"):
            try:
                day = datetime.strptime(path.stem, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                continue
            if day < cutoff:
                path.unlink(missing_ok=True)
                count += 1
        for path in self.reports_cache_dir.glob("*.json"):
            try:
                day = datetime.strptime(path.stem, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                continue
            if day < cutoff:
                path.unlink(missing_ok=True)
                count += 1
        return {"deleted_days": count}
