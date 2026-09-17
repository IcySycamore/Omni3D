"""页面托管与端口拆分（页面服务 / 重建服务）的测试。

覆盖：
- 端口 / 引导配置的单一来源（`hosting.py`），`SERVER_PORT` 与 `API_PORT` 不漂移；
- `mount_page_routes` 真的挂上了 `/`、`/assets/*`、`/app-config.json`，
  且 `/` 返回的就是 `web/index.html`；
- 重建服务默认**仍然**托管页面（旧地址照旧可用），且带 `CORS_ORIGINS` 放行；
- 页面服务（`web/pages.py`）可独立导入，且它的 `/health` 明确不是模型状态。

端点用**直接调用函数**的方式测，不启 TestClient —— 后者会触发 FastAPI startup
事件去加载模型（几十秒且与本次无关）。
"""
from __future__ import annotations

import os
import sys

# torch 必须最先导入（本机 fbgemm.dll 加载顺序冲突）
import torch  # noqa: F401,I001

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB = os.path.join(_ROOT, "web")
if _WEB not in sys.path:
    sys.path.insert(0, _WEB)

import hosting  # noqa: E402
import pages  # noqa: E402
import server  # noqa: E402


def _endpoint(app, path):
    """取出某个路径的处理函数（没有 TestClient 也能验行为）。"""
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"应用里没有路由 {path}")


class TestPortsSingleSource:
    """端口只有一处定义，别处都引用它。"""

    def test_default_ports(self):
        assert hosting.API_PORT == int(os.environ.get("PORT", "50865"))
        assert hosting.PAGE_PORT == int(os.environ.get("PAGES_PORT", "50866"))

    def test_server_shares_hosting_constants(self):
        assert server.SERVER_PORT == hosting.API_PORT
        assert server.SERVER_HOST == hosting.API_HOST

    def test_page_port_differs_from_api_port(self):
        # 拆成两个端口才有意义：默认值不能撞车
        assert hosting.PAGE_PORT != hosting.API_PORT

    def test_app_config_payload(self):
        cfg = hosting.app_config()
        assert cfg["api_port"] == hosting.API_PORT
        # 默认不写死来源：由页面按「同主机 + API 端口」自己推
        assert cfg["api_origin"] == hosting.API_ORIGIN


class TestMountPageRoutes:
    def test_routes_registered(self):
        app = FastAPI()
        hosting.mount_page_routes(app)
        paths = {getattr(r, "path", None) for r in app.routes}
        assert "/" in paths
        assert "/app-config.json" in paths
        assert "/assets" in paths

    def test_index_serves_the_real_page(self):
        app = FastAPI()
        hosting.mount_page_routes(app)
        html = _endpoint(app, "/")()
        with open(hosting.INDEX_HTML, encoding="utf-8") as fh:
            assert html == fh.read()

    def test_app_config_route_matches_hosting(self):
        app = FastAPI()
        hosting.mount_page_routes(app)
        assert _endpoint(app, "/app-config.json")() == hosting.app_config()

    def test_missing_assets_dir_is_tolerated(self, monkeypatch, tmp_path):
        monkeypatch.setattr(hosting, "ASSETS_DIR", str(tmp_path / "nope"))
        app = FastAPI()
        hosting.mount_page_routes(app)  # 不该抛
        assert "/" in {getattr(r, "path", None) for r in app.routes}


class TestReconstructionServerSplit:
    def test_still_serves_page_by_default(self):
        """`SERVE_PAGE` 默认开：`http://127.0.0.1:50865/` 依旧能直接打开页面。"""
        assert hosting.SERVE_PAGE is True
        assert "/" in {getattr(r, "path", None) for r in server.app.routes}
        assert "/app-config.json" in {getattr(r, "path", None) for r in server.app.routes}

    def test_cors_opens_for_cross_origin_pages(self):
        """页面与 API 分处两个端口时，请求是跨源的，必须放行。"""
        names = [m.cls for m in server.app.user_middleware]
        assert CORSMiddleware in names
        assert server._CORS_ORIGINS == ["*"]

    def test_api_prefix_still_served(self):
        paths = {getattr(r, "path", None) for r in server.app.routes}
        assert "/health" in paths
        assert "/api/models" in paths


class TestPagesServer:
    def test_importable_and_mounts_page(self):
        paths = {getattr(r, "path", None) for r in pages.app.routes}
        assert "/" in paths
        assert "/app-config.json" in paths

    def test_health_is_not_model_health(self):
        """页面服务的 /health 只说明自己是页面服务，别被当成模型就绪。"""
        payload = _endpoint(pages.app, "/health")()
        assert payload["role"] == "pages"
        assert "ready" not in payload
