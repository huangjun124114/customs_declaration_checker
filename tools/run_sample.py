"""真实样本端到端跑批（headless，不启动 Qt）—— v0.3.0 批次 3 / 未来回归复用。

**为什么需要它**：``docs/10_迭代方案_v0.3_0916.md`` 的验收标准要求「真实样本端到端
复跑（口径分布 + 产物三件套）」；GUI 跑批无法在 CI / 无人值守环境复现。本脚本直接调用
:class:`app.check_task.CheckTask`（与 GUI 同一条编排路径），把结果打印为可粘贴进
``CHANGELOG.md`` 的表格，并按需落一份 JSON。

**红线**：
  * ``原始输入`` 全程**只读**（不写、不改名、不移动）；
  * 输出目录遵循 v0.2.0 运行目录约定 ``<base>/报关申报要素校验/{result,logs}``，
    已存在则复用，**绝不清理**；
  * ``--check-input`` 只算指纹，不跑批（用于跑批前后比对）。

用法::

    python tools/run_sample.py                      # 自动找样本并全量跑批
    python tools/run_sample.py --check-input        # 仅打印原始输入指纹
    python tools/run_sample.py --base <锚点目录>     # 覆盖输出锚点
    python tools/run_sample.py --json <路径>         # 落分布 JSON
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

if __package__ in (None, ""):  # 允许 `python tools/run_sample.py` 直接执行
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.check_task import CheckTask  # noqa: E402
from app.events import TaskPhase  # noqa: E402
from core.constants import ALL_VERDICTS, VERDICT_TEXT  # noqa: E402

#: 默认项目空间（仓库的上一级）；可用 ``--space`` 覆盖
DEFAULT_SPACE = Path(__file__).resolve().parent.parent.parent

#: 申报要素 Excel 名关键字
_EXCEL_KEYWORD = "申报要素"

#: 票号形态（``SA26090215`` 之类；与 ``core.constants.TICKET_NO_PATTERN`` 同族）
_TICKET_RE = re.compile(r"\b([A-Z]{2}\d{8})\b", re.IGNORECASE)


def find_excel(input_dir: Path) -> Path:
    """在 ``原始输入`` 下找**最新的**含「申报要素」的 xlsx。

    Args:
        input_dir: ``原始输入`` 目录。

    Returns:
        Excel 路径。

    Raises:
        SystemExit: 找不到时。
    """
    if not input_dir.is_dir():
        sys.exit(f"[x] 原始输入目录不存在：{input_dir}")
    candidates = [
        p
        for p in input_dir.rglob("*.xlsx")
        if _EXCEL_KEYWORD in p.name and not p.name.startswith("~$")
    ]
    if not candidates:
        sys.exit(f"[x] 未在 {input_dir} 下找到含「{_EXCEL_KEYWORD}」的 xlsx")
    candidates.sort(key=lambda p: (p.stat().st_mtime, p.name))
    return candidates[-1]


def infer_ticket(excel: Path, input_dir: Path) -> str:
    """推断票号（文件名优先，其次同级目录名）。

    Args:
        excel: Excel 路径。
        input_dir: ``原始输入`` 目录（其下的票号子目录是第二线索）。

    Returns:
        票号；推断不出时返回空串。
    """
    match = _TICKET_RE.search(excel.name)
    if match:
        return match.group(1).upper()
    for child in sorted(input_dir.iterdir()):
        if child.is_dir() and _TICKET_RE.fullmatch(child.name.strip()):
            return child.name.strip().upper()
    return ""


def input_fingerprint(root: Path) -> dict[str, object]:
    """统计某个目录的文件数 / 总字节 / 最大 mtime（只读，用于跑批前后比对）。"""
    files = 0
    total = 0
    newest = 0.0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        files += 1
        total += stat.st_size
        newest = max(newest, stat.st_mtime)
    return {"dir": str(root), "files": files, "bytes": total, "newest_mtime": newest}


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="真实样本端到端跑批（headless）")
    parser.add_argument("--space", default=str(DEFAULT_SPACE), help="项目空间根目录")
    parser.add_argument("--excel", default="", help="申报要素 Excel（缺省自动探测）")
    parser.add_argument("--share", default="", help="图片根目录（缺省 = 原始输入）")
    parser.add_argument("--ticket", default="", help="票号（缺省自动推断）")
    parser.add_argument("--base", default="", help="输出锚点（缺省 = 项目空间根）")
    parser.add_argument("--json", default="", help="把分布结果写入该 JSON")
    parser.add_argument("--check-input", action="store_true", help="仅打印原始输入指纹")
    parser.add_argument("--quiet", action="store_true", help="不逐条打印进度")
    args = parser.parse_args(argv)

    space = Path(args.space).resolve()
    input_dir = space / "原始输入"

    if args.check_input:
        print(json.dumps(input_fingerprint(input_dir), ensure_ascii=False, indent=2))
        return 0

    excel = Path(args.excel).resolve() if args.excel else find_excel(input_dir)
    share = Path(args.share).resolve() if args.share else input_dir
    ticket = (args.ticket or infer_ticket(excel, input_dir)).strip()
    base = Path(args.base).resolve() if args.base else space

    before = input_fingerprint(input_dir)
    print(f"[i] Excel    ：{excel}")
    print(f"[i] 图片根   ：{share}")
    print(f"[i] 票号     ：{ticket or '（自动推断）'}")
    print(f"[i] 输出锚点 ：{base}")
    print(f"[i] 输入指纹 ：{before['files']} 文件 / {before['bytes']} 字节")

    task = CheckTask()
    started = time.perf_counter()

    def _on_progress(progress) -> None:
        if args.quiet:
            return
        if progress.phase in (TaskPhase.OCR, TaskPhase.EXPORT, TaskPhase.PARSE):
            print(f"    · {progress.message}")

    outcome = task.run(
        excel_path=excel,
        share_root=share,
        result_dir="",
        process_dir="",
        ticket_no=ticket,
        progress_cb=_on_progress,
    )
    elapsed = time.perf_counter() - started

    print("\n=========== 跑批结果 ===========")
    print(f"ok={outcome.ok} failed={outcome.failed} aborted={outcome.aborted}")
    print(f"message={outcome.message}")
    print(f"elapsed={elapsed:.1f}s  total={outcome.total} processed={outcome.processed}")
    print("\n四类分布：")
    for verdict in ALL_VERDICTS:
        count = int(outcome.counts.get(verdict.value, 0))
        print(f"  {VERDICT_TEXT.get(verdict, verdict.value)} : {count}")
    print("\n产物：")
    for name, path in (outcome.output_paths or {}).items():
        exists = Path(path).exists()
        print(f"  {name}: {path}  {'✓' if exists else '✗ 缺失'}")

    after = input_fingerprint(input_dir)
    unchanged = before["files"] == after["files"] and before["bytes"] == after["bytes"]
    print(f"\n原始输入指纹：{'未改动 ✓' if unchanged else '⚠️ 已变化'}"
          f"（{after['files']} 文件 / {after['bytes']} 字节）")

    if args.json:
        payload = {
            "ticket_no": outcome.ticket_no,
            "elapsed_sec": round(elapsed, 1),
            "total": outcome.total,
            "processed": outcome.processed,
            "counts": {k: int(v) for k, v in (outcome.counts or {}).items()},
            "output_paths": {k: str(v) for k, v in (outcome.output_paths or {}).items()},
            "input_files": after["files"],
            "input_bytes": after["bytes"],
        }
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[i] 分布 JSON 已写入：{args.json}")

    return 0 if outcome.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
