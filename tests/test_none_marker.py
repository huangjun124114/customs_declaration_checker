"""图片侧「显式无标记」识别回归测试（core.none_marker，口径 v0.3.2）。

依据：用户裁定 2026-09-17 需求 1 / 需求 2 ——

    申报品牌为「无」或**没有该要素** ＋ 图片 OCR 中有「无品牌」字样 → 品牌核验通过
    申报型号为「无」或**没有该要素** ＋ 图片 OCR 中有「无型号」字样 → 型号核验通过

⚠️ 本文件同时是**防虚高红线的反向锁**：``品牌:无锡机电`` 这类"值以『无』开头但
   并不是『无』标记"的情形**必须不命中** —— 否则会把有品牌判成"无品牌一致"。
"""

from __future__ import annotations

import pytest

from core import constants as C
from core.models import OcrText
from core.none_marker import (
    detect_none_marker,
    detect_none_markers,
    iter_none_marker_rules,
)
from core.rule_repository import RuleRepository


def _text(lines: str, name: str = "SA26090215&2660326M&001.jpg", seq: int = 0) -> OcrText:
    """构造一条 OCR 结果（``text_raw`` 多行、``image_path`` 决定图号兜底）。"""
    return OcrText(image_path=rf"\\srv\pic\{name}", text_raw=lines, seq=seq)


class TestPositiveMarkers:
    """显式「无」标记的**正向**命中集（多书写形态）。"""

    @pytest.mark.parametrize(
        "line",
        [
            "品牌:无",
            "品牌：无品牌",
            "品牌：无品",
            "Brand:NONE",
            "Brand: N/A",
            "BRAND: 无",
            "型号:无",
            "型号：无型号",
            "规格型号:无",
            "产品型号：无",
            "Model:NONE",
            "MODEL: NA",
            "无品牌",
            "无 品牌",
            "无品。",
            "无品牌|型号:HS-8A50J-12",
            "无型号",
            "无型号、用途:电视机用",
            "用途:电视机用|品牌:无|型号:无",
        ],
    )
    def test_marker_line_hits(self, line: str) -> None:
        assert detect_none_markers([_text(line)]), f"应命中无标记：{line}"

    def test_brand_and_model_detected_in_one_image(self) -> None:
        hits = detect_none_markers([_text("品牌:无\n型号:无型号")])
        assert set(hits) == {C.FIELD_BRAND, C.FIELD_MODEL}
        assert hits[C.FIELD_BRAND].marker.replace(" ", "") == "品牌:无"
        assert hits[C.FIELD_MODEL].marker.replace(" ", "") == "型号:无型号"

    def test_marker_carries_image_and_line(self) -> None:
        hits = detect_none_markers(
            [_text("无品牌", name="SA26090215&2660326M&007.jpg")]
        )
        hit = hits[C.FIELD_BRAND]
        assert hit.image == 7
        assert hit.line == "无品牌"
        assert hit.field == C.FIELD_BRAND
        assert hit.marker_name == "brand_bare"
        assert "无品牌" in hit.description

    def test_prefers_ocr_seq_when_present(self) -> None:
        hits = detect_none_markers(
            [_text("品牌:无", name="A&001.jpg", seq=12)]
        )
        assert hits[C.FIELD_BRAND].image == 12


class TestNegativeGuards:
    """⚠️ 防虚高反向锁：**不是**无标记的情形必须不命中。"""

    @pytest.mark.parametrize(
        "line",
        [
            "品牌:无锡机电",       # 值以「无」开头但不是"无"
            "品牌:无瑕科技",
            "品牌:无X",
            "品牌:无法识别",
            "品牌:无品牌有限公司",
            "品牌:SKYWORTH",
            "型号:A7A01G",
            "型号 NO: A123",       # MODEL/型号 后非「无」取值
            "制造商全称:XX",
            "品牌类型:0",          # 非值类要素（口径 v0.3.1）
            "无品名",
            "无品牌标识说明文字",   # 长句里的字样，非显式标记
            "CARTON NO.B001",
            "Brand:Daewoo",        # 有品牌
        ],
    )
    def test_non_marker_line_not_hit(self, line: str) -> None:
        assert not detect_none_markers([_text(line)]), f"不应命中：{line}"

    def test_empty_texts(self) -> None:
        assert detect_none_markers([]) == {}
        assert detect_none_markers(None) == {}

    def test_blank_lines_ignored(self) -> None:
        assert detect_none_markers([_text("\n\n   \n")]) == {}


class TestRulesSource:
    """规则外置（权威载体 = rules/brand_patterns.yaml，兜底 ≡ repo）。"""

    def test_yaml_none_markers_match_constants(self, rule_repo: RuleRepository) -> None:
        loaded = [tuple(item) for item in rule_repo.get().brand_patterns.none_markers]
        assert loaded == [tuple(item) for item in C.DEFAULT_NONE_MARKERS]

    def test_yaml_markers_compile_and_hit(self, rule_repo: RuleRepository) -> None:
        compiled = rule_repo.get().brand_patterns.compiled_none_markers()
        assert compiled
        assert all(hasattr(p, "search") for _n, _f, p, _note in compiled)

    def test_iter_rules_falls_back_when_missing(self, tmp_path) -> None:
        """YAML 缺 ``none_markers`` 节点 → 回退兜底常量（缺陷 F：兜底 ≡ repo）。"""
        from tests.conftest import RULES_DIR  # noqa: PLC0415
        from tests.test_rule_repository import _copy_rules  # noqa: PLC0415

        _copy_rules(RULES_DIR, tmp_path)
        (tmp_path / "brand_patterns.yaml").write_text(
            "patterns:\n"
            "  - name: ascii_colon\n"
            "    regex: '品牌\\s*[:：]\\s*([A-Za-z0-9\\-\\.]+)'\n"
            "    note: t\n"
            "none_tokens:\n  - 无\n",
            encoding="utf-8",
        )
        repo = RuleRepository(builtin_dir=tmp_path, user_dir=tmp_path / "user")
        assert repo.load_all().brand_patterns.none_markers == ()
        compiled = iter_none_marker_rules(repo)
        assert len(compiled) == len(C.DEFAULT_NONE_MARKERS)
        assert detect_none_markers([_text("品牌:无")], rules=repo)

    def test_missing_field_raises(self, tmp_path) -> None:
        """``none_markers`` 条目缺 ``field`` → RuleConfigError（不静默降级）。"""
        from infra.errors import RuleConfigError  # noqa: PLC0415
        from tests.conftest import RULES_DIR  # noqa: PLC0415
        from tests.test_rule_repository import _copy_rules  # noqa: PLC0415

        _copy_rules(RULES_DIR, tmp_path)
        (tmp_path / "brand_patterns.yaml").write_text(
            "patterns:\n"
            "  - name: ascii_colon\n"
            "    regex: '品牌\\s*[:：]\\s*([A-Za-z0-9\\-\\.]+)'\n"
            "    note: t\n"
            "none_markers:\n"
            "  - name: broken\n"
            "    regex: '无品牌'\n"
            "none_tokens:\n  - 无\n",
            encoding="utf-8",
        )
        repo = RuleRepository(builtin_dir=tmp_path, user_dir=tmp_path / "user")
        with pytest.raises(RuleConfigError) as info:
            repo.load_all()
        assert "field" in str(info.value)


class TestSingleFieldEntry:
    """单要素便捷入口。"""

    def test_detect_one_field(self) -> None:
        texts = [_text("品牌:无\n型号:A7A01G")]
        assert detect_none_marker(texts, C.FIELD_BRAND) is not None
        assert detect_none_marker(texts, C.FIELD_MODEL) is None

    def test_empty_field_returns_none(self) -> None:
        assert detect_none_marker([_text("品牌:无")], "") is None
