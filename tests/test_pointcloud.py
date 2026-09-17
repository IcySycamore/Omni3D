"""`app/core/pointcloud.py` 的 SOR 单元测试。

不需要 GPU / 模型：全部用合成点云构造。`k`/`std` 的边界行为、非有限值、
分块与单次结果一致性都在这里守住。
"""
import numpy as np
import pytest

# torch 必须最先导入（本机 fbgemm.dll 加载顺序冲突）
import torch  # noqa: F401,I001

from app.core.pointcloud import statistical_outlier_removal


def _lattice_with_flyers(n=6, spacing=1.0, n_flyers=5, seed=0):
    """规则点阵（内点）+ 远处随机飞点，返回 ``(points, is_inlier)``。"""
    axis = np.arange(n) * spacing
    gx, gy, gz = np.meshgrid(axis, axis, axis, indexing="ij")
    inliers = np.stack([gx.ravel(), gy.ravel(), gz.ravel()], axis=1).astype(np.float64)
    if n_flyers:
        rng = np.random.default_rng(seed)
        flyers = rng.uniform(50.0, 60.0, size=(n_flyers, 3))
        points = np.vstack([inliers, flyers])
    else:
        points = inliers
    is_inlier = np.array([True] * len(inliers) + [False] * n_flyers, dtype=bool)
    return points, is_inlier


class TestStatisticalOutlierRemoval:
    def test_removes_flyers_and_keeps_inliers(self):
        """核心行为：远离主体点云的飞点被剔除，内点保留。"""
        points, is_inlier = _lattice_with_flyers()
        keep = statistical_outlier_removal(points, k=8, std=2.0)
        assert keep.shape == (len(points),)
        # 飞点一个不留
        assert not keep[~is_inlier].any()
        # 内点几乎全留（点阵边缘邻域略稀疏，容忍极少误删）
        assert keep[is_inlier].sum() >= 0.99 * int(is_inlier.sum())

    def test_clean_cloud_is_not_gutted(self):
        """没有飞点时不应把点云削掉一大块（SOR 不是抽稀）。"""
        points, _ = _lattice_with_flyers(n_flyers=0)
        keep = statistical_outlier_removal(points, k=8, std=2.0)
        assert keep.sum() >= 0.9 * len(points)

    @pytest.mark.parametrize("std", [0.0, -1.0])
    def test_std_zero_disables(self, std):
        points, _ = _lattice_with_flyers()
        assert statistical_outlier_removal(points, k=8, std=std).all()

    @pytest.mark.parametrize("k", [0, -3])
    def test_k_zero_disables(self, k):
        points, _ = _lattice_with_flyers()
        assert statistical_outlier_removal(points, k=k, std=2.0).all()

    def test_k_larger_than_point_count_does_not_crash(self):
        """邻居数超过点数时退化为「用全部其它点」，不报错。"""
        points = np.random.default_rng(0).normal(size=(5, 3))
        keep = statistical_outlier_removal(points, k=50, std=2.0)
        assert keep.shape == (5,)
        assert keep.any()

    def test_non_finite_points_are_dropped(self):
        points, _ = _lattice_with_flyers()
        points = np.vstack([points, [[np.nan, 0.0, 0.0], [np.inf, 1.0, 2.0]]])
        keep = statistical_outlier_removal(points, k=8, std=2.0)
        assert not keep[-2] and not keep[-1]

    def test_single_point_is_kept(self):
        assert statistical_outlier_removal(np.zeros((1, 3)), k=8, std=2.0).tolist() == [True]

    def test_empty_input(self):
        keep = statistical_outlier_removal(np.zeros((0, 3)), k=8, std=2.0)
        assert keep.shape == (0,)

    def test_bad_shape_raises(self):
        with pytest.raises(ValueError):
            statistical_outlier_removal(np.zeros((10, 2)))

    def test_chunking_matches_single_pass(self):
        """分块查询只是为了省内存，结果必须与一次性查询完全一致。"""
        points, _ = _lattice_with_flyers()
        once = statistical_outlier_removal(points, k=8, std=1.0, chunk_size=10_000)
        chunked = statistical_outlier_removal(points, k=8, std=1.0, chunk_size=7)
        assert np.array_equal(once, chunked)

    def test_accepts_list_input(self):
        """调用方可能传 list（JSON 反序列化后），不应出错。"""
        points, _ = _lattice_with_flyers()
        keep = statistical_outlier_removal(points.tolist(), k=8, std=2.0)
        assert keep.shape == (len(points),)
