"""Fast3R 统一量化评测执行器。

支持在以下两种模式下运行：

1. **mock 模式**：不依赖 PyTorch，用随机/模拟数据验证评测指标与流程。
2. **model 模式**：加载 Fast3R 模型进行真实推理（需要 torch/hydra 等依赖）。

典型用法：

.. code-block:: python

    from fast3r.eval.benchmark import BenchmarkRunner
    runner = BenchmarkRunner.from_config("configs/eval/benchmark_re10k.yaml")
    runner.run()
"""

import json
import csv
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from fast3r.eval.pose_metric_np import (
    camera_to_rel_deg,
    calculate_auc_np,
    compute_ate,
    compute_rpe,
)
from fast3r.eval.recon_metric import (
    accuracy,
    completion,
    completion_ratio,
)


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


class InferenceBackend(ABC):
    """推理后端抽象基类。"""

    @abstractmethod
    def infer_poses(self, scene_dir: Path) -> dict[str, Any]:
        """推理某个场景的相机位姿。

        Returns:
            dict: 至少包含 ``pred_c2w`` 和 ``gt_c2w`` 两个 ndarray。
        """
        raise NotImplementedError

    @abstractmethod
    def infer_reconstruction(self, scene_dir: Path) -> dict[str, Any]:
        """推理某个场景的三维点云。

        Returns:
            dict: 至少包含 ``pred_points`` 和 ``gt_points`` 两个 ndarray。
        """
        raise NotImplementedError


class MockInferenceBackend(InferenceBackend):
    """模拟推理后端，用于在无 PyTorch 环境下验证评测流程。"""

    def __init__(self, num_views: int = 10, num_points: int = 1000, seed: int = 0):
        self.num_views = num_views
        self.num_points = num_points
        self.rng = np.random.default_rng(seed)

    def _generate_c2w(self, n: int, noise_scale: float = 0.01) -> np.ndarray:
        c2w = np.tile(np.eye(4, dtype=np.float32), (n, 1, 1))
        for i in range(n):
            angle = 0.05 * i
            c2w[i, :3, :3] = np.array(
                [
                    [np.cos(angle), -np.sin(angle), 0],
                    [np.sin(angle), np.cos(angle), 0],
                    [0, 0, 1],
                ],
                dtype=np.float32,
            )
            c2w[i, :3, 3] = [i * 0.1, 0, 0]
        c2w[:, :3, 3] += self.rng.normal(0, noise_scale, (n, 3)).astype(np.float32)
        return c2w

    def infer_poses(self, scene_dir: Path) -> dict[str, Any]:
        gt_c2w = self._generate_c2w(self.num_views, noise_scale=0.0)
        pred_c2w = self._generate_c2w(self.num_views, noise_scale=0.01)
        return {"pred_c2w": pred_c2w, "gt_c2w": gt_c2w}

    def infer_reconstruction(self, scene_dir: Path) -> dict[str, Any]:
        gt_points = self.rng.standard_normal((self.num_points, 3)).astype(np.float32)
        pred_points = gt_points + self.rng.normal(0, 0.02, (self.num_points, 3)).astype(np.float32)
        return {"pred_points": pred_points, "gt_points": gt_points}


class PoseEvaluator:
    """相机位姿评测器。"""

    def __init__(self, max_threshold: int = 30):
        self.max_threshold = max_threshold

    def evaluate(self, pred_c2w: np.ndarray, gt_c2w: np.ndarray) -> dict[str, float]:
        r_err, t_err = camera_to_rel_deg(pred_c2w, gt_c2w)
        auc = calculate_auc_np(r_err, t_err, max_threshold=self.max_threshold)
        ate = compute_ate(pred_c2w, gt_c2w)
        rpe = compute_rpe(pred_c2w, gt_c2w)
        return {
            "rel_rotation_mean_deg": float(r_err.mean()),
            "rel_rotation_median_deg": float(np.median(r_err)),
            "rel_translation_mean_deg": float(t_err.mean()),
            "rel_translation_median_deg": float(np.median(t_err)),
            "auc": auc,
            "ate": ate,
            "rpe_rotation_rmse_deg": rpe["rotation_rmse"],
            "rpe_translation_rmse": rpe["translation_rmse"],
        }


class ReconEvaluator:
    """三维重建评测器。"""

    def __init__(self, dist_th: float = 0.05):
        self.dist_th = dist_th

    def evaluate(self, pred_points: np.ndarray, gt_points: np.ndarray) -> dict[str, float]:
        acc, acc_med = accuracy(gt_points, pred_points)
        comp, comp_med = completion(gt_points, pred_points)
        comp_ratio = completion_ratio(gt_points, pred_points, dist_th=self.dist_th)
        return {
            "accuracy_mean": float(acc),
            "accuracy_median": float(acc_med),
            "completion_mean": float(comp),
            "completion_median": float(comp_med),
            "completion_ratio": float(comp_ratio),
        }


class BenchmarkRunner:
    """统一评测执行器。"""

    def __init__(
        self,
        name: str,
        backend: InferenceBackend,
        scene_dirs: list[Path],
        task: str,
        output_dir: Path,
        evaluator_kwargs: dict[str, Any] | None = None,
    ):
        self.name = name
        self.backend = backend
        self.scene_dirs = scene_dirs
        self.task = task
        self.output_dir = Path(output_dir)
        self.evaluator_kwargs = evaluator_kwargs or {}

        if task == "pose":
            self.evaluator = PoseEvaluator(**self.evaluator_kwargs)
        elif task == "recon":
            self.evaluator = ReconEvaluator(**self.evaluator_kwargs)
        else:
            raise ValueError(f"Unknown task: {task}")

    @classmethod
    def from_config(cls, config_path: str | Path) -> "BenchmarkRunner":
        """从 YAML 配置文件创建执行器。"""
        config_path = Path(config_path)
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        backend_cfg = cfg["backend"]
        if backend_cfg["type"] == "mock":
            backend = MockInferenceBackend(**backend_cfg.get("kwargs", {}))
        else:
            raise NotImplementedError(
                f"Backend type {backend_cfg['type']} is not implemented in this environment. "
                "Use 'mock' for pipeline validation without PyTorch."
            )

        data_cfg = cfg["data"]
        scene_dirs = [Path(d) for d in data_cfg.get("scene_dirs", [])]
        if not scene_dirs and data_cfg.get("scenes", []):
            scene_dirs = [Path(s) for s in data_cfg["scenes"]]

        return cls(
            name=cfg["name"],
            backend=backend,
            scene_dirs=scene_dirs,
            task=cfg["task"],
            output_dir=Path(cfg["output_dir"]),
            evaluator_kwargs=cfg.get("evaluator_kwargs", {}),
        )

    def _run_scene(self, scene_dir: Path) -> dict[str, Any]:
        start = time.time()
        if self.task == "pose":
            inference_output = self.backend.infer_poses(scene_dir)
            result = self.evaluator.evaluate(
                inference_output["pred_c2w"], inference_output["gt_c2w"]
            )
        elif self.task == "recon":
            inference_output = self.backend.infer_reconstruction(scene_dir)
            result = self.evaluator.evaluate(
                inference_output["pred_points"], inference_output["gt_points"]
            )
        else:
            raise ValueError(f"Unknown task: {self.task}")
        result["scene"] = scene_dir.name
        result["time_sec"] = time.time() - start
        return result

    def run(self) -> list[dict[str, Any]]:
        """运行评测并保存结果。"""
        _ensure_dir(self.output_dir)
        per_scene = []
        for scene_dir in self.scene_dirs:
            per_scene.append(self._run_scene(scene_dir))

        summary = self._summarize(per_scene)

        # Save per-scene JSON
        json_path = self.output_dir / f"{self.name}_per_scene.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(per_scene, f, indent=2, ensure_ascii=False)

        # Save summary JSON
        summary_path = self.output_dir / f"{self.name}_summary.json"
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        # Save CSV
        csv_path = self.output_dir / f"{self.name}_per_scene.csv"
        if per_scene:
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=per_scene[0].keys())
                writer.writeheader()
                writer.writerows(per_scene)

        return per_scene, summary

    def _summarize(self, per_scene: list[dict[str, Any]]) -> dict[str, float]:
        if not per_scene:
            return {}
        numeric_keys = [k for k in per_scene[0].keys() if k not in ("scene", "time_sec")]
        summary = {}
        for key in numeric_keys:
            values = [s[key] for s in per_scene if key in s]
            if values:
                summary[f"{key}_mean"] = float(np.mean(values))
                summary[f"{key}_median"] = float(np.median(values))
                summary[f"{key}_std"] = float(np.std(values))
        summary["num_scenes"] = len(per_scene)
        return summary
