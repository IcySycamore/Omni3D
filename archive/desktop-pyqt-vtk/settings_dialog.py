"""服务器设置对话框。

入口：登录窗右上角的齿轮（与最小化/关闭并排）。
服务器地址**只在这里配置**，不在主界面暴露。
"""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from api_client import ApiClient, ApiError
from config import AppConfig, DEFAULT_SERVER, normalize_server


class SettingsDialog(QDialog):
    """服务器地址配置 + 连通性测试。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setModal(True)
        self.setMinimumWidth(460)
        self._cfg = AppConfig()

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        title = QLabel("服务器")
        title.setObjectName("sectionTitle")
        root.addWidget(title)

        self.edit = QLineEdit(self._cfg.server_url)
        self.edit.setPlaceholderText(DEFAULT_SERVER)
        root.addWidget(self.edit)

        hint = QLabel("重建请求与历史记录都发往该地址（server 只有一种）。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.status = QLabel("")
        self.status.setObjectName("hint")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        row = QHBoxLayout()
        self.test_btn = QPushButton("测试连接")
        self.test_btn.setObjectName("ghostBtn")
        self.test_btn.clicked.connect(self._on_test)
        row.addWidget(self.test_btn)
        row.addStretch(1)

        cancel = QPushButton("取消")
        cancel.setObjectName("ghostBtn")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)

        save = QPushButton("保存")
        save.clicked.connect(self._on_save)
        row.addWidget(save)
        root.addLayout(row)

    # ---- 槽 ----
    def _on_test(self) -> None:
        url = normalize_server(self.edit.text())
        self.status.setText("测试中…")
        self.test_btn.setEnabled(False)
        self.test_btn.setCursor(Qt.WaitCursor)
        try:
            info = ApiClient(url, client_id="desktop").health()
            device = info.get("device", "?")
            if info.get("ready"):
                self.status.setText(f"✅ 已连通：模型就绪（device={device}）")
            else:
                err = info.get("error") or "模型仍在加载"
                self.status.setText(f"⚠️ 已连通，但未就绪：{err}")
        except ApiError as exc:
            self.status.setText(f"❌ 失败：{exc.message}")
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"❌ 无法连接：{exc}")
        finally:
            self.test_btn.setEnabled(True)
            self.test_btn.unsetCursor()

    def _on_save(self) -> None:
        self._cfg.server_url = self.edit.text()
        self._cfg.sync()
        self.accept()
