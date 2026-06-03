# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (C) 2022-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).

import torch
import torchvision.transforms
import torchvision.transforms.functional as F

# "Pair": apply a transform on a pair
# "Both": apply the exact same transform to both images


class ComposePair(torchvision.transforms.Compose):
    """将多个成对变换按顺序组合。

    与 ``Compose`` 类似，但每个变换接收并返回 ``(img1, img2)`` 元组。
    """

    def __call__(self, img1, img2):
        """依次应用所有变换。

        Args:
            img1: 第一张图像。
            img2: 第二张图像。

        Returns:
            tuple: 变换后的 ``(img1, img2)``。
        """
        for t in self.transforms:
            img1, img2 = t(img1, img2)
        return img1, img2


class NormalizeBoth(torchvision.transforms.Normalize):
    """对两张图像分别执行相同的标准化（减均值除标准差）。"""

    def forward(self, img1, img2):
        """分别标准化两张图像。

        Args:
            img1 (Tensor): 第一张图像张量。
            img2 (Tensor): 第二张图像张量。

        Returns:
            tuple: 标准化后的 ``(img1, img2)``。
        """
        img1 = super().forward(img1)
        img2 = super().forward(img2)
        return img1, img2


class ToTensorBoth(torchvision.transforms.ToTensor):
    """将两张 PIL 图像分别转换为张量。"""

    def __call__(self, img1, img2):
        """分别将 PIL Image 转为 Tensor。

        Args:
            img1 (PIL.Image): 第一张图像。
            img2 (PIL.Image): 第二张图像。

        Returns:
            tuple: 转换后的 ``(img1, img2)``。
        """
        img1 = super().__call__(img1)
        img2 = super().__call__(img2)
        return img1, img2


class RandomCropPair(torchvision.transforms.RandomCrop):
    """对两张图像分别执行随机裁剪（裁剪位置不同）."""

    # the crop will be intentionally different for the two images with this class
    def forward(self, img1, img2):
        """分别随机裁剪两张图像。

        Args:
            img1 (PIL.Image | Tensor): 第一张图像。
            img2 (PIL.Image | Tensor): 第二张图像。

        Returns:
            tuple: 裁剪后的 ``(img1, img2)``。
        """
        img1 = super().forward(img1)
        img2 = super().forward(img2)
        return img1, img2


class ColorJitterPair(torchvision.transforms.ColorJitter):
    """成对颜色抖动，可对称或非对称地扰动两张图像。

    以 ``assymetric_prob`` 的概率为第二张图重新采样抖动参数。

    Args:
        assymetric_prob (float): 非对称抖动概率。
        **kwargs: 传递给 ``ColorJitter`` 的参数（brightness, contrast 等）。
    """

    def __init__(self, assymetric_prob, **kwargs):
        """初始化成对颜色抖动，设置非对称抖动概率。"""
        super().__init__(**kwargs)
        self.assymetric_prob = assymetric_prob

    def jitter_one(
        self,
        img,
        fn_idx,
        brightness_factor,
        contrast_factor,
        saturation_factor,
        hue_factor,
    ):
        """对单张图像按给定的抖动参数和函数顺序执行颜色变换。

        Args:
            img (Tensor): 输入图像。
            fn_idx (list of int): 变换执行顺序索引列表。
            brightness_factor (float | None): 亮度因子。
            contrast_factor (float | None): 对比度因子。
            saturation_factor (float | None): 饱和度因子。
            hue_factor (float | None): 色调因子。

        Returns:
            Tensor: 抖动后的图像。
        """
        for fn_id in fn_idx:
            if fn_id == 0 and brightness_factor is not None:
                img = F.adjust_brightness(img, brightness_factor)
            elif fn_id == 1 and contrast_factor is not None:
                img = F.adjust_contrast(img, contrast_factor)
            elif fn_id == 2 and saturation_factor is not None:
                img = F.adjust_saturation(img, saturation_factor)
            elif fn_id == 3 and hue_factor is not None:
                img = F.adjust_hue(img, hue_factor)
        return img

    def forward(self, img1, img2):
        """对两张图像执行颜色抖动。

        以 ``assymetric_prob`` 的概率为第二张图重新采样抖动参数，
        否则使用与第一张图相同的参数。

        Args:
            img1 (Tensor): 第一张图像。
            img2 (Tensor): 第二张图像。

        Returns:
            tuple: 抖动后的 ``(img1, img2)``。
        """
        (
            fn_idx,
            brightness_factor,
            contrast_factor,
            saturation_factor,
            hue_factor,
        ) = self.get_params(self.brightness, self.contrast, self.saturation, self.hue)
        img1 = self.jitter_one(
            img1,
            fn_idx,
            brightness_factor,
            contrast_factor,
            saturation_factor,
            hue_factor,
        )
        if torch.rand(1) < self.assymetric_prob:  # assymetric:
            (
                fn_idx,
                brightness_factor,
                contrast_factor,
                saturation_factor,
                hue_factor,
            ) = self.get_params(
                self.brightness, self.contrast, self.saturation, self.hue
            )
        img2 = self.jitter_one(
            img2,
            fn_idx,
            brightness_factor,
            contrast_factor,
            saturation_factor,
            hue_factor,
        )
        return img1, img2


def get_pair_transforms(transform_str, totensor=True, normalize=True):
    """根据字符串描述构建成对变换流水线。

    支持的变换关键字（用 ``+`` 拼接）：

    * ``crop224`` — 随机裁剪到 224×224
    * ``acolor`` — 非对称颜色抖动

    最后可选地追加 ``ToTensorBoth`` 和 ``NormalizeBoth``。

    Args:
        transform_str (str): 变换描述字符串，如 ``"crop224+acolor"``。
        totensor (bool): 是否追加 ToTensor 变换。默认 ``True``。
        normalize (bool): 是否追加 Normalize 变换。默认 ``True``。

    Returns:
        ComposePair | list | None: 变换组合。
    """
    # transform_str is eg    crop224+color
    trfs = []
    for s in transform_str.split("+"):
        if s.startswith("crop"):
            size = int(s[len("crop") :])
            trfs.append(RandomCropPair(size))
        elif s == "acolor":
            trfs.append(
                ColorJitterPair(
                    assymetric_prob=1.0,
                    brightness=(0.6, 1.4),
                    contrast=(0.6, 1.4),
                    saturation=(0.6, 1.4),
                    hue=0.0,
                )
            )
        elif s == "":  # if transform_str was ""
            pass
        else:
            raise NotImplementedError("Unknown augmentation: " + s)

    if totensor:
        trfs.append(ToTensorBoth())
    if normalize:
        trfs.append(
            NormalizeBoth(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
        )

    if len(trfs) == 0:
        return None
    elif len(trfs) == 1:
        return trfs
    else:
        return ComposePair(trfs)