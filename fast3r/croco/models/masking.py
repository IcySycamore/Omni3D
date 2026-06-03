# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (C) 2022-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).


# --------------------------------------------------------
# Masking utils
# --------------------------------------------------------

import torch
import torch.nn as nn


class RandomMask(nn.Module):
    """
    random masking
    """

    def __init__(self, num_patches, mask_ratio):
        """初始化随机掩码生成器。

        Args:
            num_patches (int): patch 总数。
            mask_ratio (float): 掩码比例 (0~1)。
        """
        super().__init__()
        self.num_patches = num_patches
        self.num_mask = int(mask_ratio * self.num_patches)

    def __call__(self, x):
        """生成随机掩码。

        Args:
            x (Tensor): 输入张量（仅用于获取设备和批次大小）。

        Returns:
            BoolTensor: 形状 (B, num_patches) 的随机掩码。
        """
        noise = torch.rand(x.size(0), self.num_patches, device=x.device)
        return argsort < self.num_mask
