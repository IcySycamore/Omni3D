"""网页客户端（web/index.html）的结构性回归测试。

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
_INDEX = os.path.join(_ROOT, "web", "index.html")
_SHA_SCRIPT = os.path.join(_ROOT, "tests", "tools", "web_sha256_check.js")
_POINTCLOUD_SCRIPT = os.path.join(_ROOT, "tests", "tools", "web_pointcloud_check.js")

_WEB = os.path.join(_ROOT, "web")
if _WEB not in sys.path:
    sys.path.insert(0, _WEB)

import auth_store  # noqa: E402


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

    def test_defaults_to_16_frames(self, index_html):
        assert "frameCount: 16," in index_html
        assert 'class="radio-pill selected" data-value="16"' in index_html


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
