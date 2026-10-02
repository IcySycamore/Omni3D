"""页面托管与端口拆分（页面服务 / 重建服务）的测试。

覆盖：
- 端口 / 引导配置的单一来源（`hosting.py`），`SERVER_PORT` 与 `API_PORT` 不漂移；
- `mount_page_routes` 真的挂上了 `/`、`/assets/*`、`/app-config.json`，
  且 `/` 返回的就是 `panel/index.html`；
- 重建服务默认**仍然**托管页面（旧地址照旧可用），且带 `CORS_ORIGINS` 放行；
- 页面服务（`panel/pages.py`）可独立导入，且它的 `/health` 明确不是模型状态。

端点用**直接调用函数**的方式测，不启 TestClient —— 后者会触发 FastAPI startup
事件去加载模型（几十秒且与本次无关）。
"""
from __future__ import annotations

import os
import sys

import pytest

# torch 必须最先导入（本机 fbgemm.dll 加载顺序冲突）
import torch  # noqa: F401,I001

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from panel import hosting  # noqa: E402
from panel import pages  # noqa: E402
from panel import server  # noqa: E402


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

    def test_instance_id_is_stable_across_calls(self):
        """实例 id 首次生成后必须稳定（同一份部署 = 同一套历史）。"""
        first = hosting.instance_id()
        assert first
        assert hosting.instance_id() == first


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
        body = _endpoint(app, "/")().body.decode("utf-8")
        with open(hosting.INDEX_HTML, encoding="utf-8") as fh:
            raw = fh.read()
        # 页面本体原样返回，只额外把实例标识填进占位符
        expected = raw.replace(
            hosting.INSTANCE_META.format(value=""),
            hosting.INSTANCE_META.format(value=hosting.instance_id()),
        )
        assert body == expected

    def test_index_html_carries_the_instance_placeholder(self):
        """`index.html` 必须有实例标识占位符。

        没有它时注入静默失效，页面会退回「浏览器本地随机 id」——历史归属
        散掉却不报错，属最难发现的一类故障。
        """
        with open(hosting.INDEX_HTML, encoding="utf-8") as fh:
            html = fh.read()
        assert hosting.INSTANCE_META.format(value="") in html

    def test_injection_fails_loudly_when_placeholder_is_gone(
        self, monkeypatch, tmp_path
    ):
        """守卫必须被证明会失败：抽掉占位符就得报错，而不是悄悄不注入。"""
        broken = tmp_path / "index.html"
        broken.write_text("<html><body>nothing here</body></html>", encoding="utf-8")
        monkeypatch.setattr(hosting, "INDEX_HTML", str(broken))
        app = FastAPI()
        hosting.mount_page_routes(app)
        with pytest.raises(RuntimeError, match="占位符"):
            _endpoint(app, "/")()

    def test_index_is_not_heuristically_cached(self):
        """页面必须发 no-cache。

        不发时浏览器会对 HTML 做**启发式缓存**，``index.html`` 改了刷新还是旧的
        （实测被误当成「帮助页内容丢了」）。
        """
        app = FastAPI()
        hosting.mount_page_routes(app)
        headers = _endpoint(app, "/")().headers
        assert "no-cache" in headers["cache-control"].lower()

    def test_app_config_route_matches_hosting(self):
        app = FastAPI()
        hosting.mount_page_routes(app)
        assert _endpoint(app, "/app-config.json")() == hosting.app_config()

    def test_app_config_exposes_the_deployment_instance_id(self):
        """匿名归属要能拿到本部署的实例 id（页面据此设置 client_id）。"""
        cfg = hosting.app_config()
        assert cfg["instance_id"] == hosting.instance_id()
        assert cfg["instance_id"]

    def test_missing_assets_dir_is_tolerated(self, monkeypatch, tmp_path):
        monkeypatch.setattr(hosting, "ASSETS_DIR", str(tmp_path / "nope"))
        app = FastAPI()
        hosting.mount_page_routes(app)  # 不该抛
        assert "/" in {getattr(r, "path", None) for r in app.routes}


class TestReconstructionServerSplit:
    def test_is_api_only_by_default(self):
        """`SERVE_PAGE` 默认**关**：50865 只做重建 API，页面一律走 50866。

        曾经默认开（“旧习惯 / 手机 WebView 走 adb reverse”），代价是**两个
        端口都吐同一份页面**：实测排查时把它当成了“50865 上残留的旧面板”。
        """
        assert hosting.SERVE_PAGE is False
        paths = {getattr(r, "path", None) for r in server.app.routes}
        assert "/" not in paths, "50865 不该再托管页面（页面归 50866）"
        assert "/app-config.json" not in paths

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
