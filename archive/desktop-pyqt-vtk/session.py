"""客户端会话管理器：token → username。

与服务器 `web/session_manager.py` 对应：服务器持有权威映射，
客户端保存登录后拿到的 token，并据此访问自己的历史。
"""
from __future__ import annotations


class SessionManager:
    """管理当前登录态（token ↔ username）。"""

    def __init__(self) -> None:
        self._token: str | None = None
        self._username: str | None = None

    @property
    def token(self) -> str | None:
        return self._token

    @property
    def username(self) -> str | None:
        return self._username

    @property
    def logged_in(self) -> bool:
        return bool(self._token and self._username)

    def start(self, token: str, username: str) -> None:
        """登录成功后写入。"""
        self._token = token
        self._username = username

    def clear(self) -> None:
        self._token = None
        self._username = None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"SessionManager(username={self._username!r}, logged_in={self.logged_in})"
