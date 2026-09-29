"""Packet statuses, validator, run ingest, grading, ladder and pair bias — on synthetic two-pair data."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts import build_packet as B
from scripts import validate_run as V
from scripts.lib import brain

P = 0.0001
DATE = "2026-09-29"


def write_pairs(d: Path, drop=None, end="2026-09-29 23:59"):
    idx = pd.date_range("2026-09-24 00:00", end, freq="1min", tz="UTC")
    idx = idx[~((idx.dayofweek == 5) | ((idx.dayofweek == 6) & (idx.hour < 21)) | ((idx.dayofweek == 4) & (idx.hour >= 21)))]
    if drop is not None:
        idx = idx[~((idx >= drop[0]) & (idx < drop[1]))]
    rng = np.random.default_rng(5)
    common = np.cumsum(rng.normal(0, 0.8 * P, len(idx)))
    for pair, base, k in (("EURUSD", 1.17, 1.0), ("GBPUSD", 1.34, 1.3)):
        c = base + k * common + np.cumsum(rng.normal(0, 0.4 * P, len(idx)))
        o = np.r_[base, c[:-1]]
        w = np.abs(rng.normal(0, 0.5 * P, (2, len(idx))))
        pd.DataFrame({"time": idx.strftime("%Y-%m-%dT%H:%M:%SZ"), "open": o.round(5), "high": (np.maximum(o, c) + w[0]).round(5),
                      "low": (np.minimum(o, c) - w[1]).round(5), "close": c.round(5),
                      "tick_volume": rng.integers(20, 300, len(idx))}).to_csv(d / f"{pair}_M1.csv", index=False)


@pytest.fixture
def data(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    write_pairs(d)
    return d


def test_packet_ok_and_schema(data):
    p = B.build("london", DATE, data)
    pk = json.loads(p.read_text())
    assert pk["status"] == "OK" and len(pk["sha256"]) == 64
    assert V.validate(p) is None
    for pair, pre in (("EURUSD", "EU"), ("GBPUSD", "GB")):
        e = pk["pairs"][pair]
        assert len(e["bars_m1"]) == 540 and e["bars_m1"][0][0] == "07:00" and e["bars_m1"][-1][0] == "15:59"
        assert all(o["id"].startswith(pre + "-") for o in e["fvg"] + e["liquidity"] + e["swings"])
    ids = V.packet_ids(pk)
    refs = set(V.ID.findall(p.read_text()))
    assert refs <= ids, "every cited id resolves"


def test_packet_never_reads_past_close(data, tmp_path):
    """Changing bars after 16:00 must not change the London packet (except the sha256 of the files)."""
    a = json.loads(B.build("london", DATE, data).read_text())
    for pair in ("EURUSD", "GBPUSD"):
        f = data / f"{pair}_M1.csv"
        df = pd.read_csv(f)
        late = df["time"] >= "2026-09-29T16:00:00Z"
        df.loc[late, ["open", "high", "low", "close"]] = df.loc[late, ["open", "high", "low", "close"]] + 0.0100
        df.to_csv(f, index=False)
    b = json.loads(B.build("london", DATE, data).read_text())
    a.pop("sha256"), b.pop("sha256")
    assert a == b


@pytest.mark.parametrize("case,expected", [
    ("missing", "MISSING"), ("stale", "STALE"), ("gap", "GAP")])
def test_packet_status_paths(tmp_path, case, expected):
    d = tmp_path / "d"
    d.mkdir()
    if case == "missing":
        write_pairs(d)
        (d / "GBPUSD_M1.csv").unlink()
    elif case == "stale":
        write_pairs(d, end="2026-09-29 15:30")
    else:
        write_pairs(d, drop=(pd.Timestamp("2026-09-29 10:00", tz="UTC"), pd.Timestamp("2026-09-29 10:07", tz="UTC")))
    pk = json.loads(B.build("london", DATE, d).read_text())
    assert pk["status"] == expected and "pairs" not in pk


# ------------------------------------------------------------------ grading

def gbars(rows, start="2026-09-29 16:00"):
    t = pd.date_range(start, periods=len(rows), freq="1min", tz="UTC")
    return pd.DataFrame({"time": t, "high": [r[0] for r in rows], "low": [r[1] for r in rows]})


INST = {"direction": "short", "entry": 1.1010, "invalidation": 1.1020, "target": 1.0990, "flagged_at": "2026-09-29T16:00:00Z"}
CFG = {"scoring": {"horizon_hours": 8, "same_bar_both_touched": "miss"}}
FULL = 8 * 60


@pytest.mark.parametrize("rows,outcome,why", [
    ([(1.1011, 1.1005), (1.1006, 1.0989)] + [(1.1, 1.0999)] * FULL, "hit", "target"),
    ([(1.1011, 1.1005), (1.1021, 1.1005)] + [(1.1, 1.0999)] * FULL, "miss", "invalidation"),
    ([(1.1011, 1.1005), (1.1021, 1.0989)] + [(1.1, 1.0999)] * FULL, "miss", "same_bar"),
    ([(1.1005, 1.0985)] + [(1.1, 1.0999)] * FULL, "expired", "not_triggered"),   # target reached without entry
    ([(1.1011, 1.1005)] + [(1.1012, 1.1002)] * FULL, "expired", "horizon"),
])
def test_grade(rows, outcome, why):
    g = brain.grade(dict(INST), gbars(rows), CFG)
    assert (g["outcome"], g["why"]) == (outcome, why)


def test_grade_waits_for_bars():
    assert brain.grade(dict(INST), gbars([(1.1011, 1.1005)] * 30), CFG) is None


# ------------------------------------------------------------------ ladder and pair bias

def inst(i, outcome, date="2026-09-01", pair="EURUSD", sess="london"):
    return {"id": f"{date}_{sess}:C{i}", "date": date, "session": sess, "pair": pair, "entry": 1.1010, "invalidation": 1.1020,
            "target": 1.0990, "outcome": outcome, "move_pips": 15, "min_to_target": 30, "flagged_at": f"{date}T16:{i:02d}:00Z"}


def state_with(sig_instances: dict, cycles=("2026-09-01_london",)):
    st = {"patterns": {"signatures": {}, "hidden_patterns": {}, "big_move_attributions": []}, "pending": [],
          "agents": {"cycles": [], "pair_calls": []}, "cycles": [{"key": k} for k in cycles], "bias": {}}
    for key, (status, insts) in sig_instances.items():
        model, pair, sess, tf, fams = key.split("|")
        st["patterns"]["signatures"][key] = {"model": model, "pair": pair, "session": sess, "tf": tf, "families": fams.split("+"),
                                             "status": status, "first_seen": "2026-09-01", "last_seen": "2026-09-01",
                                             "status_changed": "2026-09-01", "stats": {}, "instances": insts}
    return st


def test_ladder_promotes_and_retires():
    good = [inst(i, "hit" if i % 3 else "miss") for i in range(15)]           # 10/15 = 0.667, rr 2.0
    bad = [inst(i, "miss" if i % 4 else "hit") for i in range(12)]           # 3/12 = 0.25
    few = [inst(i, "miss") for i in range(3)]                                  # n < 10: not retired yet
    st = state_with({"a|EURUSD|london|M5|FVG+LQ": ("candidate", good), "b|EURUSD|london|M5|OB": ("validated", bad),
                     "c|EURUSD|london|M5|DV": ("candidate", few)})
    ch = {c["signature"].split("|")[0]: c["to"] for c in brain.apply_ladder(st)}
    assert ch == {"a": "validated", "b": "retired"}


def test_ladder_retires_idle():
    cycles = [f"2026-09-{d:02d}_london" for d in range(1, 13)]
    st = state_with({"a|EURUSD|london|M5|FVG": ("candidate", [inst(0, "hit")])}, cycles=cycles)
    assert brain.apply_ladder(st)[0]["to"] == "retired"


def test_pair_bias_rule(monkeypatch):
    e = [inst(i, "hit" if i < 14 else "miss", pair="EURUSD") for i in range(20)]   # 0.70
    g = [inst(i, "hit" if i < 10 else "miss", pair="GBPUSD") for i in range(20)]   # 0.50
    st = state_with({"a|EURUSD|london|M5|FVG": ("validated", e), "a|GBPUSD|london|M5|FVG": ("validated", g)})
    b = brain.compute_pair_bias(st)
    assert b["london"]["call"] == "EURUSD" and b["asia"]["call"] == "either"
    g2 = [inst(i, "hit" if i < 8 else "miss", pair="GBPUSD") for i in range(20)]   # 0.40
    e2 = [inst(i, "hit" if i < 8 else "miss", pair="EURUSD") for i in range(20)]
    st = state_with({"a|EURUSD|london|M5|FVG": ("validated", e2), "a|GBPUSD|london|M5|FVG": ("core", g2)})
    assert brain.compute_pair_bias(st)["london"]["call"] == "neither"


# ------------------------------------------------------------------ end to end: agent files -> ingest -> grade

READING = {"EURUSD": "M15 lower highs; M5 wick through the Asia high closed back inside.",
           "GBPUSD": "M5 displacement close down after the sweep."}


def fake_run(pk: dict, run: Path, verdict="AGREE", extra=()):
    e = pk["pairs"]["EURUSD"]
    fvg = next(o for o in e["fvg"] if o["dir"] == "bear")
    lqs = e["liquidity"]
    cand = {"id": "C1", "pair": "EURUSD", "model": "sweep_choch_fvg", "direction": "short", "thesis": "t",
            "objects": [fvg["id"], lqs[0]["id"]], "families": ["FVG", "LQ"],
            "entry": {"object": fvg["id"], "price": fvg["mid"]}, "invalidation": {"object": fvg["id"], "price": fvg["top"]},
            "target": {"object": lqs[0]["id"], "price": lqs[0]["price"]}, "confidence": 0.6, "falsifier": "f"}
    bad = dict(cand, id="C2", entry={"object": fvg["id"], "price": fvg["mid"] + 0.0007})   # price not an object price
    sha = pk["sha256"]
    run.mkdir(parents=True, exist_ok=True)
    (run / "1_analyst.json").write_text(json.dumps({"agent": "chart-analyst", "packet_sha256": sha, "context": {},
                                                    "candle_reading": READING, "candidates": [cand, bad, *extra]}))
    (run / "2_blind.json").write_text(json.dumps({"candidates": []}))
    (run / "2_critic.json").write_text(json.dumps({"agent": "chart-critic", "packet_sha256": sha, "status": "OK",
                                                   "verdicts": [{"candidate": "C1", "verdict": verdict, "confidence": 0.6},
                                                                {"candidate": "C2", "verdict": "AGREE", "confidence": 0.5}] +
                                                               [{"candidate": x["id"], "verdict": "AGREE", "confidence": 0.5} for x in extra]}))
    (run / "3_strategist.json").write_text(json.dumps({"agent": "cross-pair-strategist", "packet_sha256": sha, "status": "OK",
                                                       "candle_reading": READING,
                                                       "third_audit": [{"candidate": "C1", "cross_pair": "confirms", "confidence": 0.65}],
                                                       "pair_decision": {"asia": "either", "london": "EURUSD", "newyork": "either", "flip_if": "x"}}))


def test_end_to_end(data, tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "BRAIN", tmp_path / "brain")
    pp = B.build("london", DATE, data)
    pk = json.loads(pp.read_text())
    run = tmp_path / "runs" / f"{DATE}_london"
    fake_run(pk, run)
    for f in ("1_analyst", "2_blind", "2_critic", "3_strategist"):
        assert V.validate(run / f"{f}.json", pp) is None, f
    st = brain.load_state()
    rep = brain.ingest_run(st, run, pp)
    assert rep["survived"] == ["C1"]
    assert rep["dropped"] == [{"id": "C2", "reason": "entry is not the price of a EURUSD object in the packet"}]
    sig = rep["stored_signatures"][0]
    assert sig.startswith("sweep_choch_fvg|EURUSD|london|") and sig.endswith("|FVG+LQ")
    from scripts.score_outcomes import load_bars
    done = brain.grade_pending(st, load_bars(data))
    assert {i["kind"] for i in done} == {"instance", "shadow"} and not st["pending"]
    rep2 = brain.recompute(st)
    assert rep2["signature_counts"] == {"candidate": 1}
    brain.save_state(st)
    assert (tmp_path / "brain" / "patterns.json").exists()
    with pytest.raises(ValueError):
        brain.ingest_run(brain.load_state(), run, pp)            # a cycle is ingested once


def test_validator_rejects(data, tmp_path):
    pp = B.build("london", DATE, data)
    pk = json.loads(pp.read_text())
    run = tmp_path / "r"
    fake_run(pk, run)
    f = run / "2_critic.json"
    d = json.loads(f.read_text())
    f.write_text(json.dumps(dict(d, status="ABORT", reason="hash mismatch")))
    assert "ABORT" in V.validate(f, pp)
    f.write_text("{not json")
    assert "not valid JSON" in V.validate(f, pp)
    a = json.loads((run / "1_analyst.json").read_text())
    a["candidates"][0]["entry"]["object"] = "EU-FVG-9999"
    (run / "1_analyst.json").write_text(json.dumps(a))
    assert "not in the packet" in V.validate(run / "1_analyst.json", pp)
    a["packet_sha256"] = "0" * 64
    (run / "1_analyst.json").write_text(json.dumps(a))
    assert "differs" in V.validate(run / "1_analyst.json", pp)


def test_reviewer_drift_needs_sample():
    st = state_with({})
    st["agents"]["cycles"] = [{"cycle": "k", "analyst_candidates": 2, "analyst_claims_refuted": 0,
                               "critic_verdicts": ["AGREE", "AGREE"], "critic_challenged": [], "critic_new": 0, "strategist_new": 0}]
    assert brain.agent_scores(st)["reviewer_drift"] is False
    st["agents"]["cycles"][0]["critic_verdicts"] = ["AGREE"] * 20
    assert brain.agent_scores(st)["reviewer_drift"] is True


# ------------------------------------------------------------------ candle references (B011)

def candle_cand(pk: dict, cid: str, off_pips: float = 0.0, basis: str | None = "wick_rejection") -> dict:
    """A short from a window M5 candle's high, stop at a later candle's high + target at a context M15 low."""
    e = pk["pairs"]["EURUSD"]
    row = max(e["bars_m5"][10:40], key=lambda r: r[2])            # a clear M5 high in the window
    ctx = min(e["bars_m15_context"], key=lambda r: r[3])          # lowest M15 context candle
    ent = f"bar:EURUSD:M5:{DATE} {row[0]}:close"
    inv = f"bar:EURUSD:M5:{DATE} {row[0]}:high"
    tgt = f"bar:EURUSD:M15:{ctx[0]}:low"
    c = {"id": cid, "pair": "EURUSD", "model": "candle_wick_rejection", "direction": "short", "thesis": "t",
         "objects": [inv], "entry": {"object": ent, "price": round(row[4] + off_pips * P, 5)},
         "invalidation": {"object": inv, "price": row[2]}, "target": {"object": tgt, "price": ctx[3]},
         "confidence": 0.5, "falsifier": "f"}
    if basis:
        c["candle_basis"] = basis
    return c


def test_bar_reference_levels(data, tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "BRAIN", tmp_path / "brain")
    pp = B.build("london", DATE, data)
    pk = json.loads(pp.read_text())
    run = tmp_path / "runs" / f"{DATE}_london"
    good, off = candle_cand(pk, "C3"), candle_cand(pk, "C4", off_pips=0.5)
    fake_run(pk, run, extra=(good, off))
    assert V.validate(run / "1_analyst.json", pp) is None          # both refs exist; the price check is the scorer's
    st = brain.load_state()
    rep = brain.ingest_run(st, run, pp)
    assert "C3" in rep["survived"]
    assert {"id": "C4", "reason": "entry is not the close of that candle"} in rep["dropped"]
    sig = next(s for s in rep["stored_signatures"] if s.startswith("candle_wick_rejection"))
    parts = sig.split("|")
    assert parts[3] == "M5" and "BAR" in parts[4].split("+") and parts[5] == "wick_rejection"
    assert st["patterns"]["signatures"][sig]["candle_basis"] == "wick_rejection"


def test_signature_without_basis_unchanged():
    assert brain.signature("m", "EURUSD", "london", "M5", ["FVG", "LQ"]) == "m|EURUSD|london|M5|FVG+LQ"
    assert brain.signature("m", "EURUSD", "london", "M5", ["BAR"], "engulfing") == "m|EURUSD|london|M5|BAR|engulfing"


@pytest.mark.parametrize("ref,why", [
    (f"bar:EURUSD:M1:{DATE} 16:00:high", "after the session close"),
    (f"bar:EURUSD:M15:{DATE} 15:50:low", "after the session close"),          # would close at 16:05
    (f"bar:EURUSD:M5:{DATE} 03:07:high", "not in the packet"),                # not a 5-minute boundary
    ("bar:EURUSD:M1:2026-09-28 22:00:high", "M1 bars exist only for the session window"),
    ("bar:EURUSD:M5:2026-09-20 10:00:high", "not in the packet"),              # before the context
])
def test_validator_rejects_bad_bar_refs(data, tmp_path, ref, why):
    pp = B.build("london", DATE, data)
    pk = json.loads(pp.read_text())
    run = tmp_path / "r"
    fake_run(pk, run)
    a = json.loads((run / "1_analyst.json").read_text())
    a["candidates"][0]["target"] = {"object": ref, "price": 1.1}
    (run / "1_analyst.json").write_text(json.dumps(a))
    assert why in V.validate(run / "1_analyst.json", pp)


def test_bar_field_must_be_valid(data, tmp_path):
    pp = B.build("london", DATE, data)
    run = tmp_path / "r"
    fake_run(json.loads(pp.read_text()), run)
    a = json.loads((run / "1_analyst.json").read_text())
    a["candidates"][0]["target"] = {"object": f"bar:EURUSD:M5:{DATE} 09:00:mid", "price": 1.1}
    (run / "1_analyst.json").write_text(json.dumps(a))
    assert "breaks its schema" in V.validate(run / "1_analyst.json", pp)


def test_no_candle_after_close_or_inside_context(data):
    pk = json.loads(B.build("london", DATE, data).read_text())
    for e in pk["pairs"].values():
        for tf, mins in (("m1", 1), ("m5", 5), ("m15", 15)):
            for r in e[f"bars_{tf}"]:
                t = pd.Timestamp(f"{DATE} {r[0]}")
                assert pd.Timestamp(f"{DATE} 07:00") <= t and t + pd.Timedelta(minutes=mins) <= pd.Timestamp(f"{DATE} 16:00")
        for tf in ("m5", "m15"):
            ctx = e[f"bars_{tf}_context"]
            assert ctx and all(pd.Timestamp(r[0]) < pd.Timestamp(f"{DATE} 07:00") for r in ctx)
            assert pd.Timestamp(ctx[0][0]) >= pd.Timestamp(f"{DATE} 07:00") - pd.Timedelta(hours=24)
