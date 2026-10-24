# 独立原生 GPT。默认只准备知识库，不操作游戏；明确加 -Play 才开始试玩。
param(
    [switch]$Play,
    [switch]$Foreground,
    [string]$Resume = "",
    [string]$Prepared = "",
    [string]$Model = "",
    [ValidateSet('explore','full')][string]$Knowledge = 'explore',
    [ValidateSet('low','medium','high','xhigh','max')][string]$Effort = 'medium',
    [int]$Chapter = 1,
    [string]$Chapters = '',
    [string]$Save = 'test'
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$env:PYTHONIOENCODING = 'utf-8'
$extra = @('--effort', $Effort, '--knowledge', $Knowledge)
if ($Model) { $extra += @('--model', $Model) }
if (-not $Play) {
    & $py -X utf8 -m orchestrator.codex_prepare --agent gpt-native @extra
} else {
    if ($Resume -and $Prepared) { throw '-Resume 和 -Prepared 不能同时使用。' }
    if (-not $Resume -and -not $Prepared) { throw '请用 -Prepared 指定准备目录，或用 -Resume 指定续跑目录。' }
    if (-not $Resume) {
        & $py -X utf8 -m orchestrator.codex_prepared $Prepared --agent gpt-native --knowledge $Knowledge
        if ($LASTEXITCODE -ne 0) { throw '准备会话校验失败；游戏、HUD 和 OBS 未改动。' }
    }
    # 只交接本仓库旧的独立 HUD 服务；桌面窗口和 OBS 继续使用相同地址。
    $hudListener = Get-NetTCPConnection -State Listen -LocalPort 47790 -ErrorAction SilentlyContinue
    foreach ($hudOwner in ($hudListener.OwningProcess | Select-Object -Unique)) {
        $hudProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$hudOwner"
        if ($hudProcess.CommandLine -like "*$PSScriptRoot*" -and
            $hudProcess.CommandLine -match '\-m\s+overlay\.server\b' -and
            $hudProcess.CommandLine -match '\-\-codex-thread\b') {
            Stop-Process -Id $hudOwner
        }
    }
    $chapterArgs = if ($Chapters) { @('--chapters', $Chapters) } else { @('--chapter', $Chapter) }
    $runArgs = @('--engine', 'codex', '--agent', 'gpt-native', '--save', $Save,
                 '--keep-obs-open', '--playback', '--continuous-music')
    if ($Resume) {
        $runArgs += @('--resume', $Resume)
        if ($PSBoundParameters.ContainsKey('Effort')) { $runArgs += @('--effort', $Effort) }
        if ($Model) { $runArgs += @('--model', $Model) }
    } else {
        $runArgs += $chapterArgs + @('--prepared', $Prepared) + $extra
    }
    if ($Foreground) {
        & $py -X utf8 -m orchestrator.run @runArgs
    } else {
        & $py -X utf8 -m orchestrator.background -- @runArgs
    }
}
exit $LASTEXITCODE
