# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (C) 2024-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).
#
# --------------------------------------------------------
# utilitary functions for DUSt3R
# --------------------------------------------------------
import torch


def fill_default_args(kwargs, func):
    """Fill missing keys in *kwargs* with the default values of *func*'s signature.

    Args:
        kwargs (dict): Keyword argument dictionary to update in-place.
        func (Callable): Function whose signature provides the defaults.

    Returns:
        dict: The updated *kwargs* dictionary.
    """
    import inspect  # a bit hacky but it works reliably

    signature = inspect.signature(func)

    for k, v in signature.parameters.items():
        if v.default is inspect.Parameter.empty:
            continue
        kwargs.setdefault(k, v.default)

    return kwargs


def freeze_all_params(modules):
    """Disable gradient computation for all parameters in the given modules.

    Args:
        modules (iterable): Iterable of ``nn.Module`` instances or
            individual ``nn.Parameter`` objects to freeze.
    """
    for module in modules:
        try:
            for n, param in module.named_parameters():
                param.requires_grad = False
        except AttributeError:
            # module is directly a parameter
            module.requires_grad = False


def is_symmetrized(gt1, gt2):
    """Check whether two view dictionaries form a symmetrized batch.

    A batch is symmetrized when each even/odd sample pair ``(x[i], y[i+1])``
    and ``(x[i+1], y[i])`` are swapped counterparts.

    Args:
        gt1 (dict): First-view batch dict containing ``'instance'`` list.
        gt2 (dict): Second-view batch dict containing ``'instance'`` list.

    Returns:
        bool: ``True`` if the batch is symmetrized, ``False`` otherwise.
    """
    x = gt1["instance"]
    y = gt2["instance"]
    if len(x) == len(y) and len(x) == 1:
        return False  # special case of batchsize 1
    ok = True
    for i in range(0, len(x), 2):
        ok = ok and (x[i] == y[i + 1]) and (x[i + 1] == y[i])
    return ok


def flip(tensor):
    """flip so that tensor[0::2] <=> tensor[1::2]"""
    return torch.stack((tensor[1::2], tensor[0::2]), dim=1).flatten(0, 1)


def interleave(tensor1, tensor2):
    """Interleave two tensors along the batch dimension.

    Returns two tensors where *res1* is ``[t1[0], t2[0], t1[1], t2[1], ...]``
    and *res2* is the reverse.

    Args:
        tensor1 (Tensor): First tensor of shape (B, ...).
        tensor2 (Tensor): Second tensor of same shape as *tensor1*.

    Returns:
        tuple: ``(res1, res2)`` each of shape (2B, ...).
    """
    res1 = torch.stack((tensor1, tensor2), dim=1).flatten(0, 1)
    res2 = torch.stack((tensor2, tensor1), dim=1).flatten(0, 1)
    return res1, res2


def transpose_to_landscape(head, activate=True):
    """Predict in the correct aspect-ratio,
    then transpose the result in landscape
    and stack everything back together.
    """

    def wrapper_no(decout, true_shape):
        """不进行横竖屏转置的包装器，直接调用预测头。"""
        B = len(true_shape)
        assert true_shape[0:1].allclose(true_shape), "true_shape must be all identical"
        H, W = true_shape[0].cpu().tolist()
        res = head(decout, (H, W))
        return res

    def wrapper_yes(decout, true_shape):
        """横竖屏转置包装器，自动检测并处理横屏/竖屏混合批次。"""
        B = len(true_shape)
        # by definition, the batch is in landscape mode so W >= H
        H, W = int(true_shape.min()), int(true_shape.max())

        height, width = true_shape.T
        is_landscape = width >= height
        is_portrait = ~is_landscape

        # true_shape = true_shape.cpu()
        if is_landscape.all():
            return head(decout, (H, W))
        if is_portrait.all():
            return transposed(head(decout, (W, H)))

        # batch is a mix of both portraint & landscape
        def selout(ar):
            """根据布尔掩码从解码器输出中选取对应元素。"""
            return [d[ar] for d in decout]

        l_result = head(selout(is_landscape), (H, W))
        p_result = transposed(head(selout(is_portrait), (W, H)))

        # allocate full result
        result = {}
        for k in l_result | p_result:
            x = l_result[k].new(B, *l_result[k].shape[1:])
            x[is_landscape] = l_result[k]
            x[is_portrait] = p_result[k]
            result[k] = x

        return result

    return wrapper_yes if activate else wrapper_no


def transposed(dic):
    """Swap axes 1 and 2 for all tensors in a dictionary (portrait <-> landscape).

    Args:
        dic (dict): Dictionary mapping string keys to tensors with at least
            3 dimensions.

    Returns:
        dict: New dictionary with the same keys and transposed tensors.
    """
    return {k: v.swapaxes(1, 2) for k, v in dic.items()}


def invalid_to_nans(arr, valid_mask, ndim=999):
    """Replace invalid (masked-out) entries with NaN.

    Args:
        arr (Tensor): Input tensor.
        valid_mask (BoolTensor or None): Boolean mask; positions where
            ``False`` are set to NaN.  If ``None``, *arr* is returned
            unchanged (after optional flattening).
        ndim (int): Maximum number of dimensions to keep; trailing
            dimensions beyond *ndim* are flattened. Defaults to ``999``.

    Returns:
        Tensor: Tensor with invalid positions set to ``float('nan')``.
    """
    if valid_mask is not None:
        arr = arr.clone()
        arr[~valid_mask] = float("nan")
    if arr.ndim > ndim:
        arr = arr.flatten(-2 - (arr.ndim - ndim), -2)
    return arr


def invalid_to_zeros(arr, valid_mask, ndim=999):
    """Replace invalid (masked-out) entries with zero and count valid points.

    Args:
        arr (Tensor): Input tensor.
        valid_mask (BoolTensor or None): Boolean mask; positions where
            ``False`` are set to 0.  If ``None``, all positions are
            treated as valid.
        ndim (int): Maximum number of dimensions to keep; trailing
            dimensions beyond *ndim* are flattened. Defaults to ``999``.

    Returns:
        tuple: ``(arr, nnz)`` where *arr* has zeros in invalid positions
        and *nnz* is the per-image count of valid points (Tensor or int).
    """
    if valid_mask is not None:
        arr = arr.clone()
        arr[~valid_mask] = 0
        nnz = valid_mask.view(len(valid_mask), -1).sum(1)
    else:
        nnz = arr.numel() // len(arr) if len(arr) else 0  # number of point per image
    if arr.ndim > ndim:
        arr = arr.flatten(-2 - (arr.ndim - ndim), -2)
    return arr, nnz
