#!/bin/bash
# Omni3D 容器启动入口。
#
# 根据 MOCK_MODE 环境变量决定启动真实服务还是 mock 服务。

set -e

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-50865}"
MOCK_MODE="${MOCK_MODE:-true}"

cd /app

if [ "$MOCK_MODE" = "true" ]; then
    echo "[Omni3D] 启动 mock server（无真实模型依赖）: http://${HOST}:${PORT}"
    exec python web/mock_server.py
else
    echo "[Omni3D] 启动真实服务: http://${HOST}:${PORT}"
    exec python web/server.py
fi
