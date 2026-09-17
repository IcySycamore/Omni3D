"""几何测量纯函数 + 测量端点（#28）。

要点：
- 退化输入必须给 0 而不是 NaN（共线 / 重合点）；
- 面积**恰为**三角形面积的 2 倍（不是近似）；
- 尺度换算按量纲：长度 ×s、面积 ×s²、体积 ×s³；
- 测量值**不存死**：改 scale 后重读同一标注，数值随之变化。
"""
from __future__ import annotations

import json
import math
import os
import sys

import pytest

# torch 必须最先导入（本机 fbgemm.dll 加载顺序冲突）
import torch  # noqa: F401,I001

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB = os.path.join(_ROOT, "web")
if _WEB not in sys.path:
    sys.path.insert(0, _WEB)

import server  # noqa: E402
from app.core.geometry import (  # noqa: E402
    GeometryError,
    apply_scale_factor,
    compute,
    parallelogram_area,
    parallelepiped_volume,
    refresh_measurements,
    resolve_points,
    segment_length,
    triangle_area,
    unit_for,
)
from session_store import SessionStore, anon_owner  # noqa: E402

O = [0.0, 0.0, 0.0]
X = [1.0, 0.0, 0.0]
Y = [0.0, 1.0, 0.0]
Z = [0.0, 0.0, 1.0]


class TestLength:
    def test_known_values(self):
        assert segment_length(O, X) == pytest.approx(1.0)
        assert segment_length(O, [3.0, 4.0, 0.0]) == pytest.approx(5.0)
        assert segment_length([1.0, 1.0, 1.0], [2.0, 2.0, 2.0]) == pytest.approx(
            math.sqrt(3.0))

    def test_coincident_is_zero_not_nan(self):
        assert segment_length(O, list(O)) == 0.0

    def test_bad_input_raises(self):
        with pytest.raises(GeometryError):
            segment_length([0.0, 0.0], O)
        with pytest.raises(GeometryError):
            segment_length(O, [float("nan"), 0.0, 0.0])


class TestArea:
    def test_triangle_known_value(self):
        # 直角边 1,1 的直角三角形 → 0.5
        assert triangle_area(O, X, Y) == pytest.approx(0.5)

    def test_area_is_exactly_twice_triangle(self):
        """验收要求：面积恰为三角形的 2 倍（不是近似相等）。"""
        cases = [
            (O, X, Y),
            (O, [1.0, 2.0, 3.0], [4.0, 5.0, 6.0]),
            ([-1.5, 0.3, 2.0], [0.0, 0.0, 0.0], [7.0, -2.0, 1.0]),
        ]
        for a, b, c in cases:
            assert parallelogram_area(a, b, c) == triangle_area(a, b, c) * 2

    def test_collinear_is_zero(self):
        assert triangle_area(O, X, [2.0, 0.0, 0.0]) == pytest.approx(0.0)
        assert parallelogram_area(O, X, [2.0, 0.0, 0.0]) == pytest.approx(0.0)

    def test_coincident_is_zero(self):
        assert triangle_area(O, O, O) == 0.0
        assert parallelogram_area(O, O, X) == 0.0

    def test_orthogonal_edges(self):
        # AB=(1,0,0), AC=(0,2,0) → |AB×AC| = 2
        assert parallelogram_area(O, X, [0.0, 2.0, 0.0]) == pytest.approx(2.0)


class TestVolume:
    def test_unit_cube(self):
        assert parallelepiped_volume(O, X, Y, Z) == pytest.approx(1.0)

    def test_equals_edge_product_when_orthogonal(self):
        a, b, c, d = O, [2.0, 0.0, 0.0], [0.0, 3.0, 0.0], [0.0, 0.0, 4.0]
        assert parallelepiped_volume(a, b, c, d) == pytest.approx(24.0)

    def test_coplanar_is_zero(self):
        assert parallelepiped_volume(O, X, Y, [1.0, 1.0, 0.0]) == pytest.approx(0.0)

    def test_coincident_is_zero(self):
        assert parallelepiped_volume(O, O, O, O) == 0.0


class TestScaleFactor:
    @pytest.mark.parametrize("dim,expected", [(1, 2.0), (2, 4.0), (3, 8.0)])
    def test_dimension_exponents(self, dim, expected):
        """验收要求：标度换算维度正确（×s、×s²、×s³）。"""
        assert apply_scale_factor(1.0, dim, 2.0) == pytest.approx(expected)

    def test_none_scale_is_identity(self):
        for dim in (1, 2, 3):
            assert apply_scale_factor(3.5, dim, None) == 3.5

    def test_bad_dim_raises(self):
        with pytest.raises(GeometryError):
            apply_scale_factor(1.0, 0, 2.0)
        with pytest.raises(GeometryError):
            unit_for(4, True)

    def test_units(self):
        assert unit_for(1, True) == "m"
        assert unit_for(2, True) == "m²"
        assert unit_for(3, True) == "m³"
        assert unit_for(1, False) == "u"
        assert unit_for(2, False) == "u²"
        assert unit_for(3, False) == "u³"


class TestComputeDispatch:
    def test_dispatch(self):
        assert compute("length", [O, X]) == pytest.approx(1.0)
        assert compute("triangle_area", [O, X, Y]) == pytest.approx(0.5)
        assert compute("area", [O, X, Y]) == pytest.approx(1.0)
        assert compute("volume", [O, X, Y, Z]) == pytest.approx(1.0)

    def test_wrong_point_count_raises(self):
        with pytest.raises(GeometryError):
            compute("length", [O])
        with pytest.raises(GeometryError):
            compute("area", [O, X, Y, Z])   # 多了第 4 个点 → 报错而不是静默忽略
        with pytest.raises(GeometryError):
            compute("volume", [O, X, Y])

    def test_unknown_op_raises(self):
        with pytest.raises(GeometryError):
            compute("nope", [O, X])


def _elements() -> list:
    return [
        {"id": "e1", "kind": "point", "points": [O]},
        {"id": "e2", "kind": "point", "points": [X]},
        {"id": "e3", "kind": "point", "points": [Y]},
        {"id": "e4", "kind": "point", "points": [Z]},
        {"id": "s1", "kind": "segment", "refs": ["e1", "e2"]},
        {"id": "a1", "kind": "face", "refs": ["e1", "e2", "e3"]},
        {"id": "v1", "kind": "solid", "refs": ["e1", "e2", "e3", "e4"]},
    ]


class TestResolvePoints:
    def test_point_and_segment(self):
        by_id = {e["id"]: e for e in _elements()}
        assert resolve_points(by_id, "e1") == [O]
        assert resolve_points(by_id, "s1") == [O, X]
        assert resolve_points(by_id, "a1") == [O, X, Y]
        assert len(resolve_points(by_id, "v1")) == 4

    def test_missing_element_raises(self):
        with pytest.raises(GeometryError, match="不存在"):
            resolve_points({}, "nope")

    def test_cycle_raises(self):
        by_id = {
            "a": {"id": "a", "refs": ["b"]},
            "b": {"id": "b", "refs": ["a"]},
        }
        with pytest.raises(GeometryError, match="成环"):
            resolve_points(by_id, "a")

    def test_element_without_points_or_refs_raises(self):
        with pytest.raises(GeometryError):
            resolve_points({"e": {"id": "e"}}, "e")


class TestRefreshMeasurements:
    def test_recomputes_with_scale(self):
        ann = {"version": 1, "elements": _elements() + [
            {"id": "m1", "kind": "measurement", "op": "length", "refs": ["e1", "e2"]},
        ]}
        refresh_measurements(ann, None)
        m = ann["elements"][-1]
        assert m["value"] == pytest.approx(1.0)
        assert m["unit"] == "u" and m["calibrated"] is False

        # 标定后同一份标注重算 → 数值跟着变
        refresh_measurements(ann, 2.5)
        assert m["value"] == pytest.approx(2.5)
        assert m["unit"] == "m" and m["calibrated"] is True

    def test_broken_reference_marks_error_without_raising(self):
        """一条坏标注不该让整个历史打不开。"""
        ann = {"elements": [
            {"id": "m1", "kind": "measurement", "op": "length", "refs": ["ghost"]},
        ]}
        refresh_measurements(ann, 1.0)
        assert "error" in ann["elements"][0]
        assert "value" not in ann["elements"][0]

    def test_non_measurement_elements_untouched(self):
        ann = {"elements": [{"id": "e1", "kind": "point", "points": [O]}]}
        before = json.dumps(ann, sort_keys=True)
        refresh_measurements(ann, 3.0)
        assert json.dumps(ann, sort_keys=True) == before

    def test_none_annotations_passthrough(self):
        assert refresh_measurements(None, 1.0) is None


# ══════════════════ 端点 ══════════════════

@pytest.fixture()
def env(tmp_path, monkeypatch):
    store = SessionStore(
        db_path=str(tmp_path / "sessions.db"),
        sessions_dir=str(tmp_path / "ply"),
    )
    monkeypatch.setattr(server, "session_store", store)
    owner = anon_owner("alice")
    store.save_session(session_id="s1", owner=owner,
                       result={"num_views": 2, "num_points": 4, "points": []})
    yield store, owner
    store.close()


def _body(resp) -> dict:
    return json.loads(bytes(resp.body).decode("utf-8"))


def _put_elements(env) -> None:
    store, owner = env
    store.save_annotations("s1", owner, {"version": 1, "elements": _elements()})


class TestMeasureEndpoint:
    def test_length_without_calibration(self, env):
        _put_elements(env)
        resp = server.measure_session(
            "s1", {"op": "length", "element_ids": ["e1", "e2"]},
            client_id="alice", x_auth_token=None,
        )
        assert resp.status_code == 200
        body = _body(resp)
        m = body["measurement"]
        assert m["value"] == pytest.approx(1.0)
        assert m["unit"] == "u" and m["calibrated"] is False
        assert m["dim"] == 1
        assert m["points"] == [O, X]

    def test_area_is_twice_triangle(self, env):
        _put_elements(env)
        area = _body(server.measure_session(
            "s1", {"op": "area", "element_ids": ["e1", "e2", "e3"]},
            client_id="alice", x_auth_token=None))["measurement"]
        tri = _body(server.measure_session(
            "s1", {"op": "triangle_area", "element_ids": ["e1", "e2", "e3"]},
            client_id="alice", x_auth_token=None))["measurement"]
        assert area["value"] == pytest.approx(tri["value"] * 2)

    def test_can_reference_a_derived_element(self, env):
        """直接引用线段元素也算得出长度（沿 refs 展开）。"""
        _put_elements(env)
        m = _body(server.measure_session(
            "s1", {"op": "length", "element_ids": ["s1"]},
            client_id="alice", x_auth_token=None))["measurement"]
        assert m["value"] == pytest.approx(1.0)

    def test_volume(self, env):
        _put_elements(env)
        m = _body(server.measure_session(
            "s1", {"op": "volume", "element_ids": ["v1"]},
            client_id="alice", x_auth_token=None))["measurement"]
        assert m["value"] == pytest.approx(1.0)
        assert m["unit"] == "u³"

    def test_one_call_both_computes_and_persists(self, env):
        """验收要求：一次调用既得到数值又落标注。"""
        store, owner = env
        _put_elements(env)
        server.measure_session("s1", {"op": "length", "element_ids": ["e1", "e2"]},
                               client_id="alice", x_auth_token=None)
        stored = store.get_annotations("s1", owner)
        kinds = [e["kind"] for e in stored["elements"]]
        assert kinds.count("measurement") == 1

    def test_scale_change_updates_stored_measurement(self, env):
        """验收要求：改动 scale 后重读同一标注，数值随之变化。"""
        store, owner = env
        _put_elements(env)
        server.measure_session("s1", {"op": "area", "element_ids": ["e1", "e2", "e3"]},
                               client_id="alice", x_auth_token=None)
        first = _body(server.get_history("s1", client_id="alice",
                                         x_auth_token=None))
        m0 = [e for e in first["annotations"]["elements"]
              if e["kind"] == "measurement"][0]
        assert m0["value"] == pytest.approx(1.0) and m0["unit"] == "u²"

        store.update_scale(session_id="s1", owner=owner, scale=3.0)
        second = _body(server.get_history("s1", client_id="alice",
                                          x_auth_token=None))
        m1 = [e for e in second["annotations"]["elements"]
              if e["kind"] == "measurement"][0]
        # 面积 ×s² = 9
        assert m1["value"] == pytest.approx(9.0)
        assert m1["unit"] == "m²" and m1["calibrated"] is True

    def test_client_supplied_values_are_recomputed(self, env):
        """客户端在 PUT 里塞的 value 不作数，会被服务器按几何重算。"""
        store, owner = env
        payload = {"version": 1, "elements": _elements() + [
            {"id": "m1", "kind": "measurement", "op": "length", "refs": ["e1", "e2"],
             "value": 999.0, "unit": "km"},
        ]}
        body = _body(server.put_annotations("s1", payload, client_id="alice",
                                            x_auth_token=None))
        m = [e for e in body["annotations"]["elements"] if e["id"] == "m1"][0]
        assert m["value"] == pytest.approx(1.0)
        assert m["unit"] == "u"

    @pytest.mark.parametrize("bad", [
        {},
        {"op": "length"},
        {"op": "length", "element_ids": []},
        {"op": "length", "element_ids": "e1"},
        {"op": "nope", "element_ids": ["e1", "e2"]},
        {"op": "length", "element_ids": ["ghost"]},
        {"op": "area", "element_ids": ["e1", "e2"]},          # 点数不足
        {"op": "length", "element_ids": ["e1"]},              # 点数不足
    ])
    def test_bad_request_is_400(self, env, bad):
        _put_elements(env)
        assert server.measure_session("s1", bad, client_id="alice",
                                      x_auth_token=None).status_code == 400

    def test_no_annotations_is_400(self, env):
        resp = server.measure_session("s1", {"op": "length", "element_ids": ["e1", "e2"]},
                                      client_id="alice", x_auth_token=None)
        assert resp.status_code == 400

    def test_owner_mismatch_is_404(self, env):
        _put_elements(env)
        resp = server.measure_session("s1", {"op": "length", "element_ids": ["e1", "e2"]},
                                      client_id="bob", x_auth_token=None)
        assert resp.status_code == 404

    def test_too_many_elements_is_400(self, env):
        _put_elements(env)
        ids = ["e1"] * (server._MEASURE_MAX_ELEMENTS + 1)
        resp = server.measure_session("s1", {"op": "length", "element_ids": ids},
                                      client_id="alice", x_auth_token=None)
        assert resp.status_code == 400


class TestScaleEndpointWithElementIds:
    def test_infer_scale_from_element_ids(self, env):
        """尺度反推改成传两个元素 id（不再传裸坐标）。"""
        _put_elements(env)
        resp = server.infer_scale("s1", {"element_ids": ["e1", "e2"],
                                         "real_distance": 0.25},
                                  client_id="alice", x_auth_token=None)
        assert resp.status_code == 200
        body = _body(resp)
        # 模型距离 1.0 → 真实 0.25 → scale = 0.25
        assert body["scale"] == pytest.approx(0.25)
        assert body["persisted"] is True

    def test_legacy_raw_points_still_work(self, env):
        resp = server.infer_scale("s1", {"point_a": O, "point_b": X,
                                         "real_distance": 2.0},
                                  client_id="alice", x_auth_token=None)
        assert resp.status_code == 200
        assert _body(resp)["scale"] == pytest.approx(2.0)

    def test_unknown_element_is_400(self, env):
        _put_elements(env)
        resp = server.infer_scale("s1", {"element_ids": ["e1", "ghost"],
                                         "real_distance": 1.0},
                                  client_id="alice", x_auth_token=None)
        assert resp.status_code == 400
