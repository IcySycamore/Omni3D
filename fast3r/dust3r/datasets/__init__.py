# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""数据集。"""

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (C) 2024-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).
from .utils.transforms import *
from .base.batched_sampler import BatchedRandomSampler  # noqa
from .arkitscenes import ARKitScenes  # noqa
from .arkitscenes_multiview import ARKitScenes_Multiview  # noqa
from .blendedmvs import BlendedMVS  # noqa
from .blendedmvs_multiview import BlendedMVS_Multiview  # noqa
from .co3d import Co3d  # noqa
from .co3d_multiview import Co3d_Multiview  # noqa: F401
from .habitat import Habitat  # noqa
from .habitat_multiview import Habitat_Multiview  # noqa
from .megadepth import MegaDepth  # noqa
from .megadepth_multiview import MegaDepth_Multiview  # noqa
from .scannetpp import ScanNetpp  # noqa
from .scannetpp_multiview import ScanNetpp_Multiview  # noqa
from .staticthings3d import StaticThings3D  # noqa
from .waymo import Waymo  # noqa
from .wildrgbd import WildRGBD  # noqa
from fast3r.data.components.spann3r_datasets import *  # noqa

def get_data_loader(dataset, batch_size, num_workers=8, shuffle=True, drop_last=True, pin_mem=True, persistent_workers=False, multiprocessing_context=None):
    """获取数据集加载器。

    Args:
        dataset: 数据集对象或名称字符串。
        batch_size: 每批样本数量。
        num_workers: 数据加载线程数。
        shuffle: 是否打乱数据。
        drop_last: 是否丢弃不足一个 batch 的数据。
        pin_mem: 是否使用锁页内存。
        persistent_workers: 是否保持工作进程存活。
        multiprocessing_context: 多进程上下文。

    Returns:
        torch.utils.data.DataLoader: 配置好的数据加载器。
    """
    import torch
    from croco.utils.misc import get_world_size, get_rank

    # pytorch dataset
    if isinstance(dataset, str):
        dataset = eval(dataset)

    world_size = get_world_size()
    rank = get_rank()

    try:
        sampler = dataset.make_sampler(batch_size, shuffle=shuffle, world_size=world_size,
                                       rank=rank, drop_last=drop_last)
    except (AttributeError, NotImplementedError):
        # not avail for this dataset
        if torch.distributed.is_initialized():
            sampler = torch.utils.data.DistributedSampler(
                dataset, num_replicas=world_size, rank=rank, shuffle=shuffle, drop_last=drop_last
            )
        elif shuffle:
            sampler = torch.utils.data.RandomSampler(dataset)
        else:
            sampler = torch.utils.data.SequentialSampler(dataset)

    data_loader = torch.utils.data.DataLoader(
        dataset,
        sampler=sampler,
        batch_size=batch_size,
        num_workers=num_workers,
        pin_memory=pin_mem,
        drop_last=drop_last,
        persistent_workers=persistent_workers,
        multiprocessing_context=multiprocessing_context,
    )

    return data_loader
