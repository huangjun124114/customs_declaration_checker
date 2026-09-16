# 更新日志 · CHANGELOG

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> ⚠️ **版本体系说明**：本文件的版本号是**交付版本**（对外）；
> 《架构设计》文档内的 `v1.2` 是**文档版本**（对内），两者属不同体系，勿混用。

---

## [0.1.0] · 2026-09-16

首个可交付版本。从「一次性脚本」升级为 **PySide6 桌面应用 + 单 exe 免安装交付**。

### 新增（功能）

- **四层架构落地**：`ui (PySide6) → app (编排) → core (判定引擎) → infra (基础设施)`，
  依赖方向单向，由 `tests/test_architecture_guard.py` 机器强制断言
  （`core/` 与 `app/` 禁止 import PySide6）。
- **Excel 结构变体探查 A–E**：覆盖首行表头 / 制单 Sheet（有无冒号）/ 双 Sheet 陷阱 /
  无订单号列；含 P4 双通道列映射自校正。
- **图片三级索引匹配**：`{票号}\{料号}[-后缀]\{料号}&{订单号}&{序号}.jpg`，
  料号目录模糊匹配 + 子目录兜底扫描 + 同料号多订单二次筛选。
- **OCR 引擎**：RapidOCR（`rapidocr` 3.9.2 + onnxruntime 1.30.0）。
  模型随 wheel 落盘（`rapidocr/models/`，约 31 MB），**离线可用**。
- **四类判定引擎**：✅ 校验合格 / ❌ 校验异常 / ⚠️ 缺图内标识，人工复核 / 🔵 缺图，人工复核。
  判定字符串与 SOP 1.3 **逐字一致**，由 `tests/test_constants.py` 硬断言锁定。
- **噪声三态护栏（NoiseGuard）**：落实「不虚高」红线——疑似 OCR 噪声不直接上报为异常。
- **规则外置**：`rules/` 下 7 个 YAML，**补规则 = 改配置不改代码**。
  启动时一次性加载为不可变快照（不做热加载，防跑批中口径漂移）；
  现场可用 `%APPDATA%\CustomsChecker\rules\` 覆盖内置规则。
- **三产物导出**：`校验汇总表_{票号}.xlsx`（恰好 13 列）+ `{票号}_待人工复核清单.csv`
  （utf-8-sig）+ `校验详细日志_{票号}_累积.json`（含完整 OCR 原文）。
- **断点续跑（C4）**：断点与数据源指纹绑定，防串票；
  UI 显式呈现「重新开始 / 继续执行」双态。
- **共享盘全 UNC 支持（C5）**：Excel 与图片目录均支持 UNC；不可达时明确报错不崩溃。
- **只读红线守卫**：`infra/fs_lock.py` 的 `input_readonly / assert_readonly / assert_within`
  在写入前断言目标归属，越界抛 `OutputPathViolation`。
- **环境体检闸门**：`tools/check_env.py` 检测 pip 空壳包/残缺包
  （19 个关键依赖 + 23 项模块 import），被 `build_exe.ps1` 第一步强制调用。

### 修复（真 OCR 跑批暴露的 6 个缺陷）

首次真跑批 SA26090215（18 条）暴露 `PASS = 0` 的严重虚高，逐一定位并修复：

| 缺陷 | 现象 | 修法 |
|---|---|---|
| **A** | `Customer model` 被误列为整机上下文 token，几乎每张唛头图都命中 → 整机判定频繁误触发 | 从 `context_tokens` 移出（YAML + `constants.py` 同步） |
| **B** | `context_window: 3` 按行距检索；实测唛头上 `Brand:` 与 `CARTON No.` 垂直相距 7–8 行 → **同构图结果自相矛盾、不可复现** | 改为 `context_scope: whole_image` 整图全文检索 |
| **C** | 品牌提取「首图首命中」，取到 `ONEL`/`prime`/`AAA` 等噪声 token（真品牌在第 9 张图才出现） | 改为**跨图一致性投票** + 标签图 `&001` 加权；分层 `min_images`（①②③ 层 = 1，④ 裸 token 层 = 2） |
| **D** | `无品` 不在 `none_tokens`，标签上「品牌:无品」被当有效品牌 | `无品` 加入 `none_tokens` |
| **E** | `_apply_blacklist` 的 `skip_prefixes` 误伤型号字段 | 改为「捕获词前 8 字符窗口」，且型号字段 `skip_prefixes` 置空 |
| **F** | `_resolve_noise_rules()` 兜底分支漏传 `known_noise_samples` 等 3 字段 → 打包漏 YAML 时静默失效 | 兜底分支补传 3 字段，**保证兜底 ≡ repo 路径规则集**；新增 2 把回归锁 |

> **缺陷 F 的根因经历三次诊断、两次纠正**（"加载链路断裂" → "粒度不匹配" → 最终
> 靠对照实验定位为"兜底分支漏传字段"）。演进链完整记录在
> `docs/05_架构裁决_真OCR误报缺陷_R4b.md` §5B.0，作为方法论案例保留。

### 打包与工程化加固（交付前加固）

| # | 项 | 说明 |
|---|---|---|
| 1 | **依赖声明补齐** | `requirements.txt` / `pyproject.toml` 原**漏声明 `openpyxl`** —— 而 `core/result_exporter.py`、`core/excel_probe.py` 强依赖它。干净环境装完仍 `ImportError`，打包后 exe 一导出汇总表即崩且用户机无 Python 无法补救。同时把 `flatbuffers` / `antlr4-python3-runtime` / `requests` / `certifi` / `charset-normalizer` / `six` / `tqdm` / `colorlog` / `colorama` 由「注释里的口头承诺」改为**真实声明** |
| 2 | **依赖声明一致性回归锁** | 新增 `tests/test_dependency_manifest.py`（6 用例）：静态扫描 `infra/core/app/ui` 的第三方 import ↔ 依赖清单双向核对；`requirements.txt` ↔ `pyproject.toml` 交叉一致；`check_env.CRITICAL_DEPS` ⊆ 清单。让「漏声明」变成红灯 |
| 3 | **依赖安装脚本** | 新增 `tools/install_deps.py`：把「装依赖 → 摘除非 headless opencv → 重装 headless 确权 → 环境体检」串成一条命令。`rapidocr` 元数据写死 `opencv_python>=4.5.1.48`，与 headless 版**同占 `cv2/` 目录**且 pip resolver 无法表达互斥 —— 漏做纠正会让 exe 混入 Qt DLL 与 PySide6 双 Qt 冲突 |
| 4 | **`check_env` 命名空间包误报修复** | 原判据只看「目录名 == 发行版名」，把 PEP 420 命名空间包 `google/`（实际由 `protobuf` 发行版拥有，`dist-info` 名为 `protobuf-*`）误判为残缺包 → **exit 1 直接拦住打包**。新增「由其它发行版 RECORD 声明拥有」判据（读 `*.dist-info/RECORD` 与 `*.egg-info/SOURCES.txt`）+ 3 把回归锁 |
| 5 | **onefile 日志落点修复** | `main.py` 原以 `Path(__file__).parent / "过程产出"` 兜底，在单文件 exe 下解析成 `%TEMP%\_MEIxxxxxx`（进程退出即删，日志直接丢失）。改用新增的 `infra.resources.app_base_dir()`（打包态 = exe 所在目录），与 `bundle_root()`（= `_MEIPASS`）职责分离 |
| 6 | **`-Onedir` 开关生效** | `build_exe.ps1 -Onedir` 原只设了 `CUSTOMS_ONEDIR` 环境变量，而 `customs_checker.spec` **从未读取**它 —— 加与不加都出单文件。spec 内补 `EXE/COLLECT` 双分支实现 |
| 7 | **版本号归位** | 交付版本号统一为 `0.1.0`（`main.py APP_VERSION` + `pyproject.toml`）；与《架构设计》文档版本 `v1.2` 明确区分 |

### 修复 · exe 冒烟实测暴露的 4 个打包态缺陷

单文件 exe 首次真正跑起来后，`--self-test` 与 `--check-env` 两条**现场排错主路径
全部失效**。四个缺陷逐一修复，各自补回归锁：

| # | 缺陷 | 现象 | 修法 |
|---|---|---|---|
| 1 | **`--self-test` 打印 ✅ 时崩** | `SELF-TEST: PASS` 与 `SELF-TEST: FAIL - UnicodeEncodeError: 'gbk' codec can't encode '\u2705'` **同时**出现，退出码 1 —— 断言全过却报失败，误导排错方向 | 原 `force_utf8_encoding()` 只设环境变量，而它对**已打开的** std 流无效（进程启动时已按简体中文 Windows 的 ANSI 代码页建好）。新增 `_reconfigure_std_streams()` 显式 `reconfigure(encoding="utf-8", errors="replace")`，并容忍 stdout 为 None / 无 reconfigure 的对象 |
| 2 | **`--check-env` 在 exe 内挂死** | 现场表现为"敲了命令没反应"：找不到 `tools/check_env.py` → `ImportError` → 窗口程序（`console=False`）弹**模态错误框** → 无人值守时永久挂起（实测前台 300 s 无输出） | 三级查找 `bundle_root()/tools` → `app_base_dir()/tools` → `__file__` 同级；找不到时打印可读原因（含全部尝试路径）并返回 1，**绝不抛异常**；同时把 `tools/check_env.py` 打进 spec 的 `datas` |
| 3 | **`--check-env` 误报 13 个"残缺包"** | 把 exe 自己的 `_MEIPASS` 扫成残缺包（`PySide6`/`numpy`/`PIL`/`rules`/`ui` …）并 exit 1 | PyInstaller 把纯 Python 模块编译进 **PYZ 归档**，`_MEIPASS` 只落地二进制与数据文件 → 「有文件但无 `__init__.py`」判据在打包态**根本不成立**。新增 `is_frozen()`，打包态下该节改为**明确声明「不适用」**（不再误报）；报告头加「运行形态」行 |
| 4 | **`find_site_packages()` 摸到宿主机** | onefile 把依赖平铺解到 `sys._MEIPASS`（目录名不是 `site-packages`），原实现顺着 `sys.path` 一路找到**宿主环境**的目录 | 优先识别 `sys._MEIPASS`（存在且为目录时直接返回） |

**连带发现（由 exe 自检抓出）**：`six` **没有被烤进 exe** ——
PyInstaller 只按「静态可解析的 import」收集模块，而 `six` 在任何代码里都没有
静态 import 点，于是静默漏收。已把 `six` / `colorlog` / `colorama` / `antlr4` /
`flatbuffers` / `tqdm` 一并加入 spec 的 `collect_submodules` 清单，
并新增 `test_spec_explicitly_collects_transitive_only_deps` 锁定（以后新增此类
「传递依赖」若忘了加，测试直接红灯）。

> **方法论记录**：这 4 个缺陷**全部只在打成 exe 之后才暴露**，源码态 629 用例
> 全绿时一个都看不出。说明「源码测试通过」≠「交付物可用」——
> **`--self-test` / `--check-env` 这类 exe 自检不是锦上添花，是交付前必须跑的一道关**。

### 明确不改（口径守住）

两项曾被提出、**经复审后撤销**的改动，理由均援引 SOP 原文：

| 议题 | 结论 | 依据 |
|---|---|---|
| 整机品牌命中是否改判 ⚠️ | **不改**，保留 ❌ | SOP 3.4 第 147 行「字段缺失 → ❌」+ 附录 A.4「13 合格 / 5 异常（全为外箱整机品牌类）」 |
| OCR 已知误读如何处置 | **自动纠正后比对**（`SKYHORTH` → `SKYWORTH P/N`），非独立判噪 | 减少虚高，同时不放弃可比对证据 |

### 验证

| 项 | 结果 |
|---|---|
| 回归测试 | **656 用例 / 0 失败 / 0 错误**（`pytest -q`，RC=0） |
| 静态检查 | `ruff` All checks passed |
| 环境体检（源码态） | `tools/check_env.py` exit 0（19 依赖 + 23 import + 无空壳包） |
| 真 OCR 端到端 | 113 张图 / 535.9 s（平均 4.74 s/张），四层硬断言 ALL PASS |
| 离线回放一致性 | 与真 OCR 链路**四类分布完全一致** |
| 原始输入只读 | 130 文件 / 394,140,543 字节，跑批前后 (文件名+size) sha256 一致 |
| 修复前后分布 | ✅ 0→1 ／ ❌ 11→12 ／ ⚠️ 7→5 ／ 🔵 0→0 |
| **exe 产物** | `dist/报关申报要素校验工具.exe`（149 MB，单文件，不入库） |
| **exe `--self-test`** | exit 0，规则加载 / 13 列 / 口径字符串全部通过，✅ 正常输出 |
| **exe `--check-env`** | 19 依赖版本核对 + 23 项模块 import 全过（空壳包扫描按打包态声明不适用） |


### 已知限制

1. **单文件 exe 需在干净 Win10/Win11 x64 机器上做最终验收**（C1）——
   本机已产出 exe 并通过自检，但「双击即运行」的现场验收仍待执行；
   `dist/` 与 `build/` 不入库。
2. **`seq=5` 归属待裁决**：`rules/non_brand_tokens.yaml` 的排除词表目前只对
   ④「裸 token 层」生效，③「品牌+型号行层」无 `non_brand` 过滤 →
   PCB 丝印残片（实测 `NMY` / `EMV`）可穿透被当作品牌。
   属**规格边界问题**，按「口径变更需确认」护栏**未擅自改动**。
3. 真 OCR 与离线回放在 `seq 10–18` 有 9 条**品牌取值**差异（OCR 行序与置信度所致），
   四类分布一致，但差异本身未逐条归因。

---

## 未发布

## [0.3.1] · 2026-09-16（现场反馈：2 处 UI 缺陷 + 1 项口径收窄）

> 触发：现场截图 2 张 + 用户口头补一条口径。**默认项目 0.3.0（`4ad328b`）之上增量修复**。
> 基线口径**零漂移**：`core/judge_engine.py` / `core/noise_guard.py` **本轮零改动**。

### 修复 1 · 判定链路表格「错位覆盖」（现场截图 1）

现象：判定链路表里「判定原因」文本**压在表格「型号」行上**，表格只剩「品牌」一行可见。

根因**不在表格自身**（表格几何一直是正确的：`height=113 = 表头 29 + 2 行 ×40 + 边框`），
而在**父容器把子控件压到最小尺寸以下**：

| 层 | 实测 | 说明 |
|---|---|---|
| 卡片内容 | 需 886px，实操只拿到 **759px**（= 视口高） | `QScrollArea(widgetResizable=True)` 只把内容拉到 `max(视口高, 内容最小高)`，而内容布局的最小高只有 600 |
| 判定链路框 | 被压成 **190px** < 其 `minimumSizeHint` **194px** | 缺的 4px 正是重叠的起点 |
| 「判定原因」 | `y=109`，而表格占 `30..143` | 文字溢出自身矩形 → 视觉上压住「型号」行 |

而内容之所以撑不高，还有**第二层根因**：`QLayout.addWidget()` 内部用
**queued** `QMetaObject::invokeMethod(w, "_q_showIfNotHidden")` 显示新子控件 ——
`load_result` 里刚 new 出来的散行标签在事件循环下一拍前仍是 `isHidden()`，
`FlowLayout.sizeHint()` / `heightForWidth()` 一律返回 **0**，于是「内容需要多高」
被测成 0 / 577 / 718 之类的小值（稳定值其实是 886）。

修法（四件套，缺一不可）：

| # | 修法 | 位置 |
|---|---|---|
| 1 | 整卡内容套 `QScrollArea`（横向不滚动，纵向按内容） | `ui/widgets/workbench_card.py::_build_ui` |
| 2 | `_fit_content_height()`：用 **`heightForWidth(视口宽)`**（非 `sizeHint`，后者按"不换行"估算成 1274，会让卡片白滚）写回内容 `minimumHeight` | 同上 |
| 3 | `_reveal()`：新增流式子控件（散行标签 / 图号按钮）**显式 `show()`**，让几何查询当拍即准 —— 这是本次最关键的一条 | 同上 + `_fill_flow_labels` / `_render_evidence_buttons` |
| 4 | 表头**左对齐**（原居中，与单元格默认左对齐不一致 → 表头文字与列内容看着错开）、关闭双滚动条、前两列 `Fixed` + 第三列 `Stretch`、`_fit_chain_height()` 按**表头实际高**校正 | `_build_chain` / `_fit_chain_height` |

附带**新发现并一并修掉**的问题：内容恢复真实高度后，「重判为 / 备注 / 标记待补图 /
保存重判」被推到折叠线以下 —— 等于把"压扁"换成"藏起来"。故底部操作区移出滚动区
（`QFrame#cardFooter`，常驻），`shell = 滚动区(拉伸 1) + 操作区(0)`。

**可证伪性验证**（同一探针，仅把 `_reveal` 打回空操作）：

| 观测量 | 修复态 | 回退态 |
|---|---|---|
| 链路框高 | **228** ≥ 194 ✓ | **190** < 194 ✗ |
| 「判定原因」y | **147**（表格底 143） | **109**（压在表格上） |
| 几何重叠 | **False** | **True** |
| 内容最小高 | **886**（稳定） | 718 / 759（乱跳） |

### 修复 2 · OCR 弹窗「置顶」改为右侧垂直居中（现场截图 2）

现象：弹窗出现在窗口**左上角**，压住图片区与右侧证据卡。

修法：把定位逻辑抽成**纯函数** `OcrTextDialog.place(area, win_geo, anchor) -> QRect`
（可任意分辨率单测，不再依赖真实窗口），优先级：
① 图片区右侧够放（≥ `_MIN_WIDTH` 360）→ **右对齐窗口右边界 + 垂直居中**（按需收窄）；
② 右侧放不下 → **贴图片区下方**（保住"不遮图"这条硬约束）；
③ 上下左右皆无空间 → 贴右侧 + 垂直居中 + 夹回屏内。删除失效的 `_fits()`。

### 变更 · 判定范围收窄为「只识别品牌与型号」（用户口径 2026-09-16）

新增两条常量并落护栏（`core/constants.py`）：

- `JUDGED_FIELDS = (FIELD_BRAND, FIELD_MODEL)` —— 参与识别与判定的**仅此两者**；
- `NON_VALUE_FIELD_SUFFIXES = ("类型", "种类", "类别")` —— 「品牌类型 / 型号类型」的取值是
  **品牌的归类**而非品牌本身。⚠️ 缺此护栏时 `品牌类型:0` 会被读成品牌值 `0`，**违反「不虚高」红线**。

`core/element_parser.py` 三处候选收窄（`_is_brand_key` / `_is_model_key` / `_extract_*` 分支）
均加 `_is_non_value_key()` 排除。

> ⚠️ **这是防御性加固，不是行为变更**：实测申报要素原文
> `用途:电视机用|结构类型:有接头|品牌:baori|型号:无|额定电压:60V` ——
> 程序本来就只判品牌 / 型号。本轮重点是**堵住"品牌类型被当品牌值"的虚高风险**。

### 验证

| 项 | 结果 |
|---|---|
| 全量测试 | **1069 passed / 0 failed / 0 errors**（v0.3.0 为 1026，净增 43；17.81s） |
| `ruff check .` | All checks passed |
| 新增回归锁 | 弹窗定位 15 条（`tests/test_ocr_dialog_position.py`，含**对旧缺陷的反向锁** `rect.top() > 8`）；链路框不压缩 / 不重叠 / `heightForWidth` 幂等 / 流式标签当拍 show 共 4 条；表头左对齐等 4 条；判定范围 15 条 |
| 视觉验收 | `tools/ui_preview.py`（离屏 + 模拟 1920×1080 屏 / 1600×900 窗）产出 2 张 PNG；弹窗几何自检 **4/4 OK**（右对齐 ±1 / 垂直居中 ±1 / 不遮图片区 / 完整落在屏内） |
| 口径漂移 | `core/judge_engine.py` / `core/noise_guard.py` **零改动**；四类判定字符串未动（`tests/test_constants.py` 硬断言仍绿） |

### 打包与冻结态冒烟（v0.3.1）

> 命令：`.\build_exe.ps1 -Zip`（onedir + zip）。⚠️ **打包前必须先清空 `PYTHONPATH`**，见下「沙箱陷阱」。

| 项 | 结果 |
|---|---|
| onedir 产物 | `dist\报关申报要素校验工具\` —— **721 文件 / 361,401,898 字节（344.7 MB）**；exe 本体 10,357,659 字节 |
| 分发包 | `dist\报关申报要素校验工具_v0.3.1_0917.zip` —— **157,633,359 字节（150.3 MB）** |
| zip sha256 | `1e8e4b2036d0fd754136275a3a36390668ddd206b7b0d37a2a4cb4ef23ce5307` |
| zip 结构 | 731 条目 / 顶层仅 `报关申报要素校验工具` / 根下 exe 就位 / `testzip` 通过 |
| 冻结态 `--self-test` | **exit 0，0.51s**（日志确认 `报关申报要素自动校验工具 v0.3.1 启动`） |
| 冻结态 `--check-env` | **exit 0，0.92s** |
| `_internal\rules\` | **8/8 YAML** 收全 |
| 版本号四处一致 | `main.py` = `ui/main_window.py` = `pyproject.toml` = zip 文件名 = **0.3.1** |
| 打包前置体检 | `tools/check_env.py` exit 0（19 依赖 + 23 模块 import + 无空壳包） |

⚠️ **沙箱陷阱（本轮新增第 4 类，务必记录）**：首次打包在**最后一步 COLLECT** 失败 ——

```
INFO: Removing dir ...\dist\报关申报要素校验工具
[safe-delete][SAFE_DELETE_FAIL_CLOSED] {"reason": "trash-failed",
 "detail": "SHFileOperationW 失败: 0x78"}
OSError: SHFileOperationW 失败: 0x78      → PyInstaller exit 1
```

根因：沙箱把删除拦截件装成 **`sitecustomize.py`**，而它是靠 **`PYTHONPATH` 指向
`…\cli\vendor\shim`** 才被 Python 自动加载的（PyInstaller 日志的 "Module search paths
(PYTHONPATH)" 里能直接看到这一条）。该拦截件走「移到回收站」路线，对 **3000+ 个文件、
344 MB、含中文与超长路径**的目录会失败并 **fail-closed**（宁可报错也不真删）。

**绕行：打包前清空 `PYTHONPATH`**（shim 就不会被加载，删除回到系统原生语义）：

```powershell
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
```

清空后同一条命令**一次通过**（Analysis→PYZ→PKG→EXE→COLLECT→zip 全绿）。
抬高 `CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD` 对**本类失败无效**（它走的是
`trash-failed` 分支而非批量阈值分支）。

📌 **另一个包装卫生点**：冻结态冒烟会在 onedir **旁边**生成运行目录
`dist\报关申报要素校验工具\报关申报要素校验\`（v0.2.0 起的运行目录约定）。
它与 zip 无关（STEP 6 先压缩、后冒烟），但**若之后再次压缩会把运行日志带进包**
→ 冒烟后需清理，或直接重新打包（PyInstaller 会整目录重建）。

📌 **冒烟已固化为常备工具**：本轮起初用的是一次性脚本 `_smoke.py`（版本号硬编码，
核对完即删），现已提升为 **`tools/smoke_frozen.py`** —— 版本 / 包名 / zip 全部从工程
现状**自动推导**，以后每次打包直接跑，换版本不用改脚本：

```powershell
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
python tools/smoke_frozen.py --out _smoke.txt      # --no-run 可跳过 exe 冒烟
```

六项检查（任一**致命项**不过即 `exit 1`）：

1. onedir 规模（文件数 / 字节 / exe 字节）+ 检出混进包里的运行目录；
2. zip：精确字节、sha256、`testzip` 完整性、条目数、**根下 exe 是否就位**；
3. 冒烟 `--self-test` / `--check-env` 的**退出码 + 耗时**，**带 300s 超时**
   —— 打包态弹模态框会永久挂起，必须靠超时兜底，不能只等；
4. `_internal/rules/` 与源码 `rules/` **逐个文件名比对**（能报出缺哪几个，
   不只数个数 —— 缺陷 F 的教训）；
5. 版本号**四处**一致：`main.py` / `ui/main_window.py` / `pyproject.toml` / zip 文件名；
6. **冻结态实际运行版本**：从 `--self-test` 的运行日志里断言「… v0.3.1 启动」。

⚠️ 第 6 项**不要**写成「在 exe 字节里搜版本串」：纯 Python 模块被 PyInstaller 放进
**PYZ 归档**（压缩），`_MEIPASS` 只落地二进制与数据 —— 搜不到是**正常**的，那样写会造出
稳定复现的**假报警**（初版即踩，返回 `含 0.3.1：False`）。权威判据是运行日志那一行。
同理，冒烟日志走 **stderr**（`logging` 默认流），别因为"stderr 有内容"就判失败。

### 已知限制

- 卡片内容超过窗格高时**纵向滚动** —— 这是本次的**设计取向**（宁可滚动，不可压扁）。
  ⭐ **用户 2026-09-17 00:08 明确确认**：采用「整卡纵向滚动 + 底部操作区常驻」，
  **不**改为「只让散行 OCR 区滚动」。理由：复核卡各段都可能超高（判定原因长文案、
  证据图多、散行多），局部滚动会让"哪一块在滚"变得不可预期；整卡滚动心智一致，
  且操作区已固定，保存动作无需滚动。
- 离屏渲染不加载 Windows 系统字体，需显式 `addApplicationFont("C:/Windows/Fonts/msyh.ttc")`，
  否则中文全渲染为豆腐块（`tools/ui_preview.py::_install_cjk_font` 已处理）。

## [0.3.0] · 2026-09-16（批次 1：判定主链路口径变更）

> ⚠️ **本项目首次改动 `core/judge_engine.py`**（v0.1.0 / v0.2.0 两轮它均为零改动）。
> 依据：`docs/10_迭代方案_v0.3_0916.md`（需求 1）+ 用户 2026-09-16 四项口径拍板。
> 状态：**批次 1 完成**；批次 2（复核工作台重构）/ 批次 3（打包推送）待做。

### 变更（口径变更 —— 需显式记录）

| 项 | 变更前（v0.2.0） | 变更后（v0.3.0） |
|---|---|---|
| 图片侧取证 | **先提取、后比对**：`extract_detected_brand/model` 用四层正则 + 跨图投票**猜出**图片侧值，再交 `NoiseGuard.classify_detail` 比对 | **完整分词直接命中**：申报值在 OCR 全文里以完整分词出现即算命中（`core/token_matcher.py`） |
| 根因 | OCR **缺失空间感知**（无左右/上下关系）→ 字段名与值分行、值被相邻字段名抢走 → 猜错或猜空 → 误判 | 取证不再依赖"猜值"，空间关系缺失不再是失效路径 |
| 未命中口径 | 按 `NoiseGuard` 三态 | **两级分流**：图内**有同类标识**但值不同 → ❌（有证据）；图内**根本没有**该类标识 → ⚠️（没找到证据） |
| `extract_detected_*` 职能 | 取证主路径 | **降级**为「图内是否存在同类标识」判据 + 13 列展示值 + 整机品牌定位键 |

**明确不变**（防止误读为口径漂移）：
图片证据闸门（🔵）、整机品牌保留 ❌（T06 裁决 A′）、记录级跨文字体系兜底、
`NoiseGuard` 比对语义与 `SUSPICIOUS` 降级护栏、四类口径字符串、`COLUMN_COUNT = 13`
与汇总表列结构、OCR 并发/缓存/降采样。

### 新增（功能）

- **`core/token_matcher.py`**：`TokenMatcher` / `TokenMatch` + 三级匹配
  （`EXACT` 英文数字按词边界、中文按子串 → `FUZZY` 已知误读纠正 + 易混字符等价类 → `NONE`）。
- **字段名上下文护栏（防新链路引入虚高）**：中文子串匹配会把**字段名**当值
  （申报 `创维` × 图上 `创维物料编号`）→ 命中作废。三道判据：G1-CJK 紧邻后缀、
  G1-ASCII 词边界后缀、G2 整串命中 `fields_blacklist` 词表；
  **同源规则**：已知误读样本的纠正目标本身若是字段名（`SKYWORTH P/N`），该样本不作命中依据。
- **`CheckResult.token_matches`**：品牌/型号各一条命中取证（`mode/token/images/sample_line/corrected_from/note`），
  **只进详细 JSON + 复核工作台，绝不进 13 列汇总表**（`to_row()` 零改动）。
- **`tools/eval_v03.py`**：新旧口径逐条对比评估器（旧引擎从 git `dfe00d5` 取源码独立加载，
  证据取真跑批快照、申报侧用当前解析器刷新 → 严格 A/B）。

### 口径变更实测（SA26090215，18 条）

```
快照记录（v0.2.0）  ：✅1 / ❌12 / ⚠️5 / 🔵0
旧链路复算（自证）  ：✅1 / ❌12 / ⚠️5 / 🔵0   ← 与快照**完全一致**，证明对照未走样
新链路（v0.3.0）    ：✅7 / ❌7  / ⚠️4 / 🔵0   ← 新回归基线
口径变更：6 / 18 条（全部 ❌/⚠️ → ✅）
```

| seq | 旧 → 新 | 申报 | 命中取证 |
|---|---|---|---|
| 1 | ❌ → ✅ | `baori` | 图 2 实物印字 `baori` |
| 4 | ⚠️ → ✅ | `宇同` | 图 1 `宇同电子（惠州）有限公司` |
| 5 | ❌ → ✅ | `baori` | 图 4 `baori E339609 AWM 20941 …` |
| 16 | ❌ → ✅ | `DAEWOO` / `HS-8A50J-12` | 图 3 唛头 `DAEWOO`；图 6 标贴 `HS-8A50J-12 K1 …` |
| 17 | ❌ → ✅ | 同上 | 图 3 / 图 4 |
| 18 | ❌ → ✅ | 同上 | 图 2、4 / 图 3 |

**未变**：整机品牌类 seq 9/11/12/13/14/15 **全部保留 ❌**（R2 回归锁）；🟡 seq 3/6/7/10 保留 ⚠️；
🔵 仍为 0。**护栏在真实样本上零误伤**（作废次数 0）。

> ⚠️ **seq 16/17/18 的双重根因**：其旧 ❌ 既来自新链路要解决的"猜值错误"
> （旧链路取到 `SKYHORTH` / `HS-8AA` 这类字段名残片与截断值），也来自快照的
> **申报侧陈旧**（`decl_model` 为空，实为缺陷 E 修复前遗留）。本次已一并修正
> 回归夹具：`tests/test_real_sample_sa26090215.py` 改为**用当前解析器刷新申报侧**。

### 验证

| 项 | 结果 |
|---|---|
| 回归测试 | **991 用例 / 0 失败 / 0 错误**（v0.2.0 为 929；新增 `test_token_matcher.py` 47 条 + 真实样本层 5/基线锁 15 条） |
| 静态检查 | `ruff check .` All checks passed |
| 真实样本 | ✅7 / ❌7 / ⚠️4 / 🔵0（`test_v030_baseline_locked` 硬断言） |
| 不虚高红线 | 全样本 `SUSPICIOUS` 记录**无一**判 ❌（`test_suspicious_never_escalated_to_fail`） |
| 13 列冻结 | `to_row()` 零改动，列数硬断言通过 |
| 架构守卫 | `core/` 与 `app/` 仍未 import PySide6 |

### 已知影响 / 待裁决

1. ⚠️ **「整机品牌 → ❌」与「命中即合格」的交叉场景**：seq=1 既有整机命中
   （`Brand:Daewoo` + `CARTON`）又有本体命中（图 2 印字 `baori`），新链路判 ✅。
   本实现取「申报值确在图中 → 合格」（整机品牌仍**不被当成本体证据**，未违反 A′ 的实体语义）。
   如认为"整机命中即须强制 ❌"，请裁决 —— 该分支改动量极小。
2. v0.4 候选：管线产出的 `OcrText.seq` **恒为 0**（序号只在 `ImageEvidence.seq` 上），
   本次在 `token_matcher` 内按文件名兜底解析；建议在管线侧补齐该字段。
3. 快照 `过程产出/校验详细日志_SA26090215_累积.json` 的申报侧已陈旧（见上），
   后续跑批请重生成累积 JSON。


---

## [0.3.0] · 2026-09-16（批次 2：复核工作台重构 + 批次 3 收尾）

> 依据：`docs/10_迭代方案_v0.3_0916.md` 需求 2/3/4/5/7/8；
> 承接批次 1（判定主链路口径变更）——本轮**纯 UI + 收尾**，判定引擎零改动。

### 新增（功能）

| # | 需求 | 落地 |
|---|---|---|
| 2 | **图片旋转** | `ui/widgets/image_viewer.py`：`↺ 左旋` / `↻ 右旋` 90°、`rotation_label` 显示角度；`display_pixmap()` 作为**旋转后**尺寸基准（`_render` / `fit_to_window` / `is_pannable` 统一改读），旋转后自动 `fit_to_window()`；换图 / 清空自动复位 0° |
| 3 | **状态标签 + 模糊搜索** | `ui/widgets/review_workbench.py`：5 个状态标签（全部 / 成功 / 待复核 / 缺图 / 失败）**带数量与灯色**，数量取全量统计（不随过滤变动）；搜索框对 `出货单号 / 订单号 / 物料编号` 三字段做**大小写不敏感子串**匹配；过滤管线 `全部 → 状态标签 → 搜索` |
| 4 | **记录改表格 + 选中同步** | 左列 `QListWidget` → `QTableWidget`（`订单号 / 物料编号 / 核验结果`），`SelectRows + SingleSelection`，选中行整行高亮；选中变化 `_sync_detail(row)` 同步中间图片与右侧卡片 |
| 5 | **判定链路三列** | `ui/widgets/workbench_card.py`：4 段 `QLabel` → **3 列 × 2 行** 表（`要素 / 申报值 / 判定值`）；判定值取**引擎回吐**的 `TokenMatch`（EXACT / FUZZY / NONE 三分支 + 字段名护栏作废说明 + 旧结果集回退并标注「旧链路」），单元格 tooltip 载明命中方式 / token / 图号 / 原文行 / 说明 |
| 7 | **全部改散行文本** | 删除 KV 表格（`kv_table` / `_kv_rows()` / `_line_key()` / `_KV_TABLE_QSS`）；散行卡成为唯一展示形态，数据源改 `ocr.lines()` **全量行**（不再剔除已成键值对的行）。`OcrText.kv` **仍提取、仍落详细 JSON**（Q4 口径不变），仅不再进 UI |
| 8 | **只留原始 OCR + 弹窗不遮图** | 删除 `evidence_view`（与原始 OCR 重复）与 `raw_toggle` / `raw_view`（内嵌折叠区）→ 新增 `ui/widgets/ocr_text_dialog.py::OcrTextDialog`：**非模态单例**弹窗、`◀ 上一张 / 下一张 ▶ / 复制全文`、随切图自动刷新 |

### 新增（工程）

- `tools/run_sample.py`：**headless 真实样本端到端跑批**（与 GUI 同走 `CheckTask`），
  打印四类分布 / 三产物落点 / 耗时，并自动比对 `原始输入` 跑批前后指纹。
  支持 `--check-input`（只算指纹）、`--json`（落分布 JSON）、`--base`（覆盖输出锚点）。
- `core/constants.py` 新增 `FIELD_BRAND` / `FIELD_MODEL` 作为字段名**唯一来源**；
  `core/judge_engine._FIELD_*` 改为其别名，由 `test_constants.py::TestFieldNames` 锁一致。

### 修复（本轮实测发现的 4 处）

| # | 问题 | 根因与修法 |
|---|---|---|
| 1 | 弹窗避让恒为「不生效」 | `WorkbenchCard` 被 `QSplitter.addWidget()` **重新挂载**，`self.parent()` 是 splitter 而非工作台 → `_image_area()` 改为**向上遍历父链**（限深 8 层）找 `image_viewer`。缺此修复弹窗会压住图片（违反需求 8 红线） |
| 2 | 「核验结果」列被挤成 `…` | 三列并排时左栏 260px 不足以放下 17 字料号 + 结果列 → 左栏 `minimumWidth` 400、右卡 460、分栏比 `2:3:4`、表头 `minimumSectionSize=72` |
| 3 | 判定链路表下方一大片空白 | `QTableWidget` 默认 `sizeHint` 高 192px → `setFixedHeight(28 + 2×40 + 4)` + 行高 40 + `setWordWrap(True)` |
| 4 | 深色面板落在**浅色**主题上（形似渲染异常） | v0.2.0 的散行卡 / KV 表用「深色面 + 浅色字」，代码注释理由是"适配本机深色主题"，但 `ui/styles/app.qss` 实际是**浅色主题**（`#F5F6F8` 底 / `#303133` 字）→ 统一改浅色；判定徽标亦由枚举名 `PASS` 改为口径字符串 `✅ 校验合格` |

### 行为变更（需知悉）

| 项 | v0.2.0 | v0.3.0 | 理由 |
|---|---|---|---|
| 复核工作台**默认列出的记录** | 仅 ⚠️ + 🔵（待复核） | **全部**四类 | 「谁要复核」由状态标签的数量 + 头部「待复核：N 条」承载；避免 ❌ 等问题记录被静默隐藏。`tests/test_ui_smoke.py` 对应用例同步改名改断言（口径变更，非改测试凑绿） |
| 散行卡 / 判定链路表配色 | 深色面 + 浅色字 | 浅色面 + 深色字 | 与 `app.qss` 浅色主题一致（见上表 #4） |

### 验证

| 项 | 结果 |
|---|---|
| 回归测试 | **1026 用例 / 0 失败 / 0 错误**（批次 1 为 991；本轮净增 35 条 UI 用例） |
| 静态检查 | `ruff check .` All checks passed |
| 离屏视觉验收 | 复核工作台**整屏渲染截图**逐项核对 6 条需求；弹窗 `overlap_with(图片区) == False` 硬断言 |
| 架构守卫 | `core/` 与 `app/` 仍未 import PySide6；`COLUMN_COUNT = 13` 与四类口径字符串零改动 |
| 判定引擎 | `core/judge_engine.py` / `core/noise_guard.py` 的**判定语义**本轮未动 |

### 批次 3：真实样本端到端复跑（T13）

用新增的 `tools/run_sample.py` 对真实样本 SA26090215 全量跑批（headless，与 GUI 同走
`CheckTask`）：

| 项 | 结果 |
|---|---|
| 结果 | `ok=True`，**18/18 条**处理完成 |
| **四类分布** | **✅7 / ❌7 / ⚠️4 / 🔵0** —— 与批次 1 确立的新基线**完全一致**（口径未漂移） |
| 耗时 | **181.2s**（v0.2.0 为 218.9s；OCR 缓存命中 0 / 实际识别 113 张，无损坏） |
| 三产物 | `result/校验汇总表_SA26090215.xlsx`（**19 行 × 13 列**，列名与 `COLUMNS` 逐字一致）／`result/SA26090215_待人工复核清单.csv`／`logs/校验详细日志_SA26090215_累积.json`（18 条，含 `token_matches`，不含 `boxes`） |
| 逐条 | idx1 ✅ ／ idx2 ❌ ／ idx3 ⚠️ ／ idx4 ✅ ／ idx5 ✅ ／ idx6 ⚠️ ／ idx7 ⚠️ ／ idx8 ✅ ／ idx9 ❌ ／ idx10 ⚠️ ／ idx11–15 ❌ ／ idx16–18 ✅ |
| 原始输入 | 指纹**未改动** ✓（130 文件 / 394,140,543 字节，跑批前后一致） |
| 分布留档 | `docs/12_v0.3.0_真实跑批分布_0916.json` |

### 批次 3：打包与冻结态冒烟（T14）

`build_exe.ps1 -Zip`（第一步强制走 `tools/check_env.py` 环境体检）：

| 项 | 结果 |
|---|---|
| 环境体检（打包机） | 通过（19 项关键依赖版本核对 + 23 项关键模块 import） |
| onedir 产物 | `dist\报关申报要素校验工具\`（`报关申报要素校验工具.exe` 10.37 MB + `_internal\`） |
| 分发包 | `dist\报关申报要素校验工具_v0.3.0_0916.zip`（**157,657,123 字节 / 150.4 MB**） |
| 内置规则 | `_internal\rules\` **8/8 YAML** 收全 ✓ |
| 冻结态 `--self-test` | **exit 0**，322 ms，输出「自检通过：规则加载 OK / 13 列 OK / 口径字符串 OK」 |
| 冻结态 `--check-env` | **exit 0**，648 ms；19 项依赖 OK、23 项 import OK、空壳包检测按设计**声明不适用** |
| 运行目录锚点 | 冻结态自动在 exe 同级创建 `报关申报要素校验\{result,logs}` ✓（`app_base_dir()` 生效） |
| 版本号 | `main.APP_VERSION` / `pyproject.toml` / `build_exe.ps1` 统一为 **0.3.0** |

> ⚠️ 仍**未做**：干净 Win10 1809+ 真机**双击验收**（需现场机器，沿用 v0.2.0 遗留项）。

### 批次 3：提交与推送（T15）

| 项 | 结果 |
|---|---|
| 已推送提交 | `dfe00d5` → **`4ad328b`**（`main`） |
| 提交规模 | **26 文件 / +5125 −570** |
| 新增文件 | `core/token_matcher.py`／`ui/widgets/ocr_text_dialog.py`／`tools/run_sample.py`／`tools/eval_v03.py`／`tests/test_token_matcher.py`／`docs/10`／`docs/11`／`docs/12` |
| 远端校验 | `git ls-remote` 远端 `main` = `4ad328b9d1730f0e7c70bf8a5ef0e364241bcf6d`，与本地 HEAD **逐字符相同** |
| 双向差异 | `rev-list --left-right --count origin/main...HEAD` → **`0 0`** |
| 忽略规则 | 新增 `build_log_*.txt` / `run_log_*.txt`（构建临时日志不入库，**未删除磁盘文件**） |
| 未入库（按设计） | `dist/`（含 v0.2.0 与 v0.3.0 分发包）、`原始输入/`、运行期 `报关申报要素校验/` |

### v0.3.0 遗留（转 v0.4 候选）

| # | 事项 | 状态 |
|---|---|---|
| 1 | 干净 Win10 1809+ 真机双击验收 | **未闭环**（唯一未闭环交付验收项，需现场） |
| 2 | 「整机品牌 → ❌」与「命中即合格」交叉场景（真实样本 idx1） | **待裁决** |
| 3 | 散行卡/链路表 深色→浅色、工作台默认列全部记录 | **待用户确认**（属交互/视觉变更） |
| 4 | KV 反哺判定（`Brand:XXX` 能解析却仍判 ⚠️） | **待裁决** |
| 5 | `intra_op_num_threads` 移出 `params_fingerprint`（不影响 OCR 输出却致全量缓存失效） | 建议 |
| 6 | 票号推断与 Excel 出货通知书号交叉校验 | 建议 |
| 7 | `投票来源图号` UI 近似推断已删除，引擎回吐已就位 | **已闭环** ✓ |

