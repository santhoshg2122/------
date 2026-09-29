#!/usr/bin/env python3
"""Grade flagged setups and keep the Brain's numbers (plan sections 9 and 11). The curator acts on the printed report.

    python scripts/score_outcomes.py --packet data/packets/2026-09-29_london.json   # grade pending with the latest bars
    python scripts/score_outcomes.py --ingest-run runs/2026-09-29_london             # store a finished run, grade, recompute
    python scripts/score_outcomes.py --all                                           # grade everything gradable, recompute
    python scripts/score_outcomes.py --recompute                                     # stats, ladder, pair bias, agent scores
    python scripts/score_outcomes.py --weekly                                        # merge signatures + precursor mining

Prints one JSON report (also saved to brain/last_report.json).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.lib import brain  # noqa: E402
from scripts.lib.ingest import load_utc  # noqa: E402
from scripts.lib.timeutil import ROOT  # noqa: E402


def load_bars(data_dir: Path) -> dict:
    out = {}
    for pair in brain.PAIRS:
        parts = []
        for p in (data_dir / "history" / f"{pair}_M1.csv", data_dir / f"{pair}_M1.csv"):
            if p.exists():
                b, _ = load_utc(p, pair)
                parts.append(b)
        if parts:
            b = pd.concat(parts).drop_duplicates("time", keep="last").sort_values("time").reset_index(drop=True)
            out[pair] = b
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--packet", type=Path)
    g.add_argument("--ingest-run", type=Path)
    g.add_argument("--all", action="store_true")
    g.add_argument("--recompute", action="store_true")
    g.add_argument("--weekly", action="store_true")
    ap.add_argument("--data", type=Path, default=ROOT / "data")
    a = ap.parse_args(argv)

    st = brain.load_state()
    report: dict = {}
    if a.ingest_run:
        run = a.ingest_run if a.ingest_run.is_absolute() else ROOT / a.ingest_run
        date, session = run.name.split("_", 1)
        packet = a.data / "packets" / f"{date}_{session}.json"
        report["ingest"] = brain.ingest_run(st, run, packet)
    if a.packet or a.all or a.ingest_run:
        graded = brain.grade_pending(st, load_bars(a.data))
        report["graded"] = [{"id": i["id"], "kind": i["kind"], "outcome": i["outcome"], "why": i["why"],
                             "move_pips": i["move_pips"], "signature": i["signature"]} for i in graded]
        report["graded_counts"] = {o: sum(i["outcome"] == o for i in graded) for o in ("hit", "miss", "expired")}
    if a.weekly:
        report["merged"] = brain.merge_signatures(st)
        report["new_hidden_patterns"] = brain.precursor_mining(st)
    report.update(brain.recompute(st))
    brain.save_state(st)
    (brain.BRAIN / "last_report.json").write_text(json.dumps(report, indent=1) + "\n")
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
