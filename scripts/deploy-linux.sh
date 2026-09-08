#!/usr/bin/env bash
# Codex AI Gateway 事务化部署脚本。
#
# 用法:
#   sudo ./deploy-linux.sh --tag v0.2.28 --url <zip_url> --sha256 <hex> [--lock-held]
#   sudo ./deploy-linux.sh --tag v0.2.28 --zip /tmp/bundle.zip --sha256 <hex>
#
# 行为: 加锁 -> 下载 -> 校验 sha256 -> 解压 -> 建 venv -> 写 unit -> 切 current
#       -> 重启 -> 健康检查(30s) -> 失败回滚 -> 清理旧 release -> 安装脚本自身。
set -euo pipefail

APP_ROOT=${CODEX_AI_GATEWAY_APP_ROOT:-/opt/codex-ai-gateway}
PORT=${CODEX_AI_GATEWAY_PORT:-8787}
DATA_DIR=$APP_ROOT/data
RELEASES=$APP_ROOT/releases
CURRENT=$APP_ROOT/current
BIND_FILE=$APP_ROOT/bind-address
UNIT=/etc/systemd/system/codex-ai-gateway.service
UPDATE_SERVICE=/etc/systemd/system/codex-ai-gateway-update.service
UPDATE_TIMER=/etc/systemd/system/codex-ai-gateway-update.timer
RETAIN_RELEASES=${CODEX_AI_GATEWAY_RETAIN_RELEASES:-5}
HEALTH_TIMEOUT=${CODEX_AI_GATEWAY_HEALTH_TIMEOUT:-30}

TAG=""
URL=""
ZIP=""
SHA256=""
ALLOW_UNVERIFIED=""
LOCK_HELD=""

usage() {
  sed -n '2,10p' "$0"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG=$2; shift 2 ;;
    --url) URL=$2; shift 2 ;;
    --zip) ZIP=$2; shift 2 ;;
    --sha256) SHA256=$2; shift 2 ;;
    --allow-unverified) ALLOW_UNVERIFIED=1; shift ;;
    --lock-held) LOCK_HELD=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 1 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "错误: 请用 root 运行"; exit 1; }
[ -n "$TAG" ] || { echo "错误: 缺少 --tag"; exit 1; }
if [ -z "$URL" ] && [ -z "$ZIP" ]; then
  echo "错误: 需要 --url 或 --zip"; exit 1
fi

mkdir -p "$RELEASES" "$DATA_DIR" "$APP_ROOT/bin"

if [ -z "$LOCK_HELD" ]; then
  exec 9>"$APP_ROOT/update.lock"
  flock -n 9 || { echo "另一个更新任务正在运行"; exit 0; }
fi

record() {
  local status=$1
  local error=${2:-}
  local py="$CURRENT/backend/.venv/bin/python"
  [ -x "$py" ] || return 0
  if [ -n "$error" ]; then
    CODEX_AI_GATEWAY_DATA_DIR="$DATA_DIR" CODEX_AI_GATEWAY_APP_ROOT="$APP_ROOT" \
      "$py" -m codex_ai_gateway.cli update record --status "$status" --version "$TAG" --error "$error" >/dev/null 2>&1 || true
  else
    CODEX_AI_GATEWAY_DATA_DIR="$DATA_DIR" CODEX_AI_GATEWAY_APP_ROOT="$APP_ROOT" \
      "$py" -m codex_ai_gateway.cli update record --status "$status" --version "$TAG" >/dev/null 2>&1 || true
  fi
}

BIND=0.0.0.0
if [ -f "$BIND_FILE" ]; then
  BIND=$(head -1 "$BIND_FILE" | tr -d '[:space:]')
fi
[ -n "$BIND" ] || BIND=0.0.0.0

TMP_ZIP=$(mktemp /tmp/codex-ai-gateway-XXXXXX.zip)
cleanup() { rm -f "$TMP_ZIP"; }
trap cleanup EXIT

if [ -n "$ZIP" ]; then
  cp "$ZIP" "$TMP_ZIP"
else
  echo "==> 下载 $URL"
  curl -fsSL --retry 3 --connect-timeout 15 --max-time 600 -o "$TMP_ZIP" "$URL"
fi

if [ -n "$SHA256" ]; then
  echo "$SHA256  $TMP_ZIP" | sha256sum -c - >/dev/null || {
    echo "错误: sha256 校验失败"; record failed "sha256 mismatch"; exit 1; }
else
  if [ -z "$ALLOW_UNVERIFIED" ]; then
    echo "错误: 未提供 --sha256，拒绝安装未校验的产物（可用 --allow-unverified 覆盖）"
    exit 1
  fi
  echo "警告: 未校验产物完整性"
fi

STAMP=$(date +%Y%m%d-%H%M%S)
REL_DIR=$RELEASES/$STAMP
mkdir -p "$REL_DIR"
unzip -q "$TMP_ZIP" -d "$REL_DIR"
BUNDLE=$REL_DIR/codex-ai-gateway-$TAG
if [ ! -d "$BUNDLE/wheels" ]; then
  found=$(find "$REL_DIR" -maxdepth 2 -type d -name wheels | head -1)
  if [ -z "$found" ]; then
    echo "错误: release 内缺少 wheels/"; record failed "missing wheels"; exit 1
  fi
  BUNDLE=$(dirname "$found")
fi

OLD_TARGET=$(readlink -f "$CURRENT" 2>/dev/null || true)
UNIT_BAK=$APP_ROOT/unit-backup-$STAMP.service
if [ -f "$UNIT" ]; then cp "$UNIT" "$UNIT_BAK"; fi

echo "==> 安装后端依赖"
mkdir -p "$BUNDLE/backend"
UV_BIN=""
if command -v uv >/dev/null 2>&1; then
  UV_BIN=$(command -v uv)
elif [ -x /root/.local/bin/uv ]; then
  UV_BIN=/root/.local/bin/uv
fi
if [ -n "$UV_BIN" ]; then
  ( cd "$BUNDLE/backend" && "$UV_BIN" venv && "$UV_BIN" pip install "$BUNDLE"/wheels/*.whl )
else
  PY=""
  if command -v python3.12 >/dev/null 2>&1; then
    PY=$(command -v python3.12)
  elif [ -x /usr/bin/python3.12 ]; then
    PY=/usr/bin/python3.12
  fi
  [ -n "$PY" ] || { echo "错误: 需要 uv 或 Python 3.12"; exit 1; }
  ( cd "$BUNDLE/backend" && "$PY" -m venv .venv \
    && ./.venv/bin/pip install --upgrade pip \
    && ./.venv/bin/pip install "$BUNDLE"/wheels/*.whl )
fi
[ -x "$BUNDLE/backend/.venv/bin/python" ] || { echo "错误: venv 创建失败"; exit 1; }

KREQ=""
if systemctl list-unit-files 2>/dev/null | grep -q '^codex-keyring-unlock.service'; then
  KREQ=$(printf 'Requires=codex-keyring-unlock.service\nAfter=codex-keyring-unlock.service')
fi

echo "==> 写入 systemd 服务"
cat > "$UNIT" <<EOF
[Unit]
Description=Codex AI Gateway
After=network-online.target
Wants=network-online.target
$KREQ

[Service]
Type=simple
WorkingDirectory=$BUNDLE/backend
Environment=CODEX_AI_GATEWAY_DATA_DIR=$DATA_DIR
Environment=CODEX_AI_GATEWAY_FRONTEND_DIST=$BUNDLE/dist
Environment=CODEX_AI_GATEWAY_APP_ROOT=$APP_ROOT
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/0/bus
Environment=XDG_RUNTIME_DIR=/run/user/0
ExecStart=$BUNDLE/backend/.venv/bin/python -m uvicorn codex_ai_gateway.app:app --host $BIND --port $PORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

ln -sfn "$BUNDLE" "$CURRENT"
systemctl daemon-reload
systemctl restart codex-ai-gateway

echo "==> 健康检查（最长 $HEALTH_TIMEOUT 秒）"
OK=""
for _ in $(seq 1 "$HEALTH_TIMEOUT"); do
  if curl -sf "http://127.0.0.1:$PORT/healthz" 2>/dev/null | grep -q '"status":"ok"'; then
    OK=1; break
  fi
  sleep 1
done

if [ -z "$OK" ]; then
  echo "健康检查失败，回滚到 $OLD_TARGET"
  if [ -n "$OLD_TARGET" ] && [ -d "$OLD_TARGET" ]; then
    ln -sfn "$OLD_TARGET" "$CURRENT"
    if [ -f "$UNIT_BAK" ]; then cp "$UNIT_BAK" "$UNIT"; fi
    systemctl daemon-reload
    systemctl restart codex-ai-gateway
  fi
  journalctl -u codex-ai-gateway -n 40 --no-pager || true
  record failed "health check failed"
  exit 1
fi

echo "$TAG" > "$APP_ROOT/deployed-version"
echo "$TAG-$STAMP" > "$APP_ROOT/deployed-build"

if [ -d "$BUNDLE/scripts" ]; then
  cp "$BUNDLE/scripts/deploy-linux.sh" "$APP_ROOT/bin/deploy-linux.sh"
  cp "$BUNDLE/scripts/auto-update.sh" "$APP_ROOT/bin/auto-update.sh"
  chmod +x "$APP_ROOT/bin/deploy-linux.sh" "$APP_ROOT/bin/auto-update.sh"
fi

cat > "$UPDATE_SERVICE" <<UNIT
[Unit]
Description=Codex AI Gateway Auto Update
After=network-online.target codex-ai-gateway.service
Wants=network-online.target

[Service]
Type=oneshot
Environment=CODEX_AI_GATEWAY_APP_ROOT=$APP_ROOT
Environment=CODEX_AI_GATEWAY_DATA_DIR=$DATA_DIR
ExecStart=$APP_ROOT/bin/auto-update.sh
TimeoutStartSec=900
UNIT

cat > "$UPDATE_TIMER" <<UNIT
[Unit]
Description=Codex AI Gateway Auto Update Timer

[Timer]
OnBootSec=2min
OnUnitActiveSec=1h
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now codex-ai-gateway-update.timer >/dev/null 2>&1 || true

record succeeded
echo "部署成功: $BUNDLE"
echo "管理界面: http://127.0.0.1:$PORT"

# 清理旧 release：保留最近 N 个，跳过 current 指向的目录。
ls -1dt "$RELEASES"/*/ 2>/dev/null | tail -n +$((RETAIN_RELEASES + 1)) | while read -r dir; do
  dir=${dir%/}
  case "$dir" in
    "$RELEASES"/*)
      if [ "$dir" != "$(readlink -f "$CURRENT" 2>/dev/null || true)" ]; then
        rm -rf -- "$dir"
      fi
      ;;
  esac
done