"""桌面客户端本地配置（服务器地址等），持久化到 QSettings。

服务器地址**不在主界面暴露**，只能从登录窗右上角齿轮进入「设置」修改。
"""
from __future__ import annotations

from PyQt5.QtCore import QSettings

ORG = "Omni3D"
APP = "Desktop"
DEFAULT_SERVER = "http://127.0.0.1:50865"


def normalize_server(url: str) -> str:
    """去掉首尾空白与结尾斜杠。"""
    v = (url or "").strip()
    while v.endswith("/"):
        v = v[:-1]
    return v


class AppConfig:
    """QSettings 包装。"""

    def __init__(self):
        self._s = QSettings(ORG, APP)

    @property
    def server_url(self) -> str:
        return normalize_server(self._s.value("server_url", DEFAULT_SERVER) or DEFAULT_SERVER)

    @server_url.setter
    def server_url(self, value: str) -> None:
        self._s.setValue("server_url", normalize_server(value))

    @property
    def username(self) -> str:
        return self._s.value("last_username", "") or ""

    @username.setter
    def username(self, value: str) -> None:
        self._s.setValue("last_username", value or "")

    @property
    def video_dir(self) -> str:
        return self._s.value("last_video_dir", "") or ""

    @video_dir.setter
    def video_dir(self, value: str) -> None:
        self._s.setValue("last_video_dir", value or "")

    def sync(self) -> None:
        self._s.sync()
