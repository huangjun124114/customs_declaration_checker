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

### 明确不改（口径守住）

两项曾被提出、**经复审后撤销**的改动，理由均援引 SOP 原文：

| 议题 | 结论 | 依据 |
|---|---|---|
| 整机品牌命中是否改判 ⚠️ | **不改**，保留 ❌ | SOP 3.4 第 147 行「字段缺失 → ❌」+ 附录 A.4「13 合格 / 5 异常（全为外箱整机品牌类）」 |
| OCR 已知误读如何处置 | **自动纠正后比对**（`SKYHORTH` → `SKYWORTH P/N`），非独立判噪 | 减少虚高，同时不放弃可比对证据 |

### 验证

| 项 | 结果 |
|---|---|
| 回归测试 | **629 用例 / 0 失败 / 0 错误**（`pytest -q`，RC=0） |
| 静态检查 | `ruff` All checks passed |
| 环境体检 | `tools/check_env.py` exit 0（19 依赖 + 23 import + 无空壳包） |
| 真 OCR 端到端 | 113 张图 / 535.9 s（平均 4.74 s/张），四层硬断言 ALL PASS |
| 离线回放一致性 | 与真 OCR 链路**四类分布完全一致** |
| 原始输入只读 | 130 文件 / 394,140,543 字节，跑批前后 (文件名+size) sha256 一致 |
| 修复前后分布 | ✅ 0→1 ／ ❌ 11→12 ／ ⚠️ 7→5 ／ 🔵 0→0 |

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

暂无。
