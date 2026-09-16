"""`app/core/pipeline.py` 中「真实尺度对齐」相关纯函数的单元测试。

不需要 GPU / 模型：全部用合成数据构造。
"""
import numpy as np
import pytest

# torch 必须最先导入（本机 fbgemm.dll 加载顺序冲突）
import torch  # noqa: I001

from app.core.pipeline import (
    apply_similarity,
    extrinsics_to_cam2world,
    metric_alignment,
    pointcloud_keys,
    resolve_pts3d_source,
    summarize_intrinsics,
)

from app.core import config


# ─────────────── 工具 ───────────────

def _rot_z(deg: float) -> np.ndarray:
    t = np.deg2rad(deg)
    c, s = np.cos(t), np.sin(t)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def _make_case(n=5, scale=2.5, rot_deg=37.0, t=(0.3, -0.2, 0.9)):
    """构造一组 GT 米制相机中心，及其在模型坐标系中的对应观测。"""
    rng = np.random.default_rng(0)
    c_gt = rng.normal(size=(n, 3)).cumsum(axis=0)     # 非退化轨迹（米）
    r_gt = _rot_z(rot_deg)
    t_gt = np.asarray(t, dtype=float)
    # 逆相似变换：c_pred = R^T (c_gt - T) / s
    c_pred = ((c_gt - t_gt) @ r_gt) / scale
    return c_gt, c_pred, r_gt, t_gt, float(scale)


def _poses_colmajor(centers, rot=None):
    """把相机中心包成 col-major 4×4 cam2world 扁平数组（ARCore 约定）。"""
    out = []
    for c in centers:
        m = np.eye(4)
        if rot is not None:
            m[:3, :3] = rot
        m[:3, 3] = c
        out.append(m.T.reshape(-1).tolist())          # 转列主序
    return out


# ─────────────── extrinsics_to_cam2world ───────────────

def test_colmajor_conversion():
    m = np.arange(16, dtype=float).reshape(4, 4)
    got = extrinsics_to_cam2world([m.T.reshape(-1).tolist()])
    assert got.shape == (1, 4, 4)
    assert np.allclose(got[0].numpy(), m)


def test_accepts_nested_4x4():
    m = np.eye(4)
    m[:3, 3] = [1.0, 2.0, 3.0]
    got = extrinsics_to_cam2world([m.tolist()])
    assert np.allclose(got[0].numpy(), m)


def test_empty_extrinsics():
    assert extrinsics_to_cam2world(None).shape == (0, 4, 4)
    assert extrinsics_to_cam2world([]).shape == (0, 4, 4)


def test_bad_extrinsics_shape():
    with pytest.raises(ValueError):
        extrinsics_to_cam2world([[1.0, 2.0, 3.0]])


# ─────────────── metric_alignment ───────────────

def test_recovers_scale_rotation_translation():
    c_gt, c_pred, r_gt, t_gt, s_gt = _make_case()
    preds = [{"camera_center": torch.tensor(c)} for c in c_pred]
    fit = metric_alignment(preds, _poses_colmajor(c_gt, r_gt))
    assert fit is not None
    assert fit["scale"] == pytest.approx(s_gt, rel=1e-6)
    assert fit["n_views"] == len(c_gt)

    # 把预测中心经恢复的相似变换映射回米制，应还原出 GT 中心
    mapped = (fit["scale"] * (torch.tensor(c_pred, dtype=torch.float64) @ fit["R"].T)
              + fit["T"])
    assert np.allclose(mapped.numpy(), c_gt, atol=1e-6)


def test_rotation_only_centers_is_enough():
    """中心点非共线时，仅用中心也能唯一确定旋转。"""
    c_gt = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    r_gt = _rot_z(25.0)
    t_gt = np.array([0.5, 0.5, 0.0])
    s_gt = 3.0
    c_pred = ((c_gt - t_gt) @ r_gt) / s_gt
    preds = [{"camera_center": torch.tensor(c)} for c in c_pred]
    fit = metric_alignment(preds, _poses_colmajor(c_gt, r_gt))
    assert fit is not None
    assert fit["scale"] == pytest.approx(s_gt, rel=1e-6)


def test_no_extrinsics_returns_none():
    preds = [{"camera_center": torch.zeros(3)} for _ in range(3)]
    assert metric_alignment(preds, None) is None
    assert metric_alignment(preds, []) is None


def test_single_view_returns_none():
    preds = [{"camera_center": torch.zeros(3)}]
    assert metric_alignment(preds, _poses_colmajor([[0.0, 0.0, 0.0]])) is None


def test_static_trajectory_returns_none():
    """相机几乎不动 → 无法确定尺度，应拒绝而不是给出垃圾值。"""
    c = np.array([[1.0, 2.0, 3.0]] * 4)
    preds = [{"camera_center": torch.zeros(3)} for _ in range(4)]
    assert metric_alignment(preds, _poses_colmajor(c)) is None


def test_missing_camera_center_returns_none():
    preds = [{"camera_center": torch.zeros(3)}, {}]
    assert metric_alignment(preds, _poses_colmajor([[0, 0, 0], [1, 1, 1]])) is None


def test_absurd_scale_rejected():
    """预测中心极小 / 米制轨迹很长 → 尺度离谱，应拒绝。"""
    c_gt = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [0.0, 10.0, 0.0]])
    c_pred = c_gt * 1e-12
    preds = [{"camera_center": torch.tensor(c)} for c in c_pred]
    assert metric_alignment(preds, _poses_colmajor(c_gt)) is None


def test_nonfinite_returns_none():
    c_gt = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    preds = [
        {"camera_center": torch.tensor([0.0, 0.0, 0.0])},
        {"camera_center": torch.tensor([float("nan"), 0.0, 0.0])},
        {"camera_center": torch.tensor([0.0, 1.0, 0.0])},
    ]
    assert metric_alignment(preds, _poses_colmajor(c_gt)) is None


def test_fewer_extrinsics_than_views_uses_prefix():
    c_gt, c_pred, r_gt, _, s_gt = _make_case(n=5)
    preds = [{"camera_center": torch.tensor(c)} for c in c_pred]
    fit = metric_alignment(preds, _poses_colmajor(c_gt[:3], r_gt))
    assert fit is not None
    assert fit["n_views"] == 3
    assert fit["scale"] == pytest.approx(s_gt, rel=1e-6)


# ─────────────── apply_similarity ───────────────

def test_apply_similarity_roundtrip():
    c_gt, c_pred, r_gt, t_gt, s_gt = _make_case()
    pts = torch.tensor(c_pred, dtype=torch.float64)
    out = apply_similarity(pts, torch.tensor(r_gt), torch.tensor(t_gt), s_gt)
    assert np.allclose(out.numpy(), c_gt, atol=1e-9)


def test_apply_similarity_returns_float64():
    """#24：内部以 float64 计算，就不该再回写 float32（那是无谓的二次舍入）。

    注意这条**反转**了旧行为（旧代码显式 `.to(dtype=points.dtype)`）。
    """
    pts = torch.zeros((2, 3), dtype=torch.float32)
    out = apply_similarity(pts, torch.eye(3), torch.zeros(3), 2.0)
    assert out.dtype == torch.float64


def test_apply_similarity_scales_distance():
    pts = torch.tensor([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]], dtype=torch.float64)
    out = apply_similarity(pts, torch.eye(3), torch.zeros(3), 4.0)
    assert float((out[1] - out[0]).norm()) == pytest.approx(4.0)


# ─────────────── summarize_intrinsics ───────────────

def test_summarize_intrinsics_list():
    k = [[500.0, 0.0, 320.0], [0.0, 500.0, 240.0], [0.0, 0.0, 1.0]]
    got = summarize_intrinsics([k, k])
    assert len(got) == 2
    assert got[0] == {"fx": 500.0, "fy": 500.0, "cx": 320.0, "cy": 240.0}


def test_summarize_intrinsics_single_matrix():
    k = np.array([[500.0, 0.0, 320.0], [0.0, 500.0, 240.0], [0.0, 0.0, 1.0]])
    got = summarize_intrinsics(k)
    assert len(got) == 1 and got[0]["fx"] == 500.0


def test_summarize_intrinsics_invalid():
    assert summarize_intrinsics(None) is None
    assert summarize_intrinsics([]) is None
    assert summarize_intrinsics([[1.0, 2.0, 3.0]]) is None


# ─────────────── 点云来源：local head / global head（#21） ───────────────

class TestPts3dSource:
    """上游重建评测用的是 **local head**，这里把「选哪个 head」的契约钉住。"""

    def test_repo_default_is_local(self):
        """回归保护：仓库默认为 local，与上游 `eval_use_pts3d_from_local_head` 一致。"""
        assert config.PTS3D_SOURCE == "local"
        assert resolve_pts3d_source(None) == "local"

    def test_local_keys(self):
        assert resolve_pts3d_source("local") == "local"
        assert pointcloud_keys("local") == (
            "pts3d_local_aligned_to_global",
            "conf_local",
        )

    def test_global_keys(self):
        assert resolve_pts3d_source("global") == "global"
        assert pointcloud_keys("global") == ("pts3d_in_other_view", "conf")

    def test_case_and_whitespace_insensitive(self):
        assert resolve_pts3d_source("  GLOBAL ") == "global"
        assert resolve_pts3d_source(" LOCAL\t") == "local"

    @pytest.mark.parametrize("bad", ["", "   ", "xyz", "loc", "globalhead"])
    def test_invalid_falls_back_to_local(self, bad):
        assert resolve_pts3d_source(bad) == "local"
        assert pointcloud_keys(bad)[1] == "conf_local"
