"""Omni3D 轻量重建服务（FastAPI）。

接口：
- GET  /                 前端页面（index.html）
- GET  /health           模型加载状态
- POST /reconstruct      上传图片包 → 3D 点云（同步，兼容旧客户端/web）
- POST /api/tasks        提交图片/视频包 → 入队，立即返回 task_id（异步）
- GET  /api/tasks/{id}   轮询任务状态（queued/running/done/failed + 进度 + 结果）
- GET  /api/tasks        任务列表（最新 N 条）

设计：
- 模型在启动时后台线程加载（首次加载需数分钟），未就绪时 /health 返回 not ready
- 复用 app/core/pipeline.py（加载→推理→对齐）
- 响应包含：降采样点坐标（前端 three.js 渲染）+ 完整 PLY（下载）+ 相机位姿
"""
import json
import os
import sys
import threading
import time
import traceback

# 确保项目根目录在 sys.path（web/ 的上一级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 必须先导入 torch（本机存在 DLL 加载顺序冲突：其他库先加载会导致 fbgemm.dll 失败）
import torch  # noqa: E402,F401

import numpy as np  # noqa: E402
from fastapi import FastAPI, File, Form, UploadFile  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402

from app.core import config  # noqa: E402
from app.core.pipeline import run_reconstruction  # noqa: E402

from task_queue import task_queue  # noqa: E402

app = FastAPI(title="Omni3D 重建服务", version="0.2.0")

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
def _pts_to_ply(points, colors):
    """点坐标 (N,3) + 颜色 (N,3,0~255) → PLY 文本。"""
    if points is None or len(points) == 0:
        return ""
    header = (
        "ply\nformat ascii 1.0\n"
        f"element vertex {len(points)}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\n"
        "end_header\n"
    )
    body = []
    for pt, col in zip(points, colors):
        body.append(f"{pt[0]:.6f} {pt[1]:.6f} {pt[2]:.6f} {int(col[0])} {int(col[1])} {int(col[2])}")
    return header + "\n".join(body)


def _collect_points(output_dict, stride=8):
    """把各视图点云降采样收集为 (points, colors) 列表。"""
    preds = output_dict["preds"]
    view_clouds = []
    for pred in preds:
        pts = pred["pts3d_in_other_view"].cpu().numpy().reshape(-1, 3)
        cloud = pts[::stride]
        view_clouds.append(cloud.tolist())

    all_points = []
    for cloud in view_clouds:
        all_points.extend(cloud)
    return all_points


def _reconstruct_to_result(image_paths, resolution, intrinsics=None, extrinsics=None,
                           is_video=False):
    """核心编排（同步 /reconstruct 与异步 _task_processor 共用）：
    推理 → 降采样 → PLY → 统一结果结构。
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
    )
    points = _collect_points(output_dict)
    ply = _pts_to_ply(
        np.asarray(points), np.tile([255, 180, 60], (len(points), 1))
    )
    elapsed = time.time() - t0
    return {
        "ok": True,
        "num_views": len(image_paths),
        "num_points": len(points),
        "elapsed_s": round(elapsed, 2),
        "points": points[:20000],  # 前端渲染上限
        "ply": ply,
        "intrinsics": intrinsics,
        "extrinsics": extrinsics,
        "is_video": is_video,
        "device": str(config.DEVICE),
    }


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


def _task_processor(task, update_progress):
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
        # 视频抽帧后内外参按帧数插值采样（传感器姿态在录制时已采样到 extrinsics）
        intrinsics_data = task.intrinsics
        extrinsics_data = task.extrinsics
        if extrinsics_data and len(extrinsics_data) > len(frames):
            step = len(extrinsics_data) / len(frames)
            extrinsics_data = [
                extrinsics_data[min(len(extrinsics_data) - 1, int(i * step))]
                for i in range(len(frames))
            ]
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
    # 统一编排（与 /reconstruct 共用）
    result = _reconstruct_to_result(
        image_paths, task.resolution,
        intrinsics=intrinsics_data, extrinsics=extrinsics_data,
        is_video=task.is_video,
    )
    update_progress(task, 0.85, "生成点云")
    result["task_id"] = task.task_id
    task.result = result


# 注册任务处理器
task_queue.set_processor(_task_processor)


@app.post("/reconstruct")
def reconstruct(
    files: list[UploadFile] = File(...),
    resolution: int = Form(config.DEFAULT_RESOLUTION),
    intrinsics: str = Form("null"),
    extrinsics: str = Form("null"),
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
        result = _reconstruct_to_result(
            image_paths, resolution,
            intrinsics=intrinsics_data, extrinsics=extrinsics_data,
            is_video=False,
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
def list_tasks(limit: int = 20):
    """任务列表（最新 N 条，不含大数据结果）。"""
    tasks = task_queue.list_recent(limit)
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


if __name__ == "__main__":
    import uvicorn

    # 监听 SERVER_HOST:SERVER_PORT（模块级单一来源）
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
