# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import os
import cv2
import json
import numpy as np
import os.path as osp
from collections import deque

from fast3r.dust3r.utils.image import imread_cv2
from .base_many_view_dataset import BaseManyViewDataset


class SevenScenes(BaseManyViewDataset):
    """7-Scenes 多视图室内场景数据集，基于 SPlan3R 序列采样。"""

    def __init__(self, num_seq=1, num_frames=5, 
                 min_thresh=10, max_thresh=100, 
                 test_id=None, full_video=False, 
                 tuple_path=None, seq_id=None,
                 kf_every=1, *args, ROOT, **kwargs):
        """初始化 7-Scenes 数据集。

        Args:
            num_seq (int): 每个场景的序列数。默认 1。
            num_frames (int): 每个序列的帧数。默认 5。
            min_thresh (int): 帧间距最小阈值。默认 10。
            max_thresh (int): 帧间距最大阈值。默认 100。
            test_id: 测试场景 ID。默认 None。
            full_video (bool): 是否使用完整视频。默认 False。
            tuple_path (str | None): 预定义元组文件路径。默认 None。
            seq_id: 序列 ID。默认 None。
            kf_every (int): 关键帧间隔。默认 1。
            ROOT (str): 数据集根目录。
        """
        
        self.ROOT = ROOT
        super().__init__(*args, **kwargs)
        self.num_seq = num_seq
        self.num_frames = num_frames
        self.max_thresh = max_thresh
        self.min_thresh = min_thresh
        self.test_id = test_id
        self.full_video = full_video
        self.kf_every = kf_every
        self.seq_id = seq_id

         # load all scenes
        self.load_all_tuples(tuple_path)
        self.load_all_scenes(ROOT)
    
    def __len__(self):
        """返回数据集大小。"""
        if self.tuple_list is not None:
            return len(self.tuple_list)
        return len(self.scene_list) * self.num_seq

    def load_all_tuples(self, tuple_path):
        """加载预定义的图像元组列表。"""
        if tuple_path is not None:
            with open(tuple_path) as f:
                self.tuple_list = f.read().splitlines()
        
        else:
            self.tuple_list = None
    
    def load_all_scenes(self, base_dir):
        """加载所有场景列表。"""
        
        if self.tuple_list is not None:
            # Use pre-defined simplerecon scene_ids
            self.scene_list = ['stairs/seq-06', 'stairs/seq-02', 
                               'pumpkin/seq-06', 'chess/seq-01',
                               'heads/seq-02', 'fire/seq-02',
                               'office/seq-03', 'pumpkin/seq-03',
                               'redkitchen/seq-07', 'chess/seq-02',
                               'office/seq-01', 'redkitchen/seq-01',
                               'fire/seq-01']
            print(f"Found {len(self.scene_list)} sequences in split {self.split}")
            return 
            
        scenes = os.listdir(base_dir)
        
        file_split = {'train': 'TrainSplit.txt', 'test': 'TestSplit.txt'}[self.split]
        
        self.scene_list = []
        for scene in scenes:
            if self.test_id is not None and scene != self.test_id:
                continue
            # read file split
            with open(osp.join(base_dir, scene, file_split)) as f:
                seq_ids = f.read().splitlines()
                
                
                for seq_id in seq_ids:
                    # seq is string, take the int part and make it 01, 02, 03
                    # seq_id = 'seq-{:2d}'.format(int(seq_id))
                    num_part = ''.join(filter(str.isdigit, seq_id))
                    seq_id = f'seq-{num_part.zfill(2)}'
                    if self.seq_id is not None and seq_id != self.seq_id:
                        continue
                    self.scene_list.append(f"{scene}/{seq_id}")
        
        
        print(f"Found {len(self.scene_list)} sequences in split {self.split}")
    

    def _get_views(self, idx, resolution, rng):
        """获取指定索引的多视图数据。"""


        if self.tuple_list is not None:
            line = self.tuple_list[idx].split(" ")
            scene_id = line[0]
            img_idxs = line[1:]
        
        else:
            scene_id = self.scene_list[idx // self.num_seq]
            seq_id = idx % self.num_seq

            data_path = osp.join(self.ROOT, scene_id)
            num_files = len([name for name in os.listdir(data_path) if 'color' in name])
            img_idxs = [f'{i:06d}' for i in range(num_files)]
            img_idxs = self.sample_frame_idx(img_idxs, rng, full_video=self.full_video)
        
        # Intrinsics used in SimpleRecon
        fx, fy, cx, cy = 525, 525, 320, 240
        intrinsics_ = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], dtype=np.float32)


        views = []
        imgs_idxs = deque(img_idxs)

        while len(imgs_idxs) > 0:
            im_idx = imgs_idxs.popleft()

            impath = osp.join(self.ROOT, scene_id, f'frame-{im_idx}.color.png')
            depthpath = osp.join(self.ROOT, scene_id, f'frame-{im_idx}.depth.proj.png')
            posepath = osp.join(self.ROOT, scene_id, f'frame-{im_idx}.pose.txt')

            rgb_image = imread_cv2(impath)
            depthmap = imread_cv2(depthpath, cv2.IMREAD_UNCHANGED)
            rgb_image = cv2.resize(rgb_image, (depthmap.shape[1], depthmap.shape[0]))

            depthmap[depthmap==65535] = 0
            depthmap = np.nan_to_num(depthmap.astype(np.float32), 0.0) / 1000.0
            depthmap[depthmap>10] = 0
            depthmap[depthmap<1e-3] = 0

            
            camera_pose = np.loadtxt(posepath).astype(np.float32)

            rgb_image, depthmap, intrinsics = self._crop_resize_if_necessary(
                rgb_image, depthmap, intrinsics_, resolution, rng=rng, info=impath)
            
            views.append(dict(
                img=rgb_image,
                depthmap=depthmap,
                camera_pose=camera_pose,
                camera_intrinsics=intrinsics,
                dataset='7scenes',
                label=osp.join(scene_id, im_idx),
                instance=osp.split(impath)[1],
            ))
        return views







                    


