"""会话管理器：登录令牌 token → username。

- 登录成功后签发随机 token；客户端后续请求携带 token。
- 服务端据此解析出 username，用于「历史记录归属」与权限隔离。
- 线程安全；带 TTL（默认 **30 分钟**，可用环境变量 ``OMNI3D_SESSION_TTL`` 覆盖）。
- **滑动过期**：每次成功解析 token 都会续期，用户持续操作就不会掉线；
  只有 **静默** 超过 TTL 才失效。
- 存储为内存字典：进程重启后需要重新登录（换取实现简单与无明文落盘）。
"""
from __future__ import annotations

import os
import secrets
import threading
import time

DEFAULT_TTL_SECONDS = 1800.0  # 30 分钟


def ttl_from_env() -> float:
    """读取 ``OMNI3D_SESSION_TTL``（秒）；缺失或非法时回退到 30 分钟。"""
    raw = (os.environ.get("OMNI3D_SESSION_TTL") or "").strip()
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TTL_SECONDS
    return value if value > 0 else DEFAULT_TTL_SECONDS


class SessionManager:
    """token → username 映射（滑动过期）。

    ``_sessions[token] = [username, created_at, last_seen]``
    —— created_at 仅用于诊断，过期判定以 last_seen 为准。
    """

    def __init__(self, ttl_seconds: float | None = None):
        self._lock = threading.Lock()
        self._sessions: dict[str, list] = {}
        self._ttl = ttl_from_env() if ttl_seconds is None else float(ttl_seconds)

    def create(self, username: str) -> str:
        """为 username 签发新 token。"""
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._sessions[token] = [username, now, now]
        return token

    def username_for(self, token: str | None) -> str | None:
        """解析 token 对应的 username；无效/过期返回 None。

        **命中即续期（滑动过期）** —— 这是本类的核心语义，
        所有鉴权路径都必须走这里，续期才会生效。
        """
        if not token:
            return None
        now = time.time()
        with self._lock:
            item = self._sessions.get(token)
            if item is None:
                return None
            if now - item[2] > self._ttl:
                self._sessions.pop(token, None)
                return None
            item[2] = now  # 滑动续期
            return item[0]

    def drop(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            return self._sessions.pop(token, None) is not None

    def count(self) -> int:
        """当前有效会话数（顺带清理过期项）。"""
        with self._lock:
            now = time.time()
            expired = [t for t, s in self._sessions.items() if now - s[2] > self._ttl]
            for t in expired:
                self._sessions.pop(t, None)
            return len(self._sessions)

    @property
    def ttl_seconds(self) -> float:
        return self._ttl
