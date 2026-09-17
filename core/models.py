"""全部数据模型与枚举（core.models，对应架构设计第 4 节类图）。

本模块是**全层共享**的数据契约：``core`` / ``app`` / ``ui`` 都依赖它，
但它**不依赖任何其他业务模块**（除 ``infra`` 的常量工具外），保证无循环依赖。

包含：
  * 枚举：``Verdict`` / ``NoiseLevel`` / ``LogLevel`` / ``StructureVariant``
  * 模型：``ImageEvidence`` / ``OcrText`` / ``DeclarationRecord`` /
    ``DifferenceDetail`` / ``CheckResult`` / ``FieldMapping`` / ``ExcelProbeResult`` /
    ``ParsedElement`` / ``Fingerprint`` / ``ResumeSnapshot``

**注意**：``Verdict`` 枚举只定义"是哪一类判定"，其**面向用户的字符串**在
``core.constants`` 中定义（与 SOP 1.3 逐字一致），避免两处硬编码漂移。
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注用，避免与 token_matcher 循环 import
    from core.token_matcher import TokenMatch

__all__ = [
    "Verdict",
    "NoiseLevel",
    "LogLevel",
    "StructureVariant",
    "ImageEvidence",
    "OcrText",
    "DeclarationRecord",
    "DifferenceDetail",
    "CheckResult",
    "FieldMapping",
    "ExcelProbeResult",
    "ParsedElement",
    "Fingerprint",
    "ResumeSnapshot",
    "build_key",
    "split_key",
]


# ══════════════════════════════════════════════════════════════════
#  枚举
# ══════════════════════════════════════════════════════════════════


class Verdict(str, Enum):
    """四类判定结果（对应 SOP 1.3）。

    | 枚举值 | SOP 口径 | 触发条件 |
    |---|---|---|
    | ``PASS``     | ✅ 校验合格 | 申报值与图片证据一致；或双方均为"无" |
    | ``FAIL``     | ❌ 校验异常 | 有值但不一致；或申报缺失但图片明确有 |
    | ``NO_MARK``  | ⚠️ 缺图内标识，人工复核 | 图片无品牌/型号文字；OCR 证据不足 |
    | ``NO_IMAGE`` | 🔵 缺图，人工复核 | 共享目录找不到对应图片 |
    """

    PASS = "PASS"
    FAIL = "FAIL"
    NO_MARK = "NO_MARK"
    NO_IMAGE = "NO_IMAGE"


class NoiseLevel(str, Enum):
    """疑似噪声三态（对应架构设计 C6 / R5，**不虚高红线**）。

    ``SUSPICIOUS`` 的记录**强制**降级为 ``Verdict.NO_MARK``（⚠️），
    **禁止**直达 ``Verdict.FAIL``（❌）。
    """

    DEFINITE_MATCH = "DEFINITE_MATCH"    # 明确匹配，证据充分
    SUSPICIOUS = "SUSPICIOUS"            # 疑似噪声（易混字符/编辑距离/低置信度）
    CLEAR_MISMATCH = "CLEAR_MISMATCH"    # 明确不一致，证据充分


class LogLevel(str, Enum):
    """日志级别（与 ``infra.logger.LogLevel`` 取值一一对应）。"""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


class StructureVariant(str, Enum):
    """Excel 结构变体（对应 SOP Phase 1 表）。

    | 变体 | 特征 |
    |---|---|
    | ``A_OLD``           | 首行即表头，含"出货通知书号"列 |
    | ``B_DOC_COLON``     | 独立 Sheet，标题 ``出货通知书号:SA...``（**有冒号**） |
    | ``C_DOC_NOCOLON``   | 独立 Sheet，标题 ``出货通知书号SA...``（**无冒号**） |
    | ``D_DUAL_SHEET``    | 双 Sheet：Sheet1 是箱明细（易误中），Sheet2 才是要素表 |
    | ``E_NO_ORDER_COL``  | 要素表无"订单号"列 → order_no 置空 + 子目录兜底扫描 |
    """

    A_OLD = "A"
    B_DOC_COLON = "B"
    C_DOC_NOCOLON = "C"
    D_DUAL_SHEET = "D"
    E_NO_ORDER_COL = "E"
    UNKNOWN = "UNKNOWN"


# ══════════════════════════════════════════════════════════════════
#  Key 构造（三级索引唯一键，幂等的基础）
# ══════════════════════════════════════════════════════════════════

#: key 分隔符（半角竖线，顺序固定）
KEY_SEP = "|"


def build_key(ticket_no: str, part_no: str, order_no: str) -> str:
    """构造三级索引唯一键 ``出货号|料号|订单``。

    规则（架构设计 9.2）：
      * 顺序固定为「出货号 → 料号 → 订单」；
      * 分隔符固定为半角竖线 ``|``；
      * 各段 ``strip`` 后**保留原大小写**（不做 upper）；
      * **空段保留**（不省略分隔符），保证段数恒为 3。

    同一 key 同时用于 ``ResumeStore`` 断点、累积、``ReviewStore`` 改判覆盖
    —— 三者共用一把钥匙，保证 resume/accum/review 永不同步（SOP 陷阱 #2）。

    Args:
        ticket_no: 出货通知书号。
        part_no: 成品料号。
        order_no: 订单号。

    Returns:
        形如 ``SA26090215|N011901-007386-001|2660326M`` 的字符串。
    """
    return KEY_SEP.join(
        [
            (ticket_no or "").strip(),
            (part_no or "").strip(),
            (order_no or "").strip(),
        ]
    )


def split_key(key: str) -> tuple[str, str, str]:
    """把 key 反解为 ``(ticket_no, part_no, order_no)``。

    因 :func:`build_key` 保证段数恒为 3，反解安全；异常 key 用空串补齐。

    Args:
        key: 由 :func:`build_key` 构造的键。

    Returns:
        三元组；段数不足时以空串补齐。
    """
    parts = (key or "").split(KEY_SEP)
    while len(parts) < 3:
        parts.append("")
    return parts[0].strip(), parts[1].strip(), parts[2].strip()


# ══════════════════════════════════════════════════════════════════
#  数据模型
# ══════════════════════════════════════════════════════════════════


@dataclass
class ImageEvidence:
    """单张图片证据。

    Attributes:
        image_path: 图片绝对路径（可能是 UNC）。
        seq: 序号（取自文件名 ``&{序号}`` 段；**不可假设连续**，会跳号）。
        exists: 文件是否存在且可读。
        ocr_confidence: 该图 OCR 的最低置信度（0.0 表示未识别/失败）。
        unreachable: 是否因「共享盘不可达 / 权限不足」导致读图失败
            （区别于"文件不存在"，对应架构设计 12.D.3）。
        not_found: 共享根**可达**、但票号/料号/订单目录确实**不存在**
            （→ 上层判 ``NO_IMAGE`` 🔵，正常继续）。
            与 ``unreachable`` **互斥**；``unreachable=True`` 时本字段恒为 ``False``。
        ocr: 该图的 OCR 结果（``OcrText``），未跑 OCR 时为 ``None``。
    """

    image_path: str = ""
    seq: int = 0
    exists: bool = False
    ocr_confidence: float = 0.0
    unreachable: bool = False
    not_found: bool = False
    ocr: OcrText | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（供 JSON 证据落盘）。"""
        return {
            "image_path": self.image_path,
            "seq": self.seq,
            "exists": self.exists,
            "ocr_confidence": self.ocr_confidence,
            "unreachable": self.unreachable,
            "not_found": self.not_found,
            "ocr": self.ocr.to_dict() if self.ocr is not None else None,
        }


@dataclass
class OcrText:
    """单张图片的 OCR 结果。

    Attributes:
        image_path: 图片来源路径。
        text_raw: 识别出的**全部**文本（多行以 ``\\n`` 连接）。
        confidence: 置信度（取最小 scores，保守估计；0.0 表示无 scores）。
        boxes: 文本框列表（``[(x1,y1,x2,y2), ...]``）。
        seq: 图片序号。
        low_confidence: 是否低于阈值（低于则 NoiseGuard 优先判 SUSPICIOUS）。
        line_scores: 逐行置信度（与 :meth:`lines` 对齐；部分后端的逐行分数）。
            用于 OCR 磁盘缓存 ``lines[].score`` 的无损往返；**不参与判定**。
        kv: 从本图 OCR 行提取的键值对（v0.2.0 点 8）。
            ⚠️ **不参与判定**（Q4 已决）：判定仍走原跨图投票链路，
            KV 的出口只有「详细 JSON 日志」与「复核工作台展示」两处。
    """

    image_path: str = ""
    text_raw: str = ""
    confidence: float = 0.0
    boxes: list[Any] = dc_field(default_factory=list)
    seq: int = 0
    low_confidence: bool = False
    line_scores: list[float] = dc_field(default_factory=list)
    kv: dict[str, str] = dc_field(default_factory=dict)

    def lines(self) -> list[str]:
        """按行拆分 ``text_raw``（去除空行，逐行 strip）。"""
        return [line.strip() for line in (self.text_raw or "").splitlines() if line.strip()]

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（**完整原文只进 JSON 证据文件，不进日志**）。

        ``kv`` 随详细 JSON 落盘（v0.2.0 点 8）；``boxes`` / ``line_scores`` 体积大
        且不参与判定，**不落**详细 JSON（``boxes`` 原有约定保持不变）。
        """
        return {
            "image_path": self.image_path,
            "text_raw": self.text_raw,
            "confidence": self.confidence,
            "seq": self.seq,
            "low_confidence": self.low_confidence,
            "kv": dict(self.kv or {}),
        }


@dataclass
class DeclarationRecord:
    """一条申报记录（Excel 一行 → 内部模型）。

    Attributes:
        ticket_no: 出货通知书号（一级索引，定位 ``{票号}`` 目录）。
        part_no: **成品料号**（内部语义，对应图片文件名**第二段**）。
        order_no: **订单号**（内部语义，对应图片目录名与文件名**首段**）。
        product_name: 中文品名（汇总表辅助列）。
        seq_no: Excel 序号（追溯对齐，**非**图片文件名序号）。
        raw_element_text: 申报要素整段原文（解析输入 + 证据留存）。
        decl_brand: 申报品牌（比对基准）。
        decl_model: 申报型号（比对基准）。
        evidences: 匹配到的图片证据列表。
        structure_variant: 来源 Excel 结构变体（供断点指纹与追溯）。
        source_row: Excel 原始行号（1-based，便于人工回溯）。
    """

    ticket_no: str = ""
    part_no: str = ""
    order_no: str = ""
    product_name: str = ""
    seq_no: int = 0
    raw_element_text: str = ""
    decl_brand: str = ""
    decl_model: str = ""
    evidences: list[ImageEvidence] = dc_field(default_factory=list)
    structure_variant: StructureVariant = StructureVariant.UNKNOWN
    source_row: int = 0

    def key(self) -> str:
        """返回三级索引唯一键（``出货号|料号|订单``）。

        Returns:
            幂等基础键。
        """
        return build_key(self.ticket_no, self.part_no, self.order_no)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "ticket_no": self.ticket_no,
            "part_no": self.part_no,
            "order_no": self.order_no,
            "product_name": self.product_name,
            "seq_no": self.seq_no,
            "raw_element_text": self.raw_element_text,
            "decl_brand": self.decl_brand,
            "decl_model": self.decl_model,
            "structure_variant": self.structure_variant.value,
            "source_row": self.source_row,
            "evidences": [e.to_dict() for e in self.evidences],
        }


@dataclass
class DifferenceDetail:
    """单字段差异明细。

    Attributes:
        field: 字段名（``品牌`` / ``型号``）。
        declared_value: 申报值。
        detected_value: 图片识别值。
        char_diffs: 逐字符差异点列表（SOP 3.4 规则 5：型号不一致必须列出不同点）。
        note: 补充说明（如"外箱整机品牌，不构成本体证据"）。
    """

    field: str = ""
    declared_value: str = ""
    detected_value: str = ""
    char_diffs: list[str] = dc_field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "field": self.field,
            "declared_value": self.declared_value,
            "detected_value": self.detected_value,
            "char_diffs": list(self.char_diffs),
            "note": self.note,
        }

    def summary(self, *, include_head: bool = True, include_note: bool = True) -> str:
        """生成人类可读的一行差异描述（用于「判定依据」列 / 复核清单「问题说明」）。

        Args:
            include_head: 是否输出「字段：申报『x』 vs 图片『y』」**对照句**。
                当该对照**已经出现在** ``CheckResult.reason`` 里时，调用方应传
                ``False`` —— 只保留字段名前缀，避免同一层依据在同一格里重复两遍（v0.3.6）。
            include_note: 是否附上 :attr:`note`。同因：``note`` 已在 ``reason`` 里
                出现时应传 ``False``。

        Returns:
            一行差异描述（字段级对照 + 逐字符差异 + 可选备注）。
        """
        if include_head:
            head = (
                f"{self.field}：申报『{self.declared_value}』 vs 图片『{self.detected_value}』"
            )
        else:
            head = f"{self.field}：" if self.field else ""
        if self.char_diffs:
            # 有对照句时用「；」分隔；仅有字段名前缀时直接衔接（``品牌：差异点：…``）
            head += ("；" if include_head else "") + "差异点：" + "".join(self.char_diffs)
        if include_note and self.note:
            head += f"（{self.note}）"
        return head


@dataclass
class CheckResult:
    """单条记录的校验结果（判定引擎输出，UI 与导出共同消费）。

    Attributes:
        key: 三级索引唯一键（与 :meth:`DeclarationRecord.key` 一致）。
        record: 源申报记录。
        verdict: 四类判定之一。
        detected_brand: 图片识别品牌。
        detected_model: 图片识别型号。
        differences: 差异明细列表。
        evidence_text: 证据文本片段（**不打印完整 OCR 原文**）。
        image_paths: 涉及的图片路径列表（``;`` 连接供导出）。
        noise_level: 疑似噪声三态。
        manually_reviewed: 是否已人工复核。
        reviewer_note: 复核备注。
        reason: 判定理由（一句话，进日志 / 汇总表）。
        evidence_images: 详细证据（图片 + OCR 原文）—— **只进 JSON**。
        unreachable: **共享盘不可达**结构化标志位（架构设计 13.13.4）。
            仅当证据中存在 ``ImageEvidence.unreachable == True`` 时置位。
            **与** ``not_found`` **严格互斥**：目录不存在（``not_found``）只判 🔵，
            **不置** 本字段。
            ⚠️ **T05 自动暂停的唯一判据**：条件是「连续 K 条 ``unreachable==True``」；
            **严禁**改用「连续 K 条 ``NO_IMAGE``」——用户票号填错时会误挂整批。
        token_matches: 品牌 / 型号的**完整分词命中结果**（v0.3.0 需求 1，各一条）。
            ⚠️ **绝不进 13 列汇总表**：只进 :meth:`to_dict`（详细 JSON 日志）与复核工作台
            （「判定链路」三列的「判定值」来源），保证 :meth:`to_row` 列数恒为 13。
    """

    key: str = ""
    record: DeclarationRecord | None = None
    verdict: Verdict = Verdict.NO_IMAGE
    detected_brand: str = ""
    detected_model: str = ""
    differences: list[DifferenceDetail] = dc_field(default_factory=list)
    evidence_text: str = ""
    image_paths: str = ""
    noise_level: NoiseLevel = NoiseLevel.DEFINITE_MATCH
    manually_reviewed: bool = False
    reviewer_note: str = ""
    reason: str = ""
    evidence_images: list[ImageEvidence] = dc_field(default_factory=list)
    #: 共享盘不可达标志（**不占 13 列**；仅进 ``to_dict``，供 T05 结构化暂停计数）
    unreachable: bool = False
    #: 完整分词命中结果（**不占 13 列**；仅进 ``to_dict`` + 复核工作台）
    token_matches: list[TokenMatch] = dc_field(default_factory=list)
    #: 【v0.3.7 需求 4】**人工重判前**的系统判定（``None`` = 本条从未被人工重判）。
    #:
    #: 「重判结果不覆盖原系统产生的结果」在**数据层**的落点：
    #: :attr:`verdict` 会被人工复核覆盖（UI / 导出的复核后版本按它走），
    #: 而**系统原本判成什么**由本字段原样保留 —— 两次及以上的重判
    #: **只在第一次**记录，后续重判不得改写它（否则会退化成"上次重判值"而非"系统值"）。
    original_verdict: Verdict | None = None
    #: 最近一次人工重判的时间（ISO 8601；未重判为空串）
    reviewed_at: str = ""

    def manual_review_mark(self) -> str:
        """返回「人工重判」标识文案（未人工重判返回空串）。

        ⚠️ **单一出口**：复核工作台记录表的「复核」列与**复核后版本**汇总表 /
        复核清单的「人工复核」列**必须**都调用本方法 —— 两处各写一遍就是漂移温床。

        Returns:
            :data:`core.constants.MANUAL_REVIEW_MARK` 或空串。
        """
        from core import constants as _C

        return _C.MANUAL_REVIEW_MARK if self.manually_reviewed else ""

    def original_verdict_text(self) -> str:
        """返回**人工重判前**的系统判定文案（口径字符串；未重判返回空串）。

        Returns:
            如 ``❌ 校验异常``；:attr:`original_verdict` 为 ``None`` 时返回空串。
        """
        from core import constants as _C

        if self.original_verdict is None:
            return ""
        return _C.verdict_text(self.original_verdict)

    def token_match_for(self, field_name: str) -> TokenMatch | None:
        """取某字段的完整分词命中结果（无则 ``None``）。

        Args:
            field_name: 字段名（``品牌`` / ``型号``）。

        Returns:
            :class:`core.token_matcher.TokenMatch` 或 ``None``。
        """
        for match in self.token_matches:
            if getattr(match, "field", "") == field_name:
                return match
        return None

    def verdict_basis(self) -> str:
        """拼装**「判定依据」**文本（v0.3.6，用户裁定 2026-09-17）。

        语义：``判定依据`` = 「**为什么**给出这个结论」的完整文字留痕，
        **每一条记录都有**（✅ / ❌ / ⚠️ / 🔵 四类均不例外）。三段拼接：

          1. :attr:`reason` —— 判定依据句。四类结论**均有**：
             ✅「…判合格」/ ❌「…明确不一致」/ ⚠️「疑似 OCR 噪声…转人工复核」/
             🔵「…未找到图片」；
          2. 差异明细 —— 字段级对照 ＋ **逐字符差异**（SOP 3.4 规则 5，可追溯红线）；
          3. :attr:`reviewer_note` —— 人工复核填写的内容（若已复核）。

        ⚠️ **去重（必须保留）**：``judge_engine`` 会把字段级依据**同时**写进
        ``reason`` 与 :attr:`DifferenceDetail.note`，无脑拼接会让同一句话在
        同一格内重复两三遍（v0.3.6 首轮端到端实测复现）。因此：

          * ``note`` 已出现在 ``reason`` 里 → 不再附注；
          * 字段级对照（申报值 ＋ 图片值）已由 ``reason`` 表达 → 只补「差异点」，
            不重复对照句；
          * 上述两者**都**冗余且无逐字符差异 → 整条差异跳过。

        ⚠️ **单一出口**：本方法是「判定依据」文本的**唯一**生产者，
        :meth:`to_row`（13 列汇总表第 10 列）与
        :meth:`core.result_exporter.ResultExporter._review_row`（复核清单
        「问题说明」）**必须**都调用它 —— 两处各写一遍就是漂移温床。

        Returns:
            以 ``；`` 连接的判定依据文本；无任何内容时返回空串。
        """
        parts: list[str] = []
        if self.reason:
            parts.append(self.reason)
        for d in self.differences:
            note_in_reason = bool(d.note) and d.note in self.reason
            # 该差异的字段级对照（申报值 + 图片值）是否已由 reason 表达过
            compared_in_reason = (
                (not d.declared_value or d.declared_value in self.reason)
                and (not d.detected_value or d.detected_value in self.reason)
            )
            if compared_in_reason and not d.char_diffs and note_in_reason:
                continue  # 该差异的全部信息都已在 reason 里 → 不重复
            parts.append(
                d.summary(
                    include_head=not compared_in_reason,
                    include_note=bool(d.note) and not note_in_reason,
                )
            )
        if self.reviewer_note:
            parts.append(f"复核备注：{self.reviewer_note}")
        return "；".join(p for p in parts if p)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（含完整证据，供 JSON 详细日志）。"""
        return {
            "key": self.key,
            "verdict": self.verdict.value,
            "detected_brand": self.detected_brand,
            "detected_model": self.detected_model,
            "differences": [d.to_dict() for d in self.differences],
            "evidence_text": self.evidence_text,
            "image_paths": self.image_paths,
            "noise_level": self.noise_level.value,
            "manually_reviewed": self.manually_reviewed,
            "reviewer_note": self.reviewer_note,
            "reason": self.reason,
            "record": self.record.to_dict() if self.record is not None else None,
            "evidence_images": [e.to_dict() for e in self.evidence_images],
            "unreachable": self.unreachable,
            "token_matches": [m.to_dict() for m in self.token_matches],
            # v0.3.7 需求 4：原系统判定 + 复核时间（**不占 13 列**，只进详细 JSON）
            "original_verdict": (
                self.original_verdict.value if self.original_verdict is not None else None
            ),
            "original_verdict_text": self.original_verdict_text(),
            "reviewed_at": self.reviewed_at,
        }

    def to_row(self) -> list[Any]:
        """返回 13 列汇总表的一行（**列顺序由 ResultExporter.COLUMNS 决定**）。

        本方法仅按固定顺序拼装值，具体列名由 ``core.result_exporter`` 定义，
        保持单一事实来源。

        ⚠️ **13 列结构已冻结**（``constants.COLUMNS``，QA 断言恰 13 列）：
        本方法**不得**因新增字段（如 ``unreachable``）而增列。需要额外信息的字段
        只进 :meth:`to_dict`（JSON 详细日志），不进汇总表。

        Returns:
            13 个值的列表（顺序与 ``ResultExporter.COLUMNS`` 严格对应）。
        """
        # 延迟 import 避免循环依赖（constants 不依赖 models 的业务类）
        from core import constants as _C

        record = self.record
        ticket_no = record.ticket_no if record is not None else ""
        part_no = record.part_no if record is not None else ""
        order_no = record.order_no if record is not None else ""
        product_name = record.product_name if record is not None else ""
        decl_brand = record.decl_brand if record is not None else ""
        decl_model = record.decl_model if record is not None else ""

        # ── 第 10 列「判定依据」（v0.3.6，用户裁定 2026-09-17）─────────────
        # 口径变化：由「差异备注」（**只在存在差异时才有内容** → ✅ 行整格为空）
        # 改为「判定依据」（**每一条记录**都写明依据）。
        # 拼装逻辑收口到 :meth:`verdict_basis`（与复核清单「问题说明」共用同一出口，
        # 避免两处各写一遍造成漂移）。
        verdict_basis = self.verdict_basis()

        return [
            ticket_no,                                # 1  出货通知书号
            part_no,                                  # 2  成品料号
            order_no,                                 # 3  订单号
            product_name,                             # 4  中文品名
            decl_brand,                               # 5  申报品牌
            decl_model,                               # 6  申报型号
            self.detected_brand,                      # 7  图片识别品牌
            self.detected_model,                      # 8  图片识别型号
            _C.verdict_text(self.verdict),            # 9  校验结果
            verdict_basis,                            # 10 判定依据（v0.3.6）
            self.evidence_text,                       # 11 证据（OCR 片段）
            self.image_paths,                         # 12 图片路径
            self.reason,                              # 13 判定说明
        ]


@dataclass
class FieldMapping:
    """列名 → 内部字段映射（ExcelProbe 产出，对应架构设计 6.2/6.3）。

    Attributes:
        col_map: ``{内部字段名: Excel 列索引(0-based)}``。
        header_row: 表头行号（0-based）。
        ticket_no: 提取到的票号。
        hit_rate: 图片命中率探针结果（0.0–1.0）。
        swapped: 是否触发了 ``part_no`` / ``order_no`` 交换自校正（P4 场景）。
        channel: 命中的映射通道（``literal`` / ``structure`` / ``probe``）。
        notes: 映射过程中的说明（进日志）。
    """

    col_map: dict[str, int] = dc_field(default_factory=dict)
    header_row: int = 0
    ticket_no: str = ""
    hit_rate: float = 0.0
    swapped: bool = False
    channel: str = "literal"
    notes: list[str] = dc_field(default_factory=list)

    def resolve(self, field_name: str) -> int:
        """返回内部字段对应的 Excel 列索引。

        Args:
            field_name: 内部字段名（如 ``part_no``）。

        Returns:
            0-based 列索引；未映射返回 ``-1``。
        """
        return self.col_map.get(field_name, -1)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "col_map": dict(self.col_map),
            "header_row": self.header_row,
            "ticket_no": self.ticket_no,
            "hit_rate": self.hit_rate,
            "swapped": self.swapped,
            "channel": self.channel,
            "notes": list(self.notes),
        }


@dataclass
class ExcelProbeResult:
    """Excel 结构探查结果（ExcelProbe 产出）。

    Attributes:
        variant: 结构变体（A–E）。
        sheet_name: 选定的要素表 Sheet 名。
        header_row: 表头行号（0-based）。
        ticket_no: 票号。
        mapping: 字段映射。
        records: 解析出的申报记录列表。
        notes: 探查说明（进日志）。
    """

    variant: StructureVariant = StructureVariant.UNKNOWN
    sheet_name: str = ""
    header_row: int = 0
    ticket_no: str = ""
    mapping: FieldMapping = dc_field(default_factory=FieldMapping)
    records: list[DeclarationRecord] = dc_field(default_factory=list)
    notes: list[str] = dc_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（不含 records，避免过大）。"""
        return {
            "variant": self.variant.value,
            "sheet_name": self.sheet_name,
            "header_row": self.header_row,
            "ticket_no": self.ticket_no,
            "mapping": self.mapping.to_dict(),
            "record_count": len(self.records),
            "notes": list(self.notes),
        }


@dataclass
class ParsedElement:
    """申报要素解析结果（ElementParser 产出）。

    Attributes:
        brand: 解析出的品牌（``""`` 表示无）。
        model: 解析出的型号（``""`` 表示无）。
        fields: 解析出的全部 ``键: 值`` 对（调试 / 证据用）。
        notes: 解析说明（护栏命中等）。
    """

    brand: str = ""
    model: str = ""
    fields: list[tuple[str, str]] = dc_field(default_factory=list)
    notes: list[str] = dc_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "brand": self.brand,
            "model": self.model,
            "fields": [list(pair) for pair in self.fields],
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class Fingerprint:
    """断点指纹（对应架构设计 12.A.3，防串票）。

    断点与数据源绑定校验的判据集合，**全部满足**才算匹配。

    Attributes:
        ticket_no: 票号（严格相等）。
        excel_path: Excel 路径（规范化）。
        excel_hash: Excel 文件字节 sha256（**首选判据**）。
        share_root: 图片根目录（规范化）。
        variant: 结构变体（结构变了则断点不可信）。
        total_count: 本次预计记录总数。
    """

    ticket_no: str = ""
    excel_path: str = ""
    excel_hash: str = ""
    share_root: str = ""
    variant: str = ""
    total_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "ticket_no": self.ticket_no,
            "excel_path": self.excel_path,
            "excel_hash": self.excel_hash,
            "share_root": self.share_root,
            "variant": self.variant,
            "total_count": self.total_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fingerprint:
        """从字典构造（容忍缺失字段）。"""
        data = data or {}
        return cls(
            ticket_no=str(data.get("ticket_no", "") or ""),
            excel_path=str(data.get("excel_path", "") or ""),
            excel_hash=str(data.get("excel_hash", "") or ""),
            share_root=str(data.get("share_root", "") or ""),
            variant=str(data.get("variant", "") or ""),
            total_count=int(data.get("total_count", 0) or 0),
        )

    def is_same_source(self, other: Fingerprint) -> bool:
        """判定两个指纹是否指向**同一数据源**（断点可复用）。

        规则（架构设计 12.A.3）：
          * ``ticket_no`` 严格相等（必查）；
          * ``excel_hash`` 严格相等（主判据）；若双方 hash 均非空则必查；
          * ``share_root`` 规范化后相等（必查）；
          * ``variant`` 相等（必查）。

        Args:
            other: 当前数据源的指纹。

        Returns:
            ``True`` 表示断点可复用。
        """
        if self.ticket_no != other.ticket_no:
            return False
        if self.share_root != other.share_root:
            return False
        if self.variant != other.variant:
            return False
        if self.excel_hash and other.excel_hash:
            return self.excel_hash == other.excel_hash
        # hash 为空时回退到路径比对（记 WARN 由调用方负责）
        return self.excel_path == other.excel_path


@dataclass
class ResumeSnapshot:
    """断点快照（``ResumeStore.load_if_match()`` 返回）。

    Attributes:
        fingerprint: 断点绑定的数据源指纹。
        done_keys: 已完成的 key 集合。
        updated_at: 断点最后更新时间（ISO 8601）。
    """

    fingerprint: Fingerprint = dc_field(default_factory=Fingerprint)
    done_keys: set[str] = dc_field(default_factory=set)
    updated_at: str = ""

    @property
    def done_count(self) -> int:
        """已完成条数。"""
        return len(self.done_keys)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 ``resume.json`` v2 结构。"""
        return {
            "schema": 2,
            "fingerprint": self.fingerprint.to_dict(),
            "done_keys": sorted(self.done_keys),
            "updated_at": self.updated_at,
        }
