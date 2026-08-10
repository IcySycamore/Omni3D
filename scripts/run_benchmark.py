#!/usr/bin/env python3
"""Fast3R 统一评测命令行入口。

用法：

.. code-block:: bash

    python scripts/run_benchmark.py --config configs/eval/benchmark_re10k.yaml
    python scripts/run_benchmark.py --config configs/eval/benchmark_recon.yaml
"""

import argparse
import sys
from pathlib import Path

# 允许从项目根目录导入 fast3r
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fast3r.eval.benchmark import BenchmarkRunner  # pylint: disable=wrong-import-position


def main() -> None:
    """解析命令行参数并运行评测。"""
    parser = argparse.ArgumentParser(description="Run Fast3R benchmark")
    parser.add_argument(
        "--config",
        type=str,
        required=True,
        help="Path to benchmark YAML config",
    )
    parser.add_argument(
        "--scenes",
        type=str,
        nargs="+",
        default=None,
        help="Override scene directories",
    )
    args = parser.parse_args()

    runner = BenchmarkRunner.from_config(args.config)
    if args.scenes:
        runner.scene_dirs = [Path(s) for s in args.scenes]

    _per_scene, summary = runner.run()

    print("\n===== Benchmark Summary =====")
    for key, value in summary.items():
        print(f"{key}: {value}")
    print(f"\nResults saved to: {runner.output_dir}")


if __name__ == "__main__":
    main()
