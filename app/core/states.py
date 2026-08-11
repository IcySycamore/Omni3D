"""应用状态机。

模型加载与推理均为异步后台任务，UI 依据状态决定可操作性并反馈给用户。
"""
from enum import Enum, auto


class AppState(Enum):
    """应用运行状态。"""

    IDLE = auto()        # 待机：等待上传
    LOADING = auto()     # 模型加载中（后台线程）
    READY = auto()       # 模型就绪，可提交推理
    PROCESSING = auto()  # 推理进行中
    ERROR = auto()       # 出错


# 各状态对应的提示文案
STATE_MESSAGE = {
    AppState.IDLE: "待机：请上传图片或视频。",
    AppState.LOADING: "正在加载模型...（首次加载需数分钟，可拖动窗口等待）",
    AppState.READY: "模型加载完成，可以上传图片/视频并提交。",
    AppState.PROCESSING: "正在重建 3D 场景...",
    AppState.ERROR: "发生错误，请查看日志。",
}
