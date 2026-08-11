"""后台线程：执行重建管线（加载→推理→对齐）。

推理耗时与视图数量、分辨率相关，放入 QThread 执行，
通过信号把进度与结果回传主线程。
"""
import traceback

import torch
from PyQt5.QtCore import QThread, pyqtSignal

from app.core.pipeline import run_reconstruction


class InferenceWorker(QThread):
    """在后台线程执行完整重建管线。"""

    progress = pyqtSignal(str)          # 阶段进度
    finished_ok = pyqtSignal(object, object)  # (output_dict, profiling_info)
    failed = pyqtSignal(str)            # 失败原因

    def __init__(
        self,
        image_paths,
        model,
        device,
        resolution=512,
        dtype=torch.float32,
        parent=None,
    ):
        super().__init__(parent)
        self.image_paths = image_paths
        self.model = model
        self.device = device
        self.resolution = resolution
        self.dtype = dtype

    def run(self):
        try:
            output_dict, profiling_info = run_reconstruction(
                self.image_paths,
                self.model,
                self.device,
                resolution=self.resolution,
                dtype=self.dtype,
                progress_callback=self.progress.emit,
            )
            self.finished_ok.emit(output_dict, profiling_info)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.failed.emit(f"重建失败: {exc}")
