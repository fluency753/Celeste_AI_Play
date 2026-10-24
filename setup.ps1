# Celeste_AI_Play 一键部署（Windows PowerShell 5.1 或 PowerShell 7）
# 用法：在仓库根目录右键"用 PowerShell 运行"，或在 PowerShell 里执行：
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1            # 交互式部署
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Check     # 只检查，不改动
# 这个脚本只负责 Python 与虚拟环境；其余步骤（游戏与 Everest、.NET、桥接 Mod、Docker 与镜像、智能体平台与密钥、
# OBS 与录制、预置模拟器、知识库、存档、开关与启动脚本、自检）由 tools\setup_wizard.py 交互完成。详见 docs\DEPLOY.md。
param([switch]$Check, [string]$Only = "")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
$env:PYTHONIOENCODING = "utf-8"

function Invoke-Py([string[]]$Cmd, [string[]]$Rest) {
    # $Cmd：例如 @("py", "-3.13") 或 @("python")
    $exe = $Cmd[0]
    $pre = if ($Cmd.Length -gt 1) { $Cmd[1..($Cmd.Length - 1)] } else { @() }
    & $exe @pre @Rest
}

function Find-Python {
    foreach ($cmd in @(@("py", "-3.13"), @("py", "-3.12"), @("py", "-3.11"), @("python"))) {
        try {
            $ver = Invoke-Py $cmd @("-c", "import sys; print('%d.%d' % sys.version_info[:2])") 2>$null
            if ($LASTEXITCODE -eq 0 -and "$ver" -match '^3\.(\d+)$' -and [int]$Matches[1] -ge 11) { return ,$cmd }
        } catch { }
    }
    return $null
}

Write-Host "== Celeste_AI_Play 部署：Python 与虚拟环境 =="
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    $python = Find-Python
    if (-not $python) {
        if ($Check) { Write-Host "  [缺少] Python 3.11 或更新"; exit 1 }
        Write-Host "  没有找到 Python 3.11 或更新的版本。"
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            $yes = Read-Host "  用 winget 安装 Python 3.13？(Y/n)"
            if ($yes -eq "" -or $yes -match '^[yY是]') {
                winget install --id Python.Python.3.13 -e --scope user --accept-source-agreements --accept-package-agreements
                $env:PATH = [Environment]::GetEnvironmentVariable("PATH", "User") + ";" + [Environment]::GetEnvironmentVariable("PATH", "Machine")
                $python = Find-Python
            }
        }
        if (-not $python) { throw "请先安装 Python 3.11 或更新（https://www.python.org/downloads/，勾选 Add to PATH），然后重新运行 setup.ps1" }
    }
    Write-Host "  建立虚拟环境 .venv（$($python -join ' ')）"
    Invoke-Py $python @("-m", "venv", ".venv")
    if ($LASTEXITCODE -ne 0) { throw "建立虚拟环境失败" }
}
if (-not $Check) {
    Write-Host "  安装依赖（requirements.txt）"
    & $venvPy -m pip install --disable-pip-version-check -q -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "安装依赖失败" }
}
Write-Host "  [就绪] .venv"

$wizardArgs = @()
if ($Check) { $wizardArgs += "--check" }
if ($Only) { $wizardArgs += @("--only", $Only) }
& $venvPy -X utf8 tools\setup_wizard.py @wizardArgs
exit $LASTEXITCODE
