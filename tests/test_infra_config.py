"""infra.config 配置与持久化测试（T01 / v1.1 E1 路径记忆）。"""

from __future__ import annotations

from pathlib import Path

from infra.config import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_BATCH_SIZE_HEAVY,
    SHARE_BASE_TEMPLATE,
    AppConfig,
    default_share_root,
)


class TestDefaults:
    """零配置可启动。"""

    def test_defaults_present(self) -> None:
        cfg = AppConfig()
        assert cfg.excel_path == ""
        assert cfg.share_root == ""
        assert cfg.batch_size == DEFAULT_BATCH_SIZE == 6
        assert cfg.batch_size_heavy == DEFAULT_BATCH_SIZE_HEAVY == 3
        assert cfg.ocr_max_workers == 2
        assert cfg.ocr_intra_op_threads == 2

    def test_share_template_contains_year_placeholder(self) -> None:
        """SOP 2.6：共享根模板必须含 {年份} 占位。"""
        assert "{年份}" in SHARE_BASE_TEMPLATE
        assert "报关要素图片" in SHARE_BASE_TEMPLATE

    def test_default_share_root_substitutes_year(self) -> None:
        root = default_share_root(2026)
        assert "2026年" in root
        assert "{年份}" not in root


class TestDerived:
    """派生属性。"""

    def test_needs_user_input_when_empty(self) -> None:
        assert AppConfig().needs_user_input is True

    def test_no_need_when_both_set(self) -> None:
        cfg = AppConfig(excel_path="D:\\a.xlsx", share_root=r"\\h\s\t")
        assert cfg.needs_user_input is False

    def test_resolved_share_root_prefers_user(self) -> None:
        """用户指定优先（SOP 2.2），绝不静默用默认。"""
        cfg = AppConfig(share_root=r"\\user\picked")
        assert cfg.resolved_share_root() == r"\\user\picked"

    def test_resolved_share_root_falls_back_to_template(self) -> None:
        cfg = AppConfig(share_year=2026)
        assert "2026年" in cfg.resolved_share_root()

    def test_effective_batch_size_downgrades_when_heavy(self) -> None:
        cfg = AppConfig()
        assert cfg.effective_batch_size(3) == 6
        assert cfg.effective_batch_size(8) == 3
        assert cfg.effective_batch_size(12) == 3


class TestPersistence:
    """持久化（E1 路径记忆，FR-024）。"""

    def test_save_then_load_roundtrip(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        original = AppConfig(
            excel_path="D:\\输入\\申报要素.xlsx",
            share_root=r"\\172.20.99.220\制造中心\仓储物流部\成品科\2026年报关要素图片",
            ticket_no="SA26090215",
            result_dir="D:\\成果产出",
            process_dir="D:\\过程产出",
            share_year=2026,
            batch_size=5,
        )
        original.save(cfg_file)

        loaded = AppConfig.load(cfg_file)
        assert loaded.excel_path == original.excel_path
        assert loaded.share_root == original.share_root
        assert loaded.ticket_no == "SA26090215"
        assert loaded.share_year == 2026
        assert loaded.batch_size == 5

    def test_load_missing_returns_defaults(self, tmp_path: Path) -> None:
        """文件缺失时返回全默认（启动永不失败）。"""
        cfg = AppConfig.load(tmp_path / "nope.json")
        assert cfg.batch_size == DEFAULT_BATCH_SIZE

    def test_load_corrupt_returns_defaults(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{ this is not json", encoding="utf-8")
        cfg = AppConfig.load(bad)
        assert cfg.batch_size == DEFAULT_BATCH_SIZE

    def test_load_ignores_unknown_keys(self, tmp_path: Path) -> None:
        """容忍旧/新版本 schema 的未知字段。"""
        cfg_file = tmp_path / "cfg.json"
        cfg_file.write_text(
            '{"schema": 99, "config": {"batch_size": 4, "future_field": "x"}}',
            encoding="utf-8",
        )
        cfg = AppConfig.load(cfg_file)
        assert cfg.batch_size == 4

    def test_chinese_path_written_unescaped(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "cfg.json"
        AppConfig(excel_path="D:\\中文\\申报要素.xlsx").save(cfg_file)
        text = cfg_file.read_text(encoding="utf-8")
        assert "申报要素" in text
        assert "\\u" not in text


class TestFromEnv:
    """环境变量通道（Q4 内部通道，不作为用户主路径）。"""

    def test_reads_excel_and_share(self) -> None:
        cfg = AppConfig.from_env(
            {
                "CUSTOMS_EXCEL": "D:\\a.xlsx",
                "CUSTOMS_SHARE": r"\\h\s",
                "MAX_RECORDS": "18",
            }
        )
        assert cfg.excel_path.endswith("a.xlsx")
        assert cfg.share_root == r"\\h\s"
        assert cfg.max_records == 18

    def test_empty_env_returns_defaults(self) -> None:
        cfg = AppConfig.from_env({})
        assert cfg.excel_path == ""
        assert cfg.max_records == 0

    def test_invalid_max_records_ignored(self) -> None:
        cfg = AppConfig.from_env({"MAX_RECORDS": "abc"})
        assert cfg.max_records == 0
