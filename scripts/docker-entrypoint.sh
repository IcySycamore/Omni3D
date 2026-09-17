#!/bin/bash
# Omni3D 容器启动入口。
#
# 按 ROLE 选进程：
#   ROLE=api    服务商 API（重建，:50865）；MOCK_MODE=true 时改为 mock 服务
#   ROLE=pages  面板页面（:50866，纯静态，无 torch）
#   ROLE=portal 官网（账号 / 计费 / API Key，:50867）

set -e

ROLE="${ROLE:-api}"
MOCK_MODE="${MOCK_MODE:-false}"

cd /app

case "$ROLE" in
    api)
        if [ "$MOCK_MODE" = "true" ]; then
            echo "[Omni3D] role=api mock 模式（无模型依赖）: http://${HOST:-0.0.0.0}:${PORT:-50865}"
            exec python web/mock_server.py
        fi
        echo "[Omni3D] role=api 重建服务: http://${HOST:-0.0.0.0}:${PORT:-50865}"
        exec python web/server.py
        ;;
    pages)
        echo "[Omni3D] role=pages 面板页面: http://${PAGES_HOST:-0.0.0.0}:${PAGES_PORT:-50866}"
        exec python web/pages.py
        ;;
    portal)
        echo "[Omni3D] role=portal 官网: http://${PORTAL_HOST:-0.0.0.0}:${PORTAL_PORT:-50867}"
        exec python web/portal.py
        ;;
    *)
        echo "[Omni3D] 未知 ROLE='$ROLE'（可选：api | pages | portal）" >&2
        exit 2
        ;;
esac
