"""服务商 API Key（多服务商场景的免登录凭据）测试。

覆盖：
- 环境变量解析（``OMNI3D_API_KEYS="key=用户名,..."``）；
- ``X-Api-Key`` 头 → ``user:<用户名>`` 的归属解析（与密码登录同一个用户名共享数据）；
- ``/api/auth/me`` 能用 Key 复验身份（客户端「校验」按钮打的就是它）；
- ``/health`` 暴露「这台服务商支不支持 Key」。

为什么不启 TestClient：``server`` 的 startup 事件会去加载模型（几十秒）。
这里直接调用端点函数 + 手动设置中间件用的 ContextVar，语义与真实请求一致。
"""
from __future__ import annotations

import json
import os
import sys

# torch 必须最先导入（本机 fbgemm.dll 加载顺序冲突）
import torch  # noqa: F401,I001

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB = os.path.join(_ROOT, "web")
if _WEB not in sys.path:
    sys.path.insert(0, _WEB)

import api_keys  # noqa: E402
import server  # noqa: E402
from session_store import anon_owner, user_owner  # noqa: E402


def _body(resp) -> dict:
    return json.loads(resp.body.decode("utf-8"))


class TestParsing:
    def test_parse_ignores_junk(self):
        table = api_keys.parse("k1=alice, k2=bob ,bad, =x, k3=")
        assert table == {"k1": "alice", "k2": "bob"}

    def test_empty_config(self):
        assert api_keys.parse(None) == {}
        assert api_keys.parse("") == {}

    def test_static_username_for(self):
        table = {"k1": "alice"}
        assert api_keys.static_username_for("k1", table) == "alice"
        assert api_keys.static_username_for("nope", table) is None
        assert api_keys.static_username_for(None, table) is None
        assert api_keys.static_username_for("k1", {}) is None

    def test_env_config(self, monkeypatch):
        monkeypatch.setenv(api_keys.ENV_NAME, "sk-a=alice")
        assert api_keys.static_username_for("sk-a") == "alice"
        assert server.static_keys_enabled() is True
        monkeypatch.delenv(api_keys.ENV_NAME)
        assert server.static_keys_enabled() is False


class TestIdentity:
    def _as_key_user(self, username):
        """模拟中间件：把 X-Api-Key 解析出的用户名放进 ContextVar。"""
        return server._REQUEST_API_KEY_USER.set(username)

    def test_api_key_maps_to_user_owner(self):
        token = self._as_key_user("alice")
        try:
            assert server._owner_of(None, "some-client") == user_owner("alice")
        finally:
            server._REQUEST_API_KEY_USER.reset(token)

    def test_falls_back_to_anonymous(self):
        token = self._as_key_user(None)
        try:
            assert server._owner_of(None, "c1") == anon_owner("c1")
        finally:
            server._REQUEST_API_KEY_USER.reset(token)

    def test_auth_me_accepts_api_key(self):
        token = self._as_key_user("bob")
        try:
            resp = server.auth_me(x_auth_token=None)
            assert resp.status_code == 200
            payload = _body(resp)
            assert payload["username"] == "bob"
            assert payload["via"] == "api_key"
        finally:
            server._REQUEST_API_KEY_USER.reset(token)

    def test_auth_me_without_credentials_is_401(self):
        token = self._as_key_user(None)
        try:
            resp = server.auth_me(x_auth_token=None)
            assert resp.status_code == 401
        finally:
            server._REQUEST_API_KEY_USER.reset(token)

    def test_middleware_is_registered(self):
        names = [m.cls.__name__ for m in server.app.user_middleware]
        assert "BaseHTTPMiddleware" in names

    def test_health_advertises_api_key_support(self, monkeypatch):
        """官网签发的 Key 总是可以（同一个门户库），静态 Key 单列一项。"""
        assert server.health()["api_key"] is True
        monkeypatch.setenv(api_keys.ENV_NAME, "sk-a=alice")
        assert server.health()["static_api_keys"] is True
        monkeypatch.delenv(api_keys.ENV_NAME)
        assert server.health()["static_api_keys"] is False
