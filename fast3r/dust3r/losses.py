# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (C) 2024-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).
#
# --------------------------------------------------------
# Implementation of DUSt3R training losses
# --------------------------------------------------------
from copy import copy, deepcopy

import torch
import torch.nn as nn

from dust3r.inference import find_opt_scaling, get_pred_pts3d
from dust3r.utils.geometry import (
    geotrf,
    get_joint_pointcloud_center_scale,
    get_joint_pointcloud_depth,
    inv,
    normalize_pointcloud,
)


def Sum(*losses_and_masks):
    """合并多个 (loss, mask) 或 (loss, mask, loss_type) 元组。

    若各损失为标量则直接求和返回；否则原样返回元组列表供 ConfLoss 使用。

    Args:
        *losses_and_masks: 可变数量的 (loss, mask) 或 (loss, mask, loss_type) 元组。

    Returns:
        若各损失为标量，返回所有损失之和（Tensor）；否则返回原始元组列表。
    """
    if len(losses_and_masks[0]) == 2:
        loss, mask = losses_and_masks[0]
    else:
        loss, mask, loss_type = losses_and_masks[0]

    if loss.ndim > 0:
        # we are actually returning the loss for every pixels
        return losses_and_masks
    else:
        # we are returning the global loss
        for loss2, mask2 in losses_and_masks[1:]:
            loss = loss + loss2
        return loss


class LLoss(nn.Module):
    """L-范数损失基类。

    子类需实现 distance() 方法定义具体的距离度量。
    支持 'none'、'sum'、'mean' 三种 reduction 模式。
    """

    def __init__(self, reduction="mean"):
        """初始化 LLoss。

        Args:
            reduction (str): 损失聚合方式，可选 'none'、'sum'、'mean'。默认 'mean'。
        """
        super().__init__()
        self.reduction = reduction

    def forward(self, a, b):
        """计算两组点之间的 L-范数损失。

        Args:
            a (Tensor): 预测点，形状为 (..., D)，D ∈ {1, 2, 3}。
            b (Tensor): 真实点，形状与 a 相同。

        Returns:
            Tensor: 根据 reduction 模式返回标量、向量或逐像素损失。
        """
        assert (
            a.shape == b.shape and a.ndim >= 2 and 1 <= a.shape[-1] <= 3
        ), f"Bad shape = {a.shape}"
        dist = self.distance(a, b)
        assert dist.ndim == a.ndim - 1  # one dimension less
        if self.reduction == "none":
            return dist
        if self.reduction == "sum":
            return dist.sum()
        if self.reduction == "mean":
            return dist.mean() if dist.numel() > 0 else dist.new_zeros(())
        raise ValueError(f"bad {self.reduction=} mode")

    def distance(self, a, b):
        """计算两点之间的距离（由子类实现）。

        Args:
            a (Tensor): 预测点。
            b (Tensor): 真实点。

        Returns:
            Tensor: 每对点的距离值，维度比输入少一维。

        Raises:
            NotImplementedError: 基类不实现此方法。
        """
        raise NotImplementedError()


class L21Loss(LLoss):
    """3D 点之间的欧氏距离损失（L2,1 范数）。"""

    def distance(self, a, b):
        """计算两组 3D 点间的欧氏距离。

        Args:
            a (Tensor): 预测点，形状为 (..., 3)。
            b (Tensor): 真实点，形状与 a 相同。

        Returns:
            Tensor: 每对点的欧氏距离，形状为 (...)。
        """
        return torch.norm(a - b, dim=-1)  # normalized L2 distance


L21 = L21Loss()


class Criterion(nn.Module):
    """损失基类，封装一个 LLoss 实例作为底层度量标准。

    提供 get_name() 和 with_reduction() 接口供子类使用。
    """

    def __init__(self, criterion=None):
        """初始化 Criterion。

        Args:
            criterion (LLoss): 底层距离度量，必须是 LLoss 的实例。
        """
        super().__init__()
        assert isinstance(criterion, LLoss), (
            f"{criterion} is not a proper criterion!" + bb()
        )
        self.criterion = copy(criterion)

    def get_name(self):
        """返回损失的名称字符串。

        Returns:
            str: 形如 'ClassName(criterion)' 的名称。
        """
        return f"{type(self).__name__}({self.criterion})"

    def with_reduction(self, mode):
        """返回一个使用指定 reduction 模式的损失副本。

        Args:
            mode (str): reduction 模式，如 'none'、'mean'。

        Returns:
            Criterion: 新的损失对象，其 criterion.reduction 已设为 'none'。
        """
        res = loss = deepcopy(self)
        while loss is not None:
            assert isinstance(loss, Criterion)
            loss.criterion.reduction = "none"  # make it return the loss for each sample
            loss = loss._loss2  # we assume loss is a Multiloss
        return res


class MultiLoss(nn.Module):
    """Easily combinable losses (also keep track of individual loss values):
        loss = MyLoss1() + 0.1*MyLoss2()
    Usage:
        Inherit from this class and override get_name() and compute_loss()
    """

    def __init__(self):
        """初始化 MultiLoss，设置权重为 1，链式损失为 None。"""
        super().__init__()
        self._alpha = 1
        self._loss2 = None

    def compute_loss(self, *args, **kwargs):
        """计算损失（由子类实现）。

        Returns:
            Tensor 或 (Tensor, dict): 损失值，或损失值与细节字典的元组。

        Raises:
            NotImplementedError: 基类不实现此方法。
        """
        raise NotImplementedError()

    def get_name(self):
        """返回损失名称字符串（由子类实现）。

        Returns:
            str: 损失名称。

        Raises:
            NotImplementedError: 基类不实现此方法。
        """
        raise NotImplementedError()

    def __mul__(self, alpha):
        """返回带权重的损失副本（支持 alpha * loss 语法）。

        Args:
            alpha (int | float): 权重系数。

        Returns:
            MultiLoss: 权重已更新的损失副本。
        """
        assert isinstance(alpha, (int, float))
        res = copy(self)
        res._alpha = alpha
        return res

    __rmul__ = __mul__  # same

    def __add__(self, loss2):
        """将两个损失链式连接（支持 loss1 + loss2 语法）。

        Args:
            loss2 (MultiLoss): 要追加的第二个损失。

        Returns:
            MultiLoss: 链式连接后的损失对象。
        """
        assert isinstance(loss2, MultiLoss)
        res = cur = copy(self)
        # find the end of the chain
        while cur._loss2 is not None:
            cur = cur._loss2
        cur._loss2 = loss2
        return res

    def __repr__(self):
        """返回损失的字符串表示，含权重和链式结构。

        Returns:
            str: 如 '0.5*LossA + LossB' 格式的字符串。
        """
        name = self.get_name()
        if self._alpha != 1:
            name = f"{self._alpha:g}*{name}"
        if self._loss2:
            name = f"{name} + {self._loss2}"
        return name

    def forward(self, *args, **kwargs):
        """前向传播，计算加权损失并递归调用链式损失。

        Args:
            *args: 传递给 compute_loss 的位置参数。
            **kwargs: 传递给 compute_loss 的关键字参数。

        Returns:
            tuple[Tensor, dict]: (总损失值, 各子损失的细节字典)。
        """
        loss = self.compute_loss(*args, **kwargs)
        if isinstance(loss, tuple):
            loss, details = loss
        elif loss.ndim == 0:
            details = {self.get_name(): float(loss)}
        else:
            details = {}
        loss = loss * self._alpha

        if self._loss2:
            loss2, details2 = self._loss2(*args, **kwargs)
            loss = loss + loss2
            details |= details2

        return loss, details


class Regr3D(Criterion, MultiLoss):
    """Ensure that all 3D points are correct.
    Asymmetric loss: view1 is supposed to be the anchor.

    P1 = RT1 @ D1
    P2 = RT2 @ D2
    loss1 = (I @ pred_D1) - (RT1^-1 @ RT1 @ D1)
    loss2 = (RT21 @ pred_D2) - (RT1^-1 @ P2)
          = (RT21 @ pred_D2) - (RT1^-1 @ RT2 @ D2)
    """

    def __init__(self, criterion, norm_mode="avg_dis", gt_scale=False):
        """初始化 Regr3D。

        Args:
            criterion (LLoss): 底层距离度量（如 L21Loss）。
            norm_mode (str): 点云归一化模式，如 'avg_dis'。
            gt_scale (bool): 若为 True，真实点不做归一化（保持原始尺度）。
        """
        super().__init__(criterion)
        self.norm_mode = norm_mode
        self.gt_scale = gt_scale

    def get_all_pts3d(self, gt1, gt2, pred1, pred2, dist_clip=None):
        """提取并归一化双视角的真实与预测 3D 点。

        所有点都变换到 view1 的相机坐标系中。

        Args:
            gt1 (dict): view1 的真实标注，包含 'camera_pose', 'pts3d', 'valid_mask'。
            gt2 (dict): view2 的真实标注。
            pred1 (dict): view1 的模型预测。
            pred2 (dict): view2 的模型预测。
            dist_clip (float, optional): 距离阈值，超出该距离的点标为无效。

        Returns:
            tuple: (gt_pts1, gt_pts2, pr_pts1, pr_pts2, valid1, valid2, monitoring_dict)。
        """
        # everything is normalized w.r.t. camera of view1
        in_camera1 = inv(gt1["camera_pose"])
        gt_pts1 = geotrf(in_camera1, gt1["pts3d"])  # B,H,W,3
        gt_pts2 = geotrf(in_camera1, gt2["pts3d"])  # B,H,W,3

        valid1 = gt1["valid_mask"].clone()
        valid2 = gt2["valid_mask"].clone()

        if dist_clip is not None:
            # points that are too far-away == invalid
            dis1 = gt_pts1.norm(dim=-1)  # (B, H, W)
            dis2 = gt_pts2.norm(dim=-1)  # (B, H, W)
            valid1 = valid1 & (dis1 <= dist_clip)
            valid2 = valid2 & (dis2 <= dist_clip)

        pr_pts1 = get_pred_pts3d(gt1, pred1, use_pose=False)
        pr_pts2 = get_pred_pts3d(gt2, pred2, use_pose=True)

        # normalize 3d points
        if self.norm_mode:
            pr_pts1, pr_pts2 = normalize_pointcloud(
                pr_pts1, pr_pts2, self.norm_mode, valid1, valid2
            )
        if self.norm_mode and not self.gt_scale:
            gt_pts1, gt_pts2 = normalize_pointcloud(
                gt_pts1, gt_pts2, self.norm_mode, valid1, valid2
            )

        return gt_pts1, gt_pts2, pr_pts1, pr_pts2, valid1, valid2, {}

    def compute_loss(self, gt1, gt2, pred1, pred2, **kw):
        """计算双视角 3D 点回归损失。

        Args:
            gt1 (dict): view1 的真实标注。
            gt2 (dict): view2 的真实标注。
            pred1 (dict): view1 的模型预测。
            pred2 (dict): view2 的模型预测。
            **kw: 传递给 get_all_pts3d 的额外参数（如 dist_clip）。

        Returns:
            tuple[Tensor | list, dict]: (损失值或损失列表, 细节字典)。
        """
        (
            gt_pts1,
            gt_pts2,
            pred_pts1,
            pred_pts2,
            mask1,
            mask2,
            monitoring,
        ) = self.get_all_pts3d(gt1, gt2, pred1, pred2, **kw)
        # loss on img1 side
        l1 = self.criterion(pred_pts1[mask1], gt_pts1[mask1])
        # loss on gt2 side
        l2 = self.criterion(pred_pts2[mask2], gt_pts2[mask2])
        self_name = type(self).__name__
        details = {
            self_name + "_pts3d_1": float(l1.mean()),
            self_name + "_pts3d_2": float(l2.mean()),
        }
        return Sum((l1, mask1), (l2, mask2)), (details | monitoring)


class Regr3DMultiview(Criterion, MultiLoss):
    """Ensure that all 3D points are correct for multiple views.
    Asymmetric loss: view1 is supposed to be the anchor.
    """

    def __init__(self, criterion, norm_mode="avg_dis", gt_scale=False):
        """初始化 Regr3DMultiview。

        Args:
            criterion (LLoss): 底层距离度量。
            norm_mode (str): 点云归一化模式，如 'avg_dis'。
            gt_scale (bool): 若为 True，真实点不做归一化。
        """
        super().__init__(criterion)
        self.norm_mode = norm_mode
        self.gt_scale = gt_scale

    def get_pts3d_for_view(self, gt_anchor, pred_anchor, gt_other, pred_other, dist_clip=None):
        """提取并归一化锚视图和目标视图的 3D 点。

        Args:
            gt_anchor (dict): 锚视图的真实标注。
            pred_anchor (dict): 锚视图的模型预测。
            gt_other (dict): 目标视图的真实标注。
            pred_other (dict): 目标视图的模型预测。
            dist_clip (float, optional): 距离裁剪阈值。

        Returns:
            tuple: (gt_pts1, gt_pts_other, pr_pts1, pr_pts_other, valid1, valid_other)。
        """
        # everything is normalized w.r.t. camera of view1 (anchor)
        in_camera1 = inv(gt_anchor["camera_pose"].float())  # FIXME: for some reason, Lightning's bf16-true mode does not automatically cast to float32

        gt_pts1 = geotrf(in_camera1, gt_anchor["pts3d"])  # B,H,W,3
        valid1 = gt_anchor["valid_mask"].clone()
        gt_pts_other = geotrf(in_camera1, gt_other["pts3d"])  # B,H,W,3
        valid_other = gt_other["valid_mask"].clone()

        if dist_clip is not None:
            # points that are too far-away == invalid
            dis1 = gt_pts1.norm(dim=-1)  # (B, H, W)
            dis_other = gt_pts_other.norm(dim=-1)  # (B, H, W)
            valid1 = valid1 & (dis1 <= dist_clip)
            valid_other = valid_other & (dis_other <= dist_clip)

        pr_pts1 = get_pred_pts3d(gt_anchor, pred_anchor, use_pose=True)
        pr_pts_other = get_pred_pts3d(gt_other, pred_other, use_pose=True)

        # normalize 3d points
        if self.norm_mode:
            pr_pts1, pr_pts_other = normalize_pointcloud(
                pr_pts1, pr_pts_other, self.norm_mode, valid1, valid_other
            )
        if self.norm_mode and not self.gt_scale:
            gt_pts1, gt_pts_other = normalize_pointcloud(
                gt_pts1, gt_pts_other, self.norm_mode, valid1, valid_other
            )

        return gt_pts1, gt_pts_other, pr_pts1, pr_pts_other, valid1, valid_other

    def compute_loss(self, gts, preds, **kw):
        """计算多视角 3D 点回归损失。

        Args:
            gts (list[dict]): 所有视图的真实标注列表，gts[0] 为锚视图。
            preds (list[dict]): 所有视图的模型预测列表。
            **kw: 传递给 get_pts3d_for_view 的额外参数。

        Returns:
            tuple[Tensor | list, dict]: (损失值或损失列表, 各视图细节字典)。
        """
        gt_anchor = gts[0]
        pred_anchor = preds[0]

        total_loss = []
        details = {}
        monitoring = {}

        for i in range(len(gts)):
            gt_other = gts[i]
            pred_other = preds[i]

            gt_pts1, gt_pts_other, pr_pts1, pr_pts_other, valid1, valid_other = self.get_pts3d_for_view(
                gt_anchor, pred_anchor, gt_other, pred_other, **kw
            )  # FIXME: this makes all other views than the anchor view to be under-trained b/c they are normalized more heavily
            loss = self.criterion(pr_pts_other[valid_other], gt_pts_other[valid_other])
            total_loss.append((loss, valid_other))

            self_name = type(self).__name__
            details[self_name + f"_pts3d_{i}_loss"] = float(loss.mean())

        return Sum(*total_loss), details


class Regr3DMultiviewV2(Criterion, MultiLoss):
    """Ensure that all 3D points are correct for multiple views.
    The point clouds from all views are concatenated together for normalization,
    but loss is calculated separately for each view.
    Compared to Regr3DMultiview, this version uses a common normalization factor for all views.
    """

    def __init__(self, criterion, norm_mode="avg_dis", gt_scale=False):
        super().__init__(criterion)
        self.norm_mode = norm_mode
        self.gt_scale = gt_scale

    def get_pts3d_from_views(self, gt_views, pred_views, dist_clip=None):
        """Get point clouds and valid masks for multiple views."""
        gt_pts_list = []
        pr_pts_list = []
        valid_mask_list = []

        # calculate the inverse transformation for the anchor view (first view)
        inv_matrix_anchor = inv(gt_views[0]["camera_pose"].float())

        for gt_view, pred_view in zip(gt_views, pred_views):
            gt_pts = geotrf(inv_matrix_anchor, gt_view["pts3d"])  # Transform GT points to anchor view
            valid_gt = gt_view["valid_mask"].clone()

            if dist_clip is not None:
                # Remove points that are too far away
                dis = gt_pts.norm(dim=-1)
                valid_gt &= dis <= dist_clip

            pr_pts = pred_view["pts3d_in_other_view"]  # Simplified for this use case

            gt_pts_list.append(gt_pts)
            pr_pts_list.append(pr_pts)
            valid_mask_list.append(valid_gt)

        # Normalize if required
        if self.norm_mode:
            pr_pts_list = self.normalize_pointcloud_from_views(pr_pts_list, self.norm_mode, valid_mask_list)
            if not self.gt_scale:
                gt_pts_list = self.normalize_pointcloud_from_views(gt_pts_list, self.norm_mode, valid_mask_list)

        return gt_pts_list, pr_pts_list, valid_mask_list

    def normalize_pointcloud_from_views(self, pts_list, norm_mode="avg_dis", valid_list=None):
        """Normalize point clouds from multiple views, excluding invalid points from normalization."""
        assert all(pts.ndim >= 3 and pts.shape[-1] == 3 for pts in pts_list)

        norm_mode, dis_mode = norm_mode.split("_")

        # Concatenate all point clouds and valid masks if provided
        all_pts = torch.cat(pts_list, dim=1)
        if valid_list is not None:
            all_valid = torch.cat(valid_list, dim=1)
            valid_pts = all_pts[all_valid]  # Keep only valid points for norm calculation
        else:
            valid_pts = all_pts

        # Compute the distance to the origin for valid points
        dis = valid_pts.norm(dim=-1)

        # Apply distance transformation based on dis_mode
        if dis_mode == "dis":
            pass  # Do nothing
        elif dis_mode == "log1p":
            dis = torch.log1p(dis)
        elif dis_mode == "warp-log1p":
            log_dis = torch.log1p(dis)
            warp_factor = log_dis / dis.clip(min=1e-8)
            all_pts = all_pts * warp_factor.view(-1, 1)  # Warp the points with the warp factor
            dis = log_dis  # The final distance is now the log-transformed distance
        else:
            raise ValueError(f"Unsupported distance mode: {dis_mode}")

        # Apply different normalization modes
        if norm_mode == "avg":
            norm_factor = dis.mean()  # Compute mean distance of valid points
        elif norm_mode == "median":
            norm_factor = dis.median()  # Compute median distance of valid points
        else:
            raise ValueError(f"Unsupported normalization mode: {norm_mode}")

        norm_factor = norm_factor.clip(min=1e-8)  # Prevent division by zero

        # Normalize all point clouds
        normalized_pts = [torch.where(valid.unsqueeze(-1), pts / norm_factor, pts)
                          for pts, valid in zip(pts_list, valid_list)]

        return normalized_pts

    def compute_loss(self, gts, preds, **kw):
        """Compute the loss by normalizing the point clouds across views and logging each view's loss."""
        total_loss = []
        details = {}
        self_name = "Regr3DMultiview"

        # Get the individual points for each view
        gt_pts_list, pr_pts_list, valid_mask_list = self.get_pts3d_from_views(gts, preds, **kw)

        # Compute the loss for each view
        for i, (gt_pts, pr_pts, valid_mask) in enumerate(zip(gt_pts_list, pr_pts_list, valid_mask_list)):
            loss = self.criterion(pr_pts[valid_mask], gt_pts[valid_mask])
            total_loss.append((loss, valid_mask))

            # Log loss for this view
            details[self_name + f"_pts3d_{i}_loss"] = float(loss.mean())

        return Sum(*total_loss), details


class Regr3DMultiviewV3(Criterion, MultiLoss):
    """Ensure that all 3D points are correct for multiple views.
    The point clouds from all views are concatenated together for normalization,
    but loss is calculated separately for each view.
    This version supports an additional local head for local coordinate systems.
    """

    def __init__(self, criterion, norm_mode="avg_dis", gt_scale=False):
        super().__init__(criterion)
        self.norm_mode = norm_mode
        self.gt_scale = gt_scale

    def get_pts3d_from_views(self, gt_views, pred_views, dist_clip=None, local=False):
        """Get point clouds and valid masks for multiple views."""
        gt_pts_list = []
        pr_pts_list = []
        valid_mask_list = []

        if not local:  # compute the inverse transformation for the anchor view (first view)
            inv_matrix_anchor = inv(gt_views[0]["camera_pose"].float())

        for gt_view, pred_view in zip(gt_views, pred_views):
            if local:
                # Rotate GT points to align with the local camera origin for supervision
                inv_matrix_local = inv(gt_view["camera_pose"].float())
                gt_pts = geotrf(inv_matrix_local, gt_view["pts3d"])  # Transform GT points to local view's origin
                pr_pts = pred_view.get("pts3d_local")  # Local predicted points
            else:
                # Use the anchor view (first view) transformation for global loss
                gt_pts = geotrf(inv_matrix_anchor, gt_view["pts3d"])  # Transform GT points to anchor view
                pr_pts = pred_view.get("pts3d_in_other_view")  # Predicted points in anchor view

            valid_gt = gt_view["valid_mask"].clone()

            if dist_clip is not None:
                dis = gt_pts.norm(dim=-1)
                valid_gt &= dis <= dist_clip

            gt_pts_list.append(gt_pts)
            pr_pts_list.append(pr_pts)
            valid_mask_list.append(valid_gt)

        return gt_pts_list, pr_pts_list, valid_mask_list

    def normalize_pointcloud_from_views(self, pts_list, norm_mode="avg_dis", valid_list=None):
        """Normalize point clouds from multiple views, excluding invalid points from normalization."""
        assert all(pts.ndim >= 3 and pts.shape[-1] == 3 for pts in pts_list)

        norm_mode, dis_mode = norm_mode.split("_")

        # Concatenate all point clouds and valid masks if provided
        all_pts = torch.cat(pts_list, dim=1)
        if valid_list is not None:
            all_valid = torch.cat(valid_list, dim=1)
            valid_pts = all_pts[all_valid]  # Keep only valid points for norm calculation
        else:
            valid_pts = all_pts

        # Compute the distance to the origin for valid points
        dis = valid_pts.norm(dim=-1)

        # Apply distance transformation based on dis_mode
        if dis_mode == "dis":
            pass  # Do nothing
        elif dis_mode == "log1p":
            dis = torch.log1p(dis)
        elif dis_mode == "warp-log1p":
            log_dis = torch.log1p(dis)
            warp_factor = log_dis / dis.clip(min=1e-8)
            all_pts = all_pts * warp_factor.view(-1, 1)  # Warp the points with the warp factor
            dis = log_dis  # The final distance is now the log-transformed distance
        else:
            raise ValueError(f"Unsupported distance mode: {dis_mode}")

        # Apply different normalization modes
        if norm_mode == "avg":
            norm_factor = dis.mean()  # Compute mean distance of valid points
        elif norm_mode == "median":
            norm_factor = dis.median()  # Compute median distance of valid points
        else:
            raise ValueError(f"Unsupported normalization mode: {norm_mode}")

        norm_factor = norm_factor.clip(min=1e-8)  # Prevent division by zero

        # Normalize all point clouds
        normalized_pts = [torch.where(valid.unsqueeze(-1), pts / norm_factor, pts)
                          for pts, valid in zip(pts_list, valid_list)]

        return normalized_pts

    def normalize_pointcloud_per_view(self, pts_list, norm_mode="avg_dis", valid_list=None):
        """Normalize point clouds on a per-view basis."""
        norm_mode, dis_mode = norm_mode.split("_")

        normed_pts_list = []
        for pts, valid in zip(pts_list, valid_list):
            if valid is not None:
                valid_pts = pts[valid]
            else:
                valid_pts = pts

            dis = valid_pts.norm(dim=-1)

            # Apply distance transformation based on dis_mode
            if dis_mode == "dis":
                pass  # Do nothing
            elif dis_mode == "log1p":
                dis = torch.log1p(dis)
            elif dis_mode == "warp-log1p":
                log_dis = torch.log1p(dis)
                warp_factor = log_dis / dis.clip(min=1e-8)
                pts = pts * warp_factor.view(-1, 1)  # Warp the points with the warp factor
                dis = log_dis  # The final distance is now the log-transformed distance
            else:
                raise ValueError(f"Unsupported distance mode: {dis_mode}")

            if norm_mode == "avg":
                norm_factor = dis.mean()  # Per-view normalization
            elif norm_mode == "median":
                norm_factor = dis.median()
            else:
                raise ValueError(f"Unsupported normalization mode: {norm_mode}")

            norm_factor = norm_factor.clip(min=1e-8)  # Avoid division by zero

            normed_pts_list.append(torch.where(valid.unsqueeze(-1), pts / norm_factor, pts))

        return normed_pts_list

    def compute_loss(self, gts, preds, **kw):
        total_loss = []
        details = {}
        self_name = "Regr3DMultiviewV3"

        # Compute loss for pts3d_in_other_view (global loss)
        gt_pts_list, pr_pts_list, valid_mask_list = self.get_pts3d_from_views(gts, preds, **kw)

        if self.norm_mode:
            pr_pts_list = self.normalize_pointcloud_from_views(pr_pts_list, self.norm_mode, valid_mask_list)
            if not self.gt_scale:
                gt_pts_list = self.normalize_pointcloud_from_views(gt_pts_list, self.norm_mode, valid_mask_list)

        # Compute loss for each view in global coordinate system
        for i, (gt_pts, pr_pts, valid_mask) in enumerate(zip(gt_pts_list, pr_pts_list, valid_mask_list)):
            loss = self.criterion(pr_pts[valid_mask], gt_pts[valid_mask])
            total_loss.append((loss, valid_mask, "global"))
            details[self_name + f"_pts3d_loss_global/{i:02d}"] = float(loss.mean())

        # Check if local loss is needed (i.e., `pts3d_local` and `conf_local` exist in preds)
        if "pts3d_local" in preds[0]:
            # Compute loss for pts3d_local (local loss)
            gt_pts_list_local, pr_pts_list_local, valid_mask_list_local = self.get_pts3d_from_views(gts, preds, local=True, **kw)

            # Normalize per-view for local coordinate system
            pr_pts_list_local = self.normalize_pointcloud_per_view(pr_pts_list_local, self.norm_mode, valid_mask_list_local)
            if not self.gt_scale:
                gt_pts_list_local = self.normalize_pointcloud_per_view(gt_pts_list_local, self.norm_mode, valid_mask_list_local)

            # Compute loss for each view in its local coordinate system
            for i, (gt_pts, pr_pts, valid_mask) in enumerate(zip(gt_pts_list_local, pr_pts_list_local, valid_mask_list_local)):
                loss_local = self.criterion(pr_pts[valid_mask], gt_pts[valid_mask])
                total_loss.append((loss_local, valid_mask, "local"))
                details[self_name + f"_pts3d_loss_local/{i:02d}"] = float(loss_local.mean())

        return Sum(*total_loss), details


class ConfLossMultiview(MultiLoss):
    """多视角置信度加权回归损失。

    利用模型学习到的置信度对逐像素损失加权，高置信区域损失惩罚更大。

    原理::

        high conf = 0.1 -> conf_loss = x/10 + alpha*log(10)  (高置信，低惩罚)
        low  conf = 10  -> conf_loss = x*10 - alpha*log(10)  (低置信，高惩罚)
    """

    def __init__(self, pixel_loss, alpha=1):
        """初始化 ConfLossMultiview。

        Args:
            pixel_loss (MultiLoss): 底层像素级回归损失（会被设为 reduction='none'）。
            alpha (float): 置信度正则化超参数，必须 > 0。
        """
        super().__init__()
        assert alpha > 0
        self.alpha = alpha
        self.pixel_loss = pixel_loss.with_reduction("none")

    def get_name(self):
        """返回损失名称。

        Returns:
            str: 形如 'ConfLossMultiview(pixel_loss)' 的名称。
        """
        return f"ConfLossMultiview({self.pixel_loss})"

    def get_conf_log(self, x):
        """返回置信度及其对数。

        Args:
            x (Tensor): 置信度预测值（正数）。

        Returns:
            tuple[Tensor, Tensor]: (conf, log_conf)。
        """
        return x, torch.log(x)

    def compute_loss(self, gts, preds, **kw):
        """计算置信度加权的多视角损失。

        Args:
            gts (list[dict]): 所有视图的真实标注。
            preds (list[dict]): 所有视图的模型预测，需包含 'conf' 键。
            **kw: 传递给 pixel_loss 的额外参数。

        Returns:
            tuple[Tensor, dict]: (总置信损失, 各视图细节字典)。
        """
        # compute per-pixel loss for all views
        total_loss, details = self.pixel_loss(gts, preds, **kw)

        total_conf_loss = 0
        conf_details = {}

        for i, (loss, mask) in enumerate(total_loss):
            # weight by confidence
            conf, log_conf = self.get_conf_log(preds[i]["conf"][mask])
            conf_loss = loss * conf - self.alpha * log_conf
            conf_loss = conf_loss.mean() if conf_loss.numel() > 0 else 0

            self_name = type(self).__name__
            conf_details[self_name + f"_conf_loss_{i}"] = float(conf_loss)

            total_conf_loss += conf_loss

        details.update(conf_details)
        return total_conf_loss, details


class ConfLossMultiviewV2(MultiLoss):
    """多视角置信度加权回归损失（V2），分别对全局和局部损失归一化。

    在 V1 基础上区分 'global' 和 'local' 损失类型，分别索引对应置信度
    ('conf' vs 'conf_local')，并按全局/局部数量各自归一化。
    """

    def __init__(self, pixel_loss, alpha=1):
        """初始化 ConfLossMultiviewV2。

        Args:
            pixel_loss (MultiLoss): 底层像素级损失（通常为 Regr3DMultiviewV3）。
            alpha (float): 置信度正则化超参数，必须 > 0。
        """
        super().__init__()
        assert alpha > 0
        self.alpha = alpha
        self.pixel_loss = pixel_loss.with_reduction("none")

    def get_name(self):
        """返回损失名称。

        Returns:
            str: 形如 'ConfLossMultiviewV2(pixel_loss)' 的名称。
        """
        return f"ConfLossMultiviewV2({self.pixel_loss})"

    def get_conf_log(self, x):
        """返回置信度及其对数。

        Args:
            x (Tensor): 置信度预测值（正数）。

        Returns:
            tuple[Tensor, Tensor]: (conf, log_conf)。
        """
        return x, torch.log(x)

    def compute_loss(self, gts, preds, **kw):
        """计算分离全局/局部路径的置信度加权损失。

        Args:
            gts (list[dict]): 所有视图的真实标注。
            preds (list[dict]): 所有视图的模型预测，
                全局路径需包含 'conf'，局部路径需包含 'conf_local'。
            **kw: 传递给 pixel_loss 的额外参数。

        Returns:
            tuple[Tensor, dict]: (归一化后的总置信损失, 细节字典)。
        """
        # compute per-pixel loss for all views
        total_loss, details = self.pixel_loss(gts, preds, **kw)

        total_conf_loss = 0
        conf_details = {}
        self_name = type(self).__name__

        # Separate counters for global and local losses
        global_count = 0
        local_count = 0

        for loss, mask, loss_type in total_loss:
            if loss_type == "global":
                conf_key = "conf"
                conf, log_conf = self.get_conf_log(preds[global_count][conf_key][mask])
                conf_loss = loss * conf - self.alpha * log_conf
                conf_loss = conf_loss.mean() if conf_loss.numel() > 0 else 0

                conf_details[self_name + f"_conf_loss_global/{global_count:02d}"] = float(conf_loss)

                global_count += 1

            elif loss_type == "local":
                conf_key = "conf_local"
                conf, log_conf = self.get_conf_log(preds[local_count][conf_key][mask])
                conf_loss = loss * conf - self.alpha * log_conf
                conf_loss = conf_loss.mean() if conf_loss.numel() > 0 else 0

                conf_details[self_name + f"_conf_loss_local/{local_count:02d}"] = float(conf_loss)

                local_count += 1

            total_conf_loss += conf_loss

        if local_count > 0:
            assert local_count == global_count, "Mismatch between the number of local and global losses."

        # Normalize total_conf_loss by the number of global and local losses separately
        total_conf_loss /= (global_count + local_count)

        details.update(conf_details)
        return total_conf_loss, details

class ConfLoss(MultiLoss):
    """双视角置信度加权回归损失（原始版本）。

    利用模型学习到的置信度对逐像素损失加权，适用于双视角 (view1, view2) 场景。

    原理::

        high conf = 0.1 -> conf_loss = x/10 + alpha*log(10)
        low  conf = 10  -> conf_loss = x*10 - alpha*log(10)
    """

    def __init__(self, pixel_loss, alpha=1):
        """初始化 ConfLoss。

        Args:
            pixel_loss (MultiLoss): 底层像素级回归损失。
            alpha (float): 置信度正则化超参数，必须 > 0。
        """
        super().__init__()
        assert alpha > 0
        self.alpha = alpha
        self.pixel_loss = pixel_loss.with_reduction("none")

    def get_name(self):
        """返回损失名称。

        Returns:
            str: 形如 'ConfLoss(pixel_loss)' 的名称。
        """
        return f"ConfLoss({self.pixel_loss})"

    def get_conf_log(self, x):
        """返回置信度及其对数。

        Args:
            x (Tensor): 置信度预测值（正数）。

        Returns:
            tuple[Tensor, Tensor]: (conf, log_conf)。
        """
        return x, torch.log(x)

    def compute_loss(self, gt1, gt2, pred1, pred2, **kw):
        """计算双视角置信度加权损失。

        Args:
            gt1 (dict): view1 的真实标注。
            gt2 (dict): view2 的真实标注。
            pred1 (dict): view1 的模型预测，需包含 'conf' 键。
            pred2 (dict): view2 的模型预测，需包含 'conf' 键。
            **kw: 传递给 pixel_loss 的额外参数。

        Returns:
            tuple[Tensor, dict]: (总置信损失, 细节字典)。
        """
        # compute per-pixel loss
        ((loss1, msk1), (loss2, msk2)), details = self.pixel_loss(
            gt1, gt2, pred1, pred2, **kw
        )
        if loss1.numel() == 0:
            print("NO VALID POINTS in img1", force=True)
        if loss2.numel() == 0:
            print("NO VALID POINTS in img2", force=True)

        # weight by confidence
        conf1, log_conf1 = self.get_conf_log(pred1["conf"][msk1])
        conf2, log_conf2 = self.get_conf_log(pred2["conf"][msk2])
        conf_loss1 = loss1 * conf1 - self.alpha * log_conf1
        conf_loss2 = loss2 * conf2 - self.alpha * log_conf2

        # average + nan protection (in case of no valid pixels at all)
        conf_loss1 = conf_loss1.mean() if conf_loss1.numel() > 0 else 0
        conf_loss2 = conf_loss2.mean() if conf_loss2.numel() > 0 else 0

        return conf_loss1 + conf_loss2, dict(
            conf_loss_1=float(conf_loss1), conf_loss2=float(conf_loss2), **details
        )


class Regr3D_ShiftInv(Regr3D):
    """深度平移不变的 3D 点回归损失。

    在 Regr3D 基础上减去联合点云的中位深度，使损失对绝对深度平移不敏感。
    """

    def get_all_pts3d(self, gt1, gt2, pred1, pred2):
        """提取并进行深度平移归一化的 3D 点。

        计算并减去真实/预测点云的联合中位深度，消除深度平移影响。

        Args:
            gt1 (dict): view1 的真实标注。
            gt2 (dict): view2 的真实标注。
            pred1 (dict): view1 的模型预测。
            pred2 (dict): view2 的模型预测。

        Returns:
            tuple: (gt_pts1, gt_pts2, pred_pts1, pred_pts2, mask1, mask2, monitoring)。
        """
        # compute unnormalized points
        (
            gt_pts1,
            gt_pts2,
            pred_pts1,
            pred_pts2,
            mask1,
            mask2,
            monitoring,
        ) = super().get_all_pts3d(gt1, gt2, pred1, pred2)

        # compute median depth
        gt_z1, gt_z2 = gt_pts1[..., 2], gt_pts2[..., 2]
        pred_z1, pred_z2 = pred_pts1[..., 2], pred_pts2[..., 2]
        gt_shift_z = get_joint_pointcloud_depth(gt_z1, gt_z2, mask1, mask2)[
            :, None, None
        ]
        pred_shift_z = get_joint_pointcloud_depth(pred_z1, pred_z2, mask1, mask2)[
            :, None, None
        ]

        # subtract the median depth
        gt_z1 -= gt_shift_z
        gt_z2 -= gt_shift_z
        pred_z1 -= pred_shift_z
        pred_z2 -= pred_shift_z

        # monitoring = dict(monitoring, gt_shift_z=gt_shift_z.mean().detach(), pred_shift_z=pred_shift_z.mean().detach())
        return gt_pts1, gt_pts2, pred_pts1, pred_pts2, mask1, mask2, monitoring


class Regr3D_ScaleInv(Regr3D):
    """尺度不变的 3D 点回归损失。

    在 Regr3D 基础上对点云进行全局尺度归一化。
    若 gt_scale=True，则将预测点云强制缩放到真实尺度；
    否则真实和预测点云各自归一化到单位尺度。
    """

    def get_all_pts3d(self, gt1, gt2, pred1, pred2):
        """提取并进行尺度归一化的 3D 点。

        Args:
            gt1 (dict): view1 的真实标注。
            gt2 (dict): view2 的真实标注。
            pred1 (dict): view1 的模型预测。
            pred2 (dict): view2 的模型预测。

        Returns:
            tuple: (gt_pts1, gt_pts2, pred_pts1, pred_pts2, mask1, mask2, monitoring)。
        """
        # compute depth-normalized points
        (
            gt_pts1,
            gt_pts2,
            pred_pts1,
            pred_pts2,
            mask1,
            mask2,
            monitoring,
        ) = super().get_all_pts3d(gt1, gt2, pred1, pred2)

        # measure scene scale
        _, gt_scale = get_joint_pointcloud_center_scale(gt_pts1, gt_pts2, mask1, mask2)
        _, pred_scale = get_joint_pointcloud_center_scale(
            pred_pts1, pred_pts2, mask1, mask2
        )

        # prevent predictions to be in a ridiculous range
        pred_scale = pred_scale.clip(min=1e-3, max=1e3)

        # subtract the median depth
        if self.gt_scale:
            pred_pts1 *= gt_scale / pred_scale
            pred_pts2 *= gt_scale / pred_scale
            # monitoring = dict(monitoring, pred_scale=(pred_scale/gt_scale).mean())
        else:
            gt_pts1 /= gt_scale
            gt_pts2 /= gt_scale
            pred_pts1 /= pred_scale
            pred_pts2 /= pred_scale
            # monitoring = dict(monitoring, gt_scale=gt_scale.mean(), pred_scale=pred_scale.mean().detach())

        return gt_pts1, gt_pts2, pred_pts1, pred_pts2, mask1, mask2, monitoring


class Regr3D_ScaleShiftInv(Regr3D_ScaleInv, Regr3D_ShiftInv):
    """同时对深度平移和尺度不变的 3D 点回归损失。

    通过 MRO 先应用 Regr3D_ShiftInv（深度平移归一化），
    再应用 Regr3D_ScaleInv（尺度归一化）。
    """
    # calls Regr3D_ShiftInv first, then Regr3D_ScaleInv
    pass
