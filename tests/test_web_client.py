"""网页客户端（panel/index.html）的结构性回归测试。

这里**不启动浏览器**，只做两件在 CI 里也能跑的事：
1. 用 node 交叉验证内联的纯 JS SHA-256（登录握手正确性的前提）；
2. 断言认证相关的关键接线仍在（防止后续重构把登录链路改断）。

真正的浏览器端到端验证见 CONTRIBUTING.md「端到端手测」一节。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INDEX = os.path.join(_ROOT, "panel", "index.html")
_SHA_SCRIPT = os.path.join(_ROOT, "tests", "tools", "web_sha256_check.js")
_POINTCLOUD_SCRIPT = os.path.join(_ROOT, "tests", "tools", "web_pointcloud_check.js")

if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from panel import auth_store  # noqa: E402


def _contains_text(haystack: str, needle: str) -> bool:
    """按**空白不敏感**判断一句文案在不在。

    为什么不直接 `needle in haystack`：编辑器/格式化进程会把长行折开
    （实测「200 点 = 1 分」被劈成两行，中间还插了缩进），直接比对会红，
    但页面上那句话完全正常 —— 这种假红比没有断言更糟，它会淹没真正的
    文案丢失。
    """
    squash = lambda s: re.sub(r"\s+", "", s)
    return squash(needle) in squash(haystack)


@pytest.fixture(scope="module")
def index_html() -> str:
    with open(_INDEX, encoding="utf-8") as fh:
        return fh.read()


def _run_node(script: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过网页端 JS 校验")
    proc = subprocess.run(
        [node, script], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60, check=False,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"


class TestMultiVertexMeasurement:
    """面积 / 体积改成「手选全部顶点 + 依次连线段」—— 下面盯住关键接线。

    为什么不在这里测数值：数值的唯一真相在服务端（`server/core/geometry.py`），
    这里只保证客户端**没有自己算**、也没把约束写丢。
    """

    def test_area_and_volume_use_the_multi_vertex_ops(self, index_html):
        assert 'op: "polygon_area"' in index_html
        assert 'op: "polyhedron_volume"' in index_html

    def test_coplanar_ratio_is_mirrored_from_the_server(self, index_html):
        """客户端要在落点那一刻判定（等不了往返），常量必须与服务端一致。

        任一边被改掉这条就会红 —— 这就是它存在的意义。
        """
        from server.core import geometry

        expected = (
            f"const COPLANAR_TOLERANCE_RATIO = {geometry.COPLANAR_TOLERANCE_RATIO}"
        )
        assert expected in index_html

    def test_ring_links_cannot_exceed_degree_two(self, index_html):
        """**只有面积**（围环）把度数卡在 2 —— 多边形的边界本来就是这样。

        现在建边路径收敛成一个 `addEdgeBetween`（手动收口、自动收口都走它），
        所以校验只需在这一处；但两条**自动连线**的路必须挂在「环工具」上，
        否则体积也会被拉成一堆线（见 `test_volume_does_not_constrain_vertex_degree`）。
        """
        assert "function degreeOfPoint" in index_html
        assert "function addEdgeBetween" in index_html
        # 「连线」工具本身仍不许连出分叉
        assert "degreeOfPoint(aId) >= 2 || degreeOfPoint(bId) >= 2" in index_html
        # 落点自动连线：上一个顶点接满两条就不再接
        assert _contains_text(
            index_html, "if (ring && prevId && degreeOfPoint(prevId) >= 2) {"
        )
        assert _contains_text(
            index_html,
            "if (ring && prevId) "
            "STATE.elements.push(makeEdgeElement(prevId, el.id));",
        )

    def test_volume_does_not_constrain_vertex_degree(self, index_html):
        """⚠️ 体积**不是**环：它量的是点集的凸包，三棱柱每顶点度数就是 3。

        用户实测：「体积引导不应该限制顶点度数」。度数 ≤ 2 + 必须收口是
        **面积**的语义（多边形的边界），照搬到体积上会把正确骨架判成不合法。
        """
        match = re.search(r"const RING_OPS = new Set\(\[(.*?)\]\)",
                          index_html, re.S)
        assert match, "没有 RING_OPS —— 度数 / 收口 / 自动连线就没有判据了"
        ops = re.findall(r'"([^"]+)"', match.group(1))
        assert ops == ["polygon_area"], (
            "只有面积是「围一个环」。体积（polyhedron_volume）量的是凸包，"
            "列进来就会把三棱柱（每顶点度数 3）判成不合法。"
        )
        # 「完成」时的收口 + 闭环校验同样只在环工具里
        body = _fn_body(index_html, "finishMultiPointMeasure")
        assert "isRingOp(def.op)" in body, "「完成」没有按 op 区分环 / 非环"
        assert _contains_text(
            body,
            "if (isRingOp(def.op)) { "
            "if (openVertices().length) await closeRing();",
        ), "closeRing 必须落在环工具判断之内，否则体积也会被强行收口"
        # 引导线（橡皮筋 + 收口虚线）也只对环有意义
        guide = _fn_body(index_html, "updateGuideLines")
        assert "ringToolActive()" in guide, "体积不该画「连回起点」的引导线"

    def test_finish_closes_the_ring_then_verifies_it(self, index_html):
        """面积点「完成」= **自动收口** + 校验闭环（体积不收，见上一条）。

        旧实现要求用户先精确点回起点、按钮才亮，再点「完成」——
        在稠密点云里很难点中（用户：「体验很难受」）。
        新契约：先 `closeRing()`，**然后仍然校验** `openVertices()` 为空才真正提交
        —— 收不回来时必须显式拒绝，不能静默放行。
        """
        body = _fn_body(index_html, "finishMultiPointMeasure")
        assert "closeRing()" in body, "「完成」没有自动收口"
        assert "openVertices()" in body, "自动收口之后没有再校验闭环"
        assert "收不了口" in body, "收不回来时必须显式拒绝"
        # 收口只把两个「只剩一条线」的端点接上，不能乱连
        close_body = _fn_body(index_html, "closeRing")
        assert "degreeOfPoint(el.id) === 1" in close_body
        assert "ends.length !== 2" in close_body

    def test_finish_is_available_once_enough_vertices(self, index_html):
        """顶点够了「完成」就得亮 —— 不再要求用户先手工收口。

        要求 `open === 0` 的话，用户得先猜着把环点上、按钮才亮，
        那正是「难受」的来源之一。
        """
        body = _fn_body(index_html, "toolStateFor")
        assert "canApply: active && got >= need," in body
        assert "open === 0" not in body

    def test_the_action_button_says_finish_for_these_tools(self, index_html):
        """多点工具的动作按钮是「完成」而不是「应用」。"""
        assert "isMultiPointTool(STATE.activeTool)" in index_html
        assert '? "完成"' in index_html

    def test_preview_computes_on_the_server_without_persisting(self, index_html):
        """实时数值走 preview：仍由服务端算，但不落库（否则每次落点都多一条标注）。"""
        assert "function previewMeasure" in index_html
        assert "preview: true" in index_html

    def test_edges_are_their_own_element_kind(self, index_html):
        """连线是独立元素（不是"长度"测量），否则元素视图会冒出一堆长度行。"""
        assert 'kind: "edge"' in index_html
        assert 'edge: "连线"' in index_html

    def test_area_rejects_off_plane_vertices(self, index_html):
        """面积是平面图形：偏出前三点平面的顶点直接拒绝落点。"""
        assert "面积要求所有顶点共面" in index_html

    def test_volume_is_not_blocked_when_coplanar(self, index_html):
        """体积共面时**不拦**（用户定的口径）：体积就是 0，但要提示一声。"""
        assert "这 4 个点共面，体积会是 0" in index_html

    def test_volume_draws_the_hull_the_server_measured(self, index_html):
        """体积画的是**服务端算的那个凸包的棱**（`el.outline`）。

        以前这里什么都不画：用户只看到自己连的线，而那条线跟凸包没关系
        （凸包对连线顺序不敏感、还会忽略内部点），于是「现在测的是什么不清楚」。
        """
        assert "const isHull = el.op === \"polyhedron_volume\"" in index_html
        body = _fn_body(index_html, "renderElements")
        assert "const edges = el.outline || [];" in body
        assert _contains_text(body, "for (const [i, j] of edges) {")
        assert _contains_text(body, "new THREE.LineSegments(geom, mat)")

    def test_client_does_not_compute_its_own_hull(self, index_html):
        """骨架不能前端自己再算一遍 —— 两套真相会画出与数值不符的东西。

        唯一实现：`server/core/geometry.py::convex_hull_edges`。
        """
        assert "ConvexHull" not in index_html, (
            "体积的凸包由服务端给（同一次计算里拿到的），前端不许再算一份"
        )

    def test_volume_hint_promises_no_ring(self, index_html):
        """体积的提示不能提「连线 / 虚线 / 收口」—— 它不连线也不收口。"""
        match = re.search(r"volume: \{(.*?)\n        \}", index_html, re.S)
        assert match, "找不到 volume 的工具定义"
        hint = match.group(1)
        for banned in ("虚线", "收口", "连一条线"):
            assert banned not in hint, f"体积不连线，提示里不该出现「{banned}」"
        assert "完成" in hint, "得说清怎么结束"


class TestElementRowContent:
    """右侧元素条目：每个事实只说一遍，控件看起来要像控件。

    用户报的两件事：
    1. 「上面显示两个坐标（一个高精度一个低精度）」—— 行尾 3 位、明细 4 位，
       同一个坐标出现两次且精度不同；
    2. 「元素种类被当成可点击控件被渲染，然而那只是一个名称」—— 因为
       `.element-kind` 被列进了「小控件」组，统一挂了 `--neu-raised-sm`。
    """

    def test_element_kind_is_not_styled_as_a_control(self, index_html):
        rule = _css_rule(index_html, ".element-kind")
        assert "box-shadow" not in rule, "种类是一个名称，不该有凸起阴影"
        assert "background" not in rule, "种类是一个名称，不该带底色"
        # 它曾在「小控件」组里（那一组统一挂 --neu-raised-sm）。
        # 取 `.tag-muted` 到下一个 `}` 之间的那段 —— 就是那组的选器表 + 声明。
        group = _css_group(index_html, ".tag-muted")
        assert ".element-kind" not in group, "又把它当小控件了"

    def test_element_row_shows_each_fact_once(self, index_html):
        body = _fn_body(index_html, "buildElementRow")
        # 行尾只给测量放值（测量确实有「一个值」）
        assert body.count('className = "element-value"') == 1
        assert "toFixed(3)" not in body, "行尾那个 3 位小数的坐标是重复的那一份"
        detail = _fn_body(index_html, "elementDetailText")
        assert "坐标" in detail and "质心" in detail
        assert "toFixed(4)" in detail
        # 点数在名字里已经写了「(2 点)」；量纲名与单位后缀重复
        assert "${pts.length} 个点" not in detail
        assert "DIM_NAME" not in detail

    def test_empty_detail_leaves_no_blank_row(self, index_html):
        body = _fn_body(index_html, "buildElementRow")
        assert _contains_text(
            body, "const detailText = elementDetailText(el); if (detailText) {"
        ), "明细为空时会白占一行高度"


class TestPicking:
    """选点必须在**屏幕空间**遍历全部渲染点。

    症状：在建模结果里点一个位置，选中的点跑到**触摸位置下方**。
    真因：用 `raycaster.intersectObject(cloud)` 取候选，而它是按**沿射线的深度**
    排序的 —— `slice(0, 64)` 只留下「圆柱里最靠相机的那批点」，它们在屏幕上
    整体偏下；而屏幕上真正离手指最近的点被截掉了。另外射线阈值是**世界距离**，
    在比环绕中心更近的点云上换算出的屏幕半径实测达 39px（远超 24px 容差），
    于是大片位置连候选都捞不到。
    """

    def test_walks_the_cloud_in_screen_space(self, index_html):
        assert "function screenNearestPoint" in index_html
        # 屏幕上定位：先换世界坐标再投影，而不是用相机空间的深度筛选
        assert "pickNearestPoint" in index_html

    def test_no_longer_truncates_candidates_by_depth(self, index_html):
        """不许回到「raycast 取候选 + slice 截断」那条路。

        只查**代码**：注释里提到这些名字是允许的（正是在解释为什么不用它）。
        """
        assert "new THREE.Raycaster()" not in index_html, "又建 raycaster 来拾取了"
        assert "PICK_RAY_PX" not in index_html
        assert ".intersectObject(" not in index_html.replace(
            "raycaster.intersectObject(cloud)", ""
        ), "又用 raycast 取候选了"

    def test_pick_radius_scales_with_the_viewport(self, index_html):
        """命中半径必须随视口缩放。

        固定 24px 在 269px 高的手机视口里占 9%，手指稍偏就会把落点放到明显
        错位的地方（用户描述的「点上面选下面」）。
        """
        assert "function pickRadiusPx" in index_html
        assert "PICK_RADIUS_RATIO" in index_html
        # 上下都要夹住：太小会处处点空，太大会选到偏离手指的点
        assert "PICK_RADIUS_MIN" in index_html
        assert "PICK_RADIUS_MAX" in index_html

    def test_fallback_radius_is_bounded(self, index_html):
        """兜底不能放太宽 —— 实测点云在屏幕上只占一小块（578x269 里只 118x155），
        放太宽就等于「你点轮廓外的背景，它把落点拉到物体边缘上」。"""
        assert "PICK_FALLBACK_FACTOR" in index_html
        assert "PICK_FALLBACK_FACTOR = 1.25" in index_html, (
            "兜底倍数被改大了：那又会出现「点上面选下面」"
        )
        assert "screenNearestPoint(mx, my, r * PICK_FALLBACK_FACTOR)" in index_html

    def test_pick_does_not_snap_to_the_full_cloud(self, index_html):
        """落点必须**就是拾取结果** —— 不再有「吸附到全量点云」那一步。

        为什么删掉它：`server._sample_for_render` 是**取索引的均匀抽样**
        （`np.linspace(...)`），不是重采样 —— 渲染点云本来就是全量点云的子集，
        屏幕上看到的每一个点都已经是真实点。那次吸附只会把落点换成
        **用户看不到的另一个点**（118 万 vs 6 万的抽样），屏幕上就落在别处，
        表现为「点了这里、落点在下面」。试过用漂移校验补救，也只能把偏差压到
        17px 量级（校验 5px + 拾取半径 12px），在 269px 高的视口里仍然看得出来。
        """
        assert "async function snapToFullCloud" not in index_html, (
            "吸附又回来了 —— 它会把落点换成用户看不到的点"
        )
        assert "snapToFullCloud(" not in index_html
        # 预览与落点必须走同一个函数、同一个位置
        assert "await onToolClick(picked);" in index_html
        assert "function pickNearestPoint" in index_html

    def test_prefers_screen_distance_over_camera_depth(self, index_html):
        """同半径内必须**屏幕距离优先**，不能按「离相机最近」取胜。

        透视投影下这两个不是一回事：离相机最近的点可能落在屏幕上偏好几个
        像素的地方。实测按相机深度取胜时平均偏移 13.11px，改成屏幕距离优先
        后降到 7.09px。
        """
        assert "bestRing" in index_html, "没有分环 —— 又回到按相机深度取胜了"
        assert "const ring = Math.floor(d)" in index_html
        assert "if (ring > bestRing) continue" in index_html


class TestElementCoordinateSpace:
    """元素坐标（原始）与屏幕投影（世界）**不能混用**。

    症状：选点落下去以后，marker 出现在触摸点**旁边/下方**（实测偏 25px），
    而悬停预览圆环却精准贴在手指上 —— 预览和落点走的是两条路，差别就是
    坐标系：

      · `hoverMarker` 直接挂在 scene 上，`pickNearestPoint` 给的是**世界坐标**，
        所以它永远正确；
      · 元素挂在 `STATE.elementGroup` 上，而 `renderElements` / `alignGridToGround`
        会给整个组设 `quaternion = STATE.modelQuat`（本机实测 6.13°），
        所以元素的**存储值必须是原始坐标**。

    此前 `selectCloudPointAt` 把世界坐标直接存进元素，渲染时又被正旋一次
    —— 转两次，落点整体偏出去。而 `pickElement` / `pickPendingVertex` / 框选
    反着错：拿原始坐标去 `projectToScreen`，少转一次，于是「点不中看得见的那个点」。

    这一组断言就是钉死这两条转换：**落点逆旋，投影正旋**。
    """

    def test_element_coordinates_are_raw_not_world(self, index_html):
        """拾取给的是世界坐标，落成元素前必须逆旋回原始坐标。"""
        assert "function worldPointToElement" in index_html
        # 选择工具落点：必须逆旋（这正是 5 轮没修好的那个 bug）
        assert "const p = worldPointToElement(picked);" in index_html, (
            "selectCloudPointAt 又把世界坐标直接存进元素了 —— 会转两次"
        )
        # 坐标轴变换就一处，避免各写各的
        assert "applyQuaternion(q.clone().invert())" in index_html

    def test_element_projections_are_rotated_before_projecting(self, index_html):
        """元素坐标投屏前必须正旋 —— 少了这一步，点选会整体打偏。"""
        assert "function elementWorldPoint" in index_html
        assert "function projectElementToScreen" in index_html
        # 三处「拿元素坐标问屏幕位置」的地方都要走它
        for site in ("pickElement", "pickPendingVertex"):
            assert site in index_html
        # 元素锚点不得再直接喂给只认世界坐标的 projectToScreen
        assert "projectToScreen(anchor)" not in index_html, (
            "pickElement/框选又拿原始坐标直接投影了"
        )
        assert "projectToScreen(v)" not in index_html.replace(
            "projectToScreen(v.toArray())", ""
        )

    def test_measurement_click_shares_the_same_rotation(self, index_html):
        """测量工具早就有逆旋（注释里写明了约定），别让它被删掉。"""
        assert "const _inv = (STATE.modelQuat || new THREE.Quaternion())" in index_html
        assert "applyQuaternion(_inv)" in index_html

    def test_box_select_applies_the_cloud_matrix(self, index_html):
        """点云几何是局部坐标，框选要先过 matrixWorld 再投影。"""
        assert "applyMatrix4(cloudMatrix)" in index_html

    def test_hover_marker_stays_in_world_space(self, index_html):
        """预览圆环挂在 scene 上、用世界坐标 —— 这是它对的原因，不要"统一"掉。"""
        assert "STATE.viewerScene.add(hoverMarker)" in index_html
        assert "hoverMarker.position.copy(p)" in index_html


class TestMultiPointEntryPoints:
    """多点工具的两条入口都必须把「工具参数」归一化。

    症状：选好顶点后点状态栏的「完成」，弹 400「未知测量类型: None」，
    测量根本没创建 —— 用起来就像「完成键点不动」。
    真因：按钮处理器传的是**工具名字符串**（`onToolButtonClick(STATE.activeTool)`），
    而 `finishMultiPointMeasure` 全程按**定义对象**用（`tool.op` / `tool.need` /
    `tool.label`），于是 `op` 是 undefined，服务端收到 None。
    """

    def test_tool_parameter_is_normalised_at_both_entry_points(self, index_html):
        assert "function toolDefOf" in index_html
        assert _contains_text(
            index_html, 'return typeof tool === "string" ? TOOL_DEFS[tool] : tool;'
        )
        # 两处（落点定稿 / 建测量）都要过它，不许直接读 tool.op
        assert _contains_text(index_html, "const def = toolDefOf(tool);")
        assert _contains_text(index_html, "op: def.op, element_ids: refs")

    def test_the_status_bar_button_path_passes_a_tool_name(self, index_html):
        """「完成」按钮走的是传名字那条路 —— 这就是上面那条归一存在的原因。"""
        assert "onToolButtonClick(STATE.activeTool)" in index_html

    def test_selected_points_are_adopted_as_vertices(self, index_html):
        """在元素列表里选好顶点、再点工具，「完成」必须马上可用。

        上一版多点工具只看「本次落下的点」，完全无视选中集 —— 于是选了 8 个点
        点体积，状态栏还写「至少要 4 个顶点」、按钮是灰的（用户实测）。
        """
        assert "async function adoptSelectionAsVertices" in index_html
        assert _contains_text(
            index_html, "if (isMultiPointTool(tool)) await adoptSelectionAsVertices(tool);"
        )
        # 面积采纳后要连线段（顺序 = 顶点顺序），够数就收口；体积只取点集
        assert _contains_text(index_html, "if (isRingOp(def.op)) {")
        assert _contains_text(index_html, "for (let i = 1; i < chain.length; i++) link(chain[i - 1], chain[i]);")

    def test_failed_measurement_keeps_the_pending_vertices(self, index_html):
        """服务端拒了就别清引导 —— 否则用户只剩一堆连线、没法接着改。"""
        assert _contains_text(
            index_html,
            "if (await createMeasurement(def, refs)) { STATE.pendingIds = [];",
        )

    def test_area_ring_order_is_derived_not_guessed(self, index_html):
        """面积曾经依赖顶点顺序，客户端负责排环序。

        现在**正确性已经收到服务端**（`polygon_area` 内部拟合平面 + 极角重排，
        与传入顺序无关 —— 见 `test_geometry.py::test_any_order_gives_the_same_answer`）。
        客户端这份排序保留下来，只是为了让**连线画得好看**（按几何环绕而不是
        元素创建顺序），不再是正确性的依赖。

        实测反例（立方体一个面的 4 个角，创建顺序恰好是 Z 字形／自交）：
        用 shoelace 直接算得 0，而「完成」照样是亮的 —— 用户会把 0 当结果。
        """
        assert "function orderRingVertices" in index_html
        assert _contains_text(index_html, "const ordered = orderRingVertices(picked);")
        assert _contains_text(index_html, "if (ordered) picked = ordered;")
        # 极角序的成立前提是「凸」—— 这行注释是给下一个改代码的人的警告
        assert _contains_text(
            index_html, "对**凸多边形**，绕质心的极角序就是唯一正确的环序"
        )

    def test_adopting_selection_checks_coplanarity(self, index_html):
        """两个入口的校验必须一致：手动落点会拦共面，勾选采纳也不能放行。

        否则勾 8 个立方体角去点「面积」，会被照单全收并给出一个无意义的数。
        """
        assert _contains_text(
            index_html, "面积要求所有顶点共面：有 ${off.length} 个点偏出前三点所在平面"
        )
        assert _contains_text(
            index_html, "return; // 不采纳、也不动数据"
        )


class TestStatusBarStaysShort:
    """状态栏只写「下一步做什么」和数值，不复述工具名与个数上限。

    用户原话：「状态栏一堆过多的说明」。原来切到体积时，状态栏同时挂着
    `TOOL_DEFS.volume.hint` 那整句操作说明、`opReq` 的进度、以及 preview 的
    「N 个顶点 · 还没收口」—— 同一件事说了三遍，按钮反而找不到。
    """

    def test_operation_manual_is_not_parked_in_the_status_bar(self, index_html):
        """那句说明属于帮助内容（帮助模式里能看到），不该常驻状态栏。"""
        assert _contains_text(
            index_html,
            'setToolHint(isMultiPointTool(def) ? "" : TOOL_DEFS[def].hint || "");',
        )

    def test_preview_hint_carries_only_the_number(self, index_html):
        # 只断到模板串为止：调用可能被格式化器拆行/加尾随逗号
        assert _contains_text(
            index_html,
            'setToolHint(`${formatMeasurement(m)}${m.calibrated ? "" : "（未标定）"}`',
        )

    def test_progress_hint_does_not_restate_the_tool_name(self, index_html):
        assert _contains_text(index_html, "if (got < tool.need) return `还差 ${tool.need - got} 个顶点`;")


class TestDesignTokens:
    """样式必须走 token —— 规矩写在 `docs/DESIGN.md`。

    这个面板曾经**圆角 19 种、字号 24 种、padding 44 种**，接近 100 个随手写的
    数值。"看起来不像正经产品"的根源在这里，不在配色 —— 所以用测试锁住：
    CSS 里出现裸的圆角/字号就直接红。
    """

    def _style_block(self, index_html: str) -> str:
        style = index_html.split("<style>")[1].split("</style>")[0]
        # 先剥掉 CSS 注释再断言。否则注释里写一句「旧做法是 padding-left: …」
        # 会让 `[^;]+` 从注释里一路吞到下一个分号，把后面声明里的裸 px 也算进来
        # → **假红**。假红比没有断言更糟：它会淹没真正的裸值。
        # 注释不是声明，剥掉不会漏检（被注释掉的代码本来就不生效）。
        return re.sub(r"/\*.*?\*/", "", style, flags=re.S)

    def test_no_raw_border_radius(self, index_html):
        style = self._style_block(index_html)
        raw = [
            v.strip()
            for v in re.findall(r"border-radius:\s*([^;]+);", style)
            if "var(" not in v and v.strip() not in ("50%", "0")
        ]
        assert raw == [], (
            "这些圆角没走 token（档位见 docs/DESIGN.md 第二节）：" f"{raw}"
        )

    def test_no_raw_font_size(self, index_html):
        style = self._style_block(index_html)
        raw = [
            v.strip()
            for v in re.findall(r"font-size:\s*([^;]+);", style)
            if "var(" not in v and v.strip() not in ("0", "1rem")
        ]
        assert raw == [], f"这些字号没走 token：{raw}"

    def test_no_raw_spacing(self, index_html):
        """padding / margin / gap 也必须走 --sp-* 阶梯。

        这三样原来散着 **44 种**取值 —— 间距是布局的节奏，节奏乱了再怎么调
        配色都救不回来。`0` / `auto` / `calc()` 是允许的。
        """
        style = self._style_block(index_html)
        prop = (r"(?:padding|margin)(?:-(?:top|right|bottom|left))?"
                r"|gap|row-gap|column-gap")
        raw = []
        for value in re.findall(rf"\b(?:{prop}):\s*([^;]+);", style):
            raw += [p for p in value.split() if re.fullmatch(r"\d+(?:\.\d+)?px", p)]
        assert raw == [], f"这些间距没走 token：{sorted(set(raw))}"

    def test_the_token_ladder_is_defined(self, index_html):
        for name in ("--r-xs", "--r-sm", "--r-md", "--r-lg", "--r-xl", "--r-full",
                     "--fs-xs", "--fs-sm", "--fs-base", "--fs-md", "--fs-lg",
                     "--fs-xl", "--fs-2xl", "--fs-3xl",
                     "--sp-1", "--sp-2", "--sp-3", "--sp-4", "--sp-5",
                     "--sp-6", "--sp-7", "--sp-8", "--sp-9"):
            assert f"{name}:" in index_html, f"缺 token {name}（docs/DESIGN.md 第二节）"

    def test_no_decorative_gradient_on_primary(self, index_html):
        """「青 → 紫」那类渐变被点名删过（「像蓝莓一样…ai 风格太浓」），别再引入。"""
        style = self._style_block(index_html)
        assert "--accent-gradient" not in style
        block = style.split(".btn-primary {")[1].split("}", 1)[0]
        assert "gradient" not in block, "主按钮又用渐变当底了（docs/DESIGN.md 第一节）"

    def test_keyboard_focus_is_visible(self, index_html):
        """键盘焦点必须有可见反馈。

        改动前是「两套都没有」：按钮用**浏览器默认焦点框**（颜色粗细与主题无关），
        输入框则 `outline: none` + 自己画的 border/glow。键盘用户在一堆图标按钮
        之间根本看不出焦点在哪。现在统一成 `:focus-visible`。
        """
        style = self._style_block(index_html)
        assert ":focus-visible" in style, "少了 :focus-visible —— 键盘用户看不到焦点"
        assert "outline: 2px solid var(--accent-cyan)" in style
        # 鼠标点击不留环（否则每点一下都有一圈）
        assert ":focus:not(:focus-visible)" in style

    def test_primary_button_is_not_a_slab_of_accent(self, index_html):
        """主按钮的底不能是主色 —— 高饱和色铺大面积就会刺眼。

        同一个地方踩了两次：先是「青 → 紫」渐变被嫌 ai 味重；删成纯色后**更糟**，
        `#22d3ee` 糊成一整块按钮底，用户原话：「这个青色太丑陋，还不如原来」。
        可见问题出在**面积**而不是色相 —— 同一支青做成 5px 指示点、图标、进度条
        都很好看（那些地方现在仍是 `--accent-cyan`）。

        所以规矩是：主色只上描边 / 文字 / 小面积，底走中性抬升面（`--fill-3`）。
        实测 `.view-task-btn` 区域平均饱和度从 0.86 → 0.37。
        """
        style = self._style_block(index_html)
        block = style.split(".btn-primary {")[1].split("}", 1)[0]
        assert "background: var(--accent-cyan)" not in block, (
            "主按钮又拿主色当整块底了 —— 大面积高饱和会刺眼（docs/DESIGN.md 第一节）"
        )
        assert "var(--accent-cyan)" in block, (
            "主按钮一点主色都没有了，主操作会认不出来（描边或文字至少留一个）"
        )


class TestInlineJavaScript:
    def test_sha256_matches_standard(self):
        """纯 JS SHA-256 必须与标准实现一致，否则登录永远失败。"""
        _run_node(_SHA_SCRIPT)

    def test_pointcloud_decode(self):
        """点云解码（嵌套/扁平 × 带不带 RGB）不能错位。"""
        _run_node(_POINTCLOUD_SCRIPT)

    def test_imports_three_js_once(self, index_html):
        assert index_html.count('import * as THREE from "three"') == 1


class TestAuthWiring:
    """登录链路的「接线」断言 —— 轻量但能挡住误删。"""

    def test_promo_page_replaces_the_account_page(self, index_html):
        """账号功能搬到官网了：面板里只留推广页 + 跳转链接。"""
        assert 'id="page-promo"' in index_html
        assert 'data-page="promo"' in index_html
        assert 'id="openPortalBtn"' in index_html
        assert ">去官网</button>" in index_html
        # 留档：老账号表单还在文件里，但界面已隐藏
        assert "auth-archive" in index_html
        assert 'id="loginForm"' in index_html

    def test_promo_page_is_pricing_only(self, index_html):
        """推广页只说官方服务的定价，不再播报“当前服务商 / 验证结果”。"""
        assert "<h3>Omni3D官方服务</h3>" in index_html
        assert '<p class="card-subtitle">定价</p>' in index_html
        assert 'id="promoIdentityLine"' not in index_html
        assert "当前服务商：" not in index_html
        # 计费方式要写在页面上
        assert _contains_text(index_html, "200 点 = 1 分")
        assert "$8.99" in index_html and "$18.99" in index_html

    def test_fetch_interceptor_is_installed(self, index_html):
        assert "window.fetch = async function" in index_html
        assert "X-Auth-Token" in index_html
        assert "isSameOrigin" in index_html
        # AR 桥是跨源，必须放行（绝不能被带上 token）
        assert "if (!isSameOrigin(rawUrl)) return nativeFetch(input, init);" in index_html

    def test_client_id_is_appended_to_same_origin_api(self, index_html):
        assert "function withClientId" in index_html
        assert "omni3d.client_id" in index_html
        assert "omni3d.token" in index_html

    def test_challenge_response_endpoints_used(self, index_html):
        for path in (
            "/api/auth/salt",
            "/api/auth/register",
            "/api/auth/challenge",
            "/api/auth/login",
            "/api/auth/logout",
            "/api/auth/me",
            "/api/auth/claim",
            "/api/auth/claim/preview",
        ):
            assert path in index_html, f"缺少认证接口调用：{path}"

    def test_history_page_uses_session_layer(self, index_html):
        """历史页必须走会话层（归属隔离 + 持久化），而不是内存任务表。"""
        assert 'fetch("/api/history?limit=20")' in index_html
        assert "/api/history/${taskId}?include_points=true" in index_html

    def test_plaintext_password_never_persisted(self, index_html):
        """只持久化凭据（令牌 / API Key），绝不能把密码/verifier 写进本地存储。"""
        assert "omni3d.password" not in index_html
        assert "omni3d.verifier" not in index_html
        assert "writeCred(serverId, cred)" in index_html

    def test_auth_rules_match_server(self, index_html):
        """网页端的账号规则常量必须与服务端一致，否则提示与实际不符。"""

        def _const(name: str) -> int:
            m = re.search(rf"\b{name}\s*=\s*(\d+)", index_html)
            assert m, f"未在 index.html 中找到常量 {name}"
            return int(m.group(1))

        assert _const("USERNAME_MIN") == auth_store.USERNAME_MIN
        assert _const("USERNAME_MAX") == auth_store.USERNAME_MAX
        assert _const("PASSWORD_MIN") == auth_store.PASSWORD_MIN


class TestQualityDefaults:
    """「精度优先」档位的网页端接线（#23）—— 防止悄悄退回 224 / 12 帧。"""

    def test_defaults_to_512_full_frame(self, index_html):
        """网页必须默认提交 512，且分辨率是**可切换的状态**而非硬编码。"""
        assert "resolution: 512," in index_html
        assert 'formData.append("resolution", String(STATE.resolution))' in index_html
        # 回归保护：曾经就是这里写死 224，导致测量全带上裁切误差
        assert 'formData.append("resolution", "224")' not in index_html

    def test_224_is_marked_preview_only(self, index_html):
        """224 必须在设置页可见地标注「仅预览」，并有告警样式。"""
        assert "224 · 仅预览" in index_html
        assert "会裁掉边缘" in index_html
        assert "radio-pill radio-warn" in index_html

    def test_frame_options_come_from_server_capabilities(self, index_html):
        """视角档位改由**服务商**给（显存约束），页面不再写死 8/12/16。

        背景：8GB 显存下 512px 的 16 帧会把显存顶到 95%、前向从 1.4s
        退化到 26s+ 直至卡死。所以「能选几个视角」是服务器的能力，
        不是客户端的偏好 —— 页面拿 `/api/models` 的 capabilities 重建按钮。
        """
        assert "frame_options" in index_html
        assert "STATE.caps =" in index_html
        assert "function frameOptions" in index_html
        assert "function renderFrameOptions" in index_html
        # 当前值不在门槛内时必须被校正（切到显存更小的服务商就会触发）
        assert "options.includes(STATE.frameCount)" in index_html
        # 兜底值不再是 16（本机 8GB 会顶满显存）
        assert "frameCount: 8," in index_html
        # 选中态由门槛决定，静态 HTML 里不再预设
        assert 'class="radio-pill selected" data-value="16"' not in index_html


class TestServerTargeting:
    """页面与 API 分处两个端口时的接线：默认地址、可配置的「本地」、同源回退。"""

    def test_reads_bootstrap_config(self, index_html):
        """页面必须问托管方「默认 API 在哪」，而不是写死某一个地址。"""
        assert '"/app-config.json"' in index_html
        assert "function loadServerConfig" in index_html
        assert "function applyServerConfig" in index_html
        # 第一次使用（注册 / 匿名）就靠这份默认值
        assert "DEFAULT_API_PORT" in index_html

    def test_same_origin_falls_back_to_relative_paths(self, index_html):
        """与页面同源时返回空串 → 继续走相对路径（不触发跨源）。"""
        assert "if (new URL(url).origin === window.location.origin) return \"\";" in index_html
        assert 'STATE.serverMode = STATE.serverUrl ? "remote" : "local";' in index_html

    def test_local_server_address_is_configurable(self, index_html):
        """内置「本地」是**可配置**的服务器，不是写死的同源。"""
        assert "omni3d.local_server" in index_html
        assert "function saveLocalServer" in index_html
        # 地址/端口不再被 builtin 禁用
        assert "host.disabled = builtin" not in index_html
        assert "port.disabled = builtin" not in index_html
        # 用户改过之后就不能再被引导配置覆盖
        assert "sv.auto = false" in index_html
        assert "sv.auto === false" in index_html

    def test_legacy_migration_only_without_saved_list(self, index_html):
        """旧版单条 server_url 只在从没写过列表时迁移，避免自造一台假服务器。"""
        assert "if (saved === null) {" in index_html


class TestSettingsTabs:
    """设置页分页（采集 / 测量 / 快捷键 / 服务 / 界面 / 行为）。"""

    def test_tabs_and_panels_exist(self, index_html):
        for name in ("capture", "measure", "keys", "service", "ui", "behavior"):
            assert f'data-settings-tab="{name}"' in index_html
            assert f'data-settings-panel="{name}"' in index_html
        assert "function setSettingsTab" in index_html

    def test_each_setting_lives_in_exactly_one_panel(self, index_html):
        """每张卡片只能出现在一个分页里，别重复（重复 = id 冲突）。"""
        for group in ("resGroup", "frameCountGroup", "arResGroup", "unitGroup"):
            assert index_html.count(f'id="{group}"') == 1
        # 采集页应该同时管分辨率 / 视角数量 / AR 分辨率
        cap = index_html.split('data-settings-panel="capture"')[1].split(
            "data-settings-panel="
        )[0]
        for group in ("resGroup", "frameCountGroup", "arResGroup"):
            assert group in cap

    def test_remembers_last_tab(self, index_html):
        assert '"omni3d.settings_tab"' in index_html

    def test_card_headings_have_no_group_prefix(self, index_html):
        """卡片标题不再写「分组 · 」前缀 —— 分页已经说明分组了。"""
        for prefix in ("重建 · ", "测量 · ", "连接 · ", "界面 · ", "AR 扫描 · "):
            assert f"<h3>{prefix}" not in index_html

    def test_turntable_toggle_lives_in_behavior(self, index_html):
        """转盘模式是「行为」，不是快捷键；恢复默认仍留在快捷键页。"""
        behavior = index_html.split('data-settings-panel="behavior"')[1]
        assert 'id="turntableToggle"' in behavior
        keys = index_html.split('data-settings-panel="keys"')[1].split(
            "data-settings-panel="
        )[0]
        assert 'id="turntableToggle"' not in keys
        assert 'id="hotkeyReset"' in keys

    def test_camera_key_labels_have_no_glyphs(self, index_html):
        """旋转键标签只留文字（↺ ↻ ⟲ ⟳ 这些符号看着像 emoji）。"""
        for glyph in ("↺", "↻", "⟲", "⟳"):
            assert glyph not in index_html


# 这些字符的粗细与形状随平台字体变，还会被当成 emoji —— 界面上不许拿它们当图标
BANNED_GLYPHS = "●○◈✕✓↑↓🗑📣🎥📷🗂☰■▭▱△▣╱✋🎨🧭🧹📏❓📖⬇↶↷⤾"


def _js_object_keys(html: str, const_name: str) -> set[str]:
    """取一个 JS 对象字面量的顶层键（`const X = { ... };`）。

    只按行首匹配，够这些小图标表用，不引 JS 解析器。
    """
    marker = f"const {const_name} = {{"
    assert marker in html, f"没有 {const_name}"
    block = html.split(marker)[1].split("\n      };")[0]
    return set(re.findall(r"""^\s*["']?([A-Za-z_][\w:.-]*)["']?\s*:""", block, re.M))


class TestUiIconsAreSvg:
    """图标只能有一个来源，而且只能是自绘 SVG。"""

    def test_no_glyph_is_assigned_as_button_text(self, index_html):
        """按钮文字不许用字形拼（`textContent = "● 录制"` 这种）。

        真迹是：图标和文字在同一个字符串里，于是**改文字就得把图标再抄一遍**，
        而且字形的粗细形状随平台字体变。现在图标走 `data-icon` 槽位、
        文字走 `.btn-text`，两者互不干扰（`setBtnText` 不会抹掉图标）。
        """
        bad = re.findall(rf'textContent\s*=\s*"[^"]*[{BANNED_GLYPHS}]', index_html)
        assert not bad, f"这些地方又用字形当按钮文字了：{bad}"
        bad_html = re.findall(rf">\s*[{BANNED_GLYPHS}]", index_html)
        assert not bad_html, f"HTML 里还有字形图标：{bad_html}"

    def test_ui_icon_map_has_no_orphans(self, index_html):
        """`UI_ICONS` 里画了却没人用的键，和用了却没有的键都要报出来。

        缺了的那种**不会报错** —— `paintUiIcons` 拿不到就静默跳过，槽位空着，
        界面上少一个图标却没人发现（侧栏「推广」就这么带着 📣 占位活了好几版）。
        """
        slots = set(re.findall(r'data-icon="([^"]+)"', index_html))
        assert slots, "一个 data-icon 槽位都没有？"
        # 有些图标不是走槽位、而是 JS 里直接写进元素的（上移/下移/圆点/停止），
        # 那些也算「有人用」，否则这里会把它们误报成孤儿。
        used = (
            slots
            | set(re.findall(r"UI_ICONS[.\[]\"?'?([A-Za-z_]\w*)", index_html))
            | set(re.findall(r'setBtnIcon\([^,]+,\s*"([^"]+)"', index_html))
        )
        keys = _js_object_keys(index_html, "UI_ICONS")
        missing = sorted(slots - keys)
        assert not missing, f"data-icon 用了 UI_ICONS 里没有的键：{missing}"
        unused = sorted(keys - used)
        assert not unused, f"UI_ICONS 里这些键没人用：{unused}"

    def test_every_nav_page_has_an_icon(self, index_html):
        """侧栏每一页都要有 `NAV_ICONS` 条目（同样是「静默不动」的坑）。"""
        pages = set(re.findall(r'data-page="([^"]+)"', index_html))
        assert pages
        missing = sorted(pages - _js_object_keys(index_html, "NAV_ICONS"))
        assert not missing, f"这些导航页没有图标：{missing}"

    def test_every_toolbar_button_has_an_icon(self, index_html):
        """工具栏每个按钮都要有 `TOOLBAR_ICONS` 条目。

        只认 `<button>` 上的 id —— 工具栏里还有 `#viewGroup` 这类容器 div。
        """
        toolbar = index_html.split('id="editorToolbar"')[1].split("editor-body")[0]
        wanted = set()
        for tag in re.findall(r"<button\b[^>]*>", toolbar):
            m = re.search(r'data-tool="([^"]+)"', tag)
            if m:
                wanted.add(f"tool:{m.group(1)}")
                continue
            m = re.search(r'id="([^"]+)"', tag)
            if m:
                wanted.add(m.group(1))
        assert wanted, "工具栏里没找到按钮？"
        missing = sorted(wanted - _js_object_keys(index_html, "TOOLBAR_ICONS"))
        assert not missing, f"这些工具栏按钮没有图标：{missing}"

    def test_icon_maps_are_declared_in_dependency_order(self, index_html):
        """图标表的**声明顺序**必须满足依赖关系。

        `const` 没有变量提升：后声明的表读先声明的表才安全。
        我一度把 `UI_ICONS` 插在 `NAV_ICONS` 之后、`META_ICONS` 之前，
        而它写着 `scan: META_ICONS.ar` → 模块求值时抛 `ReferenceError`，
        **整个初始化中断**：图标全空、设置页 tab 也点不动。
        `node --check` / `scripts/_check_js.py` 只查语法，查不出这个。
        """
        order = ("_SVG_OPEN", "TOOLBAR_ICONS", "NAV_ICONS", "META_ICONS", "UI_ICONS")
        pos = []
        for name in order:
            i = index_html.find(f"const {name} =")
            assert i >= 0, f"没有 const {name}"
            pos.append(i)
        assert pos == sorted(pos), (
            "图标表的声明顺序反了（后者引用了前者，但排在了前面）："
            + ", ".join(f"{n}@{p}" for n, p in zip(order, pos))
        )


THEME_NAMES = ("deep", "slate", "violet", "amber", "light")


def _theme_block(html: str, name: str) -> str:
    """取一个主题块的声明部分。

    `deep` 是默认主题 —— 它就是 `:root` 本身（不写 data-theme 属性）。
    """
    if name == "deep":
        m = re.search(r":root\s*\{", html)
        assert m, "没有 :root 块"
        return html[m.end() :].split("}")[0]
    marker = f':root[data-theme="{name}"]'
    assert marker in html, f"没有 {name} 主题块"
    return html.split(marker)[1].split("}")[0]


class TestThemes:
    """主题：只覆盖 CSS 变量，能在设置里切换并记住。"""

    _TOKENS = (
        # 新拟物的三色台面。以前这里要求 `--bg-deep` / `--bg-card` —— 那是旧契约
        # （靠"一层比一层亮的底色"分层）。现在 `:root` 里所有 `--bg-*` 都等于
        # `var(--neu-base)`，主题再写它们只是重复定义，真正必须覆盖的是这三色。
        "--neu-base",
        "--neu-light",
        "--neu-dark",
        "--fill-1",
        "--accent-cyan",
        "--star-opacity",
        "--viewer-bg",
    )

    @pytest.mark.parametrize("name", THEME_NAMES)
    def test_each_theme_covers_all_key_tokens(self, index_html, name: str):
        """漏覆盖 token 的后果是「切过去之后某处还是上个主题的颜色」。"""
        block = _theme_block(index_html, name)
        missing = [t for t in self._TOKENS if t not in block]
        assert not missing, f"{name} 缺少 {missing}"

    @pytest.mark.parametrize("name", THEME_NAMES)
    def test_every_theme_declares_its_own_surface_colors(self, index_html, name: str):
        """台面三色必须是**字面色值**，而且彼此不同。

        用户实测过漏掉它们的后果：切到浅色时 `--neu-light` / `--neu-dark`
        还是 `:root` 的深色值 → 白底上冒出两道黑阴影，原话「阴影问题巨大」。
        所以这里不满足于"有定义"，而是断言是具体的十六进制色；
        并且三色必须互不相同 —— 同色就等于两道阴影糊成一块，等于没做。
        """
        block = _theme_block(index_html, name)
        surface = {}
        for tok in ("--neu-base", "--neu-light", "--neu-dark"):
            m = re.search(rf"{re.escape(tok)}:\s*([^;]+);", block)
            assert m, f"{name} 没定义 {tok}"
            value = m.group(1).strip()
            assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), (
                f"{name} 的 {tok} 必须是字面色值，实际是 {value!r}"
                "（写成 var() 兜底就会静默退回别的主题的色）"
            )
            surface[tok] = value.lower()
        assert (
            surface["--neu-light"] != surface["--neu-base"]
            and surface["--neu-dark"] != surface["--neu-base"]
        ), f"{name}: 亮影/暗影与台面同色 → 阴影看不出来（{surface}）"
        assert surface["--neu-light"] != surface["--neu-dark"], (
            f"{name}: 亮影与暗影同色（{surface}）"
        )

    @pytest.mark.parametrize("name", THEME_NAMES)
    def test_every_theme_has_opaque_fills(self, index_html, name: str):
        """`--fill-*` 必须**不透明**。

        新拟物靠「同一个台面的微差」表达 hover / 选中 / 按下；半透明填充会
        透出台面，深浅差被吃掉，于是这三种状态全都看不出来。
        `light` 主题原来正是 4 个 `rgba(...)`，这条就是防它回退。
        """
        block = _theme_block(index_html, name)
        for tok in ("--fill-1", "--fill-2", "--fill-3", "--fill-4"):
            m = re.search(rf"{re.escape(tok)}:\s*([^;]+);", block)
            assert m, f"{name} 没定义 {tok}"
            value = m.group(1).strip()
            assert "rgba(" not in value and "transparent" not in value, (
                f"{name} 的 {tok} 是半透明的（{value}）—— 新拟物下看不出深浅"
            )

    def test_every_var_reference_is_defined(self, index_html):
        """`var(--x)` 引用的 token 必须在主题块里有定义。

        方向与 `test_no_orphan_tokens` 相反，但同样致命：**引用一个不存在的
        token，整条声明会被判为无效**（computed value 退成 initial/inherited）。
        实测过：写了 `box-shadow: var(--neu-inset-soft)`，而 `--neu-inset-soft`
        那句定义没落进 `:root` → 输入框的阴影整个变成 `none`。
        页面上看不出「少了个 token」，只会觉得「这里怎么平平的」。
        带兜底值的 `var(--x, fallback)` 也要查 —— 兜底值正是把这种错误藏起来的东西。
        """
        # ⚠️ 先剥掉注释再扫：注释里出现的 `var(--x)`（写说明时很容易抄一个进去）
        # 不是引用，会把守卫弄成假红 —— 与 `_style_block` 踩过的那个坑同一个。
        src = re.sub(r"/\*.*?\*/", "", index_html, flags=re.S)
        defined: set[str] = set()
        for name in THEME_NAMES:
            defined |= set(re.findall(r"(--[\w-]+)\s*:", _theme_block(src, name)))
        used = set(re.findall(r"var\(\s*(--[\w-]+)", src))
        missing = sorted(used - defined)
        assert not missing, f"这些 token 被引用但没定义：{missing}"

    def test_choices_are_in_settings_and_include_default(self, index_html):
        for name in ("deep", "slate", "violet", "amber"):
            assert f'data-theme-pick="{name}"' in index_html
        assert 'id="themeGroup"' in index_html

    def test_theme_is_persisted_and_applied(self, index_html):
        assert "omni3d.theme" in index_html
        assert "function applyTheme" in index_html
        assert "documentElement.setAttribute(\"data-theme\"" in index_html
        assert "function initTheme" in index_html

    def test_viewer_bg_follows_the_token_not_hardcoded(self, index_html):
        """three.js 的 background / clearColor 是**值**不是 CSS 变量，
        写死的话换主题后 3D 视口会留着一块上个主题的底色。"""
        assert 'new THREE.Color("#080a12")' not in index_html
        assert 'setClearColor("#080a12")' not in index_html
        assert "viewerBgColor()" in index_html
        assert "function syncViewerBackground" in index_html

    def test_light_theme_keeps_the_viewer_dark(self, index_html):
        """浅色主题下 3D 视口**必须**保持深色。

        点云的「按高度着色」是蓝→白渐变，放在浅底上基本看不见；
        浅色的 3D 工具（Figma / Blender）也都是让视口保持深色。
        """
        import re

        block = index_html.split(':root[data-theme="light"]')[1].split("}")[0]
        m = re.search(r"--viewer-bg:\s*#([0-9a-fA-F]{6})", block)
        assert m, "light 主题没写 --viewer-bg"
        h = m.group(1)
        r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
        assert (r + g + b) / 3 < 90, f"浅色主题的视口底色太亮（{h}），点云会看不清"

    def test_selected_states_do_not_use_hardcoded_white_text(self, index_html):
        """选中态曾经写死 `color: #fff` —— 浅色主题下白字落在浅底上直接隐形。"""
        for sel in (".sidebar-nav button.active {", ".radio-pill.selected {"):
            block = index_html.split(sel)[1].split("}")[0]
            assert "color: #fff" not in block, f"{sel} 又用写死的白字了"

    @pytest.mark.parametrize("name", ["deep", "slate", "violet", "amber", "light"])
    def test_every_theme_defines_accent_derivatives(self, index_html, name: str):
        """青色的半透明派生（描边/发光/选中底）漏掉的话，会退回 :root 的青色，
        与主题主色不搭 —— 比如暖琥珀主题里冒出一道青光。"""
        if name == "deep":
            block = index_html.split(":root {")[1].split("}")[0]
        else:
            block = index_html.split(f':root[data-theme="{name}"]')[1].split("}")[0]
        missing = [
            t
            for t in ("--accent-faint", "--accent-wash", "--accent-glow", "--accent-edge")
            if t not in block
        ]
        assert not missing, f"{name} 缺少 {missing}"


    def test_no_orphan_tokens(self, index_html):
        """定义了却没人用的 token = 换主题时那一处不动。

        侧栏就这么坏过：`--bg-sidebar` 在 5 个主题里都定义好了，但 `.sidebar`
        的 `background` 写死 `#0c0e18`（移动端媒体查询里还有第二处），
        于是浅色主题下侧栏依然是深色 —— 光看代码很难发现。
        """
        import re

        style = index_html.split("<style>")[1].split("</style>")[0]
        root = style.split(":root {")[1].split("}")[0]
        defs = re.findall(r"(--[a-z0-9-]+)\s*:", root)
        allowed = {
            # 给 JS 读的（getComputedStyle），不经过 var()
            "--viewer-bg",
            "--star-opacity",
            "--logo-filter",
            # 主题体系成型前就存在的预留变量（本轮不动它们）
            "--accent-purple",
            "--radius-xl",
            "--shadow-glow",
        }
        unused = [d for d in defs if f"var({d})" not in style and d not in allowed]
        assert not unused, f"这些 token 定义了却没人用：{unused}"

    def test_theme_blocks_have_no_stray_tokens(self, index_html):
        """主题块里多定义了 `:root` 没有的变量 = 只会在那一个主题里凭空出现，
        基本是删变量时漏删的残渣。"""
        import re

        style = index_html.split("<style>")[1].split("</style>")[0]
        root = style.split(":root {")[1].split("}")[0]
        base = set(re.findall(r"(--[a-z0-9-]+)\s*:", root))
        for name in ("slate", "violet", "amber", "light"):
            blk = style.split(f':root[data-theme="{name}"]')[1].split("}", 1)[0]
            extra = [v for v in re.findall(r"(--[a-z0-9-]+)\s*:", blk) if v not in base]
            assert not extra, f"{name} 多定义了 {extra}"

    def test_sidebar_background_follows_the_token(self, index_html):
        """侧栏背景必须走变量 —— 写死的话浅色主题下侧栏还是深色。

        ⚠️ 有**两处**：基础规则 + 移动端媒体查询里各一份；只改一处的话，
        在窄屏（含手机 App 的 WebView）上依然是深色。
        """
        style = index_html.split("<style>")[1].split("</style>")[0]
        blocks = style.split(".sidebar {")[1:]
        assert len(blocks) >= 2, "只找到一处 .sidebar 规则，媒体查询那份丢了？"
        for blk in blocks:
            body = blk.split("}", 1)[0]
            assert "background: var(--bg-sidebar)" in body, (
                "某处 .sidebar 的 background 没走变量：" + body.strip()[:100]
            )


class TestSidebarToggleButton:
    """窄屏的「展开键」不许压在侧栏上。

    用户原话：「侧栏展开键会挡住侧栏」。汉堡按钮是 `position: fixed; left: 14px`
    + 宽 40px → **右边缘在 x=54**，而侧栏宽 236px。旧做法是给 `.sidebar-logo`
    加 `padding-left: var(--sp-9)`（32px）—— **不够**，只让出 32px，仍叠 22px，
    实测两个矩形确实相交（汉堡与品牌标叠着画）。
    现在改成：侧栏展开时整体**移出侧栏**（于是它自然成了关闭键）。
    """

    def _style_block(self, index_html: str) -> str:
        style = index_html.split("<style>")[1].split("</style>")[0]
        return re.sub(r"/\*.*?\*/", "", style, flags=re.S)

    def test_open_sidebar_moves_the_toggle_out_of_the_way(self, index_html):
        style = self._style_block(index_html)
        assert "body.sidebar-open .hamburger" in style, (
            "侧栏展开时没有把切换键移开 —— 它会继续压在侧栏的品牌标上"
        )
        blk = style.split("body.sidebar-open .hamburger {")[1].split("}", 1)[0]
        assert "var(--sidebar-width)" in blk, (
            "移出的距离必须跟着 --sidebar-width：侧栏可拖宽（最大 460），"
            "写死像素会在拖宽后再次重叠"
        )

    def test_the_toggle_class_is_toggled_on_body(self, index_html):
        """切换键在 `.app-layout` **外面**，兄弟选择器够不着 → 只能挂 body。

        少切任一侧，按钮就会留在错误的位置（展开后压在侧栏上，或收起后飘在中间）。
        """
        for fn, verb in (("openSidebar", "add"), ("closeSidebar", "remove")):
            body = index_html.split(f"function {fn}() {{")[1].split("}", 1)[0]
            assert f'document.body.classList.{verb}("sidebar-open")' in body, (
                f"{fn} 没同步 body.sidebar-open —— 按钮会留在错的位置"
            )

    def test_the_old_padding_patch_is_gone(self, index_html):
        """那条 `padding-left: var(--sp-9)` 补丁必须删掉。

        它只让出 32px 而按钮到 x=54，既解决不了问题、还会让人误以为已经处理过。
        """
        style = self._style_block(index_html)
        assert ".sidebar.open .sidebar-logo" not in style, (
            "旧的 padding 补丁还在（只让出 32px，不够；已被 body.sidebar-open 取代）"
        )


class TestMetaInfoBarIcons:
    """元信息条的图标必须是自绘 SVG，而且只能出现一次。

    用户指着一个 span 说「一个图标」、指着另一个说「一个重复的非svg图标」：
    当时 HTML 里写死 `<span class="meta-icon">🎬</span>`，而 9 个调用方又各自
    在文字前面再拼一个 emoji（🎬/🖼/📷/🔴）—— 同一个图标画两遍，
    字形还随平台字体变。

    现在：图标只由 `META_ICONS` 按 kind 一处提供，文字走 `textContent`
    （顺带避免拼接 HTML —— 文件名里可能带 `<`）。
    """

    _GLYPHS = "🎬🖼📷🔴✓"

    def _calls(self, index_html: str) -> list:
        """每个调用的实参（截到它自己的 `);`）。跳过函数定义那一处。

        用 `);` 当终止符是安全的：实参里的 `${formatBytes(...)}` 只会出现 `)}`，
        不会出现 `);`。
        """
        out = []
        for m in re.finditer(r"showMetaInfo\(", index_html):
            if index_html[: m.start()].rstrip().endswith("function"):
                continue
            out.append(index_html[m.end():].split(");", 1)[0])
        return out

    def test_the_icon_slot_is_filled_by_js_not_by_markup(self, index_html):
        """HTML 里不能写死字形 —— 图标由 JS 按 kind 填。"""
        assert '<span class="meta-icon" id="metaIcon"></span>' in index_html

    def test_every_call_passes_a_kind_and_no_glyph(self, index_html):
        calls = self._calls(index_html)
        assert len(calls) >= 7, f"只找到 {len(calls)} 处 showMetaInfo 调用"
        for args in calls:
            # 必须是 **kind 名**（ASCII 小写标识符），不是随便一个字符串 ——
            # 否则 `showMetaInfo("录制中...")` 这种「退回拼字符串」也能蒙混过关。
            assert re.match(r'"[a-z][a-z0-9]*"', args.lstrip()), (
                f"第一个参数必须是 kind（如 \"video\"）：{args[:60]!r}"
            )
            bad = [c for c in self._GLYPHS if c in args]
            assert not bad, f"文字里又拼了字形图标 {bad}：{args[:80]!r}"

    def test_every_kind_has_an_svg_icon(self, index_html):
        kinds = set(re.findall(r'showMetaInfo\(\s*"(\w+)"', index_html))
        assert kinds, "没找到任何 kind"
        defined = index_html.split("const META_ICONS = {")[1].split("};", 1)[0]
        assert "${_SVG_OPEN}" in defined, (
            "META_ICONS 里的图标不是自绘 SVG（应复用 _SVG_OPEN，纯描边 + currentColor）"
        )
        missing = [k for k in sorted(kinds) if f"{k}:" not in defined]
        assert not missing, f"META_ICONS 缺这些 kind：{missing}"


class TestGizmoRingsFollowTheCamera:
    """万向轴（三环）：朝向跟随相机姿态；抓某条环拖动只绕那条世界轴。

    ⚠️ 这块被反复改过（方向键 → 固定姿态 → 每条环独立绕轴），最后用户明确要求
    「重置成最开始的形式」，也就是 `group.quaternion = camera.quaternion⁻¹`。
    别再自作主张改它的旋转模型。
    """

    def _gizmo_block(self, index_html: str) -> str:
        assert "万向轴（三环 / 方向棱镜）" in index_html, "万向轴实现段不见了"
        return index_html.split("万向轴（三环 / 方向棱镜）")[1].split(
            "\n      // ===================="
        )[0]

    def test_orientation_follows_the_camera(self, index_html):
        block = self._gizmo_block(index_html)
        sync = block.split("function syncGizmo()")[1].split("\n      }")[0]
        assert "gizmo.group.quaternion.copy" in sync, "朝向应跟随相机"
        assert "viewerCamera.quaternion" in sync
        assert ".invert()" in sync, "要用相机姿态的逆"

    def test_picking_and_dragging_use_the_group_orientation(self, index_html):
        """命中检测与拖动切向都必须经过 group 的姿态，否则环看得见却抓不准。"""
        block = self._gizmo_block(index_html)
        n = block.count("gizmo.group.quaternion")
        assert n >= 3, f"拾取/拖动里少了 group 姿态（只出现 {n} 次）"


class TestGizmoHUDMustStayVisible:
    """万向轴覆盖层不许把它自己盖住的环糊掉。

    实测：`.gizmo-pad` 上曾经有 `background: rgba(9,11,19,.5)` + `backdrop-filter:
    blur(6px)`，而环是画在**下面那张 canvas** 上（scissor 小视口）的，
    那层半透明模糊正好糊在环上。同一帧的像素统计：彩色像素 4498 -> **0**、
    边缘能量 4.86 -> 0.52，也就是环被彻底抹掉。
    """

    def _pad_block(self, index_html: str) -> str:
        style = index_html.split("<style>")[1].split("</style>")[0]
        assert ".gizmo-pad {" in style, "找不到 .gizmo-pad 规则"
        return style.split(".gizmo-pad {")[1].split("}", 1)[0]

    def test_pad_does_not_obscure_the_rings(self, index_html):
        blk = self._pad_block(index_html)
        assert "backdrop-filter" not in blk, ".gizmo-pad 又加模糊了 —— 会把环糊没"
        assert "background: transparent" in blk, ".gizmo-pad 不该有底色"
        assert "box-shadow" not in blk, ".gizmo-pad 不该有阴影"


class TestToolGroupTools:
    """工具组：可见性 / 顺序 / 默认不可见的三角形面积。"""

    def test_triangle_measured_as_its_own_op(self, index_html):
        """三角形面积 = 独立工具，走服务端已有的 `triangle_area`。"""
        assert "triangleArea:" in index_html
        assert '"triangle_area"' in index_html
        # 元素/数值的展示早已支持它，别再另造一套
        assert "三角形面积" in index_html

    def test_triangle_is_hidden_by_default(self, index_html):
        """默认只留一个三点流程（平行四边形），三角形在设置里开。"""
        assert "TOOL_DEFAULT_VISIBLE" in index_html
        assert "triangleArea: false" in index_html
        assert "function toolDefaultVisible" in index_html
        assert 'data-tool="triangleArea"' in index_html

    def test_hidden_buttons_actually_hide(self, index_html):
        """⚠️ `[hidden]` 会被 `.tool-btn{display:flex}` 盖掉，必须显式兜底。"""
        assert ".tool-btn[hidden]" in index_html

    def test_layout_rows_show_icons(self, index_html):
        """光看名字分不清平行四边形 / 三角形，配置项里带图标。"""
        assert "tl-icon" in index_html

    def test_triangle_has_a_shortcut_row(self, index_html):
        assert 'id: "tool:triangleArea"' in index_html


class TestTooltipsCarryNoDashTail:
    """悬浮提示只显示名称，不许再拼「名称——说明」。

    ⚠️ 这个 `——` **不在 HTML 源码里**，是 `paintToolbarIcons()` 运行时用
    `` `${name}——${btn.dataset.help}` `` 拼出来的。所以"在源文件里 grep `——`"
    会得到**假阴性** —— 实测踩过：据此还错误地告诉过用户"全文件只剩 1 处"。
    要守的是**拼接模式**，不是某个字面实例。
    """

    def test_toolbar_does_not_concatenate_a_dash(self, index_html):
        assert "${name}——" not in index_html, (
            "工具栏 title 又拼破折号了：悬浮会显示成"
            "「重置视角——重置视角：把点云重新放到画面中央并铺满。」，"
            "名称还会重复一遍。只显示名称就好。"
        )

    def test_toolbar_title_is_the_bare_name(self, index_html):
        assert "btn.title = name;" in index_html, (
            "工具栏按钮的 title 应当就是名称本身。"
        )

    def test_fusion_tooltip_has_no_dash_tail(self, index_html):
        assert "—— 共" not in index_html, (
            "华为点云融合的 title 又拼了「—— 共 N 个 AR 点」。"
        )


class TestBehaviorSettings:
    def test_box_select_rotation_toggle(self, index_html):
        """行为：禁用「框选时旋转」（默认禁），并真的接进相机拖拽分支。"""
        assert 'id="boxNoRotateToggle"' in index_html
        assert "function boxNoRotate" in index_html
        assert "STATE.boxNoRotate !== false" in index_html
        assert 'STATE.activeTool === "box2d" && boxNoRotate()' in index_html
        assert '"omni3d.box_no_rotate"' in index_html

    def test_delete_behavior_still_in_settings(self, index_html):
        assert 'id="delAlwaysToggle"' in index_html
        assert 'id="delModeGroup"' in index_html


class TestPerServerCredentials:
    """凭据按服务商分开存：别指望「一次登录通吃所有服务商」。"""

    def test_credentials_are_per_server(self, index_html):
        assert '"omni3d.cred:"' in index_html or "omni3d.cred:" in index_html
        assert "function readCred" in index_html
        assert "function writeCred" in index_html
        assert "function syncAuthFromServer" in index_html
        # 切服务商 = 换身份
        assert "syncAuthFromServer();" in index_html

    def test_legacy_token_migrates_into_local_server(self, index_html):
        """旧版全局令牌归到内置「本地」名下，迁完删掉旧键。"""
        assert "function migrateLegacyToken" in index_html
        assert "migrateLegacyToken();" in index_html

    def test_api_key_sent_as_header_api_key(self, index_html):
        assert 'headers.set("X-Api-Key", Auth.apiKey)' in index_html
        # 两个凭据不能混着发
        assert 'else if (Auth.token && !headers.has("X-Auth-Token"))' in index_html

    def test_the_row_no_longer_has_a_verify_button(self, index_html):
        """校验搬到了**提交重建那一刻**，设置页那个按钮已经拆掉。

        那边一次「校验」只能证明 `/api/auth/me` 通，证明不了这台真的能干活；
        而且它把「必须先在这儿点一下」变成了隐式前提。
        """
        assert "function verifyServerCred" not in index_html
        assert 'textContent = "校验"' not in index_html
        assert "server-cred-row" in index_html  # 这一行本身还在，只是只留输入框

    def test_the_placeholder_does_not_point_at_a_removed_page(self, index_html):
        """别再把用户指去「登录」页 —— 面板里那页已经归档隐藏（.auth-archive）。"""
        assert "去「登录」页用账号登录" not in index_html


    def test_help_doc_is_loaded_from_its_own_file(self, index_html):
        """帮助文档已抽到 panel/assets/help.html：改文案不用碰 index.html。

        这里只盯接线：空槽位 + 加载代码 + **失败要报出来**（静默留白会被
        当成「帮助页本来就是空的」，最难查）。
        """
        assert "data-help-doc" in index_html
        assert 'fetch("/assets/help.html", { cache: "no-store" })' in index_html
        assert "文档加载失败" in index_html

    def test_help_doc_content_is_gone_from_index_html(self, index_html):
        """抽干净：内容若还留在 index.html，改那个文件就不会生效（静默失效）。"""
        assert "<h4>服务商与 API Key</h4>" not in index_html
        assert "<h4>测量尺度原理</h4>" not in index_html

    def test_help_doc_file_covers_the_key_sections(self):
        """文档也是产物：小节被删掉要红。"""
        path = os.path.join(_ROOT, "panel", "assets", "help.html")
        with open(path, encoding="utf-8") as fh:
            doc = fh.read()
        for section in (
            "<h4>服务商与 API Key</h4>",
            "<h4>抽帧速度</h4>",
            "<h4>相机参数</h4>",
            "<h4>测量尺度原理</h4>",
            "<h4>关于</h4>",
        ):
            assert section in doc, f"帮助文档缺少小节：{section}"
        assert "AR 桥" in doc

    def test_help_doc_describes_the_multi_vertex_measurement(self):
        """文档得跟上新交互（手选全部顶点 / 连成线 / 完成），否则用户不知道怎么做。"""
        path = os.path.join(_ROOT, "panel", "assets", "help.html")
        with open(path, encoding="utf-8") as fh:
            doc = fh.read()
        assert "连成线" in doc, "帮助文档没说明「依次点顶点会自动连线」"
        assert "完成" in doc, "帮助文档没说明点「完成」才定稿"


def _fn_body(index_html: str, name: str) -> str:
    """取出 `function name(...) { ... }` 的完整函数体（按花括号配平）。

    不能用 `split("}", 1)[0]` —— 函数里有嵌套的 if/for，会在第一个右括号处
    截断，断言“找不到”时看着像是代码丢了。
    """
    start = index_html.index(f"function {name}(")
    open_brace = index_html.index("{", start)
    depth = 0
    for k in range(open_brace, len(index_html)):
        if index_html[k] == "{":
            depth += 1
        elif index_html[k] == "}":
            depth -= 1
            if depth == 0:
                return index_html[open_brace : k + 1]
    raise AssertionError(f"{name} 的花括号不配平")


def _css_rule(index_html: str, selector: str) -> str:
    """取出 `selector { ... }` 这条规则（选器 + 声明），选器必须**正好**是它。

    不匹配选器表里的一项（`.a,\n.b { ... }`）—— 那种情况用 `_css_group`。
    找不到就报错，而不是返回空串：空串会让 `assert "box-shadow" not in rule`
    永远绿，等于没有断言。
    """
    pattern = rf"(?:^|[{{}};\s]){re.escape(selector)}\s*\{{([^}}]*)\}}"
    found = re.search(pattern, index_html)
    if not found:
        raise AssertionError(f"找不到 CSS 规则: {selector}")
    return found.group(0)


def _css_group(index_html: str, first_selector: str) -> str:
    """取出「选器表以 first_selector 开头」的那条规则（含表里所有选器）。"""
    start = index_html.index(first_selector)
    return index_html[start : index_html.index("}", start)]


class TestStatusBarReadiness:
    """常驻状态栏只报**当前进度**，不写操作说明，也不许提前说「可完成」。"""

    def test_readiness_words_match_whether_it_can_finish(self, index_html):
        """棕色的那行文字只在真的够数之后才能写「可完成」。

        用户实测：面积只取了 2 个点，棕色的状态行已经写着「已取 2 个 · 可完成」，
        而按钮是灰的 —— 看起来就像按钮坏了。
        """
        body = _fn_body(index_html, "toolStateFor")
        assert _contains_text(body, "got < need ? `已取 ${got}/${need} · 未完成`")
        assert _contains_text(body, "`已取 ${got} 个 · 可完成`")

    def test_the_hint_is_not_pushed_back_into_the_status_bar(self, index_html):
        """工具做完不许再把整句操作说明塞回常驻状态栏（那是帮助内容）。

        画线的说明写着「选好 2 个点后点「应用」，或直接点两个点」——
        而两点一取完本来就已经算完了，那句话会让人以为还得再点一下
        （用户实测：「连线选两个点就结束了不需要点完成」）。
        """
        body = _fn_body(index_html, "onToolClick")
        assert _contains_text(body, 'setToolHint("");')
        assert "setToolHint(tool.hint" not in body


class TestTwoPointSegment:
    """画线 / 尺度这种两点工具，量完要留下看得见的一段。"""

    def test_two_point_tools_draw_their_segment(self, index_html):
        """用户实测：「尺度选完两个点没有自动连线」—— 量完只剩两个孤零零的点，
        看不出量的到底是哪一段。"""
        body = _fn_body(index_html, "onToolClick")
        assert _contains_text(body, "if (refs.length === 2) {")
        assert _contains_text(
            body, "STATE.elements.push(makeEdgeElement(refs[0], refs[1]));"
        )

    def test_calibration_persists_before_opening_the_dialog(self, index_html):
        """尺度那条路必须自己落库：否则弹窗确认时 reloadAnnotations 会把刚画的
        线段丢掉（校准这条分支没有 createMeasurement 帮忙存）。

        ⚠️ 断言的是**紧邻关系**：`onToolClick` 里别处也有 `persistCurrent()`
        （取点不足时的「先落库」），只查「函数里有没有」会永远绿。
        """
        body = _fn_body(index_html, "onToolClick")
        assert _contains_text(
            body,
            'if (STATE.activeTool === "calibrate") { '
            "await persistCurrent(); openCalibrationDialog(refs);",
        ), "开校准弹窗前没落库"


class TestColorToggle:
    """颜色开关：快照不许和几何的属性共用同一份数组。"""

    def test_color_attribute_never_shares_memory_with_the_snapshots(self, index_html):
        """颜色属性必须自己拿一份拷贝 —— 否则「颜色只能切一次」。

        three 的 `BufferAttribute` 只是**引用**传进去的数组（不拷贝），而
        `setColorMode` 直接改写那个数组。共用一份的后果：切到高度着色时把快照里
        的真彩色一起覆盖 → 再切回来时 rgb == height，画面不再变化（用户实测：
        「点击颜色按钮只会切换一次然后就卡死了」）。删掉任意一处 `.slice()` 这条就红。
        """
        assert _contains_text(
            index_html, "new THREE.BufferAttribute(colors.slice(), 3)"
        )
        assert _contains_text(
            index_html, '(STATE.colorMode === "height" ? hgt : rgb).slice()'
        )

    def test_color_button_is_disabled_without_real_colors(self, index_html):
        """没有真实颜色的云两种模式本来就是同一张图 → 置灰，而不是点了没反应。"""
        assert "cloudHasRgb" in index_html
        body = _fn_body(index_html, "updateViewButtons")
        assert _contains_text(body, "!hasCloud || !STATE.cloudHasRgb")


class TestMeasureChildrenCollapse:
    """测量完成时子元素默认折叠在测量行内（设置里可关、可逐行展开）。"""

    def test_children_are_collapsed_by_default(self, index_html):
        assert "collapseMeasureChildren: true" in index_html
        assert _contains_text(
            index_html, 'const saved = readLS("omni3d.collapse_children");'
        )
        assert _contains_text(
            index_html,
            "STATE.collapseMeasureChildren = saved === null ? true : saved",
        )
        body = _fn_body(index_html, "renderElementList")
        assert _contains_text(body, "if (childrenCollapsed(el)) return;")

    def test_collapse_can_be_turned_off_and_expanded_per_row(self, index_html):
        body = _fn_body(index_html, "childrenCollapsed")
        assert "STATE.collapseMeasureChildren" in body
        assert "STATE.expandedMeasurements.has(el.id)" in body
        row = _fn_body(index_html, "buildElementRow")
        assert _contains_text(row, "STATE.expandedMeasurements.add(el.id)")
        assert _contains_text(row, "STATE.expandedMeasurements.delete(el.id)")
        assert "UI_ICONS.caret" in row, "没有展开箭头就没法展开"
        # 设置项要真的接上（有控件、有 dom 引用、有 change 处理器）
        assert 'id="collapseChildrenToggle"' in index_html
        assert "collapseChildrenToggle: $(" in index_html
        assert "dom.collapseChildrenToggle.checked" in index_html


class TestCalibrationDialog:
    def test_the_dialog_input_is_not_a_raw_browser_input(self, index_html):
        """弹窗里的输入框必须跟别的输入框一样是「浅坑」，不能是浏览器默认外观。

        实测（在浏览器里读计算样式）：`#calDialogRealDist` 原本是
        `background: rgb(255,255,255)` + `color: rgb(0,0,0)` +
        `border: rgb(118,118,118)` + `font-family: Arial` + `appearance: auto`
        —— 一块贴在新拟物台面上的白板，深色主题下更刺眼。这是「弹窗设计的
        不好看」里最实的一条。
        """
        rule = _css_rule(index_html, ".modal-field input")
        assert "box-shadow: var(--neu-inset-soft)" in rule
        assert "background: var(--fill-1)" in rule
        assert "font-family: inherit" in rule
        # 数字框的系统上下箭头在台面上是两个灰点
        assert _contains_text(
            index_html, '.modal-field input[type="number"] { appearance: textfield;'
        )

    def test_the_dialog_does_not_repeat_the_placeholder(self, index_html):
        """「真实距离（例：A4 长边 0.297 m）」只是把输入框的 placeholder 又说一遍，
        而「已选两点」也是废话（弹窗本来就是选完两点才弹的）。"""
        assert "A4 长边" not in index_html
        assert "已选两点" not in index_html
        assert _contains_text(index_html, '<span class="prop-key">模型距离</span>')


class TestProviderLightAndSubmitGate:
    """状态灯的语义 + 凭据校验的时机（用户给的规格）。

    - 支不支持无 Key 访问是**服务商的策略**（`/health` 的 `anonymous`），
      不是客户端猜的，也不看来源地址（走内网穿透时请求同样来自 127.0.0.1）；
    - 校验发生在**每次提交重建**，不在设置页；
    - 切服务商灯回灰，提交成功变绿，提交失败变红。
    """

    def test_the_capability_comes_from_the_server(self, index_html):
        body = _fn_body(index_html, "refreshServerInfo")
        assert "anonymous: d.anonymous !== false" in body, (
            "匿名策略必须读服务商自己声明的 /health.anonymous"
        )

    def test_light_is_no_longer_driven_by_health_probing(self, index_html):
        """旧口径必须整体消失：拿 /health + /api/auth/me 预判「就绪」只能
        说明「连得上」，说明不了「这台真的能给你干活」。"""
        assert "function checkCloudStatus" not in index_html
        assert "function checkIdentity" not in index_html
        assert "cloudState" not in index_html
        assert 'classList.add("cloud-dot", state)' not in index_html

    def test_light_has_exactly_three_states(self, index_html):
        body = _fn_body(index_html, "markLight")
        assert 'classList.remove("ok", "failed")' in body
        assert '"ok"' in body and '"failed"' in body
        for old in ("ready", "loading", "unverified", "invalid", "denied"):
            assert f'"{old}"' not in body, f"旧状态 {old} 又回来了"

    def test_any_failure_turns_the_light_red(self, index_html):
        """红 = 错误（**任何**失败），灯**不区分失败的种类**。

        具体是哪一种失败由提示文案说（认证失败会点名）；灯只回答
        「这台现在能不能用」。把种类塞进颜色会有两个红/一个绿的歧义。
        """
        body = _fn_body(index_html, "submitReconstruction")
        assert 'markLight("failed")' in body
        assert "markLight(authFail" not in body, (
            "别按失败种类分颜色 —— 任何失败都点红，细节放文案里"
        )
        assert "认证失败：${why}" in body, "但文案仍要区分出认证失败"

    def test_switching_provider_resets_the_light_to_grey(self, index_html):
        assert 'markLight("unknown")' in _fn_body(index_html, "activateServer")
        # 换了凭据也是未知：旧 Key 的结果不能算在新 Key 头上
        assert 'markLight("unknown")' in _fn_body(index_html, "buildCredRow")

    def test_submit_verifies_via_the_response_status(self, index_html):
        """校验 = 请求**照发**，看服务商回的状态码。

        客户端自己预判「这台要不要凭据」会多出一处可能与服务端不一致的判断，
        而且会把「根本没发出去的请求」伪装成「提交失败」。
        """
        body = _fn_body(index_html, "submitReconstruction")
        assert "serverCaps.anonymous === false" not in body, (
            "提交前不该在客户端预判要不要凭据 —— 由服务商的状态码说了算"
        )
        assert "if (!resp.ok)" in body, "必须按 HTTP 状态码判定成败（光看 body.ok 不够）"
        assert "resp.status === 401" in body
        assert "认证失败：${why}" in body, "401/403 要说成人话（认证失败），别只报 HTTP 码"
        assert 'markLight("failed")' in body

    def test_submit_success_is_the_only_green_source(self, index_html):
        body = _fn_body(index_html, "submitReconstruction")
        assert 'markLight("ok")' in body
        # 绿只来自「请求被接受」，不来自别的任何地方
        assert index_html.count('markLight("ok")') == 1

    def test_no_health_polling(self, index_html):
        """灯不再靠轮询维持 —— 只在提交 / 换凭据 / 切服务商时变。"""
        assert "setInterval(checkCloudStatus" not in index_html
        assert "setInterval(refreshServerInfo" not in index_html
