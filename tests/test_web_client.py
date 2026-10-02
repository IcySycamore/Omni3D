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
        assert "200 点 = 1 分" in index_html
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


class TestThemes:
    """主题：只覆盖 CSS 变量，能在设置里切换并记住。"""

    _TOKENS = (
        "--bg-deep",
        "--bg-card",
        "--accent-cyan",
        "--star-opacity",
        "--viewer-bg",
    )

    @pytest.mark.parametrize("name", ["slate", "violet", "amber"])
    def test_each_theme_covers_all_key_tokens(self, index_html, name: str):
        """漏覆盖 token 的后果是「切过去之后某处还是上个主题的颜色」。"""
        marker = f':root[data-theme="{name}"]'
        assert marker in index_html, f"没有 {name} 主题块"
        block = index_html.split(marker)[1].split("}")[0]
        missing = [t for t in self._TOKENS if t not in block]
        assert not missing, f"{name} 缺少 {missing}"

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

    def test_server_row_can_verify_credential(self, index_html):
        assert "function verifyServerCred" in index_html
        assert 'headers["X-Api-Key"] = cred.apiKey' in index_html
        assert "server-cred-row" in index_html

    def test_help_page_documents_providers_and_measurement(self, index_html):
        """服务商与凭据的说明现在放在帮助页，推广页不再重复。"""
        for section in (
            "<h4>服务商与 API Key</h4>",
            "<h4>抽帧速度</h4>",
            "<h4>相机参数</h4>",
            "<h4>测量尺度原理</h4>",
            "<h4>关于</h4>",
        ):
            assert section in index_html, f"帮助页缺少小节：{section}"
        assert "AR 桥" in index_html
