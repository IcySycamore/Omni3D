"""服务器端「最近邻吸附」索引（#25）。

**为什么需要它**：客户端拿到的 `points` 是**渲染子集**（默认 6 万 / 全量约 200 万），
等于每约 24 个点只留 1 个，点距被放大约 5 倍 —— 于是「选点」只能命中被抽到的点，
量出的距离天然带上采样误差。测量必须作用在**全量点云**上，而全量点云本来就在磁盘上
（`data/sessions/{id}.ply`，SOR 之后的那一份，与 PLY 下载完全一致）。

设计取舍：
- 自己解析 PLY（我们写的二进制小端），兼容 float32 / float64 坐标 —— 不引第三方依赖；
- 按会话 **LRU 缓存** `cKDTree`，避免每次点选都重建（百万点建树约 1s）；
- **批量**查询：框选 / 多选一次请求多个点，而不是每点一次往返；
- 超出 `max_distance` 返回 ``hit: False``，而不是硬给一个远处的点
  —— 宁可明说「这里没点到东西」。
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict

import numpy as np
from scipy.spatial import cKDTree

__all__ = ["read_ply_xyz", "SnapIndex", "snap_index", "PLY_TO_NUMPY"]

# PLY 标量类型 → numpy 字符码（都按小端读，与写入端一致）
PLY_TO_NUMPY = {
    "char": "i1", "int8": "i1",
    "uchar": "u1", "uint8": "u1",
    "short": "i2", "int16": "i2",
    "ushort": "u2", "uint16": "u2",
    "int": "i4", "int32": "i4",
    "uint": "u4", "uint32": "u4",
    "float": "f4", "float32": "f4",
    "double": "f8", "float64": "f8",
}

_HEADER_END = b"end_header\n"


def read_ply_xyz(path: str) -> np.ndarray:
    """读二进制小端 PLY，返回 ``(N, 3)`` 的 float64 坐标。

    只关心顶点坐标：颜色等其它属性会被解析出来但直接丢弃。
    兼容坐标是 `float`（旧格式）或 `double`（#24 之后）。
    """
    with open(path, "rb") as fh:
        data = fh.read()

    try:
        body_offset = data.index(_HEADER_END) + len(_HEADER_END)
    except ValueError as exc:
        raise ValueError("PLY 缺少 end_header") from exc

    header = data[:body_offset].decode("ascii", errors="replace")
    if "format binary_little_endian" not in header:
        raise ValueError("只支持 binary_little_endian PLY")

    count = None
    props: list[tuple[str, str]] = []
    for line in header.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "element" and parts[1] == "vertex":
            count = int(parts[2])
        elif len(parts) == 3 and parts[0] == "property" and parts[1] != "list":
            props.append((parts[1], parts[2]))
    if count is None:
        raise ValueError("PLY 缺少 element vertex")
    for name in ("x", "y", "z"):
        if name not in {p[1] for p in props}:
            raise ValueError(f"PLY 缺少顶点属性 {name}")

    dtype = np.dtype([(name, PLY_TO_NUMPY[t]) for t, name in props])
    rec = np.frombuffer(data, dtype=dtype, count=count, offset=body_offset)
    xyz = np.empty((rec.shape[0], 3), dtype=np.float64)
    xyz[:, 0] = rec["x"]
    xyz[:, 1] = rec["y"]
    xyz[:, 2] = rec["z"]
    finite = np.isfinite(xyz).all(axis=1)
    if not finite.all():
        xyz = xyz[finite]
    return xyz


def _signature(path: str):
    """文件签名（大小 + mtime）。

    光比路径不够：会话层是 upsert 语义，同一个 `session_id` 理论上可能被重写。
    签名变了就重建树，避免吸附到旧点云。
    """
    st = os.stat(path)
    return (st.st_size, st.st_mtime_ns)


class SnapIndex:
    """按会话缓存 `cKDTree` 的批量最近邻查询。

    Args:
        max_sessions: LRU 容量（默认 3）。每 100 万点约占 60~70MB
            （float64 坐标 + 树结构），所以不能无上限。
    """

    def __init__(self, max_sessions: int = 3):
        self._max = max(1, int(max_sessions))
        self._cache: OrderedDict = OrderedDict()   # key -> (tree, cloud)
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    # ---- 缓存 ----
    def _entry(self, key, ply_path: str):
        """取缓存条目，未命中或 PLY 已变（重建/覆盖）则建树。"""
        signature = _signature(ply_path)
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None and entry[2] == signature:
                self._cache.move_to_end(key)
                self._hits += 1
                return entry[0], entry[1]
        # 建树放到锁外：百万点约 1s，不该阻塞别的会话查询
        cloud = read_ply_xyz(ply_path)
        tree = cKDTree(cloud) if cloud.shape[0] else None
        with self._lock:
            self._cache[key] = (tree, cloud, signature)
            self._cache.move_to_end(key)
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)
            self._misses += 1
        return tree, cloud

    def stats(self) -> dict:
        with self._lock:
            return {
                "cached_sessions": len(self._cache),
                "capacity": self._max,
                "hits": self._hits,
                "misses": self._misses,
                "points": {k: int(v[1].shape[0]) for k, v in self._cache.items()},
            }

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def invalidate(self, key) -> bool:
        """会话被删除 / 重建后调用。"""
        with self._lock:
            return self._cache.pop(key, None) is not None

    # ---- 查询 ----
    def query(self, key, ply_path: str, points, max_distance=None) -> list[dict]:
        """批量吸附。

        Args:
            key: 缓存键（这里用 session_id）。
            ply_path: 该会话的 PLY 路径。
            points: ``(M, 3)`` 待吸附坐标。
            max_distance: 超过该距离判为未命中；``None`` 表示不限制。

        Returns:
            长度 M 的列表，每项 ``{"hit": bool, ...}``：
            命中给 ``point`` 与 ``distance``；未命中给 ``reason``
            （``out_of_range`` / ``empty``）与参考 ``distance``。
        """
        q = np.asarray(points, dtype=np.float64).reshape(-1, 3)
        if q.shape[0] == 0:
            return []
        tree, cloud = self._entry(key, ply_path)
        if tree is None or cloud.shape[0] == 0:
            return [{"hit": False, "reason": "empty"} for _ in range(q.shape[0])]

        dist, idx = tree.query(q, k=1, workers=-1)
        dist = np.atleast_1d(dist)
        idx = np.atleast_1d(idx)
        limit = None if max_distance is None else float(max_distance)

        out: list[dict] = []
        for d, i in zip(dist, idx):
            d = float(d)
            if not np.isfinite(d) or int(i) < 0:
                out.append({"hit": False, "reason": "empty"})
                continue
            if limit is not None and d > limit:
                out.append({"hit": False, "reason": "out_of_range",
                            "distance": d})
                continue
            out.append({"hit": True, "point": cloud[int(i)].tolist(),
                        "distance": d})
        return out


# 全进程共用一个（FastAPI 同步端点跑在线程池里，所以内部要加锁）
snap_index = SnapIndex(max_sessions=int(os.environ.get("OMNI3D_SNAP_CACHE", "3") or 3))
