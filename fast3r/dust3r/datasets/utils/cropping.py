# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (C) 2024-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).
#
# --------------------------------------------------------
# croppping utilities
# --------------------------------------------------------
import PIL.Image
import os
os.environ["OPENCV_IO_ENABLE_OPENEXR"] = "1"
import cv2  # noqa
import numpy as np  # noqa
from fast3r.dust3r.utils.geometry import colmap_to_opencv_intrinsics, opencv_to_colmap_intrinsics  # noqa
try:
    lanczos = PIL.Image.Resampling.LANCZOS
    bicubic = PIL.Image.Resampling.BICUBIC
except AttributeError:
    lanczos = PIL.Image.LANCZOS
    bicubic = PIL.Image.BICUBIC


class ImageList:
    """ Convenience class to aply the same operation to a whole set of images.
    """

    def __init__(self, images):
        """初始化 ImageList。

        Args:
            images: 单个或多个 PIL Image / numpy 数组。
        """
        if not isinstance(images, list):
            images = [images]
        self.images = []
        for image in images:
            if not isinstance(image, PIL.Image.Image):
                image = PIL.Image.fromarray(image)
            self.images.append(image)

    def __len__(self):
        """返回图像数量。"""
        return len(self.images)

    def to_pil(self):
        """返回 PIL Image 元组或单个 Image。"""
        return self.images if len(self.images) > 1 else self.images[0]

    @property
    def size(self):
        """返回所有图像的统一尺寸 (W, H)。"""
        sizes = [img.size for img in self.images]
        return sizes[0]

    def resize(self, *args, **kwargs):
        """批量调整图像大小。"""
        return self._dispatch(ImageList.resize, *args, **kwargs)

    def crop(self, *args, **kwargs):
        """批量裁剪图像。"""
        return self._dispatch(ImageList.crop, *args, **kwargs)

    def _dispatch(self, func, *args, **kwargs):
        """将方法调用分发到所有图像。"""
        return ImageList([func(img, *args, **kwargs) for img in self.images])


def rescale_image_depthmap(image, depthmap, camera_intrinsics, output_resolution, force=True):
    """ Jointly rescale a (image, depthmap) 
        so that (out_width, out_height) >= output_res
    """
    image = ImageList(image)
    input_resolution = np.array(image.size)  # (W,H)
    output_resolution = np.array(output_resolution)
    if depthmap is not None:
        # can also use this with masks instead of depthmaps
        assert tuple(depthmap.shape[:2]) == image.size[::-1]

    # define output resolution
    assert output_resolution.shape == (2,)
    scale_final = max(output_resolution / image.size) + 1e-8
    if scale_final >= 1 and not force:  # image is already smaller than what is asked
        return (image.to_pil(), depthmap, camera_intrinsics)
    output_resolution = np.floor(input_resolution * scale_final).astype(int)

    # first rescale the image so that it contains the crop
    image = image.resize(tuple(output_resolution), resample=lanczos if scale_final < 1 else bicubic)
    if depthmap is not None:
        depthmap = cv2.resize(depthmap, output_resolution, fx=scale_final,
                              fy=scale_final, interpolation=cv2.INTER_NEAREST)

    # no offset here; simple rescaling
    camera_intrinsics = camera_matrix_of_crop(
        camera_intrinsics, input_resolution, output_resolution, scaling=scale_final)

    return image.to_pil(), depthmap, camera_intrinsics


def camera_matrix_of_crop(input_camera_matrix, input_resolution, output_resolution, scaling=1, offset_factor=0.5, offset=None):
    """根据裁剪参数计算新的相机内参矩阵。

    Args:
        input_camera_matrix (ndarray): 输入相机内参 (3x3)。
        input_resolution (ndarray): 输入分辨率 (W, H)。
        output_resolution (ndarray): 输出分辨率 (W, H)。
        scaling (float): 缩放比例。默认 1。
        offset_factor (float): 偏移因子。默认 0.5。
        offset (ndarray | None): 显式偏移。

    Returns:
        ndarray: 裁剪后的相机内参矩阵 (3x3)。
    """
    margins = np.asarray(input_resolution) * scaling - output_resolution
    assert np.all(margins >= 0.0)
    if offset is None:
        offset = offset_factor * margins

    # Generate new camera parameters
    output_camera_matrix_colmap = opencv_to_colmap_intrinsics(input_camera_matrix)
    output_camera_matrix_colmap[:2, :] *= scaling
    output_camera_matrix_colmap[:2, 2] -= offset
    output_camera_matrix = colmap_to_opencv_intrinsics(output_camera_matrix_colmap)

    return output_camera_matrix


def crop_image_depthmap(image, depthmap, camera_intrinsics, crop_bbox):
    """
    Return a crop of the input view.
    """
    image = ImageList(image)
    l, t, r, b = crop_bbox

    image = image.crop((l, t, r, b))
    if depthmap is not None:
        depthmap = depthmap[t:b, l:r]

    camera_intrinsics = camera_intrinsics.copy()
    camera_intrinsics[0, 2] -= l
    camera_intrinsics[1, 2] -= t

    return image.to_pil(), depthmap, camera_intrinsics


def bbox_from_intrinsics_in_out(input_camera_matrix, output_camera_matrix, output_resolution):
    """根据输入输出相机内参计算裁剪边界框。

    Args:
        input_camera_matrix (ndarray): 输入相机内参 (3x3)。
        output_camera_matrix (ndarray): 输出相机内参 (3x3)。
        output_resolution (tuple): 输出分辨率 (W, H)。

    Returns:
        tuple: 裁剪边界框 (left, top, right, bottom)。
    """
    l, t = np.int32(np.round(input_camera_matrix[:2, 2] - output_camera_matrix[:2, 2]))
    out_width, out_height = output_resolution
    crop_bbox = (l, t, l + out_width, t + out_height)
    return crop_bbox
