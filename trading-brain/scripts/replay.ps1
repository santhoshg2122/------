# Windows wrapper for the history replay (B013). Run one batch; run again to continue from the checkpoint.
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\replay.ps1 -From 2025-09-29 -To 2026-09-25 [-MaxSessions 60] [-MaxCost 100]
param(
    [Parameter(Mandatory = $true)][string]$From,
    [Parameter(Mandatory = $true)][string]$To,
    [int]$MaxSessions = 60,
    [double]$MaxCost = 100
)
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
New-Item -ItemType Directory -Force -Path logs | Out-Null
$Py = if (Get-Command python -ErrorAction SilentlyContinue) { "python" } else { "py" }
& $Py scripts\replay.py --from $From --to $To --max-sessions $MaxSessions --max-cost $MaxCost 2>&1 | Tee-Object -Append logs\replay.log
exit $LASTEXITCODE
