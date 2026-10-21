param(
    [Parameter(Mandatory = $true)]
    [string]$Model,
    [Parameter(Mandatory = $true)]
    [int]$Year,
    [Parameter(Mandatory = $true)]
    [string]$OutDir,
    [Parameter(Mandatory = $true)]
    [string]$Tag,
    [Parameter(Mandatory = $true)]
    [string]$Uids,
    [string]$ContextUids = '',
    [double]$PadMm = 15000
)

$scriptRoot = $PSScriptRoot
if (-not $scriptRoot) {
    $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$Script = Join-Path $scriptRoot 'export_focus.py'
$env:RVT_DUMP_MODEL = (Resolve-Path -LiteralPath $Model -ErrorAction Stop).Path
$OutDir = [System.IO.Path]::GetFullPath($OutDir)
$env:RVT_FOCUS_OUT = $OutDir
$env:RVT_FOCUS_TAG = $Tag
$env:RVT_FOCUS_UIDS = $Uids
$env:RVT_FOCUS_CONTEXT_UIDS = $ContextUids
$env:RVT_FOCUS_PAD_MM = $PadMm.ToString([System.Globalization.CultureInfo]::InvariantCulture)

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
