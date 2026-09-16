"""主窗口：重建 + VTK 查看 + 历史（按 username）。

依赖唯一 server 提供重建能力；本窗口只负责采集、提交、呈现。
"""
from __future__ import annotations

import os
import time

import numpy as np
from PyQt5.QtCore import QThread, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from api_client import ApiClient, ApiError, AuthExpiredError
from config import AppConfig
from vtk_view import PointCloudView

VIDEO_FILTER = "视频 (*.mp4 *.MP4 *.mov *.MOV *.avi *.mkv);;所有文件 (*)"


def find_demo_dir() -> str:
    """向上查找 demo_examples/。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cur = here
    for _ in range(6):
        candidate = os.path.join(cur, "demo_examples")
        if os.path.isdir(candidate):
            return candidate
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return ""


def fmt_time(ts) -> str:
    if not ts:
        return "-"
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


class ReconstructWorker(QThread):
    """后台执行「上传 → 轮询」，避免阻塞 UI。"""

    progressed = pyqtSignal(dict)
    finished_ok = pyqtSignal(dict)
    failed = pyqtSignal(str)
    auth_expired = pyqtSignal(str)

    def __init__(self, client: ApiClient, video_path: str, frame_count: int,
                 resolution: int = 512, parent=None):
        super().__init__(parent)
        self._client = client
        self._video = video_path
        self._frames = frame_count
        self._resolution = resolution
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:  # noqa: D102
        try:
            self.progressed.emit({"stage": "上传中…", "progress": 0.02})
            task_id = self._client.submit_video(self._video, frame_count=self._frames,
                                                resolution=self._resolution)

            def on_progress(task: dict) -> None:
                self.progressed.emit(task)

            task = self._client.poll_task(
                task_id,
                on_progress=on_progress,
                cancelled=lambda: self._cancelled,
            )
            if task.get("status") == "failed":
                self.failed.emit(str(task.get("error") or "重建失败"))
            else:
                self.finished_ok.emit(task)
        except AuthExpiredError as exc:
            self.auth_expired.emit(exc.message)
        except ApiError as exc:
            self.failed.emit(exc.message)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    """重建客户端主窗口。"""

    loggedOut = pyqtSignal()

    def __init__(self, client: ApiClient, username: str, parent=None):
        super().__init__(parent)
        self._client = client
        self._username = username
        self._cfg = AppConfig()
        self._worker: ReconstructWorker | None = None
        self._video_path = ""
        self._session_id = ""
        self._scale: float | None = None
        self._picked: list = []
        self._auth_expired_handled = False

        self.setWindowTitle(f"Omni3D · {username}")
        self.resize(1280, 820)
        self._build_ui()
        self._refresh_demos()
        self._refresh_history()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 12, 16, 14)
        root.setSpacing(10)

        root.addWidget(self._build_header())

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_view_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([330, 950])
        root.addWidget(splitter, 1)

    def _build_header(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("card")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 10, 16, 10)

        title = QLabel("◈ Omni3D")
        title.setObjectName("appTitle")
        lay.addWidget(title)

        info = QLabel(f"用户 {self._username}　·　{self._client.base_url}")
        info.setObjectName("hint")
        lay.addWidget(info)
        lay.addStretch(1)

        logout = QPushButton("退出登录")
        logout.setObjectName("ghostBtn")
        logout.setProperty("class", "smallBtn")
        logout.clicked.connect(self._on_logout)
        lay.addWidget(logout)
        return bar

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        # --- 素材 ---
        src_card = QFrame()
        src_card.setObjectName("card")
        src = QVBoxLayout(src_card)
        src.setContentsMargins(14, 12, 14, 14)
        src.setSpacing(8)

        src_title = QLabel("素材")
        src_title.setObjectName("sectionTitle")
        src.addWidget(src_title)

        pick_row = QHBoxLayout()
        self.pick_btn = QPushButton("选择视频…")
        self.pick_btn.clicked.connect(self._on_pick_video)
        pick_row.addWidget(self.pick_btn, 1)
        src.addLayout(pick_row)

        demo_row = QHBoxLayout()
        self.demo_box = QComboBox()
        demo_row.addWidget(self.demo_box, 1)
        self.demo_btn = QPushButton("用示例")
        self.demo_btn.setObjectName("ghostBtn")
        self.demo_btn.clicked.connect(self._on_use_demo)
        demo_row.addWidget(self.demo_btn)
        src.addLayout(demo_row)

        self.file_label = QLabel("未选择素材")
        self.file_label.setObjectName("hint")
        self.file_label.setWordWrap(True)
        src.addWidget(self.file_label)

        frames_row = QHBoxLayout()
        frames_row.addWidget(QLabel("抽帧数"))
        self.frames_box = QSpinBox()
        self.frames_box.setRange(2, 64)
        self.frames_box.setValue(12)
        frames_row.addWidget(self.frames_box, 1)
        frames_row.addWidget(QLabel("分辨率"))
        self.res_box = QComboBox()
        self.res_box.addItem("512", 512)
        self.res_box.addItem("224", 224)
        frames_row.addWidget(self.res_box, 1)
        src.addLayout(frames_row)

        self.run_btn = QPushButton("开始重建")
        self.run_btn.setEnabled(False)
        self.run_btn.clicked.connect(self._on_run)
        src.addWidget(self.run_btn)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        src.addWidget(self.progress)

        self.stage_label = QLabel("待机")
        self.stage_label.setObjectName("statusText")
        self.stage_label.setWordWrap(True)
        src.addWidget(self.stage_label)
        lay.addWidget(src_card)

        # --- 历史 ---
        hist_card = QFrame()
        hist_card.setObjectName("card")
        hist = QVBoxLayout(hist_card)
        hist.setContentsMargins(14, 12, 14, 14)
        hist.setSpacing(8)

        hist_head = QHBoxLayout()
        hist_title = QLabel(f"历史记录 · {self._username}")
        hist_title.setObjectName("sectionTitle")
        hist_head.addWidget(hist_title)
        hist_head.addStretch(1)
        refresh = QPushButton("刷新")
        refresh.setObjectName("ghostBtn")
        refresh.clicked.connect(self._refresh_history)
        hist_head.addWidget(refresh)
        hist.addLayout(hist_head)

        self.history = QListWidget()
        self.history.setSelectionMode(QAbstractItemView.SingleSelection)
        self.history.itemDoubleClicked.connect(self._on_history_activated)
        hist.addWidget(self.history, 1)

        hist_actions = QHBoxLayout()
        load_btn = QPushButton("加载")
        load_btn.setObjectName("ghostBtn")
        load_btn.clicked.connect(self._on_history_activated)
        hist_actions.addWidget(load_btn)
        del_btn = QPushButton("删除")
        del_btn.setObjectName("dangerBtn")
        del_btn.clicked.connect(self._on_delete_history)
        hist_actions.addWidget(del_btn)
        hist.addLayout(hist_actions)
        lay.addWidget(hist_card, 1)

        return panel

    def _build_view_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("card")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        tools = QHBoxLayout()
        self.measure_btn = QPushButton("两点测距")
        self.measure_btn.setCheckable(True)
        self.measure_btn.toggled.connect(self._on_measure_toggled)
        tools.addWidget(self.measure_btn)

        self.measure_label = QLabel("在点云上点选两点测距")
        self.measure_label.setObjectName("statusText")
        tools.addWidget(self.measure_label, 1)

        self.calib_btn = QPushButton("标尺校准…")
        self.calib_btn.setObjectName("ghostBtn")
        self.calib_btn.setEnabled(False)
        self.calib_btn.clicked.connect(self._on_calibrate)
        tools.addWidget(self.calib_btn)

        clear_btn = QPushButton("清除标记")
        clear_btn.setObjectName("ghostBtn")
        clear_btn.clicked.connect(self._on_clear_marks)
        tools.addWidget(clear_btn)

        self.ply_btn = QPushButton("下载 PLY")
        self.ply_btn.setObjectName("ghostBtn")
        self.ply_btn.setEnabled(False)
        self.ply_btn.clicked.connect(self._on_download_ply)
        tools.addWidget(self.ply_btn)

        reset_btn = QPushButton("重置视角")
        reset_btn.setObjectName("ghostBtn")
        reset_btn.clicked.connect(lambda: self.view.reset_view())
        tools.addWidget(reset_btn)
        lay.addLayout(tools)

        self.view = PointCloudView()
        self.view.pickedTwoPoints.connect(self._on_two_points)
        lay.addWidget(self.view, 1)

        self.view_hint = QLabel("尚未加载点云：请提交一次重建，或从左侧历史记录加载。")
        self.view_hint.setObjectName("hint")
        lay.addWidget(self.view_hint)
        return panel

    # ---------- 素材 ----------
    def _refresh_demos(self) -> None:
        self.demo_box.clear()
        demo_dir = find_demo_dir()
        self._demo_dir = demo_dir
        if not demo_dir:
            self.demo_box.addItem("(未找到 demo_examples)")
            self.demo_btn.setEnabled(False)
            return
        for sub in sorted(os.listdir(demo_dir)):
            d = os.path.join(demo_dir, sub)
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if name.lower().endswith((".mp4", ".mov")):
                    self.demo_box.addItem(f"{sub}/{name}", os.path.join(d, name))
        self.demo_btn.setEnabled(self.demo_box.count() > 0)

    def _on_pick_video(self) -> None:
        start = self._cfg.video_dir or (self._demo_dir or "")
        path, _ = QFileDialog.getOpenFileName(self, "选择视频", start, VIDEO_FILTER)
        if not path:
            return
        self._set_video(path)
        self._cfg.video_dir = os.path.dirname(path)
        self._cfg.sync()

    def _on_use_demo(self) -> None:
        path = self.demo_box.currentData()
        if path:
            self._set_video(path)

    def _set_video(self, path: str) -> None:
        self._video_path = path
        size_mb = os.path.getsize(path) / 1024 / 1024
        self.file_label.setText(f"{os.path.basename(path)}（{size_mb:.1f} MB）")
        self.run_btn.setEnabled(True)

    # ---------- 重建 ----------
    def _on_run(self) -> None:
        if not self._video_path:
            return
        if self._client.token is None:
            QMessageBox.warning(self, "未登录", "登录已失效，请重新登录。")
            return
        self.run_btn.setEnabled(False)
        self.progress.setValue(0)
        self.stage_label.setText("提交中…")
        self._session_id = ""
        self._scale = None
        self.view.clear_cloud()

        self._worker = ReconstructWorker(self._client, self._video_path,
                                         self.frames_box.value(),
                                         self.res_box.currentData(), self)
        self._worker.progressed.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.auth_expired.connect(self._on_auth_expired)
        self._worker.start()

    def _on_progress(self, task: dict) -> None:
        stage = task.get("stage") or "处理中"
        prog = task.get("progress")
        if prog is None:
            prog = 0.0
        self.progress.setValue(int(float(prog) * 100))
        self.stage_label.setText(f"{stage}　{int(float(prog) * 100)}%")

    def _on_finished(self, task: dict) -> None:
        result = task.get("result") or {}
        self._session_id = task.get("task_id") or ""
        points = result.get("points") or []
        # 服务器若已按 AR 位姿对齐到真实尺度，直接采用（无需手动标尺校准）
        self._scale = float(result["scale"]) if result.get("scale") else None
        quality = result.get("quality") or {}
        self.progress.setValue(100)
        elapsed = result.get("elapsed_s")
        self.stage_label.setText(
            f"完成：{result.get('num_views')} 视图 · {result.get('num_points')} 点 · "
            f"{elapsed}s"
            + (f" · 残差比 {quality['residual_ratio_median']:.4g}"
               if quality.get("residual_ratio_median") is not None else "")
        )
        self.run_btn.setEnabled(True)
        self._show_points(points)
        self._refresh_history()

    def _on_failed(self, message: str) -> None:
        self.run_btn.setEnabled(True)
        self.progress.setValue(0)
        self.stage_label.setText(f"失败：{message}")
        QMessageBox.critical(self, "重建失败", message)

    def _on_auth_expired(self, message: str = "") -> None:
        """会话过期（服务器 30 分钟滑动过期后返回 401）：提示并退回登录窗。"""
        if self._auth_expired_handled:
            return
        self._auth_expired_handled = True
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
        self._client.token = None  # 避免后续请求反复触发
        QMessageBox.warning(
            self, "会话已过期",
            "登录状态已过期（长时间无操作），请重新登录。"
            + (f"\n\n{message}" if message else ""),
        )
        self.loggedOut.emit()
        self.close()

    # ---------- 点云 / 测量 ----------
    def _show_points(self, points: list) -> None:
        self.view.clear_measurements()
        self.view.set_points(points)
        self._picked = []
        self.measure_label.setText("在点云上点选两点测距")
        self.calib_btn.setEnabled(False)
        self.ply_btn.setEnabled(bool(self._session_id and points))
        self.view_hint.setText(
            f"点数 {len(points)}　·　"
            + (f"已标定尺度 scale={self._scale:.4g}" if self._scale else "未标定尺度（用「标尺校准」）")
        )

    def _on_measure_toggled(self, on: bool) -> None:
        self.view.set_measure_mode(on)
        if on:
            self.measure_label.setText("测距模式：依次点选两点")
        else:
            self.measure_label.setText("已退出测距模式")

    def _on_clear_marks(self) -> None:
        self.view.clear_measurements()
        self._picked = []
        self.measure_label.setText("已清除标记")

    def _on_two_points(self, points: list) -> None:
        self._picked = points
        p0 = np.asarray(points[0], dtype=float)
        p1 = np.asarray(points[1], dtype=float)
        model_dist = float(np.linalg.norm(p0 - p1))
        if self._scale:
            self.measure_label.setText(
                f"模型距离 {model_dist:.4f}　→　真实距离 {model_dist * self._scale:.4f} m"
            )
        else:
            self.measure_label.setText(f"模型距离 {model_dist:.4f}（未标定，单位任意）")
        self.calib_btn.setEnabled(bool(self._session_id))

    def _on_calibrate(self) -> None:
        if len(self._picked) != 2 or not self._session_id:
            QMessageBox.information(self, "标尺校准", "请先点选两点，且当前点云来自一次重建/历史。")
            return
        real, ok = QInputDialog.getDouble(
            self, "标尺校准", "这两点的真实距离（米）：", 1.0, 1e-6, 1e6, 4
        )
        if not ok:
            return
        try:
            res = self._client.infer_scale(
                self._session_id, self._picked[0], self._picked[1], real
            )
            self._scale = float(res["scale"])
            self.measure_label.setText(
                f"已校准：scale={self._scale:.4g}（模型距离 × scale = 真实米）"
            )
            self.view_hint.setText(f"点数 {len(self.view._points)}　·　已标定 scale={self._scale:.4g}")
        except AuthExpiredError as exc:
            self._on_auth_expired(exc.message)
        except ApiError as exc:
            QMessageBox.critical(self, "校准失败", exc.message)

    # ---------- 历史 ----------
    def _refresh_history(self) -> None:
        self.history.clear()
        try:
            data = self._client.list_history(limit=50)
        except AuthExpiredError as exc:
            self._on_auth_expired(exc.message)
            return
        except ApiError as exc:
            self.history.addItem(f"(加载失败：{exc.message})")
            return
        sessions = data.get("sessions", [])
        if not sessions:
            item = QListWidgetItem("（暂无历史）")
            item.setFlags(Qt.NoItemFlags)
            self.history.addItem(item)
            return
        for s in sessions:
            text = (f"{fmt_time(s.get('created_at'))} · {s.get('num_views')} 视图 · "
                    f"{s.get('num_points')} 点 · {s.get('status')}")
            if s.get("scale"):
                text += f" · scale={float(s['scale']):.3g}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, s.get("session_id"))
            self.history.addItem(item)

    def _selected_session(self) -> str:
        item = self.history.currentItem()
        if item is None:
            return ""
        return item.data(Qt.UserRole) or ""

    def _on_history_activated(self, *_args) -> None:
        sid = self._selected_session()
        if not sid:
            return
        try:
            data = self._client.get_session(sid, include_points=True)
        except AuthExpiredError as exc:
            self._on_auth_expired(exc.message)
            return
        except ApiError as exc:
            QMessageBox.critical(self, "加载失败", exc.message)
            return
        self._session_id = sid
        self._scale = float(data["scale"]) if data.get("scale") else None
        points = data.get("points") or []
        if not points:
            QMessageBox.information(self, "无点云", "该历史没有可用点云数据。")
            return
        self._show_points(points)

    def _on_delete_history(self) -> None:
        sid = self._selected_session()
        if not sid:
            return
        if QMessageBox.question(self, "删除", "确定删除这条历史记录？") != QMessageBox.Yes:
            return
        try:
            self._client.delete_history(sid)
        except AuthExpiredError as exc:
            self._on_auth_expired(exc.message)
            return
        except ApiError as exc:
            QMessageBox.critical(self, "删除失败", exc.message)
            return
        self._refresh_history()

    # ---------- 其他 ----------
    def _on_download_ply(self) -> None:
        """下载当前会话的完整 PLY（走服务器 /api/history/{id}/ply）。"""
        if not self._session_id:
            QMessageBox.information(self, "下载 PLY",
                                    "请先完成一次重建，或从历史记录加载一条。")
            return
        start_dir = self._cfg.video_dir or os.path.expanduser("~")
        default = os.path.join(start_dir, f"{self._session_id}.ply")
        path, _ = QFileDialog.getSaveFileName(self, "保存 PLY", default,
                                              "PLY 点云 (*.ply)")
        if not path:
            return
        try:
            data = self._client.download_ply(self._session_id)
        except AuthExpiredError as exc:
            self._on_auth_expired(exc.message)
            return
        except ApiError as exc:
            QMessageBox.critical(self, "下载失败", exc.message)
            return
        try:
            with open(path, "wb") as fh:
                fh.write(data)
        except OSError as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        size_mb = len(data) / 1024 / 1024
        self.stage_label.setText(f"已保存 PLY（{size_mb:.1f} MB）：{path}")

    def _on_logout(self) -> None:
        self._client.logout()
        self.loggedOut.emit()
        self.close()

    def closeEvent(self, event):  # noqa: N802
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        try:
            self.view.interactor.Finalize()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)
