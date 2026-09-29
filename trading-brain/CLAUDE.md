# Trading Brain — project rules

EURUSD/GBPUSD multi-agent Pattern Brain. Three analyst agents (Sonnet) read one Chart Packet after every Asia, London and New York close; the curator (Opus) stores what survived into `brain/`, which the next cycle reads first. Learning comes only from scored outcomes.

## Rules every agent follows
- Agents read M1 candles only (context + session) and read the higher timeframe from them; detector objects are hints (B011, B012). Never invent or round a price: every level is an exact candle field (`bar:` ref) or an exact object price, cited.
- Pairs: EURUSD, GBPUSD. Pip = 0.0001 for both. Base timeframe M1; M5 and M15 are derived. All times in files are UTC.
- Sessions (UTC): asia 00:00-08:00, london 07:00-16:00, newyork 13:00-21:00 (`config/sessions.json`).
- Price evidence comes only from `data/packets/*.json`: candles or object ids.
- Agents write only to `runs/<date>_<session>/`. Only brain-curator writes `brain/`. Nobody edits `data/`.
- `brain/patterns.json` instances, stats and status, `pending_scores.json`, `agent_scores.json` and `cycle_log.json` are written only by `scripts/score_outcomes.py`. The curator edits `hidden_patterns`, `rules.md`, `lessons.md`, the calls in `pair_bias.json`, `research.json` and `journal/`.
- Every rule, lesson and candidate must be checkable against a packet by an agent that has not seen the session.
- No web access. No files outside this project. Use `python` (Windows) for every script.

## Models
Analysts `model: sonnet` (resolved to claude-sonnet-5-5 in the first live run), curator `model: opus` and main `/cycle` session `--model opus` (claude-opus-5-5). Aliases follow the newest versions. First live cycle on synthetic data: 7 min, $2.08 (Opus $0.94, Sonnet $1.14); after B011 (candle reading): $2.01; after B012 (M1 only): $1.65 (Opus $1.05, Sonnet $0.60), 25 M1 candle refs cited (3 from the context), none M5/M15, all files validated. If the curator logs `REVIEWER DRIFT`, set `model: opus` in `.claude/agents/chart-critic.md`.

## Pieces
| Path | Role |
|---|---|
| `scripts/export_from_mt5.py` | MT5 → `data/<PAIR>_M1.csv` (3 days) + `data/history/<PAIR>_M1.csv` (60 days), UTC; `--from-csv` seeds from a file |
| `scripts/build_packet.py` | CSVs → `data/packets/<date>_<session>.json`, never reads past the close; status OK/STALE/GAP/MISSING |
| `scripts/lib/detectors.py` | swings, BOS/CHoCH, displacement, FVG, OB/breaker, liquidity + sweeps, retracements, RSI/MACD/tick-delta divergence, big moves, lead-lag |
| `scripts/lib/ingest.py` | MT5 CSV parsing, broker-clock detection, bad-tick clip (kept from the earlier project's tested Stage 0) |
| `scripts/validate_run.py` | schema + ABORT + packet-hash + cited-id + M1 candle-ref check after every agent (`schemas/`) |
| `scripts/score_outcomes.py` / `scripts/lib/brain.py` | survival rule, price-vs-object check, signatures, grading, stats, ladder, pair bias, agent scores, weekly merge + precursor mining |
| `config/detectors.json`, `config/brain.json` | every threshold; calibrate here, never in code |
| `.claude/agents/`, `.claude/skills/` | the four agents; `/cycle`, `/score`, `/weekly-review` |
| `scripts/run_cycle.ps1`, `scripts/install_tasks.ps1` | unattended runs via Windows Task Scheduler |

## Decisions (and why)
- **B001 Numbers are code, judgement is LLM.** The survival rule, the Critic's mods, signatures, grading, stats, ladder, pair-bias rule, merges and agent grading are deterministic in `brain.py`; the curator acts on the report. An LLM computing hit rates would drift.
- **B002 Every price is checked.** A candidate whose entry/invalidation/target price is not the named object's price (±0.2 pip) is dropped as `... is not the price of a <PAIR> object`. Dropped candidates are still graded as *shadows* so the Critic's catches can be scored.
- **B003 Hit requires the entry to trade first**: entry touched, then target before invalidation within 8 h = hit; invalidation first (or both in one bar) = miss; entry never touched = expired `not_triggered`. The plan's text did not require the entry fill; without it an untaken retrace entry that ran to target would count as a hit.
- **B004 Retirement needs n ≥ 10** (`retire_min_n`) for the last-20 test, otherwise a new signature with three misses would retire before it had a chance; the idle rule (10 sessions without an instance) still applies to all.
- **B005 Big moves are descriptive, not ladder instances.** Their "entry" is defined after the move is known, so storing them as hits would inflate stats. They go to `big_move_attributions` and feed precursor mining.
- **B006 Big move** = M1 zigzag leg (reversal 1 × ATR14 M15) of ≥ 2.5 × ATR14(M15) within ≤ 90 min; `preceded_by` = objects from 30 min before to 15 min after its start; `first_entry_object` = first FVG/OB in its direction formed after the start.
- **B007 Packet pruning**: context objects before the session are kept only while they matter at the close (live FVG/OB, unswept pools, last 8 h of structure) plus anything another object cites, so a packet stays under ~1,000 lines for the agents.
- **B008 Session date** = the UTC date of the session for all three closes (NY closes 21:00 UTC = 02:30 IST, still the same UTC day); the plan's `date -u -d yesterday` would have picked the wrong day.
- **B009 Critic on Sonnet** (user's choice): independence rests on the blind read in `2_blind.json`, written before Agent 1's file is opened.
- **B010 Trust the folder once.** Claude Code ignores `.claude/settings.json` permission rules in an untrusted folder; the scheduled runs therefore also pass `--allowedTools`, and the first interactive `claude` in the folder must accept the trust prompt.
- **B011 Agents read the candles; detector objects are hints** (user decision, 2026-09-29). The packet carries the candles (`bars_m15/m5/m1` for the session, `bars_m15_context`/`bars_m5_context` for the 24 h before it) and the agents analyse them first, M15 → M5 → M1, writing a `candle_reading` per pair. Detector objects stay in the packet as "hints: pre-computed for convenience, verify on the candles". A level may cite an object id or a candle, `bar:<PAIR>:<M1|M5|M15>:<YYYY-MM-DD HH:MM>:<open|high|low|close>`; prices are still verified — `brain.resolve_bar` (shared by `validate_run.py` and the scorer) reads the candle from the packet, refuses anything after the close or an M1 candle outside the window, and a price off by more than 0.2 pip drops the candidate (`<field> is not the <bar field> of that candle`). Candle refs count as family `BAR`; an optional `candle_basis` becomes the signature's last segment so candle-read patterns get their own statistics (signatures without it are unchanged). Scoring is still code: grading, stats, ladder, pair bias and agent scores are untouched.
- **B012 Agents read M1 candles only** (user decision, 2026-09-29; supersedes the candle arrays of B011). The packet's only candle arrays are `bars_m1` (session, `HH:MM` rows) and `bars_m1_context` (every M1 candle from `context_hours` = **24** before the session to its start, full timestamps); the synthetic packets are 1,020–1,107 lines, under the 1,200 limit, so 24 h was kept. The agents work out the higher-timeframe picture (range, trend, key highs/lows, acceptance vs rejection) from the M1 candles and cite them. Candle refs are `bar:<PAIR>:M1:...` only — session or context; an M5/M15 ref fails validation with "only M1 candles can be cited" (the validator resolves candle refs before the schema so the reason is clear). The detectors still build M5/M15 internally, so objects on those timeframes remain as hints. Price check (±0.2 pip), candle_basis, candle_reading, candle_audit and all scoring are unchanged.

## Status
- Built and tested (`python -m pytest -q`): ingest, detectors, packet, validator, scorer, ladder, pair bias, end-to-end ingest.
- Not yet done on the PC: MT5 export of GBPUSD (adapt `export_from_mt5.py` to the Trading Journal exporter if preferred), detector calibration against real charts, first interactive `/cycle`, `install_tasks.ps1`.

## First run on the PC
```
pip install -r requirements.txt            (plus: pip install MetaTrader5)
python -m pytest -q
python scripts/export_from_mt5.py          (MT5 running and logged in)
python scripts/build_packet.py --session london --date <yesterday>     # compare with your chart; tune config/detectors.json
claude  →  accept the folder-trust prompt, then /cycle london <yesterday>   # read runs/<date>_london/ and brain/journal/
powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
```
