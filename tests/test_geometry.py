"""几何测量纯函数 + 测量端点（#28）。

要点：
- 退化输入必须给 0 而不是 NaN（共线 / 重合点）；
- 面积**恰为**三角形面积的 2 倍（不是近似）；
- 尺度换算按量纲：长度 ×s、面积 ×s²、体积 ×s³；
- 测量值**不存死**：改 scale 后重读同一标注，数值随之变化。
"""
from __future__ import annotations

import itertools
import json
import math
import os
import sys

import pytest

# torch 必须最先导入（本机 fbgemm.dll 加载顺序冲突）
import torch  # noqa: F401,I001

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from panel import server  # noqa: E402
from server.core.geometry import (  # noqa: E402
    GeometryError,
    apply_scale_factor,
    compute,
    convex_hull_edges,
    parallelogram_area,
    parallelepiped_volume,
    refresh_measurements,
    resolve_measurement,
    resolve_points,
    segment_length,
    triangle_area,
    unit_for,
)
from panel.session_store import SessionStore, anon_owner  # noqa: E402

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


def _degrees(edges: list, n: int) -> list:
    """每个顶点被几根棱接上（棱要用度数判断就是拿来干这个的）。"""
    counts = [0] * n
    for i, j in edges:
        counts[i] += 1
        counts[j] += 1
    return counts


def _triangular_prism() -> list:
    """三棱柱的 6 个顶点 —— 每个顶点的度数都是 **3**（用户实测的反例）。"""
    base = [[0.0, 0.0, 0.0], [2.0, 0.0, 0.0], [0.0, 2.0, 0.0]]
    return base + [[x, y, z + 1.5] for x, y, z in base]


CUBE_2 = [[float(x), float(y), float(z)]
          for x in (0, 2) for y in (0, 2) for z in (0, 2)]


class TestPolygonArea:
    """手选顶点算凸多边形面积（「面积」工具现在用这个）。"""

    def test_three_points_degenerate_to_a_triangle(self):
        """恰 3 点 = 三角形面积。

        注意这**不是**旧的 `area`（平行四边形，恰好 2 倍）：用户要的是「选这块
        面的全部顶点」，3 个顶点围出来的就是三角形。
        """
        assert compute("polygon_area", [O, X, Y]) == pytest.approx(0.5)
        assert compute("polygon_area", [O, X, Y]) == pytest.approx(
            triangle_area(O, X, Y))

    def test_square(self):
        assert compute("polygon_area", [O, X, [1.0, 1.0, 0.0], Y]) == pytest.approx(1.0)

    def test_regular_hexagon(self):
        """正六边形（外接圆半径 1）面积 = 3√3/2。"""
        hexa = [[math.cos(k * math.pi / 3), math.sin(k * math.pi / 3), 0.0]
                for k in range(6)]
        assert compute("polygon_area", hexa) == pytest.approx(3 * math.sqrt(3) / 2)

    def test_order_direction_does_not_matter(self):
        """顺时针 / 逆时针都行。"""
        sq = [O, X, [1.0, 1.0, 0.0], Y]
        assert compute("polygon_area", sq) == pytest.approx(
            compute("polygon_area", list(reversed(sq))))

    def test_any_order_gives_the_same_answer(self):
        """**任意**顺序都给同一个数 —— 不只是顺/逆时针。

        为什么必须这样：调用方给的顺序可能是「元素创建顺序」（用户在列表里勾选
        顶点那条路），与几何环绕毫无关系。实测同一个正方形的四个角，按创建顺序
        （恰好是 Z 字形、自交）用 shoelace 得 **0**，而「完成」照样是亮的
        —— 用户会拿 0 当结果。所以排序收在服务端内部做。
        """
        sq = [O, X, [1.0, 1.0, 0.0], Y]
        got = {round(compute("polygon_area", p), 9)
               for p in itertools.permutations(sq)}
        assert got == {1.0}, f"不同顶点顺序给出了不同结果：{got}"

    def test_accepts_more_than_the_minimum(self):
        """「至少 3 个点」：多给不报错 —— 这正是它区别于 `area` 的地方。"""
        assert compute("polygon_area", [O, X, [1.0, 1.0, 0.0], Y]) == pytest.approx(1.0)

    def test_rejects_fewer_than_three(self):
        with pytest.raises(GeometryError, match="至少需要 3 个点"):
            compute("polygon_area", [O, X])

    def test_collinear_first_three_no_longer_wrecks_it(self):
        """前三点共线不再报废。

        旧实现拿**首三点**钉平面 —— 一且前三个刚好共线（点云上随手勾选时太容易了，
        比如沿同一条棱勾了两个点）就直接返回 0。现在用最小二乘拟合平面，
        不需要挑出「合适的三个点」。
        """
        assert compute("polygon_area", [O, X, [2.0, 0.0, 0.0], Y]) == pytest.approx(1.0)

    def test_all_collinear_gives_zero(self):
        """所有点共线 → 围不出面积 → 0（不是 NaN）。"""
        assert compute("polygon_area", [O, X, [2.0, 0.0, 0.0], [3.0, 0.0, 0.0]]) == 0.0

    def test_off_plane_points_are_projected(self):
        """容差内的点会被投影到拟合平面上 —— 略有偏离不算错。

        正方形 O-X-(1,1)-Y，**最后一个**顶点抬高了 0.02；拟合平面随之微微倾斜，
        所以结果不是精确的 1，而是 1.0001（投影面积 = 真实面积 / cos θ）。
        旧实现拿首三点钉平面、能得到精确的 1 —— 但那是靠“挑对了前三个点”，
        代价是前三点一旦共线就整个报废（见上一条）。
        """
        tilted = [O, X, [1.0, 1.0, 0.0], [0.0, 1.0, 0.02]]
        assert compute("polygon_area", tilted) == pytest.approx(1.0, abs=1e-3)


class TestPolyhedronVolume:
    """手选顶点算凸包体积（「体积」工具现在用这个）。"""

    def test_tetrahedron(self):
        """4 点 = 四面体体积 = |det| / 6。"""
        assert compute("polyhedron_volume", [O, X, Y, Z]) == pytest.approx(1.0 / 6.0)

    def test_cube_from_all_eight_vertices(self):
        """用户选的是「凸多面体的所有顶点」—— 8 个一起给。"""
        assert compute("polyhedron_volume", CUBE_2) == pytest.approx(8.0)

    def test_order_does_not_matter(self):
        """凸包只对**集合**定义：连线的顺序不影响体积。"""
        assert compute("polyhedron_volume", CUBE_2) == pytest.approx(
            compute("polyhedron_volume", list(reversed(CUBE_2))))

    def test_interior_points_are_ignored(self):
        """手选时很难避开内部点 —— 凸包会忽略它，体积不变。"""
        pts = [O, X, Y, Z, [0.2, 0.2, 0.2]]
        assert compute("polyhedron_volume", pts) == pytest.approx(1.0 / 6.0)

    def test_coplanar_points_give_zero_not_error(self):
        """共面 → 体积 0（用户定的口径），**不能**报错。"""
        assert compute("polyhedron_volume", [O, X, Y, [1.0, 1.0, 0.0]]) == 0.0

    def test_collinear_points_give_zero(self):
        assert compute(
            "polyhedron_volume", [O, X, [2.0, 0.0, 0.0], [3.0, 0.0, 0.0]]) == 0.0

    def test_rejects_fewer_than_four(self):
        with pytest.raises(GeometryError, match="至少需要 4 个点"):
            compute("polyhedron_volume", [O, X, Y])


class TestConvexHullOutline:
    """体积的骨架由服务端给（`convex_hull_edges`）—— 前端不再自己算一遍。

    为什么必须由服务端给：体积算的是 `ConvexHull.volume`，只有**同一个**凸包的
    棱画出来才与算出来的数值对应；客户端另算一份就是两套真相
    （用户：「现在测的是什么不清楚」）。
    """

    def test_cube_has_twelve_edges_and_degree_three(self):
        edges = convex_hull_edges(CUBE_2)
        assert len(edges) == 12  # 立方体：6 面 × 2 条不重复的棱
        assert _degrees(edges, len(CUBE_2)) == [3] * 8

    def test_triangular_prism_degree_is_three(self):
        """⚠️ 用户的用例：三棱柱每个顶点的度数是 **3**，不是 2。

        旧的「每顶点最多两条线 + 必须收口」是**面积**的语义（多边形的边界：
        每个角恰好接两条边）。拿它去卡体积，三棱柱的正确骨架会被判成不合法。
        """
        pts = _triangular_prism()
        edges = convex_hull_edges(pts)
        assert len(edges) == 9  # 上底 3 + 下底 3 + 侧棱 3
        assert _degrees(edges, len(pts)) == [3] * 6

    def test_edges_are_unique_and_undirected(self):
        edges = convex_hull_edges(CUBE_2)
        assert len({tuple(e) for e in edges}) == len(edges), "有重复的棱"
        assert all(i < j for i, j in edges), "同一条棱只能有一个方向"
        assert all(0 <= i < len(CUBE_2) and 0 <= j < len(CUBE_2)
                   for i, j in edges), "返回的是索引，不是坐标"

    def test_degenerate_input_has_no_edges(self):
        """共面 / 共线 / 点数不够 → 空表（与体积给 0 同一口径：不报错）。"""
        assert convex_hull_edges([O, X, Y, [1.0, 1.0, 0.0]]) == []
        assert convex_hull_edges([O, X, [2.0, 0.0, 0.0], [3.0, 0.0, 0.0]]) == []
        assert convex_hull_edges([O, X, Y]) == []
        assert convex_hull_edges([]) == []

    def test_volume_measurement_carries_the_outline(self):
        """`resolve_measurement` 要把骨架一起给出来，前端才有得画。"""
        pts = _triangular_prism()
        elements = {f"p{i}": {"id": f"p{i}", "kind": "point", "points": [p]}
                    for i, p in enumerate(pts)}
        solved = resolve_measurement(elements, {
            "id": "m1", "kind": "measurement", "op": "polyhedron_volume",
            "refs": [f"p{i}" for i in range(len(pts))],
        })
        assert solved["raw"] == pytest.approx(3.0)  # 底面积 2 × 高 1.5
        assert len(solved["outline"]) == 9
        assert _degrees(solved["outline"], len(pts)) == [3] * 6

    def test_area_measurement_has_no_hull_outline(self):
        """面积不是凸包 —— 它的骨架就是点按序连起来，客户端本来就有。"""
        elements = {k: {"id": k, "kind": "point", "points": [p]}
                    for k, p in {"a": O, "b": X, "c": Y}.items()}
        solved = resolve_measurement(elements, {
            "id": "m1", "kind": "measurement", "op": "polygon_area",
            "refs": ["a", "b", "c"],
        })
        assert solved["outline"] == []

    def test_refresh_keeps_the_outline_in_sync(self):
        """每次读取都重算 —— 骨架不能是存死的快照。"""
        annotations = {"version": 1, "elements": [
            {"id": f"p{i}", "kind": "point", "points": [p]}
            for i, p in enumerate(_triangular_prism())
        ] + [{"id": "m1", "kind": "measurement", "op": "polyhedron_volume",
              "refs": [f"p{i}" for i in range(6)]}]}
        refreshed = refresh_measurements(annotations, None)
        m = [e for e in refreshed["elements"] if e["id"] == "m1"][0]
        assert m["value"] == pytest.approx(3.0)
        assert len(m["outline"]) == 9

    def test_bad_measurement_drops_the_stale_outline(self):
        """算不出来的测量不能留着上次的骨架（会画出个不存在的东西）。"""
        annotations = {"version": 1, "elements": [{
            "id": "m1", "kind": "measurement", "op": "polyhedron_volume",
            "refs": ["ghost"], "outline": [[0, 1]], "value": 9.0,
        }]}
        refreshed = refresh_measurements(annotations, None)
        m = refreshed["elements"][0]
        assert "error" in m
        assert "outline" not in m


class TestCoplanarJudgement:
    """共面容差口径：第 4 点起，距离 / 模型包络 < 3%（模型世界单位）。"""

    def test_ratio_is_three_percent(self):
        from server.core import geometry
        assert geometry.COPLANAR_TOLERANCE_RATIO == 0.03

    def test_envelope_is_the_cube_root_of_the_bbox_volume(self):
        """包络取**立方根**：距离 / 体积量纲不成立，必须换成同量纲的长度。"""
        from server.core import geometry
        assert geometry.bbox_volume(CUBE_2) == pytest.approx(8.0)
        assert geometry.envelope_scale(CUBE_2) == pytest.approx(2.0)

    def test_tolerance_scales_with_the_model(self):
        from server.core import geometry
        assert geometry.coplanar_tolerance(2.0) == pytest.approx(0.06)
        assert geometry.coplanar_tolerance(100.0) == pytest.approx(3.0)

    def test_empty_points_have_zero_envelope(self):
        from server.core import geometry
        assert geometry.bbox_volume([]) == 0.0
        assert geometry.envelope_scale([]) == 0.0

    def test_plane_distance(self):
        from server.core import geometry
        # a,b,c 定的是 z=0 平面 → 距离就是 |z|
        assert geometry.plane_distance([0.0, 0.0, 0.5], O, X, Y) == pytest.approx(0.5)

    def test_degenerate_plane_gives_zero(self):
        """三点共线 → 平面没定义 → 返回 0。

        不要反过来把用户的点判成「不共面」——「无从谈起」不是「违反」。
        """
        from server.core import geometry
        assert geometry.plane_distance([5.0, 5.0, 5.0], O, X, [2.0, 0.0, 0.0]) == 0.0

    def test_coplanar_distances(self):
        from server.core import geometry
        got = geometry.coplanar_distances(
            [O, X, Y], [[0.5, 0.5, 0.0], [0.5, 0.5, 0.3]])
        assert got[0] == pytest.approx(0.0)
        assert got[1] == pytest.approx(0.3)

    def test_coplanar_distances_needs_three_base_points(self):
        from server.core import geometry
        with pytest.raises(GeometryError):
            geometry.coplanar_distances([O, X], [[0.0, 0.0, 0.1]])


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

    def test_element_limit_allows_a_many_vertex_solid(self, env):
        """原上限 8 会把一个 10 个顶点的体积直接判成 400（手选全顶点必超）。"""
        _put_elements(env)
        assert server._MEASURE_MAX_ELEMENTS > 8

    def test_polygon_area_end_to_end(self, env):
        """「面积」工具现在用 polygon_area：3 个顶点 = 三角形面积。"""
        _put_elements(env)
        m = _body(server.measure_session(
            "s1", {"op": "polygon_area", "element_ids": ["e1", "e2", "e3"]},
            client_id="alice", x_auth_token=None))["measurement"]
        assert m["value"] == pytest.approx(0.5)
        assert m["dim"] == 2

    def test_polyhedron_volume_end_to_end(self, env):
        """「体积」工具现在用 polyhedron_volume：4 个顶点 = 四面体。"""
        _put_elements(env)
        m = _body(server.measure_session(
            "s1",
            {"op": "polyhedron_volume", "element_ids": ["e1", "e2", "e3", "e4"]},
            client_id="alice", x_auth_token=None))["measurement"]
        assert m["value"] == pytest.approx(1.0 / 6.0)
        assert m["unit"] == "u³"

    def test_polyhedron_volume_endpoint_returns_its_hull(self, env):
        """体积的响应要带上「算的是哪个凸包」的骨架（`outline`）。

        用户反例：三棱柱每顶点度数是 **3**，而旧的「度数 ≤ 2 / 必须收口」
        是面积（多边形边界）的语义 —— 拿它卡体积，画出来的和算出来的对不上，
        于是「现在测的是什么不清楚」。
        """
        store, owner = env
        pts = _triangular_prism()
        store.save_annotations("s1", owner, {
            "version": 1,
            "elements": [{"id": f"p{i}", "kind": "point", "points": [p]}
                         for i, p in enumerate(pts)]})
        m = _body(server.measure_session(
            "s1",
            {"op": "polyhedron_volume",
             "element_ids": [f"p{i}" for i in range(len(pts))]},
            client_id="alice", x_auth_token=None))["measurement"]
        assert m["value"] == pytest.approx(3.0)
        assert len(m["outline"]) == 9
        assert _degrees(m["outline"], len(pts)) == [3] * 6

    def test_preview_computes_but_does_not_persist(self, env):
        """preview：只算不存 —— 网页逐点落点时看实时数值就靠它。"""
        store, owner = env
        _put_elements(env)
        body = _body(server.measure_session(
            "s1",
            {"op": "polygon_area", "element_ids": ["e1", "e2", "e3"],
             "preview": True},
            client_id="alice", x_auth_token=None))
        assert body["preview"] is True
        assert body["measurement"]["value"] == pytest.approx(0.5)
        stored = store.get_annotations("s1", owner)
        assert [e["kind"] for e in stored["elements"]].count("measurement") == 0

    def test_preview_still_validates_the_op(self, env):
        """预览不是「随便算」——非法 op 一样 400。"""
        _put_elements(env)
        resp = server.measure_session(
            "s1", {"op": "nope", "element_ids": ["e1", "e2"], "preview": True},
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
