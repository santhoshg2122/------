#!/usr/bin/env bash
# Git Bash / Linux runner. Same steps as run_cycle.ps1. Usage: scripts/run_cycle.sh <asia|london|newyork> [date]
set -uo pipefail
SESSION="$1"
DATE="${2:-$(date -u +%F)}"          # the session's own UTC date for all three closes
cd "$(dirname "$0")/.."
mkdir -p logs data/packets runs
PY=$(command -v python3 || command -v python)
ALLOWED="Read,Write,Edit,Agent,Bash(python *),Bash(python3 *),Bash(mkdir *)"
$PY scripts/export_from_mt5.py --pairs EURUSD,GBPUSD --days 3 --history-days 60 2>> logs/export.err \
  || echo "$(date -u +%FT%TZ) export failed: $SESSION $DATE" >> logs/failures.log
$PY scripts/build_packet.py --session "$SESSION" --date "$DATE"
claude -p "/cycle $SESSION $DATE" --model opus --permission-mode acceptEdits --permission-prompts none \
  --allowedTools "$ALLOWED" --output-format json \
  > "logs/${DATE}_${SESSION}.json" 2> "logs/${DATE}_${SESSION}.err" \
  || echo "$(date -u +%FT%TZ) cycle failed: $SESSION $DATE" >> logs/failures.log
