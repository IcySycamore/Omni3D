"""用户认证存储（SQLite）。

密码**从不以明文或可逆形式保存**，也不在网络上传明文（注册时也不传）：

    salt      : 服务器生成的随机串（每用户唯一）
    verifier  : sha256(salt + password)          ← 数据库只存这个

登录采用挑战-应答（challenge-response），服务器发 `nonce`：

    客户端计算 proof    = sha256(nonce + sha256(salt + password_input))
    服务器计算 expected = sha256(nonce + verifier)
    二者相等 ⇒ 认证通过

好处：明文密码不出客户端；重放攻击被 nonce 阻断。

编码约定：salt / nonce 为 hex 字符串，拼接时按其 **ASCII 字节** 拼，
密码按 **UTF-8 字节** 拼，最终摘要为 hex 小写。
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time

# ---- 账号规则（单一来源，客户端只做与之一致的即时提示）----
USERNAME_MIN = 3
USERNAME_MAX = 32
PASSWORD_MIN = 8
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def validate_username(username: str) -> str | None:
    """校验用户名；合法返回 None，否则返回可直接展示给用户的错误文案。

    规则：长度 3–32，仅 ``[A-Za-z0-9_.-]``。
    限制字符集是为了避免空白/emoji/大小写变体造成 SQLite 主键与 UI 显示混乱。
    """
    if not username:
        return "用户名不能为空"
    if len(username) < USERNAME_MIN or len(username) > USERNAME_MAX:
        return f"用户名长度需为 {USERNAME_MIN}–{USERNAME_MAX} 个字符"
    if not _USERNAME_RE.match(username):
        return "用户名只能包含字母、数字、下划线、点或连字符"
    return None


def validate_password(password: str) -> str | None:
    """校验密码强度；合法返回 None。

    注意：本协议下**服务器永远看不到密码**（只收到 verifier），
    所以这个函数只在客户端调用；服务端无法复核密码长度。
    """
    if not password:
        return "密码不能为空"
    if len(password) < PASSWORD_MIN:
        return f"密码至少 {PASSWORD_MIN} 个字符"
    if not password.strip():
        return "密码不能全为空白字符"
    return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username   TEXT PRIMARY KEY,
    salt       TEXT NOT NULL,
    verifier   TEXT NOT NULL,
    created_at REAL
);
"""


def new_salt() -> str:
    """生成 16 字节随机 salt（hex）。"""
    return os.urandom(16).hex()


def new_nonce() -> str:
    """生成 32 字节随机 nonce（hex）。"""
    return os.urandom(32).hex()


def compute_verifier(salt: str, password: str) -> str:
    """verifier = sha256(salt + password)。"""
    h = hashlib.sha256()
    h.update(salt.encode("ascii"))
    h.update(password.encode("utf-8"))
    return h.hexdigest()


def compute_proof(nonce: str, verifier: str) -> str:
    """proof = sha256(nonce + verifier)。"""
    h = hashlib.sha256()
    h.update(nonce.encode("ascii"))
    h.update(verifier.encode("ascii"))
    return h.hexdigest()


class AuthStore:
    """用户表（用户名 → salt + verifier）。"""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---- 查询 ----
    def user_exists(self, username: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM users WHERE username=?", (username,)
            ).fetchone()
        return row is not None

    def get_salt(self, username: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT salt FROM users WHERE username=?", (username,)
            ).fetchone()
        return row["salt"] if row else None

    def get_verifier(self, username: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT verifier FROM users WHERE username=?", (username,)
            ).fetchone()
        return row["verifier"] if row else None

    # ---- 写入 ----
    def create_user(self, username: str, salt: str, verifier: str) -> bool:
        """创建用户；已存在返回 False。"""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO users (username, salt, verifier, created_at) VALUES (?,?,?,?)",
                    (username, salt, verifier, time.time()),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def close(self) -> None:
        with self._lock:
            self._conn.close()
