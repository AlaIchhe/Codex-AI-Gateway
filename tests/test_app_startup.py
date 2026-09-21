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

def test_logging_is_configured_so_info_logs_are_visible(monkeypatch) -> None:
    """不配置 root 时，codex_ai_gateway.* 的 INFO 日志会被静默丢弃。

    线上表现：目录维护打了上游，journald 里一行都没有，
    用户侧只剩「莫名 429」，无法归因到是网关自己的行为。
    """
    import logging

    from codex_ai_gateway import app as app_module

    root = logging.getLogger()
    level_before, handlers_before = root.level, list(root.handlers)
    try:
        root.handlers = []
        root.setLevel(logging.WARNING)
        monkeypatch.delenv("CODEX_AI_GATEWAY_LOG_LEVEL", raising=False)

        app_module._configure_logging()

        assert root.level == logging.INFO
        assert root.handlers, "没有 handler，INFO 日志依然会被丢掉"
    finally:
        root.handlers = handlers_before
        root.setLevel(level_before)


def test_logging_level_follows_env_override(monkeypatch) -> None:
    import logging

    from codex_ai_gateway import app as app_module

    root = logging.getLogger()
    level_before, handlers_before = root.level, list(root.handlers)
    try:
        root.handlers = []
        root.setLevel(logging.WARNING)

        monkeypatch.setenv("CODEX_AI_GATEWAY_LOG_LEVEL", "debug")
        app_module._configure_logging()
        assert root.level == logging.DEBUG

        monkeypatch.setenv("CODEX_AI_GATEWAY_LOG_LEVEL", "不是级别")
        root.setLevel(logging.WARNING)
        app_module._configure_logging()
        assert root.level == logging.INFO, "非法级别回落到 INFO"
    finally:
        root.handlers = handlers_before
        root.setLevel(level_before)
