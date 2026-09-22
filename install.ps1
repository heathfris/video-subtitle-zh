<#
.SYNOPSIS
    video-subtitle-zh 一键安装（Windows）

.DESCRIPTION
    在仓库目录下创建 .venv 虚拟环境、安装依赖、检查 ffmpeg，可选预下载模型。
    不污染系统 Python —— 删掉 .venv 就等同于卸载干净。

    注意：本文件必须保存为 UTF-8 with BOM。Windows PowerShell 5.1 在读取
    无 BOM 的 UTF-8 脚本时会按系统 ANSI 代码页（简体中文环境下是 GBK）解析，
    脚本里的中文会全变成乱码并触发语法错误。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
    powershell -ExecutionPolicy Bypass -File install.ps1 -WithModel
    powershell -ExecutionPolicy Bypass -File install.ps1 -WithModel -Model distil-large-v3
#>
[CmdletBinding()]
param(
    [switch]$WithModel,
    [string]$Model = "small"
)

$ErrorActionPreference = "Stop"

# 让 Python 以 UTF-8 输出，并让控制台按 UTF-8 解码，否则中文日志会变乱码
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Venv = Join-Path $Root ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"

Write-Host ""
Write-Host "=========================================================="
Write-Host " video-subtitle-zh 安装"
Write-Host "=========================================================="
Write-Host "  仓库目录：$Root"
Write-Host ""

# ---------------------------------------------------------- 1. Python
Write-Host "[1/4] 查找 Python"

function Get-PythonLauncher {
    <# 返回 @{ Exe = '...'; Args = @(...) }，找不到返回 $null #>
    foreach ($c in @("py", "python", "python3")) {
        if (-not (Get-Command $c -ErrorAction SilentlyContinue)) { continue }
        try {
            if ($c -eq "py") {
                $v = & $c -3 -c "import sys; print(sys.version_info[0]*100+sys.version_info[1])" 2>$null
                if ($v -and [int]$v -ge 309) {
                    return @{ Exe = $c; Args = @("-3") }
                }
            } else {
                $v = & $c -c "import sys; print(sys.version_info[0]*100+sys.version_info[1])" 2>$null
                if ($v -and [int]$v -ge 309) {
                    return @{ Exe = $c; Args = @() }
                }
            }
        } catch { continue }
    }
    return $null
}

$launcher = Get-PythonLauncher
if (-not $launcher) {
    Write-Host "  X 需要 Python 3.9 或更高版本，但没有找到。" -ForegroundColor Red
    Write-Host "    安装方式（任选一个）："
    Write-Host "      winget install Python.Python.3.12"
    Write-Host "      或到 https://www.python.org/downloads/ 下载，安装时勾选 Add to PATH"
    exit 1
}

$pyExe = $launcher.Exe
$pyArgs = $launcher.Args
$ver = & $pyExe @pyArgs -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
Write-Host "  OK  $pyExe $ver"

# ---------------------------------------------------------- 2. 虚拟环境
Write-Host ""
Write-Host "[2/4] 创建虚拟环境"

if (Test-Path $Py) {
    Write-Host "  已存在，复用：$Venv"
} else {
    & $pyExe @pyArgs -m venv $Venv
    if (-not (Test-Path $Py)) {
        Write-Host "  X 创建虚拟环境失败" -ForegroundColor Red
        exit 1
    }
    Write-Host "  OK  已创建：$Venv"
}

# ---------------------------------------------------------- 3. 依赖
Write-Host ""
Write-Host "[3/4] 安装 Python 依赖"

& $Py -m pip install -q --upgrade pip 2>$null | Out-Null

# 各源依次尝试。镜像可用性随网络环境变化很大，写死任何一个都会让一部分人失败。
$indexes = @(
    @{ Name = "pip 默认源"; Url = "" },
    @{ Name = "清华 TUNA";  Url = "https://pypi.tuna.tsinghua.edu.cn/simple" },
    @{ Name = "阿里云";     Url = "https://mirrors.aliyun.com/pypi/simple" },
    @{ Name = "PyPI 官方";  Url = "https://pypi.org/simple" }
)

$reqFile = Join-Path $Root "requirements.txt"
$installed = $false

foreach ($idx in $indexes) {
    Write-Host -NoNewline ("  尝试 " + $idx.Name + " ... ")

    # 注意别用 $args —— 那是 PowerShell 的自动变量
    $pipArgs = @("-m", "pip", "install", "-q")
    if ($idx.Url) { $pipArgs += @("-i", $idx.Url) }
    $pipArgs += @("-r", $reqFile)

    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $Py @pipArgs 2>$null | Out-Null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev

    if ($code -eq 0) {
        Write-Host "成功"
        $installed = $true
        break
    }
    Write-Host "失败"
}

if (-not $installed) {
    Write-Host "  X 所有源都装不上。请检查网络或代理，然后手动重试：" -ForegroundColor Red
    Write-Host "    `"$Py`" -m pip install -r `"$reqFile`""
    exit 1
}

# ---------------------------------------------------------- ffmpeg
Write-Host ""
Write-Host "  ffmpeg 检查"
$ffOk = $false
if ((Get-Command ffmpeg -ErrorAction SilentlyContinue) -and (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    $ffOk = $true
    $ffVer = (& ffmpeg -version 2>$null | Select-Object -First 1)
    Write-Host "    OK  $ffVer"
}

if (-not $ffOk) {
    Write-Host "    X 未找到 ffmpeg，抽音轨和压制都会失败。" -ForegroundColor Red
    Write-Host "      winget install Gyan.FFmpeg"
    Write-Host "      或 choco install ffmpeg"
    Write-Host "      装完重开一个终端（让 PATH 生效），然后重跑本脚本。"
}

# ---------------------------------------------------------- 4. 模型
Write-Host ""
Write-Host "[4/4] 识别模型"

if ($WithModel) {
    $envLines = @(
        "import sys",
        "sys.path.insert(0, r'$Root\scripts')",
        "import env",
        "env.ensure_model('$Model')"
    ) -join "`n"
    & $Py -c $envLines
} else {
    Write-Host "  跳过（首次使用时会自动下载）"
    Write-Host "  想现在就下好可以重跑：.\install.ps1 -WithModel"
}

# ---------------------------------------------------------- 自检
Write-Host ""
& $Py (Join-Path $Root "scripts\env.py")

Write-Host ""
Write-Host "安装完成。把视频丢给 AI 助手，或者直接跑："
Write-Host "  `"$Py`" `"$Root\scripts\run_all.py`" <你的视频>"
Write-Host ""
