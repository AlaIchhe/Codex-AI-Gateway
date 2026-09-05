"""FastAPI 应用工厂。

注册 admin/gateway 路由、托管 dist 静态资源、初始化数据目录并执行启动检查。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from codex_ai_gateway.api import admin, catalog, codex_integration, gateway, usage
from codex_ai_gateway.api.errors import problem_json
from codex_ai_gateway.integrations.secret_store import SecretStore
from codex_ai_gateway.persistence.file_store import init_data_dir
from codex_ai_gateway.runtime import Runtime
from codex_ai_gateway.services.local_codex import LocalCodexAutomationService


def create_app(
    *,
    data_dir: Path | None = None,
    secret_store: SecretStore | None = None,
    frontend_dist: Path | None = None,
) -> FastAPI:
    """构建并配置 FastAPI 应用。"""
    data_path = Path(data_dir) if data_dir else _default_data_dir()
    init_data_dir(data_path)
    selected_store = secret_store or _select_secret_store()
    runtime = Runtime.create(data_path, secret_store=selected_store)
    runtime.run_recovery()
    runtime.ensure_gateway_token()

    app = FastAPI(title="Codex AI Gateway", version="0.1.0")
    app.state.runtime = runtime
    app.state.data_dir = data_path

    app.include_router(admin.router)
    app.include_router(catalog.router)
    app.include_router(codex_integration.router)
    app.include_router(codex_integration.rev_router)
    app.include_router(usage.router)
    app.include_router(gateway.router)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_request, exc):  # type: ignore[no-untyped-def]
        return problem_json(
            error_type="invalid_request",
            code="validation_error",
            title="请求参数校验失败",
            detail=str(exc.errors()[:1]),
            status_code=422,
        )

    @app.get("/", include_in_schema=False)
    async def home() -> HTMLResponse:
        return _index_response(frontend_dist)

    @app.get("/upstreams", include_in_schema=False)
    async def upstreams_page() -> HTMLResponse:
        return _index_response(frontend_dist)

    @app.get("/models", include_in_schema=False)
    async def models_page() -> HTMLResponse:
        return _index_response(frontend_dist)

    @app.get("/codex-plugins", include_in_schema=False)
    async def codex_plugins_page() -> HTMLResponse:
        return _index_response(frontend_dist)

    @app.get("/usage", include_in_schema=False)
    async def usage_page() -> HTMLResponse:
        return _index_response(frontend_dist)

    @app.on_event("startup")
    async def run_local_codex_automation() -> None:
        if os.environ.get("CODEX_AI_GATEWAY_DISABLE_STARTUP_AUTOMATION") == "1":
            return
        try:
            LocalCodexAutomationService(data_path).run_auto_maintenance(
                runtime, trigger="startup"
            )
        except Exception:
            logging.getLogger(__name__).exception("本地 Codex 自动维护失败")

    @app.on_event("startup")
    async def start_model_refresh_loop() -> None:
        """后台定时刷新预设模型列表与协议探测（每 5 小时）。"""
        import asyncio

        async def _model_refresh_loop():
            while True:
                try:
                    state = runtime.state_store.read_state()
                    preset_upstreams = [
                        u for u in state.upstreams
                        if u.kind.value == "preset" and u.status.value == "enabled"
                    ]
                    for upstream in preset_upstreams:
                        try:
                            from codex_ai_gateway.api.admin import _run_upstream_pipeline
                            await _run_upstream_pipeline(runtime, upstream)
                        except Exception:
                            logging.getLogger(__name__).exception(
                                "自动模型探测失败: upstream=%s", upstream.id,
                            )
                    from codex_ai_gateway.api.admin import _maybe_aggregate
                    await _maybe_aggregate(runtime)
                except Exception:
                    logging.getLogger(__name__).exception("模型刷新循环异常")
                await asyncio.sleep(5 * 3600)

            asyncio.create_task(_model_refresh_loop())

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    dist = _resolve_frontend_dist(frontend_dist)
    if dist.exists() and (dist / "index.html").exists():
        assets_dir = dist / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    return app


def _resolve_frontend_dist(frontend_dist: Path | None) -> Path:
    """解析前端 dist 目录：显式参数 > 环境变量 > CWD（Release 产物包）> 仓库路径。"""
    if frontend_dist:
        return frontend_dist
    env_dist = os.environ.get("CODEX_AI_GATEWAY_FRONTEND_DIST")
    if env_dist:
        return Path(env_dist)
    cwd_dist = Path.cwd() / "dist"
    if (cwd_dist / "index.html").exists():
        return cwd_dist
    return Path(__file__).resolve().parents[2] / "dist"


def _index_response(frontend_dist: Path | None) -> HTMLResponse:
    """返回 SPA 入口，使管理端页面可直接通过 URL 访问。"""
    dist = _resolve_frontend_dist(frontend_dist)
    index = dist / "index.html"
    if index.exists():
        return HTMLResponse(content=index.read_text(encoding="utf-8"))
    return HTMLResponse(
        content="<!doctype html><html><body><h1>Codex AI Gateway</h1>"
        "<p>前端尚未构建，请先运行 frontend 构建。</p></body></html>"
    )


def _default_data_dir() -> Path:
    from codex_ai_gateway.persistence.file_store import default_data_dir

    return default_data_dir()


def _select_secret_store() -> SecretStore:
    """默认使用系统 Secret 后端；`memory` 仅限显式声明的无头测试环境（FR-045）。"""
    import os

    if os.environ.get("CODEX_AI_GATEWAY_SECRET_BACKEND") == "memory":
        if os.environ.get("CODEX_AI_GATEWAY_ALLOW_MEMORY_SECRET") != "1":
            raise RuntimeError(
                "拒绝启动（FR-045）：memory Secret 后端不具备跨重启持久性，生产环境禁止使用。"
                "无头测试环境必须显式设置 CODEX_AI_GATEWAY_ALLOW_MEMORY_SECRET=1。"
            )
        from codex_ai_gateway.integrations.secret_store import InMemorySecretStore

        return InMemorySecretStore()
    from codex_ai_gateway.integrations.secret_store import assert_no_plaintext_fallback

    store = SecretStore()
    # FR-045：启动即验证后端可用且未退化为明文文件；失败在 Runtime.create 前中止。
    store.verify_usable()
    assert_no_plaintext_fallback(store)
    return store


if os.environ.get("CODEX_AI_GATEWAY_NO_AUTO_APP") != "1":
    app = create_app()
else:
    app = None
