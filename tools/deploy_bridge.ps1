# 构建 CelesteAIBridge 并安装到游戏的 Mods 目录。
# 用法（PowerShell）：
#   .\tools\deploy_bridge.ps1 -CelesteDir "D:\path\to\steamapps\common\Celeste"
# 默认维护部署需关闭游戏；-HotReload 仅替换已安装常驻核心所支持的桥接逻辑。
param(
    [Parameter(Mandatory = $true)][string]$CelesteDir,
    [string]$Configuration = "Release",
    [switch]$HotReload,
    [int]$HudPort = 47792
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$project = Join-Path $repo "game-bridge"
$runtimeProject = Join-Path $repo "game-runtime"

if (-not (Test-Path (Join-Path $CelesteDir "Celeste.dll"))) {
    throw "在 $CelesteDir 下找不到 Celeste.dll，请检查游戏目录"
}

if (-not $HotReload) {
    # Everest can also run through its private dotnet host rather than Celeste.exe.
    $runningGame = Get-CimInstance Win32_Process -Filter "Name='Celeste.exe' OR Name='dotnet.exe'" | Where-Object {
        $_.Name -eq 'Celeste.exe' -or $_.CommandLine -match '(?i)(?:^|[\\/"\s])Celeste\.dll(?:["\s]|$)'
    }
    if ($runningGame) {
        throw "维护部署需要先安全关闭游戏；已安装常驻核心的兼容逻辑更新请用 -HotReload"
    }
}

dotnet build (Join-Path $project "CelesteAIBridge.csproj") -c $Configuration "-p:CelestePrefix=$CelesteDir"
if ($LASTEXITCODE -ne 0) { throw "构建失败" }

if ($HotReload) {
    $runtimeBuilt = Join-Path $runtimeProject "bin\CelesteAIRuntime.dll"
    $runtimeInstalled = Join-Path $CelesteDir "Mods\CelesteAIRuntime\bin\CelesteAIRuntime.dll"
    if (-not (Test-Path -LiteralPath $runtimeInstalled)) {
        throw "尚未安装常驻核心；首次安装必须维护重启，不能不停录升级"
    }
    if ((Get-FileHash -LiteralPath $runtimeBuilt).Hash -ne (Get-FileHash -LiteralPath $runtimeInstalled).Hash) {
        throw "常驻核心构建与已安装版本不同；本次需要维护重启，未替换任何在用文件"
    }
    $python = Join-Path $repo ".venv\Scripts\python.exe"
    & $python -X utf8 (Join-Path $repo "tools\reload_bridge.py") --hud-port $HudPort --dll (Join-Path $project "bin\CelesteAIBridge.dll")
    if ($LASTEXITCODE -ne 0) { throw "受控重载未完成；游戏保持暂停，请查看主控提示及保留的交接记录" }
    exit 0
}

$deploymentBackup = Join-Path $repo ("local\backups\" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-bridge-runtime-deploy")
New-Item -ItemType Directory -Force -Path $deploymentBackup | Out-Null
foreach ($module in @("CelesteAIBridge", "CelesteAIRuntime")) {
    $installed = Join-Path $CelesteDir ("Mods\" + $module)
    if (Test-Path -LiteralPath $installed) {
        Copy-Item -LiteralPath $installed -Destination (Join-Path $deploymentBackup $module) -Recurse
    }
}

$runtimeTarget = Join-Path $CelesteDir "Mods\CelesteAIRuntime"
New-Item -ItemType Directory -Force -Path (Join-Path $runtimeTarget "bin") | Out-Null
Copy-Item (Join-Path $runtimeProject "everest.yaml") $runtimeTarget -Force
Copy-Item (Join-Path $runtimeProject "bin\CelesteAIRuntime.dll") (Join-Path $runtimeTarget "bin") -Force
Copy-Item (Join-Path $runtimeProject "bin\CelesteAIRuntime.pdb") (Join-Path $runtimeTarget "bin") -Force

# 首次安装沿用原桥接的自定义端口；不覆盖用户已有的常驻核心设置。
$runtimeSettings = Join-Path $CelesteDir "Saves\modsettings-CelesteAIRuntime.celeste"
$legacySettings = Join-Path $CelesteDir "Saves\modsettings-CelesteAIBridge.celeste"
if (-not (Test-Path -LiteralPath $runtimeSettings) -and (Test-Path -LiteralPath $legacySettings)) {
    $legacyText = [IO.File]::ReadAllText($legacySettings)
    $portLines = [regex]::Matches($legacyText, '(?m)^(?:Port|ClockPort):\s*\d+\s*$') | ForEach-Object { $_.Value.Trim() }
    if ($portLines) { [IO.File]::WriteAllText($runtimeSettings, (($portLines -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false))) }
}

$target = Join-Path $CelesteDir "Mods\CelesteAIBridge"
New-Item -ItemType Directory -Force -Path (Join-Path $target "bin") | Out-Null
Copy-Item (Join-Path $project "everest.yaml") $target -Force
Copy-Item (Join-Path $project "bin\CelesteAIBridge.dll") (Join-Path $target "bin") -Force
Copy-Item (Join-Path $project "bin\CelesteAIBridge.pdb") (Join-Path $target "bin") -Force
Write-Host "已安装到 $target"
Write-Host "常驻核心已安装到 $runtimeTarget；原文件备份：$deploymentBackup"
