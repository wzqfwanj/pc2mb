#!/usr/bin/env bash
# 本机 / 服务器启动脚本（Linux/macOS）
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "缺少 .env，先运行: python server/app.py --gen-secrets > .env" >&2
  exit 1
fi

exec python3 -m uvicorn server.app:app \
  --host "${PC2MB_HOST:-127.0.0.1}" \
  --port "${PC2MB_PORT:-18091}" \
  --log-level info \
  --no-access-log
