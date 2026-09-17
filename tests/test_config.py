"""`app/core/config.py` 的默认值与环境变量语义测试。

重点守住两个容易被改坏的地方：
1. `0` 到底是「合法值（关闭）」还是「非法值（回退默认）」—— 由 `allow_zero` 决定；
2. 推理默认档位（分辨率 / 抽帧数 / 置信度过滤）属于「精度优先」的产品决策，
   改动必须是有意识的（#23）。
"""
import pytest

# torch 必须最先导入（本机 fbgemm.dll 加载顺序冲突）
import torch  # noqa: F401,I001

from app.core import config


class TestEnvParsing:
    """`_env_int` / `_env_float` 的边界语义。"""

    def test_int_missing_uses_default(self, monkeypatch):
        monkeypatch.delenv("OMNI3D_TEST_INT", raising=False)
        assert config._env_int("OMNI3D_TEST_INT", 7) == 7

    def test_int_illegal_uses_default(self, monkeypatch):
        monkeypatch.setenv("OMNI3D_TEST_INT", "abc")
        assert config._env_int("OMNI3D_TEST_INT", 7) == 7

    def test_int_zero_is_illegal_by_default(self, monkeypatch):
        """默认不允许 0：点数上限为 0 没有意义，应回退默认值。"""
        monkeypatch.setenv("OMNI3D_TEST_INT", "0")
        assert config._env_int("OMNI3D_TEST_INT", 7) == 7

    def test_int_zero_allowed_when_asked(self, monkeypatch):
        """开关类参数必须能表达「关闭」。"""
        monkeypatch.setenv("OMNI3D_TEST_INT", "0")
        assert config._env_int("OMNI3D_TEST_INT", 7, allow_zero=True) == 0

    def test_int_negative_is_illegal_even_with_allow_zero(self, monkeypatch):
        monkeypatch.setenv("OMNI3D_TEST_INT", "-3")
        assert config._env_int("OMNI3D_TEST_INT", 7, allow_zero=True) == 7

    def test_float_parsing(self, monkeypatch):
        monkeypatch.setenv("OMNI3D_TEST_FLOAT", "2.5")
        assert config._env_float("OMNI3D_TEST_FLOAT", 2.0) == 2.5

    def test_float_zero_needs_allow_zero(self, monkeypatch):
        monkeypatch.setenv("OMNI3D_TEST_FLOAT", "0")
        assert config._env_float("OMNI3D_TEST_FLOAT", 2.0) == 2.0
        assert config._env_float("OMNI3D_TEST_FLOAT", 2.0, allow_zero=True) == 0.0

    def test_float_illegal_uses_default(self, monkeypatch):
        monkeypatch.setenv("OMNI3D_TEST_FLOAT", "not-a-number")
        assert config._env_float("OMNI3D_TEST_FLOAT", 2.0) == 2.0


class TestQualityDefaults:
    """「精度优先」的默认档位（#23）。"""

    def test_default_resolution_is_full_frame(self):
        """默认必须是 512：224 会被裁成正方形，不适合测量。"""
        assert config.DEFAULT_RESOLUTION == 512

    def test_default_frame_count_is_16(self):
        assert config.DEFAULT_FRAME_COUNT == 16

    def test_confidence_percentile_within_range(self):
        assert 0 <= config.VIS_CONF_PERCENTILE <= 99

    def test_outlier_removal_defaults(self):
        """SOR 默认开启，且两个参数都必须能通过 0 关闭。"""
        assert config.SOR_K == 8
        assert config.SOR_STD == 2.0


class TestEnvChoice:
    def test_missing_uses_default(self, monkeypatch):
        monkeypatch.delenv("OMNI3D_TEST_CHOICE", raising=False)
        assert config._env_choice("OMNI3D_TEST_CHOICE", ("a", "b"), "a") == "a"

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("OMNI3D_TEST_CHOICE", "B")
        assert config._env_choice("OMNI3D_TEST_CHOICE", ("a", "b"), "a") == "b"

    def test_illegal_uses_default(self, monkeypatch):
        monkeypatch.setenv("OMNI3D_TEST_CHOICE", "zzz")
        assert config._env_choice("OMNI3D_TEST_CHOICE", ("a", "b"), "a") == "a"


class TestCheckpointDir:
    """权重目录：默认在仓库内，但必须能用环境变量指到挂载卷（部署必需）。"""

    def test_env_path_overrides(self, monkeypatch):
        monkeypatch.setenv("OMNI3D_TEST_PATH", "/models/whatever")
        assert config._env_path("OMNI3D_TEST_PATH", "/default") == "/models/whatever"

    def test_env_path_missing_and_blank_fall_back(self, monkeypatch):
        monkeypatch.delenv("OMNI3D_TEST_PATH", raising=False)
        assert config._env_path("OMNI3D_TEST_PATH", "/default") == "/default"
        monkeypatch.setenv("OMNI3D_TEST_PATH", "   ")
        assert config._env_path("OMNI3D_TEST_PATH", "/default") == "/default"

    def test_default_points_into_the_repo_cache(self):
        """默认值是 ignore 掉的本地缓存目录（权重不进仓）。"""
        assert config.CHECKPOINT_DIR.replace("\\", "/").endswith(
            "jedyang97/Fast3R_ViT_Large_512"
        )
