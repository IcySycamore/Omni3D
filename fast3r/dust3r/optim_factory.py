# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (C) 2024-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).
#
# --------------------------------------------------------
# optimization functions
# --------------------------------------------------------


def adjust_learning_rate_by_lr(optimizer, lr):
    """根据基准学习率调整优化器中所有参数组的学习率。

    支持按参数组的 lr_scale 进行缩放，未设置 lr_scale 的参数组直接使用基准学习率。

    Args:
        optimizer (torch.optim.Optimizer): 目标优化器。
        lr (float): 基准学习率。
    """
    for param_group in optimizer.param_groups:
        if "lr_scale" in param_group:
            param_group["lr"] = lr * param_group["lr_scale"]
        else:
            param_group["lr"] = lr
