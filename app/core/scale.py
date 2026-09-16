"""真实尺度反推（纯函数，无 torch / Qt 依赖）。

场景：重建得到的点云在「模型坐标系」中是任意单位。
用户在点云上选取两点并给出这两点的**已知真实距离**（标尺校准 / 实际测量），
即可反推出尺度因子，使后续所有测量直接以米为单位显示。

    scale = 真实距离 / 模型坐标系下的两点距离

反过来，已知 scale 时：

    真实距离 = scale × 模型坐标系下的两点距离

这是 client（web / Qt）与 server 共用的**唯一一份**尺度换算实现。
"""
from __future__ import annotations

import math
from typing import Sequence


class ScaleError(ValueError):
    """尺度反推失败（输入非法）。"""


def model_distance(point_a: Sequence[float], point_b: Sequence[float]) -> float:
    """计算模型坐标系下两点的欧氏距离。

    Args:
        point_a / point_b: 长度 ≥3 的数值序列 (x, y, z)。

    Raises:
        ScaleError: 点维度不足或含非有限数值。
    """
    if len(point_a) < 3 or len(point_b) < 3:
        raise ScaleError("点坐标至少需要 3 个分量 (x, y, z)")
    try:
        dx = float(point_a[0]) - float(point_b[0])
        dy = float(point_a[1]) - float(point_b[1])
        dz = float(point_a[2]) - float(point_b[2])
    except (TypeError, ValueError) as exc:
        raise ScaleError(f"点坐标含非法数值: {exc}") from exc
    dist = math.sqrt(dx * dx + dy * dy + dz * dz)
    if not math.isfinite(dist):
        raise ScaleError("点坐标含非有限数值")
    return dist


def infer_scale_from_measurement(
    point_a: Sequence[float],
    point_b: Sequence[float],
    real_distance: float,
) -> dict:
    """从「模型内两点 + 已知真实距离」反推尺度因子。

    Args:
        point_a / point_b: 用户在点云上选取的两点（模型坐标系，任意单位）。
        real_distance: 这两点对应的真实距离（米）。

    Returns:
        dict: {
            "scale": float,                 # 真实距离 / 模型距离
            "model_distance": float,        # 模型坐标系下两点距离
            "real_distance": float,         # 回显输入
            "point_a": [...3], "point_b": [...3],
        }

    Raises:
        ScaleError: 真实距离非正、点非法、或模型距离为 0（两点重合）。
    """
    if not (isinstance(real_distance, (int, float)) and math.isfinite(real_distance)):
        raise ScaleError("真实距离必须为有限数值")
    if real_distance <= 0:
        raise ScaleError("真实距离必须为正数（米）")

    md = model_distance(point_a, point_b)
    if md <= 0:
        raise ScaleError("两点在模型坐标系下重合，无法反推尺度")

    return {
        "scale": real_distance / md,
        "model_distance": md,
        "real_distance": float(real_distance),
        "point_a": [float(point_a[0]), float(point_a[1]), float(point_a[2])],
        "point_b": [float(point_b[0]), float(point_b[1]), float(point_b[2])],
    }


def apply_scale(model_distance_value: float, scale: float) -> float:
    """把模型坐标系下的距离换算为真实距离（米）。"""
    return float(model_distance_value) * float(scale)
