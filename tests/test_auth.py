"""认证 / 会话 / 历史归属的单元测试（不加载模型，秒级完成）。

覆盖：
- 用户名 / 密码规则的边界值（客户端只做「提示」，服务端是唯一强制点）
- 握手协议的**固定向量**（与 tests/tools/web_sha256_check.js 共用同一组字面量）
- SessionManager 的 **30 分钟滑动过期** 语义
- 匿名历史并入账号的 **归属隔离**（不会误并别人的记录）
"""
from __future__ import annotations

import os
import sys
import time

import pytest

# web/ 按运行时的方式加入导入路径
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB = os.path.join(_ROOT, "web")
if _WEB not in sys.path:
    sys.path.insert(0, _WEB)

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


# ---------------------------------------------------- 握手协议固定向量
# 与 tests/tools/web_sha256_check.js 中的同名向量 **必须逐字节一致**：
# 服务端（Python）与客户端（JS）各自用同一组字面量计算，任何一端改了拼接顺序
# 或编码（salt/nonce 按 ASCII、密码按 UTF-8）都会在这里暴露。
PROTOCOL_VECTORS = [
    # (salt, password, nonce, verifier, proof)
    (
        "a1b2",
        "abcd1234",
        "n0nce",
        "6accaac343825c6fe00011f5a0e55b510252c8df35a16c47eae1db8830c611fe",
        "7c76e8231e4666a2dc2dae3308b4248553b4a9fb37fdaf2cb8d27f387cdef0d7",
    ),
    (
        "salt-deadbeef",
        "p@ssw0rd-2026",
        "nonce-xyz",
        "8854270f53b29e8a68e5c4d9410438673190a680604cc4e39c6596c79cc7e350",
        "9f634ba9ddb269bdf66b7719d834bb5965bcddaf0112f0e1eba7b56da0419cfc",
    ),
    (
        "00ff",
        "____longer_pw_9",
        "n1",
        "e72d7857f0f6995d96f35751afd5c8220da905cadb2f7ba025c099be55b4bc04",
        "08d394462887142fb0f3e004c3765f286dd4e9862485b3447bf387a40819aa8d",
    ),
]


class TestProtocolVectors:
    """verifier / proof 的拼接顺序与编码被固定向量锁死。"""

    @pytest.mark.parametrize("salt,password,nonce,verifier,proof", PROTOCOL_VECTORS)
    def test_verifier_and_proof(self, salt, password, nonce, verifier, proof):
        assert auth_store.compute_verifier(salt, password) == verifier
        assert auth_store.compute_proof(nonce, verifier) == proof


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
