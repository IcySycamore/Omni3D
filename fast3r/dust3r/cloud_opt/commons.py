# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (C) 2024-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).
#
# --------------------------------------------------------
# utility functions for global alignment
# --------------------------------------------------------
import numpy as np
import torch
import torch.nn as nn


def edge_str(i, j):
    """Return a string key representing a directed edge (i -> j).

    Args:
        i (int): Source node index.
        j (int): Target node index.

    Returns:
        str: Edge identifier string in the format "i_j".
    """
    return f"{i}_{j}"


def i_j_ij(ij):
    """Convert an edge tuple to a (string_key, tuple) pair.

    Args:
        ij (tuple): A 2-tuple (i, j) representing an edge.

    Returns:
        tuple: A pair (edge_str(i, j), (i, j)).
    """
    return edge_str(*ij), ij


def edge_conf(conf_i, conf_j, edge):
    """Compute the confidence score for a single edge as the product of mean confidences.

    Args:
        conf_i (dict): Mapping from edge string key to confidence tensor for the source image.
        conf_j (dict): Mapping from edge string key to confidence tensor for the target image.
        edge (str): The edge string key.

    Returns:
        float: Product of mean confidence values for both endpoints of the edge.
    """
    return float(conf_i[edge].mean() * conf_j[edge].mean())


def compute_edge_scores(edges, conf_i, conf_j):
    """Compute confidence scores for all edges in the graph.

    Args:
        edges (iterable): Iterable of (edge_str, (i, j)) pairs.
        conf_i (dict): Per-edge confidence tensors for source images.
        conf_j (dict): Per-edge confidence tensors for target images.

    Returns:
        dict: Mapping from (i, j) tuple to float confidence score.
    """
    return {(i, j): edge_conf(conf_i, conf_j, e) for e, (i, j) in edges}


def NoGradParamDict(x):
    """Create a frozen ``nn.ParameterDict`` that does not require gradients.

    Args:
        x (dict): Dictionary mapping string keys to tensors.

    Returns:
        nn.ParameterDict: Parameter dictionary with ``requires_grad=False``.
    """
    assert isinstance(x, dict)
    return nn.ParameterDict(x).requires_grad_(False)


def get_imshapes(edges, pred_i, pred_j):
    """Infer per-image spatial shapes from pairwise predictions.

    Iterates over all edges and checks that each image has a consistent
    (H, W) shape across all edges it participates in.

    Args:
        edges (list of tuple): List of (i, j) edge tuples.
        pred_i (list of Tensor): Per-edge 3D point predictions for the source image.
        pred_j (list of Tensor): Per-edge 3D point predictions for the target image.

    Returns:
        list of tuple: List of (H, W) shape tuples, one per image.

    Raises:
        AssertionError: If the same image has conflicting shapes across edges.
    """
    n_imgs = max(max(e) for e in edges) + 1
    imshapes = [None] * n_imgs
    for e, (i, j) in enumerate(edges):
        shape_i = tuple(pred_i[e].shape[0:2])
        shape_j = tuple(pred_j[e].shape[0:2])
        if imshapes[i]:
            assert imshapes[i] == shape_i, f"incorrect shape for image {i}"
        if imshapes[j]:
            assert imshapes[j] == shape_j, f"incorrect shape for image {j}"
        imshapes[i] = shape_i
        imshapes[j] = shape_j
    return imshapes


def get_conf_trf(mode):
    """Return a confidence transformation function for the given mode.

    Args:
        mode (str): Transformation mode. One of:
            ``"log"``   – natural logarithm,
            ``"sqrt"``  – square root,
            ``"m1"``    – subtract 1,
            ``"id"`` / ``"none"`` – identity (no transformation).

    Returns:
        Callable[[Tensor], Tensor]: A function that maps a confidence tensor
        to its transformed version.

    Raises:
        ValueError: If *mode* is not one of the supported options.
    """
    if mode == "log":

        def conf_trf(x):
            return x.log()

    elif mode == "sqrt":

        def conf_trf(x):
            return x.sqrt()

    elif mode == "m1":

        def conf_trf(x):
            return x - 1

    elif mode in ("id", "none"):

        def conf_trf(x):
            return x

    else:
        raise ValueError(f"bad mode for {mode=}")
    return conf_trf


def l2_dist(a, b, weight):
    """Compute weighted squared L2 distance between two point clouds.

    Args:
        a (Tensor): First set of points, shape (..., D).
        b (Tensor): Second set of points, same shape as *a*.
        weight (Tensor): Per-point weights, shape (...).

    Returns:
        Tensor: Weighted squared Euclidean distances, shape (...).
    """
    return (a - b).square().sum(dim=-1) * weight


def l1_dist(a, b, weight):
    """Compute weighted L1 (Euclidean norm) distance between two point clouds.

    Args:
        a (Tensor): First set of points, shape (..., D).
        b (Tensor): Second set of points, same shape as *a*.
        weight (Tensor): Per-point weights, shape (...).

    Returns:
        Tensor: Weighted L2-norm distances, shape (...).
    """
    return (a - b).norm(dim=-1) * weight


ALL_DISTS = dict(l1=l1_dist, l2=l2_dist)


def signed_log1p(x):
    """Sign-preserving log1p transformation: ``sign(x) * log(1 + |x|)``.

    Args:
        x (Tensor): Input tensor.

    Returns:
        Tensor: Transformed tensor with the same sign as *x*.
    """
    sign = torch.sign(x)
    return sign * torch.log1p(torch.abs(x))


def signed_expm1(x):
    """Sign-preserving expm1 transformation: ``sign(x) * (exp(|x|) - 1)``.

    This is the inverse of :func:`signed_log1p`.

    Args:
        x (Tensor): Input tensor.

    Returns:
        Tensor: Transformed tensor with the same sign as *x*.
    """
    sign = torch.sign(x)
    return sign * torch.expm1(torch.abs(x))


def cosine_schedule(t, lr_start, lr_end):
    """Cosine annealing learning rate schedule.

    Args:
        t (float): Normalized time in [0, 1].
        lr_start (float): Initial learning rate.
        lr_end (float): Final (minimum) learning rate.

    Returns:
        float: Interpolated learning rate following a cosine curve.
    """
    assert 0 <= t <= 1
    return lr_end + (lr_start - lr_end) * (1 + np.cos(t * np.pi)) / 2


def linear_schedule(t, lr_start, lr_end):
    """Linear learning rate schedule.

    Args:
        t (float): Normalized time in [0, 1].
        lr_start (float): Initial learning rate.
        lr_end (float): Final learning rate.

    Returns:
        float: Linearly interpolated learning rate.
    """
    assert 0 <= t <= 1
    return lr_start + (lr_end - lr_start) * t
