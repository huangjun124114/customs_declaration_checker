"""图片三级索引匹配（core.image_resolver，对应架构设计 6.4 / 9.2 / 12.D.3）。

职责：把一条 :class:`core.models.DeclarationRecord`（票号 / 料号 / 订单号）
在共享图片根目录下解析为一批 :class:`core.models.ImageEvidence`。

**三级索引**（架构设计 6.1 / SOP 3.2）：
  * 一级 ``ticket_no``  → 定位 ``{票号}`` 目录；
  * 二级 ``part_no``    → 精确锁定图片文件名**第二段**（``&{B}&``）；
  * 三级 ``order_no``   → **二次筛选**文件名**首段**（``{A}&``）与子目录名，
    解决「同一子目录下含多订单」的串货问题（实测 ``2660310M`` 下含 5 个订单）。

⚠️ **P4 语义交叉铁律**（架构设计 6.2 / 6.3）：
  Excel「订单号」列值 = 图片**子目录名 / 文件名首段**（形如 ``2660310M``）；
  Excel「物料编号」列值 = 图片**文件名第二段**（形如 ``N011901-009350-001``）。
  **按列名字面理解（用物料编号去匹配目录名）会 0 命中**。本模块严格按此语义实现。

**四级降级链**（架构设计 6.4）：
  ① 料号目录**精确**匹配 → ② 料号目录**模糊**匹配（剥离 ``图片`` 等后缀）
  → ③ 子目录扫描（``{订单号}-0-{箱数}`` 形态）→ ④ 票号根目录解析
  每级均记 DEBUG 日志，便于排错。

**读图失败区分**（架构设计 12.D.3，两个**互斥**标记）：
  * 「**共享根不可达**」（断连 / 权限不足）→ ``unreachable=True``
    —— 上层应触发**自动暂停**（连续 K 条），**不是**普通缺图；
  * 「**目录确实不存在**」（共享根可达，但票号/料号/订单目录不存在）
    → ``not_found=True`` —— 上层判 ``NO_IMAGE`` 🔵 并**正常继续**。
  二者语义不同，**互斥**；``unreachable=True`` 时 ``not_found`` 恒为 ``False``。

**只读红线**：本模块**只列举与探测**，绝不复制 / 移动 / 改名 / 写入任何图片。

分层：``core`` 只能 import ``core`` / ``infra``，**禁止** import PySide6 / PyQt。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from core.models import DeclarationRecord, ImageEvidence
from infra.encoding import normalize_path
from infra.logger import Phase, get_logger

__all__ = [
    "IMAGE_SUFFIXES",
    "ImageResolver",
    "parse_image_file_name",
    "strip_part_dir_suffix",
]

#: 视为图片的文件后缀（小写比较）。
IMAGE_SUFFIXES: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
)

#: ``{A}&{B}&{C}`` 主模式（与 ``core.constants.DEFAULT_FILE_NAME_PATTERN`` 同源）。
_MAIN_PATTERN = re.compile(r"^(?P<a>[^&]+)&(?P<b>[^&]+)&(?P<c>\d+)$")

#: 宽松模式：允许缺第三段（``{A}&{B}``）或非标（``{料号} (n)``）。
_RELAXED_PATTERN = re.compile(r"^(?P<a>[^&]+)&(?P<b>[^&]+)(?:&(?P<c>\d*))?$")

#: 料号目录需剥离的后缀（实测 ``2660308M图片``；与 ``constants.PART_DIR_SUFFIXES`` 对齐）。
_DIR_SUFFIX_RE = re.compile(r"(图片|图)$|-\d+-?$|_\d+_$|[（(]\d+[)）]$")

#: 子目录兜底模式：``{订单号}-0-{箱数}``（架构设计 6.4 降级链③）。
_SUBDIR_PATTERN = re.compile(r"^(?P<order>[^\-]+)-0[-_]?(?P<box>\d*)$")


def strip_part_dir_suffix(name: str) -> str:
    """剥离目录名后缀，得到可比对的订单号。

    实测：``2660308M图片`` → ``2660308M``。剥离 ``图片`` / ``图`` / ``-0-xx`` /
    ``_0_`` / ``(n)`` 等后缀（架构设计 6.4 容错）。

    Args:
        name: 目录名（不含路径）。

    Returns:
        剥离后缀后的基础名（首尾空白已去除）。
    """
    base = (name or "").strip()
    # 先剥离明确的「图片」类后缀（可能重复出现）
    while True:
        stripped = _DIR_SUFFIX_RE.sub("", base).strip()
        if stripped == base:
            break
        base = stripped
    return base


def parse_image_file_name(file_name: str) -> tuple[str, str, int] | None:
    """解析图片文件名 ``{A}&{B}&{C}.jpg``。

    实测：``2660310M&N011901-009350-001&001.jpg`` →
    ``("2660310M", "N011901-009350-001", 1)``。

    Args:
        file_name: 文件名（含扩展名，可含路径——内部只取 ``name``）。

    Returns:
        ``(A, B, C)`` 三元组；``A`` = 订单号，``B`` = 料号，``C`` = 序号（int）。
        解析不出返回 ``None``（调用方归入所属订单组并记 WARN）。
    """
    stem = Path(file_name).stem.strip()
    if not stem:
        return None

    main = _MAIN_PATTERN.match(stem)
    if main:
        order = main.group("a").strip()
        part = main.group("b").strip()
        try:
            seq = int(main.group("c"))
        except (TypeError, ValueError):
            seq = 0
        return order, part, seq

    relaxed = _RELAXED_PATTERN.match(stem)
    if relaxed:
        order = relaxed.group("a").strip()
        part = relaxed.group("b").strip()
        raw_seq = relaxed.group("c")
        seq = int(raw_seq) if raw_seq else 0
        return order, part, seq

    return None


class ImageResolver:
    """三级索引图片解析器（架构设计第 4 节 ``ImageResolver``）。

    Args:
        ocr_engine: 预留的 OCR 编排器（本模块**不使用** OCR，仅探测与匹配；
            保留参数以对齐类图并便于上层组合，默认为 ``None``）。
        unreachable_probe: 判断「目录不可达」的钩子（**供测试注入**）；
            默认用 ``os.scandir`` 试读，抛 ``OSError`` 视为不可达。
    """

    def __init__(
        self,
        ocr_engine: object | None = None,
        *,
        unreachable_probe: object | None = None,
    ) -> None:
        self._ocr_engine = ocr_engine
        self._unreachable_probe = unreachable_probe
        self._log = get_logger(Phase.PHASE3)

    # ══════════════════════════════════════════════════════════
    #  公共入口
    # ══════════════════════════════════════════════════════════

    def resolve(
        self,
        record: DeclarationRecord,
        share_root: str | os.PathLike[str],
        ticket_no: str = "",
    ) -> list[ImageEvidence]:
        """解析一条记录的图片证据（**失败返回空列表，不抛异常**）。

        契约（架构设计第 4 节）：失败返回空列表（**不抛**），由 Judge 判 ``NO_IMAGE``。

        Args:
            record: 申报记录（提供 ``part_no`` / ``order_no``）。
            share_root: 图片根目录（可为本机票号目录或 UNC）。
            ticket_no: 票号；缺省时取 ``record.ticket_no``。

        Returns:
            :class:`core.models.ImageEvidence` 列表；按序号升序。
            未匹配到任何图片返回 ``[]``（Judge 判 🔵 缺图）。
        """
        ticket = (ticket_no or record.ticket_no or "").strip()
        part = (record.part_no or "").strip()
        order = (record.order_no or "").strip()

        root = normalize_path(share_root)

        # ① 先判「共享根」本身是否可达（断连 / 权限）—— 这是 unreachable 的唯一触发条件。
        #    共享根不可达 → unreachable=True（上层可据此触发自动暂停，架构设计 12.D.3）。
        if not self._is_reachable(root):
            self._log.warning(f"共享根不可达（断连或权限不足）：{root}")
            return [self._unreachable_evidence(root, part)]

        # ② 共享根可达，再定位票号目录；票号/料号/订单目录不存在 → not_found（正常继续）。
        base = self._locate_ticket_dir(root, ticket)
        if not self._is_reachable(base):
            self._log.info(f"票号目录不存在：{base}（共享根可达，判缺图）")
            return [self._not_found_evidence(base, part)]

        # ── 四级降级链 ──
        evidences = self._level1_exact_part_dir(base, part, order)
        if evidences:
            return evidences

        evidences = self._level2_fuzzy_part_dir(base, part, order)
        if evidences:
            return evidences

        evidences = self._level3_scan_subfolders(base, part, order)
        if evidences:
            return evidences

        evidences = self._level4_scan_root(base, part, order)
        if evidences:
            return evidences

        self._log.info(f"未匹配到图片：票号={ticket or '-'} 料号={part or '-'} 订单={order or '-'}")
        return []

    # ══════════════════════════════════════════════════════════
    #  目录定位与可达性
    # ══════════════════════════════════════════════════════════

    def _locate_ticket_dir(self, share_root: Path, ticket: str) -> Path:
        """定位 ``{票号}`` 目录。

        若 ``share_root`` 本身已是票号目录（末尾段 == 票号）则直接使用；
        否则在 ``share_root`` 下查找 ``{票号}`` 子目录（精确 → 忽略大小写）。

        Args:
            share_root: 图片根目录。
            ticket: 票号。

        Returns:
            票号目录（可能不存在，由调用方做可达性判断）。
        """
        if not ticket:
            return share_root

        if share_root.name.strip().upper() == ticket.upper():
            return share_root

        candidate = share_root / ticket
        if candidate.is_dir():
            return candidate

        # 忽略大小写再找一次
        for child in self._safe_scandir(share_root):
            if child.is_dir() and child.name.strip().upper() == ticket.upper():
                return child
        return candidate

    def _is_reachable(self, path: Path) -> bool:
        """判断目录是否可达（区别于"不存在"）。

        规则（架构设计 12.D.3）：
          * ``os.scandir`` 取首项试读；抛 ``PermissionError`` / ``OSError``
            （含 UNC 断连）→ **不可达**；
          * 目录不存在 → 也视为此处返回 ``False``（**由调用方决定**语义：
            对「共享根」判 ``unreachable``，对「票号目录」判 ``not_found``）。

        Args:
            path: 目录路径。

        Returns:
            ``True`` 表示可达（存在且可列举）。
        """
        probe = self._unreachable_probe
        if callable(probe):
            return bool(probe(path))  # type: ignore[operator]

        if not path.exists():
            return False
        try:
            with os.scandir(path) as it:
                next(it, None)  # 试取首项，触发权限/连接错误
        except OSError:
            return False
        return True

    def _safe_scandir(self, path: Path) -> list[Path]:
        """安全列举目录条目（不抛异常；不可达返回空列表）。

        Args:
            path: 目录路径。

        Returns:
            子路径列表（排序后，稳定可测）。
        """
        try:
            with os.scandir(path) as it:
                entries = [Path(entry.path) for entry in it]
        except OSError:
            return []
        return sorted(entries, key=lambda p: p.name)

    @staticmethod
    def _unreachable_evidence(base: Path, part: str) -> ImageEvidence:
        """构造「共享根不可达」占位证据（``unreachable=True``，架构设计 12.D.3）。

        语义：共享盘断连 / 权限不足 → 上层可触发**自动暂停**。与 ``not_found`` 互斥。

        Args:
            base: 共享根路径。
            part: 料号（仅作占位记载，未使用）。

        Returns:
            ``unreachable=True, not_found=False`` 的占位证据。
        """
        return ImageEvidence(
            image_path=str(base),
            seq=0,
            exists=False,
            ocr_confidence=0.0,
            unreachable=True,
            not_found=False,
            ocr=None,
        )

    @staticmethod
    def _not_found_evidence(base: Path, part: str) -> ImageEvidence:
        """构造「目录确实不存在」占位证据（``not_found=True``）。

        语义：共享根可达，但票号/料号/订单目录不存在 → 上层判 ``NO_IMAGE`` 🔵
        **正常继续**（**不**触发自动暂停）。与 ``unreachable`` 互斥。

        Args:
            base: 期望但缺失的目录路径。
            part: 料号（仅作占位记载，未使用）。

        Returns:
            ``not_found=True, unreachable=False`` 的占位证据。
        """
        return ImageEvidence(
            image_path=str(base),
            seq=0,
            exists=False,
            ocr_confidence=0.0,
            unreachable=False,
            not_found=True,
            ocr=None,
        )

    # ══════════════════════════════════════════════════════════
    #  降级链①：料号目录精确匹配
    # ══════════════════════════════════════════════════════════

    def _level1_exact_part_dir(self, base: Path, part: str, order: str) -> list[ImageEvidence]:
        """① 料号目录**精确**匹配。

        优先按 ``order``（= 图片目录名的内部语义）找目录；若 ``order`` 为空或
        找不到，则退化用 ``part`` 找目录名。命中后在该目录内按
        ``A==order & B==part`` 二次筛选。

        Args:
            base: 票号目录。
            part: 料号（文件名第二段）。
            order: 订单号（文件名首段 / 目录名）。

        Returns:
            证据列表（未命中为空）。
        """
        for name in self._candidate_dir_names(part, order):
            exact = base / name
            if exact.is_dir():
                evidences = self._collect(exact, part, order, level="L1-exact")
                if evidences:
                    self._log.debug(f"降级链①料号目录精确命中：{exact.name}（{len(evidences)} 张）")
                    return evidences
        return []

    def _candidate_dir_names(self, part: str, order: str) -> list[str]:
        """生成「料号目录」候选名（去重保序）。

        内部语义映射（P4）：目录名 ≈ ``order``（订单号）；``part`` 作为次选兜底。

        Args:
            part: 料号。
            order: 订单号。

        Returns:
            候选目录名列表。
        """
        names: list[str] = []
        for raw in (order, part):
            token = (raw or "").strip()
            if token and token not in names:
                names.append(token)
        return names

    # ══════════════════════════════════════════════════════════
    #  降级链②：料号目录模糊匹配（剥离「图片」等后缀）
    # ══════════════════════════════════════════════════════════

    def _level2_fuzzy_part_dir(self, base: Path, part: str, order: str) -> list[ImageEvidence]:
        """② 料号目录**模糊**匹配：剥离 ``图片`` / ``-0-xx`` / ``(n)`` 后缀后比对。

        实测关键场景：``2660308M图片`` → 剥离后 ``2660308M`` 命中。

        Args:
            base: 票号目录。
            part: 料号。
            order: 订单号。

        Returns:
            证据列表（未命中为空）。
        """
        wanted = {(order or "").strip().upper(), (part or "").strip().upper()}
        wanted.discard("")

        for child in self._safe_scandir(base):
            if not child.is_dir():
                continue
            stripped = strip_part_dir_suffix(child.name).upper()
            if stripped in wanted:
                evidences = self._collect(child, part, order, level="L2-fuzzy")
                if evidences:
                    self._log.debug(
                        f"降级链②目录模糊命中：{child.name}（剥离后 {stripped}，{len(evidences)} 张）"
                    )
                    return evidences
        return []

    # ══════════════════════════════════════════════════════════
    #  降级链③：子目录扫描（{订单号}-0-{箱数}）
    # ══════════════════════════════════════════════════════════

    def _level3_scan_subfolders(self, base: Path, part: str, order: str) -> list[ImageEvidence]:
        """③ 子目录扫描：递归下钻一层，找 ``{订单号}-0-{箱数}`` 形态目录。

        Args:
            base: 票号目录。
            part: 料号。
            order: 订单号。

        Returns:
            证据列表（未命中为空）。
        """
        if not order:
            return []

        for child in self._safe_scandir(base):
            if not child.is_dir():
                continue
            stripped = strip_part_dir_suffix(child.name)
            matched = _SUBDIR_PATTERN.match(stripped)
            target = matched.group("order").strip() if matched else stripped
            if target.upper() != order.strip().upper():
                continue
            evidences = self._collect_recursive(child, part, order, level="L3-subdir")
            if evidences:
                self._log.debug(
                    f"降级链③子目录扫描命中：{child.name}（{len(evidences)} 张）"
                )
                return evidences
        return []

    # ══════════════════════════════════════════════════════════
    #  降级链④：票号根目录解析
    # ══════════════════════════════════════════════════════════

    def _level4_scan_root(self, base: Path, part: str, order: str) -> list[ImageEvidence]:
        """④ 票号根目录解析：在票号目录（含一层子目录）内按文件名三段匹配。

        这是最后兜底：只要文件名 ``A``/``B`` 段能对上，无论它在哪个子目录都收。

        Args:
            base: 票号目录。
            part: 料号。
            order: 订单号。

        Returns:
            证据列表（未命中为空）。
        """
        evidences = self._collect_recursive(base, part, order, level="L4-root")
        if evidences:
            self._log.debug(f"降级链④票号根目录解析命中（{len(evidences)} 张）")
        return evidences

    # ══════════════════════════════════════════════════════════
    #  收集与二次筛选（核心）
    # ══════════════════════════════════════════════════════════

    def _collect(
        self,
        directory: Path,
        part: str,
        order: str,
        *,
        level: str,
    ) -> list[ImageEvidence]:
        """在单个目录内收集图片（**叠加订单号二次筛选**，不递归）。

        Args:
            directory: 目标目录。
            part: 料号（文件名第二段，精确锁定）。
            order: 订单号（文件名首段，二次筛选）。
            level: 降级层级标签（仅日志用）。

        Returns:
            证据列表（按序号升序；无命中为空列表）。
        """
        matches: list[tuple[int, str]] = []
        for entry in self._safe_scandir(directory):
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            parsed = parse_image_file_name(entry.name)
            if parsed is None:
                # 非标文件名：若唯一标识（part）能子串命中，则归入该订单组并记 WARN
                if part and part.upper() in entry.name.upper():
                    matches.append((0, str(entry)))
                    self._log.warning(f"非标图片文件名（已归入料号组）：{entry.name}")
                continue
            file_order, file_part, seq = parsed
            if self._match_segments(file_order, file_part, order, part):
                matches.append((seq, str(entry)))

        matches.sort(key=lambda item: (item[0], item[1]))
        return [self._make_evidence(path_str, seq) for seq, path_str in matches]

    def _collect_recursive(
        self,
        directory: Path,
        part: str,
        order: str,
        *,
        level: str,
    ) -> list[ImageEvidence]:
        """递归收集（下钻一层子目录）图片证据。

        Args:
            directory: 起始目录。
            part: 料号。
            order: 订单号。
            level: 降级层级标签（仅日志用）。

        Returns:
            证据列表（按序号升序）。
        """
        evidences = self._collect(directory, part, order, level=level)
        for child in self._safe_scandir(directory):
            if child.is_dir():
                evidences.extend(self._collect(child, part, order, level=level))
        evidences.sort(key=lambda e: (e.seq, e.image_path))
        return evidences

    @staticmethod
    def _match_segments(
        file_order: str,
        file_part: str,
        order: str,
        part: str,
    ) -> bool:
        """判定文件名 ``A``/``B`` 段是否匹配记录。

        **二次筛选铁律**：只要记录提供了 ``order``，则 ``A`` 段必须等于 ``order``
        （避免同料号跨订单串货）；``part`` 提供时 ``B`` 段必须等于 ``part``。

        Args:
            file_order: 文件名首段（订单号）。
            file_part: 文件名第二段（料号）。
            order: 记录订单号。
            part: 记录料号。

        Returns:
            ``True`` 表示匹配。
        """
        want_order = (order or "").strip().upper()
        want_part = (part or "").strip().upper()

        if want_order and file_order.strip().upper() != want_order:
            return False
        if want_part and file_part.strip().upper() != want_part:
            return False
        # 两者都为空 → 无有效筛选条件，视为不匹配（避免误收全票）
        return bool(want_order or want_part)

    def _make_evidence(self, path_str: str, seq: int) -> ImageEvidence:
        """构造一条图片证据（探测存在性与可达性）。

        Args:
            path_str: 图片路径字符串。
            seq: 图片序号。

        Returns:
            :class:`core.models.ImageEvidence`。
        """
        p = Path(path_str)
        exists = False
        unreachable = False
        try:
            exists = p.is_file()
        except OSError:
            unreachable = True
        return ImageEvidence(
            image_path=path_str,
            seq=seq,
            exists=exists,
            ocr_confidence=0.0,
            unreachable=unreachable,
            ocr=None,
        )
