# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""损失函数。"""

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (C) 2022-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).
#
# --------------------------------------------------------
# Criterion to train CroCo
# --------------------------------------------------------
# References:
# MAE: https://github.com/facebookresearch/mae
# --------------------------------------------------------

import torch


class MaskedMSE(torch.nn.Module):
    """带掩码的 MSE 损失函数，用于 CroCo 预训练。

    支持对被掩码的 patch 计算损失，并可选地按 patch 像素均值/方差归一化。
    """

    def __init__(self, norm_pix_loss=False, masked=True):
        """初始化 MaskedMSE。

        Args:
            norm_pix_loss (bool): 是否按 patch 像素均值/方差归一化。默认 False。
            masked (bool): 是否仅在被掩码的 patch 上计算损失。默认 True。
        """
        super().__init__()
        self.norm_pix_loss = norm_pix_loss
        self.masked = masked

    def forward(self, pred, mask, target):
        """计算带掩码的 MSE 损失。

        Args:
            pred (Tensor): 预测值。
            mask (BoolTensor): 掩码，True 表示被掩码的位置。
            target (Tensor): 目标值。

        Returns:
            Tensor: 标量损失值。
        """
        if self.norm_pix_loss:
            mean = target.mean(dim=-1, keepdim=True)
            var = target.var(dim=-1, keepdim=True)
            target = (target - mean) / (var + 1.0e-6) ** 0.5

        loss = (pred - target) ** 2
        loss = loss.mean(dim=-1)  # [N, L], mean loss per patch
        if self.masked:
            loss = (loss * mask).sum() / mask.sum()  # mean loss on masked patches
        else:
            loss = loss.mean()  # mean loss
        return loss
