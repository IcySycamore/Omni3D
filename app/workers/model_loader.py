"""后台线程：加载 Fast3R 模型。

模型加载（构建 ViT-Large + 读取 2.5GB 权重 + 拷贝到 GPU）耗时数分钟，
放入 QThread 执行，避免阻塞 GUI 主线程导致窗口无响应。
"""
import traceback

from PyQt5.QtCore import QThread, pyqtSignal

from fast3r.models.fast3r import Fast3R


class ModelLoaderWorker(QThread):
    """在后台线程加载 Fast3R 预训练模型。"""

    model_ready = pyqtSignal(object)  # 加载成功，携带模型对象
    failed = pyqtSignal(str)          # 加载失败，携带错误信息

    def __init__(self, checkpoint_dir, device, parent=None):
        super().__init__(parent)
        self.checkpoint_dir = checkpoint_dir
        self.device = device

    def run(self):
        try:
            model = Fast3R.from_pretrained(self.checkpoint_dir).to(self.device)
            model.eval()
            self.model_ready.emit(model)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.failed.emit(f"模型加载失败: {exc}")
