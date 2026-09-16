"""Omni3D 轻量重建服务（FastAPI）。

接口：
- GET  /                 前端页面（index.html，同源托管）
- GET  /health           模型加载状态
- POST /reconstruct      上传图片包 → 3D 点云（同步，兼容旧客户端/web）
- POST /api/tasks        提交图片/视频包 → 入队，立即返回 task_id（异步）
- GET  /api/tasks/{id}   轮询任务状态（queued/running/done/failed + 进度 + 结果）
- GET  /api/tasks        任务列表（传 client_id 或令牌时按归属隔离）
- DELETE /api/tasks/{id} 删除任务记录
- POST /api/auth/*       认证：salt / register / challenge / login / logout / claim
- GET  /api/auth/me      校验令牌；GET /api/auth/claim/preview 预览可并入条数
- GET/DELETE /api/history*   会话层历史（SQLite，按 owner 隔离）
- POST /api/tasks/{id}/scale 由「两点 + 真实距离」反推尺度

设计：
- 模型在启动时后台线程加载（首次加载需数分钟），未就绪时 /health 返回 not ready
- 复用 app/core/pipeline.py（加载→推理→对齐）
- 响应包含：降采样点坐标（前端 three.js 渲染）+ 完整 PLY（下载）+ 相机位姿

鉴权：
- 登录令牌经 ``X-Auth-Token`` 头传递，**30 分钟滑动过期**
  （``OMNI3D_SESSION_TTL`` 可覆盖）；未登录时按 ``client_id`` 归入匿名桶。
- 客户端的「历史记录」应读 ``/api/history``（持久 + 按归属隔离）；
  ``/api/tasks`` 只是内存任务表，重启即清。
"""
import json
import os
import secrets
import sys
import threading
import time
import traceback
import uuid
from typing import Optional

# 确保项目根目录在 sys.path（web/ 的上一级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 必须先导入 torch（本机存在 DLL 加载顺序冲突：其他库先加载会导致 fbgemm.dll 失败）
import torch  # noqa: E402,F401

import numpy as np  # noqa: E402
from fastapi import Body, FastAPI, File, Form, Header, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse  # noqa: E402

from app.core import config  # noqa: E402
from app.core.pipeline import pointcloud_keys, run_reconstruction  # noqa: E402
from app.core.pointcloud import statistical_outlier_removal  # noqa: E402
from app.core.scale import ScaleError, infer_scale_from_measurement  # noqa: E402

from task_queue import task_queue  # noqa: E402
from auth_store import (  # noqa: E402
    AuthStore,
    compute_proof,
    compute_verifier,
    new_nonce,
    new_salt,
    validate_username,
)
from session_manager import SessionManager  # noqa: E402
from session_store import SessionStore, anon_owner, user_owner  # noqa: E402

app = FastAPI(title="Omni3D 重建服务", version="0.4.0")

# ---- 数据目录 ----
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

# ---- 认证 / 会话 / 历史（三者共存于 data/）----
auth_store = AuthStore(os.path.join(DATA_DIR, "omni3d_users.db"))
# 会话有效期：默认 30 分钟滑动过期，可用 OMNI3D_SESSION_TTL（秒）覆盖
session_manager = SessionManager()
session_store = SessionStore(
    db_path=os.path.join(DATA_DIR, "omni3d_sessions.db"),
    sessions_dir=os.path.join(DATA_DIR, "sessions"),
)


def _owner_of(token, client_id=None):
    """把登录令牌（优先）或匿名 client_id 解析为会话归属键。

    登录用户 → ``user:<username>``；否则 → ``anon:<client_id>``。
    历史记录因此能**对应到 username**。
    """
    username = session_manager.username_for(token)
    if username:
        return user_owner(username)
    return anon_owner(client_id or "default")

# ---- 监听配置（单一来源；与 frp 映射 127.0.0.1:50865 -> frp-oil.com:50865 对齐）----
SERVER_HOST = os.environ.get("HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("PORT", "50865"))

# ---- 全局模型状态（后台线程加载） ----
_model = None
_model_ready = False
_model_error = None


def _load_model():
    """后台线程加载 Fast3R 模型。"""
    global _model, _model_ready, _model_error
    try:
        from fast3r.models.fast3r import Fast3R

        _model = Fast3R.from_pretrained(config.CHECKPOINT_DIR).to(config.DEVICE)
        _model.eval()
        _model_ready = True
        print(f"[server] 模型加载完成，设备: {config.DEVICE}")
    except Exception as exc:  # noqa: BLE001
        _model_error = str(exc)
        traceback.print_exc()


@app.on_event("startup")
def _startup():
    threading.Thread(target=_load_model, daemon=True).start()


# ---- 页面 ----
@app.get("/", response_class=HTMLResponse)
def index():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    with open(html_path, encoding="utf-8") as fh:
        return fh.read()


@app.get("/health")
def health():
    return {
        "ready": _model_ready,
        "device": str(config.DEVICE),
        "error": _model_error,
    }


# ---- 重建 ----
# PLY 走**二进制小端**：同样 100 万点，ASCII 约 80MB，二进制约 15MB。
_PLY_STRUCT = np.dtype([
    ("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
    ("red", "u1"), ("green", "u1"), ("blue", "u1"),
])
_FALLBACK_RGB = (255, 180, 60)


def _pts_to_ply(points, colors=None) -> bytes:
    """点坐标 (N,3) + 颜色 (N,3,0~255) → **二进制小端** PLY 字节串。

    没有颜色时退化为单色（不再是唯一选择：正常路径会用上真实 RGB）。
    全向量化，百万点也只是几次 numpy 拷贝。
    """
    pts = np.asarray(points, dtype=np.float32).reshape(-1, 3)
    if pts.shape[0] == 0:
        return b""
    if colors is None:
        rgb = np.tile(np.asarray(_FALLBACK_RGB, dtype=np.uint8), (pts.shape[0], 1))
    else:
        rgb = np.asarray(colors, dtype=np.uint8).reshape(-1, 3)
        if rgb.shape[0] != pts.shape[0]:
            rgb = np.tile(np.asarray(_FALLBACK_RGB, dtype=np.uint8), (pts.shape[0], 1))

    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        "comment generated by Omni3D (Fast3R)\n"
        f"element vertex {pts.shape[0]}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\n"
        "end_header\n"
    )
    rec = np.empty(pts.shape[0], dtype=_PLY_STRUCT)
    rec["x"], rec["y"], rec["z"] = pts[:, 0], pts[:, 1], pts[:, 2]
    rec["red"], rec["green"], rec["blue"] = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    return header.encode("ascii") + rec.tobytes()


def _view_colors(views, index: int, height: int, width: int):
    """从第 index 个视图取真实 RGB，返回 (H*W, 3) uint8；取不到返回 None。

    `views[i]["img"]` 是 ``ImgNorm`` 之后的张量：``ImageNet 无关``的
    ``(x/255 - 0.5)/0.5``，即值域 [-1, 1]；反归一化即可还原真实颜色。
    """
    if not views or index >= len(views):
        return None
    view = views[index]
    img = view.get("img") if isinstance(view, dict) else None
    if img is None:
        return None
    if hasattr(img, "detach"):
        img = img.detach().cpu().numpy()
    img = np.asarray(img, dtype=np.float32)
    if img.ndim == 4:          # (B, 3, H, W)
        img = img[0]
    if img.ndim != 3:
        return None
    if img.shape[0] == 3:      # (3, H, W) → (H, W, 3)
        img = np.transpose(img, (1, 2, 0))
    if img.shape[0] != height or img.shape[1] != width:
        # 尺寸与点图不一致时宁可不上色，也不能让颜色错位
        return None
    rgb = np.clip((img + 1.0) * 0.5, 0.0, 1.0) * 255.0
    return rgb.astype(np.uint8).reshape(-1, 3)


def _collect_points(output_dict, conf_percentile=None):
    """收集各视图点云（**全分辨率 + 真实 RGB + 置信度过滤**）。

    只负责「收集」。流程顺序固定为 **收集 → 剔除离群点 → 抽样渲染**：
    `_reject_outliers` 与 `_sample_for_render` 分开调用，保证显示、PLY 导出与
    测量三者看到的是**同一份**点云（看到的 = 量到的 = 导出的）。

    Returns:
        ``(points, colors)`` —— 置信度过滤后的**全量**点 ``(N, 3)`` float32 与
        其 RGB ``(N, 3)`` uint8；任一视图取不到颜色时整体为 ``None``。
    """
    if conf_percentile is None:
        conf_percentile = config.VIS_CONF_PERCENTILE

    preds = output_dict["preds"]
    views = output_dict.get("views") or []
    pts_key, conf_key = pointcloud_keys()

    pts_parts: list = []
    rgb_parts: list = []
    for index, pred in enumerate(preds):
        pts = pred[pts_key]
        if hasattr(pts, "detach"):
            pts = pts.detach().cpu().numpy()
        pts = np.asarray(pts, dtype=np.float32)
        if pts.ndim == 4:                       # (B, H, W, 3)
            pts = pts[0]
        if pts.ndim != 3:
            continue
        height, width = pts.shape[0], pts.shape[1]
        pts = pts.reshape(-1, 3)

        keep = np.isfinite(pts).all(axis=1)

        conf = pred.get(conf_key)
        if conf is not None and conf_percentile > 0:
            if hasattr(conf, "detach"):
                conf = conf.detach().cpu().numpy()
            conf = np.asarray(conf, dtype=np.float32)
            if conf.ndim == 3:                  # (B, H, W)
                conf = conf[0]
            conf = conf.reshape(-1)
            if conf.shape[0] == keep.shape[0] and keep.any():
                keep &= conf >= np.percentile(conf[keep], conf_percentile)

        colors = _view_colors(views, index, height, width)
        if colors is not None and colors.shape[0] != keep.shape[0]:
            colors = None

        pts_parts.append(pts[keep])
        rgb_parts.append(colors[keep] if colors is not None else None)

    if not pts_parts:
        return np.zeros((0, 3), dtype=np.float32), None

    points = np.concatenate(pts_parts, axis=0)
    has_colors = all(part is not None for part in rgb_parts)
    colors = np.concatenate(rgb_parts, axis=0) if has_colors else None
    return points, colors


def _reject_outliers(points, colors):
    """统计离群点剔除（SOR，实现见 `app.core.pointcloud`）。

    `config.SOR_K` / `config.SOR_STD` 任一 ``<= 0`` 即**关闭**。
    为什么不能只靠置信度过滤：置信度删的是「模型没把握的点」，而背景飞点往往
    置信度并不低，只能靠**几何孤立性**识别，两者互补。

    Returns:
        ``(points, colors, removed)`` —— ``removed`` 为被剔除的点数（关闭时为 0）。
    """
    pts = np.asarray(points, dtype=np.float32).reshape(-1, 3)
    rgb = None if colors is None else np.asarray(colors, dtype=np.uint8).reshape(-1, 3)
    if config.SOR_K <= 0 or config.SOR_STD <= 0 or pts.shape[0] < 2:
        return pts, rgb, 0
    keep = statistical_outlier_removal(pts, k=config.SOR_K, std=config.SOR_STD)
    removed = int(pts.shape[0] - int(keep.sum()))
    if removed == 0:
        return pts, rgb, 0
    return pts[keep], (None if rgb is None else rgb[keep]), removed


def _sample_for_render(points, colors, max_render_points=None):
    """把全量点云**确定性均匀抽样**到 ``max_render_points``（仅用于客户端渲染）。

    抽样必须在离群点剔除**之后**做，否则渲染子集里会残留已被剔除的飞点，
    出现「屏幕上还在、PLY 里没有」的不一致。

    Returns:
        ``(render_points, render_colors)``（已 ``.tolist()``，供直接进 JSON）。
    """
    if max_render_points is None:
        max_render_points = config.MAX_RENDER_POINTS
    pts = np.asarray(points, dtype=np.float32).reshape(-1, 3)
    rgb = None if colors is None else np.asarray(colors, dtype=np.uint8).reshape(-1, 3)
    if max_render_points and pts.shape[0] > max_render_points:
        idx = np.linspace(0, pts.shape[0] - 1, max_render_points).astype(np.int64)
        pts = pts[idx]
        rgb = None if rgb is None else rgb[idx]
    return pts.tolist(), (None if rgb is None else rgb.tolist())


def _reconstruct_to_result(image_paths, resolution, intrinsics=None, extrinsics=None,
                           is_video=False):
    """核心编排（同步 /reconstruct 与异步 _task_processor 共用）：
    推理 → 收集点云（全量 + RGB）→ 二进制 PLY → 统一结果结构。

    Returns:
        ``(result, ply_bytes)`` —— PLY **不进 JSON**（百万点会让响应体膨胀到
        几十 MB，手机端解析会直接卡死）；调用方把它落到会话目录，
        客户端统一用 ``GET /api/history/{id}/ply`` 下载。
    """
    if not _model_ready:
        raise RuntimeError("模型仍在加载中，请稍后重试")
    t0 = time.time()
    output_dict, _profiling = run_reconstruction(
        image_paths,
        _model,
        config.DEVICE,
        resolution=resolution,
        dtype=config.INFERENCE_DTYPE,
        extrinsics=extrinsics,
        intrinsics=intrinsics,
    )
    metric = output_dict.get("metric") or {}
    points, colors = _collect_points(output_dict)
    num_points_raw = int(points.shape[0])
    # 剔除离群点后再落盘 / 抽样：看到的 = 量到的 = 导出的
    points, colors, num_removed = _reject_outliers(points, colors)
    ply_bytes = _pts_to_ply(points, colors)
    render_points, render_colors = _sample_for_render(points, colors)
    # 渲染用点云带上颜色：与网页/桌面端约定的 [x,y,z,r,g,b] 形式一致
    if render_colors is not None and render_points:
        render_payload = np.concatenate(
            [np.asarray(render_points, dtype=np.float32),
             np.asarray(render_colors, dtype=np.float32)], axis=1
        ).tolist()
    else:
        render_payload = render_points
    elapsed = time.time() - t0
    result = {
        "ok": True,
        "num_views": len(image_paths),
        "num_points": len(points),
        "num_points_raw": num_points_raw,
        "num_points_removed": num_removed,
        "render_points": len(render_points),
        "elapsed_s": round(elapsed, 2),
        "points": render_payload,
        "has_colors": render_colors is not None,
        "has_ply": bool(ply_bytes),
        "ply_bytes": len(ply_bytes),
        "intrinsics": intrinsics,
        "extrinsics": extrinsics,
        "is_video": is_video,
        "device": str(config.DEVICE),
        # 真实尺度：有可用 AR 位姿时为米制；否则 None（客户端走标尺校准）
        "scale": metric.get("scale"),
        "metric": metric,
        "quality": output_dict.get("quality"),
    }
    return result, ply_bytes


def _save_upload(data: bytes, filename: str, tmp_dir: str) -> str:
    """保存上传文件到临时目录，返回路径。"""
    os.makedirs(tmp_dir, exist_ok=True)
    safe = os.path.basename(filename or "upload.bin")
    path = os.path.join(tmp_dir, f"{int(time.time() * 1000)}_{safe}")
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _extract_video_frames(video_path: str, frame_count: int, out_dir: str):
    """用 cv2 从视频均匀抽帧，返回帧图片路径列表（保持时间序）。"""
    import cv2  # noqa: E402

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video_path}")

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total <= 0:
        cap.release()
        raise RuntimeError("视频无有效帧")

    n = min(frame_count, total)
    # 均匀采样帧索引（首尾各保留一点余量）
    idxs = sorted(
        {int(i * (total - 1) / (n - 1)) for i in range(n)} if n > 1 else {0}
    )

    os.makedirs(out_dir, exist_ok=True)
    paths = []
    cur = 0
    for idx in idxs:
        while cur < idx:
            ok = cap.grab()
            cur += 1
            if not ok:
                break
        ok, frame = cap.retrieve()
        if not ok:
            continue
        p = os.path.join(out_dir, f"frame_{cur:05d}.jpg")
        cv2.imwrite(p, frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
        paths.append(p)
        cur += 1
    cap.release()

    if not paths:
        raise RuntimeError("视频抽帧失败：无有效帧")
    return paths


def _resample_to(seq, n):
    """把逐帧序列均匀重采样为 n 项（多则取样、少则重复末项）。空序列原样返回。"""
    if not seq or n <= 0:
        return seq
    if len(seq) == n:
        return seq
    if len(seq) == 1:
        return [seq[0]] * n
    step = len(seq) / n
    return [seq[min(len(seq) - 1, int(i * step))] for i in range(n)]


def _task_processor_impl(task, update_progress):
    """任务队列处理器：图片直传或视频抽帧 → 重建 → 存结果。"""
    global _model

    if not _model_ready:
        raise RuntimeError("模型仍在加载中，请稍后重试")

    tmp_dir = os.path.join(PROJECT_ROOT, "temp_preview_frames", "tasks", task.task_id)

    if task.is_video:
        update_progress(task, 0.05, "保存视频")
        # 视频：只应有一个文件
        data, filename = task.files[0]
        video_path = _save_upload(data, filename, tmp_dir)
        update_progress(task, 0.10, "抽帧中")
        frames = _extract_video_frames(video_path, task.frame_count, tmp_dir)
        image_paths = frames
        # 视频抽帧后，逐帧外参按帧数重采样（传感器姿态在录制时已采样到 extrinsics）
        intrinsics_data = task.intrinsics
        extrinsics_data = _resample_to(task.extrinsics, len(frames))
    else:
        update_progress(task, 0.05, "保存图片")
        image_paths = []
        for data, filename in task.files:
            image_paths.append(_save_upload(data, filename, tmp_dir))
        intrinsics_data = task.intrinsics
        extrinsics_data = task.extrinsics

    if not image_paths:
        raise RuntimeError("未收到有效图片/视频帧")

    update_progress(task, 0.20, f"推理 {len(image_paths)} 帧")
    # 统一编排（与 /reconstruct 共用）；PLY 字节流单独带出，不进 JSON
    result, ply_bytes = _reconstruct_to_result(
        image_paths, task.resolution,
        intrinsics=intrinsics_data, extrinsics=extrinsics_data,
        is_video=task.is_video,
    )
    update_progress(task, 0.85, "生成点云")
    result["task_id"] = task.task_id
    task.result = result
    task.ply_bytes = ply_bytes


# 注册任务处理器（包装：执行重建 + 落入会话层，成功/失败均记录）
def _task_processor(task, update_progress):
    owner = getattr(task, "owner", "anon:default") or "anon:default"
    try:
        _task_processor_impl(task, update_progress)
    except Exception as exc:  # noqa: BLE001
        session_store.save_session(
            session_id=task.task_id,
            owner=owner,
            status="failed",
            result={"error": str(exc), "is_video": task.is_video},
            created_at=task.created_at,
        )
        raise
    session_store.save_session(
        session_id=task.task_id,
        owner=owner,
        status="done",
        result=task.result or {},
        ply_bytes=getattr(task, "ply_bytes", None),
        created_at=task.created_at,
    )


task_queue.set_processor(_task_processor)


@app.post("/reconstruct")
def reconstruct(
    files: list[UploadFile] = File(...),
    resolution: int = Form(config.DEFAULT_RESOLUTION),
    intrinsics: str = Form("null"),
    extrinsics: str = Form("null"),
    client_id: str = Form("default"),
    x_auth_token: Optional[str] = Header(default=None),
):
    """上传图片包（+ 可选内外参 JSON）→ 重建点云。

    - files: 图片文件（jpg/png）
    - resolution: 512 或 224
    - intrinsics: 每视图 3x3 内参矩阵 JSON（可选，示例 [[[...],[...],[...]], ...]）
    - extrinsics: 每视图 4x4 相机位姿 JSON（可选）
    """
    if not _model_ready:
        return JSONResponse(
            {"error": "模型仍在加载中，请稍后重试", "ready": False}, status_code=503
        )

    # 解析内外参（可选）
    try:
        intrinsics_data = json.loads(intrinsics) if intrinsics not in ("null", "") else None
        extrinsics_data = json.loads(extrinsics) if extrinsics not in ("null", "") else None
    except json.JSONDecodeError as exc:
        return JSONResponse({"error": f"内外参 JSON 解析失败: {exc}"}, status_code=400)

    # 保存上传图片到临时目录
    tmp_dir = os.path.join(PROJECT_ROOT, "temp_preview_frames", "web_upload")
    os.makedirs(tmp_dir, exist_ok=True)
    image_paths = []
    for f in files:
        data = f.file.read()
        path = os.path.join(tmp_dir, f"{int(time.time() * 1000)}_{f.filename or 'img'}")
        with open(path, "wb") as fh:
            fh.write(data)
        image_paths.append(path)

    if not image_paths:
        return JSONResponse({"error": "未收到图片"}, status_code=400)

    try:
        # 统一编排（与异步任务处理器共用）
        result, ply_bytes = _reconstruct_to_result(
            image_paths, resolution,
            intrinsics=intrinsics_data, extrinsics=extrinsics_data,
            is_video=False,
        )
        # 落入会话层（同步接口也生成历史）
        session_id = uuid.uuid4().hex[:16]
        result["task_id"] = session_id
        session_store.save_session(
            session_id=session_id,
            owner=_owner_of(x_auth_token, client_id),
            status="done",
            result=result,
            ply_bytes=ply_bytes,
        )
        return JSONResponse(result)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return JSONResponse({"error": f"重建失败: {exc}"}, status_code=500)


# ---- 异步任务 API ----
def _task_to_dict(task, include_result: bool) -> dict:
    d = {
        "task_id": task.task_id,
        "status": task.status,
        "progress": round(task.progress, 3),
        "stage": task.stage,
        "error": task.error,
        "is_video": task.is_video,
        "num_views": task.result.get("num_views") if task.result else None,
        "created_at": task.created_at,
        "finished_at": task.finished_at,
    }
    if include_result and task.result:
        d["result"] = task.result
    return d


@app.post("/api/tasks")
async def create_task(
    files: list[UploadFile] = File(...),
    resolution: int = Form(config.DEFAULT_RESOLUTION),
    intrinsics: str = Form("null"),
    extrinsics: str = Form("null"),
    is_video: str = Form("false"),
    frame_count: int = Form(12),
    client_id: str = Form("default"),
    x_auth_token: Optional[str] = Header(default=None),
):
    """提交采集包（图片或视频）→ 入队，立即返回 task_id。"""
    is_video_bool = is_video.strip().lower() in ("1", "true", "yes", "on")
    try:
        intrinsics_data = json.loads(intrinsics) if intrinsics not in ("null", "") else None
        extrinsics_data = json.loads(extrinsics) if extrinsics not in ("null", "") else None
    except json.JSONDecodeError as exc:
        return JSONResponse({"error": f"内外参 JSON 解析失败: {exc}"}, status_code=400)

    if not files:
        return JSONResponse({"error": "未收到文件"}, status_code=400)

    # 读取文件内容（task_queue 线程内再落盘，避免阻塞 IO 线程）
    file_list = []
    for f in files:
        data = await f.read()
        file_list.append((data, f.filename or "upload"))

    task = task_queue.submit(
        files=file_list,
        resolution=resolution,
        intrinsics=intrinsics_data,
        extrinsics=extrinsics_data,
        is_video=is_video_bool,
        frame_count=frame_count,
        owner=_owner_of(x_auth_token, client_id),
    )
    return JSONResponse(
        {
            "ok": True,
            "task_id": task.task_id,
            "status": task.status,
            "queue_pos": task.queue_pos,
            "message": "任务已入队",
        },
        status_code=202,
    )


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str, include_result: bool = True):
    """轮询任务状态。done 时附带完整重建结果。"""
    task = task_queue.get(task_id)
    if task is None:
        return JSONResponse({"error": "任务不存在或已过期"}, status_code=404)
    return JSONResponse(_task_to_dict(task, include_result=include_result))


@app.get("/api/tasks")
def list_tasks(limit: int = 20, client_id: Optional[str] = None,
               x_auth_token: Optional[str] = Header(default=None)):
    """任务列表（最新 N 条，不含大数据结果）。

    不传 ``client_id`` 且无令牌时列出全部（兼容调试脚本）；
    传了则**按归属隔离**：登录用户看自己的，匿名看 ``anon:<client_id>`` 的。
    """
    owner = None
    if client_id is not None or x_auth_token:
        owner = _owner_of(x_auth_token, client_id)
    tasks = task_queue.list_recent(limit, owner=owner)
    return JSONResponse(
        {"tasks": [_task_to_dict(t, include_result=False) for t in tasks]}
    )


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str):
    """删除一条任务记录（排队中/已完成可删，运行中不可删）。"""
    ok = task_queue.remove(task_id)
    if not ok:
        return JSONResponse({"ok": False, "error": "任务不存在或正在运行"}, status_code=404)
    return JSONResponse({"ok": True})


# ---- 认证：挑战-应答（明文密码不上网、不落库）----
# nonce 临时表：nonce -> (username, created_at)，用完即弃
_nonces: dict = {}
_nonce_lock = threading.Lock()
NONCE_TTL = 300.0  # 秒


def _put_nonce(nonce: str, username: str) -> None:
    with _nonce_lock:
        now = time.time()
        for k in [k for k, (_, c) in _nonces.items() if now - c > NONCE_TTL]:
            _nonces.pop(k, None)
        _nonces[nonce] = (username, now)


def _take_nonce(nonce: str):
    """取出并作废 nonce（一次性）；返回其绑定的 username 或 None。"""
    with _nonce_lock:
        item = _nonces.pop(nonce, None)
    if item is None:
        return None
    username, created = item
    if time.time() - created > NONCE_TTL:
        return None
    return username


@app.post("/api/auth/salt")
def auth_salt(payload: dict = Body(...)):
    """注册前领取随机 salt。Body: {"username"}（用户名已存在则 409）。

    同时前置校验用户名格式，让用户在注册第一步就得到明确提示。
    """
    username = (payload.get("username") or "").strip()
    if not username:
        return JSONResponse({"error": "缺少 username"}, status_code=400)
    invalid = validate_username(username)
    if invalid:
        return JSONResponse({"error": invalid}, status_code=400)
    if auth_store.user_exists(username):
        return JSONResponse({"error": "用户名已存在"}, status_code=409)
    return JSONResponse({"salt": new_salt()})


@app.post("/api/auth/register")
def auth_register(payload: dict = Body(...)):
    """注册。Body: {"username","salt","verifier"}

    verifier = sha256(salt + password) 由**客户端**计算，服务器只存 verifier，
    明文密码不上网也不落库。
    """
    username = (payload.get("username") or "").strip()
    salt = payload.get("salt") or ""
    verifier = payload.get("verifier") or ""
    if not username or not salt or not verifier:
        return JSONResponse({"error": "缺少 username / salt / verifier"}, status_code=400)
    # 服务端独有的强制检查：客户端可被绕过，用户名规则必须在此兜底。
    # （密码长度无法在此校验：本协议只上行 verifier，服务器看不到明文密码。）
    invalid = validate_username(username)
    if invalid:
        return JSONResponse({"error": invalid}, status_code=400)
    if not auth_store.create_user(username, salt, verifier):
        return JSONResponse({"error": "用户名已存在"}, status_code=409)
    return JSONResponse({"ok": True, "username": username})


@app.post("/api/auth/challenge")
def auth_challenge(payload: dict = Body(...)):
    """登录第一步：取 nonce + salt。Body: {"username"}。"""
    username = (payload.get("username") or "").strip()
    salt = auth_store.get_salt(username)
    if not salt:
        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
    nonce = new_nonce()
    _put_nonce(nonce, username)
    return JSONResponse({"nonce": nonce, "salt": salt})


@app.post("/api/auth/login")
def auth_login(payload: dict = Body(...)):
    """登录第二步：Body: {"username","nonce","proof"}。

    proof = sha256(nonce + sha256(salt + password_input))
    服务器比对 sha256(nonce + verifier)。
    """
    username = (payload.get("username") or "").strip()
    nonce = payload.get("nonce") or ""
    proof = payload.get("proof") or ""
    if not username or not nonce or not proof:
        return JSONResponse({"error": "缺少 username / nonce / proof"}, status_code=400)

    bound_user = _take_nonce(nonce)
    if bound_user is None or bound_user != username:
        return JSONResponse({"error": "nonce 无效或已过期"}, status_code=401)

    verifier = auth_store.get_verifier(username)
    if not verifier:
        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
    if not secrets.compare_digest(compute_proof(nonce, verifier), proof):
        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)

    token = session_manager.create(username)
    return JSONResponse({
        "ok": True,
        "token": token,
        "username": username,
        "expires_in": session_manager.ttl_seconds,
    })


@app.post("/api/auth/logout")
def auth_logout(x_auth_token: Optional[str] = Header(default=None)):
    """注销当前令牌。"""
    return JSONResponse({"ok": session_manager.drop(x_auth_token)})


@app.get("/api/auth/me")
def auth_me(x_auth_token: Optional[str] = Header(default=None)):
    """当前令牌对应的用户名（客户端启动时用它复验 token 是否仍有效）。"""
    username = session_manager.username_for(x_auth_token)
    if not username:
        return JSONResponse({"error": "未登录"}, status_code=401)
    return JSONResponse({"ok": True, "username": username,
                         "expires_in": session_manager.ttl_seconds})


@app.get("/api/auth/claim/preview")
def auth_claim_preview(client_id: str = "default",
                       x_auth_token: Optional[str] = Header(default=None)):
    """预览「匿名记录并入当前账号」将迁移多少条。

    登录后由客户端调用：若 count>0 就提示用户是否并入
    （网页/桌面端均采用**手动确认**，避免多人共用设备时误并）。
    """
    username = session_manager.username_for(x_auth_token)
    if not username:
        return JSONResponse({"error": "未登录"}, status_code=401)
    count = session_store.count_sessions(anon_owner(client_id))
    return JSONResponse({"ok": True, "count": count, "username": username,
                         "client_id": client_id})


@app.post("/api/auth/claim")
def auth_claim(client_id: str = "default",
               x_auth_token: Optional[str] = Header(default=None)):
    """把匿名历史并入当前账号（登录后调用，使历史对应到 username）。"""
    username = session_manager.username_for(x_auth_token)
    if not username:
        return JSONResponse({"error": "未登录"}, status_code=401)
    anon = anon_owner(client_id)
    owner = user_owner(username)
    # 历史既落在 SQLite（持久）也在内存任务表（重启前可见），两边一起迁
    moved = session_store.rename_owner(anon, owner)
    task_queue.rename_owner(anon, owner)
    return JSONResponse({"ok": True, "moved": moved, "username": username})


# ---- 会话层：重建历史（登录用户对应到 username）----
@app.get("/api/history")
def list_history(client_id: str = "default", limit: int = 50,
                 x_auth_token: Optional[str] = Header(default=None)):
    """列出该归属的历史会话（登录用户 → 其 username；匿名 → client_id）。"""
    owner = _owner_of(x_auth_token, client_id)
    return JSONResponse({"owner": owner,
                         "sessions": session_store.list_sessions(owner, limit)})


@app.get("/api/history/{session_id}")
def get_history(session_id: str, client_id: str = "default",
                include_points: bool = False,
                x_auth_token: Optional[str] = Header(default=None)):
    """获取单条历史会话；归属不匹配则 404（历史隔离）。"""
    owner = _owner_of(x_auth_token, client_id)
    data = session_store.get_session(session_id, owner, include_points=include_points)
    if data is None:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    return JSONResponse(data)


@app.get("/api/history/{session_id}/ply")
def get_history_ply(session_id: str, client_id: str = "default",
                    x_auth_token: Optional[str] = Header(default=None)):
    """下载该历史会话的完整 PLY 点云文件。"""
    owner = _owner_of(x_auth_token, client_id)
    ply_path = session_store.get_ply_path(session_id, owner)
    if not ply_path:
        return JSONResponse({"error": "PLY 不存在"}, status_code=404)
    return FileResponse(ply_path, media_type="application/octet-stream",
                        filename=f"{session_id}.ply")


@app.delete("/api/history/{session_id}")
def delete_history(session_id: str, client_id: str = "default",
                   x_auth_token: Optional[str] = Header(default=None)):
    """删除该归属的某条历史会话。"""
    owner = _owner_of(x_auth_token, client_id)
    ok = session_store.delete_session(session_id, owner)
    if not ok:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    return JSONResponse({"ok": True})


# ---- 真实尺度反推：选两点 + 已知真实距离 → 尺度因子 ----


@app.post("/api/tasks/{task_id}/scale")
def infer_scale(task_id: str, payload: dict = Body(...),
                client_id: str = "default",
                x_auth_token: Optional[str] = Header(default=None)):
    """从「模型内两点 + 已知真实距离(米)」反推真实尺度因子并持久化。

    Body: {"point_a": [x,y,z], "point_b": [x,y,z], "real_distance": 1.23}
    返回 scale 后，客户端后续测量按 scale 换算为米制（web / 桌面端共用同一实现）。
    """
    point_a = payload.get("point_a")
    point_b = payload.get("point_b")
    real_distance = payload.get("real_distance")
    if point_a is None or point_b is None or real_distance is None:
        return JSONResponse(
            {"error": "缺少参数：需 point_a, point_b, real_distance"}, status_code=400
        )
    try:
        result = infer_scale_from_measurement(point_a, point_b, real_distance)
    except ScaleError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    # 持久化到会话层（校验归属）
    owner = _owner_of(x_auth_token, client_id)
    updated = session_store.update_scale(
        session_id=task_id, owner=owner,
        scale=result["scale"], real_distance=result["real_distance"],
    )
    # 同步更新内存中的任务结果，便于 /api/tasks/{id} 立即返回
    task = task_queue.get(task_id)
    if task is not None and task.result is not None:
        task.result["scale"] = result["scale"]

    return JSONResponse({
        "ok": True,
        "task_id": task_id,
        "persisted": updated,
        **result,
    })


if __name__ == "__main__":
    import uvicorn

    # 监听 SERVER_HOST:SERVER_PORT（模块级单一来源）
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)