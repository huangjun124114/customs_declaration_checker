"""SA26090215 真实样本「新旧口径逐条对比」评估器（v0.3.0，T4）。

**用途**：口径变更（v0.2.0「先提取后比对」→ v0.3.0「完整分词命中」）的
**合规动作**——产出 18 条 ×（旧判定 / 新判定 / 申报值 / 命中 token / 变更原因）对比表，
并确立 v0.3.0 的**新回归基线**。

## 方法（为什么不用快照里的判定值当"旧侧"）

``过程产出/校验详细日志_SA26090215_累积.json`` 是 v0.1/v0.2 时代的**回放产物**：
其 ``record.decl_brand/decl_model`` 携带的是**当时**的要素解析结果。实测发现
seq=16/17/18 的 ``decl_model`` 在快照里为**空**（缺陷 E 修复前的遗留），而按
``core/excel_probe.py`` 的现行链路 ``decl_* = ElementParser.parse(raw_element_text)``
应为 ``HS-8A50J-12`` —— 即"旧侧"若直接取快照判定值，会**用陈旧申报值**与"新侧"对比，
结论不可信。

故本工具的做法是**固定证据、刷新申报侧、两侧同源复算**：

  1. **证据固定**：OCR 原文（逐图 ``text_raw``）取快照，**不重跑 OCR**；
  2. **申报侧刷新**：用**当前** :class:`core.element_parser.ElementParser` 重新解析
     ``raw_element_text``（与现行管线同一行代码路径，见 ``excel_probe.py``）；
  3. **旧侧复算**：从 git 取出 v0.2.0 的 ``core/judge_engine.py``（默认 ``dfe00d5``），
     作为**独立模块**加载，对同一批记录跑旧链路 —— 得到"纯口径差异"的对照；
  4. **新侧**：当前 ``core/judge_engine.py``；
  5. **交叉校验**：打印「旧侧复算 vs 快照记录判定」的一致情况——两者在申报值未变
     的记录上应完全一致（自证旧侧复算未走样），仅在申报值被刷新的记录上不同。

用法::

    python tools/eval_v03.py                       # 对比表 + 新基线
    python tools/eval_v03.py --fingerprint         # 附加原始输入指纹校验
    python tools/eval_v03.py --json out.json       # 结构化结果落盘
    python tools/eval_v03.py --legacy-ref <ref>    # 指定旧引擎的 git 版本
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.element_parser import ElementParser  # noqa: E402
from core.judge_engine import JudgeEngine  # noqa: E402
from core.models import (  # noqa: E402
    DeclarationRecord,
    ImageEvidence,
    OcrText,
    Verdict,
)
from core.rule_repository import RuleRepository  # noqa: E402

#: v0.2.0 交付提交（旧链路权威版本）
DEFAULT_LEGACY_REF = "dfe00d5"

GIT_EXE_CANDIDATES = (
    "git",
    r"D:\Dev\IDE\git\Git\cmd\git.exe",
    r"C:\Program Files\Git\cmd\git.exe",
)

_SNAPSHOT_CANDIDATES = (
    PROJECT_ROOT.parent / "过程产出" / "校验详细日志_SA26090215_累积.json",
    PROJECT_ROOT / "过程产出" / "校验详细日志_SA26090215_累积.json",
    Path(r"D:\Data\workspace\workdoc\AI测试\过程产出\校验详细日志_SA26090215_累积.json"),
)
_INPUT_DIR_CANDIDATES = (
    PROJECT_ROOT.parent / "原始输入",
    PROJECT_ROOT / "原始输入",
)

#: v0.2.0 已知指纹（文件数 / 总字节）—— 跑批前后必须一致
EXPECTED_INPUT_FILES = 130
EXPECTED_INPUT_BYTES = 394_140_543


# ══════════════════════════════════════════════════════════════════
#  资源定位
# ══════════════════════════════════════════════════════════════════
def locate_snapshot() -> Path:
    """定位真跑批累积 JSON 快照。"""
    for candidate in _SNAPSHOT_CANDIDATES:
        if candidate.exists():
            return candidate
    raise SystemExit(f"找不到累积快照，候选：{_SNAPSHOT_CANDIDATES}")


def _git(*args: str) -> str:
    """执行 git 命令并返回 stdout（依次尝试候选可执行文件）。"""
    last: Exception | None = None
    for exe in GIT_EXE_CANDIDATES:
        try:
            proc = subprocess.run(  # noqa: S603 - 常量命令 + 项目根目录
                [exe, *args],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                check=True,
            )
        except Exception as exc:  # noqa: BLE001 - 换下一个候选
            last = exc
            continue
        return proc.stdout.decode("utf-8", errors="replace")
    raise SystemExit(f"git 不可用：{last}")


def load_legacy_engine(ref: str = DEFAULT_LEGACY_REF) -> Any:
    """从 git ``ref`` 取出 v0.2.0 的 ``judge_engine.py`` 并作为独立模块加载。

    Args:
        ref: git 引用（提交 / 标签）。

    Returns:
        旧版 ``judge_engine`` 模块对象（提供 ``JudgeEngine``）。

    Raises:
        SystemExit: 取不到源码或加载失败。
    """
    source = _git("show", f"{ref}:core/judge_engine.py")
    if "先提取、后比对" not in source and "tier_min_images" not in source:
        raise SystemExit(f"{ref} 的 judge_engine.py 不像 v0.2.0 旧链路，请用 --legacy-ref 指定")
    workdir = Path(tempfile.mkdtemp(prefix="customs_legacy_"))
    target = workdir / "legacy_judge_engine.py"
    target.write_text(source, encoding="utf-8")

    spec = importlib.util.spec_from_file_location("legacy_judge_engine_v020", target)
    if spec is None or spec.loader is None:
        raise SystemExit("旧引擎模块加载失败")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def input_fingerprint() -> tuple[int, int, str]:
    """统计 ``原始输入`` 目录指纹（文件数 / 总字节 / 路径）。"""
    for base in _INPUT_DIR_CANDIDATES:
        if base.is_dir():
            count = 0
            total = 0
            for path in base.rglob("*"):
                if path.is_file():
                    count += 1
                    total += path.stat().st_size
            return count, total, str(base)
    return 0, 0, "(未找到)"


# ══════════════════════════════════════════════════════════════════
#  记录重建（证据固定 + 申报侧刷新）
# ══════════════════════════════════════════════════════════════════
def build_record(rec: dict, parser: ElementParser | None = None) -> tuple[DeclarationRecord, str]:
    """由快照 ``record`` 重建 :class:`DeclarationRecord`。

    Args:
        rec: 快照中的 ``record`` 字典。
        parser: 当前要素解析器；提供时用它**刷新**申报品牌/型号。

    Returns:
        ``(record, 申报侧刷新说明)``。
    """
    evidences: list[ImageEvidence] = []
    for ev in rec.get("evidences", []) or []:
        raw = ev.get("ocr") or {}
        ocr = OcrText(
            image_path=raw.get("image_path", ev.get("image_path", "")),
            text_raw=raw.get("text_raw", ""),
            confidence=float(raw.get("confidence", 0.0) or 0.0),
            boxes=[],
            seq=int(raw.get("seq", ev.get("seq", 0)) or 0),
            low_confidence=bool(raw.get("low_confidence", False)),
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
    note = ""
    raw_element = rec.get("raw_element_text", "") or ""
    if parser is not None and raw_element:
        parsed = parser.parse(raw_element)
        new_brand = getattr(parsed, "brand", "") or ""
        new_model = getattr(parsed, "model", "") or ""
        if (new_brand, new_model) != (brand, model):
            note = (
                f"申报侧刷新：品牌『{brand}』→『{new_brand}』、"
                f"型号『{model}』→『{new_model}』"
            )
        brand, model = new_brand, new_model

    record = DeclarationRecord(
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
    return record, note


_VERDICT_CN = {
    Verdict.PASS.value: "✅",
    Verdict.FAIL.value: "❌",
    Verdict.NO_MARK.value: "⚠️",
    Verdict.NO_IMAGE.value: "🔵",
}


def evaluate(legacy_ref: str = DEFAULT_LEGACY_REF) -> dict:
    """跑批并返回结构化对比结果（旧链路 / 新链路 / 快照记录三方对照）。"""
    snapshot = locate_snapshot()
    data = json.loads(snapshot.read_text(encoding="utf-8"))

    repo = RuleRepository()
    engine = JudgeEngine(rules=repo)
    legacy_module = load_legacy_engine(legacy_ref)
    legacy_engine = legacy_module.JudgeEngine(rules=repo)
    parser = ElementParser(rule_repository=repo)

    rows: list[dict] = []
    old_counts = {v.value: 0 for v in Verdict}
    new_counts = {v.value: 0 for v in Verdict}
    snap_counts = {v.value: 0 for v in Verdict}
    legacy_mismatch: list[int] = []

    for item in data.get("results", []) or []:
        record, refresh_note = build_record(item.get("record", {}), parser)
        old_result = legacy_engine.judge(record)
        new_result = engine.judge(record)
        old_verdict = old_result.verdict.value
        new_verdict = new_result.verdict.value
        snap_verdict = item.get("verdict", "")

        old_counts[old_verdict] = old_counts.get(old_verdict, 0) + 1
        new_counts[new_verdict] = new_counts.get(new_verdict, 0) + 1
        snap_counts[snap_verdict] = snap_counts.get(snap_verdict, 0) + 1
        if refresh_note == "" and old_verdict != snap_verdict:
            legacy_mismatch.append(record.seq_no)

        brand_match = new_result.token_match_for("品牌")
        model_match = new_result.token_match_for("型号")
        rows.append(
            {
                "seq_no": record.seq_no,
                "key": record.key(),
                "snapshot_verdict": snap_verdict,
                "old_verdict": old_verdict,
                "new_verdict": new_verdict,
                "changed": old_verdict != new_verdict,
                "refresh_note": refresh_note,
                "decl_brand": record.decl_brand,
                "decl_model": record.decl_model,
                "old_detected_brand": old_result.detected_brand,
                "old_detected_model": old_result.detected_model,
                "new_detected_brand": new_result.detected_brand,
                "new_detected_model": new_result.detected_model,
                "brand_match": brand_match.to_dict() if brand_match else None,
                "model_match": model_match.to_dict() if model_match else None,
                "old_reason": old_result.reason,
                "new_reason": new_result.reason,
                "old_diffs": [d.note or d.field for d in old_result.differences],
                "new_diffs": [d.note or d.field for d in new_result.differences],
            }
        )

    return {
        "snapshot": str(snapshot),
        "legacy_ref": legacy_ref,
        "records": len(rows),
        "old_counts": old_counts,
        "new_counts": new_counts,
        "snapshot_counts": snap_counts,
        "changed_count": sum(1 for r in rows if r["changed"]),
        "legacy_selfcheck_mismatch": legacy_mismatch,
        "rows": rows,
    }


def _short(text: str, limit: int = 34) -> str:
    """单行压缩 + 截断（打印表格用）。"""
    compact = " ".join((text or "").split())
    return compact if len(compact) <= limit else compact[:limit] + "…"


def _match_desc(match: dict | None) -> str:
    """把 TokenMatch 字典压成一行说明。"""
    if not match:
        return "-"
    mode = match.get("mode", "")
    if mode == "EXACT":
        return f"EXACT {match.get('token', '')} @{match.get('images')}"
    if mode == "FUZZY":
        return f"FUZZY {match.get('corrected_from', '')}→{match.get('token', '')}"
    return f"NONE {_short(match.get('note', ''), 20)}"


def print_report(report: dict) -> None:
    """打印逐条对比表 + 分布对照 + 旧侧自证。"""
    width = 150
    print("=" * width)
    print(f"SA26090215 新旧口径逐条对比（旧引擎 = {report['legacy_ref']}，证据取真跑批快照）")
    print(f"快照：{report['snapshot']}")
    print("=" * width)
    print(
        f"{'seq':>3} | {'快照':^4} | {'旧':^2} | {'新':^2} | {'申报品牌':<12} | {'申报型号':<14} "
        f"| {'品牌命中':<30} | {'型号命中':<30}"
    )
    print("-" * width)
    for row in report["rows"]:
        flag = "*" if row["changed"] else ("~" if row["refresh_note"] else " ")
        snap = _VERDICT_CN.get(row["snapshot_verdict"], "?")
        print(
            f"{row['seq_no']:>3} | {snap:^4} | {_VERDICT_CN.get(row['old_verdict'], '?'):^2} "
            f"| {_VERDICT_CN.get(row['new_verdict'], '?'):^2} "
            f"| {row['decl_brand']:<12} | {row['decl_model']:<14} "
            f"| {_match_desc(row['brand_match']):<30} | {_match_desc(row['model_match']):<30} {flag}"
        )

    print("-" * width)
    old, new, snap = report["old_counts"], report["new_counts"], report["snapshot_counts"]
    fmt = lambda c: (  # noqa: E731 - 局部格式化
        f"✅{c.get('PASS', 0)} / ❌{c.get('FAIL', 0)} / ⚠️{c.get('NO_MARK', 0)} / 🔵{c.get('NO_IMAGE', 0)}"
    )
    print(f"快照记录   ：{fmt(snap)}")
    print(f"旧链路复算 ：{fmt(old)}")
    print(f"新链路     ：{fmt(new)}")
    print(f"口径变更条数：{report['changed_count']} / {report['records']}")
    if report["legacy_selfcheck_mismatch"]:
        print(
            f"⚠️ 旧链路复算与快照判定在 {report['legacy_selfcheck_mismatch']} 上不一致"
            "（这些记录申报值未刷新，需排查）"
        )
    else:
        print("✅ 旧链路自证：申报值未变的记录上，复算判定与快照判定**完全一致**")
    print("=" * width)

    for row in report["rows"]:
        if not row["changed"]:
            continue
        print(
            f"\n[seq={row['seq_no']}] {_VERDICT_CN.get(row['old_verdict'])} → "
            f"{_VERDICT_CN.get(row['new_verdict'])}  {row['key']}"
        )
        print(f"   申报：品牌『{row['decl_brand']}』型号『{row['decl_model']}』")
        print(f"   旧：{_short(row['old_reason'], 160)}")
        print(f"      识别值：品牌『{row['old_detected_brand']}』型号『{row['old_detected_model']}』")
        print(f"   新：{_short(row['new_reason'], 160)}")
        print(f"      识别值：品牌『{row['new_detected_brand']}』型号『{row['new_detected_model']}』")

    for row in report["rows"]:
        if row["refresh_note"]:
            print(f"\n[申报侧刷新] seq={row['seq_no']}：{row['refresh_note']}")


def main() -> int:
    """入口。"""
    parser = argparse.ArgumentParser(description="SA26090215 新旧口径对比")
    parser.add_argument("--json", help="把结构化结果写入指定文件")
    parser.add_argument("--fingerprint", action="store_true", help="附带原始输入指纹校验")
    parser.add_argument("--legacy-ref", default=DEFAULT_LEGACY_REF, help="旧引擎 git 引用")
    args = parser.parse_args()

    report = evaluate(args.legacy_ref)
    print_report(report)

    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n结果已写入：{args.json}")

    if args.fingerprint:
        count, total, base = input_fingerprint()
        ok = count == EXPECTED_INPUT_FILES and total == EXPECTED_INPUT_BYTES
        print(
            f"\n[原始输入指纹] {base}\n  实测：{count} 文件 / {total:,} 字节\n"
            f"  基线：{EXPECTED_INPUT_FILES} 文件 / {EXPECTED_INPUT_BYTES:,} 字节\n"
            f"  结论：{'✅ 一致（原始输入未被改动）' if ok else '❌ 不一致，请核查'}"
        )
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
