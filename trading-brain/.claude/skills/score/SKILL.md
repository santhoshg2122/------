---
description: Grade pending flagged setups against the latest bars and refresh Brain stats. Usage: /score [packet-path]
argument-hint: [packet-path]
allowed-tools: Read, Write, Edit, Agent, Bash(python *), Bash(python3 *), Bash(py *)
---

Packet: $0 (empty = the newest file in data/packets/).
1. Run `python scripts/score_outcomes.py --packet <packet>`.
2. Use the brain-curator subagent, with the report from step 1, to rewrite brain/rules.md from validated/core signatures, update the calls in brain/pair_bias.json from its `computed` block, add lessons for new misses, and append a journal line `scored: <n> graded, <hits> hits` to the newest file in brain/journal/. It does not re-run the ingest.
