"""口径常量（core.constants，对应架构设计 9.1 + 12.C.4）。

⚠️ **本模块是「黑话对齐」的唯一来源**：
  四类判定字符串 / UI 卡片标签 / 字段名黑名单默认值 / 分隔符表 /
  脏尾清洗字符集 / 外箱整机品牌上下文词表 / 13 列汇总表列名。

**同步关系（v1.1 C.4）**：本文件的默认值**同时**维护在 ``rules/*.yaml``。
  * ``rules/*.yaml`` 是**权威载体**（可外置覆盖、可快速迭代）；
  * 本文件的常量是 **YAML 缺失时的兜底默认值**；
  * ``tests/test_rule_repository.py`` 会断言二者一致，防止两处漂移。

**口径红线**：四类判定字符串必须与 SOP 1.3 **逐字一致**，任何改动等同于改口径，
必须经用户确认（SOP 8.4 进化护栏#1）。
"""

from __future__ import annotations

from core.models import NoiseLevel, Verdict

__all__ = [
    "VERDICT_PASS",
    "VERDICT_FAIL",
    "VERDICT_NO_MARK",
    "VERDICT_NO_IMAGE",
    "VERDICT_TEXT",
    "VERDICT_LABELS",
    "FIELD_BRAND",
    "FIELD_MODEL",
    "JUDGED_FIELDS",
    "NON_VALUE_FIELD_SUFFIXES",
    "verdict_text",
    "verdict_label",
    "ALL_VERDICTS",
    "REVIEW_VERDICTS",
    "WORKBENCH_VERDICTS",
    "COLUMNS",
    "COLUMN_COUNT",
    "DEFAULT_FIELD_BLACKLIST",
    "DEFAULT_SKIP_PREFIXES",
    "DEFAULT_NONE_TOKENS",
    "DEFAULT_NONE_MARKERS",
    "NONE_MARKER_SUFFIX",
    "DEFAULT_SEPARATORS",
    "DEFAULT_COLON_VARIANTS",
    "DEFAULT_DIRTY_TAIL_CHARS",
    "DEFAULT_WHOLE_MACHINE_CONTEXT",
    "DEFAULT_WHOLE_MACHINE_SCOPE",
    "DEFAULT_NON_BRAND_TOKENS",
    "DEFAULT_CONFUSABLE_CHARS",
    "DEFAULT_FUZZY_MAX_EDIT_DISTANCE",
    "DEFAULT_FUZZY_MIN_LENGTH",
    "DEFAULT_LETTER_DIFF_MIN_LENGTH",
    "DEFAULT_LETTER_DIFF_MAX_EDIT_DISTANCE",
    "DEFAULT_LETTER_DIFF_VERDICT",
    "DEFAULT_LOW_CONFIDENCE_THRESHOLD",
    "DEFAULT_FRAGMENT_MIN_CHARS",
    "DEFAULT_KNOWN_NOISE_SAMPLES",
    "DEFAULT_FILE_NAME_PATTERN",
    "PART_DIR_SUFFIXES",
    "SHEET_HEADER_HINTS",
    "TICKET_NO_PATTERN",
    "TICKET_TITLE_KEYWORDS",
    "DEFAULT_KV_FIELD_ALIASES",
    "DEFAULT_KV_SEPARATORS",
    "DEFAULT_KV_WHITESPACE_SEPARATOR_MIN",
    "DEFAULT_KV_NOISE_TERMS",
    "DEFAULT_KV_ALLOW_NEXT_LINE_VALUE",
]


# ══════════════════════════════════════════════════════════════════
#  一、四类判定字符串（⚠️ 与 SOP 1.3 逐字一致，禁止改动）
# ══════════════════════════════════════════════════════════════════

#: ✅ 校验合格 —— 申报值与图片证据一致；或双方均为"无"
VERDICT_PASS: str = "✅ 校验合格"
#: ❌ 校验异常 —— 有值但不一致；或申报缺失但图片明确有
VERDICT_FAIL: str = "❌ 校验异常"
#: ⚠️ 缺图内标识，人工复核 —— 图片无品牌/型号文字；OCR 证据不足
VERDICT_NO_MARK: str = "⚠️ 缺图内标识，人工复核"
#: 🔵 缺图，人工复核 —— 共享目录找不到对应图片
VERDICT_NO_IMAGE: str = "🔵 缺图，人工复核"

#: 枚举 → 口径字符串的唯一映射（UI 卡片 / 改判按钮 / 导出列 / 日志**全部引用此表**）
VERDICT_TEXT: dict[Verdict, str] = {
    Verdict.PASS: VERDICT_PASS,
    Verdict.FAIL: VERDICT_FAIL,
    Verdict.NO_MARK: VERDICT_NO_MARK,
    Verdict.NO_IMAGE: VERDICT_NO_IMAGE,
}

#: UI 卡片短标签（与枚举一一映射，仅用于空间受限处）
VERDICT_LABELS: dict[Verdict, str] = {
    Verdict.PASS: "校验合格",
    Verdict.FAIL: "校验异常",
    Verdict.NO_MARK: "缺图内标识·复核",
    Verdict.NO_IMAGE: "缺图·复核",
}

#: 字段名（**唯一来源**）——``CheckResult.token_matches`` 的 ``TokenMatch.field``
#: 取值、差异明细的 ``field`` 取值、UI 判定链路三列的「要素」列**全部引用此表**。
#: ``core/judge_engine.py`` 的 ``_FIELD_BRAND`` / ``_FIELD_MODEL`` 只是别名，
#: 由 ``tests/test_constants.py`` 断言二者一致，防字面量漂移。
FIELD_BRAND: str = "品牌"
FIELD_MODEL: str = "型号"

#: 【口径 · 用户裁定 2026-09-16】**只有这两个要素参与识别与判定**。
#:
#: 申报要素原文常含多个要素（实测样本：``用途`` / ``结构类型`` / ``品牌`` / ``型号`` /
#: ``额定电压`` / ``长度`` …）。本工具**只对「品牌」「型号」做图片侧识别 + 一致性判定**；
#: 其余要素**既不识别、也不判定** —— 不进入任何候选、不出现在判定链路、
#: 不影响判定结论、不进 13 列汇总表。
#: ``core/element_parser.py`` 与 ``core/judge_engine.py`` 的判定范围均以本表为准。
JUDGED_FIELDS: tuple[str, ...] = (FIELD_BRAND, FIELD_MODEL)

#: **非「值」类字段后缀** —— 形如 ``品牌类型`` / ``型号类型`` / ``商标类别`` 的**其他要素**
#: 字段名：其取值是"品牌的归类"（如 ``0`` 表示无品牌），**不是品牌本身**，必须显式排除。
#: ⚠️ 缺此护栏时 ``品牌类型:0`` 会被读成品牌值 ``0`` —— 直接违反「不虚高」红线。
NON_VALUE_FIELD_SUFFIXES: tuple[str, ...] = ("类型", "种类", "类别")

#: 全部判定（固定顺序，供 UI 四卡与统计遍历）
ALL_VERDICTS: tuple[Verdict, ...] = (    Verdict.PASS,
    Verdict.FAIL,
    Verdict.NO_MARK,
    Verdict.NO_IMAGE,
)

#: 需进「待人工复核清单」的判定（⚠️ + 🔵）
#:
#: ⚠️ **语义**：这是**「谁需要被复核」的筛选口径** —— 即"哪些判定状态要进人工复核队列"。
#: 用于 :class:`core.result_exporter.ResultExporter` 导出复核清单时的过滤。
REVIEW_VERDICTS: tuple[Verdict, ...] = (Verdict.NO_MARK, Verdict.NO_IMAGE)

#: 人工复核工作台的改判选项（4 选 1，对应架构设计第 5 节链路 B）。
#:
#: ⚠️ **与 :data:`REVIEW_VERDICTS` 语义不同，切勿混用**：
#:   * :data:`REVIEW_VERDICTS` = **「谁需要被复核」**（待复核队列的筛选口径，仅 ⚠️/🔵）；
#:   * :data:`WORKBENCH_VERDICTS` = **「可以改判成什么」**（工作台改判按钮的取值域，四选一）。
#:
#: 复核者的核心价值是把 ⚠️ 改判为 ✅ 或 ❌（例如 SOP A.4 记录的「4 条噪声虚高改判合格」
#: 即 ⚠️ → ✅），因此改判取值域必须是全部四类判定，而非仅 :data:`REVIEW_VERDICTS` 两项。
WORKBENCH_VERDICTS: tuple[Verdict, ...] = (
    Verdict.PASS,
    Verdict.FAIL,
    Verdict.NO_MARK,
    Verdict.NO_IMAGE,
)


def verdict_text(verdict: Verdict | str) -> str:
    """返回判定对应的**口径字符串**（SOP 1.3 逐字）。

    Args:
        verdict: :class:`core.models.Verdict` 或其字符串值。

    Returns:
        口径字符串；未知值返回原字符串。
    """
    key = verdict if isinstance(verdict, Verdict) else Verdict(str(verdict))
    return VERDICT_TEXT.get(key, str(verdict))


def verdict_label(verdict: Verdict | str) -> str:
    """返回判定对应的 **UI 短标签**。

    Args:
        verdict: :class:`core.models.Verdict` 或其字符串值。

    Returns:
        短标签；未知值返回原字符串。
    """
    key = verdict if isinstance(verdict, Verdict) else Verdict(str(verdict))
    return VERDICT_LABELS.get(key, str(verdict))


# ══════════════════════════════════════════════════════════════════
#  二、汇总表 13 列（SOP 七，顺序固定，禁止增删）
# ══════════════════════════════════════════════════════════════════

#: 校验汇总表列名（固定 13 列，顺序即 :meth:`core.models.CheckResult.to_row` 的顺序）
#:
#: ⚠️ **第 10 列口径（v0.3.6，用户裁定 2026-09-17）**：列名由 ``判定依据`` 改为
#: **``判定依据``**，语义同步扩展 —— 该列**不再只在存在差异时才有内容**，而是对
#: **每一条记录**（✅ 校验合格 / ❌ 校验异常 / ⚠️ 缺图内标识，人工复核 / 🔵 缺图，人工复核）
#: 都写明**判定依据**：``reason``（判定依据句）＋ 差异明细（含逐字符差异）＋ 复核备注。
#: ⚠️ **列数与顺序不变**（仍恰好 13 列，见 :data:`COLUMN_COUNT`）。
COLUMNS: list[str] = [
    "出货通知书号",      # 1
    "成品料号",          # 2
    "订单号",            # 3
    "中文品名",          # 4
    "申报品牌",          # 5
    "申报型号",          # 6
    "图片识别品牌",      # 7
    "图片识别型号",      # 8
    "校验结果",          # 9
    "判定依据",          # 10 ← v0.3.6 由「差异备注」更名并扩展语义
    "证据（OCR 片段）",  # 11
    "图片路径",          # 12
    "判定说明",          # 13
]

#: 列数常量（测试硬断言用）
COLUMN_COUNT: int = 13

#: 【v0.3.7 需求 4】**人工重判标识**文案（唯一来源）。
#:
#: 用于两处**同一语义**的落点，故必须同源：
#:   * 复核工作台记录表的「复核」列；
#:   * **复核后版本**汇总表 / 复核清单的「人工复核」列。
#:
#: ⚠️ 与 13 列汇总表**无关**：受"13 列冻结"红线约束，原汇总表不得增列；
#: 本标识只出现在**复核后版本**（另存文件）与 UI 中。
MANUAL_REVIEW_MARK: str = "人工重判"


# ══════════════════════════════════════════════════════════════════
#  三、字段名黑名单等默认兜底值（权威载体见 rules/*.yaml）
# ══════════════════════════════════════════════════════════════════
#  ⚠️ 同步关系：以下默认值与下列 YAML 保持一致，由 tests/test_rule_repository.py 断言
#     - DEFAULT_FIELD_BLACKLIST / DEFAULT_SKIP_PREFIXES → rules/fields_blacklist.yaml
#     - DEFAULT_NONE_TOKENS                             → rules/brand_patterns.yaml
#     - DEFAULT_NONE_MARKERS                            → rules/brand_patterns.yaml
#     - DEFAULT_SEPARATORS / DEFAULT_COLON_VARIANTS      → rules/separators.yaml
#     - DEFAULT_DIRTY_TAIL_CHARS                        → rules/model_clean_rules.yaml
#     - DEFAULT_WHOLE_MACHINE_CONTEXT                   → rules/whole_machine_brand.yaml
#     - DEFAULT_CONFUSABLE_CHARS / DEFAULT_FUZZY_*      → rules/noise_signals.yaml
#     - DEFAULT_LETTER_DIFF_*                           → rules/noise_signals.yaml

#: 字段名黑名单（SOP 3.5 规则#1/#3）：提取到的"值"若命中则判为空
#:
#: ⚠️ 缺陷 C / 口径问题 2（架构裁决文档 4.3 / 6.2）：追加了实测被 OCR 当作品牌值的
#: 字段名残片（``SKYWORTH P/N`` 实为 ``创维物料编号`` 的英文字段名等）。
DEFAULT_FIELD_BLACKLIST: list[str] = [
    "制造商全称",
    "原产地",
    "MFR P/N",
    "MFR P/N:",
    "Supplier Code",
    "创维物料编号",
    "制造商",
    "生产厂商",
    "SKYWORTH P/N",
    "SKYWORTH P/H",
    "IFR PIN",
    "Manufacturer",
    "Manufacturer Name",
    "Supplier Name",
    "Supplier Address",
    "Manufacturer Address",
    # ⚠️ 缺陷 C / 口径 2 追加（T06 实测 seq=8/16）：P/N 行正则把字段名前缀 ``MFR``
    #    （及其 OCR 误读 ``MWFR``）捕获为"品牌"，命中 → 识别值置空 → ⚠️（非 ❌）。
    "MFR",
    "MWFR",
    "IFR",
]

#: 语境排除前缀（SOP 3.5 规则#5）：捕获词前若含这些词则跳过
DEFAULT_SKIP_PREFIXES: list[str] = [
    "适用于",
    "用于",
    "适用",
    "适配",
]

#: 视为"无品牌"的取值（SOP 3.4 补充约定 + 陷阱#7）
DEFAULT_NONE_TOKENS: list[str] = [
    "无",
    "无品牌",
    "空",
    "N/A",
    "NA",
    "NONE",
    "NULL",
    "未标记",
    "无品",
    "",
]

# ── 【口径 · 用户裁定 2026-09-17】图片侧「显式无标记」 ────────────────────
#:
#: **语义**：图片 OCR 中出现**显式的**「无品牌 / 无型号」标记时，若**申报侧**该要素
#: 也为「无」（或申报要素原文里**根本没有该要素**，解析结果为空串），则双方构成
#: "均为无" 的**一致证据** → 该要素核验**通过**（规则①「双方均为无 → ✅」的强化证据）。
#:
#: ⚠️ **优先级（用户裁定）**：显式「无」标记是**标签本体的直接证据**，效力**高于**
#: 同一票图其他位置识别到的品牌/型号文字（唛头 ``SKYWORTH P/N``、外箱
#: ``Brand:Daewoo`` 等）。后者**仍写进「判定依据」列留痕**，仅供人工追溯，不改变结论。
#:
#: ⚠️ **防虚高护栏（务必保留边界断言）**：值侧必须落在**行尾或分隔符边界**上。
#: 缺此护栏时 ``品牌:无锡机电`` 会因前缀 ``无`` 被误判为「无」标记 → 直接违反
#: 「不虚高」红线（把有品牌判成"无品牌一致"）。
#:
#: ⚠️ **权威载体**为 ``rules/brand_patterns.yaml`` 的 ``none_markers``；
#: 本常量仅作 **YAML 缺失时的兜底**，由 ``tests/test_rule_repository.py`` 断言二者一致。
#: 每条 = ``(name, field, regex, note)``，``regex`` **无捕获组**（命中即成立）。
DEFAULT_NONE_MARKERS: list[tuple[str, str, str, str]] = [
    (
        "brand_labeled",
        FIELD_BRAND,
        r"(?:品牌|Brand|BRAND)[ \t]*[:：]?[ \t]*"
        r"(?:无品牌|无品|无|空|未标记|N/?A|NONE|NULL)[ \t]*"
        r"(?=$|[\s,，。;；、|/\\])",
        "品牌字段行的取值为「无」类（品牌:无 / 品牌：无品牌 / Brand:NONE）",
    ),
    (
        "brand_bare",
        FIELD_BRAND,
        r"^[ \t]*无[ \t]*(?:品牌|品)[ \t]*(?=$|[\s,，。;；、|/\\])",
        "行首的「无品牌 / 无品」独立标记",
    ),
    (
        "model_labeled",
        FIELD_MODEL,
        r"(?:型号|规格型号|产品型号|制造商型号|Model|MODEL)[ \t]*[:：]?[ \t]*"
        r"(?:无型号|无|空|未标记|N/?A|NONE|NULL)[ \t]*"
        r"(?=$|[\s,，。;；、|/\\])",
        "型号字段行的取值为「无」类（型号:无 / 型号：无型号 / Model:NONE）",
    ),
    (
        "model_bare",
        FIELD_MODEL,
        r"^[ \t]*无[ \t]*型号[ \t]*(?=$|[\s,，。;；、|/\\])",
        "行首的「无型号」独立标记",
    ),
]

#: 显式无标记的**每条文案**（进「判定说明」/「判定依据」，便于人工复核）
NONE_MARKER_SUFFIX: str = "图片中显式标注『{marker}』（图 {image}），与申报『无』一致"

#: 分隔符表（SOP 实测 P8：半角竖线 / 顿号 / 分号，均可能夹空值）
DEFAULT_SEPARATORS: list[str] = ["|", "、", ";", "；", "/"]

#: 冒号变体（半角 + 全角）
DEFAULT_COLON_VARIANTS: list[str] = [":", "："]

#: 型号清洗的脏尾字符集（SOP 3.5 规则#7）
#: 说明：清洗策略是"从头锚定首个 ASCII 型号主体"，脏尾字符集用于**界定主体结束**，
#:       绝不能用贪婪 ``.*`` 截断（会误伤 ``2.402GHz``）。
DEFAULT_DIRTY_TAIL_CHARS: list[str] = [
    "，", ",", "/", "\\", "；", ";", "（", "(", "）", ")", "【", "】", " ", "蓝牙", "遥控器",
]

#: 外箱整机品牌上下文词表（SOP 3.5 规则#8）
#:
#: ⚠️ 缺陷 A（架构裁决文档 1.3）：**已移出** ``Customermodel`` / ``Customer model``。
#: 原因：该 token 几乎每张唛头图都有，会单独触发整机命中 → 整机命中范围错乱。
DEFAULT_WHOLE_MACHINE_CONTEXT: list[str] = [
    "Brand:",
    "Brand：",
    "品牌:",
    "品牌：",
    "CARTON",
    "JOBNO",
    "JOB NO",
    "唛头",
]

#: 整机上下文检索范围（缺陷 B 裁决，架构裁决文档 3.3）：
#: ``whole_image`` = 同图全文检索（默认，取消行距限制）；
#: 其它值 = 退回 ``context_window`` 邻域检索（旧行为，向后兼容）。
DEFAULT_WHOLE_MACHINE_SCOPE: str = "whole_image"

#: 非品牌裸 token（缺陷 C 裁决，架构裁决文档 4.3）：
#: 这些 token 在图片侧出现时**不得**被当作品牌值（单位 / 通用词 / 元件厂标 / 应用名）。
#: ⚠️ 权威载体为 ``rules/non_brand_tokens.yaml``；本常量仅作 **YAML 缺失时的兜底**。
DEFAULT_NON_BRAND_TOKENS: list[str] = [
    "CARTON",
    "JOBNO",
    "JOBNO.",
    "JOB NO",
    "QTY",
    "MADE",
    "MADE IN CHINA",
    "CHINA",
    "ORIGIN",
    "CODE",
    "DATE",
    "MODEL",
    "BRAND",
    "PCS",
    "CTN",
    "SPARE",
    "PARTS",
    "TOTAL",
    "REMARKS",
    "DESCRIPTION",
    "REACH",
    "RoHS",
    "AAA",
    "UM-4",
    "prime",
    "video",
    "YouTube",
    "NETFLIX",
    "NFK",
    "SAMYOUNG",
    "SMT",
    "DIP",
    "ONEL",
    "WLO",
    "CONGO",
    "ZCT",
    "VBL",
    "HOT",
    "COLD",
    "SPDFOUT",
]

#: 易混字符归一化映射（v1.2 第 13.5.2 节，SOP 陷阱#6）
#: 每组内字符视为等价（归一化为组内首个字符后再比对）
DEFAULT_CONFUSABLE_CHARS: list[list[str]] = [
    ["O", "0"],
    ["I", "1", "l"],
    ["W", "H"],   # ← 实测 SKYWORTH → SKYHORTH
    ["N", "H"],   # ← 实测 P/N → P/H
    ["9", "K"],
    ["8", "B"],
    ["S", "5"],
    ["G", "6"],
]

#: 模糊相似度：编辑距离 ≤ 该值且长度 ≥ min_length → SUSPICIOUS
DEFAULT_FUZZY_MAX_EDIT_DISTANCE: int = 2
#: 模糊相似度：最小长度阈值（短串太易误判）
DEFAULT_FUZZY_MIN_LENGTH: int = 5
#: 模糊相似度命中后的判定（**强制 ⚠️，绝不直达 ❌**）
DEFAULT_FUZZY_VERDICT: NoiseLevel = NoiseLevel.SUSPICIOUS

# ── 【口径 · 用户裁定 2026-09-17】英文「只差字母」→ 待复核 ─────────────────
#:
#: **语义**：申报值与图片识别值**均为纯英文字母**、长度**均 > 3 个字母**、且两者
#: 之间**只有字母之差**（编辑距离 ≤ 上限）→ OCR 极可能只是读错了字母 →
#: **列待复核（⚠️）**，并在「判定依据」列写出逐字符差异供人工判断。
#:
#: **为什么单列一条**：通用模糊相似度（:data:`DEFAULT_FUZZY_MIN_LENGTH` = 5）
#: 要求长度 ≥ 5，长度恰为 4 的纯英文串会直接落到「明确不一致（❌）」→ 假异常。
#:
#: ⚠️ **边界（防误放）**：仅对**纯英文字母**生效 —— 含数字/符号的型号不适用
#: （数字之差是实体差异，非字母误读）；编辑距离超上限的品牌差异
#: （如 ``DAEWOO`` vs ``SKYWORTH``）仍判 ❌。
#: ⚠️ **权威载体**为 ``rules/noise_signals.yaml`` 的 ``letter_only_difference``；
#: 本组常量仅作 **YAML 缺失时的兜底**。
DEFAULT_LETTER_DIFF_MIN_LENGTH: int = 4
#: 编辑距离上限（"只有字母之差"的量化阈值）
DEFAULT_LETTER_DIFF_MAX_EDIT_DISTANCE: int = 2
#: 命中后的判定（**强制 ⚠️，绝不直达 ❌**）
DEFAULT_LETTER_DIFF_VERDICT: NoiseLevel = NoiseLevel.SUSPICIOUS

#: OCR 低置信度阈值（**兜底**；权威载体 ``noise_signals.yaml::low_confidence_threshold``）。
#: ⚠️ 兜底分支必须**显式传值**：漏传会静默回落到 dataclass 默认值，一旦两处取值
#: 不同即构成"兜底 ≠ repo"（缺陷 F 同类隐患）。
DEFAULT_LOW_CONFIDENCE_THRESHOLD: float = 0.5

#: 残片最小字符数（**兜底**；权威载体 ``noise_signals.yaml::fragment_min_chars``）。
DEFAULT_FRAGMENT_MIN_CHARS: int = 2

#: 已知 OCR 误读样本表（**兜底**；权威载体为 ``rules/noise_signals.yaml``）。
#:
#: ⚠️ **缺陷 F 教训（本组常量即为修复而补）**：``NoiseGuard._resolve_noise_rules()``
#: 的兜底分支必须与 repo 路径**规则集等价**，否则 YAML 缺失（如打包漏拷）时
#: `known_noise_samples` 静默为空 → 「已知误读自动纠正」整条链路静默失效，
#: 表现为"源码态全绿、打包后判定全变"。
#: 每条字段语义：``ocr`` 整行形态 / ``ocr_prefix`` 运行期前缀形态 / ``expected_actual``
#: 纠正后确定值 / ``note`` 说明。
DEFAULT_KNOWN_NOISE_SAMPLES: list[dict[str, str]] = [
    {
        "ocr": "SKYHORTH P/H",
        "ocr_prefix": "SKYHORTH",
        "expected_actual": "SKYWORTH P/N",
        "note": "W→H、N→H 双字符误读（v1.2 实测复现）",
    },
    {
        "ocr": "IFR PIN",
        "ocr_prefix": "IFR",
        "expected_actual": "SKYWORTH P/N",
        "note": "字段名整体误读（SOP 附录 A.4）",
    },
    {
        "ocr": "boori E339609",
        "ocr_prefix": "boori",
        "expected_actual": "baori",
        "note": "等价证据：OCR 噪声 + 型号后缀（SOP 附录 A.4）",
    },
    {
        "ocr": "600-CX9",
        "ocr_prefix": "600-CX9",
        "expected_actual": "GXD-009",
        "note": "倒置插头，OCR 读反（SOP 附录 A.3）",
    },
]


# ══════════════════════════════════════════════════════════════════
#  四、文件名 / 目录 / 票号解析相关默认值
# ══════════════════════════════════════════════════════════════════

#: 图片文件名模式（SOP 3.2 + 实测 6.4）：``{A}&{B}&{C}.jpg``
#: A=order_no（也=目录名） B=part_no C=序号（会跳号）
DEFAULT_FILE_NAME_PATTERN: str = r"^(?P<a>[^&]+)&(?P<b>[^&]+)&(?P<c>\d+)$"

#: 料号目录需剥离的后缀（实测 P5：``2660308M图片``）
PART_DIR_SUFFIXES: list[str] = ["图片", "-0-", "_0_", "图", "(1)", "（1）"]

#: 要素表识别：表头命中打分关键词（SOP Phase 1 / 架构 P1）
SHEET_HEADER_HINTS: dict[str, list[str]] = {
    "element_col": ["申报要素"],
    "part_col": ["物料编号", "成品料号", "料号", "产品料号"],
    "order_col": ["订单号", "订单编号"],
    "name_col": ["名称", "中文品名", "品名"],
    "seq_col": ["序号", "No.", "项次"],
    "qty_col": ["数量"],
    "weight_col": ["重量"],
}

#: 票号提取正则（容忍"出货通知书号 / 出货通知号"、有/无冒号、缺"书"字）
TICKET_NO_PATTERN: str = r"出货通知[书]?号\s*[:：]?\s*(?P<ticket>[A-Za-z]{0,4}\d{6,14}[A-Za-z0-9\-]*)"

#: 标题行关键词（用于区分「标题行」与「表头列名行」，SOP Phase 1）
TICKET_TITLE_KEYWORDS: list[str] = ["出货通知书号", "出货通知号"]


# ══════════════════════════════════════════════════════════════════
#  五、OCR 键值对（KV）提取默认值（v0.2.0 点 8）
# ══════════════════════════════════════════════════════════════════
#  ⚠️ 权威载体为 ``rules/ocr_kv_patterns.yaml``；本组常量仅作 **YAML 缺失时的兜底**。
#     由 tests/test_rule_repository.py 与 tests/test_kv_extractor.py 断言二者**等价**
#     （防"打包漏 YAML → 静默失效"，即历史缺陷 F 的教训）。
#
#  ⚠️ KV **不参与判定**（Q4 已决）：判定仍走原跨图投票链路。

#: KV 字段别名表（规范字段名 → 该字段在唛头/标签上可能出现的别名形态）。
#: 顺序与 ``rules/ocr_kv_patterns.yaml`` 的 ``field_aliases`` **逐项一致**。
DEFAULT_KV_FIELD_ALIASES: list[tuple[str, list[str]]] = [
    ("Brand", ["Brand", "BRAND", "品牌", "牌"]),
    ("Model", ["Model", "MODEL", "型号", "规格型号", "产品型号", "制造商型号"]),
    ("MFR P/N", ["MFR P/N", "MFR PIN", "MFR/PN", "制造商料号", "制造商 P/N"]),
    (
        "P/N",
        [
            "P/N",
            "P/H",
            "PIN",
            "料号",
            "物料编号",
            "成品料号",
            "创维物料编号",
            "SKYWORTH P/N",
            "SKYWORTH P/H",
        ],
    ),
    ("Customer model", ["Customer model", "Customermodel", "客户型号"]),
    ("Quantity", ["QTY", "数量", "订购数量"]),
    ("Weight", ["重量", "毛重", "净重"]),
    ("Origin", ["Origin", "原产地", "产地"]),
    (
        "Manufacturer",
        ["Manufacturer", "Manufacturer Name", "制造商", "制造商全称", "生产厂商"],
    ),
    ("Supplier", ["Supplier", "Supplier Code", "Supplier Name", "供应商"]),
    ("Description", ["Description", "DESCRIPTION", "品名", "名称", "中文品名"]),
    ("Date", ["Date", "DATE", "日期", "生产日期"]),
    ("Serial", ["Serial", "Serial No.", "Serial No", "序列号"]),
]

#: KV 键值分隔符（半角冒号 / 全角冒号 / 等号）
DEFAULT_KV_SEPARATORS: list[str] = [":", "：", "="]

#: 连续空白视为弱分隔符的最小空格数（键须命中别名白名单才采信）
DEFAULT_KV_WHITESPACE_SEPARATOR_MIN: int = 2

#: KV 级噪声词表（UI 图标文字 / 应用名；整行命中则判为噪声，不进 KV）
DEFAULT_KV_NOISE_TERMS: list[str] = [
    "prime video",
    "YouTube",
    "NETFLIX",
    "Spotify",
    "Prime Video",
]

#: 是否支持「值在下一行」（弱分隔，默认关闭，避免误吞相邻行）。
DEFAULT_KV_ALLOW_NEXT_LINE_VALUE: bool = False
