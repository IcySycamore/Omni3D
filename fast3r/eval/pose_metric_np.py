"""纯 NumPy 实现的相机位姿评估指标。

作为 ``cam_pose_metric.py`` 的 fallback，在无法满足项目所需 PyTorch /
PyTorch3D 版本（例如缺少 torch 2.3+ 的 ``torch.nn.attention``）的运行环境
中使用。输入输出约定与 ``cam_pose_metric.py`` 保持一致。
"""

import numpy as np


def closed_form_inverse(se3):
    """计算一批 4x4 SE(3) 矩阵的逆。

    Args:
        se3 (ndarray): 形状 (N, 4, 4) 的 SE(3) 矩阵。

    Returns:
        ndarray: 形状 (N, 4, 4) 的逆矩阵。
    """
    rotation = se3[:, :3, :3]
    translation = se3[:, :3, 3]
    rotation_inv = rotation.transpose(0, 2, 1)
    translation_inv = -np.einsum("Bij,Bj->Bi", rotation_inv, translation)
    inverse = np.zeros_like(se3)
    inverse[:, :3, :3] = rotation_inv
    inverse[:, :3, 3] = translation_inv
    inverse[:, 3, 3] = 1.0
    return inverse


def rotation_angle(rot_gt, rot_pred):
    """计算两组旋转矩阵之间的相对旋转角度（度）。

    Args:
        rot_gt (ndarray): 真实旋转矩阵，形状 (B, 3, 3)。
        rot_pred (ndarray): 预测旋转矩阵，形状 (B, 3, 3)。

    Returns:
        ndarray: 相对旋转角度（度），形状 (B,)。
    """
    rotation_rel = np.einsum("Bij,Bjk->Bik", rot_gt.transpose(0, 2, 1), rot_pred)
    trace = np.trace(rotation_rel, axis1=1, axis2=2)
    cos_theta = np.clip((trace - 1.0) / 2.0, -1.0, 1.0)
    theta = np.arccos(cos_theta)
    return np.degrees(theta)


def translation_angle(tvec_gt, tvec_pred, eps=1e-15, default_err=1e6):
    """计算两组平移向量之间的角度误差（度）。

    Args:
        tvec_gt (ndarray): 真实平移向量，形状 (B, 3)。
        tvec_pred (ndarray): 预测平移向量，形状 (B, 3)。
        eps (float): 数值稳定常数。
        default_err (float): 异常值默认误差。

    Returns:
        ndarray: 平移角度误差（度），形状 (B,)。
    """
    t_pred = tvec_pred / (np.linalg.norm(tvec_pred, axis=1, keepdims=True) + eps)
    t_gt = tvec_gt / (np.linalg.norm(tvec_gt, axis=1, keepdims=True) + eps)
    dot = np.sum(t_pred * t_gt, axis=1)
    loss = np.clip(1.0 - dot ** 2, eps, None)
    err = np.arccos(np.sqrt(1.0 - loss))
    err[np.isnan(err) | np.isinf(err)] = default_err
    return np.degrees(err)


def camera_to_rel_deg(pred_cameras_c2w, gt_cameras_c2w, batch_size=None):
    """计算预测相机与真实相机之间的相对旋转/平移角误差（度）。

    Args:
        pred_cameras_c2w (ndarray): 预测 c2w 矩阵，形状 (B, 4, 4)。
        gt_cameras_c2w (ndarray): 真实 c2w 矩阵，形状 (B, 4, 4)。
        batch_size (int | None): 若指定，结果 reshape 为 (batch_size, -1)。

    Returns:
        tuple[ndarray, ndarray]: (rel_rangle_deg, rel_tangle_deg)。
    """
    num_views = pred_cameras_c2w.shape[0]
    pairs = []
    for i in range(num_views):
        for j in range(i + 1, num_views):
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
    """计算相对位姿误差的 AUC 指标。

    Args:
        r_error (ndarray): 旋转误差数组（度）。
        t_error (ndarray): 平移误差数组（度）。
        max_threshold (int): 最大阈值。

    Returns:
        float: AUC 值。
    """
    max_errors = np.maximum(r_error, t_error)
    bins = np.arange(max_threshold + 1)
    histogram, _ = np.histogram(max_errors, bins=bins)
    normalized = histogram.astype(float) / len(max_errors)
    return float(np.mean(np.cumsum(normalized)))


def compute_ate(pred_cameras_c2w, gt_cameras_c2w):
    """计算绝对轨迹误差（ATE）。

    先将预测轨迹与 GT 轨迹进行 Sim(3) 对齐，再计算 RMSE。

    Args:
        pred_cameras_c2w (ndarray): 预测 c2w 矩阵，形状 (N, 4, 4)。
        gt_cameras_c2w (ndarray): 真实 c2w 矩阵，形状 (N, 4, 4)。

    Returns:
        float: ATE RMSE。
    """
    pred_t = pred_cameras_c2w[:, :3, 3]
    gt_t = gt_cameras_c2w[:, :3, 3]

    pred_centered = pred_t - pred_t.mean(axis=0)
    gt_centered = gt_t - gt_t.mean(axis=0)

    cross_cov = gt_centered.T @ pred_centered / len(pred_t)
    u_matrix, s_matrix, vt_matrix = np.linalg.svd(cross_cov)
    rotation = u_matrix @ vt_matrix
    if np.linalg.det(rotation) < 0:
        u_matrix[:, -1] *= -1
        rotation = u_matrix @ vt_matrix

    scale = np.trace(np.diag(s_matrix)) / np.sum(pred_centered ** 2)
    translation = gt_t.mean(axis=0) - scale * rotation @ pred_t.mean(axis=0)

    aligned_pred = scale * (rotation @ pred_t.T).T + translation
    ate = np.sqrt(np.mean(np.sum((aligned_pred - gt_t) ** 2, axis=1)))
    return float(ate)


def compute_rpe(pred_cameras_c2w, gt_cameras_c2w):
    """计算相对位姿误差（RPE）。

    Args:
        pred_cameras_c2w (ndarray): 预测 c2w 矩阵，形状 (N, 4, 4)。
        gt_cameras_c2w (ndarray): 真实 c2w 矩阵，形状 (N, 4, 4)。

    Returns:
        dict: 包含 rotation_rmse、translation_rmse 的字典。
    """
    num_views = pred_cameras_c2w.shape[0]
    r_errors = []
    t_errors = []
    for i in range(num_views - 1):
        gt_rel = np.linalg.inv(gt_cameras_c2w[i]) @ gt_cameras_c2w[i + 1]
        pred_rel = np.linalg.inv(pred_cameras_c2w[i]) @ pred_cameras_c2w[i + 1]

        rotation_err = np.linalg.inv(gt_rel[:3, :3]) @ pred_rel[:3, :3]
        trace = np.trace(rotation_err)
        r_errors.append(np.degrees(np.arccos(np.clip((trace - 1) / 2, -1, 1))))

        t_errors.append(np.linalg.norm(gt_rel[:3, 3] - pred_rel[:3, 3]))

    return {
        "rotation_rmse": float(np.sqrt(np.mean(np.array(r_errors) ** 2))),
        "translation_rmse": float(np.sqrt(np.mean(np.array(t_errors) ** 2))),
    }
