"""重建管线（纯函数，不依赖 Qt / UI）。

职责：把「图像路径 → 重建结果」的完整流程拆成可测试的纯函数：

1. ``load_images`` 加载并预处理图像
2. ``inference`` 运行 Fast3R 一次前向
3. ``align_local_pts3d_to_global`` 将局部点云刚体对齐到全局（模型）坐标系
4. ``metric_alignment`` + ``apply_similarity`` —— **用外部米制相机位姿把点云
   换算到真实尺度与 AR 世界坐标系**（有 `extrinsics` 时生效）

关于输入的外参约定：``extrinsics`` 与 ARCore / 华为 AREngine 的
``ArPose_getMatrix`` 一致 —— 每帧 16 个 float，**列主序**的 4×4
**cam2world** 矩阵，平移分量为**米**。
"""
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Sequence

import numpy as np
import roma
import torch

from fast3r.dust3r.utils.image import load_images
from fast3r.dust3r.inference_multiview import inference

from app.core import config


# ══════════════════ 外部位姿 / 内参 解析 ══════════════════

def extrinsics_to_cam2world(extrinsics) -> torch.Tensor:
    """外部相机位姿 → ``(N, 4, 4)`` 行主序 cam2world 张量（float64）。

    每项可以是 16 个元素的**列主序**数组，或已是 4×4 嵌套列表。
    """
    mats = []
    for item in (extrinsics or []):
        arr = np.asarray(item, dtype=np.float64)
        if arr.shape == (4, 4):
            mats.append(arr)
        elif arr.size == 16:
            # 列主序扁平 → 行主序矩阵（M[i][j] = a[j*4 + i]）
            mats.append(arr.reshape(4, 4).T)
        else:
            raise ValueError(f"extrinsics 每帧需 16 元素或 4×4，收到 shape={arr.shape}")
    if not mats:
        return torch.zeros((0, 4, 4), dtype=torch.float64)
    return torch.tensor(np.stack(mats), dtype=torch.float64)


def summarize_intrinsics(intrinsics) -> Optional[list]:
    """把外部内参整理为逐视图摘要（校验用）。非法输入返回 None。"""
    if intrinsics is None:
        return None
    arr_all = np.asarray(intrinsics, dtype=np.float64)
    if arr_all.size == 0:
        return None
    if arr_all.shape == (3, 3):        # 单相机内参（非逐帧）
        intrinsics = [arr_all]
    out = []
    for item in intrinsics:
        arr = np.asarray(item, dtype=np.float64).reshape(-1)
        if arr.size != 9:
            return None
        k = arr.reshape(3, 3)
        out.append({
            "fx": float(k[0, 0]), "fy": float(k[1, 1]),
            "cx": float(k[0, 2]), "cy": float(k[1, 2]),
        })
    return out


# ══════════════════ 局部 → 全局（模型坐标系） ══════════════════

def align_local_pts3d_to_global(preds, views, min_conf_thr_percentile=0):
    """将局部点云对齐到全局（模型）坐标系（刚体变换 + 缩放）。

    写入 ``preds[i]["pts3d_local_aligned_to_global"]``，
    以及 ``preds[i]["camera_center"]`` —— 第 i 帧相机中心在全局坐标系中的位置
    （即该视图 local→global 变换的平移项，供 `metric_alignment` 使用）。
    """
    for pred in preds:
        for key in ("pts3d_local", "conf_local", "pts3d_in_other_view", "conf"):
            if key not in pred:
                raise ValueError(f"Key '{key}' not found in preds.")

    num_views = len(preds)
    B = preds[0]["pts3d_local"].shape[0]

    def process_view_batch(view_index, batch_index):
        pred = preds[view_index]
        view = views[view_index]

        pts3d_local = pred["pts3d_local"][batch_index]           # (H, W, 3)
        pts3d_global = pred["pts3d_in_other_view"][batch_index]  # (H, W, 3)
        conf_global = pred["conf"][batch_index]                  # (H, W)

        H_cur, W_cur, _ = pts3d_local.shape

        if "valid_mask" in view:
            valid_mask = view["valid_mask"][batch_index]
        else:
            valid_mask = torch.ones_like(conf_global, dtype=torch.bool)

        conf_global_flat = conf_global.reshape(-1)
        conf_threshold_value = torch.quantile(
            conf_global_flat, min_conf_thr_percentile / 100.0
        )
        conf_mask = conf_global >= conf_threshold_value
        final_mask = conf_mask & valid_mask

        pts_local_flat = pts3d_local.view(-1, 3)
        pts_global_flat = pts3d_global.view(-1, 3)
        final_mask_flat = final_mask.view(-1)

        x = pts_local_flat[final_mask_flat]
        y = pts_global_flat[final_mask_flat]

        # 置信度阈值过滤后点数不足则退回仅 valid_mask
        if x.shape[0] < 3:
            final_mask_flat = valid_mask.view(-1)
            x = pts_local_flat[final_mask_flat]
            y = pts_global_flat[final_mask_flat]

        # 仍不足则使用单位变换
        if x.shape[0] < 3:
            R = torch.eye(3, device=pts_local_flat.device, dtype=pts_local_flat.dtype)
            t = torch.zeros(3, device=pts_local_flat.device, dtype=pts_local_flat.dtype)
            s = 1.0
            residual_ratio = None
        else:
            R, t, s = roma.rigid_points_registration(x, y, compute_scaling=True)
            # 质量代理：对齐后残差的 RMS 相对「点的 RMS 半径」——越小说明
            # 局部/全局两个 head 越自洽（尺度无关，可跟不同场景对比）。
            fitted = s * (x @ R.T) + t
            resid = torch.sqrt(((fitted - y) ** 2).sum(dim=1).mean())
            radius = torch.sqrt(((y - y.mean(dim=0)) ** 2).sum(dim=1).mean())
            residual_ratio = float(resid / (radius + 1e-12))

        pts_local_aligned_flat = s * (pts_local_flat @ R.T) + t
        pts_local_aligned = pts_local_aligned_flat.view(H_cur, W_cur, 3)
        # t 即该帧相机在全局坐标系中的位置（local 原点 → global）
        return view_index, batch_index, pts_local_aligned, t, float(s), residual_ratio

    tasks = [
        (view_idx, batch_idx)
        for view_idx in range(num_views)
        for batch_idx in range(B)
    ]
    with ThreadPoolExecutor() as executor:
        futures = [
            executor.submit(process_view_batch, view_idx, batch_idx)
            for view_idx, batch_idx in tasks
        ]
        results = [future.result() for future in futures]

    aligned_pts_dict = {view_idx: [None] * B for view_idx in range(num_views)}
    center_dict = {view_idx: [None] * B for view_idx in range(num_views)}
    for view_index, batch_index, pts_local_aligned, center, fit_scale, resid_ratio in results:
        aligned_pts_dict[view_index][batch_index] = pts_local_aligned
        center_dict[view_index][batch_index] = center
        preds[view_index]["align_scale"] = fit_scale
        preds[view_index]["align_residual_ratio"] = resid_ratio

    for view_index in range(num_views):
        preds[view_index]["pts3d_local_aligned_to_global"] = torch.stack(
            aligned_pts_dict[view_index], dim=0
        )
        preds[view_index]["camera_center"] = torch.stack(
            center_dict[view_index], dim=0
        )


def quality_stats(preds, source: Optional[str] = None) -> dict:
    """汇总**质量代理指标**（无 GT 时用于横向/纵向对比，非绝对精度）。

    - ``local_scale_mean/std``：各视图 local→global 拟合出的缩放。理想应≈1 且离散度小。
    - ``residual_ratio_median``：局部/全局两个 head 对齐后的残差比（越小越自洽）。
    - ``mean_conf``：**所选 head** 的平均置信度（仅作参考，跟 ``source`` 走）。
    """
    scales = [float(p["align_scale"]) for p in preds if p.get("align_scale") is not None]
    ratios = [float(p["align_residual_ratio"]) for p in preds
              if p.get("align_residual_ratio") is not None]
    conf_key = pointcloud_keys(source)[1]
    confs = [float(p[conf_key].mean()) for p in preds if p.get(conf_key) is not None]

    def _mean(v):
        return float(sum(v) / len(v)) if v else None

    def _std(v):
        if len(v) < 2:
            return None
        m = sum(v) / len(v)
        return float((sum((x - m) ** 2 for x in v) / (len(v) - 1)) ** 0.5)

    def _median(v):
        if not v:
            return None
        s = sorted(v)
        n = len(s)
        return float(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)

    return {
        "views": len(preds),
        "local_scale_mean": _mean(scales),
        "local_scale_std": _std(scales),
        "residual_ratio_median": _median(ratios),
        "mean_conf": _mean(confs),
    }


# ══════════════════ 点云来源（local head / global head） ══════════════════

def resolve_pts3d_source(source: Optional[str] = None) -> str:
    """归一化点云来源名：``"local"``（默认）或 ``"global"``。

    默认跟随上游重建评测的取值（`config.PTS3D_SOURCE`，可由
    ``OMNI3D_PTS3D_SOURCE`` 环境变量覆盖）。
    """
    value = source if source is not None else config.PTS3D_SOURCE
    value = str(value or "local").strip().lower()
    return value if value in ("local", "global") else "local"


def pointcloud_keys(source: Optional[str] = None) -> tuple:
    """返回 ``(点云键, 置信度键)``。

    - ``local``（默认）：``pts3d_local_aligned_to_global`` + ``conf_local``
      —— 由 `align_local_pts3d_to_global` 把 local head 对齐到全局坐标系后产生。
    - ``global``：``pts3d_in_other_view`` + ``conf``（Fast3R 全局 head 原始输出）。

    两种来源的点都处在**同一个全局坐标系**中，因此下游（米制尺度变换、
    置信度过滤、PLY、渲染）无需区分。
    """
    if resolve_pts3d_source(source) == "global":
        return "pts3d_in_other_view", "conf"
    return "pts3d_local_aligned_to_global", "conf_local"


# ══════════════════ 模型坐标系 → 真实米制（extrinsics 驱动） ══════════════════

def metric_alignment(preds, extrinsics, min_spread_m=1e-3) -> Optional[dict]:
    """用外部**米制**相机位姿求「模型坐标系 → AR 世界坐标系」的相似变换。

    原理：`align_local_pts3d_to_global` 已把每帧相机中心写进
    ``preds[i]["camera_center"]``（模型坐标系，任意单位）；外部位姿给出同一批
    相机在**米制世界系**中的中心。两批点做刚体+尺度配准即得

        p_metric = s · R · p_model + T

    Returns:
        ``{"scale", "R", "T", "n_views"}``；数据不足 / 退化 / 尺度异常时返回 ``None``。
    """
    if not extrinsics:
        return None
    gt = extrinsics_to_cam2world(extrinsics)
    n = min(len(preds), int(gt.shape[0]))
    if n < 2:                      # 单帧无法定尺度
        return None

    centers_pred = []
    for i in range(n):
        c = preds[i].get("camera_center")
        if c is None:
            return None
        centers_pred.append(torch.as_tensor(c, dtype=torch.float64).reshape(-1)[:3])
    c_pred = torch.stack(centers_pred)            # (n,3) 模型坐标系
    poses_gt = gt[:n]
    c_gt = poses_gt[:, :3, 3]                     # (n,3) 米制

    if not torch.isfinite(c_pred).all() or not torch.isfinite(poses_gt).all():
        return None
    # 外部位姿几乎不动 → 无法确定尺度
    if float(torch.cdist(c_gt, c_gt).max()) < min_spread_m:
        return None

    R, T, s = roma.rigid_points_registration(c_pred, c_gt, compute_scaling=True)
    s = float(s)
    if not np.isfinite(s) or s <= 1e-9 or s >= 1e9:
        return None
    return {"scale": s, "R": R.to(torch.float64), "T": T.to(torch.float64), "n_views": n}


def apply_similarity(points: torch.Tensor, R, T, s) -> torch.Tensor:
    """把模型坐标系的点云变换到米制/AR 世界坐标系：``p' = s · R · p + T``。"""
    p = points.to(torch.float64)
    r = torch.as_tensor(R, dtype=torch.float64, device=p.device)
    t = torch.as_tensor(T, dtype=torch.float64, device=p.device).reshape(-1)[:3]
    out = float(s) * (p @ r.T) + t
    return out.to(dtype=points.dtype)


# ══════════════════ 顶层管线 ══════════════════

def run_reconstruction(
    image_paths,
    model,
    device,
    resolution=512,
    dtype=torch.float32,
    align_conf_percentile=85,
    extrinsics=None,
    intrinsics=None,
    pts3d_source=None,
    progress_callback=None,
):
    """执行完整重建管线。

    Args:
        image_paths: 图像路径列表。
        model: Fast3R 模型（已 .to(device)）。
        device: 计算设备。
        resolution: 输入分辨率（512 / 224）。
        dtype: 推理精度。
        align_conf_percentile: 对齐置信度百分位。
        extrinsics: 每帧外部相机位姿（col-major 4×4 cam2world，米制）。
            提供且可用时，点云会被换算到**真实尺度 + AR 世界坐标系**。
        intrinsics: 每帧 3×3 内参（当前用于校验与结果报告）。
        pts3d_source: 点云来源 ``"local"`` / ``"global"``（None 时取
            `config.PTS3D_SOURCE`，默认 ``local``，跟随上游重建评测）。
        progress_callback: 可选进度回调 progress_callback(阶段字符串)。

    Returns:
        tuple: ``(output_dict, profiling_info)``。``output_dict["metric"]`` 记录
        尺度换算结果：``{"aligned", "scale", "n_views", "source",
        "extrinsics_provided", "intrinsics", "pts3d_source"}``；
        ``output_dict["quality"]`` 记录质量代理指标（见 `quality_stats`）。
    """
    def report(stage):
        if progress_callback:
            progress_callback(stage)

    report("加载并裁剪图像...")
    images = load_images(image_paths, size=resolution, verbose=True)

    report("模型推理中...")
    output_dict, profiling_info = inference(
        images,
        model,
        device,
        dtype=dtype,
        verbose=True,
        profiling=True,
    )

    report("对齐局部点云到全局坐标系...")
    align_local_pts3d_to_global(
        preds=output_dict["preds"],
        views=output_dict["views"],
        min_conf_thr_percentile=align_conf_percentile,
    )

    # ---- 真实尺度：用外部米制位姿换算 ----
    metric = {
        "aligned": False,
        "scale": None,
        "n_views": 0,
        "source": "none",
        "extrinsics_provided": bool(extrinsics),
        "intrinsics": summarize_intrinsics(intrinsics),
        "pts3d_source": resolve_pts3d_source(pts3d_source),
    }
    if extrinsics:
        report("按真实尺度对齐（AR 位姿）...")
        try:
            fit = metric_alignment(output_dict["preds"], extrinsics)
        except Exception as exc:  # noqa: BLE001 - 尺度换算失败不应中断重建
            print(f"[pipeline] 尺度对齐失败，回退任意尺度: {exc}")
            fit = None
        if fit is not None:
            s, R, T = fit["scale"], fit["R"], fit["T"]
            for pred in output_dict["preds"]:
                for key in ("pts3d_in_other_view", "pts3d_local_aligned_to_global"):
                    if key in pred:
                        pred[key] = apply_similarity(pred[key], R, T, s)
            metric.update({"aligned": True, "scale": s,
                           "n_views": fit["n_views"], "source": "extrinsics"})
            print(f"[pipeline] 已按 AR 位姿对齐到米制：scale={s:.6g}（{fit['n_views']} 帧）")

    output_dict["metric"] = metric
    output_dict["quality"] = quality_stats(output_dict["preds"], source=pts3d_source)
    report("完成")
    return output_dict, profiling_info
