"""``core.image_index`` 回归测试（v0.2.0 批次 2 / 点 7）。

**最高优先级验收**：倒排索引的命中集合必须与**旧四级降级链逐条等价**。
本文件的 :class:`TestEquivalenceWithLiveChain` 用同一批查询比较
``ImageResolver(index_enabled=True)``（走索引）与
``ImageResolver(index_enabled=False)``（走实时链）的**路径集合**，要求完全一致。

同时覆盖：
  * P4 语义交叉铁律（首段=订单号、第二段=物料编号）——按列名直觉会 0 命中；
  * ``lookup`` / ``has_any_candidate`` 的 O(1) 反查；
  * 索引构建失败 → 回退实时链 + ``fallback_notes``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.image_index import ImageIndex, ImageRef
from core.image_resolver import ImageResolver
from core.models import DeclarationRecord

# ══════════════════════════════════════════════════════════════════
#  合成目录（覆盖四级链的每一级）
# ══════════════════════════════════════════════════════════════════


def _mk(directory: Path, name: str) -> Path:
    """在目录下造一个占位图片文件。"""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-" + name.encode("utf-8"))
    return target


@pytest.fixture
def share(tmp_path: Path) -> Path:
    """合成票号目录：覆盖 L1 精确 / L2 模糊 / L3 子目录 / L4 根解析。

    Returns:
        ``{票号}`` 根目录。
    """
    root = tmp_path / "SA2"
    root.mkdir(parents=True, exist_ok=True)

    # ① L2 模糊：带「图片」后缀目录
    pic = root / "2660308M图片"
    for seq in ("001", "002", "003", "004"):
        _mk(pic, f"2660308M&N011901-007957-001&{seq}.jpg")

    # ② L1 精确：同目录多订单（含跳号）
    multi = root / "2660310M"
    for seq in ("001", "002", "003", "004", "005", "006", "008"):
        _mk(multi, f"2660310M&N011901-009350-001&{seq}.jpg")
    for seq in ("001", "002", "003"):
        _mk(multi, f"2660310M&N011901-009690-001&{seq}.jpg")
    for seq in ("001", "002"):
        _mk(multi, f"2660310M&N030102-001217-906&{seq}.jpg")

    # ③ L1 精确：普通目录
    _mk(root / "2660326M", "2660326M&N011901-007386-001&001.jpg")

    # ④ L3 子目录扫描：{订单号}-0-{箱数}，命中文件在下一层
    sub = root / "2660410M-0-1"
    for seq in ("001", "002"):
        _mk(sub / "box2", f"2660410M&N030999-000003-001&{seq}.jpg")

    # ⑤ L4 非标文件名（无 & 分段，靠 part 子串命中）
    _mk(root / "2660500M", "N011901-000002-001 (1).jpg")

    # ⑥ L4 根目录直接文件
    _mk(root, "2660600M&N011901-000004-001&001.jpg")

    return root


#: 查询集合（覆盖各级命中 + 缺订单号 + 空值 + 缺失）
QUERIES: list[tuple[str, str]] = [
    ("2660308M", "N011901-007957-001"),   # L2 模糊 → 4
    ("2660310M", "N011901-009350-001"),   # L1 精确 → 7（跳号 007）
    ("2660310M", "N011901-009690-001"),   # L1 → 3
    ("2660310M", "N030102-001217-906"),   # L1 → 2
    ("2660326M", "N011901-007386-001"),   # L1 → 1
    ("2660410M", "N030999-000003-001"),   # L3 子目录 → 2
    ("2660500M", "N011901-000002-001"),   # L4 非标 → 1
    ("2660600M", "N011901-000004-001"),   # L4 根 → 1
    ("2669999M", "N999999-000000-000"),   # 缺失 → 0
    ("", "N011901-007386-001"),           # 缺订单号（变体 E）→ 1
    ("2660310M", ""),                      # 缺料号 → 12
    ("", ""),                             # 全空 → 0
]


def _paths(evidences) -> list[str]:
    """证据路径集合（排序）。"""
    return sorted(e.image_path for e in evidences)


def _resolve_live(resolver: ImageResolver, root: Path, order: str, part: str) -> list[str]:
    rec = DeclarationRecord(ticket_no="SA2", part_no=part, order_no=order)
    return _paths(resolver.resolve(rec, root))


# ══════════════════════════════════════════════════════════════════
#  一、等价性（最高优先级）
# ══════════════════════════════════════════════════════════════════


class TestEquivalenceWithLiveChain:
    """索引命中集合 ≡ 实时四级链命中集合。"""

    @pytest.mark.parametrize(("order", "part"), QUERIES)
    def test_resolver_index_equals_live(self, share: Path, order: str, part: str) -> None:
        live = _resolve_live(ImageResolver(index_enabled=False), share, order, part)
        indexed = _resolve_live(ImageResolver(index_enabled=True), share, order, part)
        assert indexed == live, f"order={order!r} part={part!r}"

    def test_index_resolve_equals_live_all_queries(self, share: Path) -> None:
        """直接对比 ``ImageIndex.resolve`` 与实时链（同一条条比较）。"""
        live = ImageResolver(index_enabled=False)
        index = ImageIndex(share)
        assert index.built is True
        for order, part in QUERIES:
            expected = _resolve_live(live, share, order, part)
            got = index.resolve(part, order)
            assert got is not None
            assert _paths(got) == expected, f"order={order!r} part={part!r}"

    def test_seq_gap_preserved(self, share: Path) -> None:
        """跳号如实反映（缺 &007 → 7 张，无 007）。"""
        index = ImageIndex(share)
        got = index.resolve("N011901-009350-001", "2660310M")
        assert got is not None
        assert sorted(e.seq for e in got) == [1, 2, 3, 4, 5, 6, 8]

    def test_no_cross_contamination(self, share: Path) -> None:
        """同目录多订单隔离（无串货）。"""
        index = ImageIndex(share)
        for part in ("N011901-009350-001", "N011901-009690-001", "N030102-001217-906"):
            got = index.resolve(part, "2660310M")
            assert got is not None
            for e in got:
                assert part in Path(e.image_path).name


# ══════════════════════════════════════════════════════════════════
#  二、P4 语义交叉铁律
# ══════════════════════════════════════════════════════════════════


class TestP4Semantics:
    """文件名首段=订单号、第二段=物料编号；按列名直觉会 0 命中。"""

    def test_correct_mapping_hits(self, share: Path) -> None:
        index = ImageIndex(share)
        got = index.resolve("N011901-007386-001", "2660326M")
        assert got is not None and len(got) == 1

    def test_swapped_mapping_hits_nothing(self, share: Path) -> None:
        """把订单号/物料编号互换（按列名字面直觉）→ 0 命中。"""
        index = ImageIndex(share)
        got = index.resolve("2660326M", "N011901-007386-001")
        assert got == []

    def test_swapped_also_zero_in_live_chain(self, share: Path) -> None:
        live = ImageResolver(index_enabled=False)
        swapped = _resolve_live(live, share, "N011901-007386-001", "2660326M")
        assert swapped == []


# ══════════════════════════════════════════════════════════════════
#  三、O(1) 反查 API
# ══════════════════════════════════════════════════════════════════


class TestLookupApi:
    """``lookup`` / ``has_any_candidate``。"""

    def test_lookup_by_order(self, share: Path) -> None:
        index = ImageIndex(share)
        refs = index.lookup("2660310M", "N011901-009350-001")
        assert len(refs) == 7
        assert all(isinstance(r, ImageRef) for r in refs)

    def test_lookup_by_part_when_no_order(self, share: Path) -> None:
        index = ImageIndex(share)
        refs = index.lookup("", "N011901-007386-001")
        assert len(refs) == 1

    def test_has_any_candidate_true(self, share: Path) -> None:
        assert ImageIndex(share).has_any_candidate("2660326M", "N011901-007386-001") is True

    def test_has_any_candidate_false(self, share: Path) -> None:
        assert ImageIndex(share).has_any_candidate("NOORDER", "NOPART") is False

    def test_dirs_index(self, share: Path) -> None:
        index = ImageIndex(share)
        assert "2660308M" in index.dirs  # 剥离「图片」后缀
        assert "2660310M" in index.dirs


# ══════════════════════════════════════════════════════════════════
#  四、回退与异常
# ══════════════════════════════════════════════════════════════════


class TestFallback:
    """索引不可用时回退实时链并记录 ``fallback_notes``。"""

    def test_index_failure_falls_back_and_notes(
        self, share: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import core.image_index as mod

        class _Boom:
            def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
                raise RuntimeError("boom")

        monkeypatch.setattr(mod, "ImageIndex", _Boom)
        resolver = ImageResolver(index_enabled=True)
        rec = DeclarationRecord(
            ticket_no="SA2", part_no="N011901-007957-001", order_no="2660308M"
        )
        evidences = resolver.resolve(rec, share)
        assert len(evidences) == 4  # 回退实时链仍命中
        assert resolver.fallback_notes  # 已记录回退事件

    def test_missing_ticket_dir_not_found(self, share: Path, tmp_path: Path) -> None:
        """票号目录不存在 → not_found（索引不参与，保持原语义）。"""
        reachable = tmp_path / "reachable"
        reachable.mkdir()
        (reachable / "other").mkdir()
        rec = DeclarationRecord(ticket_no="NOPE", part_no="X", order_no="Y")
        evidences = ImageResolver(index_enabled=True).resolve(rec, reachable)
        assert len(evidences) == 1
        assert evidences[0].not_found is True
        assert evidences[0].unreachable is False
