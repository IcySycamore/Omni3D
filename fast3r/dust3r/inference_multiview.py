# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""多视图推理。"""

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (C) 2024-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).
#
# --------------------------------------------------------
# utilities needed for the inference
# --------------------------------------------------------
import time
import torch
import tqdm

from fast3r.dust3r.utils.device import collate_with_cat, to_cpu
from fast3r.dust3r.utils.geometry import depthmap_to_pts3d, geotrf
from fast3r.dust3r.utils.misc import invalid_to_nans


def loss_of_one_batch(
    batch, model, criterion, device, precision, symmetrize_batch=False, use_amp=False, ret=None, profiling=False,
):
    """
    Args:
        batch (list[dict]): a list of views, each view is a dict of tensors, the tensors are batched
    """       
    for view in batch:
        for (name) in ("img pts3d valid_mask camera_pose camera_intrinsics F_matrix corres".split()):  # pseudo_focal
            if name not in view:
                continue
            view[name] = view[name].to(device, non_blocking=True)

    views = batch

    autocast_dict = dict(device_type=device.type)
    if precision == "32":
        autocast_dict["enabled"] = False
    elif precision == "16-mixed":
        autocast_dict["dtype"] = torch.float16
    elif precision in ["bf16-mixed", "bf16-mixed-no-grad-scaling"]:
        autocast_dict["dtype"] = torch.bfloat16
    elif precision == torch.bfloat16:
        autocast_dict["dtype"] = torch.bfloat16
    

    with torch.autocast(**autocast_dict):
        if profiling:
            preds, profiling_info = model(views, profiling=profiling)
        else:
            preds = model(views, profiling=profiling)

        # loss is supposed to be symmetric
        loss = (criterion(views, preds) if criterion is not None else None)

    result = dict(views=views, preds=preds, loss=loss)
    if profiling:
        result["profiling_info"] = profiling_info
    
    return result[ret] if ret else result


@torch.no_grad()
def inference(multiple_views_in_one_sample, model, device, dtype, verbose=True, profiling=False):
    """对多视角图像进行模型推理。

    将同一组视图送入模型前向传播，返回预测结果。
    若视图像素尺寸不一致，则强制逐张推理（batch_size=1）。

    Args:
        multiple_views_in_one_sample (list[dict]): 同一组的多视角图像字典列表。
        model (nn.Module): Fast3R 模型。
        device (torch.device): 推理设备。
        dtype (str | torch.dtype): 精度模式，如 "32"、"bf16-mixed" 等。
        verbose (bool): 是否打印日志。
        profiling (bool): 是否返回性能分析信息。

    Returns:
        dict | tuple: 推理结果字典，或在 ``profiling=True`` 时返回 (result, profiling_info)。
    """
    if verbose:
        print(f">> Inference with model on {len(multiple_views_in_one_sample)} images")
    result = []

    # first, check if all images have the same size
    multiple_shapes = not (check_if_same_size(multiple_views_in_one_sample))
    if multiple_shapes:  # force bs=1
        batch_size = 1

    # Get the result from loss_of_one_batch
    res = loss_of_one_batch(
        collate_with_cat([tuple(multiple_views_in_one_sample)]), model, None, device, dtype, profiling=profiling
    )
    
    # Extract profiling_info before to_cpu if it exists
    profiling_info = None
    if profiling and "profiling_info" in res:
        profiling_info = res.pop("profiling_info")
    
    # Process the result without profiling_info
    result.append(to_cpu(res))
    result = collate_with_cat(result, lists=multiple_shapes)
    
    # Return the result with profiling_info if requested
    if profiling and profiling_info is not None:
        return result, profiling_info
    
    return result


def check_if_same_size(imgs):
    """检查一组图像是否具有相同的像素尺寸。

    Args:
        imgs (list[dict]): 图像字典列表，每个字典包含 ``img`` 张量。

    Returns:
        bool: 若所有图像尺寸相同则返回 ``True``。
    """
    shapes = [img["img"].shape[-2:] for img in imgs]
    return all(shape == shapes[0] for shape in shapes)


def get_pred_pts3d(gt, pred, use_pose=False):
    """从模型预测结果中提取 3D 点坐标。

    支持从 depth + pseudo_focal、pts3d 或 pts3d_in_other_view 三种模式提取。
    若 ``use_pose=True``，则使用预测的相机位姿对点云进行变换。

    Args:
        gt (dict): 真实视图字典，可能包含 ``camera_intrinsics``。
        pred (dict): 模型预测结果字典。
        use_pose (bool): 是否应用预测的相机位姿变换。

    Returns:
        Tensor: 3D 点云张量，形状 (B, H, W, 3)。
    """
    if "depth" in pred and "pseudo_focal" in pred:
        try:
            pp = gt["camera_intrinsics"][..., :2, 2]
        except KeyError:
            pp = None
        pts3d = depthmap_to_pts3d(**pred, pp=pp)

    elif "pts3d" in pred:
        # pts3d from my camera
        pts3d = pred["pts3d"]

    elif "pts3d_in_other_view" in pred:
        # pts3d from the other camera, already transformed
        assert use_pose is True
        return pred["pts3d_in_other_view"]  # return!

    if use_pose:
        camera_pose = pred.get("camera_pose")
        assert camera_pose is not None
        pts3d = geotrf(camera_pose, pts3d)

    return pts3d


def find_opt_scaling(
    gt_pts1,
    gt_pts2,
    pr_pts1,
    pr_pts2=None,
    fit_mode="weiszfeld_stop_grad",
    valid1=None,
    valid2=None,
):
    """通过最小化预测点云与真实点云之间的尺度差异，计算最优尺度因子。

    支持多种拟合模式：均值（avg）、中位数（median）、Weiszfeld 迭代加权。

    Args:
        gt_pts1 (Tensor): 第一组真实 3D 点，形状 (B, H, W, 3)。
        gt_pts2 (Tensor | None): 第二组真实 3D 点，可为 ``None``。
        pr_pts1 (Tensor): 第一组预测 3D 点，形状 (B, H, W, 3)。
        pr_pts2 (Tensor | None): 第二组预测 3D 点，可为 ``None``。
        fit_mode (str): 拟合模式，可选 ``"avg"``、``"median"``、
            ``"weiszfeld"`` 或带 ``"_stop_grad"`` 后缀的变体。
        valid1 (Tensor | None): 第一组有效掩码。
        valid2 (Tensor | None): 第二组有效掩码。

    Returns:
        Tensor: 最优尺度因子，形状 (B,)。
    """
    assert gt_pts1.ndim == pr_pts1.ndim == 4
    assert gt_pts1.shape == pr_pts1.shape
    if gt_pts2 is not None:
        assert gt_pts2.ndim == pr_pts2.ndim == 4
        assert gt_pts2.shape == pr_pts2.shape

    # concat the pointcloud
    nan_gt_pts1 = invalid_to_nans(gt_pts1, valid1).flatten(1, 2)
    nan_gt_pts2 = (
        invalid_to_nans(gt_pts2, valid2).flatten(1, 2) if gt_pts2 is not None else None
    )

    pr_pts1 = invalid_to_nans(pr_pts1, valid1).flatten(1, 2)
    pr_pts2 = (
        invalid_to_nans(pr_pts2, valid2).flatten(1, 2) if pr_pts2 is not None else None
    )

    all_gt = (
        torch.cat((nan_gt_pts1, nan_gt_pts2), dim=1)
        if gt_pts2 is not None
        else nan_gt_pts1
    )
    all_pr = torch.cat((pr_pts1, pr_pts2), dim=1) if pr_pts2 is not None else pr_pts1

    dot_gt_pr = (all_pr * all_gt).sum(dim=-1)
    dot_gt_gt = all_gt.square().sum(dim=-1)

    if fit_mode.startswith("avg"):
        # scaling = (all_pr / all_gt).view(B, -1).mean(dim=1)
        scaling = dot_gt_pr.nanmean(dim=1) / dot_gt_gt.nanmean(dim=1)
    elif fit_mode.startswith("median"):
        scaling = (dot_gt_pr / dot_gt_gt).nanmedian(dim=1).values
    elif fit_mode.startswith("weiszfeld"):
        # init scaling with l2 closed form
        scaling = dot_gt_pr.nanmean(dim=1) / dot_gt_gt.nanmean(dim=1)
        # iterative re-weighted least-squares
        for iter in range(10):
            # re-weighting by inverse of distance
            dis = (all_pr - scaling.view(-1, 1, 1) * all_gt).norm(dim=-1)
            # print(dis.nanmean(-1))
            w = dis.clip_(min=1e-8).reciprocal()
            # update the scaling with the new weights
            scaling = (w * dot_gt_pr).nanmean(dim=1) / (w * dot_gt_gt).nanmean(dim=1)
    else:
        raise ValueError(f"bad {fit_mode=}")

    if fit_mode.endswith("stop_grad"):
        scaling = scaling.detach()

    scaling = scaling.clip(min=1e-3)
    # assert scaling.isfinite().all(), bb()
    return scaling
