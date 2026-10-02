"""页面托管与端口的**单一来源**（页面服务 / 重建服务共用）。

为什么把「给页面」和「做重建」拆开：

- 重建服务（``panel/server.py``）启动时要加载 Fast3R 模型（几十秒 + 显存），
  而「把 index.html 交给浏览器」这件事本身**不需要它**；
- 拆开之后页面可以由**任意静态宿主**提供（``panel/pages.py``、nginx、CDN…），
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
import time
import uuid

from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

_PANEL_DIR = os.path.dirname(os.path.abspath(__file__))

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
# 默认 **0**：50865 只做重建 API，页面一律由面板服务（50866，`pages.py`）提供。
# 曾经默认 1（“旧习惯 / 手机 WebView 走 adb reverse”），代价是**两个端口都吐页面**——
# 实测踩过：排查时极易把 50865 上的那一份当成“残留的旧面板”。
# 确实需要单端口部署（如 adb reverse 只通一个端口）时，显式设 SERVE_PAGE=1。
SERVE_PAGE = _flag("SERVE_PAGE", False)

INDEX_HTML = os.path.join(_PANEL_DIR, "index.html")
ASSETS_DIR = os.path.join(_PANEL_DIR, "assets")

# 项目根（panel/ 的上一级）—— 数据目录与实例标识都放这儿
PROJECT_ROOT = os.path.dirname(_PANEL_DIR)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
_INSTANCE_FILE = os.path.join(DATA_DIR, "instance_id")

# 注入到页面里的占位符（见 `index()` 与 panel/index.html 的 CLIENT_ID）
INSTANCE_META = '<meta name="omni3d-instance" content="{value}" />'

_instance_cache: str | None = None


def instance_id() -> str:
    """本部署的**固定实例标识**（首次调用生成并落盘，之后一直不变）。

    为什么不让浏览器各自随机生成：那样换浏览器 / 清一次浏览器数据，历史就再也
    认不回来了（记录还躺在库里，但没有归属键）。历史应该跟着**这台设备上的这份
    部署**走 —— 一台设备一份 panel = 一套历史。

    服务商侧只按 ``anon:<这个 id>`` 归档；登录后改为 ``user:<用户名>``，
    那时与本机无关，任何设备登录同一账号都能看到。
    """
    global _instance_cache
    if _instance_cache:
        return _instance_cache
    try:
        with open(_INSTANCE_FILE, encoding="utf-8") as fh:
            value = fh.read().strip()
        if value:
            _instance_cache = value
            return value
    except OSError:
        pass
    value = uuid.uuid4().hex
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_INSTANCE_FILE, "w", encoding="utf-8") as fh:
            fh.write(value)
    except OSError:
        # 落盘失败（只读部署）也不致命：本次进程内保持一致即可
        pass
    _instance_cache = value
    return value


def app_config() -> dict:
    """``/app-config.json`` 的载荷（客户端引导用）。"""
    return {
        "api_origin": API_ORIGIN,
        "api_port": API_PORT,
        # 面板靠这两个把「去官网」链接拼对（官网 = 账号 / 套餐 / API Key）
        "pages_port": PAGE_PORT,
        "portal_port": PORTAL_PORT,
        # 本部署的固定实例标识：客户端用它做匿名归属（不依赖浏览器 localStorage）
        "instance_id": instance_id(),
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
        """页面本体：每次读盘返回，并**显式禁缓存**。

        为什么必须写 `Cache-Control`：不发这个头时浏览器会对 HTML 做
        **启发式缓存**（按 Last-Modified 猜一个有效期），于是"页面改了但
        用户刷新还是旧的"—— 排查问题时极易被当成"某块内容丢了"。
        assets/ 仍走 StaticFiles 的 ETag，图片照常缓存。
        """
        with open(INDEX_HTML, encoding="utf-8") as fh:
            html = fh.read()
        # 注入本部署的实例标识：页面的匿名归属用它，而不是浏览器自己随机生成。
        # 占位符必须在 —— 缺失时**必须响亮地失败**：静默跳过会让页面悄悄退回
        # 「浏览器本地随机 id」，一切看着正常，但换浏览器历史就散了。
        placeholder = INSTANCE_META.format(value="")
        if placeholder not in html:
            raise RuntimeError(
                f"{INDEX_HTML} 里找不到实例标识占位符 {placeholder!r}，无法注入。"
                " 请检查该文件是否被误改（占位符被删/改名都会走到这里）。"
            )
        html = html.replace(placeholder, INSTANCE_META.format(value=instance_id()))
        return HTMLResponse(
            content=html,
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    @app.get("/app-config.json")
    def _app_config():  # noqa: ANN202
        return app_config()
