# Stage 0 on the user's Windows PC, fully local (no GitHub):
#   powershell -ExecutionPolicy Bypass -File tools\stage0_windows.ps1
#   powershell -ExecutionPolicy Bypass -File tools\stage0_windows.ps1 -File "D:\other\EURUSD_M1.csv" [-DxyFile "...\DXY_M1.csv"]
# Uses exactly the file(s) given: never mixes timeframes (the EURUSD_data folder also holds 3m/15m/1h/4h files).
param(
    [string]$File = "$env:USERPROFILE\Documents\EURUSD_data\EURUSD_1m_NY.csv",
    [string]$DxyFile = ""
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Raw = Join-Path $Root "data\raw"

if (-not (Test-Path $File)) { throw "EURUSD file not found: $File  (pass -File <path to the M1 csv>)" }
New-Item -ItemType Directory -Force -Path $Raw | Out-Null
# Clear earlier inputs so only this run's files are ingested
Get-ChildItem $Raw -File | Where-Object { $_.Name -match 'EURUSD|DXY|USDX' } | Remove-Item -Force

Write-Host "Copying $File"
Copy-Item $File -Destination (Join-Path $Raw "EURUSD_M1.csv") -Force
if ($DxyFile) {
    if (-not (Test-Path $DxyFile)) { throw "DXY file not found: $DxyFile" }
    Write-Host "Copying $DxyFile"
    Copy-Item $DxyFile -Destination (Join-Path $Raw "DXY_M1.csv") -Force
}

$Py = if (Get-Command python -ErrorAction SilentlyContinue) { "python" } elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { throw "Python not found: install it from python.org (tick 'Add to PATH')" }
Push-Location $Root
try {
    & $Py -m pip install -q -r requirements.txt
    & $Py run.py stage0
    Write-Host ""
    Write-Host "Full report: $Root\reports\data_integrity.md  (HUMAN GATE H1)"
} finally { Pop-Location }
