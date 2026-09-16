<#
.SYNOPSIS
  Omni3D 统一入口：自动选用带依赖的 Python 解释器，并给出可操作的错误提示。

.EXAMPLE
  .\run.ps1 server                 # 启动重建服务器（默认 127.0.0.1:50865）
  .\run.ps1 desktop                # 启动桌面客户端（PyQt5 + VTK）
  .\run.ps1 test                   # 跑单元 + 集成测试
  .\run.ps1 bench                  # 重建速度/质量基准
  .\run.ps1 bench --frames 4 8     # 透传参数给基准脚本

.NOTES
  解释器解析顺序：$env:OMNI3D_PY → 常见 conda 环境路径 → PATH 里的 python。
#>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("server", "desktop", "test", "bench")]
    [string]$Target,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"

function Resolve-Python {
    if ($env:OMNI3D_PY -and (Test-Path $env:OMNI3D_PY)) { return $env:OMNI3D_PY }
    $candidates = @(
        "D:\anaconda3\envs\Omni3D\python.exe",
        (Join-Path $env:USERPROFILE "anaconda3\envs\Omni3D\python.exe"),
        (Join-Path $env:USERPROFILE "miniconda3\envs\Omni3D\python.exe")
    )
    if ($env:CONDA_PREFIX) { $candidates += (Join-Path $env:CONDA_PREFIX "python.exe") }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    return "python"
}

$py = Resolve-Python
Write-Host "python = $py" -ForegroundColor DarkGray

# 依赖自检（注意：必须先 import torch —— 本机 fbgemm.dll 加载顺序冲突）
$probeOk = $false
try {
    & $py -c "import torch, fastapi" 2>&1 | Out-Null
    $probeOk = ($LASTEXITCODE -eq 0)
}
catch {
    $probeOk = $false
}
if (-not $probeOk) {
    Write-Host ""
    Write-Host "✗ 该解释器缺少依赖（torch / fastapi）。" -ForegroundColor Yellow
    Write-Host "  任选其一：" -ForegroundColor Yellow
    Write-Host "    1) conda activate Omni3D    # 激活后再运行本脚本"
    Write-Host "    2) `$env:OMNI3D_PY = 'C:\path\to\envs\Omni3D\python.exe'"
    Write-Host "    3) & '$py' -m pip install -r requirements-app.txt"
    Write-Host ""
    Write-Host "  提示：全局 python 通常**没有**本项目的依赖，需要用项目 conda 环境。" -ForegroundColor DarkGray
    exit 1
}

switch ($Target) {
    "server" { & $py (Join-Path $root "web\server.py") @Rest }
    "desktop" { & $py (Join-Path $root "desktop\main.py") @Rest }
    "test" { & $py -m pytest (Join-Path $root "tests") -q @Rest }
    "bench" { & $py (Join-Path $root "scripts\bench_reconstruct.py") @Rest }
}
exit $LASTEXITCODE
