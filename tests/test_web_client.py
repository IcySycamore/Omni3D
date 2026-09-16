"""网页客户端（web/index.html）的结构性回归测试。

这里**不启动浏览器**，只做两件在 CI 里也能跑的事：
1. 用 node 交叉验证内联的纯 JS SHA-256（登录握手正确性的前提）；
2. 断言认证相关的关键接线仍在（防止后续重构把登录链路改断）。

真正的浏览器端到端验证见 CONTRIBUTING.md「端到端手测」一节。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INDEX = os.path.join(_ROOT, "web", "index.html")
_SHA_SCRIPT = os.path.join(_ROOT, "tests", "tools", "web_sha256_check.js")
_POINTCLOUD_SCRIPT = os.path.join(_ROOT, "tests", "tools", "web_pointcloud_check.js")

_WEB = os.path.join(_ROOT, "web")
if _WEB not in sys.path:
    sys.path.insert(0, _WEB)

import auth_store  # noqa: E402


@pytest.fixture(scope="module")
def index_html() -> str:
    with open(_INDEX, encoding="utf-8") as fh:
        return fh.read()


def _run_node(script: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过网页端 JS 校验")
    proc = subprocess.run(
        [node, script], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60, check=False,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"


class TestInlineJavaScript:
    def test_sha256_matches_standard(self):
        """纯 JS SHA-256 必须与标准实现一致，否则登录永远失败。"""
        _run_node(_SHA_SCRIPT)

    def test_pointcloud_decode(self):
        """点云解码（嵌套/扁平 × 带不带 RGB）不能错位。"""
        _run_node(_POINTCLOUD_SCRIPT)

    def test_imports_three_js_once(self, index_html):
        assert index_html.count('import * as THREE from "three"') == 1


class TestAuthWiring:
    """登录链路的「接线」断言 —— 轻量但能挡住误删。"""

    def test_login_page_and_nav_entry_exist(self, index_html):
        assert 'id="page-login"' in index_html
        assert 'data-page="login"' in index_html
        assert 'id="navAccountText"' in index_html

    def test_fetch_interceptor_is_installed(self, index_html):
        assert "window.fetch = async function" in index_html
        assert "X-Auth-Token" in index_html
        assert "isSameOrigin" in index_html
        # AR 桥是跨源，必须放行（绝不能被带上 token）
        assert "if (!isSameOrigin(rawUrl)) return nativeFetch(input, init);" in index_html

    def test_client_id_is_appended_to_same_origin_api(self, index_html):
        assert "function withClientId" in index_html
        assert "omni3d.client_id" in index_html
        assert "omni3d.token" in index_html

    def test_challenge_response_endpoints_used(self, index_html):
        for path in (
            "/api/auth/salt",
            "/api/auth/register",
            "/api/auth/challenge",
            "/api/auth/login",
            "/api/auth/logout",
            "/api/auth/me",
            "/api/auth/claim",
            "/api/auth/claim/preview",
        ):
            assert path in index_html, f"缺少认证接口调用：{path}"

    def test_history_page_uses_session_layer(self, index_html):
        """历史页必须走会话层（归属隔离 + 持久化），而不是内存任务表。"""
        assert 'fetch("/api/history?limit=20")' in index_html
        assert "/api/history/${taskId}?include_points=true" in index_html

    def test_plaintext_password_never_persisted(self, index_html):
        """只持久化 token，绝不能把密码/verifier 写进本地存储。"""
        assert "omni3d.password" not in index_html
        assert "omni3d.verifier" not in index_html
        assert "writeLS(LS_TOKEN, Auth.token)" in index_html

    def test_auth_rules_match_server(self, index_html):
        """网页端的账号规则常量必须与服务端一致，否则提示与实际不符。"""

        def _const(name: str) -> int:
            m = re.search(rf"\b{name}\s*=\s*(\d+)", index_html)
            assert m, f"未在 index.html 中找到常量 {name}"
            return int(m.group(1))

        assert _const("USERNAME_MIN") == auth_store.USERNAME_MIN
        assert _const("USERNAME_MAX") == auth_store.USERNAME_MAX
        assert _const("PASSWORD_MIN") == auth_store.PASSWORD_MIN
