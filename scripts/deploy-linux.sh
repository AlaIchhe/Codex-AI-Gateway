#!/usr/bin/env bash
# Codex AI Gateway Linux 全流程部署脚本
#
# 用法（root 运行）:
#   sudo ./deploy-linux.sh [版本] [绑定地址]
#
# 版本可省略（自动拉取最新 Release），也可指定如 v0.1.0 或 0.1.0。
# 绑定地址默认 127.0.0.1（配合同机反向代理）。Caddy/Podman 容器回源等
# 场景传 0.0.0.0 或具体内网 IP。管理面无登录，切勿将端口暴露到公网。
#
# 脚本行为:
#   1. 从 GitHub Releases 下载构建产物（无需手动下载）
#   2. 安装到 /opt/codex-ai-gateway/releases/<时间戳>/，切换 current 软链
#   3. 创建 venv 并安装 wheels/（优先用 uv，回退 python3.12 venv+pip）
#   4. 写入 /etc/systemd/system/codex-ai-gateway.service 并启用
#   5. 健康检查失败时自动回滚 current
set -euo pipefail

REPO="AlaIchhe/Codex-AI-Gateway"
APP_ROOT=/opt/codex-ai-gateway
PORT=8787
BIND=${2:-127.0.0.1}

[ "$(id -u)" -eq 0 ] || { echo "错误: 请用 root 运行（sudo）"; exit 1; }

command -v curl >/dev/null || { echo "错误: 缺少 curl"; exit 1; }
command -v unzip >/dev/null || { echo "错误: 缺少 unzip"; exit 1; }

# 解析版本 → Release tag
case "${1:-latest}" in
  ""|latest) API_REF="latest" ;;
  v[0-9]*.[0-9]*.[0-9]*|*.*.*) TAG=$1; [[ $TAG == v* ]] || TAG="v$TAG"; API_REF="tags/$TAG" ;;
  *) echo "错误: 无法识别的版本 '$1'（示例: latest / v0.1.0 / 0.1.0）"; exit 1 ;;
esac

echo "==> 查询 GitHub Release（$API_REF）"
API_URL="https://api.github.com/repos/$REPO/releases/$API_REF"
ZIP_URL=$(curl -fsSL "$API_URL" \
  | grep -o '"browser_download_url": *"[^"]*\.zip"' | head -1 | cut -d'"' -f4) \
  || { echo "错误: 查询 Release 失败"; exit 1; }
[ -n "$ZIP_URL" ] || { echo "错误: Release 中未找到 zip 产物"; exit 1; }

TMP_ZIP=$(mktemp /tmp/codex-ai-gateway-XXXXXX.zip)
echo "==> 下载 $ZIP_URL"
curl -fSL --retry 3 -o "$TMP_ZIP" "$ZIP_URL"

STAMP=$(date +%Y%m%d-%H%M%S)
REL_DIR=$APP_ROOT/releases/$STAMP
CURRENT=$APP_ROOT/current
UNIT=/etc/systemd/system/codex-ai-gateway.service

echo "==> 准备 release 目录 $REL_DIR"
mkdir -p "$APP_ROOT/releases" "$APP_ROOT/data"
unzip -q "$TMP_ZIP" -d "$REL_DIR" && rm -f "$TMP_ZIP"
# zip 内含顶层目录时自动下沉一层
entries=("$REL_DIR"/*)
if [ ! -d "$REL_DIR/wheels" ] && [ ${#entries[@]} -eq 1 ] && [ -d "${entries[0]}" ]; then
  REL_DIR=${entries[0]}
fi
[ -d "$REL_DIR/wheels" ] || { echo "错误: release 内缺少 wheels/"; exit 1; }

OLD_TARGET=$(readlink -f "$CURRENT" 2>/dev/null || true)

echo "==> 安装后端依赖"
mkdir -p "$REL_DIR/backend"
trap 'rm -rf "$REL_DIR"' ERR   # 部署中途失败时清理残留
UV_BIN=""
command -v uv >/dev/null 2>&1 && UV_BIN=$(command -v uv)
if [ -z "$UV_BIN" ]; then
  for p in "$HOME/.local/bin/uv" /root/.local/bin/uv /usr/local/bin/uv; do
    [ -x "$p" ] && UV_BIN=$p && break
  done
fi
if [ -n "$UV_BIN" ]; then
  (cd "$REL_DIR/backend" && "$UV_BIN" venv && "$UV_BIN" pip install "$REL_DIR"/wheels/*.whl)
else
  PY=""
  command -v python3.12 >/dev/null 2>&1 && PY=$(command -v python3.12)
  [ -z "$PY" ] && [ -x /usr/bin/python3.12 ] && PY=/usr/bin/python3.12
  [ -n "$PY" ] || { echo "错误: 需要 uv 或 Python 3.12（网关要求 <3.13，当前 python3 不满足）"; exit 1; }
  (cd "$REL_DIR/backend" && "$PY" -m venv .venv \
    && ./.venv/bin/pip install --upgrade pip \
    && ./.venv/bin/pip install "$REL_DIR"/wheels/*.whl)
fi
[ -x "$REL_DIR/backend/.venv/bin/python" ] || { echo "错误: venv 创建失败"; exit 1; }

echo "==> 写入 systemd 服务"
cat > "$UNIT" <<EOF
[Unit]
Description=Codex AI Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REL_DIR/backend
Environment=CODEX_AI_GATEWAY_DATA_DIR=$APP_ROOT/data
Environment=CODEX_AI_GATEWAY_FRONTEND_DIST=$REL_DIR/dist
ExecStart=$REL_DIR/backend/.venv/bin/python -m uvicorn codex_ai_gateway.app:app --host $BIND --port $PORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

ln -sfn "$REL_DIR" "$CURRENT"
systemctl daemon-reload
systemctl enable --now codex-ai-gateway >/dev/null 2>&1 || systemctl restart codex-ai-gateway

echo "==> 健康检查"
sleep 3
trap - ERR
if curl -sf "http://127.0.0.1:$PORT/healthz" | grep -q '"status":"ok"'; then
  echo "部署成功: $(readlink -f "$CURRENT")"
  echo "管理界面: http://127.0.0.1:$PORT"
else
  echo "健康检查失败，回滚到 $OLD_TARGET"
  if [ -n "$OLD_TARGET" ] && [ -d "$OLD_TARGET" ]; then
    ln -sfn "$OLD_TARGET" "$CURRENT"
    systemctl restart codex-ai-gateway
  else
    systemctl stop codex-ai-gateway || true
  fi
  journalctl -u codex-ai-gateway -n 30 --no-pager || true
  exit 1
fi
