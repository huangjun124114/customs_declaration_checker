# =============================================================================
#  报关申报要素自动校验工具 · 一键打包脚本（Windows PowerShell）
#
#  用法：
#     .\build_exe.ps1                 # 打包为单文件 exe（默认）
#     .\build_exe.ps1 -Onedir         # 打包为目录形式（体积/启动速度折中）
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
    [switch]$SkipEnvCheck,
    [switch]$Clean,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

# ── 强制 UTF-8（SOP 陷阱 #5：官方打包器用 GBK 读文件）────────────────────
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONLEGACYWINDOWSSTDIO = "0"

# 脚本所在目录 = 工程根
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

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
    & $Python "tools/check_env.py"
    $envExit = $LASTEXITCODE

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

# --onedir 备选（spec 内已定义 onedir 逻辑由 EXE 的 COLLECT 决定；
#  此处通过环境变量传递，供 build 侧选择输出形态）
if ($Onedir) {
    $env:CUSTOMS_ONEDIR = "1"
    Write-Host "       模式：--onedir（目录形式）" -ForegroundColor Cyan
} else {
    $env:CUSTOMS_ONEDIR = "0"
    Write-Host "       模式：单文件 exe" -ForegroundColor Cyan
}

& $Python -m PyInstaller @pyiArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[FAIL] PyInstaller 打包失败（exit $LASTEXITCODE）。" -ForegroundColor Red
    exit $LASTEXITCODE
}

# ── 5. 结果核对 ────────────────────────────────────────────────────
Write-Host ""
Write-Host "[STEP 5] 核对产物..." -ForegroundColor Yellow

$distDir = Join-Path $Root "dist"
if (Test-Path $distDir) {
    Get-ChildItem -Path $distDir | ForEach-Object {
        $sizeMB = [math]::Round($_.Length / 1MB, 1)
        Write-Host ("       {0}  ({1} MB)" -f $_.Name, $sizeMB) -ForegroundColor Green
    }
    Write-Host ""
    Write-Host "[OK]   打包完成。产物目录：$distDir" -ForegroundColor Green
    Write-Host "       请在**干净 Win10 1809+ / Win11 x64** 机器上双击验证。" -ForegroundColor Green
} else {
    Write-Host "[FAIL] 未找到 dist 目录，打包可能未成功。" -ForegroundColor Red
    exit 1
}

exit 0
