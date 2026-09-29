---
description: Weekly consolidation of the Brain: score, ladders, merges, then the AI writes strategies and the playbook. Usage: /weekly-review
allowed-tools: Read, Write, Edit, Agent, Bash(python *), Bash(python3 *), Bash(py *), Bash(mkdir *)
---

Run these in order and stop if any step fails. No code backtests: strategies are proven only by being traded forward and scored.
1. Run `python scripts/score_outcomes.py --all`: grades every pending trade that the replay clock allows, recomputes stats, the signature and strategy ladders, pair bias and agent scores. Keep the report.
2. Run `python scripts/score_outcomes.py --weekly`: merges signatures with the same model, pair, session and candle basis whose families overlap 80% or more. Keep the report.
3. Use the strategy-writer subagent with both reports: it reviews and writes strategies in `brain/strategies/`, registers them, updates `hidden_patterns[*].tested_in`, and rewrites `brain/playbook.md`.
4. Use the brain-curator subagent with both reports and the strategy-writer's reply to rewrite `brain/rules.md` (validated/core signatures and trusted strategies), update the calls in `brain/pair_bias.json`, retire lessons unused for 20 sessions, and write `brain/journal/week_<ISO week of the replay clock>.md`: status changes, merges, strategies created/revised/retired, per-pair and per-session hit rates, the three signatures and strategies closest to promotion.
