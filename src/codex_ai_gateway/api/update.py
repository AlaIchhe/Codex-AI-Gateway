"""自更新管理 API：状态、检查、触发安装与策略。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from codex_ai_gateway.models.schemas import (
    UpdatePolicyPatch,
    UpdateRunRequest,
    UpdateStatusView,
)
from codex_ai_gateway.services.updater import UpdateError, UpdateService

router = APIRouter(prefix="/admin/update")


def _service(request: Request) -> UpdateService:
    return request.app.state.runtime.updater


@router.get("/status", response_model=UpdateStatusView)
def update_status(request: Request) -> UpdateStatusView:
    return UpdateStatusView(**(_service(request).status()))


@router.post("/check", response_model=UpdateStatusView)
async def update_check(request: Request) -> UpdateStatusView:
    return UpdateStatusView(**await _service(request).check(force=True))


@router.post("/run", response_model=UpdateStatusView)
def update_run(request: Request, body: UpdateRunRequest) -> UpdateStatusView:
    return UpdateStatusView(**(_service(request).request_install(force=body.force)))


@router.put("/policy", response_model=UpdateStatusView)
def update_policy(request: Request, patch: UpdatePolicyPatch) -> UpdateStatusView:
    service = _service(request)
    try:
        service.write_policy(
            policy=patch.policy,
            pinned_version=patch.pinned_version,
            dismissed_version=patch.dismissed_version,
        )
    except UpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return UpdateStatusView(**service.status())