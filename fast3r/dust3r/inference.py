# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (C) 2024-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).
#
# --------------------------------------------------------
# utilities needed for the inference
# --------------------------------------------------------
import torch
import tqdm

from dust3r.utils.device import collate_with_cat, to_cpu
from dust3r.utils.geometry import depthmap_to_pts3d, geotrf
from dust3r.utils.misc import invalid_to_nans


def _interleave_imgs(img1, img2):
    """将两个图像字典按样本交错合并。

    Args:
        img1 (dict): 第一个图像批次字典。
        img2 (dict): 第二个图像批次字典。

    Returns:
        dict: 交错合并后的字典，每对样本依次排列。
    """
    res = {}
    for key, value1 in img1.items():
        value2 = img2[key]
        if isinstance(value1, torch.Tensor):
            value = torch.stack((value1, value2), dim=1).flatten(0, 1)
        else:
            value = [x for pair in zip(value1, value2) for x in pair]
        res[key] = value
    return res


def make_batch_symmetric(batch):
    """将批次数据对称化（同时考虑正向和反向图像对）。

    Args:
        batch (tuple): (view1, view2) 图像对。

    Returns:
        tuple: 交错对称后的 (view1, view2)。
    """
    view1, view2 = batch
    view1, view2 = (_interleave_imgs(view1, view2), _interleave_imgs(view2, view1))
    return view1, view2


def loss_of_one_batch(
    batch, model, criterion, device, symmetrize_batch=False, use_amp=False, ret=None
):
    """对单个批次进行推理并计算损失。

    Args:
        batch (tuple): (view1, view2) 图像对批次。
        model (nn.Module): 推理模型。
        criterion (callable | None): 损失函数，为 None 时跳过损失计算。
        device (str | torch.device): 目标设备。
        symmetrize_batch (bool): 是否对批次进行对称化处理。默认 False。
        use_amp (bool): 是否使用自动混合精度。默认 False。
        ret (str | None): 若指定，则只返回结果字典中对应键的值。

    Returns:
        dict | Any: 包含 view1, view2, pred1, pred2, loss 的字典，
            或若 ret 指定了键则返回对应值。
    """
    view1, view2 = batch
    for view in batch:
        for (
            name
        ) in (
            "img pts3d valid_mask camera_pose camera_intrinsics F_matrix corres".split()
        ):  # pseudo_focal
            if name not in view:
                continue
            view[name] = view[name].to(device, non_blocking=True)

    if symmetrize_batch:
        view1, view2 = make_batch_symmetric(batch)

    with torch.cuda.amp.autocast(enabled=bool(use_amp)):
        pred1, pred2 = model(view1, view2)

        # loss is supposed to be symmetric
        with torch.cuda.amp.autocast(enabled=False):
            loss = (
                criterion(view1, view2, pred1, pred2) if criterion is not None else None
            )

    result = dict(view1=view1, view2=view2, pred1=pred1, pred2=pred2, loss=loss)
    return result[ret] if ret else result


@torch.no_grad()
def inference(pairs, model, device, batch_size=8, verbose=True):
    """对图像对列表进行批量推理。

    Args:
        pairs (list): 图像对列表，每个元素为 (view1, view2) 字典对。
        model (nn.Module): 推理模型。
        device (str | torch.device): 目标设备。
        batch_size (int): 每批处理的图像对数量。若图像尺寸不一致则强制为 1。
        verbose (bool): 是否显示进度条。默认 True。

    Returns:
        dict: 所有批次的推理结果合并字典。
    """
    if verbose:
        print(f">> Inference with model on {len(pairs)} image pairs")
    result = []

    # first, check if all images have the same size
    multiple_shapes = not (check_if_same_size(pairs))
    if multiple_shapes:  # force bs=1
        batch_size = 1

    for i in tqdm.trange(0, len(pairs), batch_size, disable=not verbose):
        res = loss_of_one_batch(
            collate_with_cat(pairs[i : i + batch_size]), model, None, device
        )
        result.append(to_cpu(res))

    result = collate_with_cat(result, lists=multiple_shapes)

    return result


def check_if_same_size(pairs):
    """检查所有图像对中的图像是否具有相同尺寸。

    Args:
        pairs (list): 图像对列表。

    Returns:
        bool: 若所有图像尺寸相同返回 True，否则返回 False。
    """
    shapes1 = [img1["img"].shape[-2:] for img1, img2 in pairs]
    shapes2 = [img2["img"].shape[-2:] for img1, img2 in pairs]
    return all(shapes1[0] == s for s in shapes1) and all(
        shapes2[0] == s for s in shapes2
    )


def get_pred_pts3d(gt, pred, use_pose=False):
    """从模型预测中提取 3D 点云。

    根据预测字典中的键（'depth'/'pts3d'/'pts3d_in_other_view'）选择不同提取策略。

    Args:
        gt (dict): 真实标注，可能包含相机内参。
        pred (dict): 模型预测字典。
        use_pose (bool): 若为 True，将点云从相机坐标系变换到世界坐标系。

    Returns:
        Tensor: 形状为 (B, H, W, 3) 的 3D 点云。
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
    """找到使预测点云与真实点云最对齐的最优缩放因子。

    Args:
        gt_pts1 (Tensor): 视图1 的真实 3D 点，形状 (B, H, W, 3)。
        gt_pts2 (Tensor | None): 视图2 的真实 3D 点，形状 (B, H, W, 3)。
        pr_pts1 (Tensor): 视图1 的预测 3D 点，形状 (B, H, W, 3)。
        pr_pts2 (Tensor | None): 视图2 的预测 3D 点。
        fit_mode (str): 拟合方法，可选 'avg', 'median', 'weiszfeld',
            加 '_stop_grad' 后缀可停止梯度传播。
        valid1 (Tensor | None): 视图1 的有效像素掩码。
        valid2 (Tensor | None): 视图2 的有效像素掩码。

    Returns:
        Tensor: 形状为 (B,) 的最优缩放因子，已裁剪到 [1e-3, +∞)。
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
