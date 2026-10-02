"""服务商 API Key（多服务商场景的免登录凭据）测试。

覆盖：
- 环境变量解析（``OMNI3D_API_KEYS="key=用户名,..."``）；
- ``X-Api-Key`` 头 → ``user:<用户名>`` 的归属解析（与密码登录同一个用户名共享数据）；
- ``/api/auth/me`` 能用 Key 复验身份；
- ``/health`` 暴露「这台服务商支不支持 Key」以及**它自己的匿名策略**；
- ``OMNI3D_ALLOW_ANONYMOUS=0`` 时，中间件在数据端点要求凭据（白名单见 server.py）。

为什么不启 TestClient：``server`` 的 startup 事件会去加载模型（几十秒）。
端点直接调用端点函数；**中间件则直接喂一个最小 Request**
（`_identify_by_api_key(request, call_next)` 本来就是普通协程）。
"""
from __future__ import annotations

import json
import os
import sys

import pytest  # noqa: E402

# torch 必须最先导入（本机 fbgemm.dll 加载顺序冲突）
import torch  # noqa: F401,I001

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from panel import api_keys  # noqa: E402
from panel import server  # noqa: E402
from panel.session_store import anon_owner, user_owner  # noqa: E402


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

    def test_health_advertises_the_anonymous_policy(self, monkeypatch):
        """「支不支持不带凭据」是服务商的策略，必须由服务商声明。

        客户端不看来源地址、也不猜 —— 走内网穿透时请求同样来自 127.0.0.1，
        按来源判定没有意义。
        """
        monkeypatch.setattr(server, "ALLOW_ANONYMOUS", True)
        assert server.health()["anonymous"] is True
        monkeypatch.setattr(server, "ALLOW_ANONYMOUS", False)
        assert server.health()["anonymous"] is False


def _request(path: str, headers: dict | None = None):
    """造一个最小 Request —— 直接喂给中间件，不必起 TestClient。

    （TestClient 会触发 startup 去加载模型，几十秒起不来；这里只需要
    中间件看得到的 scope 字段。）
    """
    from starlette.requests import Request

    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": raw,
            "scheme": "http",
            "server": ("127.0.0.1", 50865),
            "client": ("127.0.0.1", 45678),
        }
    )


class _CallNext:
    """记录 call_next 有没有被调用 —— 「拦下了」必须同时满足「没继续往下走」。"""

    def __init__(self):
        self.called = False

    async def __call__(self, request):  # noqa: ANN001
        self.called = True

        class _R:
            status_code = 200

        return _R()


def _run_middleware(path: str, headers: dict | None = None):
    import asyncio

    nxt = _CallNext()
    resp = asyncio.run(server._identify_by_api_key(_request(path, headers), nxt))
    return nxt.called, resp


class TestAnonymousPolicy:
    """`OMNI3D_ALLOW_ANONYMOUS` —— 服务商说了算的门槛。

    默认**允许**（本机自用零配置）；对外提供服务时设 `=0`，
    数据端点随即要求 `X-Api-Key` 或 `X-Auth-Token`。
    """

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        # 别去碰真实的门户库：只走环境变量那条静态 Key 路径
        class _NoStore:
            def verify_key(self, key):  # noqa: ANN001
                return None

        monkeypatch.setattr(api_keys, "_store", _NoStore())
        monkeypatch.delenv(api_keys.ENV_NAME, raising=False)

    def test_anonymous_is_allowed_by_default(self, monkeypatch):
        monkeypatch.setattr(server, "ALLOW_ANONYMOUS", True)
        called, _ = _run_middleware("/api/history")
        assert called, "默认允许匿名时不该拦"

    def test_data_endpoint_is_refused_when_anonymous_is_off(self, monkeypatch):
        monkeypatch.setattr(server, "ALLOW_ANONYMOUS", False)
        called, resp = _run_middleware("/api/history")
        assert not called, "要求凭据时不能继续往下走"
        assert resp.status_code == 401
        assert _body(resp)["requires_key"] is True

    def test_api_key_gets_through_when_anonymous_is_off(self, monkeypatch):
        monkeypatch.setattr(server, "ALLOW_ANONYMOUS", False)
        monkeypatch.setenv(api_keys.ENV_NAME, "sk-test=alice")
        called, _ = _run_middleware("/api/history", {"x-api-key": "sk-test"})
        assert called, "带了有效 Key 就该放行"

    def test_wrong_api_key_is_still_refused(self, monkeypatch):
        monkeypatch.setattr(server, "ALLOW_ANONYMOUS", False)
        monkeypatch.setenv(api_keys.ENV_NAME, "sk-test=alice")
        called, resp = _run_middleware("/api/history", {"x-api-key": "sk-nope"})
        assert not called
        assert resp.status_code == 401

    @pytest.mark.parametrize(
        "path",
        # 白名单：登录流程、健康、模型清单、静态资源 ——
        # 少了 /api/auth/ 就永远拿不到凭据（连登录都进不去）
        ["/health", "/api/auth/salt", "/api/auth/me", "/api/models", "/"],
    )
    def test_whitelist_stays_open_without_credentials(self, monkeypatch, path):
        monkeypatch.setattr(server, "ALLOW_ANONYMOUS", False)
        called, _ = _run_middleware(path)
        assert called, f"{path} 必须在白名单里（否则登录/引导都断了）"

    def test_sync_reconstruct_is_gated(self, monkeypatch):
        """`/reconstruct` **不在** `/api/` 前缀下 —— 只按前缀写白名单会漏掉它。

        同步重建一样吃 GPU、一样产出用户数据，必须和 /api/tasks 同等对待。
        """
        monkeypatch.setattr(server, "ALLOW_ANONYMOUS", False)
        called, resp = _run_middleware("/reconstruct")
        assert not called, "/reconstruct 漏出白名单了（只按 /api/ 前缀判断就会漏）"
        assert resp.status_code == 401

    def test_the_allowlist_is_an_allowlist(self, monkeypatch):
        """新增端点默认就该要凭据 —— 显式列出来的才开放。

        反过来说：下面这些真实存在的数据端点，一个都不能在白名单里。
        """
        for path in (
            "/api/history",
            "/api/history/abc",
            "/api/history/abc/ply",
            "/api/tasks",
            "/api/tasks/abc",
            "/api/annotations",
            "/api/snap",
            "/api/measure",
            "/reconstruct",
        ):
            assert not server.anon_path_allowed(path), f"{path} 不该能匿名访问"
