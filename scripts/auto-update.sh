#!/usr/bin/env bash
# Codex AI Gateway 自更新入口（由 systemd timer 或管理端 /admin/update/run 触发）。
#
# 策略由 data/update-policy.conf 控制：
#   auto   有新版本就安装（默认只在管理端显式开启）
#   notify 只刷新清单并写状态，不安装
#   pinned 只安装 pinned_version
set -euo pipefail

APP_ROOT=${CODEX_AI_GATEWAY_APP_ROOT:-/opt/codex-ai-gateway}
DATA_DIR=${CODEX_AI_GATEWAY_DATA_DIR:-$APP_ROOT/data}
DEPLOY=$APP_ROOT/bin/deploy-linux.sh
PY=$APP_ROOT/current/backend/.venv/bin/python
LOCK=$APP_ROOT/update.lock

mkdir -p "$APP_ROOT"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "auto-update: 另一个更新任务正在运行，跳过"
  exit 0
fi

[ -x "$PY" ] || { echo "auto-update: 找不到运行中的 Python venv"; exit 1; }
[ -x "$DEPLOY" ] || { echo "auto-update: 找不到 $DEPLOY"; exit 1; }

export CODEX_AI_GATEWAY_APP_ROOT=$APP_ROOT
export CODEX_AI_GATEWAY_DATA_DIR=$DATA_DIR

# 刷新清单缓存；尊重 TTL 与失败退避，不会每次触发都请求 GitHub。
"$PY" -m codex_ai_gateway.cli update refresh >/dev/null 2>"$APP_ROOT/update-error.log" || true

PLAN=$("$PY" -m codex_ai_gateway.cli update plan)
ACTION=$(printf '%s' "$PLAN" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("action", "none"))')
TARGET=$(printf '%s' "$PLAN" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("target_version") or "")')
URL=$(printf '%s' "$PLAN" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("url") or "")')
SHA=$(printf '%s' "$PLAN" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("sha256") or "")')
REASON=$(printf '%s' "$PLAN" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("reason") or "")')

echo "auto-update: action=$ACTION reason=$REASON target=$TARGET"
[ "$ACTION" = "install" ] || exit 0

[ -n "$URL" ] || { echo "auto-update: 清单缺少下载地址"; exit 1; }
[ -n "$SHA" ] || { echo "auto-update: 清单缺少 sha256，拒绝安装"; exit 1; }

exec "$DEPLOY" --tag "$TARGET" --url "$URL" --sha256 "$SHA" --lock-held