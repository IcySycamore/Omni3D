"""Omni3D 桌面客户端入口。

流程：登录窗（右上角齿轮 → 服务器设置）→ 主窗（重建 / VTK 查看 / 历史）。

运行：
    python desktop/main.py
"""
from __future__ import annotations

import os
import sys

# 允许直接以脚本方式运行（desktop/ 自身加入 sys.path）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from api_client import ApiClient  # noqa: E402
from config import AppConfig  # noqa: E402
from login_window import LoginWindow  # noqa: E402
from main_window import MainWindow  # noqa: E402
from ui.theme import MAIN_WINDOW_QSS  # noqa: E402


class AppController:
    """管理登录窗与主窗的切换（避免对象被回收）。"""

    def __init__(self) -> None:
        self._cfg = AppConfig()
        self._login: LoginWindow | None = None
        self._main: MainWindow | None = None

    def start(self) -> None:
        self._show_login()

    def _show_login(self) -> None:
        self._main = None
        self._login = LoginWindow()
        self._login.loggedIn.connect(self._on_logged_in)
        self._login.show()
        self._center(self._login)

    def _on_logged_in(self, token: str, username: str) -> None:
        client = ApiClient(self._cfg.server_url, token=token, client_id="desktop")
        self._main = MainWindow(client, username)
        self._main.loggedOut.connect(self._on_logged_out)
        self._main.show()
        self._center(self._main)

    def _on_logged_out(self) -> None:
        self._show_login()

    @staticmethod
    def _center(win) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        win.move(geo.center().x() - win.width() // 2,
                 geo.center().y() - win.height() // 2)


def main() -> int:
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName("Omni3D Desktop")
    app.setStyleSheet(MAIN_WINDOW_QSS)

    ctrl = AppController()
    ctrl.start()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
