"""Omni3D 会话层：重建历史的统一持久化（SQLite）。

设计目标：
- **唯一一份历史存储**：取代原先三套割裂的机制
  （服务器内存 task_queue / Qt App 的 JSON / web 无持久化）。
- **按 owner 隔离**：owner 由服务器解析得出，取自登录身份
  （`user:<username>`）或匿名客户端（`anon:<client_id>`）。
  登录用户的历史**对应到 username**，各客户端互不可见。
- **零外部依赖**：仅用标准库 sqlite3，单文件数据库。

数据模型：
    sessions(
        session_id   TEXT PRIMARY KEY,   -- 会话 ID（= 重建任务 task_id）
        owner        TEXT NOT NULL,      -- 归属：user:<username> / anon:<client_id>
        status       TEXT NOT NULL,      -- done / failed
        is_video     INTEGER,
        num_views    INTEGER,
        num_points   INTEGER,
        elapsed_s    REAL,
        scale        REAL,               -- 真实尺度因子，NULL=未标定
        real_distance REAL,              -- 反推尺度时用户输入的已知真实距离(米)
        created_at   REAL,
        finished_at  REAL,
        meta_json    TEXT,
        points_blob  BLOB,               -- zlib 压缩的降采样点云 JSON
        ply_path     TEXT
    )

线程安全：单连接 + 互斥锁（FastAPI 多线程 + 队列 worker 并发访问）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import zlib
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    owner         TEXT NOT NULL,
    status        TEXT NOT NULL,
    is_video      INTEGER DEFAULT 0,
    num_views     INTEGER,
    num_points    INTEGER,
    elapsed_s     REAL,
    scale         REAL,
    real_distance REAL,
    created_at    REAL,
    finished_at   REAL,
    meta_json     TEXT,
    points_blob   BLOB,
    ply_path      TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(owner, created_at DESC);
"""


def user_owner(username: str) -> str:
    """登录用户的 owner 键。"""
    return f"user:{username}"


def anon_owner(client_id: str) -> str:
    """匿名客户端的 owner 键。"""
    return f"anon:{client_id or 'default'}"


class SessionStore:
    """SQLite 会话存储，按 owner 隔离历史。"""

    def __init__(self, db_path: str, sessions_dir: str):
        self._db_path = db_path
        self._sessions_dir = sessions_dir
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        os.makedirs(sessions_dir, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._migrate_locked()
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def _migrate_locked(self) -> None:
        """旧库（Phase 1 的 client_id 列）→ owner 列。"""
        try:
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(sessions)")}
        except sqlite3.DatabaseError:
            return
        if "client_id" in cols and "owner" not in cols:
            try:
                self._conn.execute(
                    "ALTER TABLE sessions RENAME COLUMN client_id TO owner"
                )
                self._conn.commit()
            except sqlite3.DatabaseError:
                pass

    # ---- 内部工具 ----
    def _ply_path_for(self, session_id: str) -> str:
        return os.path.join(self._sessions_dir, f"{session_id}.ply")

    @staticmethod
    def _dumps_points(points) -> Optional[bytes]:
        if not points:
            return None
        raw = json.dumps(points, separators=(",", ":")).encode("utf-8")
        return zlib.compress(raw, level=6)

    @staticmethod
    def _loads_points(blob) -> list:
        if not blob:
            return []
        return json.loads(zlib.decompress(blob).decode("utf-8"))

    # ---- 写入 ----
    def save_session(self, *, session_id: str, owner: str, result: dict,
                     status: str = "done", created_at: Optional[float] = None,
                     ply_bytes: Optional[bytes] = None) -> None:
        """保存/更新一条重建历史（幂等 upsert）。

        PLY 以**二进制**写入会话目录（百万点 ASCII 约 80MB → 二进制约 15MB）；
        兼容旧调用：没有 ``ply_bytes`` 时仍接受 ``result["ply"]`` 文本。
        """
        points = result.get("points") or []
        if ply_bytes is None:
            legacy_ply = result.get("ply") or ""
            ply_bytes = legacy_ply.encode("utf-8") if legacy_ply else None
        ply_path = ""
        if ply_bytes:
            ply_path = self._ply_path_for(session_id)
            with open(ply_path, "wb") as fh:
                fh.write(ply_bytes)

        # points / colors 都是「渲染用」大数据，不进 meta_json
        meta = {k: v for k, v in result.items()
                if k not in ("points", "colors", "ply", "task_id")}
        now = time.time()

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions (
                    session_id, owner, status, is_video, num_views, num_points,
                    elapsed_s, scale, created_at, finished_at, meta_json, points_blob, ply_path
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET
                    status=excluded.status,
                    num_views=excluded.num_views,
                    num_points=excluded.num_points,
                    elapsed_s=excluded.elapsed_s,
                    scale=COALESCE(excluded.scale, sessions.scale),
                    finished_at=excluded.finished_at,
                    meta_json=excluded.meta_json,
                    points_blob=excluded.points_blob,
                    ply_path=excluded.ply_path
                """,
                (
                    session_id, owner, status,
                    1 if result.get("is_video") else 0,
                    result.get("num_views"),
                    result.get("num_points"),
                    result.get("elapsed_s"),
                    result.get("scale"),
                    created_at if created_at is not None else now,
                    now,
                    json.dumps(meta, ensure_ascii=False),
                    self._dumps_points(points),
                    ply_path,
                ),
            )
            self._conn.commit()

    def update_scale(self, *, session_id: str, owner: str, scale: float,
                     real_distance: Optional[float] = None) -> bool:
        """更新会话的真实尺度因子（尺度反推用）。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sessions SET scale=?, real_distance=? WHERE session_id=? AND owner=?",
                (scale, real_distance, session_id, owner),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ---- 读取 ----
    def list_sessions(self, owner: str, limit: int = 50) -> list[dict]:
        """列出该归属的历史（轻量，不含点云/PLY）。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT session_id, owner, status, is_video, num_views, num_points,
                       elapsed_s, scale, real_distance, created_at, finished_at, meta_json
                FROM sessions WHERE owner=?
                ORDER BY created_at DESC LIMIT ?
                """,
                (owner, limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count_sessions(self, owner: str) -> int:
        """该归属下的历史条数（用于「匿名记录并入账号」的预览）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE owner=?", (owner,)
            ).fetchone()
        return int(row["n"]) if row else 0

    def get_session(self, session_id: str, owner: str,
                    include_points: bool = False) -> Optional[dict]:
        """获取单条历史；归属不匹配则返回 None（隔离）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id=? AND owner=?",
                (session_id, owner),
            ).fetchone()
        if row is None:
            return None
        data = self._row_to_dict(row)
        if include_points:
            data["points"] = self._loads_points(row["points_blob"])
        data["has_ply"] = bool(row["ply_path"]) and os.path.exists(row["ply_path"] or "")
        return data

    def get_ply_path(self, session_id: str, owner: str) -> Optional[str]:
        """返回该会话 PLY 文件路径（校验归属）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT ply_path FROM sessions WHERE session_id=? AND owner=?",
                (session_id, owner),
            ).fetchone()
        if row and row["ply_path"] and os.path.exists(row["ply_path"]):
            return row["ply_path"]
        return None

    def rename_owner(self, old_owner: str, new_owner: str) -> int:
        """把某归属的所有历史改挂到另一归属（例如匿名历史并入账号）。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sessions SET owner=? WHERE owner=?", (new_owner, old_owner)
            )
            self._conn.commit()
            return cur.rowcount

    # ---- 删除 ----
    def delete_session(self, session_id: str, owner: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM sessions WHERE session_id=? AND owner=?",
                (session_id, owner),
            )
            self._conn.commit()
            hit = cur.rowcount > 0
        if hit:
            ply = self._ply_path_for(session_id)
            if os.path.exists(ply):
                try:
                    os.remove(ply)
                except OSError:
                    pass
        return hit

    # ---- 序列化 ----
    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        meta = {}
        if row["meta_json"]:
            try:
                meta = json.loads(row["meta_json"])
            except json.JSONDecodeError:
                meta = {}
        owner = row["owner"] or ""
        return {
            "session_id": row["session_id"],
            "owner": owner,
            # 便于前端直接展示：登录用户显示用户名
            "username": owner.split(":", 1)[1] if owner.startswith("user:") else None,
            "status": row["status"],
            "is_video": bool(row["is_video"]),
            "num_views": row["num_views"],
            "num_points": row["num_points"],
            "elapsed_s": row["elapsed_s"],
            "scale": row["scale"],
            "real_distance": row["real_distance"],
            "created_at": row["created_at"],
            "finished_at": row["finished_at"],
            "meta": meta,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
