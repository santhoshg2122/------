# EURUSD/GBPUSD Autonomous Scalping Research & Trading System — Architecture

## Purpose
A zero-human, multi-agent system that first **learns from a year of past EURUSD/GBPUSD sessions**, replayed one at a time with no lookahead, and then trades intraday as an experienced AI trader. The agents read the M1 candles, find reversals and continuations built from divergence, imbalance and **EUR-vs-GBP divergence**, and decide the trades. From the scored experience they write their own strategies and a playbook. A small code checker only marks each trade's result, so the AI never marks its own homework. There are no code backtests: strategies are proven only by being traded forward.

Everything in this document is implemented. Decisions are logged in `CLAUDE.md` (B001–B013), and tests live in `tests/`.

---

## 1. Multi-agent pipeline and roles
Each session close (Asia, London, New York), live or in replay, runs one chain.

| # | Agent (model) | Input | Processing & decisions | Output payload |
|---|---|---|---|---|
| 0 | **Packet builder** (code) | M1 bars of both pairs, cut at the session close | Session and context candles; detector hints (swings, BOS/CHoCH, FVG, OB, liquidity and sweeps, RSI/MACD/tick-delta divergence, big moves, SMT, lead-lag) | `data/packets/<date>_<session>.json` with status OK/STALE/GAP/MISSING |
| 0b | **Analogue retriever** (code) | The packet, plus the pattern library from *before* the replay clock | Finds the 8 most similar past cases per pair (§3) | `runs/<key>/0_analogues.json`: past cases, their candle pictures, outcomes and lessons |
| 1 | **Chart Analyst** (Sonnet) | Packet, analogues, playbook, trusted and testing strategies, rules, lessons | Reads the M1 candles first (context, then session), builds the higher-timeframe picture from M1, then checks the hints. Finds every possible entry, **first on EUR/GBP divergence and imbalance** (§2). Tests the six models, the strategies and candle patterns. | `1_analyst.json`: `candle_reading`, candidates with cited levels, `strategy_id`, `candle_basis`, pattern notes, analogues used |
| 2 | **Critic — the second look** (Sonnet) | The **same** packet and analogues, then Agent 1's file | 1. Blind read first (`2_blind.json`), written before opening Agent 1. 2. Hash check. 3. Diff against its own list. 4. Audit of every level and candle claim (`candle_audit`). 5. Search for missed signals. 6. Search all models, including ones Agent 1 missed. 7. Build the opposite trade. 8. Hidden patterns. 9. Verdicts, with mods cited. | `2_critic.json`: confirmed or refuted claims, missed objects and candles, new candidates X#, hidden patterns, verdicts |
| 3 | **Cross-Pair Strategist — the third look** (Sonnet) | Same packet, Agents 1 and 2, pair bias, journals | Reads EUR against GBP: which pair led, SMT, **swing divergence between the pairs**, which pair gave the cleaner entry. Audits Agent 2's survivors against the other pair, finds what both missed, and decides which pair to trade next session. | `3_strategist.json`: relative strength, big-move attribution, audit, Y# candidates, pair decision with `flip_if` |
| 4 | **Brain Curator** (Opus) | Agents 1–3, the scorer's report | Stores the run through code, then does the judgement work: lessons with candle triggers, hidden patterns, rules, the pair call, the journal, its own memory | `brain/*` (see §3) |
| 5 | **Strategy Writer** (Opus), weekly | A week of journals, lessons, the pattern library, per-strategy and per-signature results | Writes or revises plain-language strategies. A revision gets a new id. Writes the **playbook**. | `brain/strategies/S###.md`, `brain/playbook.md` |

**How the second and third looks stay independent.** Every agent reads the identical packet, proved by `sha256`. The critic must write its own blind read before it may open Agent 1's file. The strategist must look at the other pair. Anything that reaches the Brain has survived all three reads and the code's price check (±0.2 pip against the cited candle or object).

---

## 2. Chart processing and pattern taxonomy

**Ingest.**
- MT5 M1 bars, converted to UTC; the broker clock is detected; bad ticks are clipped.
- Agents read M1 candles only (B012): the 24 h before the session plus the session itself.
- The detectors build M5 and M15 internally for their hints.

**How the agents read.** Candles first, hints second:
- wicks and rejection, closes that are accepted beyond a level versus back inside;
- displacement, momentum building and stalling, engulfing and inside bars;
- the higher timeframe read from the M1 swings.

**Usable patterns for scalping.** A setup needs an entry, a stop within 12 pips (EURUSD) or 15 pips (GBPUSD), and reward at least 1.5× risk.

| Family | Pattern | Usable when |
|---|---|---|
| Divergence | **Regular** RSI14 / MACD-histogram / tick-delta (higher high with a lower oscillator high, or the mirror) | At a swept pool or an order block, as reversal confirmation — never on its own |
| Divergence | **Hidden** (higher low with a lower oscillator low, or the mirror) | Continuation, in the direction of higher-timeframe structure read from M1 |
| **Cross-pair divergence** | **SMT**: one pair sweeps a high or low, the other fails to reach its equivalent within 5 min | Favours a reversal on the pair that *failed* to sweep |
| **Cross-pair divergence** | **Swing divergence**: one pair makes a higher high while the other makes a lower high within N minutes (lows mirrored) | Same reading as SMT at swing level; logged separately so its own stats build up |
| Cross-pair | Lead-lag and EURGBP drift | Picks the pair; never on its own |
| Imbalance | **FVG** (3-bar gap ≥ 0.4× ATR), fill %, inverse | Entry at the 50% line while less than half filled |
| Imbalance | **Order block / breaker** | First return, upper or lower third |
| Orderflow proxy | **Tick-delta** divergence and imbalance on the candles | Adds one family; never the only evidence |
| Structure | Sweep, then CHoCH, then FVG; session-open fake-out | The core reversal models |
| Candle | Wick rejection, engulfing, displacement close, inside bar, momentum stall (`candle_basis`) | As confirmation at a level; each gets its own statistics |

**The main pattern you asked for** is stored as its own model family: a **reversal at an imbalance, confirmed by divergence between EUR and GBP**. That means SMT or swing divergence, plus an FVG/OB, plus a candle rejection on the pair that failed to sweep.

---

## 3. Knowledge management — the Brain

**The Curator is the terminal agent.** It does the synthesis; the code does the bookkeeping, so numbers never drift.

| Store | Owner | Contents |
|---|---|---|
| `brain/patterns.json` | code | Signatures (`model|pair|session|tf|families|candle_basis`), instances, stats, the status ladder candidate → validated → core → retired |
| **`brain/library/cases.jsonl` + `vectors.npy`** | code | One case per graded setup and per big move. Each case holds: pair, session, pattern, levels, the **candle picture** (the 60 M1 bars before entry for both pairs), the hints present, the agents' reading, the outcome and the lesson id. |
| `brain/strategies/S###.md` + `strategy_stats.json` | writer / code | Plain-language strategies, and their results counted only on trades made *after* each strategy was written |
| `brain/playbook.md` | writer | How to trade now: which strategies, per session, pair and condition; which to avoid |
| `brain/rules.md`, `lessons.md`, `pair_bias.json`, `journal/` [exist] | curator | Proven rules only; mistakes with candle triggers an agent can check; pair calls; the session memos |
| `agent_scores.json` | code | Each agent's accuracy, the critic's catch precision, strategist pair-call accuracy |

**Retrieval (RAG)**, `scripts/retrieve.py`:
- Each case's candle picture becomes a **shape vector**: the last 60 M1 bars of both pairs, normalised by ATR (returns, wick and body ratios, the tick-delta path), plus tags for session, pair and hints present.
- At each cycle the current session's final 60 bars are embedded the same way. Cosine similarity with a tag filter picks the top 8 **past** cases.
- Only cases from before the replay clock are eligible, so there is no lookahead.
- Plain numpy: no GPU, no extra service.
- Mistakes are retrieved like wins, so the analysts see "the last 3 times it looked like this, the reversal failed because…".

---

## 4. Autonomy, error handling, self-improvement

**Fault tolerance** [exists unless marked]:

| Failure | Handling |
|---|---|
| Bad feed | The packet status is STALE, GAP or MISSING, so the session is skipped and the Brain is untouched |
| Bad ticks | Clipped at ingest, with the raw value kept |
| Malformed agent output, or a cited level not in the packet | The validator stops the chain after that step, and the Curator does not run |
| Agents disagree | Fixed survival rule: critic AGREE plus no cross-pair conflict; everything else is stored as a shadow and still scored |
| Invented or rounded price | Dropped by the ±0.2-pip check against the cited candle or object |
| API or auth failure | `logs/failures.log`; the next scheduled run continues |
| Replay crash | Checkpointed per session; resumes without repeating or skipping |
| Budget | The replay stops at `--max-cost` / `--max-sessions` |

**Self-improvement loop** (no manual input):
1. **Learn from history first**:
   - `scripts/replay.py` walks a year of past sessions in order through the same chain;
   - trades are graded only with candles up to the replay clock (grading capped by `asof`), so no later session can leak;
   - it is resumable and budgeted.
2. **Outcome scoring**: the code checker marks target-before-stop within 8 h, after the entry has traded.
3. **Status ladders** for signatures and strategies (n ≥ 15, hit ≥ 55%, reward-to-risk ≥ 1.5, retire when the last-20 hit rate falls under 40%). Strategies are judged only on trades after they were written: forward testing, no code backtests.
4. **Lessons and retrieval**: every miss becomes a lesson with a checkable candle trigger, and its case is retrieved next time the chart looks similar.
5. **Agent grading**: critic drift, calibration, and pair-call accuracy. The fix is a model change in one file.
6. **Strategy writing, weekly**: the writer turns scored evidence and hidden patterns into strategies and rewrites the playbook. Trusted strategies are "use"; testing ones are "trial, small".
7. **Go live**: after the replay, the same Brain runs on the live schedule and keeps learning.

"Machine learning" here means **outcome-scored memory plus similarity retrieval plus AI-written strategies judged forward**, not model weight training. That keeps every decision explainable and auditable.

---

---

## Where each piece lives

| Piece | Path |
|---|---|
| Packet builder, detectors, EUR/GBP SMT + swing divergence | `scripts/build_packet.py`, `scripts/lib/detectors.py`, `config/detectors.json` |
| Replay runner (learn from history first) | `scripts/replay.py`, `scripts/replay.ps1`, `config/replay.json`, `brain/replay_state.json` |
| History export | `scripts/export_from_mt5.py --from … --to … --history-only` |
| Analogue retrieval (RAG) and pattern library | `scripts/retrieve.py`, `scripts/lib/library.py`, `brain/library/` |
| Agents | `.claude/agents/chart-analyst.md`, `chart-critic.md`, `cross-pair-strategist.md`, `brain-curator.md`, `strategy-writer.md` |
| Chain and weekly loop | `.claude/skills/cycle/`, `.claude/skills/weekly-review/` |
| Result checker, stats, ladders, pair bias, agent scores | `scripts/score_outcomes.py`, `scripts/lib/brain.py`, `config/brain.json` |
| Validation between agents | `scripts/validate_run.py`, `schemas/` |
| Live schedule | `scripts/run_cycle.ps1`, `scripts/install_tasks.ps1` |

## Running it
1. `python scripts/export_from_mt5.py --from 2025-09-29 --to 2026-09-25 --history-only` (MT5 open). Check the coverage line for both pairs.
2. `powershell -ExecutionPolicy Bypass -File scripts\replay.ps1 -From 2025-09-29 -To 2026-09-25 -MaxSessions 60 -MaxCost 100`. Re-run it each night until the year is done; it continues from its checkpoint.
3. Read `reports/replay_summary.md` (code numbers) and `brain/playbook.md` (the AI's current answer to "how do I trade").
4. Go live: `powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1`. The same Brain keeps learning session by session.

