param(
    [Parameter(Mandatory = $true)]
    [string]$Model,
    [Parameter(Mandatory = $true)]
    [int]$Year,
    [string]$Script = $null,
    [string]$OutDir = (Join-Path $env:LOCALAPPDATA 'revit-downgrade-test\dumps'),
    [switch]$NoGeometry,
    [switch]$Trace,
    [string]$SkipGeometryCategories = '',
    [string]$SkipParamCategories = ''
)

if (-not $Script) {
    $scriptRoot = $PSScriptRoot
    if (-not $scriptRoot) {
        $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
    }
    $Script = Join-Path $scriptRoot 'dump_model.py'
}

if ($Trace) {
    $env:RVT_DUMP_TRACE = '1'
} else {
    Remove-Item Env:RVT_DUMP_TRACE -ErrorAction SilentlyContinue
}
$env:RVT_DUMP_SKIP_GEOMETRY_CATEGORIES = $SkipGeometryCategories
$env:RVT_DUMP_SKIP_PARAM_CATEGORIES = $SkipParamCategories

$env:RVT_DUMP_OUT = $OutDir
$env:RVT_DUMP_MODEL = (Resolve-Path -LiteralPath $Model -ErrorAction Stop).Path
if ($NoGeometry) {
    $env:RVT_DUMP_GEOMETRY = '0'
} else {
    Remove-Item Env:RVT_DUMP_GEOMETRY -ErrorAction SilentlyContinue
}

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
