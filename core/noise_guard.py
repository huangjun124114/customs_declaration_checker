"""噪声护栏（core.noise_guard，对应架构设计第 4 节 + 12.R5 + 13.5.2）。

**核心职责**：把「图片侧 OCR 值 vs 申报值」的比对结果**三态化**，以守住
「**不虚高**」红线（SOP 1.2 后果 / README 三条质量红线之一）：

    ==================  ====================================================
    ``NoiseLevel``      语义与触发条件
    ==================  ====================================================
    ``DEFINITE_MATCH``  明确匹配：归一化后完全相等，或双方均为"无"
    ``SUSPICIOUS``      疑似噪声：易混字符归一化后相等 / 编辑距离 ≤ 2 且长度 ≥ 5
                        / OCR 低置信度 / 残片。**强制降级 ⚠️，绝不直达 ❌**
    ``CLEAR_MISMATCH``  明确不一致：证据充分（长度足够、非易混、低相似、非残片）
    ==================  ====================================================

**规则外置**：黑名单 / 整机词表 / 易混字符 / 模糊阈值**全部从注入的**
:class:`core.rule_repository.RuleRepository` 读取；``core.constants`` 的
``DEFAULT_*`` 仅作**兜底默认值**（YAML 缺失时使用）。

**口径声明**：本模块**不新增、不改写、不弱化**任何 SOP 判定口径。它只是把
SOP 1.3「OCR 证据不足（残片、噪声大）→ ⚠️」与 SOP 1.2「不虚高」落成可执行条目
（架构设计 13.5.2 明确：这是"补规则"而非"改口径"）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from core import constants
from core.models import NoiseLevel, OcrText
from core.rule_repository import NoiseSignalRules, RuleRepository, WholeMachineBrandRules

__all__ = [
    "edit_distance",
    "is_none_token",
    "NoiseVerdict",
    "NoiseGuard",
]


#: 视为"无"的取值（兜底默认；权威载体为 ``rules/brand_patterns.yaml`` 的 ``none_tokens``）
_DEFAULT_NONE_TOKENS: tuple[str, ...] = tuple(constants.DEFAULT_NONE_TOKENS)

#: "品牌标签"标记（用于定位 Brand 行；**这些词本身不算"整机上下文"**）。
#:
#: ⚠️ 关键实现细节：``rules/whole_machine_brand.yaml`` 的 ``context_tokens`` 里
#: 同时包含"Brand 标签"（``Brand:`` / ``品牌:``）与"整机上下文"（``CARTON`` / ``JOBNO``）。
#: 前者只用于**定位** Brand 行，若把它也当作"上下文命中"，则任何 ``品牌:xxx`` 行都会
#: 自我命中 → 所有记录都被误判"外箱整机品牌"。故此处显式排除，只让真正的整机上下
#: 文 token（``CARTON`` 等）触发命中。**这属实现层修正，不改变口径语义**。
_BRAND_LABEL_MARKERS: tuple[str, ...] = (
    "Brand:",
    "Brand：",
    "品牌:",
    "品牌：",
    "BRAND:",
    "BRAND：",
)


def edit_distance(left: str, right: str) -> int:
    """计算两字符串的 Levenshtein 编辑距离（含插入 / 删除 / 替换）。

    使用滚动数组实现，空间复杂度 O(min(m, n))，时间复杂度 O(m·n)。

    Args:
        left: 左侧字符串（不区分大小写，内部统一 ``upper``）。
        right: 右侧字符串。

    Returns:
        最小编辑次数（两串完全相同返回 ``0``）。

    Examples:
        >>> edit_distance("SKYWORTH P/N", "SKYHORTH P/H")
        2
        >>> edit_distance("abc", "abc")
        0
    """
    a = ("" if left is None else str(left)).upper()
    b = ("" if right is None else str(right)).upper()

    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    # 滚动数组：previous_row / current_row
    previous = list(range(len(b) + 1))
    for i, ch_a in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, ch_b in enumerate(b, start=1):
            cost = 0 if ch_a == ch_b else 1
            current[j] = min(
                previous[j] + 1,        # 删除
                current[j - 1] + 1,     # 插入
                previous[j - 1] + cost,  # 替换
            )
        previous = current
    return previous[len(b)]


def is_none_token(value: str, none_tokens: tuple[str, ...] | None = None) -> bool:
    """判断某取值是否代表"无品牌 / 无型号"。

    SOP 3.4 补充约定 + 陷阱#7：标签上的品牌字段留空率极高，``无`` / ``N/A`` /
    ``NONE`` / 空串 等均应视为"无"，**不得据此判异常**。

    Args:
        value: 待判定值。
        none_tokens: "无"取值集合；``None`` 时用兜底默认表。

    Returns:
        ``True`` 表示该值等价于"无"。
    """
    tokens = none_tokens if none_tokens is not None else _DEFAULT_NONE_TOKENS
    text = ("" if value is None else str(value)).strip()
    if not text:
        return True
    upper = text.upper()
    for token in tokens:
        normalized = ("" if token is None else str(token)).strip()
        if not normalized:
            continue
        if upper == normalized.upper():
            return True
    return False


@dataclass
class NoiseVerdict:
    """单字段（品牌 / 型号）的噪声三态判定结果。

    Attributes:
        level: 三态之一（``DEFINITE_MATCH`` / ``SUSPICIOUS`` / ``CLEAR_MISMATCH``）。
        reason: 命中依据（一句话，进日志 / 汇总表「判定说明」）。
        signals: 命中的信号标签列表（如 ``["confusable_char"]`` / ``["fuzzy_similarity"]``）。
        both_absent: 是否双方均为"无"（决定 Judge 走「双方均无 → ✅」分支）。
        is_whole_machine: 是否命中外箱整机品牌上下文（不构成本体证据）。
    """

    level: NoiseLevel = NoiseLevel.DEFINITE_MATCH
    reason: str = ""
    signals: list[str] = field(default_factory=list)
    both_absent: bool = False
    is_whole_machine: bool = False


class NoiseGuard:
    """疑似噪声三态护栏（架构设计第 4 节 ``NoiseGuard``）。

    规则来源**全部外置**（v1.1 C / 13.5.2）：
      * ``confusable_chars``     —— 易混字符归一化（并查集等价类，见 ``NoiseSignalRules``）
      * ``fuzzy_max_edit_distance`` / ``fuzzy_min_length`` —— 模糊相似度阈值
      * ``low_confidence_threshold`` —— 低置信度阈值
      * ``fragment_min_chars``   —— 残片阈值
      * ``whole_machine_brand.context_tokens`` / ``context_window`` —— 外箱整机品牌上下文

    Attributes:
        rules: 注入的 :class:`core.rule_repository.RuleRepository`（规则来源）。
    """

    def __init__(self, rules: RuleRepository | None = None) -> None:
        """构造护栏。

        Args:
            rules: 规则仓库；``None`` 时回退到 :mod:`core.constants` 的兜底默认值
                （便于无 YAML 环境下的单元测试）。
        """
        self._repo: RuleRepository | None = rules
        self._noise: NoiseSignalRules = self._resolve_noise_rules()
        self._whole: WholeMachineBrandRules = self._resolve_whole_rules()

    # ══════════════════════════════════════════════════════════
    #  规则解析（外置优先，constants 兜底）
    # ══════════════════════════════════════════════════════════

    def _resolve_noise_rules(self) -> NoiseSignalRules:
        """解析噪声信号规则（YAML 优先，constants 兜底）。

        ⚠️ **缺陷 F 教训（本方法为修复现场）**：兜底分支必须与 repo 路径**规则集等价**
        —— 尤其是 ``known_noise_samples``（漏传会让「已知误读自动纠正」整条链路在
        "YAML 缺失 / 打包漏拷"时**静默失效**，表现为"源码态全绿、打包后判定全变"）。
        故此处**逐字段**从 :mod:`core.constants` 兜底默认值取值，并由
        ``tests/test_noise_guard.py::TestFallbackEquivalence`` 断言与 repo 快照等价。
        """
        if self._repo is not None:
            try:
                snapshot = self._repo.get()
                noise = snapshot.noise_signals
                if noise.confusable_chars:
                    return noise
            except Exception:  # noqa: BLE001 - 规则加载失败时退回兜底，保证不崩
                pass
        return NoiseSignalRules(
            confusable_chars=tuple(
                tuple(group) for group in constants.DEFAULT_CONFUSABLE_CHARS
            ),
            fuzzy_max_edit_distance=constants.DEFAULT_FUZZY_MAX_EDIT_DISTANCE,
            fuzzy_min_length=constants.DEFAULT_FUZZY_MIN_LENGTH,
            fuzzy_verdict=constants.DEFAULT_FUZZY_VERDICT.value,
            low_confidence_threshold=constants.DEFAULT_LOW_CONFIDENCE_THRESHOLD,
            fragment_min_chars=constants.DEFAULT_FRAGMENT_MIN_CHARS,
            known_noise_samples=tuple(
                dict(sample) for sample in constants.DEFAULT_KNOWN_NOISE_SAMPLES
            ),
            letter_diff_min_length=constants.DEFAULT_LETTER_DIFF_MIN_LENGTH,
            letter_diff_max_edit_distance=(
                constants.DEFAULT_LETTER_DIFF_MAX_EDIT_DISTANCE
            ),
            letter_diff_verdict=constants.DEFAULT_LETTER_DIFF_VERDICT.value,
        )

    def _resolve_whole_rules(self) -> WholeMachineBrandRules:
        """解析外箱整机品牌上下文规则（YAML 优先，constants 兜底）。"""
        if self._repo is not None:
            try:
                whole = self._repo.get().whole_machine_brand
                if whole.context_tokens:
                    return whole
            except Exception:  # noqa: BLE001
                pass
        return WholeMachineBrandRules(
            context_tokens=tuple(constants.DEFAULT_WHOLE_MACHINE_CONTEXT),
            context_window=3,
            context_scope=constants.DEFAULT_WHOLE_MACHINE_SCOPE,
            verdict_hint="外箱整机品牌，不构成本体证据",
        )

    # ─────────────────────── 属性（供测试与上层读取）───────────────────────

    @property
    def noise_rules(self) -> NoiseSignalRules:
        """当前生效的噪声信号规则快照。"""
        return self._noise

    @property
    def whole_machine_rules(self) -> WholeMachineBrandRules:
        """当前生效的外箱整机品牌规则快照。"""
        return self._whole

    # ══════════════════════════════════════════════════════════
    #  公共接口（对应架构设计第 4 节类图）
    # ══════════════════════════════════════════════════════════

    def classify(
        self,
        declared: str,
        detected: str,
        *,
        ocr_texts: list[OcrText] | None = None,
        field_name: str = "",
    ) -> NoiseLevel:
        """对单字段做噪声三态分类（架构设计第 4 节签名）。

        判定顺序（**首个命中即返回**，顺序本身即"不虚高"的实现）：

          1. 双方均为"无" → ``DEFINITE_MATCH``（对应 SOP 3.4「双方均为无 → ✅」）
          2. 归一化后完全相等 → ``DEFINITE_MATCH``
          3. 易混字符归一化后相等 → ``SUSPICIOUS``（SOP 陷阱#6）
          4. 编辑距离 ≤ 阈值且双方长度 ≥ 最小长度 → ``SUSPICIOUS``（13.5.2）
          4′. 双方**纯英文**、长度均 > 3、只有字母之差（编辑距离 ≤ 上限）
             → ``SUSPICIOUS``（⚠️ 口径 v0.3.2：列待复核，绝不直达 ❌）
          5. HTTP 低置信度 OCR（任一文本 ``low_confidence`` 或低于阈值）→ ``SUSPICIOUS``
          6. 残片（识别值过短且无字母/数字主体）→ ``SUSPICIOUS``
          7. 其余 → ``CLEAR_MISMATCH``（证据充分，允许 ❌）

        Args:
            declared: 申报值（品牌 / 型号）。
            detected: 图片识别值。
            ocr_texts: 该记录的 OCR 结果列表（用于低置信度 / 残片信号）。
            field_name: 字段名（``品牌`` / ``型号``，仅用于 reason 文案）。

        Returns:
            :class:`core.models.NoiseLevel` 三态之一。
        """
        return self.classify_detail(
            declared, detected, ocr_texts=ocr_texts, field_name=field_name
        ).level

    def classify_detail(
        self,
        declared: str,
        detected: str,
        *,
        ocr_texts: list[OcrText] | None = None,
        field_name: str = "",
        whole_machine: bool = False,
    ) -> NoiseVerdict:
        """对单字段做噪声三态分类，返回**含命中依据**的详细结果。

        与 :meth:`classify` 的关系：本方法是完整实现，``classify`` 是便捷包装。

        Args:
            declared: 申报值。
            detected: 图片识别值。
            ocr_texts: OCR 结果列表（低置信度 / 残片信号）。
            field_name: 字段名（进 reason 文案）。
            whole_machine: 是否已知命中外箱整机品牌上下文（由 :meth:`is_whole_machine_brand` 判定）。

        Returns:
            :class:`NoiseVerdict`。
        """
        left = ("" if declared is None else str(declared)).strip()
        right = ("" if detected is None else str(detected)).strip()
        label = field_name or "字段"

        # ── 裁决 2（架构裁决文档 §4.6）：OCR 已知误读 → **自动纠正后比对** ──
        #     业务口径（Jason）：否决"已知误读独立判噪（转 ⚠️）"，改采"自动纠正为
        #     正确品牌后，再与申报值比对"：一致 → ✅；不一致 → ❌（依据 = corrected ≠ declared）。
        #
        #     数据源（全部外置 `rules/noise_signals.yaml`）：
        #       * `confusable_chars`               —— 易混字符归一化（已生效，下方 ③ 分支）
        #       * `known_noise_samples[].ocr/ocr_prefix`   —— 实测误读形态
        #       * `known_noise_samples[].expected_actual`  —— 纠正后确定值
        #
        #     ⚠️ 归一化先于比对：命中样本 → `right` 替换为 `expected_actual`，随后
        #     走**同一套** ①/②/③/④ 比对链；**不再**直接把已知误读判 ⚠️。
        #     ⚠️ 不违反「不虚高」：先修复噪声、再据修复后的**确定值**判定 ——
        #     纠正后的不一致是"有证据证明不一致"（SOP 1.3/3.4 第 147 行），
        #     与"拿噪声当差异"方向相反。
        correction_sample = self._lookup_known_sample(right)
        correction_applied = False
        original_right = right
        if correction_sample is not None:
            expected = str(correction_sample.get("expected_actual", "") or "").strip()
            if expected and expected != right:
                right = expected
                correction_applied = True

        both_left_absent = is_none_token(left)
        both_right_absent = is_none_token(right)

        # ① 双方均为"无" → 明确匹配（SOP 3.4：双方均为无 → ✅）
        if both_left_absent and both_right_absent:
            return NoiseVerdict(
                level=NoiseLevel.DEFINITE_MATCH,
                reason=f"{label}：申报与图片均为『无』，判合格",
                signals=["both_absent"],
                both_absent=True,
                is_whole_machine=whole_machine,
            )

        # ② 完全相等 → 明确匹配
        if left == right:
            return NoiseVerdict(
                level=NoiseLevel.DEFINITE_MATCH,
                reason=self._with_correction_note(
                    f"{label}：申报与图片一致『{left}』",
                    original_right,
                    right,
                    correction_applied,
                ),
                signals=self._correction_signals(
                    ["exact_equal"], correction_applied
                ),
                is_whole_machine=whole_machine,
            )

        # ②″ 仅大小写不同 → 明确匹配（**不是噪声**）
        #     品牌/型号的大小写不构成"不一致"：申报口径通常全大写（``DAEWOO``），
        #     而唛头印刷/OCR 常为混合大小写（``Daewoo``）。二者是**同一取值**，
        #     必须判 ``DEFINITE_MATCH``；否则会被后续"易混字符归一化"（其内部会
        #     ``.upper()``）误捕为 ``SUSPICIOUS``，把本该 ✅ 的记录错误降级为 ⚠️。
        if left and right and left.upper() == right.upper():
            return NoiseVerdict(
                level=NoiseLevel.DEFINITE_MATCH,
                reason=self._with_correction_note(
                    f"{label}：申报『{left}』与图片『{right}』仅大小写不同，视为一致",
                    original_right,
                    right,
                    correction_applied,
                ),
                signals=self._correction_signals(
                    ["case_insensitive_equal"], correction_applied
                ),
                is_whole_machine=whole_machine,
            )

        # ③ 易混字符归一化后相等 → 疑似噪声（绝不允许直达 ❌）
        if left and right and self._confusable_equal(left, right):
            return NoiseVerdict(
                level=NoiseLevel.SUSPICIOUS,
                reason=(
                    f"{label}：申报『{left}』与图片『{right}』仅易混字符不同"
                    "（O/0、I/1/l、W/H、N/H 等 OCR 误读），疑似噪声，转人工复核"
                ),
                signals=["confusable_char"],
                is_whole_machine=whole_machine,
            )

        # ④ 模糊相似度（编辑距离 ≤ 阈值 且 双方长度 ≥ 最小长度）→ 疑似噪声
        distance = edit_distance(left, right)
        min_len = self._noise.fuzzy_min_length
        max_dist = self._noise.fuzzy_max_edit_distance
        if (
            left
            and right
            and len(left) >= min_len
            and len(right) >= min_len
            and distance <= max_dist
        ):
            return NoiseVerdict(
                level=NoiseLevel.SUSPICIOUS,
                reason=(
                    f"{label}：申报『{left}』与图片『{right}』编辑距离 {distance} ≤ "
                    f"{max_dist} 且长度均 ≥ {min_len}，疑似 OCR 噪声，转人工复核"
                ),
                signals=["fuzzy_similarity"],
                is_whole_machine=whole_machine,
            )

        # ④′ 【口径 v0.3.2】英文「只差字母」→ 疑似噪声（用户裁定 2026-09-17）
        #     申报值与识别值**均为纯英文字母**、长度**均 > 3 个字母**、且只有字母之差
        #     （编辑距离 ≤ 上限）→ OCR 极可能只是读错了字母 → **列待复核（⚠️）**，
        #     并由上层把逐字符差异写进「判定依据」供人工判断。
        #
        #     ⚠️ 为什么单列一条：通用模糊相似度（④）要求长度 ≥ 5，长度恰为 4 的
        #        纯英文串会直接落到 ⑦「明确不一致（❌）」→ 假异常。
        #     ⚠️ 判定**强制 SUSPICIOUS**（不虚高红线）；``letter_diff_verdict`` 仅供
        #        ``RuleRepository.validate()`` 校验与文档说明，与 ``fuzzy_verdict`` 同处理。
        #     ⚠️ 边界：仅对**纯英文字母**生效 —— 含数字/符号的型号不适用
        #        （数字之差是实体差异，非字母误读）。
        if self._is_letter_only_difference(left, right):
            return NoiseVerdict(
                level=NoiseLevel.SUSPICIOUS,
                reason=(
                    f"{label}：申报『{left}』与图片『{right}』均为英文、长度均 ≥ "
                    f"{self._noise.letter_diff_min_length}，且仅字母之差"
                    f"（编辑距离 {distance} ≤ {self._noise.letter_diff_max_edit_distance}），"
                    "疑似 OCR 字母误读，转人工复核"
                ),
                signals=["letter_only_difference"],
                is_whole_machine=whole_machine,
            )

        # ⑤ 低置信度 OCR → 疑似噪声
        if ocr_texts and self._has_low_confidence(ocr_texts):
            return NoiseVerdict(
                level=NoiseLevel.SUSPICIOUS,
                reason=(
                    f"{label}：OCR 置信度低于阈值 "
                    f"{self._noise.low_confidence_threshold}，证据不足，转人工复核"
                ),
                signals=["low_confidence"],
                is_whole_machine=whole_machine,
            )

        # ⑥ 残片 → 疑似噪声
        if self._is_fragment(right):
            return NoiseVerdict(
                level=NoiseLevel.SUSPICIOUS,
                reason=(
                    f"{label}：图片识别值『{right}』为残片（字符数 < "
                    f"{self._noise.fragment_min_chars} 且无字母/数字主体），证据不足，转人工复核"
                ),
                signals=["fragment"],
                is_whole_machine=whole_machine,
            )

        # ⑦ 其余 → 明确不一致（证据充分，允许 ❌）
        return NoiseVerdict(
            level=NoiseLevel.CLEAR_MISMATCH,
            reason=self._with_correction_note(
                f"{label}：申报『{left}』与图片『{right}』明确不一致",
                original_right,
                right,
                correction_applied,
            ),
            signals=self._correction_signals(["clear_mismatch"], correction_applied),
            is_whole_machine=whole_machine,
        )

    # ─────────────────────── 裁决 2 辅助（误读纠正）───────────────────────

    @staticmethod
    def _correction_signals(
        signals: list[str], correction_applied: bool
    ) -> list[str]:
        """在信号列表前置 ``known_misread_corrected``（命中已知误读并纠正时）。

        Args:
            signals: 原信号列表。
            correction_applied: 是否应用了纠正。

        Returns:
            可能追加了 ``known_misread_corrected`` 的信号列表（新列表，不改原值）。
        """
        result = list(signals)
        if correction_applied and "known_misread_corrected" not in result:
            result.insert(0, "known_misread_corrected")
        return result

    @staticmethod
    def _with_correction_note(
        reason: str,
        original: str,
        corrected: str,
        correction_applied: bool,
    ) -> str:
        """在 reason 中追加「已知误读已纠正」说明（供审计追溯，裁决 2）。

        Args:
            reason: 原始说明。
            original: 纠正前识别值。
            corrected: 纠正后确定值。
            correction_applied: 是否应用了纠正。

        Returns:
            追加纠正说明后的 reason（未纠正时原样返回）。
        """
        if not correction_applied:
            return reason
        return f"{reason}（已知 OCR 误读『{original}』已纠正为『{corrected}』）"

    def is_whole_machine_brand(
        self,
        text: str,
        *,
        lines: list[str] | None = None,
        brand_value: str = "",
    ) -> bool:
        """判断 OCR 文本是否为「外箱整机品牌」（SOP 3.5 规则#8）。

        判定条件：文本中存在 ``Brand:`` / ``品牌:`` 形式的品牌行，**且**同一文本内
        出现任一上下文 token（``CARTON`` / ``JOB NO`` / ``唛头`` 等）。

        **检索范围（缺陷 B 裁决，架构裁决文档第 3 节）**：

        * ``context_scope == "whole_image"``（默认）→ **整图全文检索**，取消行距限制。
          依据：SOP 3.5 规则#8 原文只说 Brand 行"**伴随** ``CARTON``/``JOBNO``
          **上下文**"（= 同一张唛头上共现），**没有"3 行"约束**；"前后 3 行"（规则#2）
          是「**取字段值**」场景的约束，此前误套到「**字段间共现**」（规则#8）上，
          属跨规则误用。实测证据：``Brand:Daewoo`` 与 ``CARTON No.`` 垂直相距 7–8 行，
          固定 3 行窗口必然漏判，导致同构图判定自相矛盾。
        * 其它 ``context_scope`` → 维持 ``context_window`` ± 邻域检索（旧行为，兼容）。

        ⚠️ **约束**：检索范围**严格限定在同一张图（同一 ``text``）内** ——
        绝不会把不同图的行拼接后检索（防跨图污染）。因此调用方须**逐图调用**
        （``judge_engine`` 是逐图判定的）。

        命中语义（**口径不变**）：唛头上的 ``Brand:Daewoo`` 是**整机适配品牌**，
        不构成零部件本体品牌证据 → **保留异常**（不降级为合格）。

        Args:
            text: **单张图**的 OCR 原始文本（多行）。
            lines: 预拆分的行列表（可选，避免重复 split）。
            brand_value: 已知品牌值（可选，用于精确定位含品牌的短行）。

        Returns:
            ``True`` 表示命中"外箱整机品牌"上下文。
        """
        rows = lines if lines is not None else [
            line.strip() for line in (text or "").splitlines() if line.strip()
        ]
        if not rows:
            return False

        # 只保留"真正的整机上下文" token（排除 Brand 标签本身，避免自我命中）
        tokens = [
            t
            for t in self._whole.context_tokens
            if t and not any(marker.lower() == t.lower() for marker in _BRAND_LABEL_MARKERS)
        ]
        if not tokens:
            return False

        # ── 全文检索（缺陷 B）：命中范围 = 本图全部行，不限行距 ──
        if self._uses_whole_image_scope():
            for row in rows:
                if self._looks_like_brand_line(row, brand_value):
                    # 同图内全文共现：Brand 行与整机上下文 token 只要同图即命中
                    if any(token in row2 for row2 in rows for token in tokens):
                        return True

            # 备选：品牌行未识别出来，但全文中 Brand 标签与上下文 token 同时出现
            joined = "\n".join(rows)
            has_brand_label = any(marker in joined for marker in _BRAND_LABEL_MARKERS)
            if has_brand_label and any(token in joined for token in tokens):
                return True
            return False

        # ── 邻域检索（向后兼容：context_scope 非 whole_image）──
        window = max(1, int(self._whole.context_window or 3))
        has_brand_marker = False
        for idx, row in enumerate(rows):
            if self._looks_like_brand_line(row, brand_value):
                has_brand_marker = True
                start = max(0, idx - window)
                end = min(len(rows), idx + window + 1)
                neighborhood = rows[start:end]
                for token in tokens:
                    if any(token in neighbor for neighbor in neighborhood):
                        return True

        # 备选：品牌行未识别出来，但全文中 Brand 标签与上下文 token 同时出现
        if not has_brand_marker:
            joined = "\n".join(rows)
            has_brand_label = any(marker in joined for marker in _BRAND_LABEL_MARKERS)
            if has_brand_label and any(token in joined for token in tokens):
                return True

        return False

    def _uses_whole_image_scope(self) -> bool:
        """当前整机上下文是否采用「整图全文检索」（缺陷 B）。

        Returns:
            ``True`` 表示 ``context_scope == "whole_image"``（默认）。
        """
        scope = (self._whole.context_scope or "").strip().lower()
        return scope == "whole_image"

    def is_known_noise_sample(self, detected: str) -> bool:
        """判断某识别值是否命中「已知 OCR 噪声样本表」（公开接口）。

        样本取自 ``rules/noise_signals.yaml`` 的 ``known_noise_samples``（实测收录
        ``SKYHORTH P/H`` / ``IFR PIN`` / ``boori E339609`` / ``600-CX9`` 等）。

        **缺陷 F 裁决（架构裁决文档 §5B）**：粒度对齐——运行期 ``extract_detected_brand``
        ② 取的是 P/N 行**前缀**（``SKYHORTH`` / ``IFR``），而样本表存的是**整行**
        （``SKYHORTH P/H`` / ``IFR PIN``）。故匹配须同时覆盖 ``ocr``（整行）与
        ``ocr_prefix``（前缀）两种粒度。

        Args:
            detected: 图片识别值（整行文本或 P/N 行前缀）。

        Returns:
            ``True`` 表示命中已知噪声样本。
        """
        return self._lookup_known_sample(detected) is not None

    def correct_known_misread(self, detected: str) -> str:
        """**裁决 2（架构裁决文档 §4.6）**：把已知 OCR 误读纠正为**正确值**。

        流程：识别值 → 归一化/纠错（命中 ``known_noise_samples`` → 取
        ``expected_actual``）→ 返回"纠正后确定值"。未命中 → 返回原值（不纠正）。

        该值是**有证据的确定值**，可直接参与与申报值的比对（一致 → ✅；
        不一致 → ❌）——正落 SOP 1.3/3.4「有证据证明不一致」的“校验异常”定义，
        **不违反**「不虚高」红线（先修复噪声、再据修复后的确定值判定）。

        Args:
            detected: 图片识别值。

        Returns:
            纠正后的确定值；未命中样本时返回 ``detected`` 原值。
        """
        original = ("" if detected is None else str(detected)).strip()
        if not original:
            return original
        sample = self._lookup_known_sample(original)
        if sample is None:
            return original
        expected = str(sample.get("expected_actual", "") or "").strip()
        return expected or original

    def is_cross_script(self, declared: str, detected: str) -> bool:
        """判断申报值与识别值是否分属**不同文字体系**（中 ↔ 拉丁）。

        背景（SOP 附录 A.4）：中文品牌常被 OCR 识别为拼音/英文（``宇同`` ↔ ``YUTONG``），
        此时逐字符比对（字符集都不相交）**不可信**，应判 ``SUSPICIOUS``（⚠️）而非 ❌
        —— 这是"不虚高"红线的直接应用。

        Args:
            declared: 申报值。
            detected: 图片识别值。

        Returns:
            ``True`` 表示一方含中文、另一方为纯拉丁且双方交集为空。
        """
        left = ("" if declared is None else str(declared)).strip()
        right = ("" if detected is None else str(detected)).strip()
        if not left or not right:
            return False

        def has_han(text: str) -> bool:
            return any("\u4e00" <= ch <= "\u9fa5" for ch in text)

        left_han, right_han = has_han(left), has_han(right)
        if left_han == right_han:
            return False
        # 一方中文、一方非中文：若拉丁侧无任何共同字符（大小写无关），视为跨体系
        left_set = {ch.upper() for ch in left if ch.isalnum()}
        right_set = {ch.upper() for ch in right if ch.isalnum()}
        return not (left_set & right_set)

    def is_field_name_misread(self, text: str) -> bool:
        """判断文本是否命中"字段名被误读为品牌值"（SOP 3.5 规则#1/#3）。

        例：OCR 把 ``制造商全称`` / ``创维物料编号`` / ``MFR P/N`` 这一**字段名**
        当作品牌值 → 命中黑名单 → 该值应判为空。

        Args:
            text: 待判定的"品牌值"文本。

        Returns:
            ``True`` 表示该文本是字段名误读（应视为无效值）。
        """
        value = ("" if text is None else str(text)).strip()
        if not value:
            return False
        terms = self._blacklist_terms()
        upper = value.upper()
        for term in terms:
            normalized = ("" if term is None else str(term)).strip()
            if not normalized:
                continue
            if upper == normalized.upper() or normalized.upper() in upper:
                return True
        return False

    # ══════════════════════════════════════════════════════════
    #  内部实现
    # ══════════════════════════════════════════════════════════

    def _blacklist_terms(self) -> tuple[str, ...]:
        """取字段名黑名单（YAML 优先，constants 兜底）。"""
        if self._repo is not None:
            try:
                terms = self._repo.get().fields_blacklist.terms
                if terms:
                    return tuple(terms)
            except Exception:  # noqa: BLE001
                pass
        return tuple(constants.DEFAULT_FIELD_BLACKLIST)

    def _lookup_known_sample(self, detected: str) -> dict[str, str] | None:
        """查找命中的已知 OCR 噪声样本（返回样本字典；未命中返回 ``None``）。

        **缺陷 F 裁决（架构裁决文档 §5B）**：粒度对齐，匹配条件（任一）：

          1. 识别值与样本 ``ocr`` **归一化后相等**（整行形态）；
          2. 识别值与样本 ``ocr_prefix`` **归一化后相等**（运行期前缀形态）；
          3. 识别值**包含**样本 ``ocr``（长度 ≥ 4，容忍前后缀）；
          4. **前缀子串兜底**：识别值 + 空格 + ``P/N`` 归一化后等于样本 ``ocr``
             （即识别值是样本整行的品牌前缀）。

        Args:
            detected: 图片识别值（整行文本或 P/N 行前缀）。

        Returns:
            命中的样本字典（含 ``expected_actual``）；未命中返回 ``None``。
        """
        value = ("" if detected is None else str(detected)).strip()
        if not value:
            return None
        samples = self._noise.known_noise_samples
        if not samples:
            return None
        upper = value.upper()
        normalized = self._normalize_for_sample(value)
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            ocr_sample = str(sample.get("ocr", "") or "").strip()
            prefix_sample = str(sample.get("ocr_prefix", "") or "").strip()

            # ① 整行相等（归一化）
            if ocr_sample:
                if upper == ocr_sample.upper():
                    return sample
                if self._normalize_for_sample(ocr_sample) == normalized:
                    return sample
                # ③ 片段包含：识别值含样本整行（如多行文本中）
                if len(ocr_sample) >= 4 and ocr_sample.upper() in upper:
                    return sample
                # ④ 前缀子串兜底：识别值 + "P/N" 归一后 == 样本整行
                for suffix in ("P/N", "P/H", "PIN"):
                    if self._normalize_for_sample(f"{value}{suffix}") == self._normalize_for_sample(
                        ocr_sample
                    ):
                        return sample

            # ② 前缀粒度相等（缺陷 F 核心：运行期取 P/N 行前缀）
            if prefix_sample:
                if upper == prefix_sample.upper():
                    return sample
                if self._normalize_for_sample(prefix_sample) == normalized:
                    return sample
        return None

    @staticmethod
    def _normalize_for_sample(text: str) -> str:
        """样本比对用的轻量归一化（去掉空白与常见分隔符后大写）。"""
        raw = ("" if text is None else str(text)).upper()
        for ch in (" ", "\t", "/", "-", "_", ".", ":"):
            raw = raw.replace(ch, "")
        return raw

    def _confusable_equal(self, left: str, right: str) -> bool:
        try:
            return self._noise.normalize_confusables(left) == self._noise.normalize_confusables(
                right
            )
        except Exception:  # noqa: BLE001 - 规则异常时退回"不相等"，绝不放宽为相等
            return False

    def _has_low_confidence(self, ocr_texts: list[OcrText]) -> bool:
        """是否存在低置信度 OCR 文本。"""
        threshold = self._noise.low_confidence_threshold
        for text in ocr_texts:
            if text is None:
                continue
            if getattr(text, "low_confidence", False):
                return True
            confidence = float(getattr(text, "confidence", 0.0) or 0.0)
            # 仅当置信度"明确可比较且偏低"时才算低置信（0.0 = 无 scores，不作为噪声依据）
            if 0.0 < confidence < threshold:
                return True
        return False

    @staticmethod
    def _is_ascii_alpha(text: str) -> bool:
        """文本是否为**纯 ASCII 英文字母**（不含数字 / 符号 / 中日韩字符）。

        ⚠️ 不能用 ``str.isalpha()`` —— 它对中文（``isalpha() == True``）同样成立，
        会把中文品牌误纳入「英文只差字母」规则。
        """
        return bool(text) and all("A" <= ch.upper() <= "Z" for ch in text)

    def _is_letter_only_difference(self, left: str, right: str) -> bool:
        """【口径 v0.3.2】是否构成「双方均为纯英文、长度均 > 3、只有字母之差」。

        命中条件（**全部**满足）：

          1. 双方长度均 ≥ ``letter_only_difference.min_length``（默认 4 = "> 3 个字母"）；
          2. 双方均为**纯 ASCII 英文字母**（含数字/符号的型号不适用 —— 数字之差是
             实体差异，不是字母误读）；
          3. 忽略大小写后**不相等**（仅大小写不同已在上游判为一致）；
          4. 编辑距离 ≤ ``letter_only_difference.max_edit_distance``（默认 2）。

        ⚠️ 命中后由调用方强制判 ``SUSPICIOUS``（⚠️ 待复核），**绝不直达 ❌**
        —— 不虚高红线的直接应用。

        Args:
            left: 申报值。
            right: 图片识别值。

        Returns:
            ``True`` 表示属"只有字母之差"，应转人工复核。
        """
        min_len = max(1, int(self._noise.letter_diff_min_length or 4))
        max_dist = max(0, int(self._noise.letter_diff_max_edit_distance or 2))

        if len(left) < min_len or len(right) < min_len:
            return False
        if not (self._is_ascii_alpha(left) and self._is_ascii_alpha(right)):
            return False
        if left.upper() == right.upper():
            return False
        return edit_distance(left, right) <= max_dist

    def _is_fragment(self, value: str) -> bool:
        """判断识别值是否为残片（过短且无字母/数字主体）。"""
        text = ("" if value is None else str(value)).strip()
        if not text:
            return False
        if re.search(r"[A-Za-z0-9\u4e00-\u9fa5]", text):
            # 含有效字符：仅当长度严格小于残片阈值才算残片
            return len(text) < max(1, int(self._noise.fragment_min_chars or 2))
        return True

    def _looks_like_brand_line(self, row: str, brand_value: str = "") -> bool:
        """判断某一行是否形如"Brand 标签行"。"""
        if any(marker in row for marker in _BRAND_LABEL_MARKERS):
            return True
        # 无标签但已知品牌值且该行即该值（如独立一行的 "Daewoo"）
        if brand_value:
            return brand_value.strip() in row
        return False

    # ─────────────────────── 调试辅助 ───────────────────────

    def describe_rules(self) -> dict[str, Any]:
        """返回当前生效规则的摘要（供日志/自检）。

        Returns:
            ``{"confusable_groups": N, "fuzzy_max_edit_distance": x, ...}``。
        """
        return {
            "confusable_groups": len(self._noise.confusable_chars),
            "fuzzy_max_edit_distance": self._noise.fuzzy_max_edit_distance,
            "fuzzy_min_length": self._noise.fuzzy_min_length,
            "fuzzy_verdict": self._noise.fuzzy_verdict,
            "low_confidence_threshold": self._noise.low_confidence_threshold,
            "fragment_min_chars": self._noise.fragment_min_chars,
            "whole_machine_tokens": len(self._whole.context_tokens),
        }
