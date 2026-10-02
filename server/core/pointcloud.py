"""点云纯几何后处理（不依赖 torch / 模型，可单独单测）。

目前只有 **SOR（统计离群点剔除）**。为什么需要它：面积/体积测量对离群飞点
极其敏感（面积误差 ~ 平方级、体积 ~ 立方级放大），而「置信度低」与
「几何上孤立」**不是一回事** —— 背景飞点往往置信度并不低，仅靠置信度过滤
挡不住。

为什么独立实现：vendored 的 `fast3r` 里 `clean_pointcloud` 依赖完整
`BasePCOptimizer`（需要 `get_im_poses` / `get_intrinsics` / `depth_to_pts3d`），
本项目不构造该对象，无法直接复用；这里只依赖 `scipy.spatial.cKDTree`。
"""
from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree

__all__ = ["statistical_outlier_removal"]

# 分批查询的批大小。百万点级别下一次性 query 会同时持有
# (N, k+1) 的距离与索引矩阵（float64 + int64 ≈ 300MB），分批可把峰值压到 1/5 左右。
_QUERY_CHUNK = 500_000


def statistical_outlier_removal(points, k: int = 8, std: float = 2.0,
                                chunk_size: int = _QUERY_CHUNK) -> np.ndarray:
    """SOR：剔除「k 近邻平均距离」显著偏大的点。

    对每个点 ``p_i`` 取**除自身以外**最近的 ``k`` 个邻居，记其平均距离 ``d_i``；
    阈值 ``tau = mean(d) + std * std(d)``（总体标准差，ddof=0）；
    保留 ``d_i <= tau`` 的点 —— 即「邻居普遍离自己很远」的孤立点被剔除。

    Args:
        points: ``(N, 3)`` 点云，列表或 ndarray 均可。
        k: 邻居数。``k <= 0`` 视为**关闭**（除非法点外全部保留）。
        std: 标准差倍数。``std <= 0`` 视为**关闭**。``0`` 不能表示「阈值取均值」，
            那是特判为关闭的取值（与 `config.SOR_STD` 语义一致）。
        chunk_size: 分批查询的批大小（压低峰值内存）。

    Returns:
        ``(N,)`` 的 bool 数组，``True`` 表示**保留**。

    边界行为（都有单测覆盖）：
        - 输入含 NaN/Inf 的点一律判为不保留；
        - ``N <= k``（邻居不够）自动退化为「用全部其它点」，不报错；
        - 有效点数 ``< 2`` 时无法估计邻域距离，全部保留；
        - 距离一律用 **float64** 计算，避免百万点尺度下 float32 的累积误差
          影响判定结果。
    """
    pts = np.asarray(points, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[1] != 3:
        raise ValueError(f"points 需为 (N, 3)，收到 {pts.shape!r}")

    n = pts.shape[0]
    keep = np.zeros(n, dtype=bool)
    finite = np.isfinite(pts).all(axis=1)
    n_finite = int(finite.sum())

    if k <= 0 or std <= 0 or n_finite < 2:
        keep[finite] = True
        return keep

    valid = pts[finite]
    # 最近邻一定是自己（距离 0），所以查 k+1 个再丢掉第 0 列
    n_neighbors = min(int(k) + 1, n_finite)
    tree = cKDTree(valid)

    mean_dist = np.empty(n_finite, dtype=np.float64)
    step = max(1, int(chunk_size))
    for start in range(0, n_finite, step):
        stop = min(start + step, n_finite)
        dist, _ = tree.query(valid[start:stop], k=n_neighbors, workers=-1)
        mean_dist[start:stop] = dist[:, 1:].mean(axis=1)

    tau = mean_dist.mean() + float(std) * mean_dist.std()
    keep[finite] = mean_dist <= tau
    return keep
