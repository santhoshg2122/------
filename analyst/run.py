#!/usr/bin/env python3
"""EURUSD research analyst — orchestrator.

    python run.py stage0 [--tz NY+7] [--force-resplit]   ingest data/raw, verify the broker clock, integrity report, splits
    python run.py status                                 where the build is and what gate it is waiting on

Later stages (annotate, pilot, research, export) are added to this file as they are built (see CLAUDE.md).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from engine import ingest, integrity, splits
from engine.timeutil import ROOT, load_killzones

REPORT_MD = ROOT / "reports" / "data_integrity.md"
REPORT_CSV = ROOT / "reports" / "data_integrity_anomalies.csv"
GATE_FILE = ROOT / "reports" / "GATE.json"


def cmd_stage0(args) -> int:
    found = ingest.discover_raw(ingest.DEFAULT_RAW)
    if "EURUSD" not in found:
        print(f"No EURUSD CSV in {ingest.DEFAULT_RAW}. Export M1 (or M5) bars from MT5 to that folder "
              "(file name must contain EURUSD; DXY optional, name containing DXY/USDX).", file=sys.stderr)
        return 2
    kz = load_killzones()
    results, covs, anomalies, statuses = {}, {}, {}, {}
    for sym in ["EURUSD"] + [s for s in found if s != "EURUSD"]:
        print(f"[stage0] {sym}: reading {len(found[sym])} file(s)…", flush=True)
        res = ingest.ingest_symbol(sym, found[sym], zone=args.tz)
        cov, an = integrity.run_checks(res, kz)
        results[sym], covs[sym], anomalies[sym] = res, cov, an
        print(f"[stage0] {sym}: M{res.timeframe_min}, clock {res.tz_detected} "
              f"({max(res.tz_scores.values()):.0%}), {res.counts['clean_rows']:,} bars, {len(an)} anomalies", flush=True)

    sp = None
    complete = covs["EURUSD"].loc[covs["EURUSD"]["complete"], "year"].tolist()
    try:
        sp = splits.compute_splits(complete)
    except ValueError as e:
        print(f"[stage0] split not possible: {e}", file=sys.stderr)

    for sym in results:
        statuses[sym] = integrity.evaluate(results[sym], covs[sym], anomalies[sym][anomalies[sym]["type"].str.startswith("gap_")],
                                           sp if sym == "EURUSD" else None)
    overall = integrity.write_report(results, covs, anomalies, statuses, sp, REPORT_MD, REPORT_CSV)

    if statuses["EURUSD"][0] != "FAIL":
        for sym, res in results.items():
            ingest.write_bars(res, ingest.DEFAULT_BARS)
        if sp:
            splits.write_splits(sp, force=args.force_resplit)
    meta = {sym: {"files": r.files, "timeframe_min": r.timeframe_min, "broker_clock": r.tz_detected,
                  "clock_scores": r.tz_scores, "counts": r.counts} for sym, r in results.items()}
    (ingest.DEFAULT_BARS / "_meta.json").write_text(json.dumps(meta, indent=2, default=str) + "\n")
    GATE_FILE.write_text(json.dumps({"gate": "H1", "status": "awaiting_approval" if overall != "FAIL" else "blocked_integrity_fail",
                                     "integrity": overall, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, indent=2) + "\n")
    print_summary(results, covs, anomalies, statuses, sp)
    print(f"[stage0] integrity {overall} → {REPORT_MD.relative_to(ROOT)}")
    print("[stage0] HUMAN GATE H1: review the report; nothing past Stage 0 runs until it is approved.")
    return 1 if overall == "FAIL" else 0


def print_summary(results, covs, anomalies, statuses, sp) -> None:
    """Compact console version of the report, short enough to paste into a chat."""
    print("\n==================== STAGE 0 SUMMARY ====================")
    for sym, (st, reasons) in statuses.items():
        r = results[sym]
        top = sorted(r.tz_scores.items(), key=lambda kv: -kv[1])[:3]
        print(f"{sym}: {st} — {'; '.join(reasons)}")
        print(f"  M{r.timeframe_min} · clock {r.tz_detected} · " + ", ".join(f"{k} {v:.0%}" for k, v in top))
        print(f"  weekly opens: " + ", ".join(f"{k} x{v}" for k, v in r.sunday_open_hist.items()))
        c = r.counts
        print(f"  rows {c['raw_rows']:,} -> {c['clean_rows']:,} · dups {c['duplicate_rows_dropped']} · "
              f"off-session {c['off_session_bars_dropped']} · bad ticks {c['bad_ticks_repaired']} · zero-range {c['zero_range_bars']}")
        cov = covs[sym]
        print("  year  days  kz_full  kz_cov  complete")
        for row in cov.itertuples():
            print(f"  {row.year}  {row.days_present:>4}/{row.expected_days:<4} {row.days_kz_full:>4}  {row.kz_coverage:>6.1%}  {row.complete}")
        an = anomalies[sym]
        if len(an):
            print("  anomalies: " + ", ".join(f"{t}={n}" for t, n in an.groupby("type").size().items()))
    if sp:
        print("split: " + " | ".join(f"{k} {sp[k]['years'][0]}-{sp[k]['years'][-1]}" for k in splits.SPLITS))
    print("==========================================================\n")


def cmd_status(_args) -> int:
    gate = json.loads(GATE_FILE.read_text()) if GATE_FILE.exists() else {"gate": None, "status": "stage0 not run"}
    print(json.dumps(gate, indent=2))
    if splits.SPLITS_PATH.exists():
        sp = splits.load_splits()
        print({k: sp[k]["years"] for k in splits.SPLITS})
    print("holdout:", "UNLOCKED" if splits.holdout_unlocked() else "sealed")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s0 = sub.add_parser("stage0", help="ingest + integrity + splits (ends at H1)")
    s0.add_argument("--tz", choices=ingest.TZ_CANDIDATES, help="force the broker clock (still scored and reported)")
    s0.add_argument("--force-resplit", action="store_true", help="overwrite existing split boundaries")
    s0.set_defaults(func=cmd_stage0)
    sub.add_parser("status").set_defaults(func=cmd_status)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
