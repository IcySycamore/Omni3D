"""pytest 前置：**必须最先导入 torch**。

本机存在 DLL 加载顺序冲突（其它库先加载会让 torch 的 fbgemm.dll 加载失败），
server.py 里也有同样的处理。
"""
import torch  # noqa: F401  (import-side-effect: 必须早于 numpy / cv2 等)
