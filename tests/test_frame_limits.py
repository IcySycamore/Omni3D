"""视角数量门槛：服务商按显存给出允许档位，并在**提交时自己拦**越界请求。

背景：8GB 显存下 512px 的 16 帧会把显存顶到 95%，前向从 1.4s 退化到 26s+
直至长时间无进展。所以档位由 `config.frame_options()` 给出，客户端展示它，
服务端在 `POST /api/tasks` 校验它 —— **不能只靠客户端**，否则旧页面、脚本
直接提交、或者手改表单，换个数字就能把显存打满。
"""
from server.core import config
from panel import server


class TestFrameOptions:
    """档位口径：环境变量优先，其次按显存分档。"""

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("OMNI3D_FRAME_OPTIONS", "8,12")
        assert config.frame_options() == [8, 12]

    def test_env_override_is_sorted_and_deduped(self, monkeypatch):
        monkeypatch.setenv("OMNI3D_FRAME_OPTIONS", "12, 8, 8")
        assert config.frame_options() == [8, 12]

    def test_options_are_never_empty(self, monkeypatch):
        """无论环境变量怎么给，档位都不能为空 —— 空档位会让页面没得选。"""
        for raw in ("", "   ", "abc", ",,,", "-1", "0"):
            monkeypatch.setenv("OMNI3D_FRAME_OPTIONS", raw)
            assert config.frame_options(), f"raw={raw!r} 时档位为空"

    def test_options_are_sorted_positive(self):
        options = config.frame_options()
        assert options == sorted(options)
        assert all(n > 0 for n in options)

    def test_default_is_inside_options(self):
        assert config.default_frame_count() in config.frame_options()

    def test_default_does_not_exceed_config_ceiling(self, monkeypatch):
        """默认档不超过 `DEFAULT_FRAME_COUNT`（「最多想要多少」的原有口径）。"""
        monkeypatch.setenv("OMNI3D_FRAME_OPTIONS", "4,8,32")
        assert config.default_frame_count() == 8


class TestFrameLimitError:
    """服务端自己拦：文案必须**点名真凶**（当前值 / 上限 / 去哪里改）。"""

    def test_inside_options_passes(self):
        for n in config.frame_options():
            assert server.frame_limit_error(n) is None

    def test_over_limit_names_current_value_and_ceiling(self, monkeypatch):
        monkeypatch.setenv("OMNI3D_FRAME_OPTIONS", "8")
        msg = server.frame_limit_error(16)
        assert msg, "越界必须返回错误文案"
        assert "16" in msg, "文案要点出**当前**的越界值"
        assert "8" in msg, "文案要点出**上限**"
        assert "设置" in msg, "文案要告诉用户去哪里改"

    def test_reports_vram_when_known(self, monkeypatch):
        monkeypatch.setenv("OMNI3D_FRAME_OPTIONS", "8")
        msg = server.frame_limit_error(99)
        if config.gpu_memory_mb():
            assert "显存" in msg

    def test_config_capabilities_exposes_the_same_options(self):
        """`/api/models` 下发的门槛与实际校验用的是**同一份**口径。"""
        caps = server.server_capabilities()
        assert caps["frame_options"] == config.frame_options()
        assert caps["default_frames"] == config.default_frame_count()
