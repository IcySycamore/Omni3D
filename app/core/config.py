"""应用配置。

集中管理模型路径、计算设备、默认参数，避免散落在各文件中。
"""
from __future__ import annotations

import os

import torch


def _env_int(name: str, default: int, allow_zero: bool = False) -> int:
    """读取整数环境变量；缺失/非法/越界时回退到默认值。

    ``allow_zero=True`` 时 ``0`` 是**合法值**（用于表示「关闭」这类开关）；
    默认 ``False`` 时分 ``0`` 视为非法（如点数上限为 0 没意义）。
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= (0 if allow_zero else 1) else default


def _env_float(name: str, default: float, allow_zero: bool = False) -> float:
    """读取浮点环境变量；缺失/非法/越界时回退到默认值（语义同 `_env_int`）。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= (0.0 if allow_zero else 1e-12) else default


def _env_choice(name: str, allowed: tuple, default: str) -> str:
    """读取枚举型环境变量；非法/缺失时回退默认。"""
    raw = (os.environ.get(name) or "").strip().lower()
    return raw if raw in allowed else default


# 项目根目录（app/ 的上两级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Fast3R 预训练模型：HF 仓库名，或相对项目根目录的本地权重目录
CHECKPOINT_DIR = os.path.join(PROJECT_ROOT, "jedyang97", "Fast3R_ViT_Large_512")

# 计算设备：优先 CUDA
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# 默认图像分辨率（UI 可选 512 / 224）
# ⚠️ `224` 在 vendored 的 `fast3r/dust3r/utils/image.py` 里会**按短边裁成
# 224×224 正方形**（横幅丢左右、竖幅丢上下），不是等比缩放 → **仅用于快速预览，
# 勿用于测量**。属 vendored 行为，不能修改。Web 设置页对它给了告警文案。
DEFAULT_RESOLUTION = 512

# 视频默认抽帧数。视角冗余比单帧清晰度更能改善自洽性（见 docs/PERFORMANCE.md）。
DEFAULT_FRAME_COUNT = _env_int("OMNI3D_DEFAULT_FRAME_COUNT", 16)

# 推理参数
INFERENCE_DTYPE = torch.float32
ALIGN_CONF_PERCENTILE = 85       # 局部→全局对齐的置信度百分位

# ---- 点云输出（可视化 / PLY）----
# 置信度过滤：丢弃置信度最低的 N% 点（0 = 不过滤）。
# 上限 99：再往上会把点云削到几乎空，超范围一律回退默认值。
_conf_percentile = _env_int("OMNI3D_CONF_PERCENTILE", 10, allow_zero=True)
VIS_CONF_PERCENTILE = _conf_percentile if _conf_percentile <= 99 else 10
# 单次返回给客户端的**渲染**点数上限（PLY 始终为全量）。
# 上限过高会让手机端 JSON 解析与上传变慢；旧默认值 20000 明显偏小。
MAX_RENDER_POINTS = _env_int("OMNI3D_MAX_RENDER_POINTS", 60000)
VIS_POINT_SIZE = 0.0004          # 点云点大小

# ---- 点云来源（用哪个 head）----
# 上游在**重建评测**里默认用 local head：`configs/model/fast3r.yaml` 的
# `eval_use_pts3d_from_local_head: true`，`multiview_dust3r_module.py` 的
# `evaluate_reconstruction` 取 `pred["pts3d_local_aligned_to_global"]` + `conf_local`。
# global head 则是 `pred["pts3d_in_other_view"]` + `conf`。
# 两者处在**同一个全局坐标系**（local head 已被对齐过去），所以下游无差别；
# 可用 OMNI3D_PTS3D_SOURCE=local|global 切换，便于 A/B 对比与回退。
PTS3D_SOURCE = _env_choice("OMNI3D_PTS3D_SOURCE", ("local", "global"), "local")

# ---- 离群点剔除（SOR：statistical outlier removal）----
# 面积误差 ~ 平方级、体积误差 ~ 立方级放大，几何飞点必须先剔除。
# 与置信度过滤**互补**：置信度过滤删的是「模型没把握的点」，
# 而背景飞点往往置信度并不低，只能靠几何孤立性识别。
# `0` 表示**关闭**（`allow_zero=True` 才会把 0 当成合法值）。
# 代价：在**全量**点上建 cKDTree 并查 k 近邻，百万点是秒级开销，
# 属「宁可要精度不要速度」的取舍。
SOR_K = _env_int("OMNI3D_SOR_K", 8, allow_zero=True)
SOR_STD = _env_float("OMNI3D_SOR_STD", 2.0, allow_zero=True)
