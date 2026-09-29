#!/usr/bin/env python3
"""Learn from history first (B013): walk past sessions in time order through the same agent chain.

    python scripts/replay.py --from 2025-09-29 --to 2026-09-25 --max-sessions 60 --max-cost 100
    python scripts/replay.py --summary                        # only rewrite reports/replay_summary.md

For each weekday session (asia, london, newyork, in closing order) after the checkpoint: build the packet from
data/history/ (nothing after the close), skip it if the data is not OK, otherwise run `/cycle` headless and add its
cost. Every `weekly_every` sessions `/weekly-review` runs (the strategy-writer turns the evidence into strategies).
The run is resumable from brain/replay_state.json and never repeats or skips a session; it stops cleanly at
--max-sessions, --max-cost or after three failures in a row. Grading inside the cycles is capped at the replay clock,
so no session ever learns where a later one went.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import build_packet  # noqa: E402
from scripts.lib import brain  # noqa: E402
from scripts.lib.timeutil import ROOT, load_config  # noqa: E402

ALLOWED = "Read,Write,Edit,Agent,Bash(python *),Bash(python3 *),Bash(py *),Bash(mkdir *)"
ORDER = ("asia", "london", "newyork")


def sessions(frm: str, to: str) -> list[str]:
    days = pd.date_range(frm, to, freq="D")
    return [f"{d:%Y-%m-%d}_{s}" for d in days if d.dayofweek < 5 for s in ORDER]


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(path: Path, st: dict) -> None:
    st["updated"] = now()
    path.write_text(json.dumps(st, indent=1) + "\n")


def run_claude(claude: str, prompt: str, log: Path, model: str) -> tuple[bool, float, str]:
    """One headless Claude Code run. Returns (ok, cost_usd, error)."""
    cmd = [claude, "-p", prompt, "--model", model, "--permission-mode", "acceptEdits", "--permission-prompts", "none",
           "--allowedTools", ALLOWED, "--output-format", "json"]
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, 0.0, f"{type(e).__name__}: {e}"
    log.write_text(p.stdout)
    if p.stderr:
        log.with_suffix(".err").write_text(p.stderr)
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        return False, 0.0, f"exit {p.returncode}, output is not JSON"
    cost = float(out.get("total_cost_usd") or 0.0)
    if p.returncode != 0 or out.get("is_error"):
        return False, cost, f"exit {p.returncode}: {str(out.get('result'))[:200]}"
    return True, cost, ""


def write_summary(state: dict, out: Path) -> None:
    """Code-written numbers only: what the replay has learned so far."""
    st = brain.load_state()
    graded = [i for s in st["patterns"]["signatures"].values() for i in s["instances"] if i["outcome"] in ("hit", "miss")]
    base = sum(i["outcome"] == "hit" for i in graded) / len(graded) if graded else 0
    L = [f"# Replay summary", "",
         f"Updated {now()} · replay {state.get('from')} → {state.get('to')} · last session {state.get('last_key')}",
         f"Sessions run {state.get('sessions_ok', 0)}, skipped (data) {state.get('sessions_skipped', 0)}, "
         f"failed {len(state.get('failures', []))} · cost ${state.get('cost_usd', 0):.2f}", "",
         f"Graded trades (survivors): {len(graded)} · hit rate {base:.1%} — the base rate every strategy is compared with.", "",
         "## Strategies (results only after each strategy was written)", "",
         "| id | status | created (replay clock) | n | hit rate | vs base | last 20 | avg rr |", "|---|---|---|---|---|---|---|---|"]
    for sid, r in sorted(st.get("strategies", {}).items()):
        x = r.get("stats", {})
        L.append(f"| {sid} | {r['status']} | {r['created_at'][:10]} | {x.get('n', 0)} | {x.get('hit_rate', 0):.1%} | "
                 f"{(x.get('hit_rate', 0) - base) * 100:+.1f} pts | {x.get('last_20_hit_rate', 0):.1%} | {x.get('avg_rr', 0)} |")
    counts: dict = {}
    for s in st["patterns"]["signatures"].values():
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    L += ["", "## Signatures", "", "Status counts: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"), "",
          "| signature | status | n | hit rate | avg rr |", "|---|---|---|---|---|"]
    top = sorted(st["patterns"]["signatures"].items(), key=lambda kv: -kv[1].get("stats", {}).get("n", 0))[:15]
    for k, s in top:
        x = s.get("stats", {})
        L.append(f"| `{k}` | {s['status']} | {x.get('n', 0)} | {x.get('hit_rate', 0):.1%} | {x.get('avg_rr', 0)} |")
    lib = brain.BRAIN / "library" / "cases.jsonl"
    n_cases = sum(1 for _ in lib.open()) if lib.exists() else 0
    L += ["", f"Pattern library: {n_cases} cases with candle pictures.",
          "Real dates were shown to the agents; the model may remember past prices, so treat replay results as an upper "
          "bound until live results confirm them."]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L) + "\n")


def main(argv=None) -> int:
    cfg = load_config("replay")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--to")
    ap.add_argument("--max-sessions", type=int, default=cfg["max_sessions_per_run"])
    ap.add_argument("--max-cost", type=float, default=cfg["max_cost_usd_per_run"])
    ap.add_argument("--claude", default=cfg["claude_command"])
    ap.add_argument("--model", default=cfg["main_model"])
    ap.add_argument("--data", type=Path, default=ROOT / "data")
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args(argv)

    state_path = brain.BRAIN / "replay_state.json"
    summary_path = ROOT / "reports" / "replay_summary.md"
    state = load_state(state_path)
    if a.summary:
        write_summary(state, summary_path)
        print(summary_path)
        return 0
    if not (a.frm and a.to):
        ap.error("--from and --to are required")
    if state and (state.get("from"), state.get("to")) != (a.frm, a.to):
        print(f"replay_state.json holds {state.get('from')} → {state.get('to')}; continuing that range is required "
              f"(delete brain/replay_state.json only if you mean to restart the Brain)", file=sys.stderr)
        return 2
    state = state or {"from": a.frm, "to": a.to, "started": now(), "last_key": None, "sessions_ok": 0,
                      "sessions_skipped": 0, "cost_usd": 0.0, "failures": [], "since_weekly": 0, "since_summary": 0}
    logs = ROOT / "logs" / "replay"
    logs.mkdir(parents=True, exist_ok=True)
    todo = [k for k in sessions(a.frm, a.to) if state["last_key"] is None or k > state["last_key"]]
    ran, cost_run, fails_in_row = 0, 0.0, 0
    for key in todo:
        if ran >= a.max_sessions or cost_run >= a.max_cost:
            print(f"stopping: {ran} sessions / ${cost_run:.2f} this run (limits {a.max_sessions} / ${a.max_cost})")
            break
        date, session = key.split("_", 1)
        pk = json.loads(build_packet.build(session, date, a.data, history=True).read_text())
        if pk["status"] != "OK":
            state["sessions_skipped"] += 1
            state["last_key"] = key
            save_state(state_path, state)
            print(f"{key}: skipped ({pk['status']}: {pk.get('status_reason', '')})")
            continue
        ok, cost, err = run_claude(a.claude, f"/cycle {session} {date}", logs / f"{key}.json", a.model)
        cost_run += cost
        state["cost_usd"] = round(state["cost_usd"] + cost, 4)
        ran += 1
        if not ok:
            fails_in_row += 1
            state["failures"].append({"key": key, "error": err, "at": now()})
            state["last_key"] = key
            save_state(state_path, state)
            print(f"{key}: FAILED ({err})", file=sys.stderr)
            if fails_in_row >= cfg["stop_after_failures_in_row"]:
                print("stopping: three failures in a row — see logs/replay/", file=sys.stderr)
                return 1
            continue
        fails_in_row = 0
        state["sessions_ok"] += 1
        state["since_weekly"] += 1
        state["since_summary"] += 1
        state["last_key"] = key
        save_state(state_path, state)
        print(f"{key}: ok (${cost:.2f}, total ${state['cost_usd']:.2f})")
        if state["since_weekly"] >= cfg["weekly_every_sessions"]:
            wok, wcost, werr = run_claude(a.claude, "/weekly-review", logs / f"{key}_weekly.json", a.model)
            cost_run += wcost
            state["cost_usd"] = round(state["cost_usd"] + wcost, 4)
            if wok:
                state["since_weekly"] = 0
            else:
                state["failures"].append({"key": f"{key} weekly", "error": werr, "at": now()})
            save_state(state_path, state)
            print(f"{key}: weekly review {'ok' if wok else 'FAILED'} (${wcost:.2f})")
        if state["since_summary"] >= cfg["summary_every_sessions"]:
            write_summary(state, summary_path)
            state["since_summary"] = 0
            save_state(state_path, state)
    write_summary(state, summary_path)
    print(f"replay: {state['sessions_ok']} sessions done, last {state['last_key']}, cost ${state['cost_usd']:.2f} → {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
