"""Omni3D 页面服务：**只**托管前端（index.html + assets），不加载模型。

存在的理由：页面和重建 API 可以分处两个端口 / 两台机器。

- 本机开发：``python panel/pages.py``（默认 ``127.0.0.1:50866``）打开页面，
  API 默认指向同主机的 50865（可用 ``API_ORIGIN`` / ``PAGES_PORT`` 覆盖）。
- 部署：这个进程很轻（无 torch / 无 CUDA），可以放在任何一台常开的机器或
  静态宿主上；页面通过 ``/app-config.json`` 知道默认 API 地址。
- 重建服务（``panel/server.py``）**默认不再**托管页面（``SERVE_PAGE=0``）：
  50865 只做 API、50866 只给页面，一个端口一个职责。需要单端口部署
  （如 adb reverse 只通一个口）时显式设 ``SERVE_PAGE=1``。

端口 / 监听地址（详见 hosting.py）：``PAGES_PORT``（50866）、``PAGES_HOST``、
``API_ORIGIN``、``API_PORT``。
"""
from __future__ import annotations

import os
import sys

# 项目根**无条件**钉到 sys.path 最前 —— 理由见 panel/server.py 里的长注释。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT in sys.path:
    sys.path.remove(_PROJECT_ROOT)
sys.path.insert(0, _PROJECT_ROOT)

from fastapi import FastAPI  # noqa: E402

from panel.hosting import (  # noqa: E402
    API_ORIGIN,
    API_PORT,
    PAGE_HOST,
    PAGE_PORT,
    app_config,
    mount_page_routes,
)

app = FastAPI(title="Omni3D 页面服务", version="0.1.0")
mount_page_routes(app)


@app.get("/health")
def health():
    """页面服务的自身健康检查（**不是**模型状态）。

    客户端的「模型服务商」状态灯读的是 API 的 ``/health``（会被改写到 API
    基地址），不会落到这里；这个端点只是给人/运维看的。
    """
    return {"role": "pages", "api_origin": API_ORIGIN, "api_port": API_PORT}


if __name__ == "__main__":
    import uvicorn

    print(f"[pages] 页面服务 http://{PAGE_HOST}:{PAGE_PORT}/")
    print(
        "[pages] 默认 API "
        + (API_ORIGIN or f"http://<页面同主机>:{API_PORT}")
        + f"（引导配置 {app_config()}）"
    )
    uvicorn.run(app, host=PAGE_HOST, port=PAGE_PORT)
