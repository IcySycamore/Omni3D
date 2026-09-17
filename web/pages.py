"""Omni3D 页面服务：**只**托管前端（index.html + assets），不加载模型。

存在的理由：页面和重建 API 可以分处两个端口 / 两台机器。

- 本机开发：``python web/pages.py``（默认 ``127.0.0.1:50866``）打开页面，
  API 默认指向同主机的 50865（可用 ``API_ORIGIN`` / ``PAGES_PORT`` 覆盖）。
- 部署：这个进程很轻（无 torch / 无 CUDA），可以放在任何一台常开的机器或
  静态宿主上；页面通过 ``/app-config.json`` 知道默认 API 地址。
- 重建服务 ``web/server.py`` 仍然能顺便托管页面（``SERVE_PAGE=1``，默认），
  所以 ``http://127.0.0.1:50865/`` 照旧可用；这里是**可选**的第二条路。

端口 / 监听地址（详见 hosting.py）：``PAGES_PORT``（50866）、``PAGES_HOST``、
``API_ORIGIN``、``API_PORT``。
"""
from __future__ import annotations

import os
import sys

# 直接以脚本运行时，web/ 就是 sys.path[0]；从别处导入时手动补上，
# 保证 `import hosting` 一定能拿到（不依赖 cwd）。
_WEB_DIR = os.path.dirname(os.path.abspath(__file__))
if _WEB_DIR not in sys.path:
    sys.path.insert(0, _WEB_DIR)

from fastapi import FastAPI  # noqa: E402

from hosting import (  # noqa: E402
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
