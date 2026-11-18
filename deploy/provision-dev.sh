#!/usr/bin/env bash
# 在 dev 机（180.76.180.249）上初始化 pc2mb 运行环境（幂等，可重复执行）
set -euo pipefail

APP_DIR=/opt/pc2mb
APP_USER=pc2mb

if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
  echo "created user $APP_USER"
fi

mkdir -p "$APP_DIR"

if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  python3 -m venv "$APP_DIR/.venv"
  echo "created venv"
fi

"$APP_DIR/.venv/bin/python" -m pip install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q 'fastapi>=0.115' 'uvicorn[standard]>=0.30'
"$APP_DIR/.venv/bin/python" -c 'import fastapi, uvicorn, websockets; print("deps ok", fastapi.__version__, uvicorn.__version__)'

if ss -tln | grep -q ':18091 '; then
  echo "WARN: port 18091 already in use:"
  ss -tlnp | grep ':18091 ' || true
else
  echo "port 18091 free"
fi
