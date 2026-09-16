"""认证 / 会话 / 历史归属的单元测试（不加载模型，秒级完成）。

覆盖：
- 用户名 / 密码规则的边界值（网页与桌面端只做「提示」，服务端是唯一强制点）
- 三端（服务端 auth_store / 桌面端 api_client）握手算法一致性
- SessionManager 的 **30 分钟滑动过期** 语义
- 匿名历史并入账号的 **归属隔离**（不会误并别人的记录）
"""
from __future__ import annotations

import os
import sys
import time

import pytest

# web/ 与 desktop/ 按运行时的方式加入导入路径
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (os.path.join(_ROOT, "web"), os.path.join(_ROOT, "desktop")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import auth_store  # noqa: E402
import session_manager as sm  # noqa: E402
from session_store import SessionStore, anon_owner, user_owner  # noqa: E402


# ---------------------------------------------------------------- 用户名规则
class TestUsernameRules:
    @pytest.mark.parametrize("name", ["", "ab", "a" * 33])
    def test_length_rejected(self, name):
        assert auth_store.validate_username(name) is not None

    @pytest.mark.parametrize(
        "name", ["abc", "a" * 32, "user.name", "user_name", "user-name", "A1b"]
    )
    def test_accepted(self, name):
        assert auth_store.validate_username(name) is None

    @pytest.mark.parametrize("name", ["a b", "中文名", "user@host", "emoji😀", "a/b"])
    def test_charset_rejected(self, name):
        assert auth_store.validate_username(name) is not None


# ---------------------------------------------------------------- 密码规则
class TestPasswordRules:
    def test_empty(self):
        assert auth_store.validate_password("") is not None

    def test_too_short(self):
        assert auth_store.validate_password("a" * (auth_store.PASSWORD_MIN - 1)) is not None

    def test_min_length_ok(self):
        assert auth_store.validate_password("a" * auth_store.PASSWORD_MIN) is None

    def test_whitespace_only(self):
        assert auth_store.validate_password(" " * 12) is not None

    def test_unicode_ok(self):
        assert auth_store.validate_password("中文密码足够长") is not None  # 7 字，不足
        assert auth_store.validate_password("中文密码足够长了") is None  # 8 字


# ---------------------------------------------------- 握手算法三端一致性
class TestProtocolConsistency:
    """服务端与桌面端的 verifier/proof 必须逐字节一致，否则登录必然失败。"""

    def test_verifier_and_proof_match_desktop_client(self):
        import api_client

        salt = auth_store.new_salt()
        nonce = auth_store.new_nonce()
        password = "p@ssw0rd中文"

        v_server = auth_store.compute_verifier(salt, password)
        v_desktop = api_client.compute_verifier(salt, password)
        assert v_server == v_desktop

        assert auth_store.compute_proof(nonce, v_server) == api_client.compute_proof(
            nonce, v_desktop
        )

    def test_desktop_client_shares_username_rules(self):
        import api_client

        assert api_client.USERNAME_MIN == auth_store.USERNAME_MIN
        assert api_client.USERNAME_MAX == auth_store.USERNAME_MAX
        assert api_client.PASSWORD_MIN == auth_store.PASSWORD_MIN
        for name in ["ab", "a b", "user.name", "a" * 32]:
            assert bool(api_client.validate_username(name)) == bool(
                auth_store.validate_username(name)
            )


# ------------------------------------------------------------ 会话滑动过期
class TestSessionManager:
    def test_default_ttl_is_30_minutes(self):
        assert sm.DEFAULT_TTL_SECONDS == 1800.0
        assert sm.SessionManager().ttl_seconds == 1800.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("OMNI3D_SESSION_TTL", "120")
        assert sm.SessionManager().ttl_seconds == 120.0

    @pytest.mark.parametrize("raw", ["", "abc", "0", "-5"])
    def test_env_invalid_falls_back(self, monkeypatch, raw):
        monkeypatch.setenv("OMNI3D_SESSION_TTL", raw)
        assert sm.SessionManager().ttl_seconds == sm.DEFAULT_TTL_SECONDS

    def test_sliding_expiry_extends_on_access(self):
        """持续访问不应过期；只有静默超过 TTL 才失效。"""
        mgr = sm.SessionManager(ttl_seconds=1.5)
        token = mgr.create("alice")
        for _ in range(3):
            time.sleep(0.3)
            assert mgr.username_for(token) == "alice"
        time.sleep(1.8)  # 静默超过 TTL
        assert mgr.username_for(token) is None

    def test_drop_and_count(self):
        mgr = sm.SessionManager(ttl_seconds=60)
        token = mgr.create("bob")
        assert mgr.count() == 1
        assert mgr.username_for("nope") is None
        assert mgr.drop(token) is True
        assert mgr.username_for(token) is None
        assert mgr.count() == 0


# -------------------------------------------------------- 匿名历史并入账号
@pytest.fixture()
def store(tmp_path):
    s = SessionStore(
        db_path=str(tmp_path / "sessions.db"),
        sessions_dir=str(tmp_path / "ply"),
    )
    yield s
    s.close()


class TestAnonClaimIsolation:
    @staticmethod
    def _save(store: SessionStore, session_id: str, owner: str) -> None:
        store.save_session(
            session_id=session_id,
            owner=owner,
            result={
                "num_views": 3,
                "num_points": 1,
                "points": [[0.0, 0.0, 0.0]],
                "ply": "",
            },
        )

    def test_count_then_rename_is_owner_scoped(self, store):
        mine = anon_owner("browser-1")
        other = anon_owner("browser-2")
        alice = user_owner("alice")

        self._save(store, "s1", mine)
        self._save(store, "s2", mine)
        self._save(store, "s3", other)
        self._save(store, "s4", alice)

        assert store.count_sessions(mine) == 2
        assert store.count_sessions(other) == 1

        moved = store.rename_owner(mine, alice)
        assert moved == 2
        assert store.count_sessions(mine) == 0  # 匿名桶已清空
        assert store.count_sessions(other) == 1  # 别人的匿名桶不受影响
        assert store.count_sessions(alice) == 3  # 原有 1 条 + 并入 2 条

    def test_claim_on_empty_anon_bucket(self, store):
        assert store.rename_owner(anon_owner("nobody"), user_owner("alice")) == 0

    def test_count_sessions_does_not_leak_across_users(self, store):
        self._save(store, "a", user_owner("alice"))
        assert store.count_sessions(user_owner("bob")) == 0
