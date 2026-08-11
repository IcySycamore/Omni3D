"""Omni3D 重建任务队列。

异步任务模型：
- POST /api/tasks  提交图片/视频包 → 返回 task_id，立即入队
- GET  /api/tasks/{id}  轮询任务状态（queued/running/done/failed + 进度 + 结果）
- GET  /api/tasks  任务列表（最新 N 条）

设计：
- 单 worker 后台线程顺序处理（GPU 推理串行，避免显存竞争）
- 任务结果缓存内存中，带过期清理（默认 30 分钟）
- 视频包：上传 mp4 → cv2 均匀抽帧（默认 12 帧）当图片处理
"""
from __future__ import annotations

import os
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

# ---- 任务状态常量 ----
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


@dataclass
class Task:
    task_id: str
    status: str = STATUS_QUEUED
    queue_pos: int = 0
    progress: float = 0.0          # 0~1
    stage: str = "等待处理"
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    # 提交参数
    files: list = field(default_factory=list)      # [(bytes, filename)]
    resolution: int = 224
    intrinsics: list | None = None
    extrinsics: list | None = None
    is_video: bool = False
    frame_count: int = 12
    # 结果
    result: dict | None = None


class TaskQueue:
    """单 worker 顺序任务队列，线程安全。"""

    def __init__(self, max_cached: int = 50, ttl_seconds: float = 1800.0):
        self._lock = threading.Lock()
        self._queue: list[Task] = []              # 等待/运行中
        self._cache: OrderedDict[str, Task] = OrderedDict()  # 已完成（含失败）
        self._max_cached = max_cached
        self._ttl = ttl_seconds
        self._worker: threading.Thread | None = None
        self._stop = False

    # ---- 提交 ----
    def submit(self, files, resolution, intrinsics, extrinsics,
               is_video=False, frame_count=12) -> Task:
        task = Task(
            task_id=uuid.uuid4().hex[:16],
            files=files,
            resolution=resolution,
            intrinsics=intrinsics,
            extrinsics=extrinsics,
            is_video=is_video,
            frame_count=frame_count,
        )
        with self._lock:
            task.queue_pos = len(self._queue)
            self._queue.append(task)
            self._start_worker_locked()
        return task

    # ---- 查询 ----
    def get(self, task_id: str) -> Task | None:
        with self._lock:
            for t in self._queue:
                if t.task_id == task_id:
                    return t
            return self._cache.get(task_id)

    def list_recent(self, limit: int = 20) -> list[Task]:
        with self._lock:
            # 运行中 + 最近的已完成
            items = list(self._queue)
            items.extend(reversed(list(self._cache.values())))
            return items[:limit]

    # ---- 删除 ----
    def remove(self, task_id: str) -> bool:
        """删除任务记录（排队中 / 已完成均可；运行中不允许删除）。"""
        with self._lock:
            for i, t in enumerate(self._queue):
                if t.task_id == task_id:
                    if t.status == STATUS_RUNNING:
                        return False  # 运行中不可删，避免破坏 worker
                    del self._queue[i]
                    return True
            if task_id in self._cache:
                del self._cache[task_id]
                return True
            return False

    # ---- worker 管理 ----
    def _start_worker_locked(self):
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._run_worker, daemon=True)
            self._worker.start()

    def _run_worker(self):
        while not self._stop:
            with self._lock:
                if not self._queue:
                    return  # 队列空，worker 退出（下次提交重新启动）
                task = self._queue[0]
                task.status = STATUS_RUNNING
                task.stage = "推理中"
            try:
                self._process(task)
                with self._lock:
                    task.status = STATUS_DONE
                    task.progress = 1.0
                    task.stage = "完成"
                    task.finished_at = time.time()
                    self._queue.pop(0)
                    self._cache[task.task_id] = task
                    self._trim_cache_locked()
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                with self._lock:
                    task.status = STATUS_FAILED
                    task.error = str(exc)
                    task.stage = "失败"
                    task.finished_at = time.time()
                    self._queue.pop(0)
                    self._cache[task.task_id] = task
                    self._trim_cache_locked()

    def _trim_cache_locked(self):
        now = time.time()
        while len(self._cache) > self._max_cached:
            self._cache.popitem(last=False)
        # 清理过期
        expired = [k for k, v in self._cache.items() if now - v.finished_at > self._ttl]
        for k in expired:
            self._cache.pop(k, None)

    # ---- 实际处理（由 server.py 注入） ----
    def _process(self, task: Task):
        # 由外部通过 set_processor 注入具体实现
        if self._processor:
            self._processor(task, self._update_progress)

    def set_processor(self, fn):
        self._processor = fn

    def _update_progress(self, task: Task, progress: float, stage: str):
        with self._lock:
            task.progress = max(0.0, min(1.0, progress))
            task.stage = stage


# 全局队列实例（server.py 注册处理器）
task_queue = TaskQueue()
