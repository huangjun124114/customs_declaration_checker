"""core.rule_repository 回归测试（T01 验收要点 ⑧ + v1.1 C.4）。

覆盖：
  1. YAML 加载正确性（6 份文件全部加载）；
  2. 必填字段缺失 → 报错（不静默降级）；
  3. 用户外置覆盖优先于内置；
  4. 不可变快照（frozen，改不动）；
  5. **外置规则与 constants.py 默认值一致性**（防两处漂移，v1.1 C.4）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core import constants as C
from core.rule_repository import RULE_FILES, RuleRepository
from infra.errors import RuleConfigError


def _copy_rules(src: Path, dst: Path) -> None:
    """把内置规则复制到临时目录，便于单点篡改。"""
    dst.mkdir(parents=True, exist_ok=True)
    for name in RULE_FILES:
        shutil.copy(src / name, dst / name)


class TestLoadAll:
    """6 份规则全部加载成功。"""

    def test_all_files_present(self, rules_dir: Path) -> None:
        # 缺陷 C：新增 `non_brand_tokens.yaml` → 共 7 份规则文件。
        assert len(RULE_FILES) == 7
        for name in RULE_FILES:
            assert (rules_dir / name).is_file(), f"缺少规则文件 {name}"

    def test_load_all_succeeds(self, rule_repo: RuleRepository) -> None:
        snapshot = rule_repo.get()
        assert snapshot is not None
        for name in RULE_FILES:
            assert snapshot.sources()[name.replace(".yaml", "")], f"{name} 未记录来源"

    def test_blacklist_loaded(self, rule_repo: RuleRepository) -> None:
        rules = rule_repo.get().fields_blacklist
        assert "制造商全称" in rules.terms
        assert "MFR P/N" in rules.terms
        assert "适用于" in rules.skip_prefixes
        # 缺陷 C / 口径问题 2：字段名残片已追加。
        assert "SKYWORTH P/N" in rules.terms
        assert "Manufacturer" in rules.terms

    def test_brand_patterns_loaded_and_compilable(self, rule_repo: RuleRepository) -> None:
        rules = rule_repo.get().brand_patterns
        assert len(rules.patterns) >= 4
        compiled = rules.compiled()   # 编译不抛异常
        assert all(hasattr(p, "search") for _n, p, _note in compiled)

    def test_model_clean_loaded(self, rule_repo: RuleRepository) -> None:
        rules = rule_repo.get().model_clean_rules
        assert rules.anchor_regex
        assert rules.dirty_tail_chars

    def test_whole_machine_loaded(self, rule_repo: RuleRepository) -> None:
        rules = rule_repo.get().whole_machine_brand
        assert "Brand:" in rules.context_tokens
        # 缺陷 B 裁决（架构裁决文档 3.3）：语义由 `context_window` 改为 `context_scope`。
        # 原 `context_window == 3` 硬断言已移除（避免两处语义冲突）。
        assert rules.context_scope == "whole_image"
        # 缺陷 A 裁决：`Customer model` 已移出 context_tokens。
        assert "Customer model" not in rules.context_tokens
        assert "Customermodel" not in rules.context_tokens

    def test_non_brand_tokens_loaded(self, rule_repo: RuleRepository) -> None:
        """缺陷 C：``non_brand_tokens.yaml`` 已加载（架构裁决文档 4.3）。"""
        rules = rule_repo.get().non_brand_tokens
        assert rules.tokens
        for token in ("AAA", "prime", "video", "NETFLIX", "NFK", "SAMYOUNG", "SMT"):
            assert token in rules.tokens, f"缺少非品牌 token {token}"

    def test_separators_loaded(self, rule_repo: RuleRepository) -> None:
        rules = rule_repo.get().separators
        assert "|" in rules.separators
        assert rules.canonical_separator == "|"
        assert "\u200b" in rules.invisible_chars

    def test_noise_signals_loaded(self, rule_repo: RuleRepository) -> None:
        rules = rule_repo.get().noise_signals
        assert rules.fuzzy_max_edit_distance == 2
        assert rules.fuzzy_min_length == 5
        assert rules.fuzzy_verdict == "SUSPICIOUS"
        assert len(rules.confusable_chars) >= 8


class TestConfusableNormalization:
    """易混字符归一化（v1.2 第 13.5.2 节，SKYWORTH→SKYHORTH 实测）。"""

    def test_w_h_normalized(self, rule_repo: RuleRepository) -> None:
        rules = rule_repo.get().noise_signals
        # SKYWORTH 归一化后应等于 SKYHORTH（W 与 H 同组）
        assert rules.normalize_confusables("SKYWORTH") == rules.normalize_confusables(
            "SKYHORTH"
        )

    def test_n_h_normalized(self, rule_repo: RuleRepository) -> None:
        """P/N 与 P/H 归一化后相等（N/H 同组，v1.2 实测 P/N→P/H）。"""
        rules = rule_repo.get().noise_signals
        assert rules.normalize_confusables("P/N") == rules.normalize_confusables("P/H")

    def test_skyworth_skyhorth_equal(self, rule_repo: RuleRepository) -> None:
        """SKYWORTH 与 SKYHORTH 归一化后相等（W/H 同组）。"""
        rules = rule_repo.get().noise_signals
        assert rules.normalize_confusables("SKYWORTH") == rules.normalize_confusables(
            "SKYHORTH"
        )

    def test_o_zero_normalized(self, rule_repo: RuleRepository) -> None:
        rules = rule_repo.get().noise_signals
        assert rules.normalize_confusables("O0") == rules.normalize_confusables("00")

    def test_unrelated_strings_differ(self, rule_repo: RuleRepository) -> None:
        rules = rule_repo.get().noise_signals
        assert rules.normalize_confusables("GXD-009") != rules.normalize_confusables("ABC-777")


class TestValidate:
    """校验（必填字段 + 正则语法），失败必须报错而非静默降级。"""

    def test_valid_rules_pass(self, rule_repo: RuleRepository) -> None:
        warnings = rule_repo.validate()
        assert warnings == []

    def test_missing_terms_raises(self, tmp_path: Path, rules_dir: Path) -> None:
        """``fields_blacklist.yaml`` 缺 terms → RuleConfigError。"""
        _copy_rules(rules_dir, tmp_path)
        (tmp_path / "fields_blacklist.yaml").write_text(
            "skip_prefixes:\n  - 适用于\n", encoding="utf-8"
        )
        repo = RuleRepository(builtin_dir=tmp_path, user_dir=tmp_path / "user")
        with pytest.raises(RuleConfigError) as info:
            repo.load_all()
            repo.validate()
        assert "terms" in str(info.value)

    def test_missing_brand_patterns_raises(self, tmp_path: Path, rules_dir: Path) -> None:
        _copy_rules(rules_dir, tmp_path)
        (tmp_path / "brand_patterns.yaml").write_text(
            "none_tokens:\n  - 无\n", encoding="utf-8"
        )
        repo = RuleRepository(builtin_dir=tmp_path, user_dir=tmp_path / "user")
        with pytest.raises(RuleConfigError):
            repo.load_all()
            repo.validate()

    def test_bad_regex_raises(self, tmp_path: Path, rules_dir: Path) -> None:
        _copy_rules(rules_dir, tmp_path)
        (tmp_path / "brand_patterns.yaml").write_text(
            "patterns:\n"
            "  - name: broken\n"
            "    regex: '(unclosed'\n"
            "none_tokens:\n  - 无\n",
            encoding="utf-8",
        )
        repo = RuleRepository(builtin_dir=tmp_path, user_dir=tmp_path / "user")
        repo.load_all()
        with pytest.raises(RuleConfigError) as info:
            repo.validate()
        assert "broken" in str(info.value) or "语法错误" in str(info.value)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """文件缺失 → RuleConfigError（含文件名）。"""
        empty = tmp_path / "empty"
        empty.mkdir()
        repo = RuleRepository(builtin_dir=empty, user_dir=tmp_path / "user")
        with pytest.raises(RuleConfigError) as info:
            repo.load_all()
        assert "缺失" in str(info.value) or "noise_signals" in str(info.value)

    def test_non_dict_yaml_raises(self, tmp_path: Path, rules_dir: Path) -> None:
        _copy_rules(rules_dir, tmp_path)
        (tmp_path / "separators.yaml").write_text("- a\n- b\n", encoding="utf-8")
        repo = RuleRepository(builtin_dir=tmp_path, user_dir=tmp_path / "user")
        with pytest.raises(RuleConfigError) as info:
            repo.load_all()
        assert "字典" in str(info.value)

    def test_malformed_yaml_raises(self, tmp_path: Path, rules_dir: Path) -> None:
        _copy_rules(rules_dir, tmp_path)
        (tmp_path / "separators.yaml").write_text(
            "separators: [unclosed\n", encoding="utf-8"
        )
        repo = RuleRepository(builtin_dir=tmp_path, user_dir=tmp_path / "user")
        with pytest.raises(RuleConfigError) as info:
            repo.load_all()
        assert "YAML" in str(info.value) or "语法" in str(info.value)


class TestUserOverride:
    """用户外置覆盖优先于内置（架构设计 12.C.3 N3）。"""

    def test_user_file_wins(self, tmp_path: Path, rules_dir: Path) -> None:
        user_dir = tmp_path / "user_rules"
        user_dir.mkdir()
        (user_dir / "fields_blacklist.yaml").write_text(
            "terms:\n  - 自定义字段名\nskip_prefixes:\n  - 适用于\n",
            encoding="utf-8",
        )
        repo = RuleRepository(builtin_dir=rules_dir, user_dir=user_dir)
        snapshot = repo.load_all()
        assert snapshot.fields_blacklist.terms == ("自定义字段名",)
        assert "用户外置覆盖" in " ".join(repo.load_notes)

    def test_builtin_used_when_no_override(self, rule_repo: RuleRepository) -> None:
        assert "制造商全称" in rule_repo.get().fields_blacklist.terms


class TestReloadAndImmutability:
    """手动重载 + 不可变快照。"""

    def test_reload_picks_up_changes(self, tmp_path: Path, rules_dir: Path) -> None:
        user_dir = tmp_path / "user_rules"
        user_dir.mkdir()
        override = user_dir / "fields_blacklist.yaml"
        override.write_text("terms:\n  - v1\nskip_prefixes: []\n", encoding="utf-8")

        repo = RuleRepository(builtin_dir=rules_dir, user_dir=user_dir)
        assert repo.load_all().fields_blacklist.terms == ("v1",)

        override.write_text("terms:\n  - v2\nskip_prefixes: []\n", encoding="utf-8")
        # 未 reload 前仍是旧快照（不自动热加载）
        assert repo.get().fields_blacklist.terms == ("v1",)

        assert repo.reload().fields_blacklist.terms == ("v2",)

    def test_snapshot_is_frozen(self, rule_repo: RuleRepository) -> None:
        """快照不可变（frozen dataclass）。"""
        rules = rule_repo.get().fields_blacklist
        with pytest.raises((AttributeError, TypeError)):
            rules.terms = ("hacked",)  # type: ignore[misc]

    def test_get_autoloads(self, rules_dir: Path, tmp_path: Path) -> None:
        repo = RuleRepository(builtin_dir=rules_dir, user_dir=tmp_path / "u")
        # 未显式 load_all 也能 get 到（自动加载）
        assert repo.get().fields_blacklist.terms


class TestConsistencyWithConstants:
    """⚠️ 外置规则与 constants.py 默认值一致性（v1.1 C.4，防两处漂移）。"""

    def test_field_blacklist_matches(self, rule_repo: RuleRepository) -> None:
        assert list(rule_repo.get().fields_blacklist.terms) == C.DEFAULT_FIELD_BLACKLIST

    def test_skip_prefixes_matches(self, rule_repo: RuleRepository) -> None:
        assert list(rule_repo.get().fields_blacklist.skip_prefixes) == C.DEFAULT_SKIP_PREFIXES

    def test_none_tokens_matches(self, rule_repo: RuleRepository) -> None:
        assert list(rule_repo.get().brand_patterns.none_tokens) == C.DEFAULT_NONE_TOKENS

    def test_separators_matches(self, rule_repo: RuleRepository) -> None:
        assert list(rule_repo.get().separators.separators) == C.DEFAULT_SEPARATORS

    def test_colon_variants_matches(self, rule_repo: RuleRepository) -> None:
        assert list(rule_repo.get().separators.colon_variants) == C.DEFAULT_COLON_VARIANTS

    def test_dirty_tail_chars_matches(self, rule_repo: RuleRepository) -> None:
        assert list(rule_repo.get().model_clean_rules.dirty_tail_chars) == C.DEFAULT_DIRTY_TAIL_CHARS

    def test_whole_machine_context_matches(self, rule_repo: RuleRepository) -> None:
        assert (
            list(rule_repo.get().whole_machine_brand.context_tokens)
            == C.DEFAULT_WHOLE_MACHINE_CONTEXT
        )

    def test_whole_machine_scope_matches(self, rule_repo: RuleRepository) -> None:
        """缺陷 B：``context_scope`` 与 constants 一致（防两处漂移）。"""
        assert (
            rule_repo.get().whole_machine_brand.context_scope
            == C.DEFAULT_WHOLE_MACHINE_SCOPE
        )

    def test_non_brand_tokens_matches(self, rule_repo: RuleRepository) -> None:
        """缺陷 C：``non_brand_tokens`` 与 constants 一致（防两处漂移）。"""
        assert (
            list(rule_repo.get().non_brand_tokens.tokens)
            == C.DEFAULT_NON_BRAND_TOKENS
        )

    def test_confusable_chars_matches(self, rule_repo: RuleRepository) -> None:
        loaded = [list(group) for group in rule_repo.get().noise_signals.confusable_chars]
        assert loaded == C.DEFAULT_CONFUSABLE_CHARS

    def test_fuzzy_params_match(self, rule_repo: RuleRepository) -> None:
        rules = rule_repo.get().noise_signals
        assert rules.fuzzy_max_edit_distance == C.DEFAULT_FUZZY_MAX_EDIT_DISTANCE
        assert rules.fuzzy_min_length == C.DEFAULT_FUZZY_MIN_LENGTH


class TestKnownNoiseSamples:
    """实测噪声样本回归集（v1.2 第 13.5.1 节）。"""

    def test_samples_present(self, rule_repo: RuleRepository) -> None:
        samples = rule_repo.get().noise_signals.known_noise_samples
        ocr_values = {s.get("ocr") for s in samples}
        assert "SKYHORTH P/H" in ocr_values
        assert "IFR PIN" in ocr_values
