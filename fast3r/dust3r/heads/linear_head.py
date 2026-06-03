# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (C) 2024-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).
#
# --------------------------------------------------------
# linear head implementation for DUST3R
# --------------------------------------------------------
import torch.nn as nn
import torch.nn.functional as F

from fast3r.dust3r.heads.postprocess import postprocess


class LinearPts3d(nn.Module):
    """
    Linear head for dust3r
    Each token outputs: - 16x16 3D points (+ confidence)
    """

    def __init__(self, net, has_conf=False):
        """初始化线性预测头。

        Args:
            net: 父模型，用于获取 patch_size、dec_embed_dim 等参数。
            has_conf (bool): 是否输出置信度通道。默认 False。
        """
        super().__init__()
        self.patch_size = net.patch_embed.patch_size[0]
        self.depth_mode = net.depth_mode
        self.conf_mode = net.conf_mode
        self.has_conf = has_conf

        self.proj = nn.Linear(net.dec_embed_dim, (3 + has_conf) * self.patch_size**2)

    def setup(self, croconet):
        """设置函数（当前为空实现，保留接口兼容性）。"""
        pass

    def forward(self, decout, img_shape):
        """线性前向传播，将解码器输出映射为像素级 3D 点和可选置信度。

        Args:
            decout (list[Tensor]): 解码器各层输出，使用最后一层。
            img_shape (tuple): 图像尺寸 (H, W)。

        Returns:
            dict: 包含 'pts3d' 和可选 'conf' 的预测结果。
        """
        H, W = img_shape
        tokens = decout[-1]
        B, S, D = tokens.shape

        # extract 3D points
        feat = self.proj(tokens)  # B,S,D
        feat = feat.transpose(-1, -2).view(
            B, -1, H // self.patch_size, W // self.patch_size
        )
        feat = F.pixel_shuffle(feat, self.patch_size)  # B,3,H,W

        # permute + norm depth
        return postprocess(feat, self.depth_mode, self.conf_mode)
