"""几何测量纯函数 + 标注元素解析（无 torch / Qt 依赖，可独立单测）。

依赖只有标准库 + numpy/scipy（凸包用 `scipy.spatial.ConvexHull`；SOR 早就
在用 scipy 的 cKDTree，所以这不是新引入的重依赖）。

**为什么要有这个模块**：面积 / 体积 / 长度必须有**唯一实现** —— 两端各算一遍
必然出现「显示的数值」与「服务器存的标注」两套真相。

定义：

- 长度 `|AB|`
- 三角形面积 `½·|AB × AC|`
- 面积（元素为平行四边形，由 3 点确定）`|AB × AC|` —— **恰为三角形面积的 2 倍**
- 体积（平行六面体，由 4 点确定）`|det[AB, AC, AD]|` —— 与「长×宽×高」同值
- 凸多边形面积 `polygon_area`（**≥3 点**，手选的全部顶点）—— **顶点顺序无关**：
  最小二乘拟合平面 → 点投影上去 → 按绕质心极角重排 → shoelace；
  恰 3 点时退化为三角形面积
- 凸包体积 `polyhedron_volume`（**≥4 点**，手选的全部顶点）—— 顶点顺序无关；
  所有点共面（围不出体）时体积为 **0**，不报错

尺度换算：长度 ×s、面积 ×s²、体积 ×s³；未标定（`scale is None`）时返回模型单位
`u` / `u²` / `u³`，由调用方在结果上标记「未标定」。

**数值重算而非存死值**：测量元素只存 `op` + `refs`（几何引用），
`raw`（模型单位值）与 `value`（换算后）都在读取时由几何**重算**。
这样重新标定 scale 后，所有已有测量会自动跟着变，不需要回写任何记录。
"""
from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

import numpy as np
from scipy.spatial import ConvexHull, QhullError

__all__ = [
    "GeometryError",
    "OPS",
    "MIN_OPS",
    "COPLANAR_TOLERANCE_RATIO",
    "required_points",
    "segment_length",
    "triangle_area",
    "parallelogram_area",
    "parallelepiped_volume",
    "polygon_area",
    "polyhedron_volume",
    "bbox_volume",
    "envelope_scale",
    "coplanar_tolerance",
    "plane_distance",
    "coplanar_distances",
    "apply_scale_factor",
    "unit_for",
    "compute",
    "resolve_points",
    "resolve_measurement",
    "refresh_measurements",
]


class GeometryError(ValueError):
    """几何输入非法（点数不足 / 元素缺失 / 引用成环）。"""


# op → (所需点数, 量纲)
OPS = {
    "length": (2, 1),
    "segment_length": (2, 1),
    "triangle_area": (3, 2),
    "area": (3, 2),
    "parallelogram_area": (3, 2),
    "volume": (4, 3),
    "parallelepiped_volume": (4, 3),
    # 手选凸多边形 / 凸多面体的**全部顶点**：点数不设上限
    "polygon_area": (3, 2),
    "polyhedron_volume": (4, 3),
}

# 这几个 op 接受「**至少** need 个点」（其它一律要求精确匹配）。
# 为什么其它要精确匹配：多给一个点却静默忽略，用户会以为它算进去了。
MIN_OPS = frozenset({"polygon_area", "polyhedron_volume"})

_UNITS = {1: ("u", "m"), 2: ("u²", "m²"), 3: ("u³", "m³")}


def required_points(op: str) -> int:
    try:
        return OPS[op][0]
    except KeyError as exc:
        raise GeometryError(f"未知测量类型: {op!r}") from exc


def _vec(point: Sequence[float]) -> tuple[float, float, float]:
    if len(point) < 3:
        raise GeometryError("点坐标至少需要 3 个分量 (x, y, z)")
    try:
        v = (float(point[0]), float(point[1]), float(point[2]))
    except (TypeError, ValueError) as exc:
        raise GeometryError(f"点坐标含非法数值: {exc}") from exc
    if not all(math.isfinite(c) for c in v):
        raise GeometryError("点坐标含非有限数值")
    return v


def _sub(a, b) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(u, v) -> tuple[float, float, float]:
    return (u[1] * v[2] - u[2] * v[1],
            u[2] * v[0] - u[0] * v[2],
            u[0] * v[1] - u[1] * v[0])


def _dot(u, v) -> float:
    return u[0] * v[0] + u[1] * v[1] + u[2] * v[2]


def _norm(u) -> float:
    return math.sqrt(_dot(u, u))


def _unit(u):
    """归一化；零向量（退化）时返回 ``None`` 而不是抛错或给 NaN。"""
    n = _norm(u)
    if n <= 1e-12:
        return None
    return (u[0] / n, u[1] / n, u[2] / n)


def segment_length(a: Sequence[float], b: Sequence[float]) -> float:
    """两点距离 ``|AB|``。两点重合时为 0（不是错误）。"""
    return _norm(_sub(_vec(a), _vec(b)))


def triangle_area(a, b, c) -> float:
    """三角形面积 ``½·|AB × AC|``。三点共线 / 重合时为 0。"""
    va, vb, vc = _vec(a), _vec(b), _vec(c)
    return 0.5 * _norm(_cross(_sub(vb, va), _sub(vc, va)))


def parallelogram_area(a, b, c) -> float:
    """由 3 点确定的平行四边形面积 ``|AB × AC|``。

    **恰为 ``triangle_area`` 的 2 倍**（不是笔误，见模块 docstring）。
    """
    va, vb, vc = _vec(a), _vec(b), _vec(c)
    return _norm(_cross(_sub(vb, va), _sub(vc, va)))


def parallelepiped_volume(a, b, c, d) -> float:
    """由 4 点确定的平行六面体体积 ``|det[AB, AC, AD]|``。

    四点共面 / 退化时为 0。与「长×宽×高」同值（正交时即三边长度乘积）。
    """
    va, vb, vc, vd = _vec(a), _vec(b), _vec(c), _vec(d)
    ab, ac, ad = _sub(vb, va), _sub(vc, va), _sub(vd, va)
    return abs(_dot(ab, _cross(ac, ad)))


def polygon_area(points) -> float:
    """用手选顶点算凸多边形面积 —— **与顶点顺序无关**。

    内部流程：最小二乘拟合平面 → 点投影上去 → 按绕质心的极角重排 → shoelace。
    恰 3 点时退化为 ``½·|AB × AC|``（与 :func:`triangle_area` 同值）。

    为什么把排序放在这里：shoelace 要求顶点沿边界有序，而调用方给的顺序可能是
    「元素创建顺序」（用户在元素列表里勾选顶点那条路），与几何环绕毫无关系。
    乱序时 shoelace 会算出无意义的数 —— 实测同一个正方形的四个角，按创建顺序
    （恰好是 Z 字形、自交）得 **0**，按环绕顺序得 **1**。
    把这件事**收在服务端**，任何调用方（网页端、桌面端、以后别的客户端）
    都不需要自己保证顺序；客户端的排序就只是为了让连线画得好看。

    ⚠️ 前提是「凸」（更一般地：以质心为中心的星形多边形）。凹多边形会被当成
    星形处理、面积偏大 —— 使用口径是「任意选择凸多面性的顶点」。

    点共线 / 重合（拟合不出平面）时返回 0，不报错。
    """
    pts = np.asarray([_vec(p) for p in points], dtype=float)
    if len(pts) < 3:
        raise GeometryError(
            f"polygon_area 至少需要 3 个点，收到 {len(pts)} 个")
    centre = pts.mean(axis=0)
    d = pts - centre
    try:
        # 最小奇异值方向就是最佳拟合平面的法向
        _, sv, vt = np.linalg.svd(d, full_matrices=False)
    except np.linalg.LinAlgError:
        return 0.0
    if len(sv) < 2 or sv[1] <= abs(sv[0]) * 1e-9:
        return 0.0  # 全部共线 → 围不出面积
    u, v = vt[0], vt[1]
    ang = np.arctan2(d @ v, d @ u)
    xy = np.stack([d @ u, d @ v], axis=1)[np.argsort(ang)]
    x, y = xy[:, 0], xy[:, 1]
    acc = float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))
    return abs(acc) * 0.5


def polyhedron_volume(points) -> float:
    """用手选顶点算**凸包体积**（顶点顺序无关，凸包只对集合定义）。

    退化的点集（4 点共面、全部共线…）qhull 会直接拒绝 → 按 **0** 返回。
    这不是错误：用户选的点确实围不出体积，报 0 比报错更贴近事实。
    """
    pts = [_vec(p) for p in points]
    if len(pts) < 4:
        raise GeometryError(
            f"polyhedron_volume 至少需要 4 个点，收到 {len(pts)} 个")
    try:
        return float(ConvexHull(np.asarray(pts, dtype=float)).volume)
    except QhullError:
        return 0.0


def convex_hull_edges(points) -> list[list[int]]:
    """凸包的**棱**（返回顶点对，索引指向 `points`）。退化点集 → 空表。

    用途：把「服务端实际拿来算体积的那个凸包」原样画给用户看。

    ⚠️ 不要在客户端再算一遍凸包 —— 那就是两套真相，画出来的与算出来的
    可能不是同一个东西（用户：「现在测的是什么不清楚」）。
    ⚠️ Qhull 的面是**三角化**的：直接取 simplices 的边，立方体每个正方形面里
    会多出一条对角线（18 条棱、度数 4），画出来就是「一堆斜线」。所以先按支撑
    平面把三角片合并成面，再取每个面**只被一个三角片用到**的边 = 面的边界。
    """
    pts = [_vec(p) for p in points]
    if len(pts) < 4:
        return []
    try:
        hull = ConvexHull(np.asarray(pts, dtype=float))
    except QhullError:
        return []  # 共面 / 共线 / 重复点：围不出体，也就没有骨架
    # 支撑平面（单位法向 + 偏移）相同的三角片属于同一个面
    facets: dict = {}
    for simplex, plane in zip(hull.simplices, hull.equations):
        key = tuple(np.round(np.asarray(plane, dtype=float), 6))
        facets.setdefault(key, []).append([int(v) for v in simplex])
    edges = set()
    for triangles in facets.values():
        # ⚠️ 计数必须**按面**分：真棱会被相邻的两个面各用一次，全局计数会把它
        # 当成对角线一起丢掉（实测：全丢，棱数为 0）。
        local: dict = {}
        for a, b, c in triangles:
            for i, j in ((a, b), (b, c), (c, a)):
                key = (i, j) if i < j else (j, i)
                local[key] = local.get(key, 0) + 1
        # 同一个面的内部对角线被两个三角片各用一次；面的边界只用一次
        edges.update(key for key, n in local.items() if n == 1)
    return [[i, j] for i, j in sorted(edges)]


def outline_for(op: str, points) -> list[list[int]]:
    """某个 op 的**骨架**（顶点对索引）—— 「这个量到底在量哪块形状」。

    目前只有体积需要服务端给：它算的是凸包，棱必须与体积出自同一个凸包。
    面积 / 长度的骨架就是点按序连起来，客户端本来就有。

    ⚠️ 唯一实现：端点（`panel/server.py::measure_session`）和重算
    （`refresh_measurements`）都必须调它 —— 两边各写一份就是这次踩过的坑
    （响应里少个字段，前端就没得画）。
    """
    return convex_hull_edges(points) if op == "polyhedron_volume" else []


# ══════════════════ 共面判定（供 UI 引导用）══════════════════
# 口径：**第 4 个点起**，每点到「前三点所在平面」的距离与模型包络之比 < 3%，
# 一律在**模型世界单位**上算（与是否标定过无关）。
#
# ⚠️ 「距离 / 体积」量纲不成立（是 1/面积），所以包络取**等效边长** = 包围盒
#    体积的立方根 —— 这是唯一量纲正确的读法，也才对得上「相对模型大小」的直觉。
# ⚠️ 客户端 panel/index.html 有一份 JS 镜像（落点时要即时判定，等不了往返）：
#    改这个常量必须两边一起改，`test_web_client.py` 里有守卫盯着。
COPLANAR_TOLERANCE_RATIO = 0.03


def bbox_volume(points) -> float:
    """点集的轴对齐包围盒体积（模型单位）。空集 → 0。"""
    pts = [_vec(p) for p in points]
    if not pts:
        return 0.0
    vol = 1.0
    for axis in range(3):
        values = [p[axis] for p in pts]
        vol *= max(0.0, max(values) - min(values))
    return vol


def envelope_scale(points) -> float:
    """模型包络的特征长度 = 包围盒体积的**立方根**（与共面容差同量纲）。"""
    return bbox_volume(points) ** (1.0 / 3.0)


def coplanar_tolerance(envelope: float) -> float:
    """由包络特征长度算出共面容差（点到平面的距离阈值，模型单位）。"""
    return COPLANAR_TOLERANCE_RATIO * max(0.0, float(envelope))


def plane_distance(point, a, b, c) -> float:
    """点 ``point`` 到平面 ``(a, b, c)`` 的垂直距离。

    三点共线（平面未定义）时返回 0：此时「是否共面」无从谈起，不该反过来
    把用户的点判成错的。
    """
    p, va, vb, vc = _vec(point), _vec(a), _vec(b), _vec(c)
    nrm = _cross(_sub(vb, va), _sub(vc, va))
    nn = _norm(nrm)
    if nn <= 1e-12:
        return 0.0
    return abs(_dot(_sub(p, va), nrm)) / nn


def coplanar_distances(base, points) -> list:
    """``points`` 中每个点到 ``base`` 前三点所定平面的距离（模型单位）。"""
    if len(base) < 3:
        raise GeometryError(f"需要 3 个基准点，收到 {len(base)} 个")
    va, vb, vc = _vec(base[0]), _vec(base[1]), _vec(base[2])
    return [plane_distance(p, va, vb, vc) for p in points]


def apply_scale_factor(value: float, dim: int, scale: Optional[float]) -> float:
    """把模型坐标系的量换算到真实尺度：长度 ×s、面积 ×s²、体积 ×s³。

    ``scale is None``（未标定）时原样返回 —— 调用方负责把单位标成 `u` 系列。
    """
    if dim not in (1, 2, 3):
        raise GeometryError(f"量纲只支持 1/2/3，收到 {dim!r}")
    if scale is None:
        return float(value)
    return float(value) * float(scale) ** dim


def unit_for(dim: int, calibrated: bool) -> str:
    """量纲 + 是否已标定 → 单位字符串（`m` / `m²` / `m³` 或 `u` / `u²` / `u³`）。"""
    if dim not in _UNITS:
        raise GeometryError(f"量纲只支持 1/2/3，收到 {dim!r}")
    return _UNITS[dim][1 if calibrated else 0]


def compute(op: str, points: Sequence[Sequence[float]]) -> float:
    """按 ``op`` 取前 N 个点计算（模型单位，未换算尺度）。

    Raises:
        GeometryError: 未知 op、点数与 op 不匹配、坐标非法。
    """
    need, _dim = OPS[op] if op in OPS else (None, None)
    if need is None:
        raise GeometryError(f"未知测量类型: {op!r}")
    pts = list(points)
    if op in MIN_OPS:
        if len(pts) < need:
            raise GeometryError(f"{op} 至少需要 {need} 个点，收到 {len(pts)} 个")
    elif len(pts) != need:
        raise GeometryError(f"{op} 需要 {need} 个点，收到 {len(pts)} 个")
    if op in ("length", "segment_length"):
        return segment_length(pts[0], pts[1])
    if op == "triangle_area":
        return triangle_area(pts[0], pts[1], pts[2])
    if op in ("area", "parallelogram_area"):
        return parallelogram_area(pts[0], pts[1], pts[2])
    if op == "polygon_area":
        return polygon_area(pts)
    if op == "polyhedron_volume":
        return polyhedron_volume(pts)
    return parallelepiped_volume(pts[0], pts[1], pts[2], pts[3])


# ══════════════════ 标注元素解析 ══════════════════

def resolve_points(elements: dict, element_id: str, _seen: Optional[set] = None):
    """把一个标注元素展开成点坐标列表。

    - 点元素（`kind == "point"`，带 `points`）→ 自身的坐标；
    - 派生元素（带 `refs`）→ 沿引用递归展开（线段 = 2 点，以此类推）。

    Raises:
        GeometryError: 元素不存在、既无坐标也无引用、或引用成环。
    """
    seen = set() if _seen is None else _seen
    if element_id in seen:
        raise GeometryError(f"元素引用成环: {element_id}")
    element = elements.get(element_id)
    if element is None:
        raise GeometryError(f"元素不存在: {element_id}")

    refs = element.get("refs") or []
    if refs:
        out: list = []
        for ref in refs:
            out.extend(resolve_points(elements, ref, seen | {element_id}))
        return out

    points = element.get("points")
    if not points:
        raise GeometryError(f"元素既无 points 也无 refs: {element_id}")
    return [list(p) for p in points]


def resolve_measurement(elements: dict, element: dict) -> dict:
    """重算一个测量元素 →
    ``{"raw": 模型单位值, "dim": 量纲, "points": 坐标, "outline": 顶点对}``。

    `outline` 是「这个量到底在量哪块形状」的骨架，也由**算法本身**给：
    体积是凸包，棱必须由服务端算（见 `convex_hull_edges`）—— 客户端自己再算
    一遍就等于两套真相。多边形的骨架就是点按序连起来，客户端本来就有。

    Raises:
        GeometryError: 未知 op、缺 refs、引用坏了或点数不匹配。
    """
    op = element.get("op")
    refs = element.get("refs") or []
    if not op or not refs:
        raise GeometryError("测量元素缺 op 或 refs")
    points: list = []
    for ref in refs:
        points.extend(resolve_points(elements, ref))
    return {
        "raw": compute(op, points),
        "dim": OPS[op][1],
        "points": points,
        "outline": outline_for(op, points),
    }


def refresh_measurements(annotations: Optional[dict],
                         scale: Optional[float]) -> Optional[dict]:
    """把标注里所有 `kind == "measurement"` 元素的 `raw` / `value` / `unit` /
    `calibrated` **按当前几何与 scale 重算**。

    这是「改 scale 后重读同一标注，数值随之变化」的实现方式：值不存死，
    每次读取都由几何 + 当前 scale 推出。

    单个测量元素算不出来（引用坏了）不会抛错，而是就地打上
    ``"error"`` 字段并跳过 —— 一条坏标注不该让整个历史打不开。
    """
    if not isinstance(annotations, dict):
        return annotations
    elements = annotations.get("elements") or []
    if not isinstance(elements, list):
        return annotations

    by_id = {e.get("id"): e for e in elements if isinstance(e, dict) and e.get("id")}
    for element in elements:
        if not isinstance(element, dict) or element.get("kind") != "measurement":
            continue
        try:
            solved = resolve_measurement(by_id, element)
        except GeometryError as exc:
            element["error"] = str(exc)
            for key in ("raw", "value", "unit", "points", "outline"):
                element.pop(key, None)
            continue
        element.pop("error", None)
        raw, dim = solved["raw"], solved["dim"]
        element["raw"] = raw
        element["dim"] = dim
        element["points"] = solved["points"]
        element["outline"] = solved["outline"]
        element["value"] = apply_scale_factor(raw, dim, scale)
        element["unit"] = unit_for(dim, scale is not None)
        element["calibrated"] = scale is not None
    return annotations


def iter_measurements(annotations: Optional[dict]) -> Iterable[dict]:
    """遍历标注里的测量元素（只读）。"""
    if not isinstance(annotations, dict):
        return []
    return [e for e in (annotations.get("elements") or [])
            if isinstance(e, dict) and e.get("kind") == "measurement"]
