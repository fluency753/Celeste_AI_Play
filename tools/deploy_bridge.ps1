# 构建 CelesteAIBridge 并安装到游戏的 Mods 目录。
# 用法（PowerShell）：
#   .\tools\deploy_bridge.ps1 -CelesteDir "D:\path\to\steamapps\common\Celeste"
# 游戏运行且 Runtime 字节相同时自动热重载；不因主控已结束而重启游戏。
# 回滚（关闭游戏后）：-Rollback "<部署时打印的备份目录>"，先把当前两个 Mod 移到游戏目录的 CelesteAI-backups，
# 再还原备份里的 Mod；备份里没有常驻核心（例如回到 1.1.x）时，核心只移走不还原，避免旧桥接与核心争用端口。
param(
    [Parameter(Mandatory = $true)][string]$CelesteDir,
    [string]$Configuration = "Release",
    [switch]$HotReload,
    [int]$HudPort = 47792,
    [int]$BridgePort = 0,
    [Nullable[int]]$SaveSlot = $null,
    [string]$ComponentRoot = "",
    [string]$Rollback = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$project = Join-Path $repo "game-bridge"
$runtimeProject = Join-Path $repo "game-runtime"

if (-not (Test-Path (Join-Path $CelesteDir "Celeste.dll"))) {
    throw "在 $CelesteDir 下找不到 Celeste.dll，请检查游戏目录"
}

if ($HotReload -and $Rollback) {
    throw "-Rollback 只能在关闭游戏后单独使用，不能与 -HotReload 同时使用"
}

# 只看部署目标目录里的游戏：AI 专用副本可以在个人的《蔚蓝》开着时部署（反过来也一样）。
# 取不到路径的 Celeste.exe 按在运行处理；Everest 也可能经它私有的 dotnet 宿主运行，那种情况一律按在运行处理。
$gameRoot = (Resolve-Path -LiteralPath $CelesteDir).Path.TrimEnd('\') + '\'
function Get-RunningTargetGame {
    Get-CimInstance Win32_Process -Filter "Name='Celeste.exe' OR Name='dotnet.exe'" | Where-Object {
    ($_.Name -eq 'Celeste.exe' -and (-not $_.ExecutablePath -or $_.ExecutablePath.StartsWith($gameRoot, [StringComparison]::OrdinalIgnoreCase))) -or
    ($_.Name -eq 'dotnet.exe' -and $_.CommandLine -match '(?i)(?:^|[\\/"\s])Celeste\.dll(?:["\s]|$)')
    }
}
$runningGame = Get-RunningTargetGame
if ($runningGame -and $Rollback) {
    throw "离线回滚要求游戏已关闭；未自动退出游戏，请使用兼容构建进行受控重载"
}

if ($Rollback) {
    $source = (Resolve-Path -LiteralPath $Rollback).Path
    if (-not (Test-Path -LiteralPath (Join-Path $source "CelesteAIBridge\everest.yaml"))) {
        throw "回滚目录里没有 CelesteAIBridge 备份：$($source)"
    }
    # 与 Mods 同一磁盘、但不在 Mods 里（Everest 不会把它当成 Mod 加载）；只移动不删除
    $aside = Join-Path $CelesteDir ("CelesteAI-backups\" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-before-rollback")
    New-Item -ItemType Directory -Force -Path $aside | Out-Null
    foreach ($module in @("CelesteAIBridge", "CelesteAIRuntime")) {
        $installed = Join-Path $CelesteDir ("Mods\" + $module)
        if (Test-Path -LiteralPath $installed) {
            Move-Item -LiteralPath $installed -Destination (Join-Path $aside $module)
        }
    }
    foreach ($module in @("CelesteAIBridge", "CelesteAIRuntime")) {
        $saved = Join-Path $source $module
        if (Test-Path -LiteralPath $saved) {
            Copy-Item -LiteralPath $saved -Destination (Join-Path $CelesteDir ("Mods\" + $module)) -Recurse
            Write-Host "已从备份还原 $($module)"
        } else {
            Write-Host "备份里没有 $($module)：当前版本已移到 $($aside)，不再加载"
        }
    }
    Write-Host "回滚前的 Mod 已移到 $($aside)（未删除）；回到 1.3.0 之前的桥接时，主控也需回到对应版本"
    exit 0
}

if ($ComponentRoot) {
    $ComponentRoot = (Resolve-Path -LiteralPath $ComponentRoot).Path
    $python = Join-Path $repo ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) { $python = "python" }
    & $python -X utf8 (Join-Path $repo "tools\component_package.py") --root $repo --validate $ComponentRoot
    if ($LASTEXITCODE -ne 0) { throw "预编译组件校验失败；未替换任何组件" }
    $project = Join-Path $ComponentRoot "mods\CelesteAIBridge"
    $runtimeProject = Join-Path $ComponentRoot "mods\CelesteAIRuntime"
} else {
    dotnet build (Join-Path $project "CelesteAIBridge.csproj") -c $Configuration "-p:CelestePrefix=$CelesteDir"
    if ($LASTEXITCODE -ne 0) { throw "构建失败" }
}

# 构建期间用户可能打开了游戏，不能沿用构建前的离线快照覆盖在用文件。
$runningGame = Get-RunningTargetGame

if ($HotReload -or $runningGame) {
    $runtimeBuilt = Join-Path $runtimeProject "bin\CelesteAIRuntime.dll"
    $runtimeInstalled = Join-Path $CelesteDir "Mods\CelesteAIRuntime\bin\CelesteAIRuntime.dll"
    if (-not (Test-Path -LiteralPath $runtimeInstalled)) {
        throw "尚未安装常驻核心；首次安装必须维护重启，不能不停录升级"
    }
    if ((Get-FileHash -LiteralPath $runtimeBuilt).Hash -ne (Get-FileHash -LiteralPath $runtimeInstalled).Hash) {
        throw "常驻核心构建与已安装版本不同；本次需要维护重启，未替换任何在用文件"
    }
    $python = Join-Path $repo ".venv\Scripts\python.exe"
    if ($BridgePort -eq 0) {
        $BridgePort = 47774
        $runtimeSettingsPath = Join-Path $CelesteDir "Saves\modsettings-CelesteAIRuntime.celeste"
        if (Test-Path -LiteralPath $runtimeSettingsPath) {
            $match = [regex]::Match([IO.File]::ReadAllText($runtimeSettingsPath), '(?m)^Port:\s*(\d+)\s*$')
            if ($match.Success) { $BridgePort = [int]$match.Groups[1].Value }
        }
    }
    $reloadArguments = @('-X', 'utf8', (Join-Path $repo "tools\reload_bridge.py"), '--hud-port', "$HudPort",
        '--bridge-port', "$BridgePort", '--game-dir', $CelesteDir,
        '--dll', (Join-Path $project "bin\CelesteAIBridge.dll"), '--runtime-dll', $runtimeBuilt)
    if ($null -ne $SaveSlot) { $reloadArguments += @('--save-slot', "$SaveSlot") }
    & $python @reloadArguments
    if ($LASTEXITCODE -ne 0) { throw "受控重载未完成；游戏保持暂停，请查看主控提示及保留的交接记录" }
    exit 0
}

if (Get-RunningTargetGame) { throw "游戏刚刚启动；未离线覆盖组件，请重试以执行受控重载" }
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
