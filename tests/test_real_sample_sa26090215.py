"""真实样本 SA26090215 回归固化（T06 批次 4，架构裁决文档 §8.4 **R4 版**）。

本模块把「真 OCR 跑批」的 18 条记录**离线复跑**固化为分层断言，作为 QA #9 的验收基座。

数据来源
--------
``过程产出/校验详细日志_SA26090215_累积.json``（一次真实跑批的累积证据快照，
含每条记录的 ``record``（申报值 + 逐图 OCR 文本）与 ``verdict``）。
本模块用该快照重建 :class:`core.models.DeclarationRecord`，跑**修复后的**
:class:`core.judge_engine.JudgeEngine`，对结果做四层硬断言 + 观测项。

⚠️ 本模块**不重跑 OCR**（图像不重新识别），OCR 文本取自快照——因此它验证的是
**判定层口径**，不是 OCR 引擎本身（后者由 T03 的用例覆盖）。

设计依据
--------
架构裁决文档《架构裁决_真OCR误报缺陷_0916.md》第 8 节（**R4**）：

  * §8.4 **四层**硬断言（原第 4 层已删除；seq=15 归第 1 层；L2 删「不得含整机」）；
  * §8.1 **方法**：以实测为准，撤销一切「推荐区间」；
  * §8.2 逐条判定依据表（复核用）。

R4 归属演进（详见 §8.0）
------------------------
  * 裁决 1（老板口径）：**整机品牌 → ❌**。seq=15 与 seq=12 结构同构 → 同判 ❌
    → **原第 4 层断言整体删除**；seq=15 归第 1 层。
  * L2 原「seq=2 note 不得含整机」**删除**（与缺陷 B 整图全文检索自相矛盾）。
  * 裁决 2：OCR 已知误读 → **自动纠正后比对**（seq=16 `SKYHORTH`→`SKYWORTH`→❌）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.judge_engine import JudgeEngine
from core.models import (
    DeclarationRecord,
    ImageEvidence,
    OcrText,
    Verdict,
)

# ── 数据源定位 ───────────────────────────────────────────────────────
_PROCESS_CANDIDATES = [
    Path(__file__).resolve().parent.parent / "过程产出",
    Path(r"D:\Data\workspace\workdoc\AI测试\过程产出"),
]
_SNAPSHOT_NAME = "校验详细日志_SA26090215_累积.json"


def _locate_snapshot() -> Path | None:
    """定位真跑批累积 JSON 快照（不存在则返回 ``None``）。"""
    for base in _PROCESS_CANDIDATES:
        candidate = base / _SNAPSHOT_NAME
        if candidate.exists():
            return candidate
    return None


def _build_record(rec: dict) -> DeclarationRecord:
    """由快照中的 ``record`` 字典重建 :class:`DeclarationRecord`（含逐图 OCR）。"""
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
    return DeclarationRecord(
        ticket_no=rec.get("ticket_no", ""),
        part_no=rec.get("part_no", ""),
        order_no=rec.get("order_no", ""),
        product_name=rec.get("product_name", ""),
        seq_no=int(rec.get("seq_no", 0) or 0),
        raw_element_text=rec.get("raw_element_text", ""),
        decl_brand=rec.get("decl_brand", ""),
        decl_model=rec.get("decl_model", ""),
        structure_variant=rec.get("structure_variant", "D"),
        source_row=int(rec.get("source_row", 0) or 0),
        evidences=evidences,
    )


@pytest.fixture
def sa26090215_results(rule_repo) -> dict[int, object]:
    """对 SA26090215 的 18 条记录跑修复后的判定引擎，返回 ``{seq_no: CheckResult}``。"""
    snapshot = _locate_snapshot()
    if snapshot is None:
        pytest.skip(f"真跑批快照不存在：{_SNAPSHOT_NAME}")

    data = json.loads(snapshot.read_text(encoding="utf-8"))
    engine = JudgeEngine(rules=rule_repo)
    results: dict[int, object] = {}
    for item in data.get("results", []) or []:
        record = _build_record(item.get("record", {}))
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
#  第 1 层：真整机命中 → 依 SOP 3.4 第 147 行保留 ❌，差异须标「整机」
#
#  R4（裁决 1）：**seq=15 归本层**（与 seq=12 结构同构，同为整机命中 + 品牌缺失）。
#  实测同图整机命中：9→img2, 11→img9/10/11, 12→img8, 13→img1, 14→img8, **15→img1**。
# ══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("seq", [9, 11, 12, 13, 14, 15])
def test_layer1_whole_machine_preserves_fail(sa26090215_results, seq: int) -> None:
    """整机品牌命中（同图 ``Brand:`` + ``CARTON``/``JOB NO.``）→ 保留 ❌ 且 note 含「整机」。

    依据：裁决 1（R4，老板口径：整机品牌 → ❌）+ SOP 3.4 第 147 行（「字段缺失→❌」）
    + §8.4 第 1 层（R4：seq=15 由原第 4 层移入本层）。
    """
    result = sa26090215_results[seq]
    assert result.verdict == Verdict.FAIL, f"seq={seq} 整机品牌应保留 ❌"
    assert any("整机" in note for note in _notes(result)), (
        f"seq={seq} 整机品牌 ❌ 须带「整机」差异说明"
    )


# ══════════════════════════════════════════════════════════════════
#  第 2 层：申报缺失但图片明确有 → SOP 3.4 第 4 条 ❌（与整机路径无关）
#
#  R4：**原「note 不得含『整机』」断言已删除** —— 该断言基于旧 ``context_window=3``
#  代码（称「seq=2 无整机命中图」），与缺陷 B（整图全文检索 ``context_scope=whole_image``）
#  **自相矛盾**。实测 seq=2 img5 同图含 ``Brand:SANSUI`` + ``JOB NO.:2660328M`` +
#  ``CARTON No.:A2`` → 整图口径下必然命中整机 → note **会**含「整机」，不再禁止。
# ══════════════════════════════════════════════════════════════════
def test_layer2_declared_missing_is_fail(sa26090215_results) -> None:
    """seq=2：申报缺失但图片明确有 → ❌（SOP 3.4 第 147 行第 4 条）。

    R4：note 是否含「整机」**不作断言**（整图口径下会含，属正常）。
    """
    assert sa26090215_results[2].verdict == Verdict.FAIL


# ══════════════════════════════════════════════════════════════════
#  第 3 层：OCR 证据不足（低置信度）→ SOP 第 29 行 ⚠️，不得 FAIL
# ══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("seq", [3, 6, 10])
def test_layer3_low_confidence_is_no_mark(sa26090215_results, seq: int) -> None:
    """低置信度 OCR → ⚠️（SOP 第 29 行 + 不虚高红线）。"""
    assert sa26090215_results[seq].verdict == Verdict.NO_MARK, (
        f"seq={seq} 低置信度证据不足应 ⚠️"
    )


# ══════════════════════════════════════════════════════════════════
#  第 4 层（**R4 重编号，原第 5 层**）：字段名残片 / 跨体系类不得 ❌
#
#  ⚠️ 原第 4 层（「seq=15 != FAIL」）**已整体删除**（R4 裁决 1）：
#    seq=15 与 seq=12 结构完全同构（整机命中 + 型号双侧一致 + 品牌缺失），
#    裁决 1 定「整机 → ❌」→ seq=15 归第 1 层判 ❌，原「不得 FAIL」断言作废。
# ══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("seq", [7, 8])
def test_layer4_field_fragment_not_fail(sa26090215_results, seq: int) -> None:
    """字段名残片 / 跨体系类不得判 ❌（口径问题 2，§8.4 第 4 层 R4）。"""
    assert sa26090215_results[seq].verdict != Verdict.FAIL, (
        f"seq={seq} 字段名残片不得判 ❌"
    )


# ══════════════════════════════════════════════════════════════════
#  回归锁（缺陷 E / F + 裁决 2）
# ══════════════════════════════════════════════════════════════════
def test_defect_e_model_not_blanked(sa26090215_results) -> None:
    """**缺陷 E 回归锁**：seq=16/17/18 申报型号 ``HS-8A50J-12`` 尾含「适用于X牌」，
    不得被 ``skip_prefixes`` 误伤判空（架构裁决文档 §5A）。"""
    for seq in (16, 17, 18):
        result = sa26090215_results[seq]
        # 判定层不再因"型号=空 + 图内有型号"制造假矛盾：型号被正确提取后，
        # 至少不应出现「型号：申报『HS-8A50J-12』与图片『』明确不一致」这类假异常。
        model_notes = [
            d.note or ""
            for d in (result.differences or [])
            if d.field == "型号"
        ]
        assert not any("申报『HS-8A50J-12』与图片『』" in n for n in model_notes), (
            f"seq={seq} 缺陷 E：型号 HS-8A50J-12 被误判空，产生假异常"
        )


def test_total_conserved(sa26090215_counts) -> None:
    """18 条记录总数守恒。"""
    assert sum(sa26090215_counts.values()) == 18


def test_no_image_zero(sa26090215_counts) -> None:
    """本样本无缺图（🔵=0，§8.2）。"""
    assert sa26090215_counts[Verdict.NO_IMAGE.value] == 0


# ══════════════════════════════════════════════════════════════════
#  观测项（**非硬断言**，§8.4：打印实际值供复核）+ 分布观测
# ══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("seq", [1, 16, 17, 18])
def test_observed_records(sa26090215_results, seq: int) -> None:
    """§8.4 观测项：打印 seq 1/16/17/18 的实际 verdict + 差异说明（供交叉复核）。"""
    result = sa26090215_results[seq]
    print(
        f"\n[观测项] seq={seq}: verdict={result.verdict.value} "
        f"brand={result.detected_brand!r} model={result.detected_model!r} "
        f"diffs={_notes(result)}"
    )


def test_distribution_observed(sa26090215_counts) -> None:
    """打印实际四类分布（**观测项，不设区间**，§8.1 R4）。

    ⚠️ R4 裁决（§8.1）：**撤销一切「推荐区间」**（原 ``[1,3]``/``[5,7]``/``[8,11]``
    均建立在此前错误归因上，会诱导「凑数」）。本用例**只打印实测分布**，不做
    区间硬断言；逐条依据成立性由 §8.2 依据表人工复核（不追求数量吻合）。
    """
    observed = {
        "PASS": sa26090215_counts[Verdict.PASS.value],
        "FAIL": sa26090215_counts[Verdict.FAIL.value],
        "NO_MARK": sa26090215_counts[Verdict.NO_MARK.value],
        "NO_IMAGE": sa26090215_counts[Verdict.NO_IMAGE.value],
    }
    print(f"\n[SA26090215 实测分布] {observed}")
    print("[§8.1 R4] 不设推荐区间；逐条依据见 §8.2 依据表")
    assert sum(observed.values()) == 18
