"""重建管线（纯函数，不依赖 Qt / UI）。

职责：把「图像路径 → 重建结果」的完整流程拆成可测试的纯函数：
1. load_images 加载并预处理图像
2. inference 运行 Fast3R 一次前向
3. align_local_pts3d_to_global 将局部点云刚体对齐到全局坐标系
"""
from concurrent.futures import ThreadPoolExecutor

import roma
import torch

from fast3r.dust3r.utils.image import load_images
from fast3r.dust3r.inference_multiview import inference


def align_local_pts3d_to_global(preds, views, min_conf_thr_percentile=0):
    """将局部点云对齐到全局坐标系（刚体变换 + 缩放）。

    在 preds 中写入新键 ``pts3d_local_aligned_to_global``。

    Args:
        preds: 每视图预测字典列表。
        views: 每视图输入字典列表。
        min_conf_thr_percentile: 对齐时使用的全局置信度百分位阈值。
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

        pts3d_local = pred["pts3d_local"][batch_index]          # (H, W, 3)
        pts3d_global = pred["pts3d_in_other_view"][batch_index]  # (H, W, 3)
        conf_global = pred["conf"][batch_index]                 # (H, W)

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
        else:
            R, t, s = roma.rigid_points_registration(x, y, compute_scaling=True)

        pts_local_aligned_flat = s * (pts_local_flat @ R.T) + t
        pts_local_aligned = pts_local_aligned_flat.view(H_cur, W_cur, 3)
        return view_index, batch_index, pts_local_aligned

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
    for view_index, batch_index, pts_local_aligned in results:
        aligned_pts_dict[view_index][batch_index] = pts_local_aligned

    for view_index in range(num_views):
        preds[view_index]["pts3d_local_aligned_to_global"] = torch.stack(
            aligned_pts_dict[view_index], dim=0
        )


def run_reconstruction(
    image_paths,
    model,
    device,
    resolution=512,
    dtype=torch.float32,
    align_conf_percentile=85,
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
        progress_callback: 可选进度回调 progress_callback(阶段字符串)。

    Returns:
        tuple: (output_dict, profiling_info)
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

    report("完成")
    return output_dict, profiling_info
