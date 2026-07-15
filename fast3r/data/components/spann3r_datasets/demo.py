# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""演示脚本。"""

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import os
import cv2
import numpy as np
import os.path as osp
from collections import deque

from fast3r.dust3r.utils.image import imread_cv2
from .base_many_view_dataset import BaseManyViewDataset


class Demo(BaseManyViewDataset):
    """演示用多视图数据集，从本地图像目录加载。"""

    def __init__(self, num_seq=1, num_frames=5, 
                 min_thresh=10, max_thresh=100,
                 full_video=True, kf_every=1, 
                 *args, ROOT, **kwargs):
        """初始化 Demo 数据集。

        Args:
            num_seq (int): 序列数量。默认 1。
            num_frames (int): 每个序列的帧数。默认 5。
            min_thresh (int): 帧间距最小阈值。默认 10。
            max_thresh (int): 帧间距最大阈值。默认 100。
            full_video (bool): 是否使用完整视频。默认 True。
            kf_every (int): 关键帧间隔。默认 1。
            ROOT (str): 图像目录路径。
        """
        
        self.ROOT = ROOT
        super().__init__(*args, **kwargs)

        self.num_seq = num_seq
        self.num_frames = num_frames
        self.max_thresh = max_thresh
        self.min_thresh = min_thresh
        self.full_video = full_video
        self.kf_every = kf_every
    
    def __len__(self):
        """返回序列数量。"""
        return self.num_seq
    
    def _get_views(self, idx, resolution, rng):
        """获取指定索引的多个视图。"""
        
        img_idxs = sorted(os.listdir(self.ROOT))
        valid_extensions = {'.jpg', '.jpeg', '.png', '.heic'}
        img_idxs = [idx for idx in img_idxs 
                    if idx.lower().endswith(tuple(valid_extensions)) and 'depth' not in idx.lower()]

        img_idxs = self.sample_frame_idx(img_idxs, rng, full_video=self.full_video)

        # pseudo intrinsics
        fx, fy = 1.0, 1.0

        views = []
        imgs_idxs = deque(img_idxs)

        while len(imgs_idxs) > 0:
            im_idx = imgs_idxs.popleft()

            impath = osp.join(self.ROOT, im_idx)
            if not osp.exists(impath):
                raise FileNotFoundError(f"Image not found: {impath}")

            print(f'Loading image: {impath}')

            if 'heic' in impath.lower():
                from PIL import Image
                rgb_image = Image.open(impath)
                if rgb_image.mode != 'RGB':
                    rgb_image = rgb_image.convert('RGB')
                rgb_image = np.array(rgb_image)
            else:
                rgb_image = imread_cv2(impath)
            

            depth_path = impath.split('.')[0] + '_depth.png'
            meta_data_path = impath.split('.')[0] + '.npz'

            if osp.exists(meta_data_path):
                input_metadata = np.load(meta_data_path)
                camera_pose = input_metadata['camera_pose'].astype(np.float32)
                intrinsics = input_metadata['camera_intrinsics'].astype(np.float32)
            else:
                cx, cy = rgb_image.shape[1]//2, rgb_image.shape[0]//2
                intrinsics = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], dtype=np.float32)

                # pseudo camera pose
                camera_pose = np.eye(4).astype(np.float32)
            
            if not osp.exists(depth_path):
                depthmap = np.ones((rgb_image.shape[0], rgb_image.shape[1])).astype(np.float32)
            else:
                depthmap = imread_cv2(depth_path, cv2.IMREAD_UNCHANGED)
                depthmap = (depthmap.astype(np.float32) / 65535) * np.nan_to_num(input_metadata['maximum_depth'])
            # resize rgb to the same size as depth
            rgb_image = cv2.resize(rgb_image, (depthmap.shape[1], depthmap.shape[0]))

            rgb_image, depthmap, intrinsics = self._crop_resize_if_necessary(
                rgb_image, depthmap, intrinsics, resolution, rng=rng, info=impath)

            views.append(dict(
                img=rgb_image,
                depthmap=depthmap,
                camera_pose=camera_pose,
                camera_intrinsics=intrinsics,
                dataset='demo',
                label=osp.join(self.ROOT, im_idx),
                instance=osp.split(impath)[1],
            ))
        return views


