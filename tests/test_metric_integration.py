"""集成测试：用真实 Fast3R 模型验证「AR 位姿 → 米制尺度」端到端生效。

设计要点（避免 flaky）：
- **精确性**用**单次前向**的数据验证：拿本次预测的相机中心，套上已知相似变换
  造出"米制 AR 位姿"，再让 `metric_alignment` 还原 —— 同一批数据，结果稳定。
- **接线正确性**用第二次前向验证：只断言结构性事实（aligned / source / n_views）
  与宽松的尺度带。第二次前向的相机中心与第一次有微小差异（cuDNN 非确定性 +
  位姿由 PnP 估计），因此**不适合**做精确数值断言。

需要 GPU 与模型权重；缺失时自动跳过。
"""
import os

import numpy as np
import pytest
import torch  # noqa: F401  (必须早于 cv2 —— fbgemm.dll 加载顺序)

from app.core import config
from app.core.pipeline import (
    apply_similarity,
    metric_alignment,
    run_reconstruction,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIDEO = os.path.join(PROJECT_ROOT, "demo_examples", "family", "Family.mp4")
N_FRAMES = 5
RESOLUTION = 224          # 小分辨率，跑得快

S_KNOWN = 3.0
ROT_DEG = 20.0
T_KNOWN = np.array([1.0, -0.5, 2.0])


def _model_available() -> bool:
    return os.path.isdir(config.CHECKPOINT_DIR) and os.path.isfile(
        os.path.join(config.CHECKPOINT_DIR, "model.safetensors")
    )


pytestmark = pytest.mark.skipif(
    not _model_available(), reason="缺少模型权重 jedyang97/Fast3R_ViT_Large_512"
)


def _rot_z(deg: float) -> np.ndarray:
    t = np.deg2rad(deg)
    c, s = np.cos(t), np.sin(t)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def _extract_frames(video: str, n: int, out_dir: str) -> list:
    import cv2

    os.makedirs(out_dir, exist_ok=True)
    cap = cv2.VideoCapture(video)
    assert cap.isOpened(), f"无法打开视频 {video}"
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    paths = []
    for i in range(n):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i * total / n))
        ok, frame = cap.read()
        if not ok:
            continue
        p = os.path.join(out_dir, f"f{i:03d}.jpg")
        cv2.imwrite(p, frame)
        paths.append(p)
    cap.release()
    return paths


def _extrinsics_from_centers(centers: np.ndarray, s: float, rot: np.ndarray,
                             trans: np.ndarray) -> list:
    """把「模型坐标系相机中心」搬到米制世界系，产出 col-major 4×4 列表。"""
    metric = s * (centers @ rot.T) + trans
    out = []
    for c in metric:
        m = np.eye(4)
        m[:3, :3] = rot
        m[:3, 3] = c
        out.append(m.T.reshape(-1).tolist())      # 转列主序（ARCore 约定）
    return out


@pytest.fixture(scope="module")
def model():
    from fast3r.models.fast3r import Fast3R

    m = Fast3R.from_pretrained(config.CHECKPOINT_DIR).to(config.DEVICE)
    m.eval()
    return m


@pytest.fixture(scope="module")
def frames(tmp_path_factory):
    out_dir = str(tmp_path_factory.mktemp("frames"))
    paths = _extract_frames(VIDEO, N_FRAMES, out_dir)
    assert len(paths) >= 3, "抽帧失败"
    return paths


@pytest.fixture(scope="module")
def baseline(model, frames):
    """一次基准重建（不带外参），供单次前向内的精确性验证复用。"""
    out, _ = run_reconstruction(frames, model, config.DEVICE, resolution=RESOLUTION)
    centers = np.stack(
        [p["camera_center"][0].numpy().astype(np.float64) for p in out["preds"]]
    )
    return out, centers


def test_no_extrinsics_keeps_arbitrary_scale(baseline):
    """回归：不传外参时不缩放，scale 为 None（与改造前一致）。"""
    out, _ = baseline
    metric = out["metric"]
    assert metric["extrinsics_provided"] is False
    assert metric["aligned"] is False
    assert metric["scale"] is None
    # 质量代理指标应存在
    q = out["quality"]
    assert q["views"] == len(out["preds"])
    assert q["residual_ratio_median"] is not None


def test_metric_alignment_exact_on_real_preds(baseline):
    """精确性（单次前向、同一批数据）：还原已知 scale 并按 s 缩放距离。"""
    out, centers = baseline
    assert centers.shape == (len(out["preds"]), 3)

    rot = _rot_z(ROT_DEG)
    ext = _extrinsics_from_centers(centers, S_KNOWN, rot, T_KNOWN)

    fit = metric_alignment(out["preds"], ext)
    assert fit is not None
    assert fit["n_views"] == len(out["preds"])
    assert fit["scale"] == pytest.approx(S_KNOWN, rel=1e-3), fit["scale"]

    # 把点云按还原出的相似变换搬到米制，距离应严格放大 s 倍
    pts = out["preds"][0]["pts3d_in_other_view"][0].clone()
    moved = apply_similarity(pts, fit["R"], fit["T"], fit["scale"])
    d_before = float(torch.linalg.norm(pts[0] - pts[-1]))
    d_after = float(torch.linalg.norm(moved[0] - moved[-1]))
    assert d_before > 0
    assert d_after / d_before == pytest.approx(S_KNOWN, rel=1e-3), (d_before, d_after)


def test_pipeline_wiring_with_extrinsics(model, frames, baseline):
    """接线（跨前向）：带外参时管线确实对齐并回填 scale。

    跨前向有非确定性，故只用宽松带（±20%）＋结构性断言。
    """
    _, centers = baseline
    ext = _extrinsics_from_centers(centers, S_KNOWN, _rot_z(ROT_DEG), T_KNOWN)

    out2, _ = run_reconstruction(
        frames, model, config.DEVICE, resolution=RESOLUTION, extrinsics=ext
    )
    metric = out2["metric"]
    assert metric["aligned"] is True, metric
    assert metric["source"] == "extrinsics"
    assert metric["extrinsics_provided"] is True
    assert metric["n_views"] == len(frames)
    assert metric["scale"] is not None
    assert 0.8 * S_KNOWN < metric["scale"] < 1.2 * S_KNOWN, metric["scale"]

    # 点云确实被变换过：与基准（未对齐）相比形状相同但数值不同
    p0 = np.asarray(out2["preds"][0]["pts3d_in_other_view"][0])
    pn = np.asarray(out2["preds"][-1]["pts3d_in_other_view"][0])
    assert float(np.linalg.norm(p0[0] - pn[0])) > 0


def test_degenerate_extrinsics_skipped(model, frames):
    """退化外参（相机不动）应优雅跳过，重建本身仍然成功。"""
    degenerate = [[0.0] * 16] * 2
    out, _ = run_reconstruction(
        frames, model, config.DEVICE, resolution=RESOLUTION, extrinsics=degenerate
    )
    assert out["metric"]["aligned"] is False
    assert out["metric"]["scale"] is None
    assert len(out["preds"]) == len(frames)
    # 质量代理仍然产出，可用于诊断
    assert out["quality"]["views"] == len(frames)


def test_intrinsics_are_reported_not_used(model, frames):
    """内参目前只做校验/报告（写入 metric.intrinsics），不参与几何求解。"""
    k = [[500.0, 0.0, 320.0], [0.0, 500.0, 240.0], [0.0, 0.0, 1.0]]
    out, _ = run_reconstruction(
        frames, model, config.DEVICE, resolution=RESOLUTION, intrinsics=k
    )
    intr = out["metric"]["intrinsics"]
    assert intr is not None and len(intr) == 1
    assert intr[0]["fx"] == 500.0 and intr[0]["cx"] == 320.0


# ─────────────── 点云来源：local head / global head（#21） ───────────────

def test_baseline_uses_local_head_by_default(baseline):
    """默认走 local head，且两种来源的预测张量都在（切换不需要重跑模型）。"""
    out, _ = baseline
    assert out["metric"]["pts3d_source"] == "local"
    for pred in out["preds"]:
        assert "pts3d_local_aligned_to_global" in pred
        assert "conf_local" in pred
        # 两种 head 的原始输出并存，供 `OMNI3D_PTS3D_SOURCE` 切换
        assert "pts3d_in_other_view" in pred
        assert "conf" in pred


def test_local_and_global_same_shape_and_frame(baseline):
    """local head 已被对齐到全局坐标系，因此形状与 global 完全一致。"""
    out, _ = baseline
    for pred in out["preds"]:
        local = pred["pts3d_local_aligned_to_global"]
        glob = pred["pts3d_in_other_view"]
        assert local.shape == glob.shape
        assert pred["conf_local"].shape == glob.shape[:3]


def test_source_override_is_reported(model, frames, baseline):
    """显式传 ``pts3d_source="global"`` 时 metric 如实上报（回归保护）。"""
    out, _ = run_reconstruction(
        frames, model, config.DEVICE, resolution=RESOLUTION, pts3d_source="global"
    )
    assert out["metric"]["pts3d_source"] == "global"


def test_quality_stats_switches_conf_key(baseline):
    """`quality_stats` 必须跟着来源读对应的置信度图（conf_local vs conf）。"""
    from app.core.pipeline import pointcloud_keys, quality_stats

    out, _ = baseline
    for source in ("local", "global"):
        conf_key = pointcloud_keys(source)[1]
        q = quality_stats(out["preds"], source=source)
        # 两条路径都应算出与当前来源 conf 一致的视图数
        assert q["views"] == len(out["preds"])
        assert conf_key in out["preds"][0]
