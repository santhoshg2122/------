# CLAUDE.md — working memory

Spec: `SPEC.md` (source of truth). Update this file at every step.

## Current stage

**Stage 0 built and tested → waiting for data, then HUMAN GATE H1.**

- Last completed trading day replayed: none (no replay yet).
- Holdout: sealed (`config/HOLDOUT_UNLOCKED` absent).
- Blocked on: the user's MT5 CSVs in `data/raw/`. Then `python run.py stage0` → `reports/data_integrity.md` → H1.

## How to run

```
pip install -r requirements.txt
python run.py stage0            # ingest data/raw → data/bars, integrity report, config/splits.json
python run.py stage0 --tz NY+7  # only if the report says the clock is ambiguous and the broker's server time is known
python run.py status
python -m pytest -q             # 17 tests
```

Data input: MT5 CSVs whose file names contain `EURUSD` (required) or `DXY`/`USDX` (optional), any split by year
or one big file, M1 preferred. Both MT5 layouts are read: the "Export bars" dialog (`<DATE>\t<TIME>\t<OPEN>…`) and
headerless script exports (`2021.03.01 00:00,o,h,l,c,v`). Epoch or ISO-with-offset timestamps are treated as UTC-anchored.

## Built

| Stage | Files | Tests |
|---|---|---|
| 0 ingest + clock + integrity + splits | `engine/ingest.py`, `engine/integrity.py`, `engine/splits.py`, `engine/timeutil.py`, `run.py stage0` | `tests/test_stage0.py` (17) |

Full-scale dry run on synthetic data (11.5 years, 4.3M M1 bars, NY+7 clock): 38 s, PASS, splits 2016–22 / 2023–24 / 2025.

## Decisions (and why)

- **D001 Project lives in `analyst/`** of this repo; the repo root holds an unrelated web page that is left untouched.
- **D002 Broker clock is detected, not assumed.** Candidates: NY+7 (the usual MT5 server clock: EET switching on US DST dates, so server midnight = 17:00 NY), America/New_York (the user's own `mt5_export.py` may already convert), UTC, UTC+1/+2/+3, London, Berlin, Athens. Score = mean of (share of weekly opens at Sun 17:00–17:15 NY) and (share of weekly closes at Fri 16:30–17:00 NY). Pass ≥ 90%. The EU/US DST mismatch weeks (March, Oct/Nov) are what separate NY+7 from an EU-DST clock, so the top two must be ≥ 3 points apart or Stage 0 FAILs as *ambiguous* (fixable with `--tz` once the broker's clock is confirmed). Holiday weeks legitimately reduce the score to ~98%.
- **D003 Trading date = NY date of (ts + 7h)**: the 17:00 NY rollover starts the next day; the Asian range belongs to the day it precedes. Parquet files are partitioned by trading-date year so no day is split across files.
- **D004 Gap = > 5 min missing between consecutive bars.** Classified: weekend (dropped), rollover (starts/ends inside 16:50–18:15 NY, ≤ 2 h: info), holiday (touches Jan 1, Dec 24–26, Dec 31, Good Friday: info), missing_days (> 12 h: high), killzone (overlaps London/NY KZ: high), session (other: medium).
- **D005 Bad tick** = excursion > 8 × ATR beyond the previous close that is back within 2 × ATR by the close of the same or next bar, and the next bar does not trade back out. ATR = rolling median TR of the preceding 60 bars (a median cannot be inflated by the spike it is measuring), floored at 0.5 pip. Spikes are **clipped** to the bar body in the clean bars, flagged `bad_tick`, and the raw extreme kept in `raw_high`/`raw_low`: an unclipped spike would register as a false liquidity sweep in Stage 1.
- **D006 Zero-range bars are flagged, not removed** (normal in quiet Asian minutes). Only runs of ≥ 5 are listed individually as possible stale-feed periods; totals per year and inside killzones are in the coverage table.
- **D007 Duplicates:** first row kept; identical vs conflicting duplicates are reported separately. Bars outside Sun 17:00–Fri 17:00 NY are dropped and logged.
- **D008 Year completeness:** a year is complete when it spans the full calendar year (≥ 98% of its expected dates inside the export) and ≥ 90% of expected non-holiday trading dates have both killzones ≥ 90% populated. The split uses the **last ten complete contiguous years**; a partial current year is not complete, so it can never become the holdout.
- **D009 Split enforcement:** `engine/splits.load_bars(symbol, split, start, end)` is the only bar loader. A split may read any date up to its own end (lookback into earlier splits is the past, and Stage 1 needs it for PWH/PML/1-year ATR percentiles). Train cannot read Validation dates. Nothing on or after the holdout start (including any partial year after Y10) is readable without `config/HOLDOUT_UNLOCKED`. Split boundaries are frozen once written (`--force-resplit` to move them, which must be logged here).
- **D010 Integrity verdict:** FAIL (stop) = clock unverified/ambiguous or < 3 complete years; WARN = split-year KZ coverage < 97%, any gap inside a killzone, or < 10 complete years. WARN still goes to H1 for the user to decide.
- **D011 Setup hold window = 0 min** after the killzone (`config/killzones.json`), matching the skill's exit "the KZ ending". Changeable in config only.
- **D012 The ict-smc-trader engine is not in this environment.** `market.py`/`journal.py` (setup, pools, divg) live on the user's Windows PC (`Documents\Trading Journal`). Stage 1 needs them; see open question Q1. Its journal is never written to.

## Open questions (for the user)

- **Q1** Can `market.py` (and `skill_refs/session_open_model.md`, `liquidity_pools.md`) be copied into `analyst/engine/vendor/`? Stage 1 should reuse its pool tiers, CHOCH and `divg` rather than re-derive them. If not, I will reimplement them from the skill text and flag every place the definitions could differ.
- **Q2** DXY: the skill uses *synthetic* DXY built from the six component pairs. Export DXY/USDX directly if the broker has it; otherwise export EURUSD, USDJPY, GBPUSD, USDCAD, USDSEK, USDCHF and I will build it the same way.
- **Q3** Data transport: 10 years of M1 is ~200–250 MB of CSV. GitHub rejects files > 100 MB, so export one file per year (~20 MB each) and push them to `analyst/data/raw/`, or run Stage 0 on the PC and push the parquet (~60 MB total) instead.

## Next steps after H1

1. Stage 1 annotator (`engine/annotate.py --asof`) + the future-bar shift test.
2. Stage 3 outcome labeller.
3. `config/news_days.json` (§8).
