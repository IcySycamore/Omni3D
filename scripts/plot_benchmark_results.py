#!/usr/bin/env python3
"""Fast3R benchmark 结果可视化脚本。

用法：

.. code-block:: bash

    python scripts/plot_benchmark_results.py \
        --json outputs/benchmark/re10k_pose_mock/re10k_pose_mock_per_scene.json \
        --out_dir docs/evaluation/figures
"""

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def plot_cdf(errors, label, ax):
    """绘制累积分布曲线。"""
    sorted_errors = np.sort(errors)
    y = np.arange(1, len(sorted_errors) + 1) / len(sorted_errors)
    ax.plot(sorted_errors, y, label=label)
    ax.set_xlabel("Error")
    ax.set_ylabel("CDF")
    ax.grid(True, linestyle="--", alpha=0.5)
    ax.legend()


def plot_bar(values, labels, title, ylabel, out_path):
    """绘制柱状图。"""
    fig, ax = plt.subplots(figsize=(8, 5))
    x = np.arange(len(labels))
    ax.bar(x, values)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description="Plot benchmark results")
    parser.add_argument("--json", type=str, required=True, help="Per-scene JSON result")
    parser.add_argument("--out_dir", type=str, required=True, help="Output figures dir")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(args.json, "r", encoding="utf-8") as f:
        data = json.load(f)

    prefix = Path(args.json).stem.replace("_per_scene", "")

    # 1) CDF of scene-level mean errors (pose)
    if "rel_rotation_mean_deg" in data[0]:
        fig, axes = plt.subplots(1, 2, figsize=(12, 5))
        rot_errors = [s["rel_rotation_mean_deg"] for s in data]
        trans_errors = [s["rel_translation_mean_deg"] for s in data]
        plot_cdf(rot_errors, "Rotation error (deg)", axes[0])
        plot_cdf(trans_errors, "Translation error (deg)", axes[1])
        fig.suptitle(f"{prefix}: CDF of scene-level mean errors")
        fig.tight_layout()
        fig.savefig(out_dir / f"{prefix}_cdf_pose.png", dpi=150)
        plt.close(fig)

        # Bar chart of ATE and AUC
        scenes = [s["scene"] for s in data]
        ate = [s["ate"] for s in data]
        auc = [s["auc"] for s in data]
        plot_bar(ate, scenes, f"{prefix}: ATE per scene", "ATE", out_dir / f"{prefix}_ate.png")
        plot_bar(auc, scenes, f"{prefix}: AUC per scene", "AUC", out_dir / f"{prefix}_auc.png")

    # 2) Reconstruction metrics
    if "accuracy_mean" in data[0]:
        fig, ax = plt.subplots(figsize=(8, 5))
        scenes = [s["scene"] for s in data]
        acc = [s["accuracy_mean"] for s in data]
        comp = [s["completion_mean"] for s in data]
        x = np.arange(len(scenes))
        width = 0.35
        ax.bar(x - width / 2, acc, width, label="Accuracy")
        ax.bar(x + width / 2, comp, width, label="Completion")
        ax.set_xticks(x)
        ax.set_xticklabels(scenes, rotation=45, ha="right")
        ax.set_ylabel("Distance")
        ax.set_title(f"{prefix}: Accuracy vs Completion")
        ax.legend()
        ax.grid(True, axis="y", linestyle="--", alpha=0.5)
        fig.tight_layout()
        fig.savefig(out_dir / f"{prefix}_recon.png", dpi=150)
        plt.close(fig)

    print(f"Figures saved to: {out_dir}")


if __name__ == "__main__":
    main()
