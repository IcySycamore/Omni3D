# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (C) 2024-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).
#
# --------------------------------------------------------
# Slower implementation of the global alignment that allows to freeze partial poses/intrinsics
# --------------------------------------------------------
import numpy as np
import torch
import torch.nn as nn

from fast3r.dust3r.cloud_opt.base_opt import BasePCOptimizer
from fast3r.dust3r.utils.device import to_cpu, to_numpy
from fast3r.dust3r.utils.geometry import depthmap_to_pts3d, geotrf


class ModularPointCloudOptimizer(BasePCOptimizer):
    """Optimize a global scene, given a list of pairwise observations.
    Unlike PointCloudOptimizer, you can fix parts of the optimization process (partial poses/intrinsics)
    Graph node: images
    Graph edges: observations = (pred1, pred2)
    """

    def __init__(
        self, *args, optimize_pp=False, fx_and_fy=False, focal_brake=20, **kwargs
    ):
        """Initialize the modular point cloud optimizer.

        Args:
            *args: Positional arguments forwarded to :class:`BasePCOptimizer`.
            optimize_pp (bool): Whether to optimize the principal point.
                Defaults to ``False``.
            fx_and_fy (bool): If ``True``, use separate focal parameters
                ``[fx, fy]`` per image; otherwise use a single shared focal
                ``[f]``. Defaults to ``False``.
            focal_brake (float): Scale factor for the log-encoded focal
                parameter. Defaults to ``20``.
            **kwargs: Additional keyword arguments forwarded to
                :class:`BasePCOptimizer`.
        """
        super().__init__(*args, **kwargs)
        self.has_im_poses = True  # by definition of this class
        self.focal_brake = focal_brake

        # adding thing to optimize
        self.im_depthmaps = nn.ParameterList(
            torch.randn(H, W) / 10 - 3 for H, W in self.imshapes
        )  # log(depth)
        self.im_poses = nn.ParameterList(
            self.rand_pose(self.POSE_DIM) for _ in range(self.n_imgs)
        )  # camera poses
        default_focals = [
            self.focal_brake * np.log(max(H, W)) for H, W in self.imshapes
        ]
        self.im_focals = nn.ParameterList(
            torch.FloatTensor([f, f] if fx_and_fy else [f]) for f in default_focals
        )  # camera intrinsics
        self.im_pp = nn.ParameterList(
            torch.zeros((2,)) for _ in range(self.n_imgs)
        )  # camera intrinsics
        self.im_pp.requires_grad_(optimize_pp)

    def preset_pose(self, known_poses, pose_msk=None):  # cam-to-world
        """Preset (freeze) camera poses for a subset of images.

        Sets the specified cam-to-world poses and disables gradient
        computation for those parameters.  If fewer than 2 poses are frozen,
        scale normalization remains active.

        Args:
            known_poses (Tensor or list of Tensor): Cam-to-world pose
                matrix/matrices of shape (4, 4) or a batch (N, 4, 4).
            pose_msk (int, list, ndarray, or None): Indices of the images
                whose poses should be set.  Defaults to ``None`` (all images).
        """
        if isinstance(known_poses, torch.Tensor) and known_poses.ndim == 2:
            known_poses = [known_poses]
        for idx, pose in zip(self._get_msk_indices(pose_msk), known_poses):
            if self.verbose:
                print(f" (setting pose #{idx} = {pose[:3,3]})")
            self._no_grad(
                self._set_pose(self.im_poses, idx, torch.tensor(pose), force=True)
            )

        # normalize scale if there's less than 1 known pose
        n_known_poses = sum((p.requires_grad is False) for p in self.im_poses)
        self.norm_pw_scale = n_known_poses <= 1

    def preset_intrinsics(self, known_intrinsics, msk=None):
        """Preset (freeze) camera intrinsics for a subset of images.

        Decomposes each 3x3 intrinsic matrix *K* into a focal length
        (mean of diagonal elements) and principal point, then calls
        :meth:`preset_focal` and :meth:`preset_principal_point`.

        Args:
            known_intrinsics (Tensor or list of Tensor): Intrinsic matrices
                of shape (3, 3) or a list thereof.
            msk (int, list, ndarray, or None): Indices of the target images.
                Defaults to ``None`` (all images).
        """
        if isinstance(known_intrinsics, torch.Tensor) and known_intrinsics.ndim == 2:
            known_intrinsics = [known_intrinsics]
        for K in known_intrinsics:
            assert K.shape == (3, 3)
        self.preset_focal([K.diagonal()[:2].mean() for K in known_intrinsics], msk)
        self.preset_principal_point([K[:2, 2] for K in known_intrinsics], msk)

    def preset_focal(self, known_focals, msk=None):
        """Preset (freeze) focal lengths for a subset of images.

        Args:
            known_focals (list of float): Focal length values.
            msk (int, list, ndarray, or None): Indices of the target images.
                Defaults to ``None`` (all images).
        """
        for idx, focal in zip(self._get_msk_indices(msk), known_focals):
            if self.verbose:
                print(f" (setting focal #{idx} = {focal})")
            self._no_grad(self._set_focal(idx, focal, force=True))

    def preset_principal_point(self, known_pp, msk=None):
        """Preset (freeze) principal points for a subset of images.

        Args:
            known_pp (list of Tensor): Principal points, each a 2-vector
                ``[cx, cy]`` in pixel coordinates.
            msk (int, list, ndarray, or None): Indices of the target images.
                Defaults to ``None`` (all images).
        """
        for idx, pp in zip(self._get_msk_indices(msk), known_pp):
            if self.verbose:
                print(f" (setting principal point #{idx} = {pp})")
            self._no_grad(self._set_principal_point(idx, pp, force=True))

    def _no_grad(self, tensor):
        """Disable gradient computation for a parameter tensor.

        Args:
            tensor (Tensor): A parameter whose gradient tracking should
                be turned off.

        Returns:
            Tensor: The same tensor with ``requires_grad=False``.
        """
        return tensor.requires_grad_(False)

    def _get_msk_indices(self, msk):
        """Convert a mask specification to a list of image indices.

        Args:
            msk (None, int, list, tuple, ndarray, or BoolTensor): The mask
                specification.  Supported types:

                * ``None`` – all image indices ``[0, n_imgs)``.
                * ``int`` – a single index wrapped in a list.
                * ``list`` / ``tuple`` – forwarded after conversion to ndarray.
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

    def _set_focal(self, idx, focal, force=False):
        """Set the log-encoded focal length for image *idx*.

        Args:
            idx (int): Image index.
            focal (float): Focal length in pixels.
            force (bool): If ``True``, set even when ``requires_grad=False``.

        Returns:
            nn.Parameter: The focal length parameter.
        """
        param = self.im_focals[idx]
        if (
            param.requires_grad or force
        ):  # can only init a parameter not already initialized
            param.data[:] = self.focal_brake * np.log(focal)
        return param

    def get_focals(self):
        """Return per-image focal lengths (decoded from log-space).

        Returns:
            Tensor: Focal length tensor of shape (n_imgs,) or (n_imgs, 2)
            when ``fx_and_fy=True``.
        """
        log_focals = torch.stack(list(self.im_focals), dim=0)
        return (log_focals / self.focal_brake).exp()

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
            nn.Parameter: The principal point parameter.
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

        Decodes from the stored centered, scaled representation back to
        absolute pixel coordinates ``(cx, cy)``.

        Returns:
            Tensor: Principal point tensor of shape (n_imgs, 2).
        """
        return torch.stack(
            [
                pp.new((W / 2, H / 2)) + 10 * pp
                for pp, (H, W) in zip(self.im_pp, self.imshapes)
            ]
        )

    def get_intrinsics(self):
        """Return per-image 3x3 intrinsic (K) matrices.

        Assembles focal lengths and principal points into standard camera
        intrinsic matrices.

        Returns:
            Tensor: Intrinsic matrices of shape (n_imgs, 3, 3).
        """
        K = torch.zeros((self.n_imgs, 3, 3), device=self.device)
        focals = self.get_focals().view(self.n_imgs, -1)
        K[:, 0, 0] = focals[:, 0]
        K[:, 1, 1] = focals[:, -1]
        K[:, :2, 2] = self.get_principal_points()
        K[:, 2, 2] = 1
        return K

    def get_im_poses(self):  # cam to world
        """Return per-image cam-to-world transformation matrices.

        Returns:
            Tensor: Homogeneous cam-to-world matrices of shape (n_imgs, 4, 4).
        """
        cam2world = self._get_poses(torch.stack(list(self.im_poses)))
        return cam2world

    def _set_depthmap(self, idx, depth, force=False):
        """Set the log-encoded depth map for image *idx*.

        Args:
            idx (int): Image index.
            depth (Tensor): Depth map of shape (H, W) with positive values.
            force (bool): If ``True``, overwrite even when
                ``requires_grad=False``.

        Returns:
            nn.Parameter: The depth map parameter.
        """
        param = self.im_depthmaps[idx]
        if (
            param.requires_grad or force
        ):  # can only init a parameter not already initialized
            param.data[:] = depth.log().nan_to_num(neginf=0)
        return param

    def get_depthmaps(self):
        """Return optimized depth maps (exponentiated from log-space).

        Returns:
            list of Tensor: Per-image depth maps, each of shape (H, W).
        """
        return [d.exp() for d in self.im_depthmaps]

    def depth_to_pts3d(self):
        """Convert depth maps to 3D point maps in world coordinates.

        Projects each pixel through the camera intrinsics to obtain
        3D coordinates in the camera frame, then transforms them to world
        coordinates using the estimated cam-to-world poses.

        Returns:
            list of Tensor: Per-image 3D point maps, each of shape (H, W, 3)
            in world coordinates.
        """
        # Get depths and  projection params if not provided
        focals = self.get_focals()
        pp = self.get_principal_points()
        im_poses = self.get_im_poses()
        depth = self.get_depthmaps()

        # convert focal to (1,2,H,W) constant field
        def focal_ex(i):
            return focals[i][..., None, None].expand(
                1, *focals[i].shape, *self.imshapes[i]
            )

        # get pointmaps in camera frame
        rel_ptmaps = [
            depthmap_to_pts3d(depth[i][None], focal_ex(i), pp=pp[i : i + 1])[0]
            for i in range(im_poses.shape[0])
        ]
        # project to world frame
        return [geotrf(pose, ptmap) for pose, ptmap in zip(im_poses, rel_ptmaps)]

    def get_pts3d(self):
        """Return optimized 3D point maps (alias for :meth:`depth_to_pts3d`).

        Returns:
            list of Tensor: Per-image 3D point maps of shape (H, W, 3).
        """
        return self.depth_to_pts3d()
