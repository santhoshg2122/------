"""Stage 0 integrity checks and reports/data_integrity.md (SPEC §2).

PASS / WARN / FAIL criteria (written into the report so the H1 reviewer sees the exact bar):
  FAIL  broker clock not verified (best candidate < 90% of weekly opens on Sunday 17:00 NY and closes on Friday 16:30-17:00 NY)
  FAIL  broker clock ambiguous (runner-up within 3 points: too few DST-mismatch weeks to tell NY+7 from an EU-DST clock)
  FAIL  fewer than 3 complete years of EURUSD
  WARN  any year inside the split with killzone coverage < 97% of expected days, or any gap inside a killzone
  WARN  fewer than 10 complete years (proportional split used)
A year is complete when >= 90% of its expected trading dates have both killzones >= 90% populated.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .ingest import TZ_PASS_SCORE, IngestResult
from .timeutil import expected_trading_dates, fx_holidays, hhmm, load_killzones, window_mask, window_minutes

KZ_KEYS = ("london_kz", "ny_am_kz")
KZ_FULL = 0.90
YEAR_COMPLETE = 0.90
YEAR_WARN = 0.97
# NY+7 and Europe/Athens differ only in the ~4 EU/US DST-mismatch weeks a year (~7.7% of weekends)
TZ_MARGIN = 0.03


def _mod(t: pd.Timestamp) -> int:
    return t.hour * 60 + t.minute


def _kz_overlap_minutes(a: pd.Timestamp, b: pd.Timestamp, kz: dict) -> int:
    """Minutes of [a, b) that fall inside the London or NY killzone (walks NY calendar days)."""
    total = 0
    day = a.normalize()
    while day < b:
        for k in KZ_KEYS:
            s, e = hhmm(kz[k]["start"]), hhmm(kz[k]["end"])
            ws = day + pd.Timedelta(hours=s.hour, minutes=s.minute)
            we = day + pd.Timedelta(hours=e.hour, minutes=e.minute)
            lo, hi = max(a, ws), min(b, we)
            if hi > lo:
                total += int((hi - lo).total_seconds() // 60)
        day = day + pd.Timedelta(days=1)
    return total


def find_gaps(bars: pd.DataFrame, tf_min: int, kzcfg: dict) -> pd.DataFrame:
    """Every hole of > 5 minutes between consecutive bars, classified.

    weekend  Fri close -> Sun open (dropped, not an anomaly)
    rollover starts inside the configured daily rollover window, ends before its end (info)
    holiday  touches a known FX holiday trading date (info)
    killzone overlaps London/NY killzone minutes (high)
    missing_days spans > 12h on weekdays (high)
    session  anything else inside the Sun 17:00 - Fri 17:00 week (medium)
    """
    kz = kzcfg["windows"]
    ro_s, ro_e = hhmm(kzcfg["rollover_window"]["start"]), hhmm(kzcfg["rollover_window"]["end"])
    ts = bars["ts"].reset_index(drop=True)
    diff = ts.diff()
    missing = diff - pd.Timedelta(minutes=tf_min)
    idx = missing[missing > pd.Timedelta(minutes=5)].index
    rows = []
    hol_cache: dict[int, set] = {}
    for i in idx:
        a_last, b = ts.iat[i - 1], ts.iat[i]
        a = a_last + pd.Timedelta(minutes=tf_min)
        miss_min = int(missing.iat[i].total_seconds() // 60)
        if a_last.dayofweek == 4 and b.dayofweek == 6 and miss_min < 60 * 60:
            continue  # ordinary weekend
        tds = pd.date_range((a.tz_localize(None) + pd.Timedelta(hours=7)).normalize(),
                            (b.tz_localize(None) + pd.Timedelta(hours=7)).normalize(), freq="D").date
        hols = set()
        for d in tds:
            hol_cache.setdefault(d.year, fx_holidays(d.year))
            if d in hol_cache[d.year]:
                hols.add(d)
        kz_min = _kz_overlap_minutes(a, b, kz)
        in_ro = (_mod(a) >= ro_s.hour * 60 + ro_s.minute) and (_mod(b) <= ro_e.hour * 60 + ro_e.minute) \
            and miss_min <= 120 and a.date() == b.date()
        if in_ro:
            cls, sev = "rollover", "info"
        elif hols:
            cls, sev = "holiday", "info"
        elif miss_min > 12 * 60:
            cls, sev = "missing_days", "high"
        elif kz_min > 0:
            cls, sev = "killzone", "high"
        else:
            cls, sev = "session", "medium"
        rows.append({"type": f"gap_{cls}", "severity": sev, "ts_ny": a, "end_ny": b,
                     "missing_min": miss_min, "kz_missing_min": kz_min,
                     "detail": f"{a:%a %Y-%m-%d %H:%M} -> {b:%a %Y-%m-%d %H:%M} NY, {miss_min} min missing"
                               + (f", {kz_min} in killzones" if kz_min else "")
                               + (f", holiday {', '.join(map(str, sorted(hols)))}" if hols else "")})
    return pd.DataFrame(rows, columns=["type", "severity", "ts_ny", "end_ny", "missing_min", "kz_missing_min", "detail"])


def coverage_by_year(bars: pd.DataFrame, tf_min: int, kzcfg: dict) -> pd.DataFrame:
    kz = kzcfg["windows"]
    df = bars[["ts", "trading_date", "zero_range", "bad_tick"]].copy()
    per_day = pd.DataFrame(index=pd.Index(sorted(df["trading_date"].unique()), name="trading_date"))
    per_day["bars"] = df.groupby("trading_date").size()
    for k in KZ_KEYS:
        m = window_mask(df["ts"], kz[k]["start"], kz[k]["end"])
        exp = window_minutes(kz[k]["start"], kz[k]["end"]) / tf_min
        per_day[f"{k}_fill"] = (df[m].groupby("trading_date").size() / exp).reindex(per_day.index).fillna(0)
    per_day["kz_full"] = (per_day[[f"{k}_fill" for k in KZ_KEYS]] >= KZ_FULL).all(axis=1)
    per_day["year"] = [d.year for d in per_day.index]

    rows = []
    data_first, data_last = min(per_day.index), max(per_day.index)
    for y in sorted(per_day["year"].unique()):
        # the first and last years are only expected from/to where the export starts/ends
        exp = [d for d in expected_trading_dates(y) if data_first <= d <= data_last]
        pdy = per_day[per_day["year"] == y]
        present = set(pdy.index)
        full = set(pdy.index[pdy["kz_full"]])
        hol = fx_holidays(y)
        exp_nonhol = [d for d in exp if d not in hol]
        missing = [d for d in exp_nonhol if d not in present]
        partial = [d for d in exp_nonhol if d in present and d not in full]
        ydf = df[[d.year == y for d in df["trading_date"]]]
        rows.append({
            "year": int(y),
            "first": str(min(present)), "last": str(max(present)),
            "expected_days": len(exp_nonhol),
            "days_present": len([d for d in exp_nonhol if d in present]),
            "days_kz_full": len([d for d in exp_nonhol if d in full]),
            "kz_coverage": round(len([d for d in exp_nonhol if d in full]) / max(1, len(exp_nonhol)), 4),
            "full_year": len(exp) >= 0.98 * len(expected_trading_dates(y)),
            "london_fill_mean": round(float(pdy["london_kz_fill"].clip(upper=1).mean()), 4),
            "ny_fill_mean": round(float(pdy["ny_am_kz_fill"].clip(upper=1).mean()), 4),
            "bars": int(len(ydf)),
            "zero_range": int(ydf["zero_range"].sum()),
            "zero_range_in_kz": int((ydf["zero_range"] & (window_mask(ydf["ts"], kz["london_kz"]["start"], kz["london_kz"]["end"])
                                                          | window_mask(ydf["ts"], kz["ny_am_kz"]["start"], kz["ny_am_kz"]["end"]))).sum()),
            "bad_ticks": int(ydf["bad_tick"].sum()),
            "missing_days": ", ".join(map(str, missing)),
            "partial_kz_days": ", ".join(map(str, partial)),
        })
    cov = pd.DataFrame(rows)
    cov["complete"] = (cov["kz_coverage"] >= YEAR_COMPLETE) & cov["full_year"]
    return cov


def evaluate(res: IngestResult, cov: pd.DataFrame, gaps: pd.DataFrame, splits: dict | None) -> tuple[str, list[str]]:
    reasons, status = [], "PASS"
    best = res.tz_scores.get(res.tz_detected, max(res.tz_scores.values(), default=0.0))
    if best < TZ_PASS_SCORE:
        status = "FAIL"
        reasons.append(f"broker clock not verified: best candidate scores {best:.0%} (< {TZ_PASS_SCORE:.0%})")
    else:
        ranked = sorted(res.tz_scores.values(), reverse=True)
        if not res.tz_forced and len(ranked) > 1 and ranked[0] - ranked[1] < TZ_MARGIN:
            status = "FAIL"
            reasons.append(f"broker clock ambiguous: top two candidates within {ranked[0] - ranked[1]:.1%} "
                           f"(need {TZ_MARGIN:.0%}); pass --tz after checking the broker's server time")
    n_complete = int(cov["complete"].sum())
    if n_complete < 3:
        status = "FAIL"
        reasons.append(f"only {n_complete} complete years")
    if status != "FAIL" and splits:
        in_split = set(splits["train"]["years"] + splits["validation"]["years"] + splits["holdout"]["years"])
        weak = cov[cov["year"].isin(in_split) & (cov["kz_coverage"] < YEAR_WARN)]
        if len(weak):
            status = "WARN"
            reasons.append("killzone coverage < 97% in " + ", ".join(f"{r.year} ({r.kz_coverage:.1%})" for r in weak.itertuples()))
        kzg = gaps[gaps["type"] == "gap_killzone"]
        if len(kzg):
            status = "WARN"
            reasons.append(f"{len(kzg)} gaps inside killzones ({int(kzg['kz_missing_min'].sum())} min)")
        if not splits.get("standard_10y", True):
            status = "WARN"
            reasons.append(splits["note"])
    if not reasons:
        reasons.append("all checks passed")
    return status, reasons


def _md_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in df.itertuples(index=False):
        out.append("| " + " | ".join("" if pd.isna(v) else str(v) for v in r) + " |")
    return "\n".join(out)


def write_report(results: dict[str, IngestResult], covs: dict[str, pd.DataFrame], anomalies: dict[str, pd.DataFrame],
                 statuses: dict[str, tuple[str, list[str]]], splits: dict | None, out_md: Path, out_csv: Path) -> str:
    overall = "FAIL" if any(s == "FAIL" for s, _ in statuses.values() if s) else \
              "WARN" if any(s == "WARN" for s, _ in statuses.values()) else "PASS"
    if "EURUSD" in statuses and statuses["EURUSD"][0] == "FAIL":
        overall = "FAIL"
    L = [f"# Data integrity report — {overall}", "",
         f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} by `python run.py stage0`. All times America/New_York.",
         "This is HUMAN GATE **H1**: nothing past Stage 0 runs until this report is approved.", "",
         "## Verdict", ""]
    for sym, (st, reasons) in statuses.items():
        L.append(f"- **{sym}: {st}** — " + "; ".join(reasons))
    L += ["", "Criteria: FAIL if the broker clock is not verified (< 90%), is ambiguous (runner-up within 3 points), or < 3 complete years; WARN if a split year has "
          "killzone coverage < 97%, any gap falls inside a killzone, or there are < 10 complete years. A year is *complete* "
          "when ≥ 90% of its expected trading dates have both killzones ≥ 90% populated.", ""]

    if splits:
        L += ["## Split (`config/splits.json`)", "",
              "| Set | Years | Trading dates |", "|---|---|---|"]
        for k in ("train", "validation", "holdout"):
            L.append(f"| {k} | {', '.join(map(str, splits[k]['years']))} | {splits[k]['start']} → {splits[k]['end']} |")
        if splits.get("note"):
            L.append(f"\n⚠ {splits['note']}")
        L += ["", "The holdout is coverage-checked here only; no price statistics from it appear anywhere.", ""]

    for sym, res in results.items():
        cov, an = covs[sym], anomalies[sym]
        L += [f"## {sym}", "",
              f"- Files: {', '.join(res.files)}",
              f"- Timeframe: M{res.timeframe_min}",
              f"- Broker clock: **{res.tz_detected}**" + (" (forced with --tz)" if res.tz_forced else ""),
              f"- Weekly opens observed (after conversion): " + ", ".join(f"{k} ×{v}" for k, v in res.sunday_open_hist.items()),
              "", "Clock candidates (share of weekly opens at Sun 17:00–17:15 NY and closes at Fri 16:30–17:00 NY):", "",
              "| Candidate | Score |", "|---|---|"]
        for k, v in sorted(res.tz_scores.items(), key=lambda kv: -kv[1]):
            L.append(f"| {k} | {v:.1%} |")
        c = res.counts
        L += ["", "Cleaning:", "",
              f"- raw rows {c['raw_rows']:,} → clean rows {c['clean_rows']:,}",
              f"- duplicate rows dropped {c['duplicate_rows_dropped']:,} · off-session bars dropped {c['off_session_bars_dropped']:,} · "
              f"unconvertible timestamps dropped {c['unconvertible_ts_dropped']:,}",
              f"- inconsistent OHLC repaired {c['ohlc_inconsistent_repaired']:,} · bad ticks clipped {c['bad_ticks_repaired']:,} "
              f"(raw extreme kept in `raw_high`/`raw_low`) · zero-range bars flagged {c['zero_range_bars']:,}",
              "", "### Coverage per year", ""]
        show = cov[["year", "first", "last", "expected_days", "days_present", "days_kz_full", "kz_coverage",
                    "london_fill_mean", "ny_fill_mean", "bars", "zero_range", "zero_range_in_kz", "bad_ticks", "full_year", "complete"]].copy()
        show["kz_coverage"] = (show["kz_coverage"] * 100).round(1).astype(str) + "%"
        L += [_md_table(show), ""]
        for r in cov.itertuples():
            if r.missing_days or r.partial_kz_days:
                L.append(f"- {r.year}: missing days [{r.missing_days or '—'}]; partial-killzone days [{r.partial_kz_days or '—'}]")
        L += ["", "### Anomalies", ""]
        if an.empty:
            L.append("None.")
        else:
            summ = an.groupby(["type", "severity"]).size().reset_index(name="count")
            L += [_md_table(summ), ""]
            for (typ, sev), grp in an.groupby(["type", "severity"], sort=False):
                L += [f"<details><summary>{typ} ({sev}) — {len(grp)}</summary>", "", "```"]
                L += [f"{t:%Y-%m-%d %H:%M}  {d}" for t, d in zip(grp["ts_ny"], grp["detail"])]
                L += ["```", "</details>", ""]
        L.append("")
    L += ["Full machine-readable list: `reports/data_integrity_anomalies.csv`."]
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(L) + "\n")
    allan = pd.concat([a.assign(symbol=s) for s, a in anomalies.items()], ignore_index=True) if anomalies else pd.DataFrame()
    allan.to_csv(out_csv, index=False)
    return overall


def build_anomalies(res: IngestResult, gaps: pd.DataFrame) -> pd.DataFrame:
    sev = {"duplicate_conflicting": "high", "duplicate_identical": "low", "off_session_bars": "low",
           "ohlc_inconsistent": "medium", "bad_tick": "medium", "zero_range_run": "medium"}
    ev = res.events.copy()
    ev["severity"] = ev["type"].map(sev).fillna("medium")
    ev["end_ny"] = pd.NaT
    ev["missing_min"] = pd.NA
    ev["kz_missing_min"] = pd.NA
    cols = ["type", "severity", "ts_ny", "end_ny", "missing_min", "kz_missing_min", "detail"]
    parts = [p[cols] for p in (ev, gaps) if not p.empty]
    if not parts:
        return pd.DataFrame(columns=cols)
    an = pd.concat(parts, ignore_index=True)
    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    an["_o"] = an["severity"].map(order)
    return an.sort_values(["_o", "type", "ts_ny"]).drop(columns="_o").reset_index(drop=True)


def run_checks(res: IngestResult, kzcfg: dict | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    kzcfg = kzcfg or load_killzones()
    gaps = find_gaps(res.bars, res.timeframe_min, kzcfg)
    cov = coverage_by_year(res.bars, res.timeframe_min, kzcfg)
    return cov, build_anomalies(res, gaps)

