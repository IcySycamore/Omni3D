# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (C) 2024-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).
#
# --------------------------------------------------------
# Initialization functions for global alignment
# --------------------------------------------------------
from functools import cache

import cv2
import numpy as np
import roma
import scipy.sparse as sp
import torch
from tqdm import tqdm

from fast3r.dust3r.cloud_opt.commons import compute_edge_scores, edge_str, i_j_ij
from fast3r.dust3r.post_process import estimate_focal_knowing_depth
from fast3r.dust3r.utils.geometry import geotrf, get_med_dist_between_poses, inv
from fast3r.dust3r.viz import to_numpy


@torch.no_grad()
def init_from_known_poses(self, niter_PnP=10, min_conf_thr=3):
    """Initialize all pairwise poses when all image poses are known.

    Uses PnP to estimate relative pairwise poses, then aligns them to the
    known ground-truth poses via rigid similarity transformation.
    Also initializes per-image depth maps from the best-confidence pairwise
    prediction.

    Args:
        self (BasePCOptimizer): The optimizer instance.
        niter_PnP (int): Number of RANSAC-PnP iterations. Defaults to ``10``.
        min_conf_thr (float): Minimum confidence threshold for PnP masking.
            Defaults to ``3``.

    Raises:
        AssertionError: If not all image poses are known.
    """
    device = self.device

    # indices of known poses
    nkp, known_poses_msk, known_poses = get_known_poses(self)
    assert nkp == self.n_imgs, "not all poses are known"

    # get all focals
    nkf, _, im_focals = get_known_focals(self)
    assert nkf == self.n_imgs
    im_pp = self.get_principal_points()

    best_depthmaps = {}
    # init all pairwise poses
    for e, (i, j) in enumerate(tqdm(self.edges, disable=not self.verbose)):
        i_j = edge_str(i, j)

        # find relative pose for this pair
        P1 = torch.eye(4, device=device)
        msk = self.conf_i[i_j] > min(min_conf_thr, self.conf_i[i_j].min() - 0.1)
        _, P2 = fast_pnp(
            self.pred_j[i_j],
            float(im_focals[i].mean()),
            pp=im_pp[i],
            msk=msk,
            device=device,
            niter_PnP=niter_PnP,
        )

        # align the two predicted camera with the two gt cameras
        s, R, T = align_multiple_poses(torch.stack((P1, P2)), known_poses[[i, j]])
        # normally we have known_poses[i] ~= sRT_to_4x4(s,R,T,device) @ P1
        # and geotrf(sRT_to_4x4(1,R,T,device), s*P2[:3,3])
        self._set_pose(self.pw_poses, e, R, T, scale=s)

        # remember if this is a good depthmap
        score = float(self.conf_i[i_j].mean())
        if score > best_depthmaps.get(i, (0,))[0]:
            best_depthmaps[i] = score, i_j, s

    # init all image poses
    for n in range(self.n_imgs):
        assert known_poses_msk[n]
        _, i_j, scale = best_depthmaps[n]
        depth = self.pred_i[i_j][:, :, 2]
        self._set_depthmap(n, depth * scale)


@torch.no_grad()
def init_minimum_spanning_tree(self, **kw):
    """Init all camera poses (image-wise and pairwise poses) given
    an initial set of pairwise estimations.
    """
    device = self.device
    pts3d, _, im_focals, im_poses = minimum_spanning_tree(
        self.imshapes,
        self.edges,
        self.pred_i,
        self.pred_j,
        self.conf_i,
        self.conf_j,
        self.im_conf,
        self.min_conf_thr,
        device,
        has_im_poses=self.has_im_poses,
        verbose=self.verbose,
        **kw,
    )

    return init_from_pts3d(self, pts3d, im_focals, im_poses)


def init_from_pts3d(self, pts3d, im_focals, im_poses):
    """Initialize optimizer parameters from pre-computed 3D point maps.

    If some camera poses are already known, aligns the provided poses to
    them via a global similarity transform.  Then sets pairwise poses,
    depth maps, per-image poses, and focal lengths.

    Args:
        self (BasePCOptimizer): The optimizer instance.
        pts3d (list of Tensor): Per-image 3D point maps of shape (H, W, 3).
        im_focals (list of float or None): Per-image focal length estimates;
            ``None`` entries are skipped.
        im_poses (Tensor): Per-image cam-to-world matrices of shape
            (n_imgs, 4, 4).
    """
    # init poses
    nkp, known_poses_msk, known_poses = get_known_poses(self)
    if nkp == 1:
        raise NotImplementedError(
            "Would be simpler to just align everything afterwards on the single known pose"
        )
    elif nkp > 1:
        # global rigid SE3 alignment
        s, R, T = align_multiple_poses(
            im_poses[known_poses_msk], known_poses[known_poses_msk]
        )
        trf = sRT_to_4x4(s, R, T, device=known_poses.device)

        # rotate everything
        im_poses = trf @ im_poses
        im_poses[:, :3, :3] /= s  # undo scaling on the rotation part
        for img_pts3d in pts3d:
            img_pts3d[:] = geotrf(trf, img_pts3d)

    # set all pairwise poses
    for e, (i, j) in enumerate(self.edges):
        i_j = edge_str(i, j)
        # compute transform that goes from cam to world
        s, R, T = rigid_points_registration(
            self.pred_i[i_j], pts3d[i], conf=self.conf_i[i_j]
        )
        self._set_pose(self.pw_poses, e, R, T, scale=s)

    # take into account the scale normalization
    s_factor = self.get_pw_norm_scale_factor()
    im_poses[:, :3, 3] *= s_factor  # apply downscaling factor
    for img_pts3d in pts3d:
        img_pts3d *= s_factor

    # init all image poses
    if self.has_im_poses:
        for i in range(self.n_imgs):
            cam2world = im_poses[i]
            depth = geotrf(inv(cam2world), pts3d[i])[..., 2]
            self._set_depthmap(i, depth)
            self._set_pose(self.im_poses, i, cam2world)
            if im_focals[i] is not None:
                self._set_focal(i, im_focals[i])

    if self.verbose:
        print(" init loss =", float(self()))


def minimum_spanning_tree(
    imshapes,
    edges,
    pred_i,
    pred_j,
    conf_i,
    conf_j,
    im_conf,
    min_conf_thr,
    device,
    has_im_poses=True,
    niter_PnP=10,
    verbose=True,
):
    """Build a consistent 3D scene by traversing the minimum spanning tree of the pairwise graph.

    Constructs a confidence-weighted graph over all image pairs, computes
    its minimum spanning tree, and propagates 3D point maps along tree
    edges starting from the most-confident pair.  Optionally estimates
    camera poses via PnP for images not covered by the tree.

    Args:
        imshapes (list of tuple): Per-image (H, W) shapes.
        edges (list of tuple): List of (i, j) edge pairs.
        pred_i (dict): Per-edge source 3D point predictions.
        pred_j (dict): Per-edge target 3D point predictions.
        conf_i (dict): Per-edge source confidence maps.
        conf_j (dict): Per-edge target confidence maps.
        im_conf (list of Tensor): Per-image aggregated confidence maps.
        min_conf_thr (float): Confidence threshold for PnP masking.
        device (torch.device): Compute device.
        has_im_poses (bool): Whether to estimate per-image poses.
            Defaults to ``True``.
        niter_PnP (int): RANSAC-PnP iteration count. Defaults to ``10``.
        verbose (bool): Print progress messages. Defaults to ``True``.

    Returns:
        tuple: ``(pts3d, msp_edges, im_focals, im_poses)`` where

        - *pts3d* – list of per-image (H, W, 3) point maps;
        - *msp_edges* – list of (i, j) tuples for MST edges used;
        - *im_focals* – list of estimated focal lengths (or ``None`` each);
        - *im_poses* – stacked (n_imgs, 4, 4) cam-to-world matrices,
          or ``None`` when *has_im_poses* is ``False``.
    """
    n_imgs = len(imshapes)
    sparse_graph = -dict_to_sparse_graph(
        compute_edge_scores(map(i_j_ij, edges), conf_i, conf_j)
    )
    msp = sp.csgraph.minimum_spanning_tree(sparse_graph).tocoo()

    # temp variable to store 3d points
    pts3d = [None] * len(imshapes)

    todo = sorted(zip(-msp.data, msp.row, msp.col))  # sorted edges
    im_poses = [None] * n_imgs
    im_focals = [None] * n_imgs

    # init with strongest edge
    score, i, j = todo.pop()
    if verbose:
        print(f" init edge ({i}*,{j}*) {score=}")
    i_j = edge_str(i, j)
    pts3d[i] = pred_i[i_j].clone()
    pts3d[j] = pred_j[i_j].clone()
    done = {i, j}
    if has_im_poses:
        im_poses[i] = torch.eye(4, device=device)
        im_focals[i] = estimate_focal(pred_i[i_j])

    # set initial pointcloud based on pairwise graph
    msp_edges = [(i, j)]
    while todo:
        # each time, predict the next one
        score, i, j = todo.pop()

        if im_focals[i] is None:
            im_focals[i] = estimate_focal(pred_i[i_j])

        if i in done:
            if verbose:
                print(f" init edge ({i},{j}*) {score=}")
            assert j not in done
            # align pred[i] with pts3d[i], and then set j accordingly
            i_j = edge_str(i, j)
            s, R, T = rigid_points_registration(pred_i[i_j], pts3d[i], conf=conf_i[i_j])
            trf = sRT_to_4x4(s, R, T, device)
            pts3d[j] = geotrf(trf, pred_j[i_j])
            done.add(j)
            msp_edges.append((i, j))

            if has_im_poses and im_poses[i] is None:
                im_poses[i] = sRT_to_4x4(1, R, T, device)

        elif j in done:
            if verbose:
                print(f" init edge ({i}*,{j}) {score=}")
            assert i not in done
            i_j = edge_str(i, j)
            s, R, T = rigid_points_registration(pred_j[i_j], pts3d[j], conf=conf_j[i_j])
            trf = sRT_to_4x4(s, R, T, device)
            pts3d[i] = geotrf(trf, pred_i[i_j])
            done.add(i)
            msp_edges.append((i, j))

            if has_im_poses and im_poses[i] is None:
                im_poses[i] = sRT_to_4x4(1, R, T, device)
        else:
            # let's try again later
            todo.insert(0, (score, i, j))

    if has_im_poses:
        # complete all missing informations
        pair_scores = list(
            sparse_graph.values()
        )  # already negative scores: less is best
        edges_from_best_to_worse = np.array(list(sparse_graph.keys()))[
            np.argsort(pair_scores)
        ]
        for i, j in edges_from_best_to_worse.tolist():
            if im_focals[i] is None:
                im_focals[i] = estimate_focal(pred_i[edge_str(i, j)])

        for i in range(n_imgs):
            if im_poses[i] is None:
                msk = im_conf[i] > min_conf_thr
                res = fast_pnp(
                    pts3d[i], im_focals[i], msk=msk, device=device, niter_PnP=niter_PnP
                )
                if res:
                    im_focals[i], im_poses[i] = res
            if im_poses[i] is None:
                im_poses[i] = torch.eye(4, device=device)
        im_poses = torch.stack(im_poses)
    else:
        im_poses = im_focals = None

    return pts3d, msp_edges, im_focals, im_poses


def dict_to_sparse_graph(dic):
    """Convert an edge-score dictionary to a sparse adjacency matrix.

    Args:
        dic (dict): Mapping from (i, j) edge tuple to scalar score value.

    Returns:
        scipy.sparse.dok_array: Sparse (n_imgs, n_imgs) matrix.
    """
    n_imgs = max(max(e) for e in dic) + 1
    res = sp.dok_array((n_imgs, n_imgs))
    for edge, value in dic.items():
        res[edge] = value
    return res


def rigid_points_registration(pts1, pts2, conf):
    """Estimate a weighted rigid similarity transform (sRT) between two point sets.

    Wraps ``roma.rigid_points_registration`` with confidence-based weighting
    and returns scale, rotation, and translation separately.

    Args:
        pts1 (Tensor): Source points of shape (H, W, 3) or (N, 3).
        pts2 (Tensor): Target points with the same shape as *pts1*.
        conf (Tensor): Per-point confidence weights, same leading shape.

    Returns:
        tuple: ``(s, R, T)`` where *s* is the scalar scale, *R* is the
        (3, 3) rotation matrix, and *T* is the (3,) translation vector.
    """
    R, T, s = roma.rigid_points_registration(
        pts1.reshape(-1, 3),
        pts2.reshape(-1, 3),
        weights=conf.ravel(),
        compute_scaling=True,
    )
    return s, R, T  # return un-scaled (R, T)


def sRT_to_4x4(scale, R, T, device):
    """Compose a similarity transform into a 4x4 homogeneous matrix.

    Constructs ``[[s*R, T], [0, 0, 0, 1]]``.

    Args:
        scale (float): Scalar scale factor.
        R (Tensor): (3, 3) rotation matrix.
        T (Tensor): (3,) or (3, 1) translation vector.
        device (torch.device): Target device.

    Returns:
        Tensor: (4, 4) homogeneous transformation matrix.
    """
    trf = torch.eye(4, device=device)
    trf[:3, :3] = R * scale
    trf[:3, 3] = T.ravel()  # doesn't need scaling
    return trf


def estimate_focal(pts3d_i, pp=None):
    """Estimate focal length from a 3D point map using the Weiszfeld algorithm.

    Args:
        pts3d_i (Tensor): Per-pixel 3D point map of shape (H, W, 3) in
            camera coordinates.
        pp (Tensor or None): Principal point (cx, cy).  If ``None``,
            defaults to the image centre.

    Returns:
        float: Estimated focal length in pixels.
    """
    if pp is None:
        H, W, THREE = pts3d_i.shape
        assert THREE == 3
        pp = torch.tensor((W / 2, H / 2), device=pts3d_i.device)
    focal = estimate_focal_knowing_depth(
        pts3d_i.unsqueeze(0), pp.unsqueeze(0), focal_mode="weiszfeld"
    ).ravel()
    return float(focal)


@cache
def pixel_grid(H, W):
    """Return a (H, W, 2) pixel coordinate grid (cached).

    Args:
        H (int): Image height.
        W (int): Image width.

    Returns:
        np.ndarray: Float32 array of shape (H, W, 2) containing (x, y)
        pixel coordinates.
    """
    return np.mgrid[:W, :H].T.astype(np.float32)


def fast_pnp(pts3d, focal, msk, device, pp=None, niter_PnP=10, num_guessed_focals=100):
    """Estimate camera pose (and optionally focal length) from a 3D point map using RANSAC-PnP.

    If *focal* is ``None``, tries ``num_guessed_focals`` logarithmically
    spaced focal values and selects the one with the most PnP inliers.

    Args:
        pts3d (Tensor): Per-pixel 3D point map of shape (H, W, 3) in
            camera coordinates.
        focal (float or None): Known focal length.  Pass ``None`` to
            estimate it automatically.
        msk (BoolTensor): Confidence mask of shape (H, W); only masked
            pixels are used for PnP.
        device (torch.device): Target device for the output pose.
        pp (Tensor or None): Principal point (cx, cy).  Defaults to image
            centre.
        niter_PnP (int): ``iterationsCount`` passed to
            ``cv2.solvePnPRansac``. Defaults to ``10``.
        num_guessed_focals (int): Number of focal length candidates when
            *focal* is ``None``. Defaults to ``100``.

    Returns:
        tuple: ``(best_focal, cam2world)`` where *best_focal* is the
        estimated focal (float) and *cam2world* is a (4, 4) tensor,
        or ``(None, None)`` if PnP fails.
    """
    # extract camera poses and focals with RANSAC-PnP
    if msk.sum() < 4:
        return None, None  # we need at least 4 points for PnP
    pts3d, msk = map(to_numpy, (pts3d, msk))

    H, W, THREE = pts3d.shape
    assert THREE == 3
    pixels = pixel_grid(H, W)

    if focal is None:
        S = max(W, H)
        tentative_focals = np.geomspace(S / 2, S * 3, num=num_guessed_focals)
    else:
        tentative_focals = [focal]

    if pp is None:
        pp = (W / 2, H / 2)
    else:
        pp = to_numpy(pp)

    best = (0,)
    for focal in tentative_focals:
        K = np.float32([(focal, 0, pp[0]), (0, focal, pp[1]), (0, 0, 1)])

        try:  # solvePnPRansac is not always solvable, especially when the predicted points are not very good
            success, R, T, inliers = cv2.solvePnPRansac(
                pts3d[msk],
                pixels[msk],
                K,
                None,
                iterationsCount=niter_PnP,
                reprojectionError=5,
                flags=cv2.SOLVEPNP_SQPNP,
            )
            if not success:
                continue
        except cv2.error:
            continue

        score = len(inliers)
        if success and score > best[0]:
            best = score, R, T, focal

    if not best[0]:
        return None, None

    _, R, T, best_focal = best
    R = cv2.Rodrigues(R)[0]  # world to cam
    R, T = map(torch.from_numpy, (R, T))
    return best_focal, inv(sRT_to_4x4(1, R, T, device))  # cam to world


def get_known_poses(self):
    """Return the known (frozen) camera poses and their mask.

    Args:
        self (BasePCOptimizer): The optimizer instance.

    Returns:
        tuple: ``(nkp, known_poses_msk, known_poses)`` where *nkp* is the
        count of known poses (int/Tensor), *known_poses_msk* is a bool
        tensor of length ``n_imgs``, and *known_poses* is a (n_imgs, 4, 4)
        tensor, or ``(0, None, None)`` when no image poses exist.
    """
    if self.has_im_poses:
        known_poses_msk = torch.tensor([not (p.requires_grad) for p in self.im_poses])
        known_poses = self.get_im_poses()
        return known_poses_msk.sum(), known_poses_msk, known_poses
    else:
        return 0, None, None


def get_known_focals(self):
    """Return the known (frozen) focal lengths and their mask.

    Args:
        self (BasePCOptimizer): The optimizer instance.

    Returns:
        tuple: ``(nkf, known_focal_msk, known_focals)`` where *nkf* is the
        count of known focals, *known_focal_msk* is a bool tensor, and
        *known_focals* is a tensor of focal lengths, or
        ``(0, None, None)`` when no image poses exist.
    """
    if self.has_im_poses:
        known_focal_msk = self.get_known_focal_mask()
        known_focals = self.get_focals()
        return known_focal_msk.sum(), known_focal_msk, known_focals
    else:
        return 0, None, None


def align_multiple_poses(src_poses, target_poses):
    """Align a set of source poses to target poses via weighted rigid registration.

    Uses the camera centres and forward-direction points as correspondence
    set for ``roma.rigid_points_registration``.

    Args:
        src_poses (Tensor): Source cam-to-world matrices of shape (N, 4, 4).
        target_poses (Tensor): Target cam-to-world matrices of shape (N, 4, 4).

    Returns:
        tuple: ``(s, R, T)`` – scalar scale, (3, 3) rotation matrix,
        (3,) translation vector of the similarity transform that maps
        *src_poses* to *target_poses*.
    """
    N = len(src_poses)
    assert src_poses.shape == target_poses.shape == (N, 4, 4)

    def center_and_z(poses):
        eps = get_med_dist_between_poses(poses) / 100
        return torch.cat((poses[:, :3, 3], poses[:, :3, 3] + eps * poses[:, :3, 2]))

    R, T, s = roma.rigid_points_registration(
        center_and_z(src_poses), center_and_z(target_poses), compute_scaling=True
    )
    return s, R, T
