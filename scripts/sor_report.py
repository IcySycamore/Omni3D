"""在真实视频上量 SOR 的剔除比例与耗时（#22）。

刻意直接调用 `web/server.py` 的**真实代码路径**（`_collect_points` →
`_reject_outliers` → `_sample_for_render`），而不是复刻一份逻辑，
这样 `docs/PERFORMANCE.md` 里记录的数字与线上行为一致。

用法：python scripts\\sor_report.py [视频路径] [帧数] [分辨率]
"""
import os
import sys
import time

import torch  # noqa: F401  必须最先导入（fbgemm.dll 加载顺序）

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "web"))

from app.core import config  # noqa: E402
from app.core.pipeline import run_reconstruction  # noqa: E402
import server as web_server  # noqa: E402

DEFAULT_VIDEO = os.path.join(PROJECT_ROOT, "demo_examples", "family", "Family.mp4")


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
        path = os.path.join(out_dir, f"sor{i:03d}.jpg")
        cv2.imwrite(path, frame)
        paths.append(path)
    cap.release()
    return paths


def _sor_stats(points):
    """用 config 参数跑一次 SOR，返回 (保留数, 剔除数, 剔除比例%, 耗时秒)。"""
    return _sor_stats_k(points, config.SOR_K, config.SOR_STD)


def _sor_stats_k(points, k, std):
    """用指定 k/std 跑一次 SOR，返回 (保留数, 剔除数, 剔除比例%, 耗时秒)。"""
    t0 = time.time()
    keep = web_server.statistical_outlier_removal(points, k=k, std=std)
    dt = time.time() - t0
    removed = int(points.shape[0] - int(keep.sum()))
    ratio = removed / max(1, points.shape[0]) * 100.0
    return int(keep.sum()), removed, ratio, dt


def main():
    video = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_VIDEO
    n_frames = int(sys.argv[2]) if len(sys.argv) > 2 else 16
    resolution = int(sys.argv[3]) if len(sys.argv) > 3 else 512

    from fast3r.models.fast3r import Fast3R

    frames = _extract_frames(
        video, n_frames, os.path.join(PROJECT_ROOT, "temp_preview_frames", "sor")
    )
    print(f"视频: {os.path.basename(video)}  帧数: {len(frames)}  分辨率: {resolution}")
    print(f"config.SOR_K={config.SOR_K}  config.SOR_STD={config.SOR_STD}")

    model = Fast3R.from_pretrained(config.CHECKPOINT_DIR).to(config.DEVICE)
    model.eval()
    out, _ = run_reconstruction(
        frames, model, config.DEVICE, resolution=resolution, pts3d_source="local"
    )

    points, colors = web_server._collect_points(out)
    raw = int(points.shape[0])
    print(f"\n置信度过滤后（SOR 前）: {raw:,} 点  colors={'有' if colors is not None else '无'}")

    kept, removed, ratio, dt = _sor_stats(points)
    print(f"\n=== SOR（k={config.SOR_K}, std={config.SOR_STD}）===")
    print(f"  保留        : {kept:,}")
    print(f"  剔除        : {removed:,}")
    print(f"  剔除比例    : {ratio:.3f} %")
    print(f"  纯算法耗时  : {dt:.2f} s  ({dt / max(1, raw) * 1e6:.2f} µs/点)")

    # 关闭 SOR 的语义自检（k=0 → 全部保留），并确认剔除后抽样仍能填满渲染上限
    keep_all = web_server.statistical_outlier_removal(points, k=0, std=config.SOR_STD)
    assert keep_all.all(), "k=0 应为关闭（全部保留）"

    keep = web_server.statistical_outlier_removal(
        points, k=config.SOR_K, std=config.SOR_STD
    )
    cleaned_cols = None if colors is None else colors[keep]
    render_pts, _ = web_server._sample_for_render(points[keep], cleaned_cols)
    print(f"  抽样后渲染点数: {len(render_pts):,}（上限 {config.MAX_RENDER_POINTS:,}）")

    # PLY 体积（#24：坐标从 float32 升到 float64 后每点 27B，旧为 15B）
    ply = web_server._pts_to_ply(points[keep], cleaned_cols)
    n_ply = int(points[keep].shape[0])
    print(f"  PLY 体积    : {len(ply):,} 字节 ({len(ply) / 1024 / 1024:.1f} MiB)")
    if n_ply:
        print(f"  PLY 每点    : {len(ply) / n_ply:.1f} 字节/点（float32 时为 15）")

    # 参数敏感性：std 越小剔得越多
    print("\n=== 参数敏感性（同一份点云）===")
    print(f"{'k':>4} {'std':>6} {'剔除比例 %':>12} {'耗时 s':>9}")
    for k, std in [(8, 1.0), (8, 2.0), (8, 3.0), (16, 2.0), (4, 2.0)]:
        _kept, _rm, r, t = _sor_stats_k(points, k, std)
        print(f"{k:>4} {std:>6.1f} {r:>12.3f} {t:>9.2f}")


if __name__ == "__main__":
    main()
