"""登录窗口（无边框）。

- 右上角并与最小化 / 关闭并排：⚙ 齿轮 → 服务器设置
- 账号密码走挑战-应答（明文不出本机）
- 登录成功发出 `loggedIn(token, username)`
"""
from __future__ import annotations

from PyQt5.QtCore import QPoint, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from api_client import (
    ApiClient,
    ApiError,
    validate_password,
    validate_username,
)
from config import AppConfig
from settings_dialog import SettingsDialog


class LoginWindow(QWidget):
    """登录 / 注册。"""

    loggedIn = pyqtSignal(str, str)  # token, username

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cfg = AppConfig()
        self._drag_offset: QPoint | None = None

        self.setWindowTitle("Omni3D · 登录")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(420, 430)
        self._build_ui()

    # ---- UI ----
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        card = QFrame()
        card.setObjectName("loginCard")
        outer.addWidget(card)

        root = QVBoxLayout(card)
        root.setContentsMargins(24, 14, 24, 22)
        root.setSpacing(10)

        root.addWidget(self._build_titlebar())

        title = QLabel("◈ Omni3D")
        title.setObjectName("appTitle")
        title.setAlignment(Qt.AlignCenter)
        root.addWidget(title)

        subtitle = QLabel("登录以查看你的重建历史")
        subtitle.setObjectName("subtitle")
        subtitle.setAlignment(Qt.AlignCenter)
        root.addWidget(subtitle)
        root.addSpacing(10)

        self.username = QLineEdit()
        self.username.setPlaceholderText("用户名")
        self.username.setText(self._cfg.username)
        root.addWidget(self.username)

        self.password = QLineEdit()
        self.password.setPlaceholderText("密码")
        self.password.setEchoMode(QLineEdit.Password)
        root.addWidget(self.password)

        root.addSpacing(6)
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.login_btn = QPushButton("登录")
        self.login_btn.clicked.connect(self._on_login)
        buttons.addWidget(self.login_btn, 2)

        self.register_btn = QPushButton("注册")
        self.register_btn.setObjectName("ghostBtn")
        self.register_btn.clicked.connect(self._on_register)
        buttons.addWidget(self.register_btn, 1)
        root.addLayout(buttons)

        self.status = QLabel("")
        self.status.setObjectName("hint")
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(34)
        root.addWidget(self.status)
        root.addStretch(1)

        self.password.returnPressed.connect(self._on_login)

    def _build_titlebar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("titleBar")
        bar.setFixedHeight(28)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        brand = QLabel("Omni3D")
        brand.setObjectName("winTitle")
        lay.addWidget(brand)
        lay.addStretch(1)

        # 齿轮：服务器设置（位于最小化 / 关闭旁边）
        self.settings_btn = QToolButton()
        self.settings_btn.setObjectName("winBtn")
        self.settings_btn.setText("⚙")
        self.settings_btn.setToolTip("服务器设置")
        self.settings_btn.clicked.connect(self._on_settings)
        lay.addWidget(self.settings_btn)

        min_btn = QToolButton()
        min_btn.setObjectName("winBtn")
        min_btn.setText("—")
        min_btn.setToolTip("最小化")
        min_btn.clicked.connect(self.showMinimized)
        lay.addWidget(min_btn)

        close_btn = QToolButton()
        close_btn.setObjectName("winBtnClose")
        close_btn.setText("✕")
        close_btn.setToolTip("关闭")
        close_btn.clicked.connect(self.close)
        lay.addWidget(close_btn)

        return bar

    # ---- 无边框拖动 ----
    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton and event.pos().y() < 48:
            self._drag_offset = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPos() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):  # noqa: N802
        self._drag_offset = None

    # ---- 动作 ----
    def _on_settings(self) -> None:
        SettingsDialog(self).exec_()

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self.login_btn.setEnabled(not busy)
        self.register_btn.setEnabled(not busy)
        self.username.setEnabled(not busy)
        self.password.setEnabled(not busy)
        if busy:
            QApplication.setOverrideCursor(Qt.WaitCursor)
        else:
            QApplication.restoreOverrideCursor()
        if text:
            self.status.setText(text)
        QApplication.processEvents()

    def _client(self) -> ApiClient:
        return ApiClient(self._cfg.server_url, client_id="desktop")

    def _on_login(self) -> None:
        user = self.username.text().strip()
        pwd = self.password.text()
        if not user or not pwd:
            self.status.setText("请填写用户名和密码")
            return

        self._set_busy(True, "登录中…")
        try:
            client = self._client()
            token = client.login(user, pwd)
            self._cfg.username = user
            self._cfg.sync()
            self._maybe_claim(client)  # 匿名历史 → 手动确认后并入
            self.status.setText("登录成功")
            self.loggedIn.emit(token, user)
            self.close()
        except ApiError as exc:
            self.status.setText(f"登录失败：{exc.message}")
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"无法连接服务器：{exc}\n（右上角 ⚙ 可修改服务器地址）")
        finally:
            self._set_busy(False)

    def _maybe_claim(self, client: ApiClient) -> None:
        """本机有匿名记录时询问是否并入账号（不自动并：多人共用设备不会误并）。"""
        try:
            count = client.claim_preview()
        except ApiError:
            return
        if count <= 0:
            return
        ans = QMessageBox.question(
            self, "匿名记录",
            f"检测到本机有 {count} 条匿名记录，是否并入当前账号？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ans == QMessageBox.Yes:
            try:
                client.claim_anonymous()
            except ApiError:
                pass

    def _on_register(self) -> None:
        user = self.username.text().strip()
        pwd = self.password.text()
        invalid = validate_username(user)
        if invalid:
            self.status.setText(invalid)
            return
        weak = validate_password(pwd)
        if weak:
            self.status.setText(weak)
            return

        self._set_busy(True, "注册中…")
        try:
            client = self._client()
            client.register(user, pwd)
            token = client.login(user, pwd)
            self._cfg.username = user
            self._cfg.sync()
            self.status.setText("注册成功，已自动登录")
            self.loggedIn.emit(token, user)
            self.close()
        except ApiError as exc:
            self.status.setText(f"注册失败：{exc.message}")
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"无法连接服务器：{exc}")
        finally:
            self._set_busy(False)
