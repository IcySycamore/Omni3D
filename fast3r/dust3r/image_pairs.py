# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""图像对处理。"""

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (C) 2024-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).
#
# --------------------------------------------------------
# utilities needed to load image pairs
# --------------------------------------------------------
import numpy as np
import torch


def make_pairs(imgs, scene_graph="complete", prefilter=None, symmetrize=True):
    """根据场景图策略从图像列表中生成图像对。

    支持 ``complete``（全连接）、``swin-{winsize}``（滑动窗口）、
    ``oneref-{refid}``（以某张图为参考）等策略。

    Args:
        imgs (list): 图像字典列表。
        scene_graph (str): 场景图类型，默认 ``"complete"``。
        prefilter (str | None): 预过滤策略，如 ``"seq{N}"`` 或 ``"cyc{N}"``。
        symmetrize (bool): 是否对图像对进行对称化（双向）。

    Returns:
        list[tuple]: 图像对列表。
    """
    pairs = []
    if scene_graph == "complete":  # complete graph
        for i in range(len(imgs)):
            for j in range(i):
                pairs.append((imgs[i], imgs[j]))
    elif scene_graph.startswith("swin"):
        winsize = int(scene_graph.split("-")[1]) if "-" in scene_graph else 3
        pairsid = set()
        for i in range(len(imgs)):
            for j in range(1, winsize + 1):
                idx = (i + j) % len(imgs)  # explicit loop closure
                pairsid.add((i, idx) if i < idx else (idx, i))
        for i, j in pairsid:
            pairs.append((imgs[i], imgs[j]))
    elif scene_graph.startswith("oneref"):
        refid = int(scene_graph.split("-")[1]) if "-" in scene_graph else 0
        for j in range(len(imgs)):
            if j != refid:
                pairs.append((imgs[refid], imgs[j]))
    if symmetrize:
        pairs += [(img2, img1) for img1, img2 in pairs]

    # now, remove edges
    if isinstance(prefilter, str) and prefilter.startswith("seq"):
        pairs = filter_pairs_seq(pairs, int(prefilter[3:]))

    if isinstance(prefilter, str) and prefilter.startswith("cyc"):
        pairs = filter_pairs_seq(pairs, int(prefilter[3:]), cyclic=True)

    return pairs


def sel(x, kept):
    """根据索引 ``kept`` 从张量、数组或字典中选择元素。

    Args:
        x (dict | Tensor | ndarray | tuple | list): 输入数据。
        kept (list[int]): 要保留的索引列表。

    Returns:
        与输入同类型的筛选后数据。
    """
    if isinstance(x, dict):
        return {k: sel(v, kept) for k, v in x.items()}
    if isinstance(x, (torch.Tensor, np.ndarray)):
        return x[kept]
    if isinstance(x, (tuple, list)):
        return type(x)([x[k] for k in kept])


def _filter_edges_seq(edges, seq_dis_thr, cyclic=False):
    """根据序列距离阈值过滤边（内部辅助函数）。

    Args:
        edges (list[tuple]): 边列表，每个边为 (i, j) 索引对。
        seq_dis_thr (int): 最大允许序列距离。
        cyclic (bool): 是否考虑循环距离。

    Returns:
        list[int]: 保留下来的边在原始列表中的索引。
    """
    # number of images
    n = max(max(e) for e in edges) + 1

    kept = []
    for e, (i, j) in enumerate(edges):
        dis = abs(i - j)
        if cyclic:
            dis = min(dis, abs(i + n - j), abs(i - n - j))
        if dis <= seq_dis_thr:
            kept.append(e)
    return kept


def filter_pairs_seq(pairs, seq_dis_thr, cyclic=False):
    """根据序列距离阈值过滤图像对。

    Args:
        pairs (list[tuple]): 图像对列表。
        seq_dis_thr (int): 最大允许序列距离。
        cyclic (bool): 是否考虑循环距离。

    Returns:
        list[tuple]: 过滤后的图像对列表。
    """
    edges = [(img1["idx"], img2["idx"]) for img1, img2 in pairs]
    kept = _filter_edges_seq(edges, seq_dis_thr, cyclic=cyclic)
    return [pairs[i] for i in kept]


def filter_edges_seq(view1, view2, pred1, pred2, seq_dis_thr, cyclic=False):
    """根据序列距离阈值过滤视图和预测结果中的边。

    Args:
        view1 (dict): 第一组视图字典。
        view2 (dict): 第二组视图字典。
        pred1 (dict): 第一组预测结果。
        pred2 (dict): 第二组预测结果。
        seq_dis_thr (int): 最大允许序列距离。
        cyclic (bool): 是否考虑循环距离。

    Returns:
        tuple: (filtered_view1, filtered_view2, filtered_pred1, filtered_pred2)。
    """
    edges = [(int(i), int(j)) for i, j in zip(view1["idx"], view2["idx"])]
    kept = _filter_edges_seq(edges, seq_dis_thr, cyclic=cyclic)
    print(
        f">> Filtering edges more than {seq_dis_thr} frames apart: kept {len(kept)}/{len(edges)} edges"
    )
    return sel(view1, kept), sel(view2, kept), sel(pred1, kept), sel(pred2, kept)
