"""真实样本 SA26090215 回归固化（**v0.3.0 R5 版**）。

本模块把「真 OCR 跑批」的 18 条记录**离线复跑**固化为分层断言，作为 QA #9 的验收基座。

## 数据来源与「申报侧刷新」（v0.3.0 修正）

``过程产出/校验详细日志_SA26090215_累积.json``（一次真实跑批的累积证据快照，
含每条记录的 ``record``（逐图 OCR 文本）与当时的判定值）。

⚠️ **v0.3.0 修正**：该快照的 ``record.decl_brand/decl_model`` 是**当时**的要素解析
产物。实测 seq=16/17/18 的 ``decl_model`` 在快照里为**空**（缺陷 E 修复前的遗留），
而现行链路 ``core/excel_probe.py`` 是 ``decl_* = ElementParser.parse(raw_element_text)``
→ 应为 ``HS-8A50J-12``。若沿用陈旧申报值，回归会**以错误的输入**校验判定层。
故本模块**用当前解析器重新解析** ``raw_element_text`` 得到申报侧
（:func:`_build_record` 的 ``parser`` 入参），OCR 证据仍取快照（**不重跑 OCR**）。

## v0.3.0 口径变更（本模块的核心职责）

判定主链路由「**先提取、后比对**」改为「**完整分词直接命中**」
（``docs/10_迭代方案_v0.3_0916.md``），必然改变既有分布 —— 这是**口径变更的合规动作**，
不是"改测试凑绿"：

    ============  =====================================================
    基线          分布（18 条）
    ============  =====================================================
    v0.2.0        ✅1 / ❌12 / ⚠️5 / 🔵0
    v0.3.0        ✅7 / ❌7  / ⚠️4 / 🔵0   ← 本模块硬断言
    ============  =====================================================

变更 6 条（1/4/5/16/17/18，全部 ❌/⚠️ → ✅），逐条依据见
``tools/eval_v03.py --json`` 产出的对比表与本文档各层用例。

## 分层断言（沿用 R4 四层 + 新增第 5 层）

  * 第 1 层：整机品牌命中 → **保留 ❌**（T06 裁决 A′，v0.3.0 **不受影响**）；
  * 第 2 层：申报缺失但图片明确有 → ❌（SOP 3.4 第 4 条）；
  * 第 3 层：OCR 证据不足（低置信度）→ ⚠️（不虚高红线）；
  * 第 4 层：字段名残片 / 跨文字体系类不得 ❌；
  * **第 5 层（v0.3.0 新增）**：申报值在图中以完整分词命中 → ✅（含 FUZZY 纠正路径）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.element_parser import ElementParser
from core.judge_engine import JudgeEngine
from core.models import (
    DeclarationRecord,
    ImageEvidence,
    NoiseLevel,
    OcrText,
    Verdict,
)

# ── 数据源定位 ───────────────────────────────────────────────────────
_PROCESS_CANDIDATES = [
    Path(__file__).resolve().parent.parent / "过程产出",
    Path(r"D:\Data\workspace\workdoc\AI测试\过程产出"),
]
_SNAPSHOT_NAME = "校验详细日志_SA26090215_累积.json"

#: v0.2.0 基线（**仅供参考**：由旧链路复算 + 快照共同印证，``tools/eval_v03.py``）
BASELINE_V020: dict[str, int] = {"PASS": 1, "FAIL": 12, "NO_MARK": 5, "NO_IMAGE": 0}
#: ⭐ v0.3.0 实测基线（2026-09-16 确立，**口径变更后新回归基准**）
BASELINE_V030: dict[str, int] = {"PASS": 7, "FAIL": 7, "NO_MARK": 4, "NO_IMAGE": 0}

#: v0.3.0 由 ❌/⚠️ 变更为 ✅ 的记录（完整分词命中）
V030_NEW_PASS: tuple[int, ...] = (1, 4, 5, 16, 17, 18)


def _locate_snapshot() -> Path | None:
    """定位真跑批累积 JSON 快照（不存在则返回 ``None``）。"""
    for base in _PROCESS_CANDIDATES:
        candidate = base / _SNAPSHOT_NAME
        if candidate.exists():
            return candidate
    return None


def _build_record(rec: dict, parser: ElementParser | None = None) -> DeclarationRecord:
    """由快照中的 ``record`` 字典重建 :class:`DeclarationRecord`（含逐图 OCR）。

    Args:
        rec: 快照 ``record`` 字典。
        parser: 当前要素解析器；提供时用它**刷新申报侧**（消除快照的陈旧申报值）。

    Returns:
        :class:`core.models.DeclarationRecord`。
    """
    evidences: list[ImageEvidence] = []
    for ev in rec.get("evidences", []) or []:
        ocr_raw = ev.get("ocr") or {}
        ocr = OcrText(
            image_path=ocr_raw.get("image_path", ev.get("image_path", "")),
            text_raw=ocr_raw.get("text_raw", ""),
            confidence=float(ocr_raw.get("confidence", 0.0) or 0.0),
            boxes=[],
            seq=int(ocr_raw.get("seq", 0) or 0),
            low_confidence=bool(ocr_raw.get("low_confidence", False)),
        )
        evidences.append(
            ImageEvidence(
                image_path=ev.get("image_path", ""),
                seq=int(ev.get("seq", 0) or 0),
                exists=bool(ev.get("exists", False)),
                ocr_confidence=float(ev.get("ocr_confidence", 0.0) or 0.0),
                unreachable=bool(ev.get("unreachable", False)),
                not_found=bool(ev.get("not_found", False)),
                ocr=ocr,
            )
        )

    brand = rec.get("decl_brand", "")
    model = rec.get("decl_model", "")
    raw_element = rec.get("raw_element_text", "") or ""
    if parser is not None and raw_element:
        parsed = parser.parse(raw_element)
        brand = getattr(parsed, "brand", "") or ""
        model = getattr(parsed, "model", "") or ""

    return DeclarationRecord(
        ticket_no=rec.get("ticket_no", ""),
        part_no=rec.get("part_no", ""),
        order_no=rec.get("order_no", ""),
        product_name=rec.get("product_name", ""),
        seq_no=int(rec.get("seq_no", 0) or 0),
        raw_element_text=raw_element,
        decl_brand=brand,
        decl_model=model,
        structure_variant=rec.get("structure_variant", "D"),
        source_row=int(rec.get("source_row", 0) or 0),
        evidences=evidences,
    )


@pytest.fixture
def sa26090215_results(rule_repo) -> dict[int, object]:
    """对 SA26090215 的 18 条记录跑现行判定引擎，返回 ``{seq_no: CheckResult}``。

    ⚠️ 申报侧由**当前** :class:`core.element_parser.ElementParser` 重新解析
    ``raw_element_text``（与 ``excel_probe.py`` 同行代码路径），不沿用快照旧值。
    """
    snapshot = _locate_snapshot()
    if snapshot is None:
        pytest.skip(f"真跑批快照不存在：{_SNAPSHOT_NAME}")

    data = json.loads(snapshot.read_text(encoding="utf-8"))
    engine = JudgeEngine(rules=rule_repo)
    parser = ElementParser(rule_repository=rule_repo)
    results: dict[int, object] = {}
    for item in data.get("results", []) or []:
        record = _build_record(item.get("record", {}), parser)
        results[record.seq_no] = engine.judge(record)
    return results


@pytest.fixture
def sa26090215_counts(sa26090215_results) -> dict[str, int]:
    """四类分布计数。"""
    counts = {v.value: 0 for v in Verdict}
    for res in sa26090215_results.values():
        counts[res.verdict.value] += 1
    return counts


def _notes(result) -> list[str]:
    """取一条结果的差异说明列表。"""
    return [d.note or "" for d in (result.differences or [])]


# ══════════════════════════════════════════════════════════════════
#  ⭐ 第 0 层：v0.3.0 新基线硬断言（口径变更的合规锚点）
# ══════════════════════════════════════════════════════════════════
def test_v030_baseline_locked(sa26090215_counts) -> None:
    """**v0.3.0 实测基线**：✅7 / ❌7 / ⚠️4 / 🔵0（18 条守恒）。

    依据：``docs/10_迭代方案_v0.3_0916.md`` §3.5 + 本模块文档头。
    变更来源 = 判定主链路改「完整分词命中」（6 条：1/4/5/16/17/18）。
    """
    observed = {
        "PASS": sa26090215_counts[Verdict.PASS.value],
        "FAIL": sa26090215_counts[Verdict.FAIL.value],
        "NO_MARK": sa26090215_counts[Verdict.NO_MARK.value],
        "NO_IMAGE": sa26090215_counts[Verdict.NO_IMAGE.value],
    }
    assert observed == BASELINE_V030, (
        f"v0.3.0 基线漂移：实测 {observed}，期望 {BASELINE_V030}"
        f"（v0.2.0 参考基线 {BASELINE_V020}）"
    )


def test_baseline_moved_from_v020_to_v030(sa26090215_counts) -> None:
    """口径变更**确实发生**且方向符合预期：✅ 上升、❌ 下降（防止"改了但没生效"）。"""
    assert sa26090215_counts[Verdict.PASS.value] > BASELINE_V020["PASS"]
    assert sa26090215_counts[Verdict.FAIL.value] < BASELINE_V020["FAIL"]


# ══════════════════════════════════════════════════════════════════
#  第 1 层：真整机命中 → 依 SOP 3.4 第 147 行保留 ❌，差异须标「整机」
#
#  R4（裁决 1）：seq=15 归本层；v0.3.0：本层**不受新链路影响**（R2 回归锁）。
# ══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("seq", [9, 11, 12, 13, 14, 15])
def test_layer1_whole_machine_preserves_fail(sa26090215_results, seq: int) -> None:
    """整机品牌命中（同图 ``Brand:`` + ``CARTON``/``JOB NO.``）→ 保留 ❌ 且 note 含「整机」。

    依据：裁决 1（整机品牌 → ❌）+ SOP 3.4 第 147 行（「字段缺失→❌」）。
    ⚠️ v0.3.0 回归锁：新链路「命中即合格」**不得**抹掉本裁决。
    """
    result = sa26090215_results[seq]
    assert result.verdict == Verdict.FAIL, f"seq={seq} 整机品牌应保留 ❌"
    assert any("整机" in note for note in _notes(result)), (
        f"seq={seq} 整机品牌 ❌ 须带「整机」差异说明"
    )


# ══════════════════════════════════════════════════════════════
#  第 2 层：申报缺失但图片明确有 → SOP 3.4 第 4 条 ❌（与整机路径无关）
# ══════════════════════════════════════════════════════════════
def test_layer2_declared_missing_is_fail(sa26090215_results) -> None:
    """seq=2：申报缺失但图片明确有 → ❌（SOP 3.4 第 147 行第 4 条）。"""
    assert sa26090215_results[2].verdict == Verdict.FAIL


# ══════════════════════════════════════════════════════════════
#  第 3 层：OCR 证据不足（低置信度）→ SOP 第 29 行 ⚠️，不得 FAIL
# ══════════════════════════════════════════════════════════════
@pytest.mark.parametrize("seq", [3, 6, 10])
def test_layer3_low_confidence_is_no_mark(sa26090215_results, seq: int) -> None:
    """低置信度 OCR → ⚠️（SOP 第 29 行 + 不虚高红线）。"""
    assert sa26090215_results[seq].verdict == Verdict.NO_MARK, (
        f"seq={seq} 低置信度证据不足应 ⚠️"
    )


# ══════════════════════════════════════════════════════════════
#  第 4 层：字段名残片 / 跨体系类不得 ❌
# ══════════════════════════════════════════════════════════════
@pytest.mark.parametrize("seq", [7, 8])
def test_layer4_field_fragment_not_fail(sa26090215_results, seq: int) -> None:
    """字段名残片 / 跨体系类不得判 ❌（口径问题 2，§8.4 第 4 层 R4）。"""
    assert sa26090215_results[seq].verdict != Verdict.FAIL, (
        f"seq={seq} 字段名残片不得判 ❌"
    )


# ══════════════════════════════════════════════════════════════
#  ⭐ 第 5 层（v0.3.0 新增）：申报值以完整分词命中 → ✅
# ══════════════════════════════════════════════════════════════
@pytest.mark.parametrize("seq", V030_NEW_PASS)
def test_layer5_complete_token_hit_is_pass(sa26090215_results, seq: int) -> None:
    """申报品牌/型号在 OCR 全文里以**完整分词**出现 → ✅（v0.3.0 需求 1 主路径）。

    逐条依据（``tools/eval_v03.py`` 实测）：

      * seq=1  ``baori`` → 图 2 实物印字 ``baori``；
      * seq=4  ``宇同``  → 图 1 ``宇同电子（惠州）有限公司``；
      * seq=5  ``baori`` → 图 4 ``baori E339609 AWM 20941 …``；
      * seq=16/17/18 ``DAEWOO`` + ``HS-8A50J-12`` → 唛头 ``DAEWOO`` 行 + 标贴
        ``HS-8A50J-12 K1 …`` 行（旧链路误取 ``SKYHORTH`` / ``HS-8AA`` 等
        字段名残片/截断值 → 假异常）。
    """
    result = sa26090215_results[seq]
    assert result.verdict == Verdict.PASS, f"seq={seq} 完整分词命中应判 ✅"
    assert any(m.hit for m in result.token_matches), f"seq={seq} 应至少一个字段命中"


def test_hit_field_has_no_difference(sa26090215_results) -> None:
    """**结构锁**：某字段命中后，不得再为该字段生成差异明细（否则结论自相矛盾）。"""
    for seq, result in sa26090215_results.items():
        for field_name in ("品牌", "型号"):
            match = result.token_match_for(field_name)
            if match is None or not match.hit:
                continue
            leftovers = [d for d in result.differences if d.field == field_name]
            assert not leftovers, f"seq={seq} {field_name} 已命中却仍有差异明细：{leftovers}"


def test_fuzzy_hits_are_traceable(sa26090215_results) -> None:
    """**可追溯锁**：``FUZZY`` 命中必须带「图内误读原文」，不得静默纠正。"""
    for seq, result in sa26090215_results.items():
        for match in result.token_matches:
            if match.mode == "FUZZY":
                assert match.corrected_from, f"seq={seq} FUZZY 命中未记录误读原文"
                assert "误读" in match.note, f"seq={seq} FUZZY 说明未标注误读"


def test_token_matches_present_for_both_fields(sa26090215_results) -> None:
    """每条记录必须产出品牌 / 型号两条 ``TokenMatch``（UI 三列展示的数据源）。"""
    for seq, result in sa26090215_results.items():
        fields = {m.field for m in result.token_matches}
        assert fields == {"品牌", "型号"}, f"seq={seq} token_matches 字段不全：{fields}"


def test_token_matches_not_in_summary_row(sa26090215_results) -> None:
    """**13 列硬锁**：新增的 ``token_matches`` **绝不**进汇总表行。"""
    for seq, result in sa26090215_results.items():
        assert len(result.to_row()) == 13, f"seq={seq} 汇总表列数被改动"


def test_suspicious_never_escalated_to_fail(sa26090215_results) -> None:
    """**不虚高红线锁**：``SUSPICIOUS`` 的记录**绝不**判 ❌。"""
    for seq, result in sa26090215_results.items():
        if result.noise_level == NoiseLevel.SUSPICIOUS:
            assert result.verdict != Verdict.FAIL, f"seq={seq} 疑似噪声不得判 ❌"


# ══════════════════════════════════════════════════════════════
#  回归锁（缺陷 E / F + 裁决 2）
# ══════════════════════════════════════════════════════════════
def test_defect_e_model_parsed_and_matched(sa26090215_results) -> None:
    """**缺陷 E 回归锁**：seq=16/17/18 申报型号 ``HS-8A50J-12`` 尾含「适用于X牌」，
    不得被 ``skip_prefixes`` 误伤判空；且现应在图中以完整分词命中。"""
    for seq in (16, 17, 18):
        result = sa26090215_results[seq]
        assert result.record is not None
        assert result.record.decl_model == "HS-8A50J-12", (
            f"seq={seq} 缺陷 E：型号被误判空（实际 {result.record.decl_model!r}）"
        )
        model_match = result.token_match_for("型号")
        assert model_match is not None and model_match.mode == "EXACT", (
            f"seq={seq} 型号 HS-8A50J-12 应命中图中标贴完整分词"
        )


def test_total_conserved(sa26090215_counts) -> None:
    """18 条记录总数守恒。"""
    assert sum(sa26090215_counts.values()) == 18


def test_no_image_zero(sa26090215_counts) -> None:
    """本样本无缺图（🔵=0，§8.2）。"""
    assert sa26090215_counts[Verdict.NO_IMAGE.value] == 0


# ══════════════════════════════════════════════════════════════
#  观测项（**非硬断言**，打印实际值供复核）
# ══════════════════════════════════════════════════════════════
@pytest.mark.parametrize("seq", [1, 4, 5, 16, 17, 18])
def test_observed_token_hits(sa26090215_results, seq: int) -> None:
    """观测项：打印 v0.3.0 变更为 ✅ 的记录的命中取证。"""
    result = sa26090215_results[seq]
    detail = "；".join(
        f"{m.field}={m.mode}({m.token}@{m.images})" for m in result.token_matches
    )
    print(
        f"\n[观测项] seq={seq}: verdict={result.verdict.value} "
        f"brand={result.detected_brand!r} model={result.detected_model!r} {detail}"
    )


def test_distribution_observed(sa26090215_counts) -> None:
    """打印实测四类分布（附 v0.2.0 参考基线，便于口径变更审阅）。"""
    observed = {
        "PASS": sa26090215_counts[Verdict.PASS.value],
        "FAIL": sa26090215_counts[Verdict.FAIL.value],
        "NO_MARK": sa26090215_counts[Verdict.NO_MARK.value],
        "NO_IMAGE": sa26090215_counts[Verdict.NO_IMAGE.value],
    }
    print(f"\n[SA26090215 实测分布] v0.3.0={observed}  v0.2.0(参考)={BASELINE_V020}")
    assert sum(observed.values()) == 18
