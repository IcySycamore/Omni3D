# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (C) 2024-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).
#
# --------------------------------------------------------
# Base class for the global alignement procedure
# --------------------------------------------------------
from copy import deepcopy

import numpy as np
import roma
import torch
import torch.nn as nn
import tqdm

import fast3r.dust3r.cloud_opt.init_im_poses as init_fun
from fast3r.dust3r.cloud_opt.commons import (
    ALL_DISTS,
    NoGradParamDict,
    cosine_schedule,
    edge_str,
    get_conf_trf,
    get_imshapes,
    linear_schedule,
    signed_expm1,
    signed_log1p,
)
from fast3r.dust3r.optim_factory import adjust_learning_rate_by_lr
from fast3r.dust3r.utils.device import to_numpy
from fast3r.dust3r.utils.geometry import geotrf, inv
from fast3r.dust3r.utils.image import rgb
from fast3r.dust3r.viz import SceneViz, auto_cam_size, segment_sky


class BasePCOptimizer(nn.Module):
    """Optimize a global scene, given a list of pairwise observations.
    Graph node: images
    Graph edges: observations = (pred1, pred2)
    """

    def __init__(self, *args, **kwargs):
        """初始化 BasePCOptimizer，支持从视图对或深拷贝构造。

        如果只传入一个位置参数且无关键字参数，则执行深拷贝；
        否则从视图对初始化。

        Args:
            *args: 单个 BasePCOptimizer 实例（深拷贝）或视图对参数。
            **kwargs: 传递给 _init_from_views 的关键字参数。
        """
        if len(args) == 1 and len(kwargs) == 0:
            attrs = """edges is_symmetrized dist n_imgs pred_i pred_j imshapes
                        min_conf_thr conf_thr conf_i conf_j im_conf
                        base_scale norm_pw_scale POSE_DIM pw_poses
                        pw_adaptors pw_adaptors has_im_poses rand_pose imgs verbose""".split()
            self.__dict__.update({k: other[k] for k in attrs})
        else:
            self._init_from_views(*args, **kwargs)

    def _init_from_views(
        self,
        view1,
        view2,
        pred1,
        pred2,
        dist="l1",
        conf="log",
        min_conf_thr=3,
        base_scale=0.5,
        allow_pw_adaptors=False,
        pw_break=20,
        rand_pose=torch.randn,
        iterationsCount=None,
        verbose=True,
    ):
        """Initialize the optimizer from pairwise view observations.

        Builds the pose graph, stores prediction tensors, computes per-image
        confidence maps, and initializes learnable pairwise pose parameters.

        Args:
            view1 (dict): First-view data containing ``'idx'`` (image indices)
                and optionally ``'img'`` (image tensors).
            view2 (dict): Second-view data with the same structure as *view1*.
            pred1 (dict): Model predictions for view1, must contain
                ``'pts3d'`` and ``'conf'`` tensors.
            pred2 (dict): Model predictions for view2, must contain
                ``'pts3d_in_other_view'`` and ``'conf'`` tensors.
            dist (str): Distance function for the alignment loss, one of
                ``'l1'`` or ``'l2'``. Defaults to ``'l1'``.
            conf (str): Confidence transformation mode passed to
                :func:`get_conf_trf`. Defaults to ``'log'``.
            min_conf_thr (float): Minimum confidence threshold for masking.
                Defaults to ``3``.
            base_scale (float): Base scale used for pairwise pose
                normalization. Defaults to ``0.5``.
            allow_pw_adaptors (bool): Whether to optimize per-edge XY/Z
                scale adaptors. Defaults to ``False``.
            pw_break (float): Break value for pairwise adaptor normalization.
                Defaults to ``20``.
            rand_pose (Callable): Function used to initialize random pose
                parameters (e.g. ``torch.randn``). Defaults to
                ``torch.randn``.
            iterationsCount (int or None): Reserved for future use.
                Defaults to ``None``.
            verbose (bool): Whether to print progress messages.
                Defaults to ``True``.
        """
        if not isinstance(view1["idx"], list):
            view1["idx"] = view1["idx"].tolist()
        if not isinstance(view2["idx"], list):
            view2["idx"] = view2["idx"].tolist()
        self.edges = [(int(i), int(j)) for i, j in zip(view1["idx"], view2["idx"])]
        self.is_symmetrized = set(self.edges) == {(j, i) for i, j in self.edges}
        self.dist = ALL_DISTS[dist]
        self.verbose = verbose

        self.n_imgs = self._check_edges()

        # input data
        pred1_pts = pred1["pts3d"]
        pred2_pts = pred2["pts3d_in_other_view"]
        self.pred_i = NoGradParamDict(
            {ij: pred1_pts[n] for n, ij in enumerate(self.str_edges)}
        )
        self.pred_j = NoGradParamDict(
            {ij: pred2_pts[n] for n, ij in enumerate(self.str_edges)}
        )
        self.imshapes = get_imshapes(self.edges, pred1_pts, pred2_pts)

        # work in log-scale with conf
        pred1_conf = pred1["conf"]
        pred2_conf = pred2["conf"]
        self.min_conf_thr = min_conf_thr
        self.conf_trf = get_conf_trf(conf)

        self.conf_i = NoGradParamDict(
            {ij: pred1_conf[n] for n, ij in enumerate(self.str_edges)}
        )
        self.conf_j = NoGradParamDict(
            {ij: pred2_conf[n] for n, ij in enumerate(self.str_edges)}
        )
        self.im_conf = self._compute_img_conf(pred1_conf, pred2_conf)
        for i in range(len(self.im_conf)):
            self.im_conf[i].requires_grad = False

        # pairwise pose parameters
        self.base_scale = base_scale
        self.norm_pw_scale = True
        self.pw_break = pw_break
        self.POSE_DIM = 7
        self.pw_poses = nn.Parameter(
            rand_pose((self.n_edges, 1 + self.POSE_DIM))
        )  # pairwise poses
        self.pw_adaptors = nn.Parameter(
            torch.zeros((self.n_edges, 2))
        )  # slight xy/z adaptation
        self.pw_adaptors.requires_grad_(allow_pw_adaptors)
        self.has_im_poses = False
        self.rand_pose = rand_pose

        # possibly store images for show_pointcloud
        self.imgs = None
        if "img" in view1 and "img" in view2:
            imgs = [torch.zeros((3,) + hw) for hw in self.imshapes]
            for v in range(len(self.edges)):
                idx = view1["idx"][v]
                imgs[idx] = view1["img"][v]
                idx = view2["idx"][v]
                imgs[idx] = view2["img"][v]
            self.imgs = rgb(imgs)

    @property
    def n_edges(self):
        """Total number of directed edges (pairwise observations) in the graph.

        Returns:
            int: Number of edges.
        """
        return len(self.edges)

    @property
    def str_edges(self):
        """List of edge string keys in the format ``"i_j"`` for all edges.

        Returns:
            list of str: Edge identifier strings.
        """
        return [edge_str(i, j) for i, j in self.edges]

    @property
    def imsizes(self):
        """List of image sizes as (W, H) tuples for all images.

        Returns:
            list of tuple: Per-image (width, height) tuples.
        """
        return [(w, h) for h, w in self.imshapes]

    @property
    def device(self):
        """Device of the first trainable parameter.

        Returns:
            torch.device: The device the model lives on.
        """
        return next(iter(self.parameters())).device

    def state_dict(self, trainable=True):
        """Return a state dict containing either trainable or frozen parameters.

        Args:
            trainable (bool): If ``True`` (default), return only the trainable
                parameters (i.e. pose and adaptor parameters).  If ``False``,
                return only the frozen input data tensors
                (``pred_i``, ``pred_j``, ``conf_i``, ``conf_j``).

        Returns:
            dict: Filtered state dictionary.
        """
        all_params = super().state_dict()
        return {
            k: v
            for k, v in all_params.items()
            if k.startswith(("_", "pred_i.", "pred_j.", "conf_i.", "conf_j."))
            != trainable
        }

    def load_state_dict(self, data):
        """Load a state dict, merging frozen input data with the provided trainable weights.

        Args:
            data (dict): State dictionary containing trainable parameters
                (as returned by :meth:`state_dict`).

        Returns:
            ``_IncompatibleKeys``: Result of the underlying ``load_state_dict`` call.
        """
        return super().load_state_dict(self.state_dict(trainable=False) | data)

    def _check_edges(self):
        """Validate that edge indices cover a contiguous range [0, n_imgs).

        Returns:
            int: Number of unique images (nodes) in the graph.

        Raises:
            AssertionError: If any image index is missing (non-contiguous range).
        """
        indices = sorted({i for edge in self.edges for i in edge})
        assert indices == list(range(len(indices))), "bad pair indices: missing values "
        return len(indices)

    @torch.no_grad()
    def _compute_img_conf(self, pred1_conf, pred2_conf):
        """Compute per-image confidence maps as the element-wise maximum over all edges.

        For each image, the confidence is the pixel-wise maximum of all
        pairwise confidence maps that include that image.

        Args:
            pred1_conf (list of Tensor): Per-edge confidence maps for source images.
            pred2_conf (list of Tensor): Per-edge confidence maps for target images.

        Returns:
            nn.ParameterList: List of per-image confidence tensors (frozen).
        """
        im_conf = nn.ParameterList(
            [torch.zeros(hw, device=self.device) for hw in self.imshapes]
        )
        for e, (i, j) in enumerate(self.edges):
            im_conf[i] = torch.maximum(im_conf[i], pred1_conf[e])
            im_conf[j] = torch.maximum(im_conf[j], pred2_conf[e])
        return im_conf

    def get_adaptors(self):
        """Compute normalized per-edge XY/Z scale adaptors.

        The raw 2-D adaptor parameters are expanded to (scale_xy, scale_xy, scale_z),
        optionally normalized so their product equals 1, and exponentiated.

        Returns:
            Tensor: Per-edge adaptor tensors of shape (n_edges, 3).
        """
        adapt = self.pw_adaptors
        adapt = torch.cat(
            (adapt[:, 0:1], adapt), dim=-1
        )  # (scale_xy, scale_xy, scale_z)
        if self.norm_pw_scale:  # normalize so that the product == 1
            adapt = adapt - adapt.mean(dim=1, keepdim=True)
        return (adapt / self.pw_break).exp()

    def _get_poses(self, poses):
        """Convert raw pose parameters to homogeneous rigid transformation matrices.

        Normalizes the quaternion component and applies :func:`signed_expm1`
        to the translation component before constructing the 4x4 matrix.

        Args:
            poses (Tensor): Raw pose parameter tensor of shape (N, 7+),
                where columns 0:4 are quaternion (Q) and 4:7 are log-scale
                translation.

        Returns:
            Tensor: Homogeneous cam-to-world matrices of shape (N, 4, 4).
        """
        # normalize rotation
        Q = poses[:, :4]
        T = signed_expm1(poses[:, 4:7])
        RT = roma.RigidUnitQuat(Q, T).normalize().to_homogeneous()
        return RT

    def _set_pose(self, poses, idx, R, T=None, scale=None, force=False):
        """Set the rotation, translation, and optionally scale for a pose parameter.

        Only modifies the parameter if it currently requires a gradient or
        *force* is True (i.e., preset / known poses).

        Args:
            poses (nn.ParameterList or ParameterStack): Collection of pose parameters.
            idx (int): Index of the pose to set.
            R (Tensor): Either a 3x3 rotation matrix or a full 4x4 rigid
                transform (in which case *T* must be ``None``).
            T (Tensor or None): Translation vector of length 3. Ignored when
                *R* is a 4x4 matrix.
            scale (float or None): Optional scale value to store in the last
                parameter slot (log-encoded).
            force (bool): If ``True``, set even if ``requires_grad=False``.

        Returns:
            Tensor: The (possibly updated) pose parameter slice.
        """
        # all poses == cam-to-world
        pose = poses[idx]
        if not (pose.requires_grad or force):
            return pose

        if R.shape == (4, 4):
            assert T is None
            T = R[:3, 3]
            R = R[:3, :3]

        if R is not None:
            pose.data[0:4] = roma.rotmat_to_unitquat(R)
        if T is not None:
            pose.data[4:7] = signed_log1p(
                T / (scale or 1)
            )  # translation is function of scale

        if scale is not None:
            assert poses.shape[-1] in (8, 13)
            pose.data[-1] = np.log(float(scale))
        return pose

    def get_pw_norm_scale_factor(self):
        """Compute the global scale normalization factor for pairwise poses.

        When scale normalization is enabled, returns ``exp(log(base_scale)
        - mean(log_scales))`` so that the mean pairwise scale equals
        ``base_scale``. Returns 1 when normalization is disabled.

        Returns:
            float or Tensor: Scalar normalization factor.
        """
        if self.norm_pw_scale:
            # normalize scales so that things cannot go south
            # we want that exp(scale) ~= self.base_scale
            return (np.log(self.base_scale) - self.pw_poses[:, -1].mean()).exp()
        else:
            return 1  # don't norm scale for known poses

    def get_pw_scale(self):
        """Return per-edge scale values (exponentiated and normalized).

        Returns:
            Tensor: Per-edge scale tensor of shape (n_edges,).
        """
        scale = self.pw_poses[:, -1].exp()  # (n_edges,)
        scale = scale * self.get_pw_norm_scale_factor()
        return scale

    def get_pw_poses(self):  # cam to world
        """Return scaled pairwise cam-to-world transformation matrices.

        The rotation and translation columns are multiplied by the per-edge
        scale so that the scene scale is encoded in the translation magnitude.

        Returns:
            Tensor: Homogeneous cam-to-world matrices of shape (n_edges, 4, 4).
        """
        RT = self._get_poses(self.pw_poses)
        scaled_RT = RT.clone()
        scaled_RT[:, :3] *= self.get_pw_scale().view(
            -1, 1, 1
        )  # scale the rotation AND translation
        return scaled_RT

    def get_masks(self):
        """Return per-image binary confidence masks.

        A pixel is included (True) when its confidence exceeds
        ``self.min_conf_thr``.

        Returns:
            list of BoolTensor: Per-image masks of shape (H, W).
        """
        return [(conf > self.min_conf_thr) for conf in self.im_conf]

    def depth_to_pts3d(self):
        """Convert depth maps to 3D point maps in world coordinates.

        Must be implemented by concrete subclasses.

        Raises:
            NotImplementedError: Always raised in this base class.
        """
        raise NotImplementedError()

    def get_pts3d(self, raw=False):
        """Return optimized 3D point maps.

        Args:
            raw (bool): If ``False`` (default), reshape the flat output of
                :meth:`depth_to_pts3d` back to (H, W, 3) tensors.
                If ``True``, return the raw flat tensors.

        Returns:
            list of Tensor: Per-image point maps, each of shape (H, W, 3)
            when *raw* is ``False``, or flat tensors when *raw* is ``True``.
        """
        res = self.depth_to_pts3d()
        if not raw:
            res = [dm[: h * w].view(h, w, 3) for dm, (h, w) in zip(res, self.imshapes)]
        return res

    def _set_focal(self, idx, focal, force=False):
        """Set the focal length for a specific image.

        Must be implemented by concrete subclasses.

        Args:
            idx (int): Image index.
            focal (float): Focal length value.
            force (bool): If ``True``, overwrite even frozen parameters.

        Raises:
            NotImplementedError: Always raised in this base class.
        """
        raise NotImplementedError()

    def get_focals(self):
        """Return per-image focal lengths.

        Must be implemented by concrete subclasses.

        Raises:
            NotImplementedError: Always raised in this base class.
        """
        raise NotImplementedError()

    def get_known_focal_mask(self):
        """Return a boolean mask indicating which images have known (frozen) focal lengths.

        Must be implemented by concrete subclasses.

        Raises:
            NotImplementedError: Always raised in this base class.
        """
        raise NotImplementedError()

    def get_principal_points(self):
        """Return per-image principal points (cx, cy) in pixel coordinates.

        Must be implemented by concrete subclasses.

        Raises:
            NotImplementedError: Always raised in this base class.
        """
        raise NotImplementedError()

    def get_conf(self, mode=None):
        """Return per-image confidence maps after applying a transformation.

        Args:
            mode (str or None): Confidence transformation mode (see
                :func:`get_conf_trf`). If ``None``, uses the mode set
                during initialization.

        Returns:
            list of Tensor: Transformed per-image confidence tensors.
        """
        trf = self.conf_trf if mode is None else get_conf_trf(mode)
        return [trf(c) for c in self.im_conf]

    def get_im_poses(self):
        """Return per-image cam-to-world transformation matrices.

        Must be implemented by concrete subclasses.

        Raises:
            NotImplementedError: Always raised in this base class.
        """
        raise NotImplementedError()

    def _set_depthmap(self, idx, depth, force=False):
        """Set the depth map for a specific image.

        Must be implemented by concrete subclasses.

        Args:
            idx (int): Image index.
            depth (Tensor): Depth map of shape (H, W).
            force (bool): If ``True``, overwrite even frozen parameters.

        Raises:
            NotImplementedError: Always raised in this base class.
        """
        raise NotImplementedError()

    def get_depthmaps(self, raw=False):
        """Return optimized depth maps for all images.

        Must be implemented by concrete subclasses.

        Args:
            raw (bool): If ``False`` (default), return (H, W) shaped tensors.
                If ``True``, return flat tensors.

        Raises:
            NotImplementedError: Always raised in this base class.
        """
        raise NotImplementedError()

    @torch.no_grad()
    def clean_pointcloud(self, tol=0.001, max_bad_conf=0):
        """Method:
        1) express all 3d points in each camera coordinate frame
        2) if they're in front of a depthmap --> then lower their confidence
        """
        assert 0 <= tol < 1
        cams = inv(self.get_im_poses())
        K = self.get_intrinsics()
        depthmaps = self.get_depthmaps()
        res = deepcopy(self)

        for i, pts3d in enumerate(self.depth_to_pts3d()):
            for j in range(self.n_imgs):
                if i == j:
                    continue

                # project 3dpts in other view
                Hi, Wi = self.imshapes[i]
                Hj, Wj = self.imshapes[j]
                proj = geotrf(cams[j], pts3d[: Hi * Wi]).reshape(Hi, Wi, 3)
                proj_depth = proj[:, :, 2]
                u, v = geotrf(K[j], proj, norm=1, ncol=2).round().long().unbind(-1)

                # check which points are actually in the visible cone
                msk_i = (proj_depth > 0) & (0 <= u) & (u < Wj) & (0 <= v) & (v < Hj)
                msk_j = v[msk_i], u[msk_i]

                # find bad points = those in front but less confident
                bad_points = (proj_depth[msk_i] < (1 - tol) * depthmaps[j][msk_j]) & (
                    res.im_conf[i][msk_i] < res.im_conf[j][msk_j]
                )

                bad_msk_i = msk_i.clone()
                bad_msk_i[msk_i] = bad_points
                res.im_conf[i][bad_msk_i] = res.im_conf[i][bad_msk_i].clip_(
                    max=max_bad_conf
                )

        return res

    def forward(self, ret_details=False):
        """Compute the global alignment loss over all pairwise edges.

        For each edge (i, j), the predicted 3D points from both views are
        transformed by the pairwise pose and compared against the globally
        optimized point maps using the configured distance function and
        confidence weighting.

        Args:
            ret_details (bool): If ``True``, also return a matrix of
                per-edge losses. Defaults to ``False``.

        Returns:
            Tensor or tuple: The scalar alignment loss.  When *ret_details*
            is ``True``, returns ``(loss, details)`` where *details* is a
            ``(n_imgs, n_imgs)`` tensor with per-pair loss values
            (``-1`` for absent edges).
        """
        pw_poses = self.get_pw_poses()  # cam-to-world
        pw_adapt = self.get_adaptors()
        proj_pts3d = self.get_pts3d()
        # pre-compute pixel weights
        weight_i = {i_j: self.conf_trf(c) for i_j, c in self.conf_i.items()}
        weight_j = {i_j: self.conf_trf(c) for i_j, c in self.conf_j.items()}

        loss = 0
        if ret_details:
            details = -torch.ones((self.n_imgs, self.n_imgs))

        for e, (i, j) in enumerate(self.edges):
            i_j = edge_str(i, j)
            # distance in image i and j
            aligned_pred_i = geotrf(pw_poses[e], pw_adapt[e] * self.pred_i[i_j])
            aligned_pred_j = geotrf(pw_poses[e], pw_adapt[e] * self.pred_j[i_j])
            li = self.dist(proj_pts3d[i], aligned_pred_i, weight=weight_i[i_j]).mean()
            lj = self.dist(proj_pts3d[j], aligned_pred_j, weight=weight_j[i_j]).mean()
            loss = loss + li + lj

            if ret_details:
                details[i, j] = li + lj
        loss /= self.n_edges  # average over all pairs

        if ret_details:
            return loss, details
        return loss

    @torch.amp.autocast("cuda", enabled=False)
    def compute_global_alignment(self, init=None, niter_PnP=10, **kw):
        """Run the full global alignment pipeline.

        Optionally initializes poses using a minimum spanning tree or known
        poses, then delegates to :func:`global_alignment_loop`.

        Args:
            init (str or None): Initialization strategy.  One of:
                ``None`` – skip initialization,
                ``'msp'`` / ``'mst'`` – minimum spanning tree,
                ``'known_poses'`` – initialize from preset poses.
            niter_PnP (int): Number of PnP iterations for pose initialization.
                Defaults to ``10``.
            **kw: Additional keyword arguments forwarded to
                :func:`global_alignment_loop` (e.g. ``lr``, ``niter``,
                ``schedule``).

        Returns:
            float: Final alignment loss after optimization.

        Raises:
            ValueError: If *init* is not one of the supported options.
        """
        if init is None:
            pass
        elif init == "msp" or init == "mst":
            init_fun.init_minimum_spanning_tree(self, niter_PnP=niter_PnP)
        elif init == "known_poses":
            init_fun.init_from_known_poses(
                self, min_conf_thr=self.min_conf_thr, niter_PnP=niter_PnP
            )
        else:
            raise ValueError(f"bad value for {init=}")

        return global_alignment_loop(self, **kw)

    @torch.no_grad()
    def mask_sky(self):
        """Return a copy of this optimizer with sky pixels zeroed out in confidence maps.

        Uses :func:`~fast3r.dust3r.viz.segment_sky` to detect sky regions in
        the stored RGB images and sets the corresponding confidence values to 0.

        Returns:
            BasePCOptimizer: A deep-copied optimizer with sky pixels masked.

        Raises:
            AttributeError: If ``self.imgs`` is ``None``.
        """
        res = deepcopy(self)
        for i in range(self.n_imgs):
            sky = segment_sky(self.imgs[i])
            res.im_conf[i][sky] = 0
        return res

    def show(self, show_pw_cams=False, show_pw_pts3d=False, cam_size=None, **kw):
        """Visualize the optimized scene in a 3D viewer.

        Renders the reconstructed point cloud, camera frustums, and
        optionally the raw pairwise predictions using
        :class:`~fast3r.dust3r.viz.SceneViz`.

        Args:
            show_pw_cams (bool): If ``True``, also render per-edge pairwise
                camera poses. Defaults to ``False``.
            show_pw_pts3d (bool): If ``True`` and *show_pw_cams* is also
                ``True``, render the raw pairwise 3D points.
                Defaults to ``False``.
            cam_size (float or None): Camera frustum display size. If
                ``None``, auto-computed from the pose spread.
            **kw: Additional keyword arguments forwarded to
                :meth:`~fast3r.dust3r.viz.SceneViz.show`.

        Returns:
            SceneViz: The scene visualization object.
        """
        viz = SceneViz()
        if self.imgs is None:
            colors = np.random.randint(0, 256, size=(self.n_imgs, 3))
            colors = list(map(tuple, colors.tolist()))
            for n in range(self.n_imgs):
                viz.add_pointcloud(self.get_pts3d()[n], colors[n], self.get_masks()[n])
        else:
            viz.add_pointcloud(self.get_pts3d(), self.imgs, self.get_masks())
            colors = np.random.randint(256, size=(self.n_imgs, 3))

        # camera poses
        im_poses = to_numpy(self.get_im_poses())
        if cam_size is None:
            cam_size = auto_cam_size(im_poses)
        viz.add_cameras(
            im_poses,
            self.get_focals(),
            colors=colors,
            images=self.imgs,
            imsizes=self.imsizes,
            cam_size=cam_size,
        )
        if show_pw_cams:
            pw_poses = self.get_pw_poses()
            viz.add_cameras(pw_poses, color=(192, 0, 192), cam_size=cam_size)

            if show_pw_pts3d:
                pts = [
                    geotrf(pw_poses[e], self.pred_i[edge_str(i, j)])
                    for e, (i, j) in enumerate(self.edges)
                ]
                viz.add_pointcloud(pts, (128, 0, 128))

        viz.show(**kw)
        return viz


def global_alignment_loop(net, lr=0.01, niter=300, schedule="cosine", lr_min=1e-6):
    """Run the Adam optimization loop for global scene alignment.

    Iterates for *niter* steps, adjusting the learning rate according to
    the chosen schedule, and prints a tqdm progress bar when verbose mode
    is enabled.

    Args:
        net (BasePCOptimizer): The optimizer module whose trainable
            parameters will be updated.
        lr (float): Initial learning rate. Defaults to ``0.01``.
        niter (int): Total number of optimization iterations. Defaults to
            ``300``.
        schedule (str): Learning rate schedule, either ``'cosine'`` or
            ``'linear'``. Defaults to ``'cosine'``.
        lr_min (float): Minimum (final) learning rate. Defaults to ``1e-6``.

    Returns:
        float: The final loss value after the last iteration, or
        ``float('inf')`` if there are no trainable parameters.
    """
    params = [p for p in net.parameters() if p.requires_grad]
    if not params:
        return net

    verbose = net.verbose
    if verbose:
        print("Global alignement - optimizing for:")
        print([name for name, value in net.named_parameters() if value.requires_grad])

    lr_base = lr
    optimizer = torch.optim.Adam(params, lr=lr, betas=(0.9, 0.9))

    loss = float("inf")
    if verbose:
        with tqdm.tqdm(total=niter) as bar:
            while bar.n < bar.total:
                loss, lr = global_alignment_iter(
                    net, bar.n, niter, lr_base, lr_min, optimizer, schedule
                )
                bar.set_postfix_str(f"{lr=:g} loss={loss:g}")
                bar.update()
    else:
        for n in range(niter):
            loss, _ = global_alignment_iter(
                net, n, niter, lr_base, lr_min, optimizer, schedule
            )
    return loss


def global_alignment_iter(net, cur_iter, niter, lr_base, lr_min, optimizer, schedule):
    """Execute a single optimization iteration for global scene alignment.

    Adjusts the learning rate, zeroes gradients, computes the forward loss,
    back-propagates, and steps the optimizer.

    Args:
        net (BasePCOptimizer): The optimizer module.
        cur_iter (int): Current iteration index (0-based).
        niter (int): Total number of iterations (used to compute ``t``).
        lr_base (float): Initial learning rate.
        lr_min (float): Minimum learning rate.
        optimizer (torch.optim.Optimizer): The Adam optimizer instance.
        schedule (str): Learning rate schedule, ``'cosine'`` or ``'linear'``.

    Returns:
        tuple: ``(loss, lr)`` where *loss* is the scalar loss value (float)
        and *lr* is the learning rate used in this iteration.

    Raises:
        ValueError: If *schedule* is not ``'cosine'`` or ``'linear'``.
    """
    t = cur_iter / niter
    if schedule == "cosine":
        lr = cosine_schedule(t, lr_base, lr_min)
    elif schedule == "linear":
        lr = linear_schedule(t, lr_base, lr_min)
    else:
        raise ValueError(f"bad lr {schedule=}")
    adjust_learning_rate_by_lr(optimizer, lr)
    optimizer.zero_grad()
    loss = net()
    loss.backward()
    optimizer.step()

    return float(loss), lr
