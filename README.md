# 报关申报要素自动校验工具

> **交付版本：v0.3.0**（2026-09-16）
>
> 源代码仓库：<https://github.com/huangjun124114/customs_declaration_checker>
> （exe / zip 为可重建产物，**不入库**）
>
> 对**报关申报要素（品牌 / 型号）**与**产品实拍标签**做自动化交叉校验，
> 输出带完整证据链的《校验汇总表》与《待人工复核清单》。
>
> 口径权威来源：《报关申报要素自动校验 SOP v2.0》
> 架构设计：v1.2（v1.0 基线 + v1.1 用户 5 点硬约束 + v1.2 环境实测）
>
> ⚠️ **交付版本号（v0.3.0）与文档版本号（架构设计 v1.2）属不同体系**，勿混用。

---

## 〇、文档索引

| 文档 | 说明 |
|---|---|
| `CHANGELOG.md` | 版本变更记录（v0.1.0 六个真 OCR 误报缺陷 / v0.3.0 判定口径变更 + 复核工作台重构） |
| `docs/01_SOP_报关申报要素自动校验_v2.0.md` | **口径权威来源**（四类判定逐字依据） |
| `docs/02_使用说明_v2.0.md` | 面向使用者：怎么用、要准备什么、14 类异常处置 |
| `docs/03_PRD_v1.0.md` | 产品需求 |
| `docs/04_架构设计_v1.2.md` | 架构设计（四层分层、规则外置、打包规格） |
| `docs/05_架构裁决_真OCR误报缺陷_R4b.md` | 真 OCR 误报缺陷的**架构裁决与实施规格**（R2→R4b 四轮修订） |
| `docs/06_执行约束清单_0916.md` | 用户 5 点硬约束（C1–C5）与验收依据 |
| `docs/07_T06逐条判定依据表.md` | 真实样本 18 条逐条 verdict + SOP 依据（**v0.3.0 前的历史证据**） |
| `docs/08_交付说明_v0.1.md` | v0.1.0 交付说明（历史） |
| `docs/09_验收问题修复方案_v0.2_0916.md` | v0.2.0 现场 10 点验收问题的修复方案 |
| `docs/10_迭代方案_v0.3_0916.md` | **v0.3.0 迭代方案 + 实施记录**（判定口径变更 + 复核工作台重构） |
| `docs/11_v0.3.0_新旧口径对比实测_0916.json` | 新旧口径逐条对比（评估器 `tools/eval_v03.py` 产出） |
| `docs/12_v0.3.0_真实跑批分布_0916.json` | v0.3.0 真实样本端到端跑批分布（`tools/run_sample.py` 产出） |

---

## 一、快速开始

### 1.1 运行环境

| 项 | 要求 |
|---|---|
| 操作系统 | **Windows 10 1809+（LTSC 2019+）及以上，含 Windows 11** |
| Python（开发态） | **3.11+（推荐 3.13）** |
| 交付形态 | 单个 `.exe`，双击即运行，**用户机器无需安装 Python** |
| 共享盘 | 需域账号**读权限** |

> Qt 6.2 起官方最低要求 Windows 10 版本 1809（Build 17763）。本工具仅使用
> `pathlib` / `os.scandir` / `QFileDialog` / `QThread`，**无任何 Win11 专属 API**。

### 1.2 开发态安装

**推荐：一条命令走完（含 opencv 归属纠正 + 环境体检）**

```bash
python tools/install_deps.py --with-dev --check
```

等价的手工三步（**顺序不可换**）：

```bash
# 1) 装运行时依赖（必须带 --no-cache-dir，避免中断留下空壳包，见「六、排错」R11）
pip install --no-cache-dir -r requirements.txt
# 2) 摘掉 rapidocr 强拉进来的非 headless opencv（否则与 PySide6 双 Qt 冲突）
pip uninstall -y opencv-python
# 3) 强制重装 headless 版，确权 cv2/ 目录归属
pip install --no-cache-dir --force-reinstall --no-deps opencv-python-headless

# 开发/测试依赖
pip install --no-cache-dir -r requirements-dev.txt
```

> ⚠️ **禁止使用 `--no-deps` 安装主依赖**（只允许在步骤 3 纠正 opencv 时使用）。
> ⚠️ 第 2、3 步是本工具特有的步骤：`rapidocr` 的元数据写死 `opencv_python>=4.5.1.48`，
> 与我们要用的 `opencv-python-headless` **包名不同但同占 `cv2/` 目录**，
> pip 的 resolver 无法表达这种互斥，只能装完后手工纠正。
> 漏做这一步 → exe 内混入 opencv 自带的 Qt DLL → 与 PySide6 双 Qt，运行时闪退且打包阶段无感。

### 1.3 运行

```bash
python main.py                # 启动 GUI
python main.py --self-test    # 自检（规则加载 + 口径字符串 + 13 列结构），不启动 GUI
python main.py --check-env    # 环境体检（空壳包检测），不启动 GUI
```

### 1.4 测试

```bash
pytest                        # 全部回归测试（SOP Phase 2 强制）
pytest -m architecture        # 仅分层架构守卫用例
pytest -m ocr                 # 仅 OCR 相关用例
```

---

## 二、目录结构

```
customs_declaration_checker/
├── pyproject.toml               # 项目元数据 + 依赖声明 + pytest/ruff 配置
├── requirements.txt             # 运行时依赖（版本按 v1.2 第 13.6 节实测锁定）
├── requirements-dev.txt         # 开发/打包依赖
├── build_exe.ps1                # 一键打包脚本（第一步即环境体检，R11 防线）
├── customs_checker.spec         # PyInstaller spec（注入 rapidocr 模型与规则 YAML）
├── README.md                    # 本文档
├── CHANGELOG.md                 # 版本变更记录
├── .gitignore                   # 排除构建产物 / 原始输入 / 运行期产物
├── main.py                      # 程序唯一入口
│
├── docs/                        # 随交付版本归档的规格文档（01–08）
│
├── infra/                       # L1 基础设施层（第三方库唯一边界）
│   ├── config.py                #   AppConfig：默认共享根/输出落点/批大小/OCR 参数 + %APPDATA% 持久化
│   ├── encoding.py              #   启动即强制 UTF-8；中文/UNC 路径安全读写
│   ├── logger.py                #   统一日志：滚动文件 + 信号双通道
│   ├── errors.py                #   异常体系 + 中文文案映射（分级：致命/记录/红线）
│   ├── fs_lock.py               #   只读红线守卫（input_readonly / assert_readonly / assert_within）
│   └── resources.py             #   资源定位（bundle_root 取 _MEIPASS / app_base_dir 取 exe 目录）
│
├── core/                        # L2 核心引擎层（纯 Python，禁止 import PySide6）
│   ├── models.py                #   数据模型与枚举（Verdict/NoiseLevel/StructureVariant/…）
│   ├── constants.py             #   口径常量（四类判定字符串，与 SOP 1.3 逐字一致）
│   ├── rule_repository.py       #   规则仓库（加载/校验/重载/外置覆盖，不可变快照）
│   ├── excel_probe.py           #   【T02】Excel 结构探查（变体 A–E）
│   ├── field_mapping.py         #   【T02】列名映射 + P4 双通道自校正
│   ├── element_parser.py        #   【T02】申报要素解析
│   ├── image_resolver.py        #   【T03】图片三级索引匹配
│   ├── ocr_engine.py            #   【T03】OCR 抽象 + RapidOcrBackend
│   ├── judge_engine.py          #   【T04】判定引擎
│   ├── noise_guard.py           #   【T04】噪声三态（不虚高红线）
│   ├── diff_util.py             #   【T04】型号逐字符差异
│   ├── resume_store.py          #   【T04】断点 + 累积（resume.json v2 指纹）
│   ├── result_exporter.py       #   【T04】三产物导出（13 列）
│   └── pipeline.py              #   【T05】引擎编排（无 Qt）
│
├── app/                         # L3 应用服务层（编排，不含判定，不含 Qt）
│   └── …                        #   【T05】path_policy/preflight/events/check_task/run_controller/…
│
├── ui/                          # L4 表现层（PySide6）
│   ├── main_window.py           #   主窗口（T01 骨架 / T05 三区布局）
│   ├── widgets/                 #   【T05】data_source_panel/progress_log_panel/…
│   └── styles/                  #   【T05】app.qss + palette.py
│
├── rules/                       # 规则外置载体（v1.1 C：补规则 = 改 YAML 不改代码）
│   ├── fields_blacklist.yaml    #   字段名黑名单 + 语境排除前缀
│   ├── brand_patterns.yaml      #   品牌正则（ASCII / 中文 / 单"牌"字）+ none_tokens
│   ├── model_clean_rules.yaml   #   型号清洗（头部锚定，防误伤 2.402GHz）
│   ├── whole_machine_brand.yaml #   外箱整机品牌上下文词表
│   ├── separators.yaml          #   分隔符/冒号归一化 + 不可见字符表
│   └── noise_signals.yaml       #   疑似噪声信号（易混字符 + 模糊相似度）
│
├── tools/
│   ├── check_env.py             # 环境体检（空壳包检测，R11 防线；被 build_exe.ps1 调用）
│   └── install_deps.py          # 依赖安装（含 opencv headless 纠正 + 可选体检）
│
└── tests/                       # 回归测试（SOP Phase 2 强制）
    └── test_dependency_manifest.py  # 依赖声明一致性锁（代码 import ↔ 依赖清单，防漏声明）
```

**依赖方向（单向，机器强制）**：`ui → app → core → infra`
> `core/` 与 `app/` **不得 import PySide6/PyQt**，由 `tests/test_architecture_guard.py` 断言。

---

## 三、数据源约定

```
{共享根}\{人员}\{出货通知书号}\{成品料号}[-后缀]\{料号}&{订单号}&{序号}.jpg
    ↑                      ↑
    含 {年份} 变量        一级索引（票号）
```

**默认共享根模板**（`infra/config.py` 的 `SHARE_BASE_TEMPLATE`）：

```
\\172.20.99.220\制造中心\仓储物流部\成品科\{年份}年报关要素图片
```

> ⚠️ **`{年份}` 是变量** —— SOP 2.6「迁移必改三处」之一，跨年必须改。
> UI 推荐**直接选到「票号目录」那一层**（如 `…\2026年报关要素图片\{人员}\SA26090215\`），
> 年份被用户的选择自然消化，工具无需猜（架构设计 12.D.2 方案 3）。

**数据源优先级（铁律，SOP 2.2）**：`用户显式指定 > 项目空间默认约定`
> **绝不盲目扫描默认目录** —— 若默认目录只有旧票数据，会错跑成上一票。
> 未指定时返回 `needs_user_input=True` 由 UI 提示（**不静默扫描**）。

### 目录空间约束（三条铁律，SOP 2.4）

```
项目空间/
├── 原始输入/     🔒 只读红线：内容绝不允许修改、重命名、删除
├── 成果产出/     ✅ 最终交付物（校验汇总表 + 待人工复核清单）
└── 过程产出/     🔧 过程文件（详细日志 / 断点 / 缓存，可清理）
```

---

## 四、输出产物（SOP 七）

| 产物 | 落点 | 内容 |
|---|---|---|
| `校验汇总表_{票号}.xlsx` | **成果产出** | **恰好 13 列**（顺序固定） |
| `{票号}_待人工复核清单.csv` | **成果产出** | ⚠️ + 🔵 的行动项清单（`utf-8-sig` 防乱码） |
| `校验详细日志_{票号}_累积.json` | **过程产出** | 每条记录全部 OCR 原文 + 判定依据 |
| `resume_{票号}.json` | **过程产出** | 断点（含数据源指纹，防串票） |
| `校验运行日志_{票号}.log` | **过程产出** | 滚动运行日志 |

### 四类判定口径（SOP 1.3，**逐字一致，禁止改动**）

| 判定 | 触发条件 |
|---|---|
| ✅ 校验合格 | 申报值与图片证据一致；或双方均为"无" |
| ❌ 校验异常 | 申报值有、图片有但**不一致**；或申报值缺失但图片明确有 |
| ⚠️ 缺图内标识，人工复核 | 图片无任何品牌/型号文字；OCR 证据不足（残片、噪声大） |
| 🔵 缺图，人工复核 | 共享目录找不到对应图片 |

> **关键区分**：`校验异常` = 有证据证明不一致；`缺图内标识` = 没找到证据。**不可混为一谈。**

---

## 五、打包（交付给现场用户）

### 5.1 前置要求（**务必遵守**）

1. **必须在全新干净 venv 中打包** —— 避免 pip 损坏包（空壳/残缺）污染随 exe 出厂（R11）。
2. 依赖安装一律走 `python tools/install_deps.py --with-dev`（它会带 `--no-cache-dir`
   并做完 opencv headless 纠正）；**禁止**对主依赖使用 `--no-deps`。
3. **确认 `cv2` 是 headless 版**：`python -c "import cv2;print(cv2.__version__)"` 应为
   `4.14.0`；若装到 `5.x`（非 headless）说明漏做了纠正步骤，**必须重做**。
4. 打包机与目标机**同为 x64**（禁止在 Win11 ARM 上打包后给 Win10 x64 用）。
5. **VC++ Redistributable**：`onnxruntime` / `numpy` 依赖 `VCRUNTIME140.dll`。
   Win10 未必预装；若目标机缺装，exe 可能无法启动 —— 请参考
   [Microsoft VC++ Redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe) 说明。
   `build_exe.ps1` 会检测并提示。
6. **强制闸门**：打包前必须通过 `python tools/check_env.py`（exit 0）；
   若报 `exit 1`（有损坏包）**必须修复后再打包**，不得跳过。

### 5.2 一键打包

```powershell
# 单文件 exe（默认，体积约 200–350MB，全内置免安装）
.\build_exe.ps1

# 目录形式（体积/启动速度折中，旧机器更友好）
.\build_exe.ps1 -Onedir

# 打包前清理 build/dist 缓存
.\build_exe.ps1 -Clean

# 指定解释器
.\build_exe.ps1 -Python "C:\path\to\venv\Scripts\python.exe"
```

> **`build_exe.ps1` 的第一步就是环境体检**（调用 `tools/check_env.py`）：
> 检测到**完全空目录 / 残缺包**或关键依赖损坏时**立即中止打包**（R11 防线）——
> 这样可避免"exe 带着隐藏 bug 出厂，而用户机器无 Python 无法排查"的致命后果。
>
> 体检范围包含 **19 个关键依赖 + 22 条模块 import 验证**（含 `openpyxl.pivot.table`
> 等深路径子模块，专门针对"包在但子模块丢"的残缺包）。

### 5.3 打包注意事项

| # | 注意点 | 处置 |
|---|---|---|
| 1 | rapidocr 模型/配置漏收 → 运行时 `ModelNotFound` | spec 内 `collect_all("rapidocr")` + `collect_data_files` 双保险 |
| 2 | onnxruntime 动态库漏收 | spec 内 `collect_all("onnxruntime")` |
| 3 | 体积过大 | 排除 PySide6 的 WebEngine/Sql/Quick/Multimedia 等重模块 |
| 4 | PySide6 + opencv 双 Qt 冲突 → exe 闪退 | 使用 `opencv-python-headless`（不含 Qt 插件） |
| 5 | 官方打包器用 GBK 读文件 | `build_exe.ps1` 内设 `PYTHONUTF8=1` |

---

## 六、排错指南

### R11 · pip 空壳包污染（**最易踩、后果最重**）

> ⚠️ **本问题已累计复发 7 次**（`numpy` / `omegaconf` / `onnxruntime` / `colorlog` /
> `flatbuffers` / `openpyxl` / `charset_normalizer`），且**第 3 次打穿过体检脚本**
> —— 当时 `openpyxl` 未登记进 `CRITICAL_DEPS`，漏检后 `from openpyxl import
> load_workbook` 在 exe 内直接 `ImportError`。**故必须严格遵守下方"打包红线"。**
>
> 2026-09-16 复现时**再次**打穿：`numpy` / `omegaconf` / `flatbuffers` / `colorlog` /
> `charset-normalizer` / `idna` / `et_xmlfile` / `iniconfig` 同时进入损坏态。

**现象**：`import` 报 `cannot import name 'X'` / `module has no attribute 'Y'` /
`__file__ is None`；pip 报 `Cannot uninstall X: no RECORD file`。

**根因**：pip 安装在沙箱被中断后留下损坏目录，分**三类**（**都可能随 exe 出厂**）：

| 类型 | 特征 | 典型包 |
|---|---|---|
| 完全空目录 | 目录在、**里面一个文件都没有**、无 `__init__.py`、无 `dist-info` | `flatbuffers`、`charset_normalizer`、`iniconfig` |
| **残缺包** | 目录在、**部分文件在**，但**关键文件丢失**（如 `openpyxl/__init__.py`、`openpyxl/pivot/table.py`）、无 `dist-info` | `openpyxl`、`colorlog`、`numpy` |
| **反向缺体**（最阴） | **`dist-info` 在、包目录整个没了** → pip 认为"已安装"而**跳过安装**，`pip install` 反复跑都修不好 | `numpy`（`pip list` 显示版本为 `None`） |

> **残缺包更隐蔽**：目录"看起来正常"、`import openpyxl` 甚至可能侥幸成功，
> 直到调用到丢失的子模块才炸 —— 这是它最危险的地方。
>
> **反向缺体更致命**：`check_env` 的第一道扫描是"遍历 site-packages 里的目录"，
> 包目录既然不在，就扫描不到；**只有第二道「逐包 import 验证」能抓住它**
> （表现为 `ModuleNotFoundError`）。修复时必须**先删掉残留 dist-info**，
> 否则 pip 永远认为已满足。

**诊断（打包前必做）**：
```bash
python tools/check_env.py          # exit 1 = 有损坏包，禁止打包
```

**修复**：
```bash
# 1) 删除损坏目录 + 其残缺 dist-info（反向缺体必须删 dist-info，否则 pip 认为已装好）
rm -rf "<site-packages>/<包名>" "<site-packages>/<包名>-*.dist-info"
# 2) 重新安装（务必带 --no-cache-dir；禁止 --no-deps）
pip install --no-cache-dir <包名>
# 3) 复检（务必确认 exit 0）
python tools/check_env.py
```

> 💡 **若 1) 删不动 / pip 报 `no RECORD file`，说明该次 pip 进程被中断过。**
> 大概率还有**其它包**处于半安装态 —— 不要只修报错的那一个，
> 直接 `python tools/check_env.py` 全量复检，或干脆**新建干净 venv 重来**（更省时间）。

**预防（三条红线，不可妥协）**：

1. **打包机必须用全新干净 venv** —— 在复用/污染过的环境里打包，会把这些隐藏
   损坏直接烤进 exe，而用户机器无 Python 无法排查；
2. 安装依赖一律带 `--no-cache-dir`，**禁止** `--no-deps`；
3. **`tools/check_env.py` 是强制打包闸门** —— `build_exe.ps1` 第一步即调用它，
   **exit 1 必须中止打包**（脚本已内置拦截），不得用任何方式跳过。

### 其它常见问题

| 现象 | 原因 | 处置 |
|---|---|---|
| `cv2.imread` 返回 `None`（中文路径） | OpenCV 已知缺陷 | 本工具统一用 `np.frombuffer + cv2.imdecode`（已内建） |
| 提示缺 `opencv_python` | rapidocr 的 `requires_dist` 写的是非 headless 版 | **属预期**，我们用 `opencv-python-headless` 规避双 Qt 冲突 |
| 中文路径乱码 | 参数经命令行传递 | **禁止经命令行传中文**；统一 Python 内 `os.environ` 赋值 |
| 共享盘中途断连 | 网络/认证问题 | 预检探测可达性；运行期每批 re-check；断连**自动暂停**并保留断点 |
| 首次 OCR 慢 | 模型加载 | `RapidOCR()` 初始化实测仅 ~0.5s；仍建议异步 warmup |
| 图片全部报"缺图" | **列名与数据语义交叉**（P4） | 本工具内建三通道自校正映射；若干跑预演命中率异常低，请反馈实施人员 |

### 规则文件缺失 / 校验失败

规则在**启动时一次性加载**（不做自动热加载，避免跑批中口径不一致）：

* 缺字段 / YAML 语法错误 → 启动即给明确错误（含文件名 + 字段名），**不静默降级**；
* 现场补规则：把同名 YAML 放到 `%APPDATA%\CustomsChecker\rules\`，
  该目录**优先于**内置规则；改完用菜单「规则 → 重载规则文件」手动重载
  （**下次跑批生效**）。

---

## 七、口径一致性声明

* 本工具**未新增、未改写、未弱化**任何判定口径。
* 四类判定字符串（`core/constants.py`）与 SOP 1.3 **逐字一致**，由
  `tests/test_constants.py` 硬断言锁定。
* 规则外置（`rules/*.yaml`）改的是**规则的实现载体**，**口径语义完全不变**；
  `tests/test_rule_repository.py` 断言 YAML 与 `constants.py` 默认值一致，防两处漂移。
* OCR 封装库由 `rapidocr_onnxruntime` 升级为 `rapidocr`（**被迫的版本升级**：
  旧库硬卡 Python<3.13），**推理后端仍是 onnxruntime**，属工程实现层变更。

**三条质量红线（不可妥协）**：

1. **不虚高** —— 疑似 OCR 噪声不得直接上报为异常，必须提供看图确认入口；
2. **不误放** —— 每条结论必须附证据（OCR 原文片段 / 图片路径 / 对照记录）；
3. **可追溯** —— 详细日志落盘（JSON），记录每条记录的全部 OCR 文本与判定依据。

---

## 八、实施者部署检查清单（SOP 2.6「迁移必改三处」）

| # | 位置 | 说明 |
|---|---|---|
| 1 | Excel 扫描目录 | 默认用脚本同目录的 `原始输入\`，相对路径可零修改 |
| 2 | `SHARE_BASE_TEMPLATE` | ⚠️ **含年份**，跨年必须修改（或 UI 直接选到票号目录） |
| 3 | 输出落点（成果产出 / 过程产出） | 默认 exe 同目录；可在 UI 指定 |

**首次部署还需**：① 核对共享路径读权限；② 安装 VC++ Redistributable；
③ 运行 `python tools/check_env.py` 确认环境干净。
