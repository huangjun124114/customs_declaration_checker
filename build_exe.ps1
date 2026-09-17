# =============================================================================
#  报关申报要素自动校验工具 · 一键打包脚本（Windows PowerShell）
#
#  用法：
#     .\build_exe.ps1                 # 打包为目录形式（onedir，v0.2.0 默认）
#     .\build_exe.ps1 -Zip            # 目录形式 + 产出 dist 下的分发包 zip
#     .\build_exe.ps1 -Onefile        # 回退单文件 exe（非默认）
#     .\build_exe.ps1 -Onedir         # 显式目录形式（= 默认；保留以兼容旧脚本调用）
#     .\build_exe.ps1 -SkipEnvCheck   # 跳过环境体检（⚠️ 不推荐，仅供排错）
#     .\build_exe.ps1 -Clean          # 打包前清理 build/dist 缓存
#
#  前置要求（架构设计 13.9 / B.3）：
#     ① **必须在全新干净 venv 中执行**（避免 R11 空壳包随 exe 出厂）
#     ② 必须带 --no-cache-dir 安装依赖，禁止 --no-deps
#     ③ 打包机与目标机同为 x64
#
#  ⚠️ 本脚本**第一步**即调用 tools/check_env.py 体检，**不通过立即中止**（R11 防线）。
# =============================================================================

[CmdletBinding()]
param(
    [switch]$Onedir,
    [switch]$Onefile,
    [switch]$Zip,
    [switch]$SkipEnvCheck,
    [switch]$Clean,
    # 一线员工操作手册随包出厂（v0.3.7 起，用户裁定 2026-09-17）。
    #   -SkipManual 显式排除手册；-ManualHtml 指定手册源文件（默认按版本号+日期自动探测）。
    [switch]$SkipManual,
    [string]$ManualHtml = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

# 交付版本（与 main.APP_VERSION / ui.main_window.APP_VERSION 对齐）
$Version = "0.3.7"
# 分发包名（v0.2.0 点 10）
# ⚠️ 日期后缀 = **实际打包产出日**（v0.3.2 口径变更 / v0.3.3 工作台弹窗 /
#    v0.3.4「元素值连写」泄漏修复 / v0.3.5 品牌分词边界 / v0.3.6「判定依据」列 /
#    v0.3.7 复核工作台四项优化 + 两处信号/残影缺陷修复 + 一线员工操作手册，
#    均于 0917 完成）
$BuildDate = "0917"
$ZipName = "报关申报要素校验工具_v${Version}_${BuildDate}.zip"

# ── 一线员工操作手册（v0.3.7 起**随包出厂**，用户裁定 2026-09-17）────────────
# 手册位于项目空间 `成果产出\`（= 仓库根的上一级），命名规则 `操作手册_v<版本>_<日期>.html`。
# ⚠️ **必须由本脚本写入 app 目录后再压缩**。v0.3.7 曾出现"手工把手册塞进 onedir、再手工压缩"
#    的临时动作 → 脚本产出的交付名 zip 与 onedir 内容不一致（交付名那个**不含手册**），
#    且每次打包都会复现。故此处固化为流程中**唯一的手册入口**，禁止绕开。
# ⚠️ 路径变量必须在 $Root 赋值**之后**计算（见下方「脚本所在目录 = 工程根」），否则取到空值。
$ManualName   = "操作手册.html"          # 入包后的固定名（不带版本，现场直观）
$ManualFigDir = "操作手册插图"

# ── 强制 UTF-8（SOP 陷阱 #5：官方打包器用 GBK 读文件）────────────────────
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONLEGACYWINDOWSSTDIO = "0"
# ⚠️ 配套（SOP 陷阱 #5 的反面）：本脚本强制子进程输出 UTF-8，而 Windows
#    PowerShell 5.1 默认用 GBK 控制台编码解读「原生进程 stdout」→ check_env /
#    PyInstaller 的中文日志会变成乱码（实测打包日志第一步整段乱码）。
#    把控制台输出编码设为 UTF-8，与子进程对齐。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# 脚本所在目录 = 工程根
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# 项目空间 = 工程根的上一级；手册源目录在此（v0.3.7 起手册随包出厂）
$SpaceRoot = Split-Path -Parent $Root
$ManualDir = Join-Path $SpaceRoot "成果产出"
if ([string]::IsNullOrWhiteSpace($ManualHtml)) {
    $ManualHtml = Join-Path $ManualDir "操作手册_v${Version}_${BuildDate}.html"
}
$ManualFigSrc = Join-Path $ManualDir $ManualFigDir

Write-Host "==============================================================" -ForegroundColor Cyan
Write-Host " 报关申报要素自动校验工具 · 打包脚本" -ForegroundColor Cyan
Write-Host " 工程根：$Root" -ForegroundColor Cyan
Write-Host "==============================================================" -ForegroundColor Cyan

# ── 0. 解析 Python 解释器 ────────────────────────────────────────────
if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if ([string]::IsNullOrWhiteSpace($Python)) {
    Write-Host "[FAIL] 未找到 Python 解释器。请用 -Python 指定绝对路径。" -ForegroundColor Red
    exit 1
}
Write-Host "[INFO] 使用解释器：$Python"

# ── 1. 环境体检（R11 防线，不通过立即中止）───────────────────────────
if (-not $SkipEnvCheck) {
    Write-Host ""
    Write-Host "[STEP 1] 环境体检（tools/check_env.py）..." -ForegroundColor Yellow
    # ⚠️ PS 5.1 陷阱（实测打包失败真因之一）：原生 exe 往 stderr 写日志时，
    #    在 $ErrorActionPreference='Stop' 下会被当作致命 NativeCommandError 并中止
    #    整个脚本。体检脚本 / PyInstaller 的日志都会写 stderr，故原生调用期间临时
    #    降为 'Continue'，退出码一律由 $LASTEXITCODE 显式判读。
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $Python "tools/check_env.py"
    $envExit = $LASTEXITCODE
    $ErrorActionPreference = $prevEAP

    if ($envExit -eq 1) {
        Write-Host ""
        Write-Host "[FAIL] 环境体检不通过（检测到空壳包或关键依赖损坏）。" -ForegroundColor Red
        Write-Host "       请按体检报告处置后重试；务必使用全新干净 venv。" -ForegroundColor Red
        Write-Host "       打包已中止（R11 防线）。" -ForegroundColor Red
        exit 1
    }
    elseif ($envExit -eq 2) {
        Write-Host "[WARN] 体检有警告（版本偏差），继续打包。请复核上方报告。" -ForegroundColor Yellow
    }
    else {
        Write-Host "[OK]   环境体检通过。" -ForegroundColor Green
    }
} else {
    Write-Host "[WARN] 已跳过环境体检（-SkipEnvCheck）。风险自负。" -ForegroundColor Yellow
}

# ── 2. VC++ 运行库检测提示（B.3 注意事项 #1）─────────────────────────
Write-Host ""
Write-Host "[STEP 2] 检测 VC++ 运行库..." -ForegroundColor Yellow
$vcRuntime = Join-Path $env:SystemRoot "System32\VCRUNTIME140.dll"
if (Test-Path $vcRuntime) {
    Write-Host "[OK]   已检测到 VCRUNTIME140.dll。" -ForegroundColor Green
} else {
    Write-Host "[WARN] 未检测到 VCRUNTIME140.dll！" -ForegroundColor Yellow
    Write-Host "       onnxruntime / numpy 依赖 VC++ Redistributable。" -ForegroundColor Yellow
    Write-Host "       目标机器若缺装，exe 可能无法启动。" -ForegroundColor Yellow
    Write-Host "       请参考 README「部署前置条件」安装 VC_redist.x64.exe。" -ForegroundColor Yellow
}

# ── 3. 清理旧构建产物 ───────────────────────────────────────────────
if ($Clean) {
    Write-Host ""
    Write-Host "[STEP 3] 清理 build / dist 缓存..." -ForegroundColor Yellow
    foreach ($dir in @("build", "dist", "__pycache__")) {
        if (Test-Path $dir) {
            Remove-Item -Recurse -Force $dir -ErrorAction SilentlyContinue
            Write-Host "       已删除 $dir"
        }
    }
}

# ── 4. 执行 PyInstaller ────────────────────────────────────────────
Write-Host ""
Write-Host "[STEP 4] PyInstaller 打包..." -ForegroundColor Yellow

$pyiArgs = @(
    "--noconfirm",
    "--clean",
    "customs_checker.spec"
)

# --onedir 为 **默认**（spec 内由 CUSTOMS_ONEDIR 决定输出形态）；
#   -Onefile 显式回退单文件；未指定时为目录形式（v0.2.0 点 10）。
if ($Onefile) {
    $env:CUSTOMS_ONEDIR = "0"
    Write-Host "       模式：单文件 exe（-Onefile）" -ForegroundColor Cyan
} else {
    $env:CUSTOMS_ONEDIR = "1"
    if ($Onedir) {
        Write-Host "       模式：--onedir（目录形式，显式 -Onedir）" -ForegroundColor Cyan
    } else {
        Write-Host "       模式：--onedir（目录形式，默认）" -ForegroundColor Cyan
    }
}

# ⚠️ 同 STEP 1 的 PS 5.1 stderr 陷阱：PyInstaller 的 INFO/进度日志写 stderr，
#    在 'Stop' 偏好下会中止脚本 → 原生调用期间降为 'Continue'。
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $Python -m PyInstaller @pyiArgs
$pyiExit = $LASTEXITCODE
$ErrorActionPreference = $prevEAP
if ($pyiExit -ne 0) {
    Write-Host ""
    Write-Host "[FAIL] PyInstaller 打包失败（exit $pyiExit）。" -ForegroundColor Red
    exit $pyiExit
}

# ── 5. 结果核对 ────────────────────────────────────────────────────
Write-Host ""
Write-Host "[STEP 5] 核对产物..." -ForegroundColor Yellow

$distDir = Join-Path $Root "dist"
if (Test-Path $distDir) {
    Get-ChildItem -Path $distDir | ForEach-Object {
        if ($_.PSIsContainer) {
            Write-Host ("       {0}\  (目录)" -f $_.Name) -ForegroundColor Green
        } else {
            $sizeMB = [math]::Round($_.Length / 1MB, 1)
            Write-Host ("       {0}  ({1} MB)" -f $_.Name, $sizeMB) -ForegroundColor Green
        }
    }
    Write-Host ""
    Write-Host "[OK]   打包完成。产物目录：$distDir" -ForegroundColor Green

    # ── 6. 可选：压缩为分发包 zip（v0.2.0 点 10，由 -Zip 开关触发）──────
    if ($Zip) {
        Write-Host ""
        Write-Host "[STEP 6] 压缩分发包..." -ForegroundColor Yellow
        $appDirName = "报关申报要素校验工具"
        $appDir = Join-Path $distDir $appDirName
        $zipPath = Join-Path $distDir $ZipName

        if ($Onefile) {
            Write-Host "[WARN] -Onefile 模式无目录可压缩，已跳过 -Zip。" -ForegroundColor Yellow
        }
        elseif (-not (Test-Path $appDir)) {
            Write-Host "[FAIL] 未找到目录形式产物：$appDir（无法压缩）。" -ForegroundColor Red
            exit 1
        }
        else {
            # ── 6a. 一线员工操作手册随包出厂（v0.3.7 起，用户裁定 2026-09-17）──
            # ⚠️ 必须在 Compress-Archive **之前**写入 app 目录，否则交付包不含手册
            #    （这正是 v0.3.7 交付名 zip 与 onedir 不一致的根因）。
            if ($SkipManual) {
                Write-Host "[WARN] -SkipManual：本次**不**打包操作手册。" -ForegroundColor Yellow
            }
            else {
                if (-not (Test-Path $ManualHtml)) {
                    Write-Host "[FAIL] 未找到一线员工操作手册：$ManualHtml" -ForegroundColor Red
                    Write-Host "       手册自 v0.3.7 起随包出厂（用户裁定）。" -ForegroundColor Red
                    Write-Host "       请先生成手册，或显式加 -SkipManual 排除。" -ForegroundColor Red
                    exit 1
                }
                $manualDst = Join-Path $appDir $ManualName
                Copy-Item -Force $ManualHtml $manualDst
                $manualKB = [math]::Round((Get-Item $manualDst).Length / 1KB, 1)
                Write-Host ("       手册入包：{0}  ({1} KB)" -f $ManualName, $manualKB) -ForegroundColor Green

                if (Test-Path $ManualFigSrc) {
                    $figDst = Join-Path $appDir $ManualFigDir
                    if (Test-Path $figDst) {
                        Remove-Item -Recurse -Force $figDst -ErrorAction SilentlyContinue
                    }
                    New-Item -ItemType Directory -Force -Path $figDst | Out-Null
                    Copy-Item -Force (Join-Path $ManualFigSrc "*") $figDst
                    $figCount = @(Get-ChildItem -File $figDst).Count
                    Write-Host ("       插图入包：{0}\  ({1} 个文件)" -f $ManualFigDir, $figCount) -ForegroundColor Green
                }
                else {
                    Write-Host "[WARN] 未找到手册插图目录：$ManualFigSrc（手册仍已入包）" -ForegroundColor Yellow
                }
            }

            if (Test-Path $zipPath) {
                Remove-Item -Force $zipPath -ErrorAction SilentlyContinue
            }
            Compress-Archive -Path $appDir -DestinationPath $zipPath -CompressionLevel Optimal -Force
            if (Test-Path $zipPath) {
                $zipMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
                Write-Host ("[OK]   分发包：{0}  ({1} MB)" -f $zipPath, $zipMB) -ForegroundColor Green
                Write-Host "       交付物 = 该 zip（解压出「报关申报要素校验工具」文件夹，双击其中的 exe 运行）。" -ForegroundColor Green
                Write-Host "       包内附一线员工操作手册：操作手册.html + 操作手册插图\（v0.3.7 起）。" -ForegroundColor Green
            } else {
                Write-Host "[FAIL] zip 生成失败。" -ForegroundColor Red
                exit 1
            }
        }
    }

    Write-Host "       请在**干净 Win10 1809+ / Win11 x64** 机器上双击验证。" -ForegroundColor Green
} else {
    Write-Host "[FAIL] 未找到 dist 目录，打包可能未成功。" -ForegroundColor Red
    exit 1
}

exit 0
