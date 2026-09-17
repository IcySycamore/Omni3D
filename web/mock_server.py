"""Omni3D 重建服务 Mock 版本。

接口与 ``web/server.py`` 完全一致，但不加载真实模型，返回模拟点云数据，
用于前端开发、服务调试和网页样式优化。

用法：

.. code-block:: bash

    cd Omni3D
    python web/mock_server.py
"""

import json
import os
import sys
import threading
import time
import traceback

import numpy as np
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.core import config
from task_queue import task_queue

app = FastAPI(title="Omni3D 重建服务 (Mock)", version="0.2.0-mock")

SERVER_HOST = os.environ.get("HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("PORT", "50865"))

_model_ready = True
_model_error = None


def _mock_points(num_views: int = 3, num_points: int = 5000):
    """生成模拟点云数据。"""
    rng = np.random.default_rng(42)
    pts = []
    for _ in range(num_views):
        center = rng.standard_normal(3) * 0.5
        cloud = rng.normal(center, 0.15, (num_points, 3))
        pts.extend(cloud.tolist())
    return pts


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


def _mock_reconstruct(image_paths, resolution, intrinsics=None, extrinsics=None, is_video=False):
    """返回模拟重建结果。"""
    t0 = time.time()
    points = _mock_points(num_views=len(image_paths))
    ply = _pts_to_ply(points, np.tile([255, 180, 60], (len(points), 1)))
    elapsed = time.time() - t0
    return {
        "ok": True,
        "num_views": len(image_paths),
        "num_points": len(points),
        "elapsed_s": round(elapsed, 2),
        "points": points[:20000],
        "ply": ply,
        "intrinsics": intrinsics,
        "extrinsics": extrinsics,
        "is_video": is_video,
        "device": "mock",
    }


@app.on_event("startup")
def _startup():
    print("[mock_server] 模型已就绪（mock 模式）")


@app.get("/", response_class=HTMLResponse)
def index():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    with open(html_path, encoding="utf-8") as fh:
        return fh.read()


@app.get("/health")
def health():
    return {
        "ready": _model_ready,
        "device": "mock",
        "error": _model_error,
    }


def _save_upload(data: bytes, filename: str, tmp_dir: str) -> str:
    os.makedirs(tmp_dir, exist_ok=True)
    safe = os.path.basename(filename or "upload.bin")
    path = os.path.join(tmp_dir, f"{int(time.time() * 1000)}_{safe}")
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _task_processor(task, update_progress):
    """Mock 任务处理器。"""
    tmp_dir = os.path.join(PROJECT_ROOT, "temp_preview_frames", "tasks", task.task_id)
    os.makedirs(tmp_dir, exist_ok=True)

    update_progress(task, 0.05, "保存文件")
    image_paths = []
    for data, filename in task.files:
        image_paths.append(_save_upload(data, filename, tmp_dir))

    if not image_paths:
        raise RuntimeError("未收到有效图片/视频帧")

    update_progress(task, 0.30, f"mock 推理 {len(image_paths)} 帧")
    time.sleep(0.5)

    update_progress(task, 0.85, "生成点云")
    result = _mock_reconstruct(
        image_paths, task.resolution,
        intrinsics=task.intrinsics, extrinsics=task.extrinsics,
        is_video=task.is_video,
    )
    result["task_id"] = task.task_id
    task.result = result


task_queue.set_processor(_task_processor)


@app.post("/reconstruct")
def reconstruct(
    files: list[UploadFile] = File(...),
    resolution: int = Form(config.DEFAULT_RESOLUTION),
    intrinsics: str = Form("null"),
    extrinsics: str = Form("null"),
):
    try:
        intrinsics_data = json.loads(intrinsics) if intrinsics not in ("null", "") else None
        extrinsics_data = json.loads(extrinsics) if extrinsics not in ("null", "") else None
    except json.JSONDecodeError as exc:
        return JSONResponse({"error": f"内外参 JSON 解析失败: {exc}"}, status_code=400)

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
        result = _mock_reconstruct(
            image_paths, resolution,
            intrinsics=intrinsics_data, extrinsics=extrinsics_data,
            is_video=False,
        )
        return JSONResponse(result)
    except Exception as exc:
        traceback.print_exc()
        return JSONResponse({"error": f"重建失败: {exc}"}, status_code=500)


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
    is_video_bool = is_video.strip().lower() in ("1", "true", "yes", "on")
    try:
        intrinsics_data = json.loads(intrinsics) if intrinsics not in ("null", "") else None
        extrinsics_data = json.loads(extrinsics) if extrinsics not in ("null", "") else None
    except json.JSONDecodeError as exc:
        return JSONResponse({"error": f"内外参 JSON 解析失败: {exc}"}, status_code=400)

    if not files:
        return JSONResponse({"error": "未收到文件"}, status_code=400)

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
    task = task_queue.get(task_id)
    if task is None:
        return JSONResponse({"error": "任务不存在或已过期"}, status_code=404)
    return JSONResponse(_task_to_dict(task, include_result=include_result))


@app.get("/api/tasks")
def list_tasks(limit: int = 20):
    tasks = task_queue.list_recent(limit)
    return JSONResponse(
        {"tasks": [_task_to_dict(t, include_result=False) for t in tasks]}
    )


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str):
    ok = task_queue.remove(task_id)
    if not ok:
        return JSONResponse({"ok": False, "error": "任务不存在或正在运行"}, status_code=404)
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
