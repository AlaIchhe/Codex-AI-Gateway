"""Codex CLI 版本探测与更新 API。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from codex_ai_gateway.models.schemas import CodexCliStatusView, CodexCliUpdateRequest
from codex_ai_gateway.services.codex_cli import CodexCliService

router = APIRouter(prefix="/admin/codex-cli")


def _service(request: Request) -> CodexCliService:
    return request.app.state.runtime.codex_cli


@router.get("/status", response_model=CodexCliStatusView)
def codex_cli_status(request: Request) -> CodexCliStatusView:
    return CodexCliStatusView(**(_service(request).status()))


@router.post("/check", response_model=CodexCliStatusView)
async def codex_cli_check(request: Request) -> CodexCliStatusView:
    return CodexCliStatusView(**(await _service(request).check()))


@router.post("/update", response_model=CodexCliStatusView)
def codex_cli_update(request: Request, body: CodexCliUpdateRequest) -> CodexCliStatusView:
    return CodexCliStatusView(**(_service(request).request_update(force=body.force)))
