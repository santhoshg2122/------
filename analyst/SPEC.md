# EURUSD Autonomous Research Analyst — Build Spec for Claude Code

> Paste this whole file into Claude Code (or save it as `SPEC.md` in the project root and say "build SPEC.md"). Build it stage by stage. Stop only at the **HUMAN GATES** listed in §12. Everything else you decide yourself and log why.

---

## 0. Your role

You are the **brain** of a self-improving market research system. You are not a trading bot and you do not place trades. Your job is to work through ten years of EURUSD one day at a time, the way a professional research analyst would, and turn it into **tested, portable knowledge** about:

- **Direction bias** — what the day and each killzone were going to do.
- **Setup validity** — whether an ICT/SMC setup was real (it reached its target before its invalidation) or false.
- **Causation** — *why* the move happened: which liquidity was taken, which PD array it was delivered into, what the draw was.

The user trades ICT/SMC session opens (see the existing `ict-smc-trader` skill, v5.4). Reuse its engine (`setup`, `pools`, `divg`, the candle engine, the journal) wherever it already computes something; do not rebuild what exists, and **do not modify its journal** — this system writes to its own store.

**Out of scope, by the user's decision:** spread, slippage, commissions, position sizing, live execution. Score direction and setup validity only. The user owns execution.

**Honesty rule, above everything:** report the real number. A rule at 54% is reported as 54%. Never tune on the holdout, never drop inconvenient days, never describe an in-sample result as an edge.

---

## 1. Time, sessions, killzones (all America/New_York)

| Window | Time (NY) |
|---|---|
| Asian range | 19:00 – 02:00 |
| London killzone | 02:00 – 05:00 |
| NY AM killzone | 07:00 – 10:00 |
| London close | 10:00 – 12:00 |
| NY PM | 13:30 – 16:00 |
| Trading day boundary | 17:00 NY (FX rollover) |

DST is handled by converting all timestamps to `America/New_York` at ingest. Never hard-code a UTC offset.

---

## 2. Data and the three-way split

**Input:** EURUSD (and DXY if available) exported from MT5 as CSV, M1 preferred (M5 acceptable). Columns: timestamp, open, high, low, close, tick volume. The user will place files in `data/raw/`.

**Integrity checks (Stage 0, must pass before anything else):**
- Detect the broker's server timezone and convert correctly (verify: the Sunday open lands at 17:00 NY).
- Flag gaps > 5 minutes inside trading hours, duplicate bars, zero-range bars, bad ticks (> 8 × ATR spikes that fully revert within 1 bar).
- Produce `reports/data_integrity.md` with coverage per year and every anomaly.

**Split (by calendar, never shuffled):** with ten years Y1…Y10:

| Set | Years | Who may see it |
|---|---|---|
| **Train** | Y1 – Y7 | Everything: replay, research, rule fitting |
| **Validation** | Y8 – Y9 | Only for confirming a rule already found on Train |
| **Holdout** | Y10 | **Sealed.** Opened once, at the end, by HUMAN GATE H3 |

Write the split boundaries to `config/splits.json` and enforce them in code: any function that loads bars takes a `split` argument and raises if asked for holdout without the unlock file `config/HOLDOUT_UNLOCKED` present.

---

## 3. Project layout

```
analyst/
  SPEC.md                      # this file
  CLAUDE.md                    # your working memory: current stage, decisions, open questions
  config/
    splits.json
    killzones.json
    news_days.json             # high-impact events (§8)
    budget.json                # token / run limits (§11)
  data/
    raw/                       # MT5 CSVs (user supplies)
    bars/                      # cleaned parquet, NY time, one file per year
  engine/                      # Stage 1: objective annotator (deterministic Python)
  panel/                       # Stage 2: analyst prompts + output schemas
  scoring/                     # Stage 3: outcome labeller + scorer
  research/                    # Stage 5: hypothesis generator, backtester, promotion gates
  kb/                          # Stage 4: the knowledge base (the system's memory)
    stats.duckdb               # every day, every call, every outcome
    days/                      # one annotated JSON per day (the RAG corpus)
    index/                     # vector index over days/ (LanceDB or Chroma, local)
    hypotheses.jsonl           # every idea ever tested, with result
    rulebook.md                # promoted rules only, with evidence
    causes.md                  # the cause taxonomy (§6)
    reflections/               # every 50-day review
  .claude/agents/              # the analyst panel as Claude Code subagents
  export/                      # Stage 8: the portable package
  reports/
  run.py                       # the orchestrator
```

---

## 4. Stage 1 — Objective annotator (code, no LLM)

For each trading day, compute deterministically and store in `kb/days/<date>.json`:

- **Ranges:** Asian H/L, London H/L, NY H/L, PDH/PDL, PWH/PWL, PMH/PML, midnight open, 08:30 open, daily open.
- **Structure (M5, M15, H1, H4):** swing highs/lows (fractal, configurable), BOS, CHOCH/MSS, the last displacement leg per timeframe and its direction.
- **Liquidity pools:** equal highs/lows (≥ 2 touches within tolerance, count touches), session extremes, prior-day/week extremes, relative-equal shelves. Each pool: price, tier (reuse the skill's tier 1a/1b/2), age, whether already consumed.
- **Sweeps:** every pool taken, with time, killzone, depth beyond pool, and whether the wick closed back inside (grab) or price was accepted beyond (break: close > 1 ATR past).
- **PD arrays:** FVGs and order blocks on M1/M5/M15/H1/H4 with state (unfilled / partially filled / filled / respected-before count); premium/discount of price relative to the current dealing range.
- **DXY divergence** at each sweep if DXY data exists (reuse `divg`).
- **Regime tags (§7) and news tags (§8).**

Everything is computed **as-of** a timestamp. The annotator takes `--asof` and must never read a bar after it. Write a unit test that shifts future bars and asserts the as-of output is unchanged.

---

## 5. Stage 2 — The analyst panel

Four agents, defined as Claude Code subagents in `.claude/agents/`. The three specialists are **independent**: each gets only the annotated as-of snapshot, never another specialist's output.

| Agent | Reads | Must answer |
|---|---|---|
| `structure-analyst` | Swings, BOS/CHOCH, displacement per TF | Which way is structure pointing on H4 / H1 / M15? Is the Asian session a retrace inside a higher-TF leg or a new leg? |
| `liquidity-analyst` | Pools, sweeps, consumed state | Which pools are resting, which side is the draw, which pool will the next killzone take first? |
| `array-analyst` | FVGs, OBs, premium/discount, respected history | Which PD array is the delivery target, which is the likely reaction point, is price in premium or discount? |
| `lead-analyst` | The three reports + RAG retrieval (§9) + current rulebook | The final verdict |

**Decision points each day (as-of snapshots):**
1. **02:00 NY** — before London (Asian session complete).
2. **07:00 NY** — before NY (London complete).

**Every agent outputs strict JSON** (schemas in `panel/schemas/`). Minimum lead verdict:

```json
{
  "date": "YYYY-MM-DD",
  "asof": "02:00",
  "day_bias": "bull|bear|none",
  "day_bias_p": 0.0,
  "kz_first_pool": "asian_high|asian_low|none",
  "kz_first_pool_p": 0.0,
  "draw_on_liquidity": {"level": 0.0, "name": "PDH", "p_reached_today": 0.0},
  "setups": [
    {"id": "...", "type": "E1|E2|COUNTER_E1|COUNTER_E2|other",
     "direction": "long|short", "trigger": "...", "entry_zone": [0.0, 0.0],
     "invalidation": 0.0, "target": 0.0, "p_valid": 0.0}
  ],
  "cause_tags": ["stop_run_equal_lows", "delivery_to_h1_fvg"],
  "rules_applied": ["R0012", "R0031"],
  "panel_agreement": {"structure": "bear", "liquidity": "bear", "array": "bull"},
  "reasoning": "short, specific, names levels and times"
}
```

Every probability must be a number. "No bias" is a valid answer and is scored.

---

## 6. Stage 3 — Outcomes, scoring, and the "why"

**Outcome labeller (code, runs after the day closes, never visible to the panel before its call):**
- Day direction (close vs daily open), which extreme printed first, whether PDH/PDL/each named draw was reached.
- For each killzone: which Asian/London extreme was taken first, max excursion each way.
- For each setup: **valid** if price reached `target` before `invalidation` within the killzone (plus the hold window configured in `config/killzones.json`); **invalid** otherwise; **not triggered** if entry zone was never reached. Direction-only, no costs.

**Scoring per call:** hit/miss, Brier score for every probability, calibration buckets, and **panel disagreement** (which specialist was right when they split).

**Post-mortem ("why") pass — the lead analyst, after outcomes are known:**
For each day, write a short causal explanation of what actually happened and tag it from `kb/causes.md`. Start the taxonomy with these and let it grow (new tags require ≥ 20 occurrences before they are used in statistics):

`stop_run_equal_highs`, `stop_run_equal_lows`, `asian_range_grab`, `pdh_pdl_raid`, `delivery_to_htf_fvg`, `rebalance_ltf_fvg`, `ob_reaction`, `judas_swing`, `continuation_after_confirmed_sweep`, `failed_grab_acceptance`, `news_driven`, `range_day_no_draw`, `smt_divergence_reversal`.

**The key principle:** a "why" is a hypothesis, not a fact. A cause tag only earns weight when the statistics show that days carrying that pre-conditions pattern produce that outcome more often than the base rate.

---

## 7. Regime tagging

Tag every day so rules are judged per regime, not averaged over the decade:
- **Volatility:** 20-day ATR percentile within its trailing 1-year window → low / normal / high.
- **Trend:** H4/daily structure over the last 20 days → trending up / trending down / ranging.
- **Day type (prior day):** expansion (closed near its extreme, body ≥ 45% of range) / inside / reversal — matching the skill's DAY read.

A rule is only promoted if it holds (above base rate) in **at least two** of the three volatility buckets it has samples in, or it is promoted **with an explicit regime condition** attached.

---

## 8. News tagging

Build `config/news_days.json` covering the full ten years: FOMC decisions, NFP, US CPI, ECB decisions, and any other high-impact USD/EUR releases. Source from a public historical economic calendar; if a source cannot be fetched, reconstruct the recurring schedule (NFP first Friday, FOMC published dates, ECB published dates) and mark the file `partial`. Every event gets date and NY time.

Days and killzones containing a high-impact event are tagged `news`. They are **scored but reported separately**, and excluded from rule promotion statistics unless the rule is explicitly a news rule.

---

## 9. Stage 4 — The knowledge base (the system's memory)

The panel is stateless. The KB is what makes it learn.

- **`stats.duckdb`** — tables: `days`, `calls`, `setups`, `outcomes`, `hypotheses`, `rules`, `rule_evidence`, `reflections`. Every number in any report must be reproducible with a SQL query in this DB.
- **`days/` + `index/`** — each annotated day with its post-mortem is embedded (local embedding model, e.g. a sentence-transformers model) into a local vector index. At each decision point the lead analyst retrieves the **10 most similar past days from Train only and only before the current date** (no lookahead) and sees what they did.
- **`hypotheses.jsonl`** — every idea ever tested, including failures and why they failed. Nothing is retested unless the reason it failed has changed.
- **`rulebook.md`** — promoted rules only. Each rule: ID, plain-English statement, exact code definition, sample size, hit rate, base rate, confidence interval, regimes it holds in, date promoted, evidence query.

The lead analyst's context each day = current rulebook + retrieved analogues + the last reflection. That is the memory.

---

## 10. Stage 5 — The research loop (self-directed)

You generate hypotheses yourself. The user does not supply them. Sources, in rotation:

1. **Own mistakes:** query `stats.duckdb` for the worst-calibrated situations (confident and wrong). Each cluster becomes a hypothesis about what you were missing.
2. **Panel disagreements:** when one specialist was consistently right against the other two in some condition, that is a hypothesis.
3. **External research:** search the web for ICT/SMC concepts, session-behaviour studies, published intraday FX research, and strategy write-ups. Most of it is noise; extract only claims that can be stated as a precise, testable rule.
4. **Mutation:** take a promoted rule and test a variant (different pool tier, different timeframe for the FVG, different killzone).

**Every hypothesis must be formalised as code** before testing: precondition (as-of computable), prediction, outcome definition, killzone. If it cannot be coded, it is not tested.

**Promotion gates — all must pass, measured on Train:**
- n ≥ 100 occurrences (n ≥ 50 allowed with status `provisional`).
- Hit rate beats the relevant base rate, one-sided binomial test p < 0.05 **after Benjamini–Hochberg correction** across all hypotheses tested so far.
- Stable: above base rate in at least 5 of the 7 train years.
- Regime check per §7.

**Then Validation:** a rule that passes Train is tested once on Y8–Y9. Passes → `promoted`. Fails → `rejected_on_validation`, logged with both numbers. No re-tuning a rule after seeing its validation result; a changed rule is a new hypothesis with a new ID.

**Demotion:** every 250 replayed days, re-score all promoted rules on all Train data seen so far. A rule whose lower confidence bound falls below base rate is demoted and the reason logged.

---

## 11. Stage 6 — The daily loop and reflections

`run.py` does, for each trading day in order (Train first, day by day):

1. Annotate as-of 02:00 → specialists (parallel) → lead → store verdict.
2. Annotate as-of 07:00 → specialists → lead → store verdict.
3. After the day: label outcomes → score → post-mortem "why" → embed day into the index.
4. Every **50 days: reflection.** The lead analyst reads its calibration table, its worst misses, and the panel-disagreement table, then writes `kb/reflections/NNNN.md`: what went wrong, what hypotheses it is adding, what prompt changes it is making to the specialists. Prompt changes are versioned (`panel/prompts/v###/`) and every call records which version produced it.
5. Every **100 days: research cycle** (Stage 5).

**Checkpointing:** the loop is resumable from the last completed day. A crash never repeats or skips a day.

**Budget (`config/budget.json`):** max days per run, max tokens per day, max web searches per research cycle. When the budget is hit, finish the current day cleanly and stop.

**Cost note:** four agents × two decision points × ~1,800 train days is a large number of calls. Start with a **pilot of 60 days**, report token use and early numbers, then scale.

**Scheduling:** the user's PC runs Windows with MT5. Provide a Task Scheduler entry that runs `run.py` nightly in headless mode (`claude -p` or direct API calls — choose whichever is more reliable and document it).

---

## 12. HUMAN GATES — the only times you stop and ask

- **H1 — After Stage 0:** show `reports/data_integrity.md`. Proceed only on approval.
- **H2 — After the 60-day pilot:** show accuracy, calibration, token cost per day, projected total cost. Proceed only on approval.
- **H3 — Holdout unlock:** only after the full Train + Validation pass. Run the final rulebook on Y10 exactly once and report the numbers, whatever they are.

Also stop if: the data integrity check fails, a no-lookahead test fails, or the budget is exceeded. Everything else — which hypotheses to test, what to search for, prompt revisions — you decide and log.

---

## 13. Reports (auto-generated, `reports/`)

- `status.md` — updated every run: days done, current split, rules promoted/provisional/rejected, overall bias accuracy and Brier vs base rate.
- `rulebook_summary.md` — each rule, n, hit rate, base rate, CI, regimes.
- `calibration.md` — predicted probability vs actual frequency, per question type.
- `panel.md` — accuracy per specialist, and who wins when they disagree.
- `causes.md` — which cause tags actually predict outcomes, with numbers.

Always show the **base rate next to every hit rate**. "62%" means nothing without "vs 51% base".

---

## 14. Stage 8 — The portable export

When Train + Validation are complete (and again after H3), build `export/`:

- `SKILL.md` — a Claude skill containing the promoted rulebook, the cause taxonomy with evidence, the killzone framework, and instructions for using the retrieval index. Written so it drops into the user's skills folder next to `ict-smc-trader`.
- `rulebook.md` + `rules.py` — the rules as plain English and as executable checks.
- `stats.duckdb` — the full evidence base.
- `index/` + `retrieve.py` — the analogue library with a one-command retrieval script (`python retrieve.py --date YYYY-MM-DD --asof 02:00 --k 10`).
- `local_llm/README.md` — how to point a local model (e.g. via Ollama) at the rulebook and the retrieval script as a RAG system.

The export must work with no dependency on this project folder.

---

## 15. Build order

1. Stage 0 — ingest, timezone, integrity report → **H1**
2. Stage 1 — annotator + no-lookahead tests
3. Stage 3 outcome labeller (built before the panel so scoring is ready)
4. Stage 2 panel subagents + schemas
5. Stage 4 KB + retrieval
6. `run.py` loop → 60-day pilot → **H2**
7. Stage 5 research loop and promotion gates
8. Full Train run with reflections and research cycles
9. Validation pass
10. Export → **H3** holdout → final export

Keep `CLAUDE.md` updated at every step with: current stage, last completed day, decisions made and why, open questions.

---

*This system does research, not financial advice. Its output is evidence for the user's own decisions.*
