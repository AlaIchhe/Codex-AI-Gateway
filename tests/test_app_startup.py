"""启动钩子回归测试：模型刷新后台任务必须真正被创建。"""

from __future__ import annotations

import asyncio
import contextlib

from codex_ai_gateway.app import create_app
from codex_ai_gateway.integrations.secret_store import InMemorySecretStore


def test_model_refresh_loop_task_is_started(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_AI_GATEWAY_DISABLE_STARTUP_AUTOMATION", "1")
    app = create_app(
        data_dir=tmp_path,
        secret_store=InMemorySecretStore(),
        frontend_dist=tmp_path / "missing-dist",
    )

    async def run() -> None:
        for handler in app.router.on_startup:
            await handler()
        task = getattr(app.state, "model_refresh_task", None)
        assert task is not None, "模型刷新循环没有被启动（create_task 不可达）"
        assert not task.done()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())