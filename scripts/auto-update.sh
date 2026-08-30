#!/usr/bin/env bash
# Codex AI Gateway 自动更新脚本
#
# 由 codex-ai-gateway-update.timer 定期触发（或手动运行）。
# 检测 GitHub Releases 最新版本号，与已部署版本比较；
# 有新版本时自动调用 deploy-linux.sh 完成下载、安装和健康检查。
set -euo pipefail

REPO="AlaIchhe/Codex-AI-Gateway"
APP_ROOT=/opt/codex-ai-gateway
VERSION_FILE="$APP_ROOT/deployed-version"

DEPLOYED=""
[ -f "$VERSION_FILE" ] && DEPLOYED=$(head -1 "$VERSION_FILE" | tr -d '[:space:]')

LATEST=$(curl -fsSL --connect-timeout 10 --max-time 30 \
    "https://api.github.com/repos/$REPO/releases/latest" \
    | grep -o '"tag_name": *"[^"]*"' | head -1 | cut -d'"' -f4) || {
    echo "auto-update: GitHub API 不可达，跳过本次检查" >&2
    exit 0
}

if [ -z "$LATEST" ]; then
    echo "auto-update: 未获取到最新版本号，跳过" >&2
    exit 0
fi

if [ "$DEPLOYED" = "$LATEST" ]; then
    exit 0
fi

echo "auto-update: 发现新版本 $LATEST（当前 ${DEPLOYED:-未安装}），开始部署..."
curl -fsSL "https://raw.githubusercontent.com/$REPO/main/scripts/deploy-linux.sh" \
    | bash -s -- "$LATEST"