# Windows runner (Task Scheduler entry). Plan section 10, adapted for PowerShell.
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_cycle.ps1 -Session london [-Date 2026-09-29]
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_cycle.ps1 -Weekly
# The session date is today's UTC date for all three closes (NY closes 21:00 UTC = 02:30 IST, still that UTC day).
param(
    [ValidateSet("asia", "london", "newyork")][string]$Session,
    [string]$Date = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd"),
    [switch]$Weekly,
    [switch]$NoExport
)
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
New-Item -ItemType Directory -Force -Path logs, data\packets, runs | Out-Null
$Py = if (Get-Command python -ErrorAction SilentlyContinue) { "python" } else { "py" }
$Allowed = "Read,Write,Edit,Agent,Bash(python *),Bash(python3 *),Bash(py *),Bash(mkdir *)"

function Fail($msg) { Add-Content logs\failures.log "$(Get-Date -Format s) $msg"; exit 1 }

if ($Weekly) {
    claude -p "/weekly-review" --model opus --permission-mode acceptEdits --permission-prompts none `
        --allowedTools $Allowed --output-format json > logs\weekly_$Date.json 2> logs\weekly_$Date.err
    if ($LASTEXITCODE -ne 0) { Fail "weekly-review failed ($LASTEXITCODE)" }
    exit 0
}
if (-not $Session) { Fail "no -Session given" }

# 1. export the last 3 days (+60 days history) of M1 bars for both pairs, UTC
if (-not $NoExport) {
    & $Py scripts\export_from_mt5.py --pairs EURUSD,GBPUSD --days 3 --history-days 60 2>> logs\export.err
    if ($LASTEXITCODE -ne 0) { Add-Content logs\failures.log "$(Get-Date -Format s) export failed: $Session $Date (packet will say MISSING/STALE)" }
}
# 2. build the packet (writes status STALE/GAP/MISSING instead of failing)
& $Py scripts\build_packet.py --session $Session --date $Date
# 3. run the chain unattended: main session on Opus, subagents per their own model lines
claude -p "/cycle $Session $Date" --model opus --permission-mode acceptEdits --permission-prompts none `
    --allowedTools $Allowed --output-format json > "logs\${Date}_${Session}.json" 2> "logs\${Date}_${Session}.err"
if ($LASTEXITCODE -ne 0) { Fail "cycle failed: $Session $Date (exit $LASTEXITCODE)" }
