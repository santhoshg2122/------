"""B013: history packets, grading capped at the replay clock, pattern library + retrieval, EUR/GBP swing divergence,
AI-written strategies judged forward only, and the replay runner."""
from __future__ import annotations

import json
import stat
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts import build_packet as B
from scripts import replay as R
from scripts import validate_run as V
from scripts.lib import brain, library
from tests.test_pipeline import DATE, fake_run, write_pairs

P = 0.0001


@pytest.fixture
def hist(tmp_path):
    d = tmp_path / "data"
    (d / "history").mkdir(parents=True)
    write_pairs(d / "history")
    return d


@pytest.fixture
def brain_dir(tmp_path, monkeypatch):
    b = tmp_path / "brain"
    (b / "strategies").mkdir(parents=True)
    monkeypatch.setattr(brain, "BRAIN", b)
    return b


# ------------------------------------------------------------------ history packets

def test_history_packet_cut_at_close_and_cached(hist):
    p = B.build("london", DATE, hist, history=True)
    pk = json.loads(p.read_text())
    assert pk["status"] == "OK" and len(pk["sha256"]) == 64
    assert (hist / "history" / "EURUSD_M1.parquet").exists()
    for e in pk["pairs"].values():
        assert all(r[0] < "16:00" for r in e["bars_m1"])
        assert all(r[0] < f"{DATE} 07:00" for r in e["bars_m1_context"])
    again = json.loads(B.build("london", DATE, hist, history=True).read_text())   # from the parquet cache
    assert again == pk


# ------------------------------------------------------------------ no lookahead in grading

def test_grading_capped_at_replay_clock(brain_dir):
    t = pd.date_range("2026-09-29 15:50", periods=300, freq="1min", tz="UTC")
    b = pd.DataFrame({"time": t, "open": 1.1015, "high": 1.1016, "low": 1.1014, "close": 1.1015, "tick_volume": 10})
    b.loc[b["time"] == pd.Timestamp("2026-09-29 16:10", tz="UTC"), "high"] = 1.1011   # entry trades
    b.loc[b["time"] == pd.Timestamp("2026-09-29 17:00", tz="UTC"), "low"] = 1.0980    # target, but after the clock
    inst = {"id": "k:C1", "kind": "instance", "pair": "EURUSD", "direction": "short", "entry": 1.1010,
            "invalidation": 1.1030, "target": 1.0990, "flagged_at": "2026-09-29T16:00:00Z", "signature": "s",
            "date": DATE, "session": "london"}
    b.loc[b["time"] == pd.Timestamp("2026-09-29 16:10", tz="UTC"), "high"] = 1.1012
    st = {"pending": [dict(inst)], "patterns": {"signatures": {}}, "agents": {}}
    assert brain.grade_pending(st, {"EURUSD": b}, asof=pd.Timestamp("2026-09-29 16:30", tz="UTC")) == []
    assert len(st["pending"]) == 1, "the 17:00 target is after the clock and must not be seen"
    done = brain.grade_pending(st, {"EURUSD": b}, asof=pd.Timestamp("2026-09-30 01:00", tz="UTC"))
    assert [d["outcome"] for d in done] == ["hit"]


def test_replay_clock_and_session_end():
    st = {"cycles": [{"key": "2026-09-29_asia"}, {"key": "2026-09-29_newyork"}, {"key": "2026-09-29_london"}]}
    assert brain.replay_clock(st) == pd.Timestamp("2026-09-29 21:00", tz="UTC")


# ------------------------------------------------------------------ pattern library + retrieval

def bars_from(closes, start):
    t = pd.date_range(start, periods=len(closes), freq="1min", tz="UTC")
    c = np.asarray(closes)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"time": t, "open": o, "high": np.maximum(o, c) + 0.2 * P, "low": np.minimum(o, c) - 0.2 * P,
                         "close": c, "tick_volume": 50})


def test_retrieval_finds_planted_twin_and_never_the_future(brain_dir):
    rng = np.random.default_rng(3)
    lib = library.Library(brain_dir)
    shapes = {name: 1.1 + np.cumsum(rng.normal(0, P, 60)) for name in ("a", "b", "c", "twin")}
    shapes["twin"] = 1.1 + np.linspace(0, 25 * P, 60) + np.sin(np.linspace(0, 6, 60)) * 4 * P   # a distinctive picture
    cases = []
    for i, (name, closes) in enumerate(shapes.items()):
        start = pd.Timestamp("2026-09-2%d 06:00" % (1 + i), tz="UTC")
        bars = {"EURUSD": bars_from(closes, start), "GBPUSD": bars_from(closes * 1.14, start)}
        inst = {"id": f"k{i}:C1", "kind": "instance", "flagged_at": (start + pd.Timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "date": f"2026-09-2{1 + i}", "session": "london", "pair": "EURUSD", "direction": "long", "entry": 1.1,
                "invalidation": 1.099, "target": 1.102, "outcome": "hit" if name == "twin" else "miss", "why": "target",
                "move_pips": 20, "signature": name, "thesis": name}
        cases.append(library.case_from_instance(inst, bars, pd.Timestamp(inst["flagged_at"]) + pd.Timedelta(hours=8)))
    # one more twin that only becomes known after the query's close: must never be retrieved
    late = dict(cases[-1][0], case_id="late:C1", known_at="2026-12-01T00:00:00Z")
    lib.add(cases + [(late, cases[-1][1])])
    rows = lambda b: [[t.strftime("%Y-%m-%d %H:%M"), o, h, lo, c, 50] for t, o, h, lo, c in  # noqa: E731
                      zip(b["time"], b["open"], b["high"], b["low"], b["close"])]
    qstart = pd.Timestamp("2026-09-29 14:59", tz="UTC")
    tw = shapes["twin"] + 0.004
    packet = {"date": DATE, "session": "london", "window_utc": ["07:00", "16:00"],
              "pairs": {"EURUSD": {"bars_m1_context": [], "bars_m1": rows(bars_from(tw, qstart))},
                        "GBPUSD": {"bars_m1_context": [], "bars_m1": rows(bars_from(tw * 1.14, qstart))}}}
    res = library.retrieve(lib, packet, k=3)
    ids = [c["case_id"] for c in res["analogues"]]
    assert ids[0] == "k3:C1" and res["analogues"][0]["similarity"] > 0.9
    assert "late:C1" not in ids
    assert res["analogues"][0]["candles"] and res["summary"]


# ------------------------------------------------------------------ EUR/GBP swing divergence

def test_swing_divergence():
    t = lambda hm: pd.Timestamp(f"{DATE} {hm}", tz="UTC")  # noqa: E731
    sw = lambda i, typ, hm, px: {"id": i, "tf": "M5", "type": typ, "_t": t(hm), "price": px}  # noqa: E731
    packs = {"EURUSD": {"swings": [sw("EU-SW-1", "high", "08:00", 1.1010), sw("EU-SW-2", "high", "09:00", 1.1020)]},
             "GBPUSD": {"swings": [sw("GB-SW-1", "high", "08:05", 1.3510), sw("GB-SW-2", "high", "09:05", 1.3505)]}}
    out = B.strip_private(B.swing_divergence(packs, t("07:00"), DATE, {"swing_divergence": {"tf": "M5", "max_minutes_apart": 10}}))
    assert out == [{"id": "X-SDV-1", "time": "09:00", "type": "high", "EURUSD": ["EU-SW-1", "EU-SW-2"],
                    "GBPUSD": ["GB-SW-1", "GB-SW-2"], "reading": "EURUSD higher high, GBPUSD lower high"}]
    packs["GBPUSD"]["swings"][1]["price"] = 1.3520                     # both higher highs: no divergence
    assert B.swing_divergence(packs, t("07:00"), DATE, {"swing_divergence": {"tf": "M5", "max_minutes_apart": 10}}) == []


# ------------------------------------------------------------------ strategies: written by the AI, judged forward

def strat_inst(i, outcome, day):
    return {"id": f"{day}_london:C{i}", "date": day, "session": "london", "pair": "EURUSD", "entry": 1.1010,
            "invalidation": 1.1020, "target": 1.0990, "outcome": outcome, "move_pips": 15, "min_to_target": 30,
            "flagged_at": f"{day}T16:{i:02d}:00Z", "strategy_id": "S001"}


def test_strategy_forward_only_and_ladder(brain_dir):
    (brain_dir / "strategies" / "S001.md").write_text("# S001\n")
    st = {"patterns": {"signatures": {"x": {"instances": [strat_inst(i, "hit", "2026-09-01") for i in range(20)]}}},
          "cycles": [{"key": "2026-09-10_london"}], "strategies": {}}
    reg = brain.register_strategy(st, "S001")
    assert reg["created_at"] == "2026-09-10T16:00:00Z" and reg["status"] == "testing"
    brain.strategy_ladder(st)
    assert st["strategies"]["S001"]["stats"]["n"] == 0, "trades from before the strategy existed do not count"
    later = [strat_inst(i, "hit" if i % 3 else "miss", "2026-09-15") for i in range(15)]   # 10/15, rr 2.0
    st["patterns"]["signatures"]["x"]["instances"] += later
    ch = brain.strategy_ladder(st)
    assert ch and ch[0]["to"] == "trusted" and st["strategies"]["S001"]["stats"]["n"] == 15
    with pytest.raises(ValueError):
        brain.register_strategy(st, "S001")                               # a revision needs a new id
    with pytest.raises(ValueError):
        brain.register_strategy(st, "S002")                               # no file written
    (brain_dir / "strategies" / "S002.md").write_text("# S002\n")
    brain.register_strategy(st, "S002", supersedes="S001")
    assert st["strategies"]["S001"]["status"] == "retired"


def test_strategy_retired_on_poor_results(brain_dir):
    (brain_dir / "strategies" / "S001.md").write_text("# S001\n")
    st = {"patterns": {"signatures": {"x": {"instances": []}}}, "cycles": [{"key": "2026-09-10_london"}], "strategies": {}}
    brain.register_strategy(st, "S001")
    st["patterns"]["signatures"]["x"]["instances"] = [strat_inst(i, "miss" if i % 4 else "hit", "2026-09-15") for i in range(12)]
    assert brain.strategy_ladder(st)[0]["to"] == "retired"


def test_validator_rejects_unregistered_strategy(tmp_path, brain_dir):
    d = tmp_path / "data"
    d.mkdir()
    write_pairs(d)
    pp = B.build("london", DATE, d)
    run = tmp_path / "r"
    fake_run(json.loads(pp.read_text()), run)
    a = json.loads((run / "1_analyst.json").read_text())
    a["candidates"][0]["strategy_id"] = "S007"
    (run / "1_analyst.json").write_text(json.dumps(a))
    assert "unregistered strategy S007" in V.validate(run / "1_analyst.json", pp)
    (brain_dir / "strategy_stats.json").write_text(json.dumps({"S007": {"status": "testing"}}))
    assert V.validate(run / "1_analyst.json", pp) is None


def test_code_backtests_and_precursor_mining_are_gone():
    assert not hasattr(brain, "precursor_mining")
    assert not (B.ROOT / "scripts" / "backtests").exists()
    assert "precursor_mining" not in json.loads((B.ROOT / "config" / "brain.json").read_text())


# ------------------------------------------------------------------ replay runner

def fake_claude(tmp_path: Path, code: int = 0) -> str:
    f = tmp_path / ("fake_claude_fail" if code else "fake_claude")
    f.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                 f"print(json.dumps({{'total_cost_usd': 0.5, 'is_error': {bool(code)}, 'result': ' '.join(sys.argv[1:3])}}))\n"
                 f"sys.exit({code})\n")
    f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return str(f)


def test_replay_runs_resumes_and_stops(hist, brain_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "ROOT", tmp_path)
    claude = fake_claude(tmp_path)
    args = ["--from", "2026-09-28", "--to", "2026-09-29", "--claude", claude, "--data", str(hist)]
    assert R.main(args + ["--max-sessions", "4"]) == 0
    st = json.loads((brain_dir / "replay_state.json").read_text())
    assert st["sessions_ok"] == 4 and st["last_key"] == "2026-09-29_asia" and st["cost_usd"] == 2.0
    assert R.main(args + ["--max-sessions", "10"]) == 0                   # continues, never repeats
    st = json.loads((brain_dir / "replay_state.json").read_text())
    assert st["sessions_ok"] == 6 and st["last_key"] == "2026-09-29_newyork"
    assert (tmp_path / "reports" / "replay_summary.md").exists()
    assert R.main(["--from", "2026-09-01", "--to", "2026-09-29", "--claude", claude, "--data", str(hist)]) == 2  # other range


def test_replay_skips_bad_data_and_cost_limit(hist, brain_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "ROOT", tmp_path)
    args = ["--from", "2026-09-24", "--to", "2026-09-25", "--claude", fake_claude(tmp_path), "--data", str(hist)]
    assert R.main(args + ["--max-cost", "1.0"]) == 0                       # 2 sessions at $0.50 then stop
    st = json.loads((brain_dir / "replay_state.json").read_text())
    assert st["cost_usd"] == 1.0
    # an empty GBPUSD export: every remaining session is skipped as MISSING, and the run still finishes
    (hist / "history" / "GBPUSD_M1.csv").write_text("time,open,high,low,close,tick_volume\n")
    (hist / "history" / "GBPUSD_M1.parquet").unlink(missing_ok=True)
    before = st["sessions_skipped"]
    R.main(args + ["--max-sessions", "10"])
    st = json.loads((brain_dir / "replay_state.json").read_text())
    assert st["sessions_skipped"] > before and st["last_key"] == "2026-09-25_newyork"


def test_replay_stops_after_three_failures(hist, brain_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "ROOT", tmp_path)
    args = ["--from", "2026-09-28", "--to", "2026-09-29", "--claude", fake_claude(tmp_path, code=1), "--data", str(hist)]
    assert R.main(args) == 1
    st = json.loads((brain_dir / "replay_state.json").read_text())
    assert len(st["failures"]) == 3 and st["sessions_ok"] == 0
