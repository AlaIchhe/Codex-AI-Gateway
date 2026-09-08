"""Command line entry point for Codex AI Gateway."""

from __future__ import annotations

import argparse
import asyncio
import json

from codex_ai_gateway.services.updater import UpdateError, UpdateService


def _run_update(args: argparse.Namespace) -> dict:
    service = UpdateService.from_env()
    if args.update_command == "refresh":
        return asyncio.run(service.check(force=args.force))
    if args.update_command == "plan":
        return service.plan()
    return service.record_install(
        status=args.status, version=args.version, error=args.error
    )


def main(argv: list[str] | None = None) -> None:
    """网关命令行入口。"""
    parser = argparse.ArgumentParser(prog="codex-ai-gateway")
    subparsers = parser.add_subparsers(dest="command")
    update = subparsers.add_parser("update", help="自更新相关操作")
    update_sub = update.add_subparsers(dest="update_command")
    refresh = update_sub.add_parser("refresh", help="刷新 Release 清单缓存并写状态")
    refresh.add_argument("--force", action="store_true", help="忽略缓存与退避")
    update_sub.add_parser("plan", help="输出本地安装决策 JSON（不发网络请求）")
    record = update_sub.add_parser("record", help="记录安装结果")
    record.add_argument(
        "--status",
        required=True,
        choices=["idle", "running", "succeeded", "failed"],
    )
    record.add_argument("--version")
    record.add_argument("--error")

    args = parser.parse_args(argv)
    if args.command == "update" and args.update_command:
        try:
            result = _run_update(args)
        except UpdateError as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            raise SystemExit(2) from exc
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(0)
    parser.print_help()
    raise SystemExit(0)


if __name__ == "__main__":
    main()
