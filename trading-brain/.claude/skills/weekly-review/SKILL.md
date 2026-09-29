---
description: Weekly consolidation of the Brain. Usage: /weekly-review
allowed-tools: Read, Write, Edit, Agent, Bash(python *), Bash(python3 *), Bash(py *), Bash(mkdir *)
---

Use the brain-curator subagent to do all of the following, in order, and stop if any step fails:
1. Run `python scripts/score_outcomes.py --all` to grade every pending instance and recompute all stats, the status ladder, the pair bias and the agent scores. List every status change it reports.
2. Run `python scripts/score_outcomes.py --weekly`: it merges signatures with the same model, pair and session whose families overlap 80% or more, and mines precursors (family combinations preceding 60% or more of big moves over the last 20 sessions of a type, n >= 12) into `hidden_patterns` as `hp_<slug>`. Report the merges and new hidden patterns.
3. For every hidden pattern with 3 or more instances and `backtest: pending`, write `scripts/backtests/<slug>.py` from the rule text. It must use only `scripts/lib/ingest.py` and `scripts/lib/detectors.py` on `data/history/<PAIR>_M1.csv`, never read a bar after the one it decides on, and print JSON `{"n", "hits", "hit_rate"}` graded with `scripts/lib/brain.py`'s `grade`. Run it, store the result in `brain/research.json` under the slug, and set the pattern's `backtest` to `pass` (hit_rate >= 0.55 and n >= 15) or `fail`.
4. Rewrite brain/rules.md and the calls in brain/pair_bias.json from the refreshed stats; retire lessons unused for 20 sessions.
5. Write brain/journal/week_<ISO week>.md: status changes, merges, new hidden patterns, backtest results, per-pair and per-session hit rates, and the three signatures closest to promotion (from brain/patterns.json stats).
