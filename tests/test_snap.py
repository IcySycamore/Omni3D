"""全量点云吸附（#25）测试。

覆盖：自写 PLY 的读取往返、批量最近邻、`max_distance` 超限、按会话 LRU 缓存
（含「PLY 被覆盖后必须重建树」）、以及端点的越权 404 / 入参校验。

端点用**直接调用函数**的方式测，不启 TestClient —— 后者会触发 FastAPI startup
事件去加载模型（几十秒且与本次无关）。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

# torch 必须最先导入（本机 fbgemm.dll 加载顺序冲突）
import torch  # noqa: F401,I001

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB = os.path.join(_ROOT, "web")
if _WEB not in sys.path:
    sys.path.insert(0, _WEB)

import server  # noqa: E402
from session_store import SessionStore, anon_owner  # noqa: E402
from snap_index import SnapIndex, read_ply_xyz  # noqa: E402


def _write_ply(path: str, points) -> str:
    """用**生产代码**写 PLY，保证读端面对的就是真实格式。"""
    with open(path, "wb") as fh:
        fh.write(server._pts_to_ply(np.asarray(points, dtype=np.float64), None))
    return path


def _grid(n: int = 4, spacing: float = 1.0) -> np.ndarray:
    axis = np.arange(n, dtype=np.float64) * spacing
    gx, gy, gz = np.meshgrid(axis, axis, axis, indexing="ij")
    return np.stack([gx.ravel(), gy.ravel(), gz.ravel()], axis=1)


class TestPlyReader:
    def test_roundtrip_is_exact(self, tmp_path):
        pts = _grid(3) + np.array([0.1, -7.5, 1234.25])
        path = _write_ply(str(tmp_path / "a.ply"), pts)
        got = read_ply_xyz(path)
        assert got.shape == pts.shape
        assert got.dtype == np.float64
        assert np.array_equal(got, pts)

    def test_reads_legacy_float32_ply(self, tmp_path):
        """旧 PLY（property float）也要能读 —— 历史会话还在磁盘上。"""
        pts = _grid(2)
        header = (
            "ply\nformat binary_little_endian 1.0\n"
            f"element vertex {len(pts)}\n"
            "property float x\nproperty float y\nproperty float z\n"
            "end_header\n"
        )
        rec = np.empty(len(pts), dtype=np.dtype(
            [("x", "<f4"), ("y", "<f4"), ("z", "<f4")]))
        rec["x"], rec["y"], rec["z"] = pts[:, 0], pts[:, 1], pts[:, 2]
        path = tmp_path / "old.ply"
        path.write_bytes(header.encode("ascii") + rec.tobytes())
        got = read_ply_xyz(str(path))
        assert np.allclose(got, pts)

    def test_rejects_missing_header_end(self, tmp_path):
        path = tmp_path / "bad.ply"
        path.write_bytes(b"ply\nformat binary_little_endian 1.0\n")
        with pytest.raises(ValueError, match="end_header"):
            read_ply_xyz(str(path))

    def test_rejects_missing_coordinate_property(self, tmp_path):
        path = tmp_path / "noz.ply"
        path.write_bytes(
            b"ply\nformat binary_little_endian 1.0\nelement vertex 1\n"
            b"property double x\nproperty double y\nend_header\n" + b"\x00" * 16
        )
        with pytest.raises(ValueError, match="z"):
            read_ply_xyz(str(path))

    def test_rejects_ascii_ply(self, tmp_path):
        path = tmp_path / "ascii.ply"
        path.write_bytes(
            b"ply\nformat ascii 1.0\nelement vertex 1\n"
            b"property double x\nproperty double y\nproperty double z\n"
            b"end_header\n0 0 0\n"
        )
        with pytest.raises(ValueError, match="binary_little_endian"):
            read_ply_xyz(str(path))


class TestSnapIndex:
    @pytest.fixture()
    def cloud(self):
        return _grid(4)

    @pytest.fixture()
    def ply(self, tmp_path, cloud):
        return _write_ply(str(tmp_path / "c.ply"), cloud)

    def test_hits_a_real_full_cloud_point(self, ply, cloud):
        """吸附结果必须是**全量点云里的真实点**（不是插值/不是抽样点）。"""
        idx = SnapIndex()
        target = cloud[7]
        probe = target + np.array([0.01, -0.02, 0.03])
        got = idx.query("s1", ply, [probe], max_distance=None)
        assert len(got) == 1
        assert got[0]["hit"] is True
        assert np.allclose(got[0]["point"], target, atol=0)
        assert got[0]["distance"] == pytest.approx(np.linalg.norm(probe - target))

    def test_batch_returns_one_result_per_query(self, ply, cloud):
        idx = SnapIndex()
        queries = [cloud[0], cloud[1] + 0.25, cloud[-1]]
        got = idx.query("s1", ply, queries)
        assert len(got) == 3
        assert all(r["hit"] for r in got)
        assert np.allclose(got[2]["point"], cloud[-1], atol=0)

    def test_out_of_range_reports_miss_not_a_far_point(self, ply):
        """超距必须 `hit: false`，而不是硬给一个远处的点。"""
        idx = SnapIndex()
        got = idx.query("s1", ply, [[100.0, 100.0, 100.0]], max_distance=0.5)
        assert got[0]["hit"] is False
        assert got[0]["reason"] == "out_of_range"
        assert got[0]["distance"] > 0.5

    def test_none_distance_always_hits(self, ply):
        idx = SnapIndex()
        got = idx.query("s1", ply, [[100.0, 100.0, 100.0]], max_distance=None)
        assert got[0]["hit"] is True

    def test_empty_query_list(self, ply):
        assert SnapIndex().query("s1", ply, []) == []

    def test_empty_cloud_reports_empty(self, tmp_path):
        """0 顶点是合法的 PLY（注意 `_pts_to_ply` 对 0 点返回空字节，是「无 PLY」
        的哨兵，所以这里手写一个合法的 0 顶点文件）。"""
        path = tmp_path / "empty.ply"
        path.write_bytes(
            b"ply\nformat binary_little_endian 1.0\nelement vertex 0\n"
            b"property double x\nproperty double y\nproperty double z\n"
            b"end_header\n"
        )
        got = SnapIndex().query("s1", str(path), [[0.0, 0.0, 0.0]])
        assert got[0] == {"hit": False, "reason": "empty"}

    def test_second_query_hits_cache(self, ply):
        """验收要求：第二次请求必须命中缓存（用计数证明，不靠计时）。"""
        idx = SnapIndex()
        idx.query("s1", ply, [[0.0, 0.0, 0.0]])
        idx.query("s1", ply, [[1.0, 1.0, 1.0]])
        st = idx.stats()
        assert st["misses"] == 1
        assert st["hits"] == 1
        assert st["cached_sessions"] == 1

    def test_lru_evicts_oldest(self, tmp_path):
        idx = SnapIndex(max_sessions=2)
        for name in ("a", "b", "c"):
            path = _write_ply(str(tmp_path / f"{name}.ply"), _grid(2))
            idx.query(name, path, [[0.0, 0.0, 0.0]])
        st = idx.stats()
        assert st["cached_sessions"] == 2
        assert "a" not in st["points"]

    def test_invalidate_forces_rebuild(self, ply):
        idx = SnapIndex()
        idx.query("s1", ply, [[0.0, 0.0, 0.0]])
        assert idx.invalidate("s1") is True
        idx.query("s1", ply, [[0.0, 0.0, 0.0]])
        assert idx.stats()["misses"] == 2

    def test_rewritten_ply_invalidates_cache(self, tmp_path):
        """会话层是 upsert，同一路径被覆盖后必须重建树，否则会吸附到旧点云。"""
        path = _write_ply(str(tmp_path / "r.ply"), _grid(2))
        idx = SnapIndex()
        first = idx.query("s1", path, [[0.0, 0.0, 0.0]])[0]
        assert np.allclose(first["point"], [0.0, 0.0, 0.0])
        # 覆盖成完全不同的点云（单个点挪到远处）
        _write_ply(path, np.array([[5.0, 5.0, 5.0]]))
        os.utime(path, ns=(0, 0))  # 保证 mtime 一定变化
        second = idx.query("s1", path, [[0.0, 0.0, 0.0]])[0]
        assert np.allclose(second["point"], [5.0, 5.0, 5.0])
        assert idx.stats()["misses"] == 2


@pytest.fixture()
def snap_env(tmp_path, monkeypatch):
    """真实 SessionStore（临时目录）+ 独立吸附索引，替换掉 server 的全局对象。"""
    store = SessionStore(
        db_path=str(tmp_path / "sessions.db"),
        sessions_dir=str(tmp_path / "ply"),
    )
    index = SnapIndex()
    monkeypatch.setattr(server, "session_store", store)
    monkeypatch.setattr(server, "snap_index", index)
    yield store, index
    store.close()


def _save(store: SessionStore, session_id: str, owner: str, points) -> None:
    store.save_session(
        session_id=session_id,
        owner=owner,
        result={"num_views": 2, "num_points": len(points), "points": []},
        ply_bytes=server._pts_to_ply(np.asarray(points, dtype=np.float64), None),
    )


class TestSnapEndpoint:
    def test_returns_nearest_point_of_full_cloud(self, snap_env):
        store, _ = snap_env
        cloud = _grid(3)
        _save(store, "sess1", anon_owner("alice"), cloud)
        resp = server.snap_to_session(
            "sess1", {"points": [cloud[4] + 0.05], "max_distance": 1.0},
            client_id="alice", x_auth_token=None,
        )
        import json
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert resp.status_code == 200
        assert body["ok"] is True and body["hits"] == 1
        assert np.allclose(body["results"][0]["point"], cloud[4], atol=0)
        assert "elapsed_ms" in body

    def test_owner_mismatch_is_404(self, snap_env):
        """越权访问必须 404（与「不存在」不可区分，避免探测）。"""
        store, _ = snap_env
        _save(store, "sess1", anon_owner("alice"), _grid(2))
        resp = server.snap_to_session(
            "sess1", {"points": [[0.0, 0.0, 0.0]]},
            client_id="bob", x_auth_token=None,
        )
        assert resp.status_code == 404

    def test_unknown_session_is_404(self, snap_env):
        resp = server.snap_to_session(
            "nope", {"points": [[0.0, 0.0, 0.0]]}, client_id="alice",
            x_auth_token=None,
        )
        assert resp.status_code == 404

    def test_out_of_range_batch(self, snap_env):
        store, _ = snap_env
        _save(store, "sess1", anon_owner("alice"), _grid(2))
        import json
        resp = server.snap_to_session(
            "sess1",
            {"points": [[0.0, 0.0, 0.0], [50.0, 50.0, 50.0]], "max_distance": 0.1},
            client_id="alice", x_auth_token=None,
        )
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert [r["hit"] for r in body["results"]] == [True, False]
        assert body["hits"] == 1

    @pytest.mark.parametrize("bad", [
        {}, {"points": []}, {"points": "nope"},
        {"points": [[0.0, 0.0]]},                     # 不是 3 维
        {"points": [[float("nan"), 0.0, 0.0]]},       # 非有限值
        {"points": [[0.0, 0.0, 0.0]], "max_distance": "far"},
    ])
    def test_bad_payload_is_400(self, snap_env, bad):
        store, _ = snap_env
        _save(store, "sess1", anon_owner("alice"), _grid(2))
        resp = server.snap_to_session("sess1", bad, client_id="alice",
                                      x_auth_token=None)
        assert resp.status_code == 400

    def test_too_many_queries_is_400(self, snap_env):
        store, _ = snap_env
        _save(store, "sess1", anon_owner("alice"), _grid(2))
        resp = server.snap_to_session(
            "sess1", {"points": [[0.0, 0.0, 0.0]] * (server._SNAP_MAX_QUERIES + 1)},
            client_id="alice", x_auth_token=None,
        )
        assert resp.status_code == 400
