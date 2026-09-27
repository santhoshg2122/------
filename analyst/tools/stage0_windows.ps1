# Stage 0 on the user's Windows PC: copy the MT5 exports in, install deps, run the integrity check.
#   powershell -ExecutionPolicy Bypass -File tools\stage0_windows.ps1
#   powershell -ExecutionPolicy Bypass -File tools\stage0_windows.ps1 -Source "D:\MT5 exports"
param(
    [string]$Source = "$env:USERPROFILE\Documents\Trading Journal\data\hist"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Raw = Join-Path $Root "data\raw"

if (-not (Test-Path $Source)) { throw "Source folder not found: $Source  (pass -Source <folder with the EURUSD CSVs>)" }
$files = Get-ChildItem -Path $Source -Recurse -File -Include *.csv, *.txt |
         Where-Object { $_.Name -match 'EURUSD|DXY|USDX' }
if (-not $files) { throw "No CSV/TXT with EURUSD, DXY or USDX in its name under $Source" }

Write-Host "Copying $($files.Count) file(s) from $Source to $Raw"
New-Item -ItemType Directory -Force -Path $Raw | Out-Null
$files | ForEach-Object { Copy-Item $_.FullName -Destination $Raw -Force; Write-Host "  $($_.Name)  $([math]::Round($_.Length/1MB,1)) MB" }

Push-Location $Root
try {
    python -m pip install -q -r requirements.txt
    python run.py stage0
    Write-Host ""
    Write-Host "Report: $Root\reports\data_integrity.md  (HUMAN GATE H1)"
} finally { Pop-Location }
