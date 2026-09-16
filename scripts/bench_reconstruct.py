"""重建**速度与质量**基准（开发用）。

    python scripts/bench_reconstruct.py                 # 默认：全部 5 个示例
    python scripts/bench_reconstruct.py --frames 4 8 12 --res 224 512
    python scripts/bench_reconstruct.py --video demo_examples/family/Family.mp4

输出：逐组合的墙钟耗时、点数、以及**质量代理指标**
（`residual_ratio_median` / `local_scale_std` / `mean_conf`，定义见
`app/core/pipeline.py: quality_stats`）。最后给出 markdown 汇总表。

⚠️ 质量代理 ≠ 绝对精度：没有 GT 点云/位姿时只能衡量**自洽性与稳定性**。
绝对精度需跑 vendored 的 `fast3r/eval/`（需要 re10k / robustmvd 数据集，本机未附）。
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

# torch 必须最先导入（本机 fbgemm.dll 加载顺序冲突）
import torch  # noqa: F401

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2  # noqa: E402

from app.core import config  # noqa: E402
from app.core.pipeline import run_reconstruction  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_ROOT = os.path.join(PROJECT_ROOT, "demo_examples")
OUT_DIR = os.path.join(PROJECT_ROOT, "temp_preview_frames", "bench")


def find_videos() -> list:
    out = []
    for sub in sorted(os.listdir(DEMO_ROOT)):
        d = os.path.join(DEMO_ROOT, sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.lower().endswith((".mp4", ".mov")):
                out.append((sub, os.path.join(d, name)))
    return out


def extract_frames(video: str, n: int, tag: str) -> list:
    out_dir = os.path.join(OUT_DIR, tag)
    os.makedirs(out_dir, exist_ok=True)
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video}")
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Omni3D 重建速度/质量基准")
    ap.add_argument("--frames", type=int, nargs="+", default=[4, 8, 12])
    ap.add_argument("--res", type=int, nargs="+", default=[224, 512])
    ap.add_argument("--video", type=str, default="", help="只测单个视频")
    ap.add_argument("--repeat", type=int, default=1, help="每组合重复次数（取中位数）")
    ap.add_argument("--warmup", type=int, default=1, help="预热次数（不计入统计）")
    args = ap.parse_args()

    if not os.path.isfile(os.path.join(config.CHECKPOINT_DIR, "model.safetensors")):
        print(f"✗ 缺少模型权重: {config.CHECKPOINT_DIR}")
        return 2

    from fast3r.models.fast3r import Fast3R

    print(f"加载模型 … ({config.CHECKPOINT_DIR})")
    t0 = time.time()
    model = Fast3R.from_pretrained(config.CHECKPOINT_DIR).to(config.DEVICE)
    model.eval()
    print(f"模型就绪: {config.DEVICE}，耗时 {time.time() - t0:.1f}s\n")

    videos = ([("custom", args.video)] if args.video else find_videos())
    rows = []

    for tag, video in videos:
        for n in args.frames:
            frames = extract_frames(video, n, f"{tag}_{n}")
            if len(frames) < 2:
                print(f"x {tag}: 抽帧不足，跳过")
                continue
            for res in args.res:
                times, points, quals = [], [], []
                for run in range(args.warmup + args.repeat):
                    t = time.time()
                    out, profiling = run_reconstruction(
                        frames, model, config.DEVICE, resolution=res
                    )
                    dt = time.time() - t
                    count = sum(int(p["pts3d_in_other_view"].numel() // 3)
                                for p in out["preds"])
                    if run >= args.warmup:
                        times.append(dt)
                        points.append(count)
                        quals.append(out.get("quality") or {})
                if not times:
                    continue
                med = statistics.median(times)
                q = quals[-1]
                rows.append({
                    "demo": tag,
                    "frames": len(frames),
                    "res": res,
                    "wall_s": med,
                    "s_per_view": med / len(frames),
                    "points": int(statistics.median(points)),
                    "resid": q.get("residual_ratio_median"),
                    "scale_std": q.get("local_scale_std"),
                    "conf": q.get("mean_conf"),
                })
                print(f"  {tag:<12} frames={len(frames):>2} res={res:>3}  "
                      f"{med:5.2f}s  {med / len(frames):4.2f}s/帧  "
                      f"points={rows[-1]['points']:>7}  "
                      f"resid={_fmt(q.get('residual_ratio_median'))}  "
                      f"conf={_fmt(q.get('mean_conf'))}")

    # ---- markdown 汇总 ----
    print("\n### 速度 / 质量基准（本机）\n")
    print(f"device = `{config.DEVICE}`，模型 = Fast3R ViT-Large 512，"
          f"每次取 {args.repeat} 次中位数（warmup {args.warmup}）\n")
    print("| 示例 | 帧数 | 分辨率 | 总耗时(s) | 每帧(s) | 点数 | 残差比↓ | 尺度离散↓ | 置信度 |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        print(f"| {r['demo']} | {r['frames']} | {r['res']} | {r['wall_s']:.2f} | "
              f"{r['s_per_view']:.2f} | {r['points']} | {_fmt(r['resid'])} | "
              f"{_fmt(r['scale_std'])} | {_fmt(r['conf'])} |")

    # ---- 汇总统计 ----
    if rows:
        by_res = {}
        for r in rows:
            by_res.setdefault(r["res"], []).append(r["s_per_view"])
        print("\n每帧耗时（按分辨率）：")
        for res, v in sorted(by_res.items()):
            print(f"- {res}px: 中位 {statistics.median(v):.2f}s/帧 "
                  f"(n={len(v)})")
        resid = [r["resid"] for r in rows if r["resid"] is not None]
        if resid:
            print(f"\n残差比（越小越自洽）：中位 {statistics.median(resid):.4f}，"
                  f"范围 {min(resid):.4f}~{max(resid):.4f}")
    return 0


def _fmt(v) -> str:
    return "-" if v is None else f"{v:.4g}"


if __name__ == "__main__":
    sys.exit(main())
