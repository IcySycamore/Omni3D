# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""优化器。"""

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (C) 2024-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).
#
# --------------------------------------------------------
# Main class for the implementation of the global alignment
# --------------------------------------------------------
import numpy as np
import torch
import torch.nn as nn

from fast3r.dust3r.cloud_opt.base_opt import BasePCOptimizer
from fast3r.dust3r.utils.device import to_cpu, to_numpy
from fast3r.dust3r.utils.geometry import geotrf, xy_grid


class PointCloudOptimizer(BasePCOptimizer):
    """Optimize a global scene, given a list of pairwise observations.
    Graph node: images
    Graph edges: observations = (pred1, pred2)
    """

    def __init__(self, *args, optimize_pp=False, focal_break=20, **kwargs):
        """Initialize the batched point cloud optimizer.

        Calls the base class constructor, then stacks all per-image
        learnable parameters (depth maps, poses, focals, principal points)
        into single ``ParameterStack`` tensors for efficient batched
        operations, and pre-computes pixel grids and confidence weight stacks.

        Args:
            *args: Positional arguments forwarded to :class:`BasePCOptimizer`.
            optimize_pp (bool): Whether to optimize the principal point.
                Defaults to ``False``.
            focal_break (float): Scale factor for the log-encoded focal
                parameter. Defaults to ``20``.
            **kwargs: Additional keyword arguments forwarded to
                :class:`BasePCOptimizer`.
        """
        super().__init__(*args, **kwargs)

        self.has_im_poses = True  # by definition of this class
        self.focal_break = focal_break

        # adding thing to optimize
        self.im_depthmaps = nn.ParameterList(
            torch.randn(H, W) / 10 - 3 for H, W in self.imshapes
        )  # log(depth)
        self.im_poses = nn.ParameterList(
            self.rand_pose(self.POSE_DIM) for _ in range(self.n_imgs)
        )  # camera poses
        self.im_focals = nn.ParameterList(
            torch.FloatTensor([self.focal_break * np.log(max(H, W))])
            for H, W in self.imshapes
        )  # camera intrinsics
        self.im_pp = nn.ParameterList(
            torch.zeros((2,)) for _ in range(self.n_imgs)
        )  # camera intrinsics
        self.im_pp.requires_grad_(optimize_pp)

        self.imshape = self.imshapes[0]
        im_areas = [h * w for h, w in self.imshapes]
        self.max_area = max(im_areas)

        # adding thing to optimize
        self.im_depthmaps = ParameterStack(
            self.im_depthmaps, is_param=True, fill=self.max_area
        )
        self.im_poses = ParameterStack(self.im_poses, is_param=True)
        self.im_focals = ParameterStack(self.im_focals, is_param=True)
        self.im_pp = ParameterStack(self.im_pp, is_param=True)
        self.register_buffer(
            "_pp", torch.tensor([(w / 2, h / 2) for h, w in self.imshapes])
        )
        self.register_buffer(
            "_grid",
            ParameterStack(
                [xy_grid(W, H, device=self.device) for H, W in self.imshapes],
                fill=self.max_area,
            ),
        )

        # pre-compute pixel weights
        self.register_buffer(
            "_weight_i",
            ParameterStack(
                [self.conf_trf(self.conf_i[i_j]) for i_j in self.str_edges],
                fill=self.max_area,
            ),
        )
        self.register_buffer(
            "_weight_j",
            ParameterStack(
                [self.conf_trf(self.conf_j[i_j]) for i_j in self.str_edges],
                fill=self.max_area,
            ),
        )

        # precompute aa
        self.register_buffer(
            "_stacked_pred_i",
            ParameterStack(self.pred_i, self.str_edges, fill=self.max_area),
        )
        self.register_buffer(
            "_stacked_pred_j",
            ParameterStack(self.pred_j, self.str_edges, fill=self.max_area),
        )
        self.register_buffer("_ei", torch.tensor([i for i, j in self.edges]))
        self.register_buffer("_ej", torch.tensor([j for i, j in self.edges]))
        self.total_area_i = sum([im_areas[i] for i, j in self.edges])
        self.total_area_j = sum([im_areas[j] for i, j in self.edges])

    def _check_all_imgs_are_selected(self, msk):
        """Assert that the mask selects all images (complete coverage required).

        PointCloudOptimizer uses stacked parameter tensors that cannot
        handle partial updates, so all images must be specified at once.

        Args:
            msk: Mask specification (see :meth:`_get_msk_indices`).

        Raises:
            AssertionError: If *msk* does not select all ``n_imgs`` images.
        """
        assert np.all(
            self._get_msk_indices(msk) == np.arange(self.n_imgs)
        ), "incomplete mask!"

    def preset_pose(self, known_poses, pose_msk=None):  # cam-to-world
        """Preset (freeze) all camera poses at once.

        Because poses are stored as a single stacked tensor, **all** images
        must be specified simultaneously (enforced by
        :meth:`_check_all_imgs_are_selected`).

        Args:
            known_poses (Tensor or list of Tensor): Cam-to-world matrices of
                shape (4, 4) or (N, 4, 4).
            pose_msk: Must select all images; see :meth:`_check_all_imgs_are_selected`.
        """
        self._check_all_imgs_are_selected(pose_msk)

        if isinstance(known_poses, torch.Tensor) and known_poses.ndim == 2:
            known_poses = [known_poses]
        for idx, pose in zip(self._get_msk_indices(pose_msk), known_poses):
            if self.verbose:
                print(f" (setting pose #{idx} = {pose[:3,3]})")
            self._no_grad(self._set_pose(self.im_poses, idx, torch.tensor(pose)))

        # normalize scale if there's less than 1 known pose
        n_known_poses = sum((p.requires_grad is False) for p in self.im_poses)
        self.norm_pw_scale = n_known_poses <= 1

        self.im_poses.requires_grad_(False)
        self.norm_pw_scale = False

    def preset_focal(self, known_focals, msk=None):
        """Preset (freeze) all focal lengths at once.

        All images must be specified simultaneously.

        Args:
            known_focals (list of float): Focal length values, one per image.
            msk: Must select all images; see :meth:`_check_all_imgs_are_selected`.
        """
        self._check_all_imgs_are_selected(msk)

        for idx, focal in zip(self._get_msk_indices(msk), known_focals):
            if self.verbose:
                print(f" (setting focal #{idx} = {focal})")
            self._no_grad(self._set_focal(idx, focal))

        self.im_focals.requires_grad_(False)

    def preset_principal_point(self, known_pp, msk=None):
        """Preset (freeze) all principal points at once.

        All images must be specified simultaneously.

        Args:
            known_pp (list of Tensor): Principal points ``[cx, cy]`` in pixel
                coordinates, one per image.
            msk: Must select all images; see :meth:`_check_all_imgs_are_selected`.
        """
        self._check_all_imgs_are_selected(msk)

        for idx, pp in zip(self._get_msk_indices(msk), known_pp):
            if self.verbose:
                print(f" (setting principal point #{idx} = {pp})")
            self._no_grad(self._set_principal_point(idx, pp))

        self.im_pp.requires_grad_(False)

    def _get_msk_indices(self, msk):
        """Convert a mask specification to a list of image indices.

        Args:
            msk (None, int, list, tuple, ndarray, or BoolTensor): Mask
                specification. Supported types:

                * ``None`` – all indices ``[0, n_imgs)``.
                * ``int`` – a single index wrapped in a list.
                * ``list`` / ``tuple`` – converted to ndarray and processed.
                * Bool array/tensor of length ``n_imgs`` – non-zero indices.
                * Integer array – returned as-is.

        Returns:
            range or ndarray: Sequence of image indices.

        Raises:
            ValueError: If *msk* has an unsupported dtype.
        """
        if msk is None:
            return range(self.n_imgs)
        elif isinstance(msk, int):
            return [msk]
        elif isinstance(msk, (tuple, list)):
            return self._get_msk_indices(np.array(msk))
        elif msk.dtype in (bool, torch.bool, np.bool_):
            assert len(msk) == self.n_imgs
            return np.where(msk)[0]
        elif np.issubdtype(msk.dtype, np.integer):
            return msk
        else:
            raise ValueError(f"bad {msk=}")

    def _no_grad(self, tensor):
        """Assert that a parameter already requires a gradient (sanity check).

        Unlike :class:`ModularPointCloudOptimizer`, this class does not
        actually disable gradients on individual parameters, since they
        are stored in a shared stacked tensor.  This method is a no-op that
        verifies the parameter is still trainable before it is overwritten.

        Args:
            tensor (Tensor): Parameter to check.

        Raises:
            AssertionError: If ``tensor.requires_grad`` is ``False``.
        """
        assert (
            tensor.requires_grad
        ), "it must be True at this point, otherwise no modification occurs"

    def _set_focal(self, idx, focal, force=False):
        """Set the log-encoded focal length for image *idx*.

        Args:
            idx (int): Image index.
            focal (float): Focal length in pixels.
            force (bool): If ``True``, set even when ``requires_grad=False``.

        Returns:
            Tensor: The updated focal parameter slice.
        """
        param = self.im_focals[idx]
        if (
            param.requires_grad or force
        ):  # can only init a parameter not already initialized
            param.data[:] = self.focal_break * np.log(focal)
        return param

    def get_focals(self):
        """Return per-image focal lengths (decoded from log-space).

        Returns:
            Tensor: Focal length tensor of shape (n_imgs,).
        """
        log_focals = torch.stack(list(self.im_focals), dim=0)
        return (log_focals / self.focal_break).exp()

    def get_known_focal_mask(self):
        """Return a boolean mask indicating which images have frozen focal lengths.

        Returns:
            BoolTensor: Tensor of shape (n_imgs,) where ``True`` means the
            focal length is frozen (not being optimized).
        """
        return torch.tensor([not (p.requires_grad) for p in self.im_focals])

    def _set_principal_point(self, idx, pp, force=False):
        """Set the principal point for image *idx*.

        Stores the principal point as a small offset from the image center
        (divided by 10 for numerical stability).

        Args:
            idx (int): Image index.
            pp (Tensor or array-like): Principal point ``[cx, cy]`` in pixel
                coordinates.
            force (bool): If ``True``, set even when ``requires_grad=False``.

        Returns:
            Tensor: The updated principal point parameter slice.
        """
        param = self.im_pp[idx]
        H, W = self.imshapes[idx]
        if (
            param.requires_grad or force
        ):  # can only init a parameter not already initialized
            param.data[:] = to_cpu(to_numpy(pp) - (W / 2, H / 2)) / 10
        return param

    def get_principal_points(self):
        """Return per-image principal points in pixel coordinates.

        Decodes from the centered, scaled representation stored in
        ``im_pp`` to absolute ``(cx, cy)`` pixel coordinates.

        Returns:
            Tensor: Principal point tensor of shape (n_imgs, 2).
        """
        return self._pp + 10 * self.im_pp

    def get_intrinsics(self):
        """Return per-image 3x3 intrinsic (K) matrices.

        Assembles the (shared) focal length and per-image principal points
        into standard camera intrinsic matrices.

        Returns:
            Tensor: Intrinsic matrices of shape (n_imgs, 3, 3).
        """
        K = torch.zeros((self.n_imgs, 3, 3), device=self.device)
        focals = self.get_focals().flatten()
        K[:, 0, 0] = K[:, 1, 1] = focals
        K[:, :2, 2] = self.get_principal_points()
        K[:, 2, 2] = 1
        return K

    def get_im_poses(self):  # cam to world
        """Return per-image cam-to-world transformation matrices.

        Returns:
            Tensor: Homogeneous cam-to-world matrices of shape (n_imgs, 4, 4).
        """
        cam2world = self._get_poses(self.im_poses)
        return cam2world

    def _set_depthmap(self, idx, depth, force=False):
        """Set the log-encoded depth map for image *idx*.

        Ravels the (H, W) depth map to a 1-D vector padded to ``max_area``
        before storing.

        Args:
            idx (int): Image index.
            depth (Tensor): Depth map of shape (H, W) with positive values.
            force (bool): If ``True``, overwrite even when
                ``requires_grad=False``.

        Returns:
            Tensor: The updated depth map parameter slice.
        """
        depth = _ravel_hw(depth, self.max_area)

        param = self.im_depthmaps[idx]
        if (
            param.requires_grad or force
        ):  # can only init a parameter not already initialized
            param.data[:] = depth.log().nan_to_num(neginf=0)
        return param

    def get_depthmaps(self, raw=False):
        """Return optimized depth maps (exponentiated from log-space).

        Args:
            raw (bool): If ``False`` (default), return per-image (H, W)
                tensors.  If ``True``, return the padded flat stacked tensor.

        Returns:
            list of Tensor or Tensor: Per-image depth maps of shape (H, W)
            when *raw* is ``False``, or the raw stacked tensor otherwise.
        """
        res = self.im_depthmaps.exp()
        if not raw:
            res = [dm[: h * w].view(h, w) for dm, (h, w) in zip(res, self.imshapes)]
        return res

    def depth_to_pts3d(self):
        """Convert depth maps to 3D point maps in world coordinates.

        Uses the fast batched projection :func:`_fast_depthmap_to_pts3d`
        on the pre-computed pixel grid, then transforms to world coordinates.

        Returns:
            Tensor: Stacked point maps of shape (n_imgs, max_area, 3).
        """
        # Get depths and  projection params if not provided
        focals = self.get_focals()
        pp = self.get_principal_points()
        im_poses = self.get_im_poses()
        depth = self.get_depthmaps(raw=True)

        # get pointmaps in camera frame
        rel_ptmaps = _fast_depthmap_to_pts3d(depth, self._grid, focals, pp=pp)
        # project to world frame
        return geotrf(im_poses, rel_ptmaps)

    def get_pts3d(self, raw=False):
        """Return optimized 3D point maps.

        Args:
            raw (bool): If ``False`` (default), reshape the output of
                :meth:`depth_to_pts3d` to per-image (H, W, 3) tensors.
                If ``True``, return the raw batched tensor.

        Returns:
            list of Tensor or Tensor: Per-image point maps of shape (H, W, 3)
            when *raw* is ``False``, or the raw batched tensor otherwise.
        """
        res = self.depth_to_pts3d()
        if not raw:
            res = [dm[: h * w].view(h, w, 3) for dm, (h, w) in zip(res, self.imshapes)]
        return res

    def forward(self):
        """Compute the batched global alignment loss.

        Transforms all pairwise predictions using pairwise poses and adaptors,
        then computes the weighted distance between the transformed predictions
        and the globally optimized point maps for all edges simultaneously.

        Returns:
            Tensor: Scalar alignment loss.
        """
        pw_poses = self.get_pw_poses()  # cam-to-world
        pw_adapt = self.get_adaptors().unsqueeze(1)
        proj_pts3d = self.get_pts3d(raw=True)

        # rotate pairwise prediction according to pw_poses
        aligned_pred_i = geotrf(pw_poses, pw_adapt * self._stacked_pred_i)
        aligned_pred_j = geotrf(pw_poses, pw_adapt * self._stacked_pred_j)

        # compute the less
        li = (
            self.dist(proj_pts3d[self._ei], aligned_pred_i, weight=self._weight_i).sum()
            / self.total_area_i
        )
        lj = (
            self.dist(proj_pts3d[self._ej], aligned_pred_j, weight=self._weight_j).sum()
            / self.total_area_j
        )

        return li + lj


def _fast_depthmap_to_pts3d(depth, pixel_grid, focal, pp):
    """Efficiently convert a batched depth map to 3D point clouds.

    Applies the pin-hole projection formula::

        pts3d = (pixel_grid - pp) / focal * depth  (xy channels)
        pts3d_z = depth                             (z channel)

    Args:
        depth (Tensor): Flat depth map, shape (N, max_area).
        pixel_grid (Tensor): Pixel coordinate grid, shape (N, max_area, 2).
        focal (Tensor): Focal lengths, shape (N, 1, 1).
        pp (Tensor): Principal points, shape (N, 1, 2).

    Returns:
        Tensor: 3D point cloud in camera space, shape (N, max_area, 3).
    """
    pp = pp.unsqueeze(1)
    focal = focal.unsqueeze(1)
    assert focal.shape == (len(depth), 1, 1)
    assert pp.shape == (len(depth), 1, 2)
    assert pixel_grid.shape == depth.shape + (2,)
    depth = depth.unsqueeze(-1)
    return torch.cat((depth * (pixel_grid - pp) / focal, depth), dim=-1)


def ParameterStack(params, keys=None, is_param=None, fill=0):
    """Stack a collection of parameter tensors into a single tensor.

    Optionally pads each tensor to a fixed length along the first dimension
    and wraps the result as an ``nn.Parameter``.

    Args:
        params (dict or list): Parameter tensors to stack.  If a dict,
            *keys* must be provided to select entries in order.
        keys (list or None): Ordered keys used to index *params* when it is
            a dict.  Defaults to ``None``.
        is_param (bool or None): If ``True``, wrap the stacked tensor as an
            ``nn.Parameter``.  If ``None``, inherits from the first tensor's
            ``requires_grad``.  Defaults to ``None``.
        fill (int): If > 0, pad each tensor with zeros so that its first
            dimension equals *fill* (via :func:`_ravel_hw`).
            Defaults to ``0``.

    Returns:
        Tensor or nn.Parameter: Stacked tensor of shape
        ``(len(params), fill, ...)`` or ``(len(params), ...)``,
        optionally wrapped as a parameter.
    """
    if keys is not None:
        params = [params[k] for k in keys]

    if fill > 0:
        params = [_ravel_hw(p, fill) for p in params]

    requires_grad = params[0].requires_grad
    assert all(p.requires_grad == requires_grad for p in params)

    params = torch.stack(list(params)).float().detach()
    if is_param or requires_grad:
        params = nn.Parameter(params)
        params.requires_grad_(requires_grad)
    return params


def _ravel_hw(tensor, fill=0):
    """Flatten the first two (H, W) dimensions of a tensor and optionally zero-pad.

    Args:
        tensor (Tensor): Input tensor of shape (H, W, ...) or (H*W, ...).
        fill (int): If > 0, pad the flattened tensor with zeros so that its
            length equals *fill*.  Defaults to ``0``.

    Returns:
        Tensor: Flattened (and possibly padded) tensor of shape
        ``(H*W, ...)`` or ``(fill, ...)``.
    """
    # ravel H,W
    tensor = tensor.view((tensor.shape[0] * tensor.shape[1],) + tensor.shape[2:])

    if len(tensor) < fill:
        tensor = torch.cat(
            (tensor, tensor.new_zeros((fill - len(tensor),) + tensor.shape[1:]))
        )
    return tensor


def acceptable_focal_range(H, W, minf=0.5, maxf=3.5):
    """Compute a reasonable focal length range for images of size (H, W).

    The base focal is derived from the longer image dimension assuming a
    60-degree field of view, then multiplied by *minf* and *maxf* to give
    the acceptable range.

    Args:
        H (int): Image height in pixels.
        W (int): Image width in pixels.
        minf (float): Minimum focal length as a multiple of the base focal.
            Defaults to ``0.5``.
        maxf (float): Maximum focal length as a multiple of the base focal.
            Defaults to ``3.5``.

    Returns:
        tuple: ``(min_focal, max_focal)`` in pixels.
    """
    focal_base = max(H, W) / (
        2 * np.tan(np.deg2rad(60) / 2)
    )  # size / 1.1547005383792515
    return minf * focal_base, maxf * focal_base


def apply_mask(img, msk):
    """Zero out pixels in *img* where *msk* is True.

    Args:
        img (np.ndarray): Image array to modify (copied internally).
        msk (np.ndarray of bool): Boolean mask of the same spatial shape as *img*.

    Returns:
        np.ndarray: Copy of *img* with masked pixels set to 0.
    """
    img = img.copy()
    img[msk] = 0
    return img
