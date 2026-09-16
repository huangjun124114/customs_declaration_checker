# 架构裁决：真 OCR 跑批误报缺陷（SA26090215）

> ⚠️ **v0.3.0 更新提示（2026-09-16）**：本裁决文档的**实体裁决全部继续有效**
> （缺陷 A/B/C/D/E/F、裁决 1「整机品牌 → ❌」、裁决 3「工作台四类全开」）。
>
> **仅一处口径被后续迭代取代**：本文件 §4.6 **裁决 2**（「OCR 已知误读 → 自动纠正后
> 比对」，即 `SKYHORTH` → `SKYWORTH P/N` ≠ 申报 `SKYWORTH` → ❌）建立在 v0.2.0
> 「先提取、后比对」主链路上。v0.3.0 主链路改为「**申报值在 OCR 全文里以完整分词
> 命中即合格**」（`docs/10_迭代方案_v0.3_0916.md` §3），取证方式变更后：
>
> * **仍保留**"误读纠正"能力（`known_noise_samples` + 易混字符等价类，见
>   `core/token_matcher.py`），命中时标注「OCR 疑似误读，已纠正」（可追溯）；
> * **新增同源护栏**：纠正目标本身若是**字段名**（`SKYWORTH P/N` = 创维物料编号），
>   则该样本不作命中依据 —— 与"字段名残片不得作本体证据"（缺陷 C / 口径 2）同源；
> * 新旧分布：✅1 / ❌12 / ⚠️5 / 🔵0 → **✅7 / ❌7 / ⚠️4 / 🔵0**（变更 seq 1、4、5、16、17、18）；
>   seq=16 由 ❌ 变 ✅ 的直接原因是：申报值 `SKYWORTH` 在图中以完整分词出现，
>   而不再依赖"猜出的图片侧字段值"。逐条对比见 §3.5 / `CHANGELOG.md`。

> **裁决人**：高见远（架构师 software-architect-2）
> **裁决日期**：2026-09-16（**修订 R4b**：更正缺陷 F 根因为「兜底分支漏传字段」；**修订 R4**：老板口径拍板后重写第 8 节为"方法论+依据表"、新增裁决 2 与缺陷 E/F 规格；**R3**：第 8 节硬断言重写；**R2**：A′ 撤销重裁）
> **裁决依据**：SOP v2.0 原文（`成果产出\报关申报要素自动校验SOP_0916.md`）+ 一手实测证据（`过程产出\校验详细日志_SA26090215_累积.json`）+ 源码复核（`core/judge_engine.py` / `core/noise_guard.py` / `rules/*.yaml` / `core/constants.py`）
> **裁决对象**：T04 判定引擎在真实样本 SA26090215 上产生的 11 条 ❌
> **受益方**：T06 工程师（本裁决为其实施规格）
> **架构师声明**：本裁决**不修改任何 `core/` / `rules/` 代码**，仅出具裁决与规格；实现由 T06 执行。

> ⚠️ **修订记录 R2（重要）**：初版裁决 A′ 曾主张「整机品牌 → 改判 ⚠️」。**经主理人援引 SOP 第 147 行「字段缺失 → ❌」与附录 A.4 实测（5 异常全为整机品牌类）驳回后，A′ 已撤销**。现裁决为「**整机品牌 → 保留 ❌**」。详见第 2 节（已重写）与第 10 节（含 4 条 SOP 铁证原文引用）。
>
> ⚠️ **修订记录 R3（重要）**：R2 的第 8 节硬断言经主理人**逐图实跑 `NoiseGuard.is_whole_machine_brand`** 核对后发现 **3 处规格错误**（① 整机 FAIL 层 seq 列表含 4 个错项 3/6/10/15；② seq=2 归因错列整机路径；③ 引用不存在的函数 `_is_whole_machine_for_seq`）。**架构师复核 JSON 证据后全部确认主理人正确**，第 8 节重写为分层断言。详见第 8.0 节。
>
> ⚠️ **修订记录 R4（重要，老板口径拍板）**：① **裁决 1**：整机品牌 → **❌**（沿 SOP 附录 A.4 历史口径，**确认 R2 撤销正确**）；由此 **seq=15 与 seq=12 同构 → 同判 ❌**（**R3 的"第 4 层 seq=15 不得 FAIL"作废**）；② **裁决 2（新增规则）**：**OCR 已知误读 → 自动纠正为正确值后比对**（`known_noise_samples[].expected_actual` 首次启用）→ seq=16 由 ⚠️ **改判 ❌**，原 R3「(d) 独立判噪」作废；③ **裁决 3**：工作台改判四类全开（维持现状）；④ **新增缺陷 E**（`_apply_blacklist` 误伤型号，SOP 陷阱 #5 语义纠偏）与 **缺陷 F**（`known_noise_samples` 粒度不匹配）；⑤ **§4.2 正式化** `min_images` 分层（① ② ③ =1，④ =2）；⑥ **第 8 节撤销一切"推荐区间"，改为"方法论 + 逐条依据表"**。详见 §4.6 / §5A / §5B / §8。**缺陷 A/B/C/D 与 A′ 的实体裁决不变。**
>
> ⚠️ **修订记录 R4b（重要，缺陷 F 根因更正）**：**缺陷 F 的 R4 根因诊断"粒度不匹配"经主理人深入实测推翻**。主理人对照实验证明：注入 repo 时整行与前缀**都能命中**（`_lookup_known_sample` 五重兜底已实现），而**无参构造时 `known_noise_samples` 为空** —— **真实根因 = `_resolve_noise_rules()` 兜底分支漏传 3 字段**（`known_noise_samples` / `low_confidence_threshold` / `fragment_min_chars`）。更正要点：① **`ocr_prefix` 已实现，无需新增**（原 R4 指令作废）；② 裁定改为「**兜底分支必须与 repo 路径规则集等价**」（防打包漏 YAML 静默失效）；③ 回归锁改为 `test_noise_guard_default_construction_has_samples` + `test_noise_guard_default_and_repo_agree`；④ 诊断演进链（**三次诊断、两次纠正**）作为方法论案例记入 §8.0 ⑤ / §5B.0。**缺陷 F 的实体修复仍归 T06（T06-21 已更正）。**

---

## 0. 总裁决摘要（一表读全）

| 议题 | 裁决 | 性质 | 是否改口径 |
|---|---|---|---|
| **缺陷 A** `Customer model` 误列整机上下文 | **移出** `context_tokens`（YAML + constants 同步） | 实现层 | 否 |
| **缺陷 A′**（**已撤销，保留 ❌**） | **整机品牌命中 → 保留 ❌**（依 SOP 3.4 第 147 行「字段缺失 → ❌」+ 附录 A.4 实测 5 异常全为该类）。源码 `judge_engine.py:713-724` 分支**正确，保留不动** | 实现层 | 否 |
| **缺陷 B** `context_window: 3` | **改为"整图全文检索"**（新增 `context_scope: whole_image`） | 实现层 | 否 |
| **缺陷 C** 品牌提取无跨图一致性 | **改为跨图一致性投票** + 标签图优先级（`&001` 加权） | 实现层 | 否 |
| **缺陷 D** `无品` 不在 `none_tokens` | **加入** `none_tokens`（YAML + constants 同步） | 实现层 | 否 |
| **口径问题 1** `Customer model` vs 制造商型号定位 | **型号比对基准取"制造商型号"（本体）优先；`Customer model` 仅作整机上下文，不作本体型号证据** | 实现层（对齐 SOP 3.3/3.5#8） | 否 |
| **口径问题 2** 字段名残片算不算「图片明确有」 | **不算**（`SKYWORTH P/N` 等字段名残片 = 无本体标识 → ⚠️，非 ❌） | 实现层（对齐 SOP 3.5#1/#3 + 1.3） | 否 |
| **口径问题 3** 本体品牌图片来源范围 | **不限定单张图**（SOP 3.3 明确"必须全量 OCR"），但**本体证据需全量交叉投票**，且**整机上下文图（唛头）单独剔除** | 实现层（对齐 SOP 3.3） | 否 |

**一句话结论**：11 条 ❌ 中，**由整机品牌触发的部分为符合口径的正确判定（保留 ❌，SOP 第 147 行 + 附录 A.4）**；真误报来自**缺陷 A/B/C/D**（整机命中范围错乱 + 窗口不可复现 + 品牌取到 `ONEL`/`prime`/`AAA` 等垃圾值 + `无品` 未识别）。**修复 A/B/C/D 后，整机命中回到正确范围，❌ 自然下降**。预期分布详见第 8 节（**已重算**）。

---

## 1. 缺陷 A 裁决：`Customer model` 移出整机上下文

### 1.1 事实认定（一手证据）

`rules/whole_machine_brand.yaml` 的 `context_tokens` 当前含：
```yaml
- Customermodel
- Customer model     # ← 误列
```
`core/constants.py:220` 的 `DEFAULT_WHOLE_MACHINE_CONTEXT` 同款误列。

**证据（SA26090215 实测）**：唛头图 `&003` / `&004` 等几乎**每张都含** `Brand:Daewoo` + `Customer model:50DA25QL`。

### 1.2 裁决：**移出**。理由（SOP 原文支撑）

**SOP 3.5 规则 #8 原文**：
> **外箱整机品牌混淆** | 唛头上的 `Brand:Daewoo` 伴随 `Customermodel`/`CARTON`/`JOBNO` 上下文时，是**整机适配品牌**，不构成零部件本体品牌证据 |

**逐字解读**：SOP 确实**点名了** `Customermodel` 作为整机上下文。因此"移出"**不是推翻 SOP**，而是**修正实现的判定条件**——SOP 说的是"**伴随** `Customermodel`/`CARTON`/`JOBNO` 上下文"（三选一的**组**），而实现在于**触发时机**：

- SOP 的整机语义是「**唛头（外箱）**」这一**图片类型**上的 Brand 行才算整机品牌。
- `Customer model` 之所以在 SOP 里被列出，是因为它**只出现在唛头**；但它**不能作为"这张图是唛头"的充分证据**——因为它同样出现在**非唛头**的标签图上（或与本体品牌共现）。
- 实测反证：`seq=11/12/15` 的 `&001` 图（**主板 PCB 丝印图**）里出现 `Brand:Daewoo` 时，`Customer model` 也在同图 → 被误判"整机品牌"，进而把本体型号图（`MODEL:A9KB9G` 明确可见）的正向证据**反推成 ❌**。

**结论**：`Customer model` / `Customermodel` 作为**整机上下文的"追加确认项"**保留是正确的；但现状是它**单独**就能触发命中。裁决：**从 `context_tokens` 移出**，改由更强的整机信号（`CARTON No.` / `JOB NO.` + `Brand:` 组合）承担命中判定。

> **口径红线说明**：本裁决**不改变** SOP 3.5#8 的语义（唛头 + 整机上下文 → 非本体证据）。它只修正"什么构成整机上下文命中"的**实现条件**，属实现层。移出后，`CARTON No.` / `JOB NO.` 仍在词表内，唛头图仍能被正确识别（见 1.4 回归锚点）。

### 1.3 同步修改清单（防两处漂移）

| 文件 | 位置 | 改动 |
|---|---|---|
| `rules/whole_machine_brand.yaml` | `context_tokens` | **删除** `- Customermodel` 与 `- Customer model` 两行 |
| `core/constants.py` | `DEFAULT_WHOLE_MACHINE_CONTEXT`（L220–231） | **删除** `"Customermodel"` 与 `"Customer model"` 两项 |
| `tests/test_rule_repository.py` | `TestConsistencyWithConstants::test_whole_machine_context_matches`（L253–257） | **无需改**（断言是"YAML == constants"，两边同删即仍相等）；但 `TestLoadAll::test_whole_machine_loaded`（L61–64）**断言的是 `"Brand:" in tokens` + `window == 3`**——删 `Customer model` 不影响该断言。⚠️ 但 1.4/缺陷 B 会改 `context_window` 语义，**该断言 `window == 3` 需同步改**（见第 2 节）。 |

### 1.4 回归锚点（T06 必须补的测试）

```python
# tests/test_whole_machine_brand.py（新增）
def test_customer_model_not_trigger(ng_guard):
    """Customer model 单独出现（无 CARTON/JOB NO）→ 不得判整机品牌。"""
    text = "Brand:Daewoo\nCustomer model:50DA25QL"
    assert ng_guard.is_whole_machine_brand(text, brand_value="Daewoo") is False

def test_carton_jobno_still_trigger(ng_guard):
    """CARTON No. / JOB NO. + Brand: → 仍正确判整机品牌（不误伤）。"""
    text = ("Brand:Daewoo\nCustomer:M&M\nJOB NO.:2660326M\n"
            "CARTON No.:A2\nCustomer model:50DA25QL")
    assert ng_guard.is_whole_machine_brand(text, brand_value="Daewoo") is True
```

---

## 2. 缺陷 A′ 裁决（**修订 R2**）：整机品牌命中 **保留 ❌**

> **本节经历一次撤销重裁。** 初版裁决主张「整机品牌 → 改判 ⚠️」，**被主理人援引 SOP 原文驳回**。经复核 SOP v2.0 原文（逐行核对如下），**架构师接受驳回，A′ 撤销**。

### 2.1 事实认定

`core/judge_engine.py:713–724` 的 `_apply_degrade_guard` 尾段：
```python
if result.verdict == Verdict.PASS and result.differences:
    has_preserved_fail = any(d.note and "整机品牌" in d.note for d in result.differences)
    if has_preserved_fail:
        result.verdict = Verdict.FAIL     # ← 把整机品牌差异「保留为 ❌」
```

### 2.2 裁决（R2）：**保留 ❌**。源码该分支**正确，不动**。

**SOP 铁证 1 — SOP 3.4「5 条比对 → 结论映射」原文（第 147 行，逐字）**：
```
| 3 | 文字有差异、字段缺失、图片无对应字段 | ❌ 校验异常（标注差异明细） |
```
**「字段缺失」明确归 ❌**。外箱整机品牌的本质是**本体品牌字段缺失**（SOP 3.5#8 原话「不构成零部件**本体品牌证据**」——即本体侧无证据、字段缺失）→ **依第 147 行落 ❌**。

**SOP 铁证 2 — SOP 第 32 行「关键区分」原文（逐字）**：
```
> 校验异常 = 有证据证明不一致；缺图内标识 = 没找到证据。两者不可混为一谈。
```
整机品牌场景**有证据**——唛头 `Brand:Daewoo` 与申报值/semantic 不一致，是实打实的**可见证据**，**不是**「没找到证据」→ 属 ❌ 不属 ⚠️。

**SOP 铁证 3 — SOP 附录 A.4 原文（第 381 行，上一版真实执行统计，逐字）**：
```
- 结果：13 合格 / 5 异常（全为外箱整机品牌类）
```
**同一样本、同一口径的历史事实：外箱整机品牌类就是归入「异常」（❌）的，且构成 5 条异常的全部。**

**SOP 铁证 4 — Phase 4「处置」列是执行动作指引，不是判定标**：该列同时写「OCR 噪声虚高 → **改判合格**」「等价证据 → **改判合格**」——明显是"人接下来做什么"。「保留异常，进人工复核」= **保留 ❌ 状态** + 转人工确认。初版把它当判定标用是**推论错误**。

> **架构师撤销声明**：初版 A′ 的推理链（"Phase 4 处置列 = 判定标 → 进人工复核 = ⚠️"）**不成立**，已作废。**`judge_engine.py:713-724` 保留 ❌ 是正确的实现**，符合 SOP 3.4 第 147 行 + 附录 A.4。

### 2.3 与「不虚高」红线的关系（澄清）

初版曾援引 SOP 1.2「不虚高」主张降级，**该援引亦不成立**：
- SOP 1.2「不虚高」原文限定于「**OCR 噪声造成的"假异常"**」；
- 整机品牌是**图片上真实可见的文字**（非 OCR 噪声），且 SOP 3.5#8 已将其定义为"整机适配品牌"——它是**真实存在但语义层级不符**的证据，SOP 用 ❌ 标记它、并以 Phase 4 动作（转人工）承接。
- 故对本类判 ❌ **不违反**「不虚高」；**真正违反**「不虚高」的是缺陷 A/B/C/D 造成的**垃圾值比对异常**（`ONEL`/`prime`/`AAA` 与申报值比对）。

### 2.4 需改代码位置（**R2 修订：本缺陷不改代码**）

| 文件 | 位置 | 改动（R2） |
|---|---|---|
| `core/judge_engine.py` | `_apply_degrade_guard`（L698–724） | **保留不动**（原指令"删除该分支"**已撤销**） |
| `core/judge_engine.py` | `_field_state`（L643–672） | **保留不动**（整机 + 申报缺失 → `_FIELD_DECLARED_MISSING` → ❌，**符合 SOP 第 147 行**） |
| `core/judge_engine.py` | `_decide`（L612–618） | **保留不动** |

> ⚠️ **初版 T06-15 / T06-16 两条指令已作废**（原：`_field_state` 整机→SUSPICIOUS、删 `_apply_degrade_guard` 推涨分支）。**新指令**见第 7 节 T06-15R / T06-16R。

### 2.5 回归锚点（**R2 重写**）

```python
# tests/test_judge_engine.py（新增/修订）
def test_whole_machine_brand_preserves_fail(engine):
    """整机品牌命中 + 申报无品牌 → ❌（依 SOP 3.4 第 147 行「字段缺失→❌」）。"""
    ev = _mk_evidence("SPARE PARTS\nJOB NO.:2660326M\nBrand:Daewoo\n"
                      "CARTON No.:A11\nCustomer model:50DA25QL")
    rec = _mk_record(decl_brand="", decl_model="A9KB9G", evidences=[ev])
    result = engine.judge(rec)
    assert result.verdict == Verdict.FAIL          # ← R2：保留 ❌
    assert any("整机品牌" in d.note for d in result.differences)

def test_whole_machine_branch_not_falsely_triggered(engine):
    """缺陷 A 修复后：非整机图不得误命中断机分支（防范围错乱）。"""
    # 仅 Brand: 无 CARTON/JOB NO → whole_machine=False → 不得因整机分支判 ❌
    ev = _mk_evidence("Brand:Daewoo\nCustomer model:50DA25QL")
    rec = _mk_record(decl_brand="Daewoo", decl_model="", evidences=[ev])
    assert engine.judge(rec).verdict != Verdict.FAIL
```

---

## 3. 缺陷 B 裁决：`context_window` 改为"整图全文检索"

### 3.1 事实认定

`rules/whole_machine_brand.yaml`: `context_window: 3`；`NoiseGuard.is_whole_machine_brand`（L447–457）按 `idx ± window` 取邻域行。

**实测矛盾（同一逻辑、同构成图，判定相反）**：
- `seq=17 img3` / `seq=18 img4`（含 `Brand:Daewoo` + `JOB NO.` + `CARTON No.` + `Customer model`）→ `WM=False`（漏判）
- `seq=11 img9/10/11` / `seq=12 img8`（同构）→ `WM=True`（命中）

原因：OCR 按**视觉顺序**切行，`Brand:` 与 `CARTON No.` 垂直相距 **7–8 行**（见 JSON 中 `Brand:Daewoo` 与 `CARTON No.:A2` 之间夹着 `Customer` / `DIMENSION` / `Part NO` / `NOM…` 等），固定 3 行窗口**必然漏判**。

### 3.2 裁决：**改为整图全文检索**（不做行距限制）

**理由（SOP 原文支撑）**：

**SOP 3.5 规则 #2 原文**：
> **值在字段名上方** | 标签表格 OCR 行序可能错位，需在字段名**前后 3 行**窗口内搜索有效值，而非只看下一行

**关键区分**：SOP 规则 #2 的"前后 3 行"针对的是「**同一字段的键与值**」（如 `品牌:` 与 `Daewoo` 的垂直邻近），**属"取字段值"场景**；而**整机上下文判定**是「**字段间共现**」（`Brand:` 行 与 `CARTON No.` 行是否在**同一张图**上）——这是 SOP 规则 #8 的范畴，**规则 #8 原文没有"3 行"约束**，只说"唛头上的 `Brand:Daewoo` **伴随** `Customermodel`/`CARTON`/`JOBNO` **上下文**"。**伴随 = 同一张唛头上共现**，不是"相邻 3 行"。

**结论**：把规则 #2 的"前后 3 行"**误套到**规则 #8 的整机上下文判定上，是本次实现的**跨规则误用**。裁决：整机上下文判定采用**整图（同一 OcrText.text_raw 全部行）全文共现**，取消行距限制。

> ⚠️ **必须同时防的新风险（T06 必做）**：取消行距后，"整机上下文命中"会变**宽松**。若不同时把 `Customer model` 移出（缺陷 A），会把所有含 `Brand:` 的图都误判整机。**因此缺陷 A 与缺陷 B 必须同批次修**。此外，取消行距后命中判定应**要求 `Brand:` 与整机 token 在同一 OcrText 内**（而非跨图拼接全文），避免把不同图的行拼到一起误判。

### 3.3 实现规格（三选一，裁决取"语义化"方案）

| 方案 | 说明 | 裁决 |
|---|---|---|
| ① `context_window: 0` | 0 表示"不限制"（需在代码里区分 0 与"未设"） | 可用，但语义隐晦 |
| ② `context_window: -1` | -1 表示"全文" | 可读性差 |
| ③ **新增 `context_scope: whole_image`** | 显式布尔/枚举，最清晰 | ✅ **采用** |

**裁决**：新增 `context_scope: whole_image`（默认 `whole_image`），保留 `context_window` 字段但**仅作向后兼容**（`context_scope != whole_image` 时生效）。

| 文件 | 位置 | 改动 |
|---|---|---|
| `rules/whole_machine_brand.yaml` | 新增键 | `context_scope: whole_image` |
| `core/constants.py` | 新增 | `DEFAULT_WHOLE_MACHINE_SCOPE: str = "whole_image"` |
| `core/rule_repository.py` | `WholeMachineBrandRules` | 新增字段 `context_scope: str = "whole_image"` |
| `core/noise_guard.py` | `is_whole_machine_brand`（L447–457） | `context_scope == "whole_image"` 时，`neighborhood = rows`（整图全文）；否则维持 ±window |
| `tests/test_rule_repository.py` | `test_whole_machine_loaded`（L61–64） | 断言改为 `context_scope == "whole_image"`（`window == 3` 断言**移除以避免两处语义冲突**，或改为断言字段存在） |

### 3.4 回归锚点

```python
def test_whole_machine_whole_image_scope(ng_guard):
    """Brand: 与 CARTON No. 相距 >3 行 → 仍应命中（整图全文）。"""
    text = "\n".join(["Brand:Daewoo", "X0","X1","X2","X3","X4","X5","X6","X7",
                      "CARTON No.:A2"])
    assert ng_guard.is_whole_machine_brand(text, brand_value="Daewoo") is True
```

---

## 4. 缺陷 C 裁决：品牌提取改为**跨图一致性投票** + 标签图优先级

### 4.1 事实认定

`judge_engine.py:139 extract_detected_brand` 的「④ 独立裸 token 行」分支**逐图逐行扫描、首个命中即返回**。实测（`seq=11`）：

```
img1='ONEL'  img2='WLO'  img3='CONGO'  img4='ZCT'  img5='VBL'
img6='HOT'   img7='SPDFOUT'  img8='1anufacturer'  img9/10/11='Daewoo'
```
真实品牌 `Daewoo` 到**第 9 张图**才出现，但引擎取第 1 张的 `ONEL`。`seq=17` 取 `SKYWORTH`（实为 `创维物料编号 SKYWORTH P/N` 字段名）、`prime`（`prime video` 应用图标）、`AAA`（`AAA UM-4` 电池型号）。

同类误捕源（实测 token）：`Manufacturer` / `Manufacturer Name`（字段名）、`SMT` / `NFK` / `SAMYOUNG`（PCB 元件厂标）、`CONGO` / `ONEL`（丝印残片）。

### 4.2 裁决：**采用跨图一致性投票 + 标签图优先级**

**SOP 原文支撑**：

**SOP 3.3 原文（图片类型与信息分布规律）**：
> | 图片序号 | 通常内容 | 品牌/型号信息丰富度 |
> | `&001` | 标签图 / 出货单 / 外箱唛头 | ⭐⭐⭐ 信息最全 |
> | `&002` 起 | 产品实物照、线缆印字、PCB 丝印 | ⭐ 需看实物印刷 |
>
> **结论**：**必须全量 OCR**，不能只取第 1 张。

**SOP 1.2「不虚高」原文**：宁可判"待人工复核"，不可把 OCR 噪声造成的"假异常"当真异常。

**SOP 附录 B 待沉淀项 #3**：
> OCR 字段噪声的自动识别特征（如**同一票内同位置多次误读可交叉验证**）

→ 附录 B 已经把"**跨图交叉验证**"列为**待沉淀的正式方向**。本裁决即将其**落地**，**这正是 SOP 期望的进化方向**（非新增口径）。

**裁决细则**：

1. **候选收集**：遍历**所有图**的所有行，按现有 ① 显式标签 / ② P/N 行 / ③ 品牌+型号行 / ④ 裸 token 四层分别收集候选（**收集全部，不再首个即返回**）。
2. **分层优先级**：`① 显式标签 > ② P/N 行 > ③ 品牌+型号行 > ④ 裸 token`。**高优先级层一旦有候选，低优先级层不参与投票**（如显式 `Brand:Daewoo` 存在时，不再让 `ONEL` 等裸 token 干扰）。
3. **跨图一致性投票（层内，**R4 修订：`min_images` 分层**）**：
   - 归一化后（upper + 去分隔符）**出现次数最多**者为胜；
   - **"≥2 张不同图一致"的约束仅适用于 ④ 裸 token 层**（`min_images=2`）——这是为压制 `ONEL`/`prime`/`AAA` 类**单图孤例噪声**而设；
   - **① 显式标签 / ② P/N 行 / ③ 品牌+型号行 三层 `min_images=1`**（单图即可采信）——理由：这三层是**有结构锚点的证据**（`Brand:`/`品牌:` 标签、P/N 行、品牌+型号同行），单张标签图上的显式品牌即足以采信，若也要求 ≥2 图会把"只有一张标签图"的正当场景误杀为无品牌；
   - **层内**：胜出候选须满足该层 `min_images`；不满足 → 该层无候选 → 落到下一层；
   - **全层落空** → **品牌识别值置空** → 上层判 ⚠️（不虚高）。

   > **R4 正式化说明**：原文写"必须满足 ≥2 张图"**未分层**，T06 工程师实际实现为**按层设置 `min_images`**（① ② ③ = 1，④ = 2），并已验证正确。本 R4 **将该分层表述正式写入规格，使 T06 实现合法化**，消除"文档与实现不一致"。
4. **标签图优先级（加权）**：`seq == 1`（即 `&001`）的候选**权重 ×2**（SOP 3.3 明确 `&001` "信息最全"）。投票时以加权票数决胜；加权后仍并列则取 `&001` 中的候选。
5. **落空处置**：投票无胜出 → `detected_brand = ""` → `_field_state` 走 `_FIELD_DETECTED_MISSING` 或 `both_absent` → ⚠️/✅，**绝不因单图噪声判 ❌**。

> **口径红线说明**：这不新增口径——SOP 1.3 的 ⚠️（"图片无任何品牌文字 / OCR 证据不足"）本就应该覆盖"候选互相矛盾、无一致证据"的情形。本裁决把"证据不足"的**判定条件**落成可执行条目（附录 B#3 的正式化），属实现层。

### 4.3 排除规则落在哪个文件（**裁决**）

**裁决：分层落位 + 非品牌 token 外置（**R2 修订**：撤回原"可暂留硬编码"降级）。**

| token 类型 | 示例 | 落位 | 理由 |
|---|---|---|---|
| **字段名类**（OCR 字段名被误当品牌值） | `SKYWORTH P/N`、`Manufacturer`、`Manufacturer Name`、`创维物料编号`、`Supplier Code` | **`rules/fields_blacklist.yaml::terms`** | 正是 SOP 3.5#1/#3 的定义域（"字段名被当成值"），已有载体，**追加即可** |
| **非品牌裸 token**（单位/通用词/元件厂标/应用名） | `AAA`、`prime`、`video`、`YouTube`、`NETFLIX`、`NFK`、`SAMYOUNG`、`SMT`、`RoHS`、`REACH`、`MADE`、`CHINA` | **新建 `rules/non_brand_tokens.yaml`**（**必须外置，R2 裁决**） | 属"图片侧提取护栏"；"规则解耦"是用户第 2 点**硬约束**，且这是缺陷 C 的**直接对策**。`_NON_BRAND_BARE_TOKENS` 仅作 YAML 缺失时的**常量兜底**（非权威载体） |
| **整机上下文类** | `CARTON`、`JOB NO` | `rules/whole_machine_brand.yaml::context_tokens`（现状） | 已归位 |

> ⚠️ **架构硬约束提醒**：`core/` 禁止 import PySide6（不改）；"能进 YAML 的判定条目一律外置"。故 `AAA`/`prime` 类**必须外置**。裁决：**T06 新建 `rules/non_brand_tokens.yaml`**，并在 `rule_repository.py` 增 `NonBrandTokenRules`。
> **R2 变更**：原文的"若 T06 时间紧可暂留硬编码"**已作废**（主理人驳回）。**外置为必做项**。

### 4.4 需改代码位置

| 文件 | 位置 | 改动 |
|---|---|---|
| `core/judge_engine.py` | `extract_detected_brand`（L139–211） | 重写为**收集全部候选 + 分层投票**（见 4.2 细则）；新增内部 `_vote_brand(candidates)` |
| `core/judge_engine.py` | `_NON_BRAND_BARE_TOKENS`（L121–136） | 追加 `AAA`/`prime`/`video`/`YouTube`/`NETFLIX`/`NFK`/`SAMYOUNG`/`SMT`/`RoHS`/`REACH` 等；**优先从 YAML 读取** |
| `rules/fields_blacklist.yaml` | `terms` | 追加 `SKYWORTH P/N`、`Supplier Code`（已有）、`Manufacturer`、`Manufacturer Name`、`Supplier Name` |
| `core/constants.py` | `DEFAULT_FIELD_BLACKLIST`（L174–183） | 同步追加（防漂移断言） |
| `rules/non_brand_tokens.yaml`（**新建**） | — | `tokens:` 列表（见上表） |
| `core/rule_repository.py` | 新增 `NonBrandTokenRules` + 加载 | 加载新 YAML |
| `tests/test_excel_probe.py` / 新增 `tests/test_brand_extraction.py` | — | 见 4.5 |

### 4.5 回归锚点

```python
# tests/test_brand_extraction.py（新增）
def test_cross_image_vote_picks_majority():
    """真实 seq=11 形态：img1='ONEL'(单图) … img9/10/11='Daewoo' → 取 Daewoo。"""
    texts = [_mk_ocr(f"ONEL"), _mk_ocr("WLO"), _mk_ocr("CONGO"),
             _mk_ocr("Daewoo"), _mk_ocr("Daewoo"), _mk_ocr("Daewoo")]
    assert extract_detected_brand(texts) == "Daewoo"

def test_single_image_noise_rejected():
    """单图孤例噪声 → 不采信（返回空，上层判 ⚠️）。"""
    assert extract_detected_brand([_mk_ocr("ONEL"), _mk_ocr("WLO")]) == ""

def test_field_name_not_brand():
    """SKYWORTH P/N 字段名 → 不得当品牌。"""
    assert extract_detected_brand([_mk_ocr("创维物料编号\nSKYWORTH P/N")]) != "SKYWORTH"

def test_label_image_priority():
    """&001 与其他图并列 → 取 &001 的候选。"""
    ...
```

### 4.6 裁决 2（**R4 新增**）：OCR 已知误读 → **自动纠正后比对**

> **业务裁决（Jason 老板，R4）**：**否决**「已知误读独立判噪（转 ⚠️）」方案，改采「**自动纠正为正确品牌后，再与申报值比对**」。

#### 4.6.1 规则定义

**流程（四步）**：

```
识别值 raw
  → ① 归一化/纠错（confusable_chars 归一化 + known_noise_samples 映射纠正）
  → ② 得到"纠正后确定值"corrected
  → ③ 用 corrected 与申报值 declared 比对
  → ④ 一致 → ✅ ；不一致 → ❌（依据 = corrected ≠ declared）
```

**数据源（全部外置于 `rules/noise_signals.yaml`，不硬编码）**：

| 源 | 字段 | 用途 |
|---|---|---|
| `confusable_chars` | 易混字符组（`["W","H"]` / `["N","H"]` / `["O","0"]` / `["I","1","l"]` …） | 把双方归一化为组内首字符后再比（**已生效**） |
| `known_noise_samples[].ocr` | 实测误读形态（`SKYHORTH P/H` / `IFR PIN` / `boori E339609` / `600-CX9`） | 命中 → 查映射 |
| `known_noise_samples[].expected_actual` | **正确值**（`SKYWORTH P/N` / `SKYWORTH P/N` / `baori` / `GXD-009`） | **此前未被使用**（R4 首次启用）：作为"纠正后确定值" |

**查找顺序（确定性，防歧义）**：
1. **精确归一化匹配**：识别值与 `sample.ocr` 归一化（去空白 + upper）后相等 → 取 `sample.expected_actual`；
2. **片段包含匹配**：识别值包含 `sample.ocr`（长度 ≥ 4）→ 取 `sample.expected_actual`；
3. **易混字符归一化**：双方经 `confusable_chars` 归一化后相等 → `corrected = declared`（视为一致）；
4. 均未命中 → `corrected = 原识别值`（不纠正）。

**关键约束（粒度对齐，见缺陷 F）**：纠正的**比对须在同一粒度上进行**——运行期若取的是 **P/N 行前缀**（`SKYHORTH`），则样本表须同时提供**前缀形态可命中的条目**（或匹配时对 `sample.ocr` 与其"品牌前缀"双向比对）。**不得**出现"样本存整行 `SKYHORTH P/H`、运行期传前缀 `SKYHORTH` → 谁都匹配不上"的死角（这正是缺陷 F）。

#### 4.6.2 与「不虚高」红线的兼容性（**架构师裁定：不违反**）

**裁定：纠正后不一致仍判 ❌，不违反 SOP 1.2「不虚高」。**

理由（与主理人判断一致）：
- SOP 1.2「不虚高」针对的是「**OCR 噪声造成的"假异常"**」——即"把疑似噪声当成真实差异"；
- 本规则在比对**之前**已用外置映射**消解噪声**、拿到**确定值**（`SKYWORTH` / `baori` 等）；纠正后的不一致是「**有证据证明不一致**」，正落 SOP 第 147 行 / 第 32 行「校验异常」定义；
- 故本规则**不是**把噪声当真异常，而是**先修复噪声、再据修复后的确定值判定**——与「不虚高」**同向**（都反对"拿噪声当差异"）。

> **与 R2「A′ 保留 ❌」的一致性**：整机品牌保留 ❌ 与本规则同源于 SOP 第 147 行「字段缺失/不一致 → ❌」；本规则只是把"误读"这一层噪声先纠正掉，使 ❌ 的**依据更准确、可审计**。

#### 4.6.3 对 seq=16 的影响（示例）

- 识别 `SKYHORTH` → 经 `confusable_chars`（W↔H）+ `known_noise_samples` 纠正为 `SKYWORTH`；
- 与申报 `DAEWOO` 比对 → **仍不一致** → 判 **❌**；
- **差异依据由"SKYHORTH≠DAEWOO"变为"SKYWORTH≠DAEWOO"**（更准确、可审计）。
- → **原 R3 的「(d) 新增已知误读独立判噪规则」作废**，以本节为准（seq=16 由 ⚠️ **改判为 ❌**）。

#### 4.6.4 需改代码位置（归 T06）

| 文件 | 位置 | 改动 |
|---|---|---|
| `core/noise_guard.py` | `classify_detail`（L286–406） | 在"②′ 已知噪声样本"分支（L326–330）前插入「纠正」步骤：命中 `known_noise_samples` → 取 `expected_actual` 作为 `right`（识别值）**再进入后续比对**；即把 `right` 替换为纠正值，而非直接判 `SUSPICIOUS` |
| `core/noise_guard.py` | `_is_known_noise_sample`（L592–629） | 粒度对齐修复（见缺陷 F）：对 `sample.ocr` **及其品牌前缀**双向匹配；或由 YAML 额外提供 `ocr_prefix` 字段 |
| `rules/noise_signals.yaml` | `known_noise_samples` | 启用 `expected_actual`；按需补 `ocr_prefix`（如 `ocr_prefix: "SKYHORTH"` / `"IFR"`），使运行期前缀可命中 |
| `tests/test_noise_guard.py` | 新增 | `test_known_misread_corrected_then_compared`：`SKYHORTH`→纠正 `SKYWORTH`→与 `DAEWOO` 比对 → ❌（**不再是 ⚠️**） |

> **口径红线说明**：本规则**不新增判定口径**——它把 SOP 附录 A.4「OCR 噪声虚高 → 改判」的**处理动作**从"改判合格"细化为"先纠正噪声值、再据确定值判定"，仍落 SOP 1.3/3.4。**属实现层。**

---

## 5. 缺陷 D 裁决：`无品` 加入 `none_tokens`

### 5.1 事实认定

实测 `is_none_token("无品") == False`（`无品` 不在 `DEFAULT_NONE_TOKENS`）。样本 `seq=1/5` 的图片识别值为 `无品`（**"无品牌"的 OCR 截断残片**，见 JSON `seq=1` evidence_text: `…无品…`），导致：
- `seq=1`：申报 `baori` vs 识别 `无品` → 未识别为"无" → 判 ⚠️（此条结果尚可）；
- 但若申报为"无"、识别为 `无品`，会因 `无品` 非 none_token 而**判 ❌**——这正是"假异常"温床。

### 5.2 裁决：**加入** `none_tokens`

**依据**：SOP 3.4 补充约定原文：
> 标签无出口标识 → 视为"无品牌"；申报品牌为"无/空" 且 标签也无品牌 → **判合格**

`无品` 是"无品牌"的 OCR 截断形态（`无品牌` → `无品`），语义等同"无品牌"，**必须归入 none_tokens**，否则违反「标签无标识 → 视为无品牌」。**属实现层补条目，不改口径。**

### 5.3 同步修改清单

| 文件 | 位置 | 改动 |
|---|---|---|
| `rules/brand_patterns.yaml` | `none_tokens`（L41–50） | 追加 `- 无品` |
| `core/constants.py` | `DEFAULT_NONE_TOKENS`（L194–204） | 追加 `"无品"` |
| `tests/test_rule_repository.py` | `test_none_tokens_matches`（L241–242） | 断言 `YAML == constants`，两边同加即通过 |

**回归锚点**：
```python
def test_wupin_is_none_token():
    assert is_none_token("无品") is True
```

---

## 5A. 缺陷 E 裁决（**R4 新增**）：`_apply_blacklist` 误伤型号字段

### 5A.1 事实认定（架构师独立复核，`core/element_parser.py`）

**实测复现**（主理人提供，架构师逐层核对源码确认）：
```python
raw = '...品牌:DAEWOO；型号：HS-8A50J-12  蓝牙遥控器，工作频率2.402GHz，...，适用于DAEWOO牌电视机'
parse(raw) -> brand='DAEWOO', model=''      # ← 型号 HS-8A50J-12 丢失
```

**逐层定位（源码核实）**：
| 层 | 函数 | 结果 |
|---|---|---|
| 字段识别 | `_is_model_key('型号')`（L562–582） | `True` ✓ |
| 取值 | `_extract_model_from_value(v)`（L529–534） | `''` ✗ |
| 清洗 | `clean_model_tail('HS-8A50J-12 ...')` | **`'HS-8A50J-12'` ✓（清洗本身正确）** |
| 护栏 | `_clean_model`（L660–669）→ `_apply_blacklist(cleaned, value)` | `''` ← **根因** |

**根因（源码 L282–285）**：
```python
haystack = context or candidate
for prefix in prefixes:
    if prefix and prefix in haystack:   # ← 全串"包含"匹配，非"捕获词前"
        return True
```
- `_clean_model` 把**整段原始值**（含 `适用于DAEWOO牌电视机`）作为 `context` 传入 `_apply_blacklist`；
- `is_blacklisted` 对 `haystack` 做**子串包含** → 命中 `适用于` → **整个型号值判空**。

**SOP 原文支撑（陷阱 #5，第 166 行逐字）**：
> **"适用于某品牌"语境** | 捕获词**前**若有 `适用于`/`用于`/`适用`，须跳过——那是适配对象不是申报品牌

**语义是"捕获词前"，不是"值中含"**。当前实现违反原文，且 `适用于` 是**品牌提取的语境排除词**，**不应让型号字段失效**。

### 5A.2 裁决（修法）

**裁决：① `is_blacklisted` 的 `skip_prefixes` 匹配改为"前缀窗口"（非全串包含）；② 适用范围限定"品牌字段"。**

**① 前缀窗口匹配（替代全串包含）**：
- 语义：仅当 **捕获词之前 N 字符内**出现 `适用于`/`用于`/`适用`/`适配` 才跳过；
- **N 取 8**（架构师裁定）：`适用于` 后通常紧跟品牌名（`适用于DAEWOO`），品牌名 ≤ 数十字符，8 字符足以覆盖"紧邻"场景，又能避免跨句误伤（如型号值尾部 `…适用于DAEWOO牌电视机` 与型号头 `HS-8A50J-12` 相距 > 8 字符 → 不再误伤）。
- 实现：定位捕获词（品牌候选）在原文中的位置，向前取 `N` 字符窗口，检查窗口内是否含 `skip_prefixes`。

**② 适用范围限定品牌字段**：
- `skip_prefixes` 语境排除**仅对品牌字段生效**（`_match_brand_value` 的调用点）；
- **型号字段（`_clean_model`）调用 `_apply_blacklist` 时 `skip_prefixes` 置空**（型号清洗已由 `clean_model_tail` + `anchor_regex` 处理脏尾，无需语境排除）；
- 理由：SOP 陷阱 #5 的语境排除对象是"申报品牌"，不涉及型号；型号的脏尾处理由陷阱 #7 负责。

**③ 保留项**：`terms`（字段名黑名单）**继续对型号字段生效**（型号若恰好等于字段名残片仍应判空），仅 **`skip_prefixes` 不对型号生效**。

### 5A.3 需改代码位置（归 T06）

| 文件 | 位置 | 改动 |
|---|---|---|
| `core/element_parser.py` | `is_blacklisted`（L247–287） | `skip_prefixes` 匹配由"`prefix in haystack`"改为"**捕获词前 N 字符窗口**含 prefix（N=8）" |
| `core/element_parser.py` | `_clean_model`（L660–669） | 调 `_apply_blacklist` 时 **`skip_prefixes` 传空**（只保留 `terms` 黑名单） |
| `core/element_parser.py` | `_match_brand_value`（L592–658） | 品牌路径**保留** `skip_prefixes`（前缀窗口版） |
| `tests/test_element_parser.py` | 新增 | 见 5A.4 回归锁 |

### 5A.4 回归锁（T06 必写）

```python
def test_model_not_killed_by_shiyongyu_context():
    """缺陷 E：型号尾部含『适用于X牌』不得致型号判空（SOP 陷阱#5 语义=捕获词前）。"""
    raw = "品牌:DAEWOO；型号：HS-8A50J-12  蓝牙遥控器，工作频率2.402GHz，适用于DAEWOO牌电视机"
    parsed = ElementParser(rule_repo).parse(raw)
    assert parsed.brand == "DAEWOO"
    assert parsed.model == "HS-8A50J-12"       # ← 核心断言（当前为 ''）

def test_brand_still_guarded_by_shiyongyu():
    """品牌语境排除仍生效：『适用于PHILIPS电视机』不判 PHILIPS 为申报品牌。"""
    parsed = ElementParser(rule_repo).parse("适用于PHILIPS电视机")
    assert parsed.brand != "PHILIPS"

def test_clean_model_tail_still_correct():
    """陷阱#7 型号脏尾清洗不受影响。"""
    assert clean_model_tail("HS-8A50J-12  蓝牙...2.402GHz...") == "HS-8A50J-12"
```

> **口径红线说明**：本裁决**纠偏实现以对齐 SOP 陷阱 #5 原文**（"捕获词前"），**不新增口径**。属实现层。

---

## 5B. 缺陷 F 裁决（**R4 新增，R4b 更正**）：兜底分支漏传字段致 `known_noise_samples` 静默为空

> ⚠️ **本节经过更正（R4 → R4b）**。诊断经历**三次迭代、两次纠正**（详见 5B.0 演进链）。**最终根因 = `NoiseGuard` 兜底构造分支漏传字段**，**不是**"加载链路断裂"（主理人初判），**也不是**"粒度不匹配"（架构师 R4 初判，已被主理人深入实测推翻）。

### 5B.0 诊断演进链（如实记录，方法论案例）

| 阶段 | 诊断 | 依据 | 结论 |
|---|---|---|---|
| **主理人初判** | "加载链路断裂" | `is_known_noise_sample()` 全 False | 不准确 |
| **架构师 R4 修正** | "粒度不匹配"（样本存整行 vs 运行期取前缀） | 复刻 `_is_known_noise_sample` 逻辑 | **有误** |
| **主理人深入实测 → 最终根因** | **兜底分支漏传字段** | 对照实验：`NoiseGuard()` vs `NoiseGuard(repo)` 分别测 `_noise.known_noise_samples` | **正确** |

**关键对照实验（定位根因的手段）**：
```
len(NoiseGuard()._noise.known_noise_samples)        == 0   ✗ 兜底路径（无 repo）
len(NoiseGuard(RuleRepository())._noise....)        == 4   ✓ repo 路径
len(JudgeEngine().guard._noise....)                 == 0   ✗
len(JudgeEngine(RuleRepository()).guard._noise....) == 4   ✓
```
→ **仅靠"分别测无参构造 vs 注入构造"才暴露**。R4 初判"粒度不匹配"时只测了注入路径（`is_known_noise_sample('SKYHORTH')==True`），**恰好绕过了缺陷所在的无参路径**。

> **方法论沉淀**：**"规则外置 + constants 兜底"设计下，必须验证两条路径（注入 repo / 无参兜底）规则集等价**。否则兜底路径会**静默降级**——不报错、悄悄失效（与 R11 空壳包同类风险）。

### 5B.1 事实认定（主理人实测 + 架构师源码复核）

**主理人实测（推翻"粒度不匹配"）**：
```python
ng = NoiseGuard(RuleRepository())
len(ng._noise.known_noise_samples)          # -> 4
ng.is_known_noise_sample('SKYHORTH P/H')    # -> True   ← 整行匹配正常
ng.is_known_noise_sample('SKYHORTH')        # -> True   ← 前缀匹配也正常
```
→ `noise_guard.py` 的 `_lookup_known_sample`（L687–740）**已有五重兜底**：①整行归一化相等 ②`ocr_prefix` 粒度相等 ③片段包含 ④前缀子串兜底（`P/N`/`P/H`/`PIN`）。**整行与前缀都能匹配 → 粒度问题不存在。**

> ⚠️ **架构师更正**：R4 初判"粒度不匹配 → 需新增 `ocr_prefix`"**错误**。`ocr_prefix` 五重兜底**已被实现**（实测 `rules/noise_signals.yaml` 4 条样本均已含 `ocr_prefix` 字段），**无需新增**。

**架构师源码复核（确认最终根因）**：`core/noise_guard.py:180–197` `_resolve_noise_rules()`
```python
def _resolve_noise_rules(self) -> NoiseSignalRules:
    if self._repo is not None:
        try:
            noise = self._repo.get().noise_signals
            if noise.confusable_chars:
                return noise                 # ← repo 路径：完整（含 known_noise_samples）
        except Exception:
            pass
    return NoiseSignalRules(                 # ← 兜底路径（L190–197）
        confusable_chars=...,
        fuzzy_max_edit_distance=...,
        fuzzy_min_length=...,
        fuzzy_verdict=...,
        # ⚠️ 漏传 known_noise_samples     → 空元组 ()
        # ⚠️ 漏传 low_confidence_threshold → 默认
        # ⚠️ 漏传 fragment_min_chars       → 默认
    )
```
`NoiseSignalRules`（`core/rule_repository.py`）实有 **8 个字段**：`confusable_chars` / `fuzzy_max_edit_distance` / `fuzzy_min_length` / `fuzzy_verdict` / `low_confidence_threshold` / `fragment_min_chars` / `known_noise_samples` / `source_path`。兜底构造**只传了前 4 个**，**漏传 3 个**。

**准确表述**：**「YAML 优先 / constants 兜底」设计中兜底分支构造不完整，漏传 3 个字段** —— 其中 `known_noise_samples` 漏传直接导致「已知误读纠正」在无参路径**完全失效**。

**影响面**：

| 项 | 结论 |
|---|---|
| 生产链路 | **不受影响**（`core/pipeline.py:176` 走 `JudgeEngine(self._repo)`，注入 repo） |
| 受影响范围 | ① `JudgeEngine()` / `NoiseGuard()` 无参构造（测试、CLI、自检）；② **打包风险**：YAML 缺失/损坏时兜底生效 → 「已知误读纠正」**静默失效且无报错** |
| 危险性质 | **静默降级** —— 与 R11 空壳包同类风险（不报错、悄悄失效） |

### 5B.2 裁决（修法）

**裁决：兜底分支必须与 repo 路径规则集等价（防打包漏 YAML 静默失效）；`ocr_prefix` 已实现，无需新增。**

**修法（优先级从高到低）**：
1. **首选（核心）**：`_resolve_noise_rules()` 兜底构造**补传全部字段** —— `known_noise_samples` / `low_confidence_threshold` / `fragment_min_chars`。方法：若 `core/constants.py` 缺 `DEFAULT_KNOWN_NOISE_SAMPLES` 等常量，**补常量**并纳入 `tests/test_rule_repository.py` 漂移断言；兜底即引用这些常量。
2. **等价性保证**：**兜底分支规则集 ≡ repo 路径规则集**（键维度一致），以回归锁 `test_noise_guard_default_and_repo_agree` 固化。
3. **`ocr_prefix` 说明**：**已实现，无需新增**（`known_noise_samples[].ocr_prefix` 4 条均已具备，`_lookup_known_sample` ②分支已消费）。

**必须同时保证**（回归锁，见 5B.3）：`NoiseGuard()`（无参）与 `NoiseGuard(repo)` 的 `known_noise_samples` 数量与内容等价；两者对 `'SKYHORTH'` / `'IFR'` / `'boori E339609'` / `'600-CX9'` 命中结果一致。

### 5B.3 需改代码位置（归 T06）

| 文件 | 位置 | 改动 |
|---|---|---|
| `core/noise_guard.py` | `_resolve_noise_rules()`（L180–197） | 兜底构造**补传** `known_noise_samples` / `low_confidence_threshold` / `fragment_min_chars`（引用 `constants` 常量） |
| `core/constants.py` | 噪声默认常量区 | **补** `DEFAULT_KNOWN_NOISE_SAMPLES` / `DEFAULT_LOW_CONFIDENCE_THRESHOLD` / `DEFAULT_FRAGMENT_MIN_CHARS`（若缺失） |
| `tests/test_rule_repository.py` | 漂移断言区 | **纳入**上述常量的 YAML↔constants 一致断言 |
| `rules/noise_signals.yaml` | `known_noise_samples` | **无需改**（`ocr_prefix` 已具备） |
| `core/noise_guard.py` | `_lookup_known_sample`（L687–740） | **无需改**（五重兜底已实现） |

**回归锁（T06 必写；主理人已给出具体断言，直接引用）**：
```python
def test_noise_guard_default_construction_has_samples():
    """缺陷 F：无参构造（兜底路径）必须带 known_noise_samples（当前为空）。"""
    ng = NoiseGuard()                                   # ← 无参 = 兜底路径
    assert len(ng.noise_rules.known_noise_samples) == 4  # ← 当前为 0

def test_noise_guard_default_and_repo_agree():
    """缺陷 F：兜底路径与 repo 路径规则集等价（防打包漏 yaml 静默失效）。"""
    from core.rule_repository import RuleRepository
    ng_def  = NoiseGuard()
    ng_repo = NoiseGuard(RuleRepository())
    assert (len(ng_def.noise_rules.known_noise_samples)
            == len(ng_repo.noise_rules.known_noise_samples))
    for val in ("SKYHORTH", "IFR", "boori E339609", "600-CX9", "SKYHORTH P/H"):
        assert (ng_def.is_known_noise_sample(val)
                == ng_repo.is_known_noise_sample(val) is True), val
```

> **口径红线说明**：本裁决为**实现层兜底等价性修复**（使两条规则加载路径一致），**不新增口径**。同时它是 **4.6「误读自动纠正后比对」的前置条件**——纠正规则要生效，兜底路径也必须能命中已知样本；两处须**同批次修**。

---

## 6. 第三节 口径问题裁决（逐条引 SOP 章节）

### 6.1 口径问题 1：`Customer model` vs 制造商型号 —— 型号比对基准取哪个？

**裁决：型号比对基准取「制造商型号」（本体）；`Customer model` 仅作整机上下文，不作本体型号证据。**

**SOP 原文支撑**：

**SOP 3.3 原文**：
> 品牌/型号可能出现在：… 标签的 `产品名称` / **`制造商型号`** 字段 …

**SOP 3.5 规则 #8 原文**：整机适配品牌"**不构成零部件本体品牌证据**"——同理，唛头上的 `Customer model`（客户/整机型号）**不构成本体型号证据**。

**SOP 陷阱 #8 原文**：
> ERP 描述与 PCB 丝印系统性不一致｜属厂内码差异（如 ERP `9KC9T` vs PCB `A9KB9G`），**不构成异常证据**

**实测印证**：`seq=17/18` 申报型号为空、识别型号取到 `50DA25QL`/`HS-8AA`（来自 `Customer model` 与 PCB `MODEL:HS-8AA`）。

**裁决细则**：
1. `_MODEL_PATTERNS` 中 **`Customer model` / `Customermodel` 分支移出"本体型号"提取**，改归"整机上下文"（与缺陷 A 一致）。
2. 本体型号证据来源优先级：`型号/规格型号/产品型号/制造商型号`（标签）> PCB `MODEL:` 丝印 > 其他。
3. 若识别型号仅来自 `Customer model` → 视为**无本体型号标识** → 该字段走 ⚠️（不虚高），**不得**因"申报型号为空 + 整机型号有值"判 ❌。

> **口径红线说明**：这是**纠正"取错字段造成假异常"**（任务书原文亦判定为"取错字段造成的假异常"），使实现对齐 SOP 3.3/3.5#8/陷阱#8，**不新增口径**。

### 6.2 口径问题 2：字段名残片（`SKYWORTH P/N`）算不算「图片明确有」？

**裁决：不算。** → 归"**图片侧仅有字段名残片，无本体标识**" → **⚠️ 缺图内标识**，**非 ❌**。

**SOP 原文支撑**：

**SOP 3.5 规则 #1 原文**：
> **字段名被当成值** | 建立字段名黑名单（`制造商全称`/`原产地`/`MFR P/N`/`Supplier Code`/**`创维物料编号`**…），提取到的值若在黑名单内则**判为空**

**SOP 3.5 规则 #3 原文**：
> **"创维"被误当品牌** | "创维物料编号"是字段名，全文搜索品牌时**须排除该上下文**

**SOP 1.3 原文**：
> ⚠️ 缺图内标识 … = **没找到证据**；❌ = 有证据证明不一致

`SKYWORTH P/N` 是 `创维物料编号` 的**英文字段名**（SOP 3.5#3 点名）。它被提取为"品牌"后，**该值应判为空**（SOP 3.5#1 原文"判为空"）；判空后，图片侧 = **无本体品牌标识** → **⚠️**（SOP 1.3 "没找到证据"）。

**SOP 附录 A.4 原文**：
> 4 条噪声虚高改判合格：… 1 条 `IFR PIN`（=SKYWORTH P/N 误读）…

→ SOP 已明确该场景**应改判合格**（历史上），至少**绝不判 ❌**。本裁决取**保守口径 ⚠️**（因无法确证本体无品牌时判合格），与 SOP 1.2「不虚高」一致。

**裁决细则**：
1. `SKYWORTH P/N` / 字段名残片 → 命中 `fields_blacklist` → 识别值**置空**；
2. 置空后 → `_FIELD_DETECTED_MISSING`（申报有值）/ `both_absent`（申报无值）→ **⚠️ / ✅**；
3. **禁止**把"字段名残片"当作"图片明确有"的证据来判 ❌。

### 6.3 口径问题 3：本体品牌的图片来源范围 —— 是否限定标签图（`&001`）？

**裁决：不限定单张图（保持全量）；但本体证据需"全量交叉投票"，且整数机上下文图单独剔除。**

**SOP 原文支撑**：

**SOP 3.3 原文**：
> **结论**：**必须全量 OCR**，不能只取第 1 张。

→ SOP **明确否决**"只取第 1 张"。故**不得**限定图片来源为 `&001`。

但 SOP 3.3 同时区分了"信息丰富度"，且 SOP 3.5#8 明确唛头图（整机）**不构成本体证据**。两者结合：

**裁决细则**：
1. **全量 OCR + 全量候选收集**（不限定图）；
2. **投票时按图片类型加权**：`&001`（标签/唛头）权重高，但**含整机上下文的图其 `Brand:` 行不计入本体品牌候选**（剔除整机证据）；
3. **单图孤例不采信**（需 ≥2 图一致，见 4.2）；
4. 证据文本（`evidence_text`）仍可含实物照/丝印，但**判定只认投票结果**。

> ⚠️ **关于"上岗证"类证书图**：任务书提到样本混有证书图。裁决：**证书图不参与品牌投票**（其 `text_raw` 不含 `Brand:` 标签与裸品牌 token 时自然落空；若混入，由 4.2 的"≥2 图一致"过滤）。**不新增"证书图"专门规则**（避免过度工程），仅靠投票一致性兜住。

> **口径红线说明**：本裁决**严格遵守 SOP 3.3 的"必须全量"**（未限定单张），只是把"全量证据如何收敛为一个值"落成投票规则（附录 B#3 的正式化）。属实现层。

---

## 7. T06 修复任务列表（有序、含依赖、按实现顺序）

> 说明：本裁决**不新增任务编号体系**，按"批次"组织，供 T06 一次性执行。**测试文件统一置 `customs_declaration_checker/tests/`**。
> 依赖：**批次 1（规则外置层）→ 批次 2（护栏层）→ 批次 3（判定层）→ 批次 4（回归固化）**；批次 1 内两项可并行。

### 批次 1：规则外置层（YAML + constants，**必须同批改以保证一致性断言通过**）

| ID | 文件 | 位置 | 改法 | 依赖 |
|---|---|---|---|---|
| **T06-01** | `rules/whole_machine_brand.yaml` | `context_tokens` | **删** `Customermodel` / `Customer model`；**新增** `context_scope: whole_image` | — |
| **T06-02** | `core/constants.py` | `DEFAULT_WHOLE_MACHINE_CONTEXT`（L220–231） | **删** 对应两项；**新增** `DEFAULT_WHOLE_MACHINE_SCOPE = "whole_image"` | — |
| **T06-03** | `rules/brand_patterns.yaml` | `none_tokens` | **追加** `- 无品` | — |
| **T06-04** | `core/constants.py` | `DEFAULT_NONE_TOKENS`（L194–204） | **追加** `"无品"` | — |
| **T06-05** | `rules/fields_blacklist.yaml` | `terms` | **追加** `SKYWORTH P/N`、`Manufacturer`、`Manufacturer Name`、`Supplier Name` | — |
| **T06-06** | `core/constants.py` | `DEFAULT_FIELD_BLACKLIST`（L174–183） | 同步追加 | — |
| **T06-07** | `rules/non_brand_tokens.yaml`（**新建**） | — | `tokens:`（`AAA`/`prime`/`video`/`YouTube`/`NETFLIX`/`NFK`/`SAMYOUNG`/`SMT`/`RoHS`/`REACH`/`MADE`/`CHINA`/`CARTON`/`JOBNO`…） | — |
| **T06-08** | `core/rule_repository.py` | `RULE_FILES` + `WholeMachineBrandRules` + 新增 `NonBrandTokenRules` | 加载新 YAML；`WholeMachineBrandRules` 增 `context_scope`；新增 token 规则类 | T06-07 |

**批次 1 测试点**：
- `tests/test_rule_repository.py::TestConsistencyWithConstants` **全部通过**（7 项 YAML↔constants 一致断言；`Customer model`/`无品` 两边同改）。
- `tests/test_rule_repository.py::test_whole_machine_loaded` **改为**断言 `context_scope == "whole_image"`（移除 `window == 3` 硬断言）。
- 新增 `tests/test_rule_repository.py::test_non_brand_tokens_loaded`。

### 批次 2：护栏层（noise_guard）

| ID | 文件 | 位置 | 改法 | 依赖 |
|---|---|---|---|---|
| **T06-09** | `core/noise_guard.py` | `is_whole_machine_brand`（L447–457） | `context_scope == "whole_image"` → `neighborhood = rows`（整图全文）；要求 `Brand:` 与 token **同一 OcrText 内** | T06-01,08 |
| **T06-10** | `core/noise_guard.py` | `_resolve_whole_rules`（L199–212） | 兜底默认补 `context_scope` | T06-08 |
| **T06-11** | `core/noise_guard.py` | `is_field_name_misread`（L513–536） | 确认新黑名单术语被覆盖（`SKYWORTH P/N` 等） | T06-05 |

**批次 2 测试点**（新增 `tests/test_whole_machine_brand.py`）：
- `test_customer_model_not_trigger`（A 回归锚点）
- `test_carton_jobno_still_trigger`（不误伤）
- `test_whole_machine_whole_image_scope`（B 回归锚点）

### 批次 3：判定层（judge_engine）—— **核心**

| ID | 文件 | 位置 | 改法 | 依赖 |
|---|---|---|---|---|
| **T06-12** | `core/judge_engine.py` | `extract_detected_brand`（L139–211） | 重写为**收集全部候选 + 分层投票 + 标签图加权 + ≥2 图一致** | T06-07,08 |
| **T06-13** | `core/judge_engine.py` | `_NON_BRAND_BARE_TOKENS`（L121–136） | **改为从 `rules/non_brand_tokens.yaml` 读取**（YAML 优先，常量兜底；**必须外置，见第 9 节**） | T06-07,08 |
| **T06-14** | `core/judge_engine.py` | `_MODEL_PATTERNS`（L111–114） | **移出** `Customermodel`/`Customer model`（不作本体型号） | T06-01 |
| ~~T06-15~~ | — | — | **❌ 已作废（R2）**：原「`_field_state` 整机 → `_FIELD_SUSPICIOUS`」**撤销**（违反 SOP 3.4 第 147 行「字段缺失→❌」） | — |
| ~~T06-16~~ | — | — | **❌ 已作废（R2）**：原「删除 `_apply_degrade_guard` 推涨分支」**撤销**（该分支为正确实现，**保留不动**） | — |
| **T06-15R** | `core/judge_engine.py` | `_field_state`（L643–672）+ `_apply_degrade_guard`（L698–724） | **保留不动**（整机品牌 → ❌，依 SOP 3.4 第 147 行 + 附录 A.4） | — |
| **T06-16R** | `core/judge_engine.py` | 集成验证 | **验证 `_apply_degrade_guard` 整机分支在缺陷 A/B 修复后不再被错误触发**：即"非整机图"（仅 `Brand:`、无 `CARTON No.`/`JOB NO.`）不得命中整机上下文 → 不得因整机分支判 ❌。**新增用例 `test_whole_machine_branch_not_falsely_triggered`** | T06-09, T06-16R 依赖批次 1–2 完成 |

**批次 3 测试点**（新增 `tests/test_brand_extraction.py` + 扩充 `tests/test_judge_engine.py`）：
- 4.5 节 4 个品牌提取用例（T06-12）；
- **`test_whole_machine_brand_preserves_fail`（R2：断言 ❌，非 ⚠️）**；
- **`test_whole_machine_branch_not_falsely_triggered`（T06-16R：验证分支不再误触发）**；
- `test_customer_model_not_body_model`（口径 1）。

### 批次 4：真实样本回归固化（**QA #9 的验收基座**）

| ID | 文件 | 位置 | 改法 | 依赖 |
|---|---|---|---|---|
| **T06-17** | `tests/test_real_sample_sa26090215.py`（**新建**） | — | 用 `process` 修复后**实跑** 18 条（或离线跑 `JudgeEngine.judge`），按 **R4 四层硬断言 + 观测项**（见 **8.4**）：<br>① 硬断言：seq 9/11/12/13/14/**15** = ❌ 且带"整机"差异；seq 2 = ❌（**不再禁"整机"**）；seq 3/6/10 = ⚠️；seq 7/8 ≠ ❌；总数 = 18。<br>② **观测项（不硬断言）**：seq 1/16/17/18 打印实际 verdict+diffs，交主理人交叉复核。<br>③ **不再比对推荐区间**（R4 撤销区间，见 8.1）。 | 批次 1–3 |
| **T06-18** | `tests/test_judge_engine.py` | 新增 | **R4 变更**：`SKYHORTH`/`IFR` 经**裁决 2 纠正后比对**断言 ❌（依据 `SKYWORTH≠DAEWOO`）；原"硬断言不为 ❌"**作废** | 批次 3 |
| **T06-19** | 全量回归 | — | `pytest -q`（原 578 用例 + 新增，**原用例不得因本次修改而失败**；若原用例断言了旧行为（如 `test_whole_machine_loaded` 的 `window==3`），**以本裁决为准同步更新该断言**并在提交说明中列出） | 批次 1–3 |
| **T06-20**（**R4 新增**） | `core/noise_guard.py` | `classify_detail`（L286–406）+ `_lookup_known_sample`（L687–740） | **裁决 2 + 缺陷 F**：① 命中 `known_noise_samples` → 取 `expected_actual` **纠正识别值后比对**（不再直接判 ⚠️）；② 缺陷 F **非**粒度问题——五重兜底已实现，**保留**；真正修复见 T06-21 | 批次 1–3 |
| **T06-21**（**R4b 更正**） | `core/noise_guard.py` + `core/constants.py` | `_resolve_noise_rules()`（L180–197）兜底分支 + constants | **缺陷 F（真实根因）**：兜底构造**漏传 3 字段** → **补传** `known_noise_samples` / `low_confidence_threshold` / `fragment_min_chars`（引用 `constants` 常量；缺则补 `DEFAULT_KNOWN_NOISE_SAMPLES` 等）；**保证兜底 ≡ repo 路径**。⚠️ `ocr_prefix` **已实现，无需新增**（原 R4 指令作废）；`rules/noise_signals.yaml` **无需改** | 批次 1–3 |
| **T06-22**（**R4 新增**） | `core/element_parser.py` | `is_blacklisted`（L247–287）+ `_clean_model`（L660–669） | **缺陷 E**：`skip_prefixes` 改"捕获词前 N 字符窗口（N=8）"；**型号字段 `skip_prefixes` 置空**（仅保留 `terms`） | 批次 1–3 |

**批次 4 测试点**：见第 8 节（R4 方法论 + 逐条依据表 + 四层断言）。

---

## 8. 回归验收标准（**R4：方法论 + 逐条依据表**，不再给推荐区间）

### 8.0 修订演进链（R2 → R3 → R4 → R4b，如实记录）

> ⚠️ **本节经历四次修订。** 演进链本身是"**规格随证据迭代**"的案例，如实记录如下，供后续维护者理解每条规格的来历。

**① R2 → R3：3 处硬断言规格错误（主理人逐图实跑驳回，架构师复核确认）**

| # | R2 错误 | 证据 | R3 更正 |
|---|---|---|---|
| **1** | 断言 `for seq in (2,3,6,9,10,11,12,13,14,15)` 必须 FAIL —— 10 个 seq 里 **4 个错**（3/6/10/15） | 实测：`seq=3/6/10` 的 `differences` 品牌 note 为 **「OCR 置信度低于阈值 0.5，证据不足」**（低置信度→SUSPICIOUS→⚠️），**不是整机命中** | 改为分层断言（见 8.4） |
| **2** | `seq=2` 归因「整机品牌→❌」 | 实测：`seq=2` note = **「申报『』与图片『SANSUI』明确不一致」**；其 FAIL 依 SOP 3.4 第 147 行**第 4 条**，**与整机路径无关** | 归「申报缺失」层 |
| **3** | `assert not _is_whole_machine_for_seq(11, "&001")` 引用**不存在的函数** | `grep` 确认该函数不存在；真实 API 为 `NoiseGuard.is_whole_machine_brand(text, *, brand_value="")` | 删除该断言 |

**② R3 → R4：seq=15 归属的演进（**本轮核心**）**

> **规格演进链（如实记录）**：
> | 版本 | seq=15 归属 | 依据 |
> |---|---|---|
> | **R2** | 归**第 1 层**（整机→❌） | 当时**误认定"seq=15 无整机命中图"** |
> | **R3** | 改归**第 4 层**（"不得 FAIL"） | 架构师**自行更正**："实测 seq=15 img1 **有**整机命中"；并据"型号双侧一致、FAIL 仅因品牌噪声"主张消除误报 |
> | **R4** | **最终归第 1 层（整机→❌）** | **老板口径裁决 1**：seq=15 与 seq=12 **结构完全同构**（见下），必须**同判**；整机品牌沿 SOP 附录 A.4 历史口径判 ❌ |
>
> **R4 更正理由**：R3 的"第 4 层"结论**建立在"seq=15 无整机命中"这一已被推翻的前提上**。经主理人 dump JSON 逐字段复核，**seq=12 与 seq=15 结构同构**：
>
> | 字段 | seq=12 | seq=15 |
> |---|---|---|
> | `decl_brand` | `''` | `''` |
> | `decl_model` | `A7A01G` | `A9KFBG` |
> | `det_model`（双侧一致） | `A7A01G` | `A9KFBG` |
> | `det_brand` | `Daewoo` | `Daewoo` |
> | 整机命中图 | img8（`&008`：`Brand:Daewoo`+CARTON+JOB NO.） | img1（`&001`：同上） |
>
> **二者同构 → 必须同判。** 依裁决 1（整机品牌 → ❌），**seq=15 判 ❌**，**第 4 层断言整体删除**。

**④ R4 另一处规格错误更正（L2 的"不得含整机"）**

> R3 第 2 层中的 `assert not any("整机" in d.note ...)`（seq=2）**已删除**。理由：该断言基于**旧 `window=3` 代码**的实测，与**缺陷 B（整图全文检索）改动自相矛盾**。实测 `seq=2` img5 含 `Brand:SANSUI`+`JOB NO.`+`CARTON No.`，**整图口径下必然命中整机**。seq=2 仍判 FAIL（依 SOP 147 第 4 条申报缺失），但 note **会**含"整机"，**断言不得禁止**。

**⑤ R4 → R4b：缺陷 F 根因诊断的演进链（**方法论案例**）**

> **三次诊断、两次纠正**，最终靠**对照实验**定位：
>
> | 阶段 | 诊断 | 依据 | 结论 |
> |---|---|---|---|
> | **主理人初判** | "加载链路断裂" | `is_known_noise_sample()` 全 False | 不准确 |
> | **架构师 R4 修正** | "粒度不匹配"（样本存整行 vs 运行期取前缀） | 复刻 `_is_known_noise_sample` 逻辑 | **有误** |
> | **主理人深入实测 → 最终根因** | **兜底分支漏传字段**（`_resolve_noise_rules()` 漏传 `known_noise_samples` 等 3 字段） | 对照实验：`NoiseGuard()`（0 条）vs `NoiseGuard(repo)`（4 条） | **正确** |
>
> **两份"推翻证据"**：① 注入 repo 时**整行与前缀都能命中**（五重兜底已实现）→ 否定"粒度不匹配"；② **无参构造时样本为 0** → 暴露真实缺陷在**兜底路径**。
>
> > **方法论沉淀（有沉淀价值）**：**"规则外置 + constants 兜底"设计下，必须验证两条路径（注入 repo / 无参兜底）规则集等价**。R4 初判之所以错，是因为只测了注入路径（`is_known_noise_sample('SKYHORTH')==True`），**恰好绕过了缺陷所在的无参路径**。只有"分别测无参 vs 注入"这一**对照实验**才暴露根因。兜底不等价 = **静默降级**（不报错、悄悄失效），与 R11 空壳包同类风险。

### 8.1 回归验收方法论（**R4：不再给推荐区间**）

> **R4 裁决：撤销一切"推荐区间"（如 `[1,3]`/`[5,7]`/`[8,11]`）。**
>
> **理由**：历史两轮区间均被证明**建立在错误归因上**（R2 的 `[8,10]` 含 4 个错项；R3 的 `[5,7]` 又把 seq=15 错列 L4）。给出的区间**会诱导工程师"凑数"**，反噬"不虚高/不误放"红线。
>
> **替代方案：以实测为准，逐条对照 SOP 复核。** 具体：
> 1. T06 修复后**实跑** `process` 全 18 条，**导出四类 `counts` + 逐条 `verdict`/`differences`**；
> 2. **逐条**用 8.2 的"判定依据表"对照 SOP 条款复核——**依据成立才认可该 verdict**，不看总数、不设目标区间；
> 3. 任一条**依据不成立**（如把低置信度当整机、把申报缺失漏判）→ **视为缺陷，回修**；
> 4. 复核结论**回报主理人**，由主理人与 T06 实测**交叉复核**后固化基线。
>
> **禁止**：为达成某区间而调参、放宽断言、或改动 SOP 条款。

### 8.2 逐条判定依据表（**R4：每 seq 一行 = 判定 + SOP 依据 + 是否争议**）

> 说明：本表是**复核依据表**，非"期望值表"。T06 实测后逐条对照。

| seq | 申报(b,m) | 同图整机命中图 | 判定 | 依据（SOP 条款） | 争议? |
|---|---|---|---|---|---|
| 1 | baori / — | img3 | ⚠️ | 品牌多值并存 + 证据不足（SOP 1.3 / 第 29 行） | ⚠️ 待实测确认 |
| 2 | — / — | img5 | **❌** | 申报缺失但图片明确有 → SOP 3.4 第 147 行**第 4 条**（**与整机无关**；note **会**含"整机"，不再禁止） | 否 |
| 3 | — / — | img7 | ⚠️ | 低置信度 OCR（<0.5）证据不足 → SOP 第 29 行 | 否 |
| 4 | 宇同 / — | — | ⚠️ | 跨文字体系（宇同↔拉丁）→ SOP 1.2 不虚高 | 否 |
| 5 | baori / — | — | ✅ | `无品`→none + `baori` 一致 → SOP 3.4 规则① | 否 |
| 6 | YUTONG / — | img5 | ⚠️ | 低置信度 OCR 证据不足 → SOP 第 29 行 | 否 |
| 7 | YUTONG / — | — | ⚠️ | 字段名残片（`MFR`）判空 → 无本体标识 → SOP 3.5#1 + 1.3 | 否 |
| 8 | — / — | — | ⚠️ | 字段名残片 `SKYWORTH P/N` 判空 → SOP 3.5#1/#3 + 1.3 | 否 |
| 9 | — / L8M30E0 | img2 | **❌** | 整机命中 + 品牌字段缺失 → SOP 3.4 第 147 行 + 附录 A.4 | 否 |
| 10 | — / L5B40L1 | img10 | ⚠️ | 低置信度 OCR 证据不足 → SOP 第 29 行 | 否 |
| 11 | — / A9KB9G | img9/10/11 | **❌** | 整机命中（品牌缺失）→ SOP 147 + A.4；型号一致不改变品牌缺失 | 否 |
| 12 | — / A7A01G | img8 | **❌** | 整机命中（品牌缺失）→ SOP 147 + A.4 | 否 |
| 13 | — / A7A01G | img1 | **❌** | 整机命中（品牌缺失）→ SOP 147 + A.4 | 否 |
| 14 | — / A9KA8B | img8 | **❌** | 整机命中（品牌缺失）→ SOP 147 + A.4 | 否 |
| **15** | **— / A9KFBG** | **img1** | **❌** | **整机命中（品牌缺失）→ SOP 147 + A.4**；**R4：与 seq=12 同构，同判 ❌** | 否（R4 已决） |
| 16 | DAEWOO / — | — | **❌** | **裁决 2（R4）**：`SKYHORTH` 经纠正为 `SKYWORTH` → 与 `DAEWOO` 不一致 → ❌（依据 = `SKYWORTH≠DAEWOO`） | 否（R4 新增规则） |
| 17 | DAEWOO / — | img3 | 观测项 | 品牌侧合格（大小写）；FAIL 仅来自型号（值取自 `Customer model`）→ **缺陷 A 修复后型号来源变化** | ✅ 观测（依实测） |
| 18 | DAEWOO / — | img4 | 观测项 | 品牌侧合格；型号 申报空 vs PCB `MODEL:` 本体型号 → 依修复后取值定 | ✅ 观测（依实测） |

> **注**：① **争议列**标 ⚠️/✅ 的 seq（1/17/18）**不设硬断言，均为观测项**，以 T06 实测 + 主理人交叉复核为准；② **seq=16 由 R3 的 ⚠️ 改判为 ❌**（裁决 2 生效）；③ 申报 `decl_model=""` 的根因含 T02 parser 未提取 `型号：HS-8A50J-12`（见**缺陷 E**）—— **归因记「待 T02 复修 parser」**。

### 8.3 关于「13/5/4 基准」的处理（**R4：仅陈述关系，不作目标**）

- SOP 附录 A.4 的历史结果「13 合格 / 5 异常（全为外箱整机品牌类）」**与本次口径同向**：整机品牌类 → ❌。
- **R4 不设定"应达到 5 异常"或任何目标数**——按 8.1 方法论，**只看每条依据是否成立**，不追求数量吻合。
- 若实测 ❌ 数显著偏离 A.4，**先查"整机命中范围是否修复完整"（缺陷 A/B）与"低置信度是否被误当整机"（8.0 错误 1）**，而非调参。

### 8.4 硬断言（**R4：四层**，第 4 层已删除、seq=15 归第 1 层、L2 禁"整机"已删除）

```python
# ── 第 1 层：真整机命中（同图 Brand: + CARTON/JOB NO）→ 依 SOP 147 保留 ❌，差异须标"整机"
#    实测同图整机命中 seq: 9→img2, 11→img9/10/11, 12→img8, 13→img1, 14→img8, 15→img1
for seq in (9, 11, 12, 13, 14, 15):
    assert results[seq].verdict == Verdict.FAIL, f"seq={seq} 整机品牌应保留 ❌"
    assert any("整机" in (d.note or "") for d in results[seq].differences), \
        f"seq={seq} 整机品牌 ❌ 须带'整机'差异说明"

# ── 第 2 层：申报缺失但图片明确有 → SOP 147 第 4 条 ❌（与整机无关）
#    ⚠️ R4：删除原「不得含'整机'」断言（与缺陷 B 整图口径自相矛盾；seq=2 整图下会命中整机）
assert results[2].verdict == Verdict.FAIL

# ── 第 3 层：OCR 证据不足（低置信度）→ 依 SOP 第 29 行 ⚠️，不得 FAIL
for seq in (3, 6, 10):
    assert results[seq].verdict == Verdict.NO_MARK, f"seq={seq} 低置信度证据不足应 ⚠️"

# ── 第 4 层：字段名残片 / 跨体系类不得 ❌
for seq in (7, 8):
    assert results[seq].verdict != Verdict.FAIL, f"seq={seq} 字段名残片不得判 ❌"

# ── 观测项（不硬断言，打印实际值供主理人交叉复核）：seq 1 / 16 / 17 / 18
for seq in (1, 16, 17, 18):
    print(f"observed seq={seq}: verdict={results[seq].verdict} "
          f"diffs={[d.note for d in results[seq].differences]}")

# ── 总数守恒
assert sum(counts.values()) == 18
```

> **附注（原 R2 错误断言 3）**：`assert not _is_whole_machine_for_seq(...)` **已删除**（该函数不存在）。如 T06 仍需断言"某图不得命中整机"，**改用真实 API + 该图真实 `text_raw`**：
> ```python
> guard = NoiseGuard(rule_repo)
> assert not guard.is_whole_machine_brand(seq11_img1_text, brand_value="Daewoo"), \
>     "seq=11 &001 非整机图，不得命中整机上下文"
> ```

> **附注（R3 已删、R4 确认删除）**：原 R3 的「第 4 层：`results[15].verdict != FAIL`」**整体作废** —— seq=15 现归**第 1 层**，断言 **=`FAIL` 且带"整机"**。

### 8.5 四条红线断言（QA #9 复用）

| 红线 | 断言 |
|---|---|
| **不虚高** | **由"字段名残片/跨体系/垃圾值比对"触发的 ❌ 条数 = 0**（整机品牌类 ❌ 属口径内，不计入虚高）；`SUSPICIOUS` 记录无一提为 `FAIL` |
| **不误放** | 每条结果 `evidence_text` / `image_paths` 非空（证据链完整）；整机品牌 ❌ 必带 `note="外箱整机品牌…"` |
| **可追溯** | JSON 落盘含每条 `差异明细` + `image_paths` + 图片路径 |
| **只读/外置/分层** | `原始输入` 未变更；规则全在 `rules/*.yaml`（含新建 `non_brand_tokens.yaml`）；`core/` 无 PySide6 import |

---

## 9. 对既有架构文档的增量说明（**R2：主理人已批准追加 13.14**）

经主理人确认，**由架构师在架构文档末尾追加 `13.14 真 OCR 误报缺陷裁决（A/B/C/D）`**（一句话索引 + 关键裁决 + T06 归属），正文第 1–13.13 节**不动**。详见本次提交的第二项交付物（`架构设计_报关申报要素自动校验工具_0916.md` 末尾追加）。

---

## 10. 口径红线自检（**R2 修订**：含 4 条 SOP 铁证原文引用）

| 检查项 | 结论 |
|---|---|
| 是否新增判定口径？ | **否**。所有裁决均为"实现层"——把 SOP 正文既有语义（3.3 全量 / 3.4 双方无 + 第 147 行字段缺失 / 3.5#1#2#3#8 / 1.2 不虚高 / 1.3 三态 / Phase4 归类）落成可执行条目 |
| **A′ 是否改口径？** | **R2：否，且 A′ 已撤销**。整机品牌 → **保留 ❌**，依据见下「四条铁证」。初版"改判 ⚠️"作废。 |
| 是否与 §13.13 冲突？ | **否**。13.13 裁决"比对输入源/变体优先级/ImageEvidence"，本节裁决"图片侧提取与整机判定"，互补不冲突 |

### 10.1 SOP 原文铁证（A′ 撤销依据，含行号，可逐行复查 `成果产出/报关申报要素自动校验SOP_0916.md`）

**铁证 1 — SOP 第 141–149 行「3.4 比对判定规则（5 条核心）」，第 147 行逐字**：
```
| 3 | 文字有差异、字段缺失、图片无对应字段 | ❌ 校验异常（标注差异明细） |
```
→ **「字段缺失」明确归 ❌**。整机品牌 = 本体品牌字段缺失 → **❌**。

**铁证 2 — SOP 第 32 行「关键区分」逐字**：
```
> 校验异常 = 有证据证明不一致；缺图内标识 = 没找到证据。两者不可混为一谈。
```
→ 整机品牌场景**有证据**（唛头 `Brand:Daewoo` 实打实可见）→ 属 ❌，**非** ⚠️。

**铁证 3 — SOP 附录 A.4 第 381 行（上一版真实执行统计）逐字**：
```
- 结果：13 合格 / 5 异常（全为外箱整机品牌类）
```
→ 同一样本、同一口径的历史事实：整机品牌类**归入「异常」（❌）**，构成 5 条异常全部。

**铁证 4 — SOP 四、Phase 4「异常类型归类表」第 243 行「处置」列**：
```
| 外箱整机品牌 | `Brand:Daewoo` + `CARTON`/`JOBNO` 上下文 | 保留异常，进人工复核 |
```
→ 「处置」列**是执行动作指引**（列内同时有「OCR 噪声虚高 → 改判合格」「等价证据 → 改判合格」），**不是四类判定标**。「保留异常，进人工复核」= **保留 ❌ 状态** + 转人工确认。初版把它当判定标用是**推论错误**。

### 10.2 R2 自检结论

| 检查项 | 结论 |
|---|---|
| A′ 是否改口径？ | **否**（R2）。整机品牌 → 保留 ❌，**符合 SOP 3.4 第 147 行 + 附录 A.4**；源码 `judge_engine.py:713-724` 分支**正确，保留不动** |
| 缺陷 A/B/C/D 是否改口径？ | **否**。A（移出误列 token）/B（窗口语义）/C（投票一致性）/D（`无品`）均为**实现层**，使整机**命中范围回归正确**，不改"整机→❌"的定性 |
| 口径问题 1/2/3 是否改口径？ | **否**。均引 SOP 3.3/3.5#1/#3/#8/陷阱#8 原文，属"取正确输入源 + 正确判空" |
| 是否违反「不虚高」？ | **否**。R2 明确：整机品牌**不是** OCR 噪声（是图上真实文字），判 ❌ 不违反 1.2；**真正违反** 1.2 的是缺陷 A/B/C/D 的垃圾值比对，已由本裁决剔除 |
| 是否违反「不误放」？ | **否**。整机品牌 ❌ 保留 + 强制 `note` 证据，反而**强化**了不误放 |
| 「13/5/4 基准」如何处理？ | **R4**：**撤销一切"推荐区间"**（含 R3 的 `[5,7]`）；改为**方法论 + 逐条依据表**（8.1 / 8.2），**只看每条依据是否成立**，不追求数量吻合 |
| 是否改动 core/rules 代码？ | **否**。仅改本裁决文档 + 架构文档 13.14.5 节 |
| **R3：回归规格是否与主理人实测一致？** | **是**。3 处错误已按主理人逐图实测更正（第 8.0 节） |
| **R4：老板口径是否落地？** | **是**。裁决 1（整机→❌，seq=15 归 L1）、裁决 2（误读纠正后比对，§4.6）、裁决 3（四类全开）、缺陷 E（§5A）、缺陷 F（§5B）、§4.2 min_images 分层，全部写入 |
| **R4：新增项是否改口径？** | **否**。裁决 2 属"先消解噪声再据确定值判定"（与不虚高同向）；缺陷 E 属"纠偏实现对齐 SOP 陷阱#5 原文"；缺陷 F 属"粒度对齐"；§4.2 属"正式化已成实现" |

> **R2 总裁决结论**：A′ 已撤销（整机品牌**保留 ❌**，4 条 SOP 铁证）；缺陷 A/B/C/D 给出唯一裁决；两处口径问题逐条引 SOP；T06 任务按 4 批次分解（含 R2 撤销/新增项）。全部改动属实现层，SOP 判定口径**逐字未动**。
>
> **R3 总裁决结论**：第 8 节回归规格经主理人实测驳回后重写为分层断言；3 处错误全部更正。（**注：R3 给出的期望区间 `✅∈[1,3] / ❌∈[5,7] / ⚠️∈[8,11]` 已被 R4 撤销**，见下。）
>
> **R4 总裁决结论（最新，老板口径拍板）**：① **裁决 1** 整机品牌 → **❌**（沿 A.4 历史口径），**seq=15 与 seq=12 同构 → 同判 ❌**（R3 的 L4 断言作废）；② **裁决 2** OCR 已知误读 → **自动纠正后比对**（新增 §4.6），seq=16 由 ⚠️ **改判 ❌**；③ **裁决 3** 工作台四类全开（维持现状）；④ **缺陷 E**（`_apply_blacklist` 误伤型号 → §5A）、**缺陷 F**（`known_noise_samples` 粒度不匹配 → §5B）；⑤ **§4.2 正式化** `min_images` 分层；⑥ **第 8 节撤销一切推荐区间，改"方法论 + 逐条依据表"**（§8.1 / §8.2）。**缺陷 A/B/C/D 与 A′ 的实体裁决不变。** 全部新增项均属实现层，SOP 判定口径**逐字未动**。

---

## 11. 主理人三项待确认的答复（**R2**）

| # | 主理人倾向 | **架构师答复** | 理由/落地 |
|---|---|---|---|
| **①** seq=16/18 边界归因 | 允许暂标「待 T02 复修 parser」但**不得**判 ❌ 归因到整机品牌 | **同意**（与主理人一致） | seq=16/18 的 ❌ 由**型号字段**触发（T02 parser 未提取 `HS-8A50J-12`），**与整机品牌路径无关**。T06-17 断言须**显式标注该归因**（见 8.1 注）。**新增待办**：T06 完成后开 T02 复修项 |
| **②** 是否追加架构文档 `13.14` | **同意**，由架构师写 | **同意，已执行** | 已在 `架构设计_报关申报要素自动校验工具_0916.md` 末尾追加 `13.14 真 OCR 误报缺陷裁决（A/B/C/D）`（正文 1–13.13 未动） |
| **③** `non_brand_tokens.yaml` 是否接受暂留硬编码降级 | **不接受**，必须外置 | **接受主理人意见，撤回原降级选项** | "规则解耦"是用户第 2 点硬约束，且缺陷 C 的直接对策。**裁决：`non_brand_tokens.yaml` 必须外置**，`_NON_BRAND_BARE_TOKENS` 仅作 YAML 缺失时的常量兜底（保留但**不作为权威载体**）。T06-07/T06-13 保持为**必做项** |

> **R2 三问答复均与主理人一致，无异议项。** 原第 3 节（缺陷 C 落位）中"若工期紧可暂留硬编码"的降级说明**已作废**，以本节为准。

---

## 12. R3 追加答复（主理人第 8 节驳回项的处理结果）

| # | 主理人驳回项 | **架构师 R3 处理** | 落地位置 |
|---|---|---|---|
| **1** | 整机 FAIL 层 seq 列表含错项（3/6/10/15） | **采纳**。复核 JSON 确认：seq=3/6/10 品牌 note =「低置信度 OCR，证据不足」→ ⚠️；seq=15 型号双侧一致、无整机触发 FAIL 的正当性 → 移出 FAIL 层 | 第 8.0 节错误 1 + 第 8.4 节五层断言 |
| **2** | seq=2 归因错列整机路径 | **采纳**。复核 JSON 确认：seq=2 note =「申报『』与图片『SANSUI』明确不一致」，**无"整机"字样**；FAIL 依 SOP 147 第 4 条 → 归**第 2 层**，断言不得含"整机" | 第 8.0 节错误 2 + 第 8.4 节第 2 层 |
| **3** | `_is_whole_machine_for_seq` 函数不存在 | **采纳**。`grep` 确认该函数不存在；**删除该断言**，改用真实 API `NoiseGuard.is_whole_machine_brand(text, brand_value=...)` + 该图真实 `text_raw` | 第 8.4 节附注 |
| **4** | 逐条复核 seq=16/17/18 归因 | **采纳**。① seq=16 `SKYHORTH`=已知 OCR 误读 → **⚠️**（非 ❌）；② seq=17/18 品牌侧**合格**（大小写差异），FAIL **仅来自型号**，型号值取自 `Customer model`/PCB `MODEL:`，**缺陷 A 修复后来源变化** → 预期**随之为 ✅/⚠️/❌**，**列为观测项（不作硬断言）** | 第 8.1 节注 + 第 8.4 节 + T06-17 观测项 |
| **5** | 重算 8.1 逐条表与四类分布 | **采纳**。seq=15 改「修复后不得 FAIL」；seq=3/6/10 改 ⚠️；❌ 期望**下调 `[8,10]`→`[5,7]`** | 第 8.1 / 8.2 / 8.3 节 |
| **6** | 架构文档 13.14.5 与裁决文档第 8 节逐字一致 | **已执行**。13.14.5 节同步重写（同一五层断言 + 同一期望区间） | `架构设计_0916.md::13.14.5` |

> **R3 结论**：第 8 节硬断言已按主理人实测证据全部重写为五层分层断言；3 处错误全部更正；❌ 期望区间由 `[8,10]` 修正为 `[5,7]`。**缺陷 A/B/C/D 与 A′ 的实体裁决（R2）不变**——本轮**仅修订回归验收规格**，不涉及任何口径或实现裁决的实体改动。范围仍限：裁决文档 + 架构文档 13.14.5，未动 `core/`/`rules/`。

> **⚠️ 附加提醒（给 T06，R4 强化）**：**回归期望值必须"实测后回填"**。R4 **已撤销一切推荐区间**——T06 修复后**实跑** 18 条，逐条对照 §8.2 依据表复核，**只看依据是否成立**，不追求数量吻合。任一依据不成立即视为缺陷回修；结论回报主理人交叉复核。

---

## 13. R4 追加答复（老板口径拍板项的落地）

| # | 老板/主理人裁决或发现 | **架构师 R4 处理** | 落地位置 |
|---|---|---|---|
| **1** | **裁决 1**：整机品牌 → ❌（沿 A.4 历史口径） | **采纳**。确认 R2 撤销正确；**seq=15 与 seq=12 同构 → 同判 ❌** | §8.0 演进链 + §8.4 第 1 层 |
| **2** | seq=15 归 L4 的 R3 结论作废 | **采纳**。**删除第 4 层断言**；seq=15 归**第 1 层**（`for seq in (9,11,12,13,14,15): == FAIL`） | §8.0 / §8.4 |
| **3** | L2「seq=2 不得含整机」与整图口径矛盾 | **采纳**。**删除该断言**（与缺陷 B 自相矛盾）；seq=2 仍判 FAIL，note 不禁止"整机" | §8.0 ④ / §8.4 第 2 层 |
| **4** | **裁决 2**：OCR 误读 → 自动纠正后比对 | **采纳，新增 §4.6**（规则定义 + 查找顺序 + 不虚高兼容性 + 代码位置）；原 R3「(d) 独立判噪」作废；seq=16 改判 ❌ | **§4.6** |
| **5** | **裁决 3**：工作台四类全开 | **记录**（维持现状，无需规格变更） | 本节 |
| **6** | **缺陷 E**：`_apply_blacklist` 误伤型号 | **采纳，新增 §5A**。裁定：`skip_prefixes` 改"捕获词前 N=8 字符窗口"；**型号字段 `skip_prefixes` 置空**（保留 `terms`）；引 SOP 陷阱 #5 原文（"捕获词前"） | **§5A** |
| **7** | **缺陷 F**：`is_known_noise_sample()` 失效 | **R4 初判 → R4b 更正**。R4 初判"粒度不匹配"经主理人深入实测**推翻**；**最终根因 = 兜底分支漏传字段**（`_resolve_noise_rules()` 漏传 `known_noise_samples` / `low_confidence_threshold` / `fragment_min_chars`）。裁定：兜底分支补传 3 字段 + **保证兜底 ≡ repo 路径**；**`ocr_prefix` 已实现，无需新增** | **§5B（含 5B.0 演进链）** |
| **8** | §4.2 把「≥2 图一致」限定裸 token 层 | **采纳，正式化**（① ② ③ `min_images=1`；④ `min_images=2`），使 T06 实现合法化 | **§4.2 规则 3** |
| **9** | 第 8 节不再给推荐区间 | **采纳，重写**。撤销 `[1,3]/[5,7]/[8,11]`；改**方法论（8.1）+ 逐条依据表（8.2）** | **§8.1 / §8.2** |
| **10** | 记录 seq=15 归属演进链 | **采纳**。如实记录 R2(→L1) → R3(→L4) → R4(→L1)，作为"规格随证据迭代"案例 | **§8.0 ②** |
| **11** | 架构文档 13.14.5 与裁决文档逐字一致 | **已执行**（同步重写） | `架构设计_0916.md::13.14.5` |

> **R4 结论**：老板三项裁决 + 2 个新缺陷全部落地为规格；第 8 节由"期望区间"改为"方法论 + 依据表"；`min_images` 分层正式化。**全部新增项属实现层**（SOP 判定口径逐字未动）。**缺陷 A/B/C/D 与 A′ 的实体裁决不变。** 范围仍限：裁决文档 + 架构文档 13.14.5，未动 `core/`/`rules/`/`tests/`。
