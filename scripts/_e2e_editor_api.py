"""临时端到端：在真实会话（anon:ply-e2e，146 万点）上跑测量闭环。

覆盖：#25 /snap 全量吸附、#27 annotations 幂等整体替换、
      #28 /measure 几何计算 + 读时重算、owner 隔离。
用完即删；结束会把标注恢复为空。
"""
import json
import math
import sys
import urllib.error
import urllib.request

sys.path.insert(0, ".")
from panel.snap_index import read_ply_xyz  # noqa: E402

BASE = "http://127.0.0.1:50865"
SID = "19ba1b3317ce4716"
PLY = f"data/sessions/{SID}.ply"
CLIENT = "ply-e2e"

FAIL = []


def call(method, path, body=None, client=CLIENT):
    url = f"{BASE}{path}"
    if client is not None:
        url += ("&" if "?" in path else "?") + f"client_id={client}"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


# ---- 1. 从真实 PLY 取 4 个顶点（截面上隔开取，避免四点共面退化）----
status, hist = call("GET", f"/api/history/{SID}")
check("GET /history 200", status == 200, f"status={status}")
print(f"  会话点数: {hist.get('num_points')}  views: {hist.get('num_views')}")
# 会话可能已经标定过（scale 被持久化）→ 断言要跟着 scale 走，不能假设未标定
SCALE = hist.get("scale")
DIM_UNIT = {
    1: ("u", "m"),
    2: ("u²", "m²"),
    3: ("u³", "m³"),
}
print(f"  会话 scale: {SCALE}")
call("PUT", f"/api/sessions/{SID}/annotations", {"version": 1, "elements": []})

xyz = read_ply_xyz(PLY)
print(f"  PLY 顶点数: {len(xyz)}")
check("PLY 顶点与元数据一致", len(xyz) == int(hist.get("num_points") or -1),
      f"ply={len(xyz)} meta={hist.get('num_points')}")

step = max(len(xyz) // 4, 1)
coords = [[float(v) for v in xyz[i * step]] for i in range(4)]
span = max(math.dist(coords[0], coords[i]) for i in range(1, 4))
print(f"  取 4 点，最大跨距≈{span:.4f}")
check("4 点互不重合", span > 1e-6, f"span={span}")

elements = [
    {"id": f"p{i + 1}", "kind": "point", "points": [coords[i]]}
    for i in range(len(coords))
]
status, resp = call("PUT", f"/api/sessions/{SID}/annotations",
                    {"version": 1, "elements": elements})
check("PUT annotations 200", status == 200, f"status={status} {str(resp)[:120]}")
saved = ((resp.get("annotations") or {}).get("elements")) or []
check("PUT 后 4 个点元素都在（未被无意丢弃）", len(saved) == 4, f"n={len(saved)}")

# ---- 2. 面积：3 点 → 平行四边形面积 ----
status, area = call("POST", f"/api/sessions/{SID}/measure",
                    {"op": "area", "element_ids": ["p1", "p2", "p3"]})
check("measure area 200", status == 200, str(area)[:120])
m = area.get("measurement", {}) if isinstance(area, dict) else {}
if m:
    a, b, c = coords[0], coords[1], coords[2]
    ab = [b[i] - a[i] for i in range(3)]
    ac = [c[i] - a[i] for i in range(3)]
    cross = [ab[1] * ac[2] - ab[2] * ac[1],
             ab[2] * ac[0] - ab[0] * ac[2],
             ab[0] * ac[1] - ab[1] * ac[0]]
    expect = math.sqrt(sum(v * v for v in cross))
    check("面积 = |AB×AC|（平行四边形）",
          abs(float(m.get("raw", -1)) - expect) < 1e-9,
          f"raw={m.get('raw')} expect={expect:.6g}")
    want_unit = DIM_UNIT[2][1 if SCALE is not None else 0]
    check("dim=2 单位随 scale 变化",
          m.get("dim") == 2 and m.get("unit") == want_unit
          and m.get("calibrated") == (SCALE is not None),
          f"unit={m.get('unit')} want={want_unit} calibrated={m.get('calibrated')}")

# ---- 3. 体积：4 点 → 平行六面体 ----
status, vol = call("POST", f"/api/sessions/{SID}/measure",
                   {"op": "volume", "element_ids": ["p1", "p2", "p3", "p4"]})
check("measure volume 200", status == 200, str(vol)[:120])
mv = vol.get("measurement", {}) if isinstance(vol, dict) else {}
if mv:
    a, b, c, d = coords[:4]
    ab = [b[i] - a[i] for i in range(3)]
    ac = [c[i] - a[i] for i in range(3)]
    ad = [d[i] - a[i] for i in range(3)]
    det = (ab[0] * (ac[1] * ad[2] - ac[2] * ad[1])
           - ab[1] * (ac[0] * ad[2] - ac[2] * ad[0])
           + ab[2] * (ac[0] * ad[1] - ac[1] * ad[0]))
    check("体积 = |det|", abs(float(mv.get("raw", -1)) - abs(det)) < 1e-9,
          f"raw={mv.get('raw')} expect={abs(det):.6g}")
    check("dim=3 单位随 scale 变化",
          mv.get("dim") == 3
          and mv.get("unit") == DIM_UNIT[3][1 if SCALE is not None else 0],
          f"unit={mv.get('unit')}")

# ---- 4. 点数不匹配要 400（不是静默算错）----
status, bad = call("POST", f"/api/sessions/{SID}/measure",
                   {"op": "area", "element_ids": ["p1", "p2", "p3", "p4"]})
check("面积给 4 点 → 400", status == 400, f"status={status} {str(bad)[:80]}")

# ---- 5. /snap：全量吸附 ----
near = [[coords[0][0] + 1e-4, coords[0][1], coords[0][2]]]
status, snap = call("POST", f"/api/sessions/{SID}/snap",
                    {"points": near, "max_distance": 0.1})
check("snap 200", status == 200, str(snap)[:120])
if status == 200:
    r = (snap.get("results") or [{}])[0]
    d = math.dist(near[0], r.get("point", near[0])) if r.get("hit") else None
    check("近距离查询命中", r.get("hit") is True, str(r)[:80])
    check("吸附距离 < 查询偏移", d is not None and d <= 1e-4 + 1e-9, f"d={d}")
    check("返回 elapsed_ms", snap.get("elapsed_ms") is not None,
          f"elapsed_ms={snap.get('elapsed_ms')}")

status, miss = call("POST", f"/api/sessions/{SID}/snap",
                    {"points": [[coords[0][0] + 10.0, coords[0][1], coords[0][2]]],
                     "max_distance": 1e-3})
far = (miss.get("results") or [{}])[0] if status == 200 else {}
check("远处查询未命中 out_of_range",
      status == 200 and far.get("hit") is False, f"status={status} {str(far)[:80]}")

status, empty = call("POST", f"/api/sessions/{SID}/snap", {"points": []})
check("空 points → 400", status == 400, f"status={status}")

# ---- 6. 读时重算：annotations 里应带有算好的测量值 ----
status, hist2 = call("GET", f"/api/history/{SID}")
els = ((hist2.get("annotations") or {}).get("elements")) or []
meas = [e for e in els if e.get("kind") == "measurement"]
check("读回的标注含 2 条测量", len(meas) == 2, f"n={len(meas)}")
check("读回的测量带 raw/value/unit",
      all(all(k in e for k in ("raw", "value", "unit")) for e in meas),
      str([{k: e.get(k) for k in ("op", "raw", "unit")} for e in meas])[:160])
check("读回值与落库值一致",
      all(abs(float(e["value"])
              - float(e["raw"]) * ((SCALE or 1.0) ** int(e["dim"]))) < 1e-9
          for e in meas),
      f"(value == raw × scale^dim, scale={SCALE})")

# ---- 7. owner 隔离：换个 client_id 一律 404 ----
status, _ = call("POST", f"/api/sessions/{SID}/snap",
                 {"points": near}, client="intruder")
check("陌生 client_id → snap 404", status == 404, f"status={status}")
status, _ = call("PUT", f"/api/sessions/{SID}/annotations",
                 {"version": 1, "elements": []}, client="intruder")
check("陌生 client_id → PUT 404", status == 404, f"status={status}")
status, _ = call("POST", f"/api/sessions/{SID}/measure",
                 {"op": "length", "element_ids": ["p1", "p2"]}, client="intruder")
check("陌生 client_id → measure 404", status == 404, f"status={status}")

# ---- 8. 幂等：重复 PUT 不产生重复数据 ----
call("PUT", f"/api/sessions/{SID}/annotations", {"version": 1, "elements": elements})
_, hist3 = call("GET", f"/api/history/{SID}")
n3 = len(((hist3.get("annotations") or {}).get("elements")) or [])
check("重复 PUT 幂等（元素数回到 4）", n3 == 4, f"n={n3}")

# ---- 清理 ----
status, _ = call("PUT", f"/api/sessions/{SID}/annotations",
                 {"version": 1, "elements": []})
_, hist4 = call("GET", f"/api/history/{SID}")
left = ((hist4.get("annotations") or {}).get("elements")) or []
check("清理后标注为空", status == 200 and left == [], f"left={left}")

print()
print(f"结果：{'全部通过' if not FAIL else '失败 ' + str(len(FAIL)) + ' 项：' + ', '.join(FAIL)}")
