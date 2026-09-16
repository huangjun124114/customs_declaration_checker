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
    "DEFAULT_SEPARATORS",
    "DEFAULT_COLON_VARIANTS",
    "DEFAULT_DIRTY_TAIL_CHARS",
    "DEFAULT_WHOLE_MACHINE_CONTEXT",
    "DEFAULT_WHOLE_MACHINE_SCOPE",
    "DEFAULT_NON_BRAND_TOKENS",
    "DEFAULT_CONFUSABLE_CHARS",
    "DEFAULT_FUZZY_MAX_EDIT_DISTANCE",
    "DEFAULT_FUZZY_MIN_LENGTH",
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

#: 全部判定（固定顺序，供 UI 四卡与统计遍历）
ALL_VERDICTS: tuple[Verdict, ...] = (
    Verdict.PASS,
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
    "差异备注",          # 10
    "证据（OCR 片段）",  # 11
    "图片路径",          # 12
    "判定说明",          # 13
]

#: 列数常量（测试硬断言用）
COLUMN_COUNT: int = 13


# ══════════════════════════════════════════════════════════════════
#  三、字段名黑名单等默认兜底值（权威载体见 rules/*.yaml）
# ══════════════════════════════════════════════════════════════════
#  ⚠️ 同步关系：以下默认值与下列 YAML 保持一致，由 tests/test_rule_repository.py 断言
#     - DEFAULT_FIELD_BLACKLIST / DEFAULT_SKIP_PREFIXES → rules/fields_blacklist.yaml
#     - DEFAULT_NONE_TOKENS                             → rules/brand_patterns.yaml
#     - DEFAULT_SEPARATORS / DEFAULT_COLON_VARIANTS      → rules/separators.yaml
#     - DEFAULT_DIRTY_TAIL_CHARS                        → rules/model_clean_rules.yaml
#     - DEFAULT_WHOLE_MACHINE_CONTEXT                   → rules/whole_machine_brand.yaml
#     - DEFAULT_CONFUSABLE_CHARS / DEFAULT_FUZZY_*      → rules/noise_signals.yaml

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
