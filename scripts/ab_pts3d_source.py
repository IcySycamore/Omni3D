"""临时脚本：同一视频 A/B 对比 local head 与 global head 的点云输出（#21）。

一次前向即产出两种 head 的输出，因此两档切换零成本（无需重跑模型）。
无外参 → 无米制尺度，几何差异用「场景对角线归一化」表示（无量纲）。
"""
import torch  # 必须最先导入（fbgemm.dll 加载顺序），且早于 numpy
import os
import sys
import time

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app.core import config                      # noqa: E402
from app.core.pipeline import (                  # noqa: E402
    pointcloud_keys,
    quality_stats,
    run_reconstruction,
)

VIDEO = os.path.join(PROJECT_ROOT, "demo_examples", "family", "Family.mp4")
N_FRAMES = 16
RESOLUTION = 512


def _extract_frames(video, n, out_dir):
    import cv2

    os.makedirs(out_dir, exist_ok=True)
    cap = cv2.VideoCapture(video)
    assert cap.isOpened(), video
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    paths = []
    for i in range(n):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i * total / n))
        ok, frame = cap.read()
        assert ok, i
        path = os.path.join(out_dir, f"f{i:03d}.jpg")
        cv2.imwrite(path, frame)
        paths.append(path)
    cap.release()
    return paths


def _clouds(out, source):
    """复刻 web/server.py::_collect_points 的过滤语义，返回每视图 (pts, conf_mean)。"""
    pts_key, conf_key = pointcloud_keys(source)
    per_view = []
    for pred in out["preds"]:
        p = pred[pts_key]
        p = (p.detach().cpu().numpy() if hasattr(p, "detach") else np.asarray(p))
        p = np.asarray(p, dtype=np.float32)
        if p.ndim == 4:
            p = p[0]
        p = p.reshape(-1, 3)
        keep = np.isfinite(p).all(axis=1)

        c = pred.get(conf_key)
        if c is not None and config.VIS_CONF_PERCENTILE > 0:
            c = c.detach().cpu().numpy() if hasattr(c, "detach") else np.asarray(c)
            c = np.asarray(c, dtype=np.float32)
            if c.ndim == 3:
                c = c[0]
            c = c.reshape(-1)
            if c.shape[0] == keep.shape[0] and keep.any():
                keep &= c >= np.percentile(c[keep], config.VIS_CONF_PERCENTILE)
        raw = int(keep.size)
        sel = p[keep]
        per_view.append((sel, float(np.asarray(c)[keep].mean()), raw))
    return per_view


def _median_nn(a, b, sample=20000, seed=0):
    """a 中每个点到 b 的最近距离的中位数（归一化前，单位同点云）。"""
    from scipy.spatial import cKDTree

    rng = np.random.default_rng(seed)
    if a.shape[0] > sample:
        a = a[rng.choice(a.shape[0], sample, replace=False)]
    if b.shape[0] > sample:
        b = b[rng.choice(b.shape[0], sample, replace=False)]
    d, _ = cKDTree(b).query(a, k=1)
    return float(np.median(d))


def main():
    from fast3r.models.fast3r import Fast3R

    frames = _extract_frames(
        VIDEO, N_FRAMES, os.path.join(PROJECT_ROOT, "temp_preview_frames", "ab")
    )
    print(f"视频: {os.path.basename(VIDEO)}  帧数: {len(frames)}  分辨率: {RESOLUTION}")

    t0 = time.time()
    model = Fast3R.from_pretrained(config.CHECKPOINT_DIR).to(config.DEVICE)
    model.eval()
    print(f"模型加载: {time.time() - t0:.1f}s")

    t1 = time.time()
    out, prof = run_reconstruction(
        frames, model, config.DEVICE, resolution=RESOLUTION, pts3d_source="local"
    )
    print(f"单次前向重建: {time.time() - t1:.1f}s   metric.pts3d_source={out['metric']['pts3d_source']}")

    assets = {}
    for source in ("local", "global"):
        per_view = _clouds(out, source)
        allpts = np.concatenate([v[0] for v in per_view], axis=0)
        raw_total = sum(v[2] for v in per_view)
        counts = [v[0].shape[0] for v in per_view]
        confs = [v[1] for v in per_view]
        q = quality_stats(out["preds"], source=source)
        assets[source] = allpts
        print(f"\n=== source={source} ===")
        print(f"  pts_key/conf_key        : {pointcloud_keys(source)}")
        print(f"  过滤前总点数            : {raw_total:,}")
        print(f"  过滤后总点数            : {allpts.shape[0]:,}")
        print(f"  单视图点数 min/median/max: {min(counts):,} / {int(np.median(counts)):,} / {max(counts):,}")
        print(f"  过滤后均值置信度        : {np.mean(confs):.4f}")
        print(f"  质量代理 residual_ratio : {q['residual_ratio_median']:.6f}")
        print(f"  质量代理 local_scale    : mean={q['local_scale_mean']:.6f} std={q['local_scale_std']:.6f}")
        print(f"  渲染点数(上限)          : {min(allpts.shape[0], config.MAX_RENDER_POINTS):,}")

    lo, gl = assets["local"], assets["global"]
    diag = float(np.linalg.norm(
        np.concatenate([lo, gl], axis=0).max(axis=0) - np.concatenate([lo, gl], axis=0).min(axis=0)
    ))
    d_l2g = _median_nn(lo, gl)
    d_g2l = _median_nn(gl, lo)
    print("\n=== 两种 head 的几何一致性（无量纲，已按场景对角线归一化）===")
    print(f"  场景对角线              : {diag:.4f}")
    print(f"  中位最近邻 local→global : {d_l2g:.6f}  ({d_l2g / diag * 100:.3f} % 对角线)")
    print(f"  中位最近邻 global→local : {d_g2l:.6f}  ({d_g2l / diag * 100:.3f} % 对角线)")
    print(f"  对称中位数(Chamfer)     : {(d_l2g + d_g2l) / 2 / diag * 100:.3f} % 对角线")


if __name__ == "__main__":
    main()
