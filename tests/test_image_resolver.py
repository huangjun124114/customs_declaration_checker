"""T03 回归测试：图片三级索引匹配 + OCR 引擎封装。

对应架构设计第 7 节 T03 的**九条验收要点**：

  1. 对样本票 ``SA26090215``：``2660308M`` 记录能命中 ``2660308M图片\\``（**带"图片"后缀**）；
  2. ``2660310M`` 的 5 个订单能被**订单号二次筛选**分别隔离，**无串货**；
  3. 序号跳号（缺 ``&007``）不影响枚举；
  4. 缺图记录返回**空列表**（**不抛异常**）；
  5. **中文路径**图片通过 ``cv2.imdecode`` 正确读取（``cv2.imread`` 必失败场景**有断言**）；
  6. :meth:`OcrEngine.recognize_batch` 单张失败**不中断**；
  7. OCR 结果按**图片哈希**缓存命中；
  8. :meth:`warmup` 可预加载模型（供首启加速）；
  9. ``pytest`` 全绿（OCR 用例用 **mock 后端**离线跑）。

设计取舍：真实样本目录不存在时相关用例 ``skip``（不 fail）；OCR 用例默认走 mock 后端，
真实 OCR 用例标 ``@pytest.mark.ocr`` 且仅在样本存在时执行，避免测试依赖真实引擎耗时。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from core.image_resolver import (
    ImageResolver,
    parse_image_file_name,
    strip_part_dir_suffix,
)
from core.models import DeclarationRecord, OcrText
from core.ocr_engine import OcrBackend, OcrEngine, RapidOcrBackend

# ══════════════════════════════════════════════════════════════════
#  测试辅助：Mock OCR 后端
# ══════════════════════════════════════════════════════════════════


class _MockBackend(OcrBackend):
    """可控的离线 OCR mock 后端（不依赖真实 rapidocr / onnxruntime）。

    Args:
        text_map: ``{路径 或 文件名: 文本}`` 映射。
        fail_paths: 需要**抛异常**的路径集合（模拟单张失败）。
        confidence: 返回的置信度。
        delay: 每次识别的模拟耗时（秒）。
    """

    def __init__(
        self,
        text_map: dict[str, str] | None = None,
        fail_paths: set[str] | None = None,
        confidence: float = 0.95,
        delay: float = 0.0,
    ) -> None:
        self.text_map = text_map or {}
        self.fail_paths = fail_paths or set()
        self.confidence = confidence
        self.delay = delay
        self.calls: list[str] = []
        self.warmed = False

    def recognize(self, image_path) -> OcrText:
        if self.delay:
            time.sleep(self.delay)
        path_str = str(image_path)
        self.calls.append(path_str)
        if path_str in self.fail_paths or Path(path_str).name in self.fail_paths:
            raise RuntimeError(f"mock OCR failure: {path_str}")
        text = self.text_map.get(path_str) or self.text_map.get(Path(path_str).name, "")
        return OcrText(
            image_path=path_str,
            text_raw=text,
            confidence=self.confidence,
            boxes=[(0, 0, 10, 10)],
            seq=0,
        )

    def warmup(self) -> None:
        self.warmed = True


# ══════════════════════════════════════════════════════════════════
#  一、纯函数：文件名 / 目录名解析
# ══════════════════════════════════════════════════════════════════


class TestFileNameParsing:
    """``{A}&{B}&{C}.jpg`` 解析 + 目录名后缀剥离（架构设计 6.4）。"""

    def test_parse_main_pattern(self) -> None:
        """实测形态：``2660310M&N011901-009350-001&001.jpg``。"""
        parsed = parse_image_file_name("2660310M&N011901-009350-001&001.jpg")
        assert parsed == ("2660310M", "N011901-009350-001", 1)

    def test_parse_seq_is_int(self) -> None:
        """序号解析为 int（``&009`` → 9）。"""
        parsed = parse_image_file_name("2660308M&N030107-003211-001&009.jpg")
        assert parsed is not None
        assert parsed[2] == 9

    def test_parse_with_path_argument(self) -> None:
        """传入含路径的字符串时只取文件名部分。"""
        parsed = parse_image_file_name("a/b/c/2660310M&X&002.jpg")
        assert parsed == ("2660310M", "X", 2)

    def test_parse_invalid_returns_none(self) -> None:
        """非标文件名返回 ``None``（不抛异常）。"""
        assert parse_image_file_name("random_name.png") is None
        assert parse_image_file_name("") is None
        assert parse_image_file_name(".jpg") is None

    def test_strip_part_dir_suffix_picture(self) -> None:
        """实测核心场景：``2660308M图片`` → ``2660308M``。"""
        assert strip_part_dir_suffix("2660308M图片") == "2660308M"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2660310M", "2660310M"),
            ("2660308M图", "2660308M"),
            ("2660310M(1)", "2660310M"),
            ("2660310M（1）", "2660310M"),
            ("2660310M-0-2", "2660310M"),   # 箱数后缀一并剥离为订单号
            ("2660310M_0_", "2660310M"),
        ],
    )
    def test_strip_part_dir_suffix_variants(self, raw: str, expected: str) -> None:
        """各类后缀剥离（容错，架构设计 6.4）。"""
        assert strip_part_dir_suffix(raw) == expected


# ══════════════════════════════════════════════════════════════════
#  二、合成目录：三级索引 + 二次筛选 + 跳号 + 空列表（要点 1–4）
# ══════════════════════════════════════════════════════════════════


def _make_image(directory: Path, name: str) -> Path:
    """在目录下造一个占位图片文件（只写少量字节，测试只做存在性/枚举）。"""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return target


@pytest.fixture
def synthetic_share(tmp_path: Path) -> Path:
    """合成共享根：复刻真实样本结构（含"图片"后缀目录 + 同目录多订单 + 跳号）。

    Returns:
        ``{票号}`` 根目录路径。
    """
    root = tmp_path / "SA26090215"
    root.mkdir(parents=True, exist_ok=True)

    # ① 2660308M 带「图片」后缀，单订单
    pic_dir = root / "2660308M图片"
    for seq in ("001", "002", "003", "004"):
        _make_image(pic_dir, f"2660308M&N011901-007957-001&{seq}.jpg")

    # ② 2660310M 同目录含 3 个订单，其中 N011901-009350-001 缺 &007（跳号）
    multi = root / "2660310M"
    for seq in ("001", "002", "003", "004", "005", "006", "008"):  # 缺 007
        _make_image(multi, f"2660310M&N011901-009350-001&{seq}.jpg")
    for seq in ("001", "002", "003"):
        _make_image(multi, f"2660310M&N011901-009690-001&{seq}.jpg")
    for seq in ("001", "002"):
        _make_image(multi, f"2660310M&N030102-001217-906&{seq}.jpg")

    # ③ 2660326M 普通目录
    normal = root / "2660326M"
    _make_image(normal, "2660326M&N011901-007386-001&001.jpg")

    return root


class TestImageResolverSynthetic:
    """合成目录下的三级索引与降级链（不依赖真实样本）。"""

    def test_level1_exact_hit(self, synthetic_share: Path) -> None:
        """精确目录命中：订单号 = 目录名。"""
        record = DeclarationRecord(
            ticket_no="SA26090215",
            part_no="N011901-007386-001",
            order_no="2660326M",
        )
        evidences = ImageResolver().resolve(record, synthetic_share)
        assert len(evidences) == 1
        assert evidences[0].exists is True
        assert evidences[0].seq == 1
        assert "2660326M" in evidences[0].image_path

    def test_level2_fuzzy_dir_with_picture_suffix(self, synthetic_share: Path) -> None:
        """要点①：``2660308M`` 命中带「图片」后缀的 ``2660308M图片\\``。"""
        record = DeclarationRecord(
            ticket_no="SA26090215",
            part_no="N011901-007957-001",
            order_no="2660308M",
        )
        evidences = ImageResolver().resolve(record, synthetic_share)
        assert len(evidences) == 4
        assert all("2660308M图片" in e.image_path for e in evidences)

    def test_order_secondary_filter_no_cross_contamination(self, synthetic_share: Path) -> None:
        """要点②：``2660310M`` 多订单被二次筛选隔离，**无串货**。"""
        resolver = ImageResolver()
        cases = {
            "N011901-009350-001": 7,   # 缺 007 → 7 张
            "N011901-009690-001": 3,
            "N030102-001217-906": 2,
        }
        for part, expected in cases.items():
            record = DeclarationRecord(
                ticket_no="SA26090215",
                part_no=part,
                order_no="2660310M",
            )
            evidences = resolver.resolve(record, synthetic_share)
            assert len(evidences) == expected, f"{part} 应命中 {expected} 张"
            # 无串货：每张的第二段必须等于本单料号
            for e in evidences:
                assert part in Path(e.image_path).name

    def test_seq_gap_not_affect_enumeration(self, synthetic_share: Path) -> None:
        """要点③：序号跳号（缺 ``&007``）不影响枚举完整性（合成数据）。"""
        record = DeclarationRecord(
            ticket_no="SA26090215",
            part_no="N011901-009350-001",
            order_no="2660310M",
        )
        evidences = ImageResolver().resolve(record, synthetic_share)
        seqs = sorted(e.seq for e in evidences)
        assert seqs == [1, 2, 3, 4, 5, 6, 8]
        assert 7 not in seqs  # 跳号如实反映，不臆造

    def test_missing_record_returns_empty_list(self, synthetic_share: Path) -> None:
        """要点④：缺图记录返回**空列表**（**不抛异常**）。"""
        record = DeclarationRecord(
            ticket_no="SA26090215",
            part_no="N999999-000000-000",
            order_no="2669999M",
        )
        evidences = ImageResolver().resolve(record, synthetic_share)
        assert evidences == []

    def test_nonexistent_ticket_dir_returns_not_found(self, tmp_path: Path) -> None:
        """裁决①-断言A：共享根可达 + 票号目录不存在 → ``not_found=True, unreachable=False``。

        语义：这是"目录确实不存在"（正常缺图 🔵），**不**应触发自动暂停。
        """
        reachable_root = tmp_path / "reachable_share"
        reachable_root.mkdir(parents=True, exist_ok=True)
        (reachable_root / "some_other_dir").mkdir(exist_ok=True)  # 让根非空、确保可列举

        record = DeclarationRecord(
            ticket_no="SA99999999",
            part_no="N011901-007386-001",
            order_no="2660326M",
        )
        evidences = ImageResolver().resolve(record, reachable_root)
        assert len(evidences) == 1
        assert evidences[0].not_found is True
        assert evidences[0].unreachable is False   # 互斥断言
        assert evidences[0].exists is False

    def test_unreachable_share_root_returns_unreachable(self, tmp_path: Path) -> None:
        """裁决①-断言B：共享根**本身不可达** → ``unreachable=True, not_found=False``。

        语义：模拟共享盘断连（注入 probe 判定根不可达）→ 上层可触发自动暂停。
        """
        root = tmp_path / "unc_share"
        root.mkdir(parents=True, exist_ok=True)

        # 注入钩子：共享根本身不可达（模拟断连）
        resolver = ImageResolver(unreachable_probe=lambda p: Path(p) != root)
        record = DeclarationRecord(
            ticket_no="SA26090215",
            part_no="N011901-007386-001",
            order_no="2660326M",
        )
        evidences = resolver.resolve(record, root)
        assert len(evidences) == 1
        assert evidences[0].unreachable is True
        assert evidences[0].not_found is False     # 互斥断言
        assert evidences[0].exists is False

    def test_unreachable_and_not_found_mutually_exclusive(self, synthetic_share: Path) -> None:
        """两个标记在所有输出中互斥（不存在同时为 True 的证据）。"""
        resolver = ImageResolver()
        records = [
            DeclarationRecord(ticket_no="SA26090215", part_no="N011901-007957-001", order_no="2660308M"),
            DeclarationRecord(ticket_no="SA26090215", part_no="N999999-000000-000", order_no="2669999M"),
            DeclarationRecord(ticket_no="SA40404040", part_no="X", order_no="9999999M"),
        ]
        for record in records:
            for e in resolver.resolve(record, synthetic_share):
                assert not (e.unreachable and e.not_found), "unreachable 与 not_found 必须互斥"

    def test_empty_record_fields_returns_empty(self, synthetic_share: Path) -> None:
        """料号/订单号全空的记录 → 空列表（避免误收全票）。"""
        record = DeclarationRecord(ticket_no="SA26090215")
        assert ImageResolver().resolve(record, synthetic_share) == []

    def test_share_root_is_ticket_dir_itself(self, synthetic_share: Path) -> None:
        """``share_root`` 直接就是票号目录时也能正确定位。"""
        record = DeclarationRecord(
            ticket_no="SA26090215",
            part_no="N011901-007957-001",
            order_no="2660308M",
        )
        evidences = ImageResolver().resolve(record, synthetic_share, "SA26090215")
        assert len(evidences) == 4

    def test_evidence_sort_by_seq(self, synthetic_share: Path) -> None:
        """证据按序号升序返回。"""
        record = DeclarationRecord(
            ticket_no="SA26090215",
            part_no="N011901-009350-001",
            order_no="2660310M",
        )
        evidences = ImageResolver().resolve(record, synthetic_share)
        seqs = [e.seq for e in evidences]
        assert seqs == sorted(seqs)

    def test_unreachable_probe_hook(self, synthetic_share: Path) -> None:
        """注入 ``unreachable_probe`` 钩子可强制判定「共享根不可达」（模拟断连）。"""
        resolver = ImageResolver(unreachable_probe=lambda _p: False)
        record = DeclarationRecord(
            ticket_no="SA26090215",
            part_no="N011901-007957-001",
            order_no="2660308M",
        )
        evidences = resolver.resolve(record, synthetic_share)
        assert len(evidences) == 1
        assert evidences[0].unreachable is True
        assert evidences[0].not_found is False

    def test_input_not_modified(self, synthetic_share: Path) -> None:
        """只读红线：解析过程不新增/删除/改名任何图片（目录清单哈希不变）。"""
        before = sorted(p.name for p in synthetic_share.rglob("*"))
        record = DeclarationRecord(
            ticket_no="SA26090215",
            part_no="N011901-009350-001",
            order_no="2660310M",
        )
        ImageResolver().resolve(record, synthetic_share)
        after = sorted(p.name for p in synthetic_share.rglob("*"))
        assert before == after


# ══════════════════════════════════════════════════════════════════
#  三、OCR 引擎（要点 5–8，全部 mock 离线）
# ══════════════════════════════════════════════════════════════════


class TestOcrEngineWithMock:
    """用 mock 后端验证编排层（并发 / 容错 / 缓存 / 低置信）。"""

    def test_recognize_batch_order_preserved(self, tmp_path: Path) -> None:
        """批量结果与输入**等长同序**。"""
        paths = [tmp_path / f"a{i}.jpg" for i in range(3)]
        backend = _MockBackend({p.name: f"text-{p.stem}" for p in paths})
        engine = OcrEngine(backend=backend)
        results = engine.recognize_batch(paths)
        assert len(results) == 3
        assert [r.text_raw for r in results] == ["text-a0", "text-a1", "text-a2"]

    def test_recognize_batch_empty(self) -> None:
        """空输入返回空列表。"""
        assert OcrEngine(backend=_MockBackend()).recognize_batch([]) == []

    def test_single_failure_does_not_interrupt_batch(self, tmp_path: Path) -> None:
        """要点⑥：单张失败**不中断**整批（该张低置信占位 + 记 WARN）。"""
        paths = [tmp_path / f"b{i}.jpg" for i in range(4)]
        backend = _MockBackend(
            {p.name: f"ok-{p.stem}" for p in paths},
            fail_paths={str(paths[2])},
        )
        engine = OcrEngine(backend=backend)
        results = engine.recognize_batch(paths)
        assert len(results) == 4
        # 失败张被占位（低置信、空文本），其余正常
        assert results[2].confidence == 0.0
        assert results[2].low_confidence is True
        assert results[2].text_raw == ""
        assert results[0].text_raw == "ok-b0"
        assert results[3].text_raw == "ok-b3"

    def test_recognize_one_never_raises(self, tmp_path: Path) -> None:
        """``recognize_one`` 对失败路径**永不抛异常**。"""
        backend = _MockBackend(fail_paths={"bad.jpg"})
        engine = OcrEngine(backend=backend)
        result = engine.recognize_one(tmp_path / "bad.jpg")
        assert result.confidence == 0.0
        assert result.low_confidence is True

    def test_hash_cache_hit(self, tmp_path: Path) -> None:
        """要点⑦：OCR 结果按**图片哈希**缓存命中（同图第二次不再调用后端）。"""
        img = tmp_path / "cached.jpg"
        img.write_bytes(b"\xff\xd8same-bytes")
        backend = _MockBackend({"cached.jpg": "cached-text"})
        engine = OcrEngine(backend=backend)

        first = engine.recognize_one(img)
        second = engine.recognize_one(img)

        assert backend.calls == [str(img)]  # 只调用一次后端
        assert first.text_raw == second.text_raw == "cached-text"
        assert engine.cache.hits >= 1

    def test_hash_cache_across_different_paths(self, tmp_path: Path) -> None:
        """**同内容不同路径**也应命中缓存（证明 key 用内容哈希而非路径）。"""
        a = tmp_path / "path_a.jpg"
        b = tmp_path / "path_b.jpg"
        a.write_bytes(b"identical-content")
        b.write_bytes(b"identical-content")
        backend = _MockBackend()
        engine = OcrEngine(backend=backend)

        engine.recognize_one(a)
        engine.recognize_one(b)

        assert len(backend.calls) == 1  # 第二次命中内容哈希缓存

    def test_cache_disabled_still_calls(self, tmp_path: Path) -> None:
        """关闭缓存时不命中。"""
        img = tmp_path / "d.jpg"
        img.write_bytes(b"xyz")
        backend = _MockBackend()
        engine = OcrEngine(backend=backend, cache_enabled=False)
        engine.recognize_one(img)
        engine.recognize_one(img)
        assert len(backend.calls) == 2

    def test_low_confidence_marked(self, tmp_path: Path) -> None:
        """置信度低于阈值 → ``low_confidence=True``（架构设计 9.7）。"""
        img = tmp_path / "low.jpg"
        img.write_bytes(b"low-conf-bytes")
        backend = _MockBackend(confidence=0.2)
        engine = OcrEngine(backend=backend, confidence_threshold=0.5)
        result = engine.recognize_one(img)
        assert result.low_confidence is True

    def test_high_confidence_not_marked(self, tmp_path: Path) -> None:
        """置信度达标 → ``low_confidence=False``。"""
        img = tmp_path / "high.jpg"
        img.write_bytes(b"high-conf-bytes")
        engine = OcrEngine(backend=_MockBackend(confidence=0.99), confidence_threshold=0.5)
        assert engine.recognize_one(img).low_confidence is False

    def test_warmup_invokes_backend(self) -> None:
        """要点⑧：``warmup()`` 触发后端预加载（供首启加速）。"""
        backend = _MockBackend()
        engine = OcrEngine(backend=backend)
        assert backend.warmed is False
        engine.warmup()
        assert backend.warmed is True

    def test_warmup_failure_not_fatal(self) -> None:
        """``warmup`` 内部异常不影响调用方（仅 WARN）。"""

        class _BrokenBackend(_MockBackend):
            def warmup(self) -> None:
                raise RuntimeError("boom")

        OcrEngine(backend=_BrokenBackend()).warmup()  # 不抛即通过

    @pytest.mark.parametrize("requested", [0, 1, 4, 99])
    def test_max_workers_clamped(self, requested: int) -> None:
        """并发数被夹在 ``[1, MAX_WORKERS_LIMIT]``（R8 守门常量）。

        ⚠️ 批次 3-A 因《验收问题修复方案》Q6「放宽到 4–8」把上限由 2 改为 8，
        故本断言随之更新（原先固定 ``<= 2``）；改为引用常量，避免再次硬编码。
        """
        engine = OcrEngine(backend=_MockBackend(), max_workers=requested)
        assert 1 <= engine.max_workers <= OcrEngine.MAX_WORKERS_LIMIT
        assert OcrEngine.MAX_WORKERS_LIMIT == 8

    def test_batch_concurrent_results_consistent(self, tmp_path: Path) -> None:
        """并发（``max_workers=2``）下批量结果仍与输入对齐。"""
        paths = [tmp_path / f"e{i}.jpg" for i in range(6)]
        backend = _MockBackend({p.name: f"v-{p.stem}" for p in paths}, delay=0.01)
        engine = OcrEngine(backend=backend, max_workers=2)
        results = engine.recognize_batch(paths)
        assert [r.text_raw for r in results] == [f"v-e{i}" for i in range(6)]


# ══════════════════════════════════════════════════════════════════
#  四、中文路径实锤（要点⑤）
# ══════════════════════════════════════════════════════════════════


class TestChinesePathImageRead:
    """中文路径读图：``cv2.imread`` 必失败 vs ``cv2.imdecode`` 成功（架构设计 13.4）。"""

    def test_rapid_backend_imdecode_cn_path(self, tmp_path: Path) -> None:
        """自造中文路径 PNG → ``imdecode`` 成功、``imread`` 返回 ``None``。"""
        cv2 = pytest.importorskip("cv2")
        np = pytest.importorskip("numpy")

        cn_dir = tmp_path / "中文图片目录"
        cn_dir.mkdir(parents=True, exist_ok=True)
        cn_path = cn_dir / "订单&料号&001.png"

        # 造一张 8x8 的单色 PNG（不经 cv2.imwrite，避免其自身的编码路径问题）
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        image[:, :] = (10, 20, 30)
        ok, buf = cv2.imencode(".png", image)
        assert ok is True
        cn_path.write_bytes(buf.tobytes())

        # ⚠️ 断言：cv2.imread 对中文路径**必然静默失败**（返回 None）
        assert cv2.imread(str(cn_path)) is None, "cv2.imread 本应对中文路径返回 None"

        # ✅ 断言：imdecode 正常读入
        arr = RapidOcrBackend.load_image_array(cn_path)
        assert arr is not None
        assert arr.shape == (8, 8, 3)

    def test_load_image_array_missing_returns_none(self, tmp_path: Path) -> None:
        """路径不存在时 ``load_image_array`` 返回 ``None``（不抛）。"""
        assert RapidOcrBackend.load_image_array(tmp_path / "不存在.png") is None


# ══════════════════════════════════════════════════════════════════
#  五、真实样本端到端（存在样本时执行；否则 skip）
# ══════════════════════════════════════════════════════════════════


@pytest.fixture
def ticket_root(sample_dir: Path) -> Path:
    """真实样本票 ``SA26090215`` 目录（不存在则 skip）。"""
    candidates = [
        sample_dir / "SA26090215",
        sample_dir / "原始输入" / "SA26090215",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    pytest.skip("未找到样本票 SA26090215 目录，跳过真实样本用例")


class TestRealSample:
    """真实样本 ``SA26090215`` 的三级索引（要点 1–3 的真实数据复核）。"""

    def test_2660308M_with_picture_suffix(self, ticket_root: Path) -> None:
        """要点①：``2660308M`` 命中 ``2660308M图片\\``（带"图片"后缀）。"""
        record = DeclarationRecord(
            ticket_no="SA26090215",
            part_no="N011901-007957-001",
            order_no="2660308M",
        )
        evidences = ImageResolver().resolve(record, ticket_root)
        assert len(evidences) == 4
        assert all("2660308M图片" in e.image_path for e in evidences)

    def test_2660310M_five_orders_isolated(self, ticket_root: Path) -> None:
        """要点②：``2660310M`` 的 5 个订单被分别隔离（无串货）。"""
        resolver = ImageResolver()
        resolver.resolve(
            DeclarationRecord(
                ticket_no="SA26090215",
                part_no="N030107-003211-001",
                order_no="2660310M",
            ),
            ticket_root,
        )
        expected_parts = {
            "N011901-009350-001",
            "N011901-009690-001",
            "N030102-001217-906",
            "N030103-023966-905",
            "N030107-003211-001",
        }
        for part in expected_parts:
            record = DeclarationRecord(
                ticket_no="SA26090215",
                part_no=part,
                order_no="2660310M",
            )
            evidences = resolver.resolve(record, ticket_root)
            assert evidences, f"{part} 应有图"
            for e in evidences:
                assert part in Path(e.image_path).name, "串货！"
            # 各订单图数不应相同（证明真正做了隔离，而非同一批）
        counts = {
            part: len(
                resolver.resolve(
                    DeclarationRecord(
                        ticket_no="SA26090215", part_no=part, order_no="2660310M"
                    ),
                    ticket_root,
                )
            )
            for part in expected_parts
        }
        assert sum(counts.values()) == 36  # 与实测 2660310M 目录 36 张吻合
        # 五个订单图数互不相同/组合覆盖，证明真正隔离（非同一批重复）
        assert counts["N011901-009350-001"] == 7
        assert counts["N011901-009690-001"] == 3
        assert counts["N030102-001217-906"] == 12
        assert counts["N030103-023966-905"] == 8
        assert counts["N030107-003211-001"] == 6

    def test_real_seq_gap_2660308M(self, ticket_root: Path) -> None:
        """要点③（真实数据）：``2660308M图片\\N030107-003211-001`` 缺 ``&007``。

        实测该料号序号为 ``001..006, 008, 009``（共 8 张，跳号 007），
        跳号**不影响枚举**（不臆造 007）。
        """
        record = DeclarationRecord(
            ticket_no="SA26090215",
            part_no="N030107-003211-001",
            order_no="2660308M",
        )
        evidences = ImageResolver().resolve(record, ticket_root)
        seqs = sorted(e.seq for e in evidences)
        assert seqs == [1, 2, 3, 4, 5, 6, 8, 9]
        assert 7 not in seqs
        assert all("2660308M图片" in e.image_path for e in evidences)

    def test_total_images_129(self, ticket_root: Path) -> None:
        """样本票共 129 张 jpg（架构设计事实核对）。"""
        count = sum(
            1
            for p in ticket_root.rglob("*")
            if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg"}
        )
        assert count == 129


@pytest.mark.ocr
class TestRealOcr:
    """真实 OCR 引擎端到端（耗时；标 ``ocr`` 标记；无样本或未装 rapidocr 时 skip）。"""

    def test_ticket_root_has_2660308M(self, ticket_root: Path) -> None:
        """前置：样本目录存在（否则整体 skip）。"""
        assert ticket_root.is_dir()

    def test_real_rapidocr_on_sample(self) -> None:
        """对一张真实样本图跑真实 OCR，验证 3.x API（``res.txts``）可用。"""
        base = Path(r"D:\Data\workspace\workdoc\AI测试\原始输入")
        candidates = [
            base / "SA26090215" / "2660308M图片" / "2660308M&N011901-007957-001&001.jpg",
            base / "SA26090215" / "2660310M" / "2660310M&N011901-009350-001&001.jpg",
        ]
        image = next((p for p in candidates if p.is_file()), None)
        if image is None:
            pytest.skip("未找到真实样本图片，跳过真实 OCR 用例")

        pytest.importorskip("rapidocr")
        backend = RapidOcrBackend()
        backend.warmup()
        result = backend.recognize(image)
        # 3.x API 校验：res.txts 取到文本、置信度在 (0,1]、boxes 为序列
        assert isinstance(result.text_raw, str)
        assert result.text_raw.strip(), "真实 OCR 应识别出文本（验证 res.txts API 正确）"
        assert 0.0 < result.confidence <= 1.0
        assert len(result.boxes) > 0
        assert result.image_path.endswith(".jpg")
