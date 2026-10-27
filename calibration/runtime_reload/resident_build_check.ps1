# Opt-in native build regression; never installs or connects to a game.
param([Parameter(Mandatory = $true)][string]$CelesteDir)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$project = Join-Path $repo 'game-runtime\CelesteAIRuntime.csproj'
$binary = Join-Path $repo 'game-runtime\bin\CelesteAIRuntime.dll'
$baseline = $null
function Build-Resident([string]$revision) {
    $buildArgs = @('build', $project, '-t:Rebuild', '-c', 'Release', "-p:CelestePrefix=$CelesteDir")
    if ($revision) { $buildArgs += "-p:SourceRevisionId=$revision" }
    & dotnet @buildArgs
    if ($LASTEXITCODE -ne 0) { throw 'Resident build failed' }
}
try {
    foreach ($revision in @('1111111111111111111111111111111111111111', '2222222222222222222222222222222222222222')) {
        Build-Resident $revision
        $hash = (Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash
        if ($baseline -and $hash -ne $baseline) { throw "Git metadata changed the resident DLL hash: $baseline -> $hash" }
        $baseline = $hash
    }
} finally {
    # Restore ordinary artifacts even when the synthetic-revision check fails.
    Build-Resident ''
}
if ((Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash -ne $baseline) { throw 'Current checkout changed the resident DLL hash' }
Write-Output "PASS: two synthetic revisions and the current checkout produce the same resident DLL: $baseline"
