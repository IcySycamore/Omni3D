# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (C) 2022-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).

import torch

try:
    import curope as _kernels  # run `python setup.py install`
except ModuleNotFoundError:
    from . import curope as _kernels  # run `python setup.py build_ext --inplace`


class cuRoPE2D_func(torch.autograd.Function):
    """2D 旋转位置编码（RoPE）的自定义 autograd 函数，使用 CUDA 内核加速。"""
    @staticmethod
    @torch.amp.custom_fwd(cast_inputs=torch.float32, device_type='cuda')
    def forward(ctx, tokens, positions, base, F0=1):
        """前向传播：对 tokens 应用 2D RoPE。"""
        ctx.save_for_backward(positions)
        ctx.saved_base = base
        ctx.saved_F0 = F0
        # tokens = tokens.clone() # uncomment this if inplace doesn't work
        _kernels.rope_2d(tokens, positions, base, F0)
        ctx.mark_dirty(tokens)
        return tokens

    @staticmethod
    @torch.amp.custom_bwd(device_type='cuda')
    def backward(ctx, grad_res):
        """反向传播：对梯度应用逆向 2D RoPE。"""
        positions, base, F0 = ctx.saved_tensors[0], ctx.saved_base, ctx.saved_F0
        _kernels.rope_2d(grad_res, positions, base, -F0)
        ctx.mark_dirty(grad_res)
        return grad_res, None, None, None


class cuRoPE2D(torch.nn.Module):
    """2D 旋转位置编码模块，封装 CUDA 加速的 RoPE 实现。"""

    def __init__(self, freq=100.0, F0=1.0):
        """初始化 cuRoPE2D。

        Args:
            freq (float): 频率基数。默认 100.0。
            F0 (float): 频率缩放因子。默认 1.0。
        """
        super().__init__()
        self.base = freq
        self.F0 = F0

    def forward(self, tokens, positions):
        """对输入 tokens 应用 2D 旋转位置编码。

        Args:
            tokens (Tensor): 输入 token 张量。
            positions (Tensor): 2D 位置张量。

        Returns:
            Tensor: 编码后的 token 张量。
        """
        cuRoPE2D_func.apply(tokens.transpose(1, 2), positions, self.base, self.F0)
        return tokens
