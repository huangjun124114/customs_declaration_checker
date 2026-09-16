"""图片倒排索引（core.image_index，对应 v0.2.0 方案 §3.1 / §3.3「点 7」）。

**目的**：把「每条记录都重新扫描 ``{票号}`` 目录」改为「**单次扫描**建索引 + O(1) 反查」，
在**不改变任何判定证据范围**的前提下省去重复扫描。

**索引结构**（一次扫描 ``{票号}`` 目录得到）::

    by_order: {订单号: [ImageRef, ...]}     # 文件名首段（A 段）索引
    by_part:  {物料编号: [ImageRef, ...]}   # 文件名第二段（B 段）索引
    dirs:     {目录名: Path}                # 顶层子目录

**命中集合等价红线**（最高优先级验收）：
  * 本模块的 :meth:`ImageIndex.resolve` **逐条等价**复现
    :class:`core.image_resolver.ImageResolver` 的四级降级链结果
    （同层级优先级、同二次筛选、同排序）——由 ``tests/test_image_index.py`` 断言；
  * 索引**只允许省去重复扫描**，**绝不允许**改变证据范围。

⚠️ **P4 语义交叉铁律**：文件名 ``{订单号}&{物料编号}&{序号}.jpg`` —— 首段=订单号、
第二段=物料编号。**按 Excel 列名字面理解（用物料编号匹配目录名）会 0 命中**。
本模块严格复用 :func:`core.image_resolver.parse_image_file_name`，**不**按列名臆测。

分层：``core`` 只能 import ``core`` / ``infra``，**禁止** import PySide6 / PyQt。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from core.image_resolver import (
    _SUBDIR_PATTERN,
    IMAGE_SUFFIXES,
    candidate_dir_names,
    match_segments,
    parse_image_file_name,
    strip_part_dir_suffix,
)
from core.models import ImageEvidence
from infra.encoding import normalize_path

__all__ = ["ImageIndex", "ImageRef"]


@dataclass(frozen=True)
class ImageRef:
    """索引中的一条图片引用（已解析文件名三段）。

    Attributes:
        path: 图片绝对路径（字符串）。
        name: 文件名。
        order: 文件名首段（订单号）；非标文件为空串。
        part: 文件名第二段（物料编号）；非标文件为空串。
        seq: 文件名序号；非标/无序号为 0。
        standard: 是否符合 ``{A}&{B}&{C}`` 标准形态（``False`` 为宽松/非标）。
    """

    path: str
    name: str
    order: str = ""
    part: str = ""
    seq: int = 0
    standard: bool = True


@dataclass
class _DirSnapshot:
    """目录快照（单次扫描得到，供离线索引复现四级链）。"""

    path: Path
    name: str
    files: list[ImageRef] = field(default_factory=list)
    children: list[_DirSnapshot] = field(default_factory=list)


def _snapshot_dir(directory: Path) -> _DirSnapshot:
    """递归快照目录（**单次** ``scandir``；子项按名排序，与 ``_safe_scandir`` 一致）。

    Args:
        directory: 目录路径。

    Returns:
        :class:`_DirSnapshot`；不可达目录返回空快照。
    """
    files: list[ImageRef] = []
    children: list[_DirSnapshot] = []

    try:
        with os.scandir(directory) as it:
            entries = sorted(it, key=lambda entry: entry.name)
    except OSError:
        entries = []  # type: ignore[assignment]

    for entry in entries:
        try:
            is_dir = entry.is_dir()
            is_file = entry.is_file()
        except OSError:
            continue
        if is_dir:
            children.append(_snapshot_dir(Path(entry.path)))
        elif is_file:
            suffix = os.path.splitext(entry.name)[1].lower()
            if suffix not in IMAGE_SUFFIXES:
                continue
            parsed = parse_image_file_name(entry.name)
            if parsed is None:
                files.append(ImageRef(path=str(entry.path), name=entry.name, standard=False))
            else:
                order, part, seq = parsed
                files.append(
                    ImageRef(
                        path=str(entry.path),
                        name=entry.name,
                        order=order,
                        part=part,
                        seq=seq,
                        standard=True,
                    )
                )

    return _DirSnapshot(path=directory, name=directory.name, files=files, children=children)


class ImageIndex:
    """``{票号}`` 目录的图片倒排索引（单次扫描 + O(1) 反查）。

    Args:
        ticket_dir: 票号目录。
    """

    def __init__(self, ticket_dir: str | os.PathLike[str]) -> None:
        self.ticket_dir: Path = normalize_path(ticket_dir)
        self.built: bool = False
        self.by_order: dict[str, list[ImageRef]] = {}
        self.by_part: dict[str, list[ImageRef]] = {}
        self.dirs: dict[str, Path] = {}
        self._nonstandard: list[ImageRef] = []
        self._root: _DirSnapshot | None = None
        self._build()

    # ───────────────────── 构建 ─────────────────────

    def _build(self) -> None:
        """单次扫描构建快照 + 倒排表。"""
        try:
            root = _snapshot_dir(self.ticket_dir)
        except OSError:
            self.built = False
            return

        if not root.path.exists():
            self.built = False
            return

        self._root = root

        def walk(node: _DirSnapshot, top_level: bool) -> None:
            for ref in node.files:
                if ref.standard:
                    self.by_order.setdefault(ref.order.strip().upper(), []).append(ref)
                    self.by_part.setdefault(ref.part.strip().upper(), []).append(ref)
                else:
                    self._nonstandard.append(ref)
            for child in node.children:
                if top_level:
                    self.dirs.setdefault(strip_part_dir_suffix(child.name).upper(), child.path)
                    self.dirs.setdefault(child.name.upper(), child.path)
                walk(child, False)

        walk(root, True)

        for refs in self.by_order.values():
            refs.sort(key=lambda r: (r.seq, r.path))
        for refs in self.by_part.values():
            refs.sort(key=lambda r: (r.seq, r.path))

        self.built = True

    # ───────────────────── O(1) 反查（方案 §3.3 指定 API）─────────────────────

    def lookup(self, order_no: str, part_no: str) -> list[ImageRef]:
        """按键反查候选图片（O(1) 定位后过滤）。

        规则：有订单号 → ``by_order[订单号]`` 再按 ``part_no`` 过滤；
        无订单号（变体 E）→ ``by_part[物料编号]`` 再按 ``order_no`` 过滤。

        ⚠️ 这是**跨目录全局**反查（取并集），与四级链的**层级优先级**结果可能不同；
        判定证据请用 :meth:`resolve`（逐条等价复现四级链）。

        Args:
            order_no: 订单号（文件名首段）。
            part_no: 物料编号（文件名第二段）。

        Returns:
            命中的 :class:`ImageRef` 列表（按 ``(seq, path)`` 升序）。
        """
        o = (order_no or "").strip().upper()
        p = (part_no or "").strip().upper()

        if o and o in self.by_order:
            refs = list(self.by_order[o])
        elif p and p in self.by_part:
            refs = list(self.by_part[p])
        elif p:
            refs = [r for r in self._nonstandard if p in r.name.upper()]
        else:
            return []

        out: list[ImageRef] = []
        for ref in refs:
            if o and ref.order.strip().upper() != o:
                continue
            if p and ref.part.strip().upper() != p:
                continue
            out.append(ref)
        out.sort(key=lambda r: (r.seq, r.path))
        return out

    def has_any_candidate(self, order_no: str, part_no: str) -> bool:
        """是否**任何位置**存在可能匹配的图片（用于 O(1) 快速否决）。"""
        o = (order_no or "").strip().upper()
        p = (part_no or "").strip().upper()
        if o and o in self.by_order:
            return True
        if p and p in self.by_part:
            return True
        if p and any(p in ref.name.upper() for ref in self._nonstandard):
            return True
        return False

    # ───────────────────── 逐条等价复现四级链 ─────────────────────

    def resolve(self, part: str, order: str) -> list[ImageEvidence] | None:
        """离线复现 :class:`ImageResolver` 的四级降级链（**逐条等价**）。

        Args:
            part: 料号（文件名第二段）。
            order: 订单号（文件名首段）。

        Returns:
            证据列表（与实时链**完全一致**，含空列表）；索引未构建返回 ``None``。
        """
        if not self.built or self._root is None:
            return None

        # O(1) 快速否决：全局无候选 → 任何层级都不可能命中（等价，且省去逐层扫描）
        if not self.has_any_candidate(order, part):
            return []

        evidences = self._level1(self._root, part, order)
        if evidences:
            return evidences
        evidences = self._level2(self._root, part, order)
        if evidences:
            return evidences
        evidences = self._level3(self._root, part, order)
        if evidences:
            return evidences
        return self._level4(self._root, part, order)

    # ── 层级（与 ImageResolver._level1..4 一一对应）──

    def _level1(self, base: _DirSnapshot, part: str, order: str) -> list[ImageEvidence]:
        """① 料号目录精确匹配。"""
        for name in candidate_dir_names(part, order):
            child = _find_child(base, name)
            if child is not None:
                evidences = self._collect(child, part, order)
                if evidences:
                    return evidences
        return []

    def _level2(self, base: _DirSnapshot, part: str, order: str) -> list[ImageEvidence]:
        """② 料号目录模糊匹配（剥离「图片」等后缀）。"""
        wanted = {(order or "").strip().upper(), (part or "").strip().upper()}
        wanted.discard("")
        for child in base.children:
            if strip_part_dir_suffix(child.name).upper() in wanted:
                evidences = self._collect(child, part, order)
                if evidences:
                    return evidences
        return []

    def _level3(self, base: _DirSnapshot, part: str, order: str) -> list[ImageEvidence]:
        """③ 子目录扫描（``{订单号}-0-{箱数}``）。"""
        if not order:
            return []
        target_order = order.strip().upper()
        for child in base.children:
            stripped = strip_part_dir_suffix(child.name)
            matched = _SUBDIR_PATTERN.match(stripped)
            target = matched.group("order").strip() if matched else stripped
            if target.upper() != target_order:
                continue
            evidences = self._collect_recursive(child, part, order)
            if evidences:
                return evidences
        return []

    def _level4(self, base: _DirSnapshot, part: str, order: str) -> list[ImageEvidence]:
        """④ 票号根目录解析（含一层子目录）。"""
        return self._collect_recursive(base, part, order)

    # ── 收集与二次筛选 ──

    def _collect(
        self, directory: _DirSnapshot, part: str, order: str
    ) -> list[ImageEvidence]:
        """单目录收集（叠加订单号二次筛选，不递归）。"""
        matches: list[tuple[int, str]] = []
        for ref in directory.files:
            if not ref.standard:
                if part and part.strip().upper() in ref.name.upper():
                    matches.append((0, ref.path))
                continue
            if match_segments(ref.order, ref.part, order, part):
                matches.append((ref.seq, ref.path))
        matches.sort(key=lambda item: (item[0], item[1]))
        return [self._evidence(path_str, seq) for seq, path_str in matches]

    def _collect_recursive(
        self, directory: _DirSnapshot, part: str, order: str
    ) -> list[ImageEvidence]:
        """递归收集（下钻一层子目录）。"""
        evidences = self._collect(directory, part, order)
        for child in directory.children:
            evidences.extend(self._collect(child, part, order))
        evidences.sort(key=lambda e: (e.seq, e.image_path))
        return evidences

    @staticmethod
    def _evidence(path_str: str, seq: int) -> ImageEvidence:
        """构造证据（快照已确认文件存在，故 ``exists=True``）。"""
        return ImageEvidence(
            image_path=path_str,
            seq=seq,
            exists=True,
            ocr_confidence=0.0,
            unreachable=False,
            not_found=False,
            ocr=None,
        )


def _find_child(node: _DirSnapshot, name: str) -> _DirSnapshot | None:
    """按目录名查找直接子目录（先精确、再忽略大小写，贴合 Windows 语义）。"""
    wanted = (name or "").strip()
    if not wanted:
        return None
    for child in node.children:
        if child.name == wanted:
            return child
    upper = wanted.upper()
    for child in node.children:
        if child.name.strip().upper() == upper:
            return child
    return None
