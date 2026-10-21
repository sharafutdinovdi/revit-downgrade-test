param(
    [Parameter(Mandatory = $true)]
    [string]$Model,
    [Parameter(Mandatory = $true)]
    [int]$Year,
    [string]$OutDir = (Join-Path $env:LOCALAPPDATA 'revit-downgrade-test\families')
)

$scriptRoot = $PSScriptRoot
if (-not $scriptRoot) {
    $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$Script = Join-Path $scriptRoot 'extract_families.py'

$env:RVT_DUMP_MODEL = (Resolve-Path -LiteralPath $Model -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force -ErrorAction Stop | Out-Null
}
$OutDir = (Resolve-Path -LiteralPath $OutDir -ErrorAction Stop).Path
$env:RVT_EXTRACT_OUT = $OutDir

$command = Get-Command pyrevit -ErrorAction SilentlyContinue
if ($command) {
    $pyrevit = $command.Source
} else {
    $pyrevit = Join-Path $env:APPDATA 'pyRevit-Master\bin\pyrevit.exe'
}

$timer = [System.Diagnostics.Stopwatch]::StartNew()
$output = @(& $pyrevit run $Script ('--revit=' + $Year) 2>&1)
$exitCode = $LASTEXITCODE
$timer.Stop()
$output | ForEach-Object { Write-Output $_.ToString() }
Write-Output ('ELAPSED_S=' + $timer.Elapsed.TotalSeconds.ToString('F3', [System.Globalization.CultureInfo]::InvariantCulture))

$execution = [regex]::Match(($output -join [Environment]::NewLine), 'Execution Id:\s*"([^"]+)"')
if ($execution.Success) {
    $id = $execution.Groups[1].Value
    $log = Join-Path (Join-Path $env:TEMP $id) ('PyRevitRunner_' + $id + '.log')
    if (Test-Path -LiteralPath $log) {
        Get-Content -LiteralPath $log -Tail 80
    } else {
        Write-Warning ('Runner log not found: ' + $log)
    }
}

if (Test-Path -LiteralPath $OutDir) {
    Get-ChildItem -LiteralPath $OutDir -File | Select-Object Name, Length | Format-Table -AutoSize
}
exit $exitCode
