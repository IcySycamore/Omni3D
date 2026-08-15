"""主窗口：完全代码手写的现代 Qt 前端（卡片化布局）。

设计：
- 不使用 .ui / pyuic 生成文件，全部控件用代码创建
- 标准系统窗口边框（标题栏由系统提供，可拖动/最小化/关闭）
- 卡片化布局（QFrame#card + 深色渐变主题 theme.py）
- 后台线程执行模型加载与推理，主线程只负责 UI
"""
import os
import shutil

from PyQt5.QtCore import QUrl
from PyQt5.QtMultimedia import QMediaContent, QMediaPlayer
from PyQt5.QtMultimediaWidgets import QVideoWidget
from PyQt5.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from app.core import config
from app.core.states import AppState, STATE_MESSAGE
from app.render import start_visualization
from app.ui.image_gallery import ImageGallery
from app.ui.theme import MAIN_WINDOW_QSS
from app.VTK_Widget import VTKWidget
from app.workers.inference_worker import InferenceWorker
from app.workers.model_loader import ModelLoaderWorker
from fast3r.viz.video_utils import extract_frames_from_video


class MainWindow(QMainWindow):
    """Omni3D 主窗口（手写卡片化布局）。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Omni3D · 3D 重建与测量")
        self.setMinimumSize(1280, 800)
        self.resize(1440, 900)
        self.setStyleSheet(MAIN_WINDOW_QSS)

        # 应用状态
        self.state = AppState.IDLE
        self.model = None
        self.image_paths = []
        self.resolution = config.DEFAULT_RESOLUTION
        self.model_loader = None
        self.inference_worker = None

        # 媒体播放器（视频预览）
        self.media_player = QMediaPlayer(None, QMediaPlayer.VideoSurface)

        self._build_ui()
        self._connect_signals()

        # 后台加载模型（不阻塞主线程）
        self._start_model_load()

    # ------------------------------------------------------------------ #
    # UI 构建
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(20)

        left_col = QVBoxLayout()
        left_col.setSpacing(16)

        # ---- Header 卡片 ----
        header = QFrame()
        header.setObjectName("card")
        h_layout = QVBoxLayout(header)
        h_layout.setContentsMargins(20, 16, 20, 16)
        h_layout.setSpacing(2)
        title = QLabel("⚡ Omni3D")
        title.setObjectName("appTitle")
        subtitle = QLabel("多视图图像 → 3D 点云 → 智能测量")
        subtitle.setObjectName("subtitle")
        h_layout.addWidget(title)
        h_layout.addWidget(subtitle)
        left_col.addWidget(header)

        # ---- 控制卡片 ----
        control = QFrame()
        control.setObjectName("card")
        c_layout = QVBoxLayout(control)
        c_layout.setContentsMargins(20, 16, 20, 16)
        c_layout.setSpacing(12)

        c_layout.addWidget(_section_label("输入"))

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self.btn_upload_images = QPushButton("📷 上传图片")
        self.btn_upload_video = QPushButton("🎬 上传视频")
        btn_row.addWidget(self.btn_upload_images)
        btn_row.addWidget(self.btn_upload_video)
        c_layout.addLayout(btn_row)

        opt_row = QHBoxLayout()
        opt_row.setSpacing(10)
        self.radio_512 = QRadioButton("512")
        self.radio_512.setChecked(True)
        self.radio_224 = QRadioButton("224")
        opt_row.addWidget(QLabel("分辨率:"))
        opt_row.addWidget(self.radio_512)
        opt_row.addWidget(self.radio_224)
        opt_row.addStretch(1)
        self.btn_submit = QPushButton("▶ 开始重建")
        opt_row.addWidget(self.btn_submit)
        c_layout.addLayout(opt_row)

        left_col.addWidget(control)

        # ---- 画廊卡片 ----
        gallery_card = QFrame()
        gallery_card.setObjectName("card")
        g_layout = QVBoxLayout(gallery_card)
        g_layout.setContentsMargins(20, 16, 20, 16)
        g_layout.setSpacing(10)
        g_layout.addWidget(_section_label("已选图像"))
        self.gallery = ImageGallery()
        self.gallery.setMinimumHeight(260)
        g_layout.addWidget(self.gallery, 1)
        left_col.addWidget(gallery_card, 1)

        # ---- 视频预览 + 状态卡片 ----
        bottom_card = QFrame()
        bottom_card.setObjectName("card")
        b_layout = QVBoxLayout(bottom_card)
        b_layout.setContentsMargins(20, 16, 20, 16)
        b_layout.setSpacing(10)

        self.video_view = QVideoWidget()
        self.video_view.setObjectName("videoView")
        self.video_view.setMinimumHeight(120)
        self.media_player.setVideoOutput(self.video_view)
        b_layout.addWidget(self.video_view)

        self.status_label = QLabel(STATE_MESSAGE[AppState.IDLE])
        self.status_label.setObjectName("statusText")
        self.status_label.setWordWrap(True)
        b_layout.addWidget(self.status_label)

        left_col.addWidget(bottom_card)

        # ---- 右侧 3D 可视化卡片 ----
        right_col = QVBoxLayout()
        right_col.setSpacing(16)

        viz_card = QFrame()
        viz_card.setObjectName("card")
        v_layout = QVBoxLayout(viz_card)
        v_layout.setContentsMargins(20, 16, 20, 16)
        v_layout.setSpacing(10)

        # 测量模式（render 模块读取这两个控件的选中状态）
        measure_row = QHBoxLayout()
        self.radioButton_3 = QRadioButton("🎯 选点")
        self.radioButton_4 = QRadioButton("📏 两点测距")
        self.radioButton_3.setChecked(True)
        measure_row.addWidget(QLabel("测量模式:"))
        measure_row.addWidget(self.radioButton_3)
        measure_row.addWidget(self.radioButton_4)
        measure_row.addStretch(1)
        v_layout.addLayout(measure_row)

        self.widget_3 = VTKWidget()
        self.widget_3.setMinimumSize(640, 600)
        v_layout.addWidget(self.widget_3, 1)

        hint = QLabel("提示：左键点击点云放置标记；「两点测距」模式下连续点击两点即可测量；按住中键拖拽平移")
        hint.setObjectName("hint")
        v_layout.addWidget(hint)

        right_col.addWidget(viz_card, 1)

        root.addLayout(left_col)
        root.addLayout(right_col, 1)

    # ------------------------------------------------------------------ #
    # 信号绑定
    # ------------------------------------------------------------------ #
    def _connect_signals(self):
        self.btn_upload_images.clicked.connect(self.on_upload_images)
        self.btn_upload_video.clicked.connect(self.on_upload_video)
        self.btn_submit.clicked.connect(self.on_submit)
        self.radio_512.toggled.connect(
            lambda checked: checked and self._set_resolution(512)
        )
        self.radio_224.toggled.connect(
            lambda checked: checked and self._set_resolution(224)
        )

    # ------------------------------------------------------------------ #
    # 状态管理
    # ------------------------------------------------------------------ #
    def _set_state(self, state, message=None):
        self.state = state
        self.status_label.setText(message or STATE_MESSAGE[state])

    def _set_resolution(self, resolution):
        self.resolution = resolution

    # ------------------------------------------------------------------ #
    # 用户操作
    # ------------------------------------------------------------------ #
    def on_upload_images(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择场景图片", "", "Image Files (*.png *.jpg *.bmp)"
        )
        if files:
            self.image_paths = files
            self.gallery.set_images(files)

    def on_upload_video(self):
        video_file, _ = QFileDialog.getOpenFileName(
            self, "选择视频", "", "Video Files (*.mp4 *.avi *.mov *.mkv)"
        )
        if not video_file:
            return

        temp_dir = os.path.join(config.PROJECT_ROOT, "temp_preview_frames", "preview")
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        os.makedirs(temp_dir, exist_ok=True)

        self.image_paths = extract_frames_from_video(video_file, temp_dir)
        self.gallery.set_images(self.image_paths)

        self.media_player.setMedia(QMediaContent(QUrl.fromLocalFile(video_file)))
        self.media_player.play()

    def on_submit(self):
        if self.model is None:
            self._set_state(AppState.LOADING, "模型仍在加载中，请稍候...")
            return
        if not self.image_paths:
            self._set_state(AppState.ERROR, "请先上传图片或视频。")
            return

        if hasattr(self,'widget_3'):
            self.widget_3.renderer.RemoveAllViewProps()
            self.widget_3.renderer.ResetCamera()
            self.widget_3.render_window.Render()
        self._set_state(AppState.PROCESSING)
        self.inference_worker = InferenceWorker(
            self.image_paths,
            self.model,
            config.DEVICE,
            resolution=self.resolution,
            dtype=config.INFERENCE_DTYPE,
            parent=self,
        )
        self.inference_worker.progress.connect(self._on_progress)
        self.inference_worker.finished_ok.connect(self._on_inference_done)
        self.inference_worker.failed.connect(self._on_failed)
        self.inference_worker.start()

    # ------------------------------------------------------------------ #
    # 模型加载回调
    # ------------------------------------------------------------------ #
    def _start_model_load(self):
        self._set_state(AppState.LOADING)
        self.model_loader = ModelLoaderWorker(
            config.CHECKPOINT_DIR, config.DEVICE, self
        )
        self.model_loader.model_ready.connect(self._on_model_ready)
        self.model_loader.failed.connect(self._on_failed)
        self.model_loader.start()

    def _on_model_ready(self, model):
        self.model = model
        self._set_state(AppState.READY)

    # ------------------------------------------------------------------ #
    # 推理回调
    # ------------------------------------------------------------------ #
    def _on_progress(self, stage):
        self.status_label.setText(f"重建中：{stage}")

    def _on_inference_done(self, output_dict, _profiling_info):
        self._set_state(AppState.READY, "正在渲染 3D 场景...")
        start_visualization(self, output_dict)
        self._set_state(AppState.READY, "重建完成，可在 3D 窗口中测量。")

    def _on_failed(self, message):
        self._set_state(AppState.ERROR, message)


def _section_label(text):
    """分区小标题。"""
    label = QLabel(text)
    label.setObjectName("sectionTitle")
    return label

if __name__ == "__main__":
    import sys
    from PyQt5.QtWidgets import QApplication
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())