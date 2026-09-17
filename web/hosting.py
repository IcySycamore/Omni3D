"""页面托管与端口的**单一来源**（页面服务 / 重建服务共用）。

为什么把「给页面」和「做重建」拆开：

- 重建服务（``web/server.py``）启动时要加载 Fast3R 模型（几十秒 + 显存），
  而「把 index.html 交给浏览器」这件事本身**不需要它**；
- 拆开之后页面可以由**任意静态宿主**提供（``web/pages.py``、nginx、CDN…），
  API 指向任意一台服务器 —— 手机 / 别的机器不必先在本机跑起重建服务。

端口约定（都可用环境变量覆盖）：

- 重建 API（服务商）：``PORT``（默认 **50865**，与 frp 映射 ``127.0.0.1:50865`` 对齐）
- 页面托管（面板 panel）：``PAGES_PORT``（默认 **50866**）
- 官网门户（账号 / 套餐 / API Key）：``PORTAL_PORT``（默认 **50867**）
- 监听地址：``HOST`` / ``PAGES_HOST`` / ``PORTAL_HOST``

客户端引导：页面首次打开会读 ``GET /app-config.json``，拿到「默认去哪个地址、
哪个端口找 API」（``API_ORIGIN`` 可直接写死绝对地址，不设则按「页面同主机 +
API 端口」推）。这只是**第一次使用（注册账号 / 匿名）时的默认值**，
之后用户可以在「设置 → 服务器」里改成任何地址。
"""
from __future__ import annotations

import os

from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

_WEB_DIR = os.path.dirname(os.path.abspath(__file__))

# ---- 端口 ----
# `SERVER_HOST/SERVER_PORT`（server.py）与页面服务共用这两个常量，避免两边写岔。
API_HOST = os.environ.get("HOST", "127.0.0.1")
API_PORT = int(os.environ.get("PORT", "50865"))
PAGE_HOST = os.environ.get("PAGES_HOST", API_HOST)
PAGE_PORT = int(os.environ.get("PAGES_PORT", "50866"))
PORTAL_HOST = os.environ.get("PORTAL_HOST", API_HOST)
PORTAL_PORT = int(os.environ.get("PORTAL_PORT", "50867"))

# 页面要连的 API 绝对地址（如 `http://frp-oil.com:50865`）。
# 留空 = 页面自己按「同主机 + API_PORT」推，本机/局域网部署就不用配。
API_ORIGIN = os.environ.get("API_ORIGIN", "").strip().rstrip("/")


def _flag(name: str, default: bool) -> bool:
    """环境变量开关：空串/未设置 → 默认值。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


# 重建服务是否**顺便**也托管页面。
# 默认 1：`http://127.0.0.1:50865/` 依旧能直接打开页面（旧习惯 / 手机 WebView 用
# adb reverse 时也走这条路）。设成 0 就得到一个纯 API 服务，适合丢到云上。
SERVE_PAGE = _flag("SERVE_PAGE", True)

INDEX_HTML = os.path.join(_WEB_DIR, "index.html")
ASSETS_DIR = os.path.join(_WEB_DIR, "assets")


def app_config() -> dict:
    """``/app-config.json`` 的载荷（客户端引导用）。"""
    return {
        "api_origin": API_ORIGIN,
        "api_port": API_PORT,
        # 面板靠这两个把「去官网」链接拼对（官网 = 账号 / 套餐 / API Key）
        "pages_port": PAGE_PORT,
        "portal_port": PORTAL_PORT,
    }


def mount_page_routes(app) -> None:
    """把 ``GET /``、``/assets/*``、``/app-config.json`` 挂到 FastAPI 应用上。

    页面本体（index.html）用读取文件的方式返回，保持与原实现一致（不加缓存层，
    开发期改完刷新即生效）。
    """
    if os.path.isdir(ASSETS_DIR):
        app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

    @app.get("/", response_class=HTMLResponse)
    def index():  # noqa: ANN202
        with open(INDEX_HTML, encoding="utf-8") as fh:
            return fh.read()

    @app.get("/app-config.json")
    def _app_config():  # noqa: ANN202
        return app_config()
