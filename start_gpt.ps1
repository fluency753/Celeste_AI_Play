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
    [string]$Save = '',
    [Nullable[int]]$SaveSlot = $null,
    [switch]$CreateSave,
    [ValidateSet('chapter','campaign')][string]$Scope = 'chapter'
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$env:PYTHONIOENCODING = 'utf-8'
$extra = @('--effort', $Effort, '--knowledge', $Knowledge)
if ($Model) { $extra += @('--model', $Model) }
if (-not $Play) {
    & $py -X utf8 -m orchestrator.preparation --agent gpt-native @extra
} else {
    if ($Resume -and $Prepared) { throw '-Resume 和 -Prepared 不能同时使用。' }
    if (-not $Resume -and -not $Prepared) { throw '请用 -Prepared 指定准备目录，或用 -Resume 指定续跑目录。' }
    if (-not $Resume) {
        & $py -X utf8 -m orchestrator.prepared $Prepared --agent gpt-native --knowledge $Knowledge
        if ($LASTEXITCODE -ne 0) { throw '准备会话校验失败；游戏、HUD 和 OBS 未改动。' }
    }
    $chapterArgs = if ($Chapters) { @('--chapters', $Chapters) } else { @('--chapter', $Chapter) }
    $runArgs = @('--engine', 'codex', '--agent', 'gpt-native',
                 '--keep-obs-open', '--playback', '--continuous-music')
    if (-not $Resume) {
        if (-not $Save.Trim()) { throw '请显式指定 -Save 玩家名；不再默认使用 test。' }
        if ($CreateSave) {
            if ($null -ne $SaveSlot) { throw '-CreateSave 不能同时指定 -SaveSlot。' }
            $runArgs += @('--create-save')
        } elseif ($null -eq $SaveSlot) { throw '使用已有存档时请显式指定 -SaveSlot 槽位编号。' }
        $runArgs += @('--save', $Save, '--scope', $Scope)
        if ($null -ne $SaveSlot) { $runArgs += @('--save-slot', $SaveSlot.ToString()) }
    } elseif ($Save -or $CreateSave -or $null -ne $SaveSlot) {
        throw '续跑使用原存档，不接受 -Save、-SaveSlot 或 -CreateSave。'
    }
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
