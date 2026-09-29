---
description: Run the four-agent session cycle on a chart packet. Usage: /cycle <session> [date]. Sessions: asia, london, newyork.
argument-hint: <session> [date]
allowed-tools: Read, Write, Edit, Agent, Bash(python *), Bash(python3 *), Bash(py *), Bash(mkdir *)
---

Session: $0. Date: $1 (empty = today's UTC date).
Packet: `data/packets/<date>_<session>.json`. Run folder: `runs/<date>_<session>/`.

Run these steps strictly in order, one at a time, waiting for each file before starting the next. Pass every subagent the packet path, the run folder and `0_analogues.json` in its task. "Validate X" means run `python scripts/validate_run.py runs/<date>_<session>/X --packet data/packets/<date>_<session>.json`; anything but `OK` is a failure.

1. Check the packet: run `python scripts/validate_run.py data/packets/<date>_<session>.json` and read its `status`. If validation fails or the status is not `OK`, write `runs/<date>_<session>/skipped.txt` with the status and reason and stop. Run no agent.
2. `mkdir -p runs/<date>_<session>`. If `runs/<date>_<session>/1_analyst.json` already exists, this cycle already ran: stop and say so.
3. Score first: run `python scripts/score_outcomes.py --packet data/packets/<date>_<session>.json` so pending setups from earlier cycles are graded before analysis.
3b. Retrieve analogues: run `python scripts/retrieve.py --packet data/packets/<date>_<session>.json --out runs/<date>_<session>/0_analogues.json`. Pass that file to every analyst below.
4. Use the chart-analyst subagent on the packet. It must produce `1_analyst.json`. Validate 1_analyst.json.
5. Use the chart-critic subagent on the same packet with `1_analyst.json`. It must produce `2_blind.json` and `2_critic.json`. Validate 2_blind.json, then 2_critic.json.
6. Use the cross-pair-strategist subagent on the same packet with both files. It must produce `3_strategist.json`. Validate 3_strategist.json.
7. Use the brain-curator subagent to store the run and write the Brain.
8. Print the journal file it wrote.

If any validation fails, stop, write `skipped.txt` with the step and the validator's line, and do not run the Curator. The Brain is then untouched.
