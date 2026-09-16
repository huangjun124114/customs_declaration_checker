"""v0.2.0 点 1：运行目录自动创建（``app.path_policy`` 运行目录约定）。

覆盖：
  * 常量口径：``报关申报要素校验``（**校**非检）、``result``、``logs``；
  * 默认解析锚点 = ``app_base_dir()``；显式 ``app_home`` / ``base`` 可覆盖锚点；
  * 目录**自动创建**且**已存在不被清空**（哨兵文件存活）；
  * 显式覆盖参数（result_dir / process_dir）仍完全可用；
  * 旧 ``config.json`` 的 ``result_dir`` / ``process_dir`` 被正常加载但**不影响**输出目录（Q8）。
"""

from __future__ import annotations

from app import path_policy
from app.path_policy import (
    PROCESS_DIR_NAME,
    RESULT_DIR_NAME,
    WORKSPACE_DIR_NAME,
    PathPolicy,
)

# ══════════════════════════════════════════════════════════════════
#  常量口径
# ══════════════════════════════════════════════════════════════════


class TestConstants:
    """运行目录常量。"""

    def test_workspace_name_uses_jiao_not_jian(self) -> None:
        """根目录名是「报关申报要素**校**验」（现场原文「检验」为笔误，Q1）。"""
        assert WORKSPACE_DIR_NAME == "报关申报要素校验"
        assert "检验" not in WORKSPACE_DIR_NAME

    def test_subdir_names(self) -> None:
        assert RESULT_DIR_NAME == "result"
        assert PROCESS_DIR_NAME == "logs"


# ══════════════════════════════════════════════════════════════════
#  默认解析
# ══════════════════════════════════════════════════════════════════


class TestDefaultResolution:
    """默认（无显式覆盖）解析。"""

    def test_default_anchors_on_app_base_dir(self, tmp_path, monkeypatch) -> None:
        """无参默认锚点 = ``app_base_dir()``。"""
        monkeypatch.setattr(path_policy, "app_base_dir", lambda: tmp_path)
        result_dir, process_dir = PathPolicy().resolve_outputs()
        assert result_dir == tmp_path / "报关申报要素校验" / "result"
        assert process_dir == tmp_path / "报关申报要素校验" / "logs"

    def test_default_paths_end_with_expected_suffix(self, tmp_path, monkeypatch) -> None:
        """（任务要求 a）默认路径以 ``报关申报要素校验/result``、``…/logs`` 结尾。"""
        monkeypatch.setattr(path_policy, "app_base_dir", lambda: tmp_path)
        result_dir, process_dir = PathPolicy().resolve_outputs()
        assert result_dir.as_posix().endswith("报关申报要素校验/result")
        assert process_dir.as_posix().endswith("报关申报要素校验/logs")

    def test_explicit_app_home_overrides_anchor(self, tmp_path) -> None:
        base = tmp_path / "工程根"
        result_dir, process_dir = PathPolicy(app_home=base).resolve_outputs()
        assert result_dir == base / "报关申报要素校验" / "result"
        assert process_dir == base / "报关申报要素校验" / "logs"

    def test_explicit_base_overrides_anchor(self, tmp_path) -> None:
        base = tmp_path / "落点"
        result_dir, process_dir = PathPolicy().resolve_outputs(base=base)
        assert result_dir == base / "报关申报要素校验" / "result"
        assert process_dir == base / "报关申报要素校验" / "logs"


# ══════════════════════════════════════════════════════════════════
#  自动创建 + 不清理已有内容
# ══════════════════════════════════════════════════════════════════


class TestAutoCreate:
    """目录自动就位，且已存在则复用。"""

    def test_dirs_are_created(self, tmp_path) -> None:
        """（任务要求 b）目录能自动创建。"""
        result_dir, process_dir = PathPolicy(app_home=tmp_path).resolve_outputs()
        assert result_dir.is_dir()
        assert process_dir.is_dir()

    def test_existing_sentinel_preserved(self, tmp_path) -> None:
        """（任务要求 c）预先放哨兵文件后再次调用，哨兵仍在（不清空已有内容）。"""
        policy = PathPolicy(app_home=tmp_path)
        result_dir, process_dir = policy.resolve_outputs()

        sentinel = result_dir / "哨兵.txt"
        sentinel.write_text("keep me", encoding="utf-8")
        keep_log = process_dir / "keep.log"
        keep_log.write_text("data", encoding="utf-8")

        again_result, again_process = policy.resolve_outputs()
        assert again_result == result_dir
        assert again_process == process_dir
        assert sentinel.read_text(encoding="utf-8") == "keep me"
        assert keep_log.read_text(encoding="utf-8") == "data"


# ══════════════════════════════════════════════════════════════════
#  显式覆盖参数（必须保留）
# ══════════════════════════════════════════════════════════════════


class TestExplicitOverrides:
    """显式目录覆盖能力保留（测试 / CLI 依赖）。"""

    def test_both_explicit(self, tmp_path) -> None:
        result = tmp_path / "r"
        process = tmp_path / "p"
        result_dir, process_dir = PathPolicy().resolve_outputs(
            result_dir=result, process_dir=process
        )
        assert result_dir == result
        assert process_dir == process

    def test_only_result(self, tmp_path) -> None:
        result = tmp_path / "r"
        result_dir, process_dir = PathPolicy().resolve_outputs(result_dir=result)
        assert result_dir == result
        assert process_dir == result / "logs"

    def test_only_process(self, tmp_path) -> None:
        process = tmp_path / "p"
        result_dir, process_dir = PathPolicy().resolve_outputs(process_dir=process)
        assert process_dir == process
        assert result_dir == process / "result"


# ══════════════════════════════════════════════════════════════════
#  旧配置兼容（Q8）
# ══════════════════════════════════════════════════════════════════


class TestLegacyConfigIgnored:
    """旧 ``config.json`` 的路径字段被忽略，不影响输出目录（Q8）。"""

    def test_old_config_does_not_change_outputs(self, tmp_path) -> None:
        """（任务要求 d）加载含旧 ``result_dir`` / ``process_dir`` 的配置不改输出目录。"""
        from infra.config import AppConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            '{"schema": 1, "config": {"ticket_no": "SA26090215", '
            '"result_dir": "D:\\\\legacy\\\\成果产出", '
            '"process_dir": "D:\\\\legacy\\\\过程产出"}}',
            encoding="utf-8",
        )
        cfg = AppConfig.load(cfg_file)
        # 字段能正常反序列化（防 from_dict 崩溃），但输出目录与之无关
        assert cfg.ticket_no == "SA26090215"
        assert cfg.result_dir.endswith("成果产出")

        anchor = tmp_path / "落点"
        result_dir, process_dir = PathPolicy(app_home=anchor).resolve_outputs()
        assert result_dir == anchor / "报关申报要素校验" / "result"
        assert process_dir == anchor / "报关申报要素校验" / "logs"
