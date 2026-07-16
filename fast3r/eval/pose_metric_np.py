"""纯 NumPy 实现的相机位姿评估指标。

作为 ``cam_pose_metric.py`` 的 fallback，在无法安装 PyTorch / PyTorch3D
的运行环境中使用。输入输出约定与 ``cam_pose_metric.py`` 保持一致。
"""

import numpy as np


def closed_form_inverse(se3):
    """计算一批 4x4 SE(3) 矩阵的逆。"""
    R = se3[:, :3, :3]
    t = se3[:, :3, 3]
    R_inv = R.transpose(0, 2, 1)
    t_inv = -np.einsum("Bij,Bj->Bi", R_inv, t)
    inv = np.zeros_like(se3)
    inv[:, :3, :3] = R_inv
    inv[:, :3, 3] = t_inv
    inv[:, 3, 3] = 1.0
    return inv


def rotation_angle(rot_gt, rot_pred):
    """计算两组旋转矩阵之间的相对旋转角度（度）。"""
    R_rel = np.einsum("Bij,Bjk->Bik", rot_gt.transpose(0, 2, 1), rot_pred)
    trace = np.trace(R_rel, axis1=1, axis2=2)
    cos_theta = np.clip((trace - 1.0) / 2.0, -1.0, 1.0)
    theta = np.arccos(cos_theta)
    return np.degrees(theta)


def translation_angle(tvec_gt, tvec_pred, eps=1e-15, default_err=1e6):
    """计算两组平移向量之间的角度误差（度）。"""
    t_pred = tvec_pred / (np.linalg.norm(tvec_pred, axis=1, keepdims=True) + eps)
    t_gt = tvec_gt / (np.linalg.norm(tvec_gt, axis=1, keepdims=True) + eps)
    dot = np.sum(t_pred * t_gt, axis=1)
    loss = np.clip(1.0 - dot ** 2, eps, None)
    err = np.arccos(np.sqrt(1.0 - loss))
    err[np.isnan(err) | np.isinf(err)] = default_err
    return np.degrees(err)


def camera_to_rel_deg(pred_cameras_c2w, gt_cameras_c2w, batch_size=None):
    """计算预测相机与真实相机之间的相对旋转/平移角误差（度）。"""
    B = pred_cameras_c2w.shape[0]
    pairs = []
    for i in range(B):
        for j in range(i + 1, B):
            pairs.append((i, j))
    pairs = np.array(pairs)

    gt_rel = np.einsum(
        "Bij,Bjk->Bik",
        closed_form_inverse(gt_cameras_c2w[pairs[:, 0]]),
        gt_cameras_c2w[pairs[:, 1]],
    )
    pred_rel = np.einsum(
        "Bij,Bjk->Bik",
        closed_form_inverse(pred_cameras_c2w[pairs[:, 0]]),
        pred_cameras_c2w[pairs[:, 1]],
    )

    rel_rangle_deg = rotation_angle(gt_rel[:, :3, :3], pred_rel[:, :3, :3])
    rel_tangle_deg = translation_angle(gt_rel[:, :3, 3], pred_rel[:, :3, 3])

    if batch_size is not None:
        rel_rangle_deg = rel_rangle_deg.reshape(batch_size, -1)
        rel_tangle_deg = rel_tangle_deg.reshape(batch_size, -1)

    return rel_rangle_deg, rel_tangle_deg


def calculate_auc_np(r_error, t_error, max_threshold=30):
    """计算相对位姿误差的 AUC 指标。"""
    max_errors = np.maximum(r_error, t_error)
    bins = np.arange(max_threshold + 1)
    histogram, _ = np.histogram(max_errors, bins=bins)
    normalized = histogram.astype(float) / len(max_errors)
    return float(np.mean(np.cumsum(normalized)))


def compute_ate(pred_cameras_c2w, gt_cameras_c2w):
    """计算绝对轨迹误差（ATE）。"""
    pred_t = pred_cameras_c2w[:, :3, 3]
    gt_t = gt_cameras_c2w[:, :3, 3]

    pred_centered = pred_t - pred_t.mean(axis=0)
    gt_centered = gt_t - gt_t.mean(axis=0)

    H = gt_centered.T @ pred_centered / len(pred_t)
    U, S, Vt = np.linalg.svd(H)
    R = U @ Vt
    if np.linalg.det(R) < 0:
        U[:, -1] *= -1
        R = U @ Vt

    scale = np.trace(np.diag(S)) / np.sum(pred_centered ** 2)
    t = gt_t.mean(axis=0) - scale * R @ pred_t.mean(axis=0)

    aligned_pred = scale * (R @ pred_t.T).T + t
    ate = np.sqrt(np.mean(np.sum((aligned_pred - gt_t) ** 2, axis=1)))
    return float(ate)


def compute_rpe(pred_cameras_c2w, gt_cameras_c2w):
    """计算相对位姿误差（RPE）。"""
    N = pred_cameras_c2w.shape[0]
    r_errors = []
    t_errors = []
    for i in range(N - 1):
        gt_rel = np.linalg.inv(gt_cameras_c2w[i]) @ gt_cameras_c2w[i + 1]
        pred_rel = np.linalg.inv(pred_cameras_c2w[i]) @ pred_cameras_c2w[i + 1]

        R_err = np.linalg.inv(gt_rel[:3, :3]) @ pred_rel[:3, :3]
        trace = np.trace(R_err)
        r_errors.append(np.degrees(np.arccos(np.clip((trace - 1) / 2, -1, 1))))

        t_errors.append(np.linalg.norm(gt_rel[:3, 3] - pred_rel[:3, 3]))

    return {
        "rotation_rmse": float(np.sqrt(np.mean(np.array(r_errors) ** 2))),
        "translation_rmse": float(np.sqrt(np.mean(np.array(t_errors) ** 2))),
    }
