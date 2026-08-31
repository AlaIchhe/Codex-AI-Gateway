"""用量与预算管理端点。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from codex_ai_gateway.runtime import Runtime
from codex_ai_gateway.services.usage_reporting import UsageReportingService

router = APIRouter(prefix="/admin/usage")


def _runtime(request: Request) -> Runtime:
    return request.app.state.runtime


@router.get("/summary")
def summary(
    request: Request,
    from_: str | None = None,
    to: str | None = None,
    group_by: str = "period",
) -> dict[str, Any]:
    runtime = _runtime(request)
    events = runtime.usage_log.read_events()
    filtered = _filter_events(events, from_, to)
    service = UsageReportingService(runtime.data_dir)
    rows = service.sum_usage(filtered, group_by=group_by)
    return {"rows": rows, "estimate_note": "部分汇总仅包含估算值。"}


@router.get("/attempts")
def attempts(
    request: Request,
    from_: str | None = None,
    to: str | None = None,
    limit: int = 50,
    before: str | None = None,
) -> dict[str, Any]:
    """分页返回 attempts 审计（按时间倒序，游标为早于 before 的记录）。"""
    runtime = _runtime(request)
    events = runtime.usage_log.read_events()
    filtered = _filter_events(events, from_, to)
    filtered.sort(key=lambda ev: ev.get("started_at") or "", reverse=True)
    if before:
        filtered = [ev for ev in filtered if (ev.get("started_at") or "") < before]
    limit = max(1, min(limit, 200))
    page = filtered[:limit]
    next_cursor = page[-1].get("started_at") if len(filtered) > limit else None
    service = UsageReportingService(runtime.data_dir)
    return {"items": service.list_attempts(page), "next_cursor": next_cursor}


def _filter_events(events: list[dict[str, Any]], from_: str | None, to: str | None) -> list[dict[str, Any]]:
    result = []
    for ev in events:
        started = ev.get("started_at") or ""
        day = started[:10]
        if from_ and day < from_[:10]:
            continue
        if to and day > to[:10]:
            continue
        result.append(ev)
    return result
