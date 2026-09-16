"""几何测量纯函数 + 标注元素解析（无 torch / Qt 依赖，可独立单测）。

**为什么要有这个模块**：面积 / 体积 / 长度必须有**唯一实现** —— 两端各算一遍
必然出现「显示的数值」与「服务器存的标注」两套真相。

定义（与 issue #28 一致，UI 需要写清楚避免误用）：

- 长度 `|AB|`
- 三角形面积 `½·|AB × AC|`
- 面积（元素为平行四边形，由 3 点确定）`|AB × AC|` —— **恰为三角形面积的 2 倍**
- 体积（平行六面体，由 4 点确定）`|det[AB, AC, AD]|` —— 与「长×宽×高」同值

尺度换算：长度 ×s、面积 ×s²、体积 ×s³；未标定（`scale is None`）时返回模型单位
`u` / `u²` / `u³`，由调用方在结果上标记「未标定」。

**数值重算而非存死值**：测量元素只存 `op` + `refs`（几何引用），
`raw`（模型单位值）与 `value`（换算后）都在读取时由几何**重算**。
这样重新标定 scale 后，所有已有测量会自动跟着变，不需要回写任何记录。
"""
from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

__all__ = [
    "GeometryError",
    "OPS",
    "required_points",
    "segment_length",
    "triangle_area",
    "parallelogram_area",
    "parallelepiped_volume",
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
}

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
    if len(pts) != need:
        raise GeometryError(f"{op} 需要 {need} 个点，收到 {len(pts)} 个")
    if op in ("length", "segment_length"):
        return segment_length(pts[0], pts[1])
    if op == "triangle_area":
        return triangle_area(pts[0], pts[1], pts[2])
    if op in ("area", "parallelogram_area"):
        return parallelogram_area(pts[0], pts[1], pts[2])
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
    """重算一个测量元素 → ``{"raw": 模型单位值, "dim": 量纲, "points": 坐标}``。

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
    return {"raw": compute(op, points), "dim": OPS[op][1], "points": points}


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
            for key in ("raw", "value", "unit", "points"):
                element.pop(key, None)
            continue
        element.pop("error", None)
        raw, dim = solved["raw"], solved["dim"]
        element["raw"] = raw
        element["dim"] = dim
        element["points"] = solved["points"]
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
