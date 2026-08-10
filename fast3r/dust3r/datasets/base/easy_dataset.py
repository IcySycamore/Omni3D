# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""简易数据集。"""

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (C) 2024-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).
#
# --------------------------------------------------------
# A dataset base class that you can easily resize and combine.
# --------------------------------------------------------
import numpy as np

from fast3r.dust3r.datasets.base.batched_sampler import BatchedRandomSampler


class EasyDataset:
    """a dataset that you can easily resize and combine.
    Examples:
    ---------
        2 * dataset ==> duplicate each element 2x

        10 @ dataset ==> set the size to 10 (random sampling, duplicates if necessary)

        dataset1 + dataset2 ==> concatenate datasets
    """

    def __add__(self, other):
        """拼接两个数据集。"""
        return CatDataset([self, other])

    def __rmul__(self, factor):
        """重复数据集 factor 次（如 2 * dataset）。"""
        return MulDataset(factor, self)

    def __rmatmul__(self, factor):
        """调整数据集大小为 factor（如 10 @ dataset）。"""
        return ResizedDataset(factor, self)

    def set_epoch(self, epoch):
        """设置当前 epoch（用于随机种子控制）。"""
        pass  # nothing to do by default

    def set_ratio(self, train_ratio):
        """设置训练比例。"""
        self.train_ratio = train_ratio

    def make_sampler(
        self, batch_size, shuffle=True, world_size=1, rank=0, drop_last=True
    ):
        """创建按宽高比分批的随机采样器。

        Args:
            batch_size (int): 批次大小。
            shuffle (bool): 是否打乱。默认 True。
            world_size (int): 分布式训练的进程数。默认 1。
            rank (int): 当前进程的 rank。默认 0。
            drop_last (bool): 是否丢弃不完整的批次。默认 True。

        Returns:
            BatchedRandomSampler: 采样器实例。
        """
        if not (shuffle):
            raise NotImplementedError()  # cannot deal yet
        num_of_aspect_ratios = len(self._resolutions)
        return BatchedRandomSampler(
            self,
            batch_size,
            num_of_aspect_ratios,
            world_size=world_size,
            rank=rank,
            drop_last=drop_last,
        )


class MulDataset(EasyDataset):
    """Artifically augmenting the size of a dataset."""

    multiplicator: int

    def __init__(self, multiplicator, dataset):
        """初始化重复数据集。

        Args:
            multiplicator (int): 重复倍数。
            dataset (EasyDataset): 原始数据集。
        """
        assert isinstance(multiplicator, int) and multiplicator > 0
        self.multiplicator = multiplicator
        self.dataset = dataset

    def __len__(self):
        """返回数据集大小（原始大小 × 倍数）。"""
        return self.multiplicator * len(self.dataset)

    def __repr__(self):
        """返回数据集的字符串表示。"""
        return f"{self.multiplicator}*{repr(self.dataset)}"

    def __getitem__(self, idx):
        """根据索引获取数据（取模映射回原始数据集）。"""
        if isinstance(idx, tuple):
            idx, other = idx
            return self.dataset[idx // self.multiplicator, other]
        else:
            return self.dataset[idx // self.multiplicator]

    @property
    def _resolutions(self):
        """返回原始数据集的分辨率列表。"""
        return self.dataset._resolutions


class ResizedDataset(EasyDataset):
    """Artifically changing the size of a dataset."""

    new_size: int

    def __init__(self, new_size, dataset):
        """初始化调整大小数据集。

        Args:
            new_size (int): 新的数据集大小。
            dataset (EasyDataset): 原始数据集。
        """
        assert isinstance(new_size, int) and new_size > 0
        self.new_size = new_size
        self.dataset = dataset

    def __len__(self):
        """返回调整后的数据集大小。"""
        return self.new_size

    def __repr__(self):
        """返回数据集的字符串表示。"""
        size_str = str(self.new_size)
        for i in range((len(size_str) - 1) // 3):
            sep = -4 * i - 3
            size_str = size_str[:sep] + "_" + size_str[sep:]
        return f"{size_str} @ {repr(self.dataset)}"

    def set_epoch(self, epoch):
        """设置 epoch，生成确定性随机索引映射。"""
        # this random shuffle only depends on the epoch
        rng = np.random.default_rng(seed=epoch + 777)

        # shuffle all indices
        perm = rng.permutation(len(self.dataset))

        # rotary extension until target size is met
        shuffled_idxs = np.concatenate(
            [perm] * (1 + (len(self) - 1) // len(self.dataset))
        )
        self._idxs_mapping = shuffled_idxs[: self.new_size]

        assert len(self._idxs_mapping) == self.new_size

    def set_ratio(self, train_ratio):
        """设置训练比例，传递给原始数据集。"""
        self.dataset.train_ratio = train_ratio

    def __getitem__(self, idx):
        """根据索引映射获取数据。"""
        assert hasattr(
            self, "_idxs_mapping"
        ), "You need to call dataset.set_epoch() to use ResizedDataset.__getitem__()"
        if isinstance(idx, tuple):
            idx, other = idx
            return self.dataset[self._idxs_mapping[idx], other]
        else:
            return self.dataset[self._idxs_mapping[idx]]

    @property
    def _resolutions(self):
        """返回原始数据集的分辨率列表。"""
        return self.dataset._resolutions


class CatDataset(EasyDataset):
    """Concatenation of several datasets"""

    def __init__(self, datasets):
        """初始化拼接数据集。

        Args:
            datasets (list[EasyDataset]): 要拼接的数据集列表。
        """
        for dataset in datasets:
            assert isinstance(dataset, EasyDataset)
        self.datasets = datasets
        self._cum_sizes = np.cumsum([len(dataset) for dataset in datasets])

    def __len__(self):
        """返回拼接后的数据集总大小。"""
        return self._cum_sizes[-1]

    def __repr__(self):
        """返回数据集的字符串表示。"""
        # remove uselessly long transform
        return " + ".join(
            repr(dataset).replace(
                ",transform=Compose( ToTensor() Normalize(mean=(0.5, 0.5, 0.5), std=(0.5, 0.5, 0.5)))",
                "",
            )
            for dataset in self.datasets
        )

    def set_epoch(self, epoch):
        """设置 epoch，传递给所有子数据集。"""
        for dataset in self.datasets:
            dataset.set_epoch(epoch)

    def set_ratio(self, train_ratio):
        """设置训练比例，传递给所有子数据集。"""
        for dataset in self.datasets:
            dataset.set_ratio(train_ratio)

    def __getitem__(self, idx):
        """根据索引获取数据，自动路由到对应的子数据集。"""
        other = None
        if isinstance(idx, tuple):
            idx, other = idx

        if not (0 <= idx < len(self)):
            raise IndexError()

        db_idx = np.searchsorted(self._cum_sizes, idx, "right")
        dataset = self.datasets[db_idx]
        new_idx = idx - (self._cum_sizes[db_idx - 1] if db_idx > 0 else 0)

        if other is not None:
            new_idx = (new_idx, other)
        return dataset[new_idx]

    @property
    def _resolutions(self):
        """返回所有子数据集的统一分辨率列表。"""
        resolutions = self.datasets[0]._resolutions
        for dataset in self.datasets[1:]:
            assert tuple(dataset._resolutions) == tuple(resolutions)
        return resolutions
