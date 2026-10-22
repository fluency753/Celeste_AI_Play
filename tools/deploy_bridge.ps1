# 构建 CelesteAIBridge 并安装到游戏的 Mods 目录。
# 用法（PowerShell）：
#   .\tools\deploy_bridge.ps1 -CelesteDir "D:\path\to\steamapps\common\Celeste"
# 需要 .NET 8 SDK。游戏运行中无法覆盖 DLL，请先关闭游戏。
param(
    [Parameter(Mandatory = $true)][string]$CelesteDir,
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$project = Join-Path $repo "game-bridge"

if (-not (Test-Path (Join-Path $CelesteDir "Celeste.dll"))) {
    throw "在 $CelesteDir 下找不到 Celeste.dll，请检查游戏目录"
}

dotnet build (Join-Path $project "CelesteAIBridge.csproj") -c $Configuration "-p:CelestePrefix=$CelesteDir"
if ($LASTEXITCODE -ne 0) { throw "构建失败" }

$target = Join-Path $CelesteDir "Mods\CelesteAIBridge"
New-Item -ItemType Directory -Force -Path (Join-Path $target "bin") | Out-Null
Copy-Item (Join-Path $project "everest.yaml") $target -Force
Copy-Item (Join-Path $project "bin\CelesteAIBridge.dll") (Join-Path $target "bin") -Force
Copy-Item (Join-Path $project "bin\CelesteAIBridge.pdb") (Join-Path $target "bin") -Force
Write-Host "已安装到 $target"
