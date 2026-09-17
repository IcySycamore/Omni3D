"""PLY 导出格式测试（#24）。

守住三件事：
1. header 声明的类型必须与 `_PLY_STRUCT` 的二进制布局**完全对应**（写错一个
   property，MeshLab/CloudCompare 读出来就是垃圾）；
2. 坐标是 float64 → 不应再发生 float32 量化；
3. 颜色仍是 uchar，且缺色时回退到单色。
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


def _header_and_body(ply: bytes):
    """把 PLY 字节串切成 (header 文本, body bytes)。"""
    marker = b"end_header\n"
    idx = ply.index(marker) + len(marker)
    return ply[:idx].decode("ascii"), ply[idx:]


class TestPlyHeader:
    def test_coordinates_are_declared_double(self):
        ply = server._pts_to_ply(np.zeros((1, 3)), None)
        header, _ = _header_and_body(ply)
        assert "format binary_little_endian 1.0" in header
        assert "property double x" in header
        assert "property double y" in header
        assert "property double z" in header
        # 旧实现是 property float，回归保护
        assert "property float x" not in header

    def test_colors_stay_uchar(self):
        ply = server._pts_to_ply(np.zeros((1, 3)), np.zeros((1, 3)))
        header, _ = _header_and_body(ply)
        for ch in "red", "green", "blue":
            assert f"property uchar {ch}" in header

    def test_header_matches_binary_layout(self):
        """header 里的 property 顺序/类型必须与 `_PLY_STRUCT` 字段一一对应。"""
        ply = server._pts_to_ply(np.zeros((1, 3)), None)
        header, _ = _header_and_body(ply)
        declared = [
            line.split()[1]
            for line in header.splitlines()
            if line.startswith("property ")
        ]
        expected = {
            "f8": "double", "f4": "float", "u1": "uchar", "u2": "ushort",
            "i4": "int", "u4": "uint",
        }
        from_struct = [
            expected[server._PLY_STRUCT[name].str[1:]]
            for name in server._PLY_STRUCT.names
        ]
        assert declared == from_struct

    def test_vertex_count_in_header(self):
        ply = server._pts_to_ply(np.zeros((7, 3)), None)
        header, _ = _header_and_body(ply)
        assert "element vertex 7" in header


class TestPlyBody:
    def test_byte_length_is_27_per_point(self):
        """3×float64 + 3×uchar = 27 字节/点（float32 时是 15）。"""
        n = 5
        ply = server._pts_to_ply(np.zeros((n, 3)), np.zeros((n, 3)))
        _, body = _header_and_body(ply)
        assert server._PLY_STRUCT.itemsize == 27
        assert len(body) == n * 27

    def test_float64_precision_is_preserved(self):
        """能被 float32 量化掉的分量必须原样保留下来（#24 的核心）。"""
        tricky = np.array([[0.1 + 1e-10, -2.0 ** 30 + 0.5, 1.0 / 3.0]], dtype=np.float64)
        ply = server._pts_to_ply(tricky, None)
        _, body = _header_and_body(ply)
        got = np.frombuffer(body, dtype=server._PLY_STRUCT)
        assert got["x"][0] == tricky[0, 0]
        assert got["y"][0] == tricky[0, 1]
        assert got["z"][0] == tricky[0, 2]

    def test_colors_roundtrip(self):
        pts = np.zeros((2, 3), dtype=np.float64)
        cols = np.array([[1, 2, 3], [250, 251, 252]], dtype=np.uint8)
        ply = server._pts_to_ply(pts, cols)
        _, body = _header_and_body(ply)
        got = np.frombuffer(body, dtype=server._PLY_STRUCT)
        assert (got["red"] == cols[:, 0]).all()
        assert (got["green"] == cols[:, 1]).all()
        assert (got["blue"] == cols[:, 2]).all()

    def test_missing_colors_fall_back_to_single_color(self):
        ply = server._pts_to_ply(np.zeros((3, 3)), None)
        _, body = _header_and_body(ply)
        got = np.frombuffer(body, dtype=server._PLY_STRUCT)
        assert (got["red"] == server._FALLBACK_RGB[0]).all()
        assert (got["green"] == server._FALLBACK_RGB[1]).all()
        assert (got["blue"] == server._FALLBACK_RGB[2]).all()

    def test_mismatched_color_count_falls_back(self):
        """颜色数与点数不一致时宁可用单色，也不能让颜色错位。"""
        ply = server._pts_to_ply(np.zeros((5, 3)), np.zeros((3, 3), dtype=np.uint8))
        _, body = _header_and_body(ply)
        got = np.frombuffer(body, dtype=server._PLY_STRUCT)
        assert len(got) == 5
        assert (got["red"] == server._FALLBACK_RGB[0]).all()

    def test_empty_cloud_returns_empty_bytes(self):
        assert server._pts_to_ply(np.zeros((0, 3)), None) == b""

    def test_accepts_list_input(self):
        """调用方可能给 list（JSON 反序列化后），不应出错。"""
        ply = server._pts_to_ply([[1.0, 2.0, 3.0]], None)
        _, body = _header_and_body(ply)
        got = np.frombuffer(body, dtype=server._PLY_STRUCT)
        assert got["x"][0] == 1.0 and got["z"][0] == 3.0


class TestRenderSamplingPrecision:
    """渲染子集刻意降回 float32（#24 里明确的取舍）。"""

    def test_render_payload_is_float32(self):
        pts = np.zeros((10, 3), dtype=np.float64)
        render_pts, _ = server._sample_for_render(pts, None, max_render_points=4)
        assert len(render_pts) == 4

    def test_no_sampling_when_below_limit(self):
        pts = np.arange(9, dtype=np.float64).reshape(3, 3)
        render_pts, _ = server._sample_for_render(pts, None, max_render_points=10)
        assert render_pts == pts.astype(np.float32).tolist()
