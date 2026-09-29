"""The Brain's deterministic bookkeeping: resolve a run, store instances, grade, stats, ladder, pair bias, agent scores.

Everything numeric lives here so the curator agent never computes a threshold or a hit rate itself.
Thresholds come from config/brain.json. Instances in brain/patterns.json are append-only; stats are recomputed.
"""
from __future__ import annotations

import itertools
import json
import re

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .timeutil import ROOT, load_config

BRAIN = ROOT / "brain"
FAMILY_OF = {"SW": "ST", "ST": "ST", "DP": "ST", "FVG": "FVG", "OB": "OB", "LQ": "LQ", "RT": "RT", "DV": "DV", "SMT": "SMT", "BM": "BM"}
SESSIONS = ("asia", "london", "newyork")
PAIRS = ("EURUSD", "GBPUSD")


# ------------------------------------------------------------------ files

def _load(name: str, default):
    p = BRAIN / name
    return json.loads(p.read_text()) if p.exists() and p.read_text().strip() else default


def _save(name: str, obj) -> None:
    BRAIN.mkdir(parents=True, exist_ok=True)
    (BRAIN / name).write_text(json.dumps(obj, indent=1) + "\n")


def load_state() -> dict:
    return {
        "patterns": _load("patterns.json", {"signatures": {}, "hidden_patterns": {}, "big_move_attributions": []}),
        "pending": _load("pending_scores.json", []),
        "agents": _load("agent_scores.json", {"cycles": [], "pair_calls": []}),
        "cycles": _load("cycle_log.json", []),
        "bias": _load("pair_bias.json", {}),
    }


def save_state(st: dict) -> None:
    _save("patterns.json", st["patterns"])
    _save("pending_scores.json", st["pending"])
    _save("agent_scores.json", st["agents"])
    _save("cycle_log.json", st["cycles"])
    _save("pair_bias.json", st["bias"])


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ candle references (B011, M1 only since B012)

BAR_REF = re.compile(r"^bar:(EURUSD|GBPUSD):(M1):(\d{4}-\d{2}-\d{2} \d{2}:\d{2}):(open|high|low|close)$")
BAR_REF_LOOSE = re.compile(r"^bar:([A-Z]{6}):([A-Z]\d+):(\d{4}-\d{2}-\d{2} \d{2}:\d{2}):([a-z]+)$")
BAR_REF_ANY = re.compile(r"bar:[A-Z]{6}:[A-Z]\d+:\d{4}-\d{2}-\d{2} \d{2}:\d{2}:[a-z]+")
FIELD_COL = {"open": 1, "high": 2, "low": 3, "close": 4}


def bar_index(packet: dict) -> dict:
    """(pair, 'M1', 'YYYY-MM-DD HH:MM') -> bar row, from bars_m1 (session) and bars_m1_context (before it)."""
    out, date = {}, packet["date"]
    for pair, pk in (packet.get("pairs") or {}).items():
        for row in pk.get("bars_m1", []):
            out[(pair, "M1", f"{date} {row[0]}" if len(row[0]) == 5 else row[0])] = row
        for row in pk.get("bars_m1_context", []):
            out[(pair, "M1", row[0])] = row
    return out


def resolve_bar(ref: str, packet: dict, bidx: dict | None = None) -> tuple[float | None, str]:
    """Price of a candle reference, or (None, why it is not usable)."""
    loose = BAR_REF_LOOSE.match(ref or "")
    if loose and loose.group(2) != "M1":
        return None, f"{ref}: only M1 candles can be cited"
    m = BAR_REF.match(ref or "")
    if not m:
        return None, f"{ref!r} is not a valid bar reference (bar:<EURUSD|GBPUSD>:M1:<YYYY-MM-DD HH:MM>:<open|high|low|close>)"
    pair, tf, ts, field = m.groups()
    end = pd.Timestamp(f"{packet['date']} {packet['window_utc'][1]}", tz="UTC")
    if pd.Timestamp(ts, tz="UTC") + pd.Timedelta(minutes=1) > end:
        return None, f"{ref} is after the session close"
    row = (bidx if bidx is not None else bar_index(packet)).get((pair, tf, ts))
    if row is None:
        return None, f"{ref} is not in the packet"
    return float(row[FIELD_COL[field]]), ""


class LevelIndex(dict):
    """Object ids -> {pair, kind, tf, prices}; bar references are resolved from the packet's candles on demand."""

    def __init__(self, packet: dict):
        super().__init__()
        self.packet, self._bars = packet, None

    def _bar(self, key):
        if not (isinstance(key, str) and key.startswith("bar:")):
            return None
        if self._bars is None:
            self._bars = bar_index(self.packet)
        price, _ = resolve_bar(key, self.packet, self._bars)
        if price is None:
            return None
        pair, tf, _, field = BAR_REF.match(key).groups()
        v = {"pair": pair, "kind": "BAR", "tf": tf, "field": field, "prices": {field: price}}
        dict.__setitem__(self, key, v)
        return v

    def get(self, key, default=None):
        v = dict.get(self, key)
        if v is None:
            v = self._bar(key)
        return default if v is None else v

    def __getitem__(self, key):
        v = self.get(key)
        if v is None:
            raise KeyError(key)
        return v

    def __contains__(self, key):
        return dict.__contains__(self, key) or self._bar(key) is not None


# ------------------------------------------------------------------ packet objects

def object_index(packet: dict) -> LevelIndex:
    """id -> {pair, kind, tf, prices{...}} for every object in a packet, plus bar references on demand."""
    idx = LevelIndex(packet)
    for pair, pk in (packet.get("pairs") or {}).items():
        for coll in ("swings", "structure", "displacement", "fvg", "order_blocks", "liquidity", "retracements", "divergences", "big_moves"):
            for o in pk.get(coll, []):
                prices = {}
                for k in ("price", "top", "bottom", "mid"):
                    if isinstance(o.get(k), (int, float)):
                        prices[k] = o[k]
                if coll == "order_blocks":
                    third = (o["top"] - o["bottom"]) / 3
                    prices["upper_third"], prices["lower_third"] = o["top"] - third, o["bottom"] + third
                for k, v in (o.get("levels") or {}).items():
                    prices[k] = v
                idx[o["id"]] = {"pair": pair, "kind": o["id"].split("-")[1], "tf": o.get("tf", "M1"), "prices": prices}
    for s in (packet.get("cross") or {}).get("smt", []):
        idx[s["id"]] = {"pair": None, "kind": "SMT", "tf": "M1", "prices": {}}
    return idx


def price_matches(obj: dict, price: float, pip: float = 0.0001, tol_pips: float = 0.2) -> bool:
    return any(abs(v - price) <= tol_pips * pip + 1e-12 for v in obj["prices"].values())


def families_of(object_ids: list[str]) -> list[str]:
    fams = set()
    for oid in object_ids:
        if oid.startswith("bar:"):
            fams.add("BAR")
            continue
        parts = oid.split("-")
        if len(parts) >= 3 and parts[1] in FAMILY_OF:
            fams.add(FAMILY_OF[parts[1]])
    return sorted(fams)


def signature(model: str, pair: str, session: str, tf: str, fams: list[str], candle_basis: str | None = None) -> str:
    base = f"{model}|{pair}|{session}|{tf}|{'+'.join(fams)}"
    return f"{base}|{candle_basis}" if candle_basis else base


# ------------------------------------------------------------------ resolve a run

def _read(p: Path):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def resolve_run(run_dir: Path, packet: dict) -> dict:
    """Apply the plan's survival rule to every candidate of a run. Returns survivors and dropped (with reasons)."""
    a1, a2, a3 = (_read(run_dir / f) for f in ("1_analyst.json", "2_critic.json", "3_strategist.json"))
    if not (a1 and a2 and a3):
        raise ValueError("run incomplete: 1_analyst, 2_critic and 3_strategist are all required")
    for a in (a1, a2, a3):
        if a.get("status", "OK") != "OK" or a.get("packet_sha256") != packet["sha256"]:
            raise ValueError(f"{a.get('agent')}: status {a.get('status')} or packet hash mismatch")
    idx = object_index(packet)
    verdicts = {v["candidate"]: v for v in a2.get("verdicts", [])}
    refuted = {(x["candidate"], x["field"]) for x in a2.get("audit", []) if x.get("verdict") == "refuted"}
    audit3 = {x["candidate"]: x for x in a3.get("third_audit", [])}

    pool = [("analyst", c) for c in a1.get("candidates", [])] + \
           [("critic", c) for c in a2.get("new_candidates", [])] + \
           [("strategist", c) for c in a3.get("new_candidates", [])]
    survivors, dropped = [], []
    for src, c in pool:
        c = json.loads(json.dumps(c))
        cid = c["id"]
        reason = None
        if src == "analyst":
            v = verdicts.get(cid, {})
            if v.get("verdict") not in ("AGREE", "AGREE_WITH_MODS"):
                reason = f"critic verdict {v.get('verdict', 'missing')}"
            elif any((cid, f) in refuted for f in ("entry", "invalidation", "target")):
                reason = "critic refuted entry/invalidation/target"
            else:
                for m in v.get("mods", []):
                    f, oid = m.get("field"), m.get("new_object")
                    if f in ("entry", "invalidation", "target") and oid in idx:
                        pr = idx[oid]["prices"]  # a bar reference has exactly one price: its candle field
                        c[f] = {"object": oid, "price": pr.get("mid", pr.get("price", next(iter(pr.values()), None)))}
            if reason is None and audit3.get(cid, {}).get("cross_pair") == "conflicts":
                reason = "strategist: cross-pair conflict"
        elif src == "critic":
            if cid not in audit3:
                reason = "not audited by strategist"
            elif audit3[cid].get("cross_pair") == "conflicts":
                reason = "strategist: cross-pair conflict"
        # price integrity: every level is an object of this pair at that price, or that candle's field exactly
        if reason is None:
            for f in ("entry", "invalidation", "target"):
                ref = (c.get(f) or {}).get("object")
                o = idx.get(ref)
                if isinstance(ref, str) and ref.startswith("bar:") and o is None:
                    reason = f"{f}: {resolve_bar(ref, packet)[1]}"
                    break
                if o is None or o["pair"] != c.get("pair"):
                    reason = f"{f} is not the price of a {c.get('pair')} object in the packet"
                    break
                if not price_matches(o, float(c[f]["price"])):
                    reason = (f"{f} is not the {o['field']} of that candle" if o["kind"] == "BAR"
                              else f"{f} is not the price of a {c.get('pair')} object in the packet")
                    break
        if reason is None:
            conf = audit3.get(cid, {}).get("confidence", verdicts.get(cid, {}).get("confidence", c.get("confidence")))
            c["confidence"] = conf
        rec = {"source_agent": src, "candidate": c, "reason": reason,
               "critic_verdict": verdicts.get(cid, {}).get("verdict"), "strategist": audit3.get(cid, {}).get("cross_pair")}
        (dropped if reason else survivors).append(rec)
    return {"survivors": survivors, "dropped": dropped, "a1": a1, "a2": a2, "a3": a3}


def ingest_run(st: dict, run_dir: Path, packet_path: Path) -> dict:
    """Store a resolved run: instances for survivors, shadow instances for dropped candidates, big-move attributions."""
    packet = json.loads(packet_path.read_text())
    session, date = packet["session"], packet["date"]
    key = f"{date}_{session}"
    if any(c["key"] == key for c in st["cycles"]):
        raise ValueError(f"cycle {key} already ingested")
    res = resolve_run(run_dir, packet)
    idx = object_index(packet)
    end = pd.Timestamp(f"{date} {packet['window_utc'][1]}", tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
    stored = []
    for kind, recs in (("instance", res["survivors"]), ("shadow", res["dropped"])):
        for r in recs:
            c = r["candidate"]
            try:
                entry, inv, tgt = (float(c[f]["price"]) for f in ("entry", "invalidation", "target"))
            except (KeyError, TypeError, ValueError):
                continue
            levels = [c[f]["object"] for f in ("entry", "invalidation", "target")]
            objs = [o for o in c.get("objects", []) if o in idx]
            objs += [o for o in levels if o not in objs]
            fams = families_of(objs)
            tf = idx.get(c["entry"]["object"], {}).get("tf", "M1")
            basis = c.get("candle_basis") or None
            sig = signature(c.get("model", "other"), c["pair"], session, tf, fams, basis)
            inst = {"id": f"{key}:{c['id']}", "date": date, "session": session, "packet": str(packet_path),
                    "pair": c["pair"], "objects": objs, "direction": c.get("direction"), "entry": entry,
                    "invalidation": inv, "target": tgt, "flagged_at": end, "outcome": "pending", "move_pips": 0,
                    "min_to_target": None, "scored_on": "", "source": c["id"], "source_agent": r["source_agent"],
                    "confidence": c.get("confidence"), "critic_verdict": r["critic_verdict"], "strategist": r["strategist"],
                    "signature": sig, "kind": kind, "drop_reason": r["reason"]}
            st["pending"].append(inst)
            if kind == "instance":
                s = st["patterns"]["signatures"].setdefault(sig, {
                    "model": c.get("model", "other"), "pair": c["pair"], "session": session, "tf": tf, "families": fams,
                    "candle_basis": basis,
                    "status": "candidate", "first_seen": date, "last_seen": date, "status_changed": date,
                    "stats": {}, "instances": []})
                s["last_seen"] = date
                s["instances"].append({k: v for k, v in inst.items() if k not in ("kind", "drop_reason")})
                stored.append(sig)
    for pair, pk in packet["pairs"].items():
        for bm in pk.get("big_moves", []):
            st["patterns"]["big_move_attributions"].append({
                "cycle": key, "pair": pair, "move": bm["id"], "pips": bm["pips"],
                "families": families_of(bm.get("preceded_by", [])), "entry_was_available": bm.get("entry_was_available"),
                "entry_risk_pips": bm.get("entry_risk_pips")})
    pd_ = res["a3"].get("pair_decision", {})
    st["agents"]["pair_calls"].append({"cycle": key, "calls": {s: pd_.get(s) for s in SESSIONS}, "resolved": {}})
    st["agents"]["cycles"].append(agent_cycle_record(key, res))
    st["agents"]["cycles"] = st["agents"]["cycles"][-load_config("brain")["agent_grading_window_cycles"]:]
    st["cycles"].append({"key": key, "date": date, "session": session, "ingested_at": now()})
    return {"cycle": key, "survived": [r["candidate"]["id"] for r in res["survivors"]],
            "dropped": [{"id": r["candidate"].get("id"), "reason": r["reason"]} for r in res["dropped"]],
            "stored_signatures": stored}


def agent_cycle_record(key: str, res: dict) -> dict:
    a1, a2 = res["a1"], res["a2"]
    verdicts = [v.get("verdict") for v in a2.get("verdicts", [])]
    refuted = [x for x in a2.get("audit", []) if x.get("verdict") == "refuted"]
    challenged = sorted({v["candidate"] for v in a2.get("verdicts", []) if v.get("verdict") == "DISAGREE"} |
                        {x["candidate"] for x in refuted})
    return {"cycle": key, "analyst_candidates": len(a1.get("candidates", [])), "analyst_claims_refuted": len(refuted),
            "critic_verdicts": verdicts, "critic_challenged": [f"{key}:{c}" for c in challenged],
            "critic_new": len(a2.get("new_candidates", [])), "strategist_new": len(res["a3"].get("new_candidates", []))}


# ------------------------------------------------------------------ grading

def grade(inst: dict, bars: pd.DataFrame, cfg: dict, pip: float = 0.0001) -> dict | None:
    """Entry must trade first; then target before invalidation within the horizon = hit, invalidation first = miss.
    A bar touching both after entry counts as `same_bar_both_touched` (miss). Returns None while the horizon is open
    and nothing decided. Not triggered by the horizon = expired (reason not_triggered)."""
    t0 = pd.Timestamp(inst["flagged_at"])
    horizon = t0 + pd.Timedelta(hours=cfg["scoring"]["horizon_hours"])
    w = bars[(bars["time"] >= t0) & (bars["time"] < horizon)]
    long = inst["direction"] == "long"
    e, inv, tgt = inst["entry"], inst["invalidation"], inst["target"]
    entered_at, best = None, 0.0
    for t, h, l in zip(w["time"], w["high"], w["low"]):
        if entered_at is None:
            if (l <= e) if long else (h >= e):
                entered_at = t
            else:
                continue
        best = max(best, (h - e) if long else (e - l))
        hit_t = (h >= tgt) if long else (l <= tgt)
        hit_i = (l <= inv) if long else (h >= inv)
        if hit_i and hit_t:
            return _outcome("hit" if cfg["scoring"]["same_bar_both_touched"] == "hit" else "miss", best, pip, entered_at, t, "same_bar")
        if hit_i:
            return _outcome("miss", best, pip, entered_at, t, "invalidation")
        if hit_t:
            return _outcome("hit", best, pip, entered_at, t, "target")
    covered = len(bars) and bars["time"].iat[-1] >= horizon - pd.Timedelta(minutes=1)
    if not covered:
        return None
    return _outcome("expired", best, pip, entered_at, None, "not_triggered" if entered_at is None else "horizon")


def _outcome(o, best, pip, entered_at, t, why):
    return {"outcome": o, "move_pips": round(best / pip, 1), "why": why,
            "min_to_target": int((t - entered_at).total_seconds() // 60) if (o == "hit" and t is not None) else None,
            "scored_on": now()}


def grade_pending(st: dict, bars_by_pair: dict) -> list[dict]:
    cfg = load_config("brain")
    done, still = [], []
    for inst in st["pending"]:
        b = bars_by_pair.get(inst["pair"])
        g = grade(inst, b, cfg) if b is not None and len(b) else None
        if g is None:
            still.append(inst)
            continue
        inst.update(g)
        done.append(inst)
        if inst.get("kind") == "instance":
            sig = st["patterns"]["signatures"].get(inst["signature"])
            for x in (sig or {}).get("instances", []):
                if x["id"] == inst["id"]:
                    x.update(g)
    st["pending"] = still
    graded = st["agents"].setdefault("graded", [])
    graded.extend({"id": i["id"], "kind": i["kind"], "pair": i["pair"], "outcome": i["outcome"],
                   "cycle": i["id"].split(":")[0]} for i in done)
    st["agents"]["graded"] = graded[-5000:]
    return done


# ------------------------------------------------------------------ stats, ladder, bias, agent scores

def sig_stats(instances: list[dict], pip: float = 0.0001) -> dict:
    graded = [i for i in instances if i["outcome"] in ("hit", "miss")]
    hits = sum(i["outcome"] == "hit" for i in graded)
    last = graded[-20:]
    risk = [abs(i["entry"] - i["invalidation"]) / pip for i in instances]
    rr = [abs(i["target"] - i["entry"]) / abs(i["entry"] - i["invalidation"]) for i in instances if i["entry"] != i["invalidation"]]
    ttt = sorted(i["min_to_target"] for i in graded if i["outcome"] == "hit" and i.get("min_to_target") is not None)
    mean = lambda xs: round(sum(xs) / len(xs), 2) if xs else 0  # noqa: E731
    return {"n": len(graded), "hits": hits, "misses": len(graded) - hits,
            "expired": sum(i["outcome"] == "expired" for i in instances),
            "pending": sum(i["outcome"] == "pending" for i in instances),
            "hit_rate": round(hits / len(graded), 3) if graded else 0,
            "last_20_hit_rate": round(sum(i["outcome"] == "hit" for i in last) / len(last), 3) if last else 0,
            "avg_move_pips": mean([i.get("move_pips", 0) for i in graded]), "avg_risk_pips": mean(risk),
            "avg_rr": mean(rr), "median_min_to_target": ttt[len(ttt) // 2] if ttt else 0}


def apply_ladder(st: dict) -> list[dict]:
    L = load_config("brain")["ladder"]
    cycles = [c["key"] for c in st["cycles"]]
    changes = []
    for key, s in st["patterns"]["signatures"].items():
        s["stats"] = sig_stats(s["instances"])
        x, old = s["stats"], s["status"]
        last_key = f"{s['last_seen']}_{s['session']}"
        idle = len([c for c in cycles if c > last_key])
        new = old
        if old == "retired":
            fresh = [i for i in s["instances"] if i["date"] > s["status_changed"] and i["outcome"] in ("hit", "miss")]
            if len(fresh) >= L["revive_fresh_instances"] and \
                    sum(i["outcome"] == "hit" for i in fresh) / len(fresh) >= L["revive_min_hit_rate"]:
                new = "candidate"
        else:
            if (x["n"] >= L.get("retire_min_n", 10) and x["last_20_hit_rate"] < L["retire_last20_below"]) or \
                    idle >= L["retire_idle_sessions"]:
                new = "retired"
            elif old in ("candidate", "validated"):
                v, c = L["validated"], L["core"]
                ok = lambda t: x["n"] >= t["min_n"] and x["hit_rate"] >= t["min_hit_rate"] and x["avg_rr"] >= t["min_avg_rr"]  # noqa: E731
                if ok(c):
                    new = "core"
                elif old == "candidate" and ok(v):
                    new = "validated"
        if new != old:
            s["status"], s["status_changed"] = new, (cycles[-1][:10] if cycles else s["last_seen"])
            changes.append({"signature": key, "from": old, "to": new, "n": x["n"], "hit_rate": x["hit_rate"],
                            "last_20": x["last_20_hit_rate"], "avg_rr": x["avg_rr"], "idle_sessions": idle})
    return changes


def compute_pair_bias(st: dict) -> dict:
    B = load_config("brain")["pair_bias"]
    out = {}
    for sess in SESSIONS:
        rates, ns = {}, {}
        for pair in PAIRS:
            inst = [i for s in st["patterns"]["signatures"].values()
                    if s["pair"] == pair and s["session"] == sess and s["status"] in ("validated", "core")
                    for i in s["instances"] if i["outcome"] in ("hit", "miss")]
            inst.sort(key=lambda i: i["flagged_at"])
            last = inst[-B["window"]:]
            ns[pair] = len(last)
            rates[pair] = round(sum(i["outcome"] == "hit" for i in last) / len(last), 3) if last else None
        e, g = rates["EURUSD"], rates["GBPUSD"]
        enough = all(n >= B["min_n_each"] for n in ns.values())
        if enough and e < B["neither_below"] and g < B["neither_below"]:
            call = "neither"
        elif enough and abs(e - g) >= B["lead_by"]:
            call = "EURUSD" if e > g else "GBPUSD"
        else:
            call = "either"
        out[sess] = {"call": call, "hit_rate": rates, "n": ns, "enough_data": enough}
    return out


def agent_scores(st: dict) -> dict:
    cfg = load_config("brain")
    cyc = st["agents"]["cycles"][-cfg["agent_grading_window_cycles"]:]
    graded = {g["id"]: g["outcome"] for g in st["agents"].get("graded", [])}
    verdicts = [v for c in cyc for v in c["critic_verdicts"] if v]
    agree = sum(v in ("AGREE", "AGREE_WITH_MODS") for v in verdicts)
    challenged = [cid for c in cyc for cid in c["critic_challenged"]]
    catches = sum(graded.get(cid) == "miss" for cid in challenged)
    false_alarms = sum(graded.get(cid) == "hit" for cid in challenged)
    prec = round(catches / (catches + false_alarms), 3) if (catches + false_alarms) else None
    agree_rate = round(agree / len(verdicts), 3) if verdicts else None
    # strategist pair calls vs the pair whose flagged setups scored better in that session
    by_cycle: dict[str, dict] = {}
    for g in st["agents"].get("graded", []):
        if g["outcome"] in ("hit", "miss"):
            d = by_cycle.setdefault(g["cycle"], {p: [0, 0] for p in PAIRS})
            d[g["pair"]][0] += g["outcome"] == "hit"
            d[g["pair"]][1] += 1
    right = wrong = 0
    keys = [c["key"] for c in st["cycles"]]
    for pc in st["agents"]["pair_calls"]:
        i = keys.index(pc["cycle"]) if pc["cycle"] in keys else -1
        for nxt in keys[i + 1:]:
            sess = nxt.split("_", 1)[1]
            call = pc["calls"].get(sess)
            if call in (None, "either", "neither") or nxt not in by_cycle:
                continue
            d = by_cycle[nxt]
            rate = {p: (d[p][0] / d[p][1]) if d[p][1] else None for p in PAIRS}
            if None in rate.values() or rate["EURUSD"] == rate["GBPUSD"]:
                continue
            realised = max(rate, key=rate.get)
            right += call == realised
            wrong += call != realised
            break
    drift = cfg["reviewer_drift"]
    flag = bool((agree_rate is not None and len(verdicts) >= drift["min_verdicts"] and agree_rate > drift["max_agreement"]) or
                (prec is not None and catches + false_alarms >= 10 and prec < drift["min_catch_precision"]))
    return {"window_cycles": len(cyc),
            "analyst": {"candidates": sum(c["analyst_candidates"] for c in cyc),
                        "claims_refuted": sum(c["analyst_claims_refuted"] for c in cyc)},
            "critic": {"agreement_rate": agree_rate, "catches": catches, "false_alarms": false_alarms,
                       "catch_precision": prec, "new_candidates": sum(c["critic_new"] for c in cyc)},
            "strategist": {"pair_calls_right": right, "pair_calls_wrong": wrong,
                           "new_candidates": sum(c["strategist_new"] for c in cyc)},
            "reviewer_drift": flag}


def recompute(st: dict) -> dict:
    changes = apply_ladder(st)
    bias = compute_pair_bias(st)
    st["bias"] = {**{k: v for k, v in st["bias"].items() if k not in ("computed", "updated")},
                  "computed": bias, "updated": now()}
    scores = agent_scores(st)
    st["agents"]["summary"] = scores
    counts = {}
    for s in st["patterns"]["signatures"].values():
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    return {"status_changes": changes, "pair_bias": bias, "agent_scores": scores, "signature_counts": counts,
            "pending": len(st["pending"])}


# ------------------------------------------------------------------ weekly

def merge_signatures(st: dict) -> list[dict]:
    thr = load_config("brain")["merge_signatures_object_overlap"]
    sigs = st["patterns"]["signatures"]
    merged = []
    keys = sorted(sigs, key=lambda k: sigs[k]["first_seen"])
    gone = set()
    for a, b in itertools.combinations(keys, 2):
        if a in gone or b in gone:
            continue
        A, Bs = sigs[a], sigs[b]
        if (A["model"], A["pair"], A["session"], A.get("candle_basis")) != \
                (Bs["model"], Bs["pair"], Bs["session"], Bs.get("candle_basis")):
            continue
        fa, fb = set(A["families"]), set(Bs["families"])
        if len(fa | fb) and len(fa & fb) / len(fa | fb) >= thr:
            A["instances"] = sorted(A["instances"] + Bs["instances"], key=lambda i: i["flagged_at"])
            A.setdefault("merged_from", []).append(b)
            A["last_seen"] = max(A["last_seen"], Bs["last_seen"])
            gone.add(b)
            merged.append({"kept": a, "merged": b})
    for b in gone:
        del sigs[b]
    return merged


def precursor_mining(st: dict) -> list[dict]:
    P = load_config("brain")["precursor_mining"]
    recent = [c["key"] for c in st["cycles"]]
    found = []
    for sess in SESSIONS:
        keys = [k for k in recent if k.endswith(sess)][-P["sessions"]:]
        moves = [m for m in st["patterns"]["big_move_attributions"] if m["cycle"] in keys]
        if len(moves) < P["min_n"]:
            continue
        combos: dict[tuple, int] = {}
        for m in moves:
            fams = sorted(set(m["families"]) - {"BM"})
            for r in (2, 3):
                for combo in itertools.combinations(fams, r):
                    combos[combo] = combos.get(combo, 0) + 1
        for combo, k in combos.items():
            share = k / len(moves)
            if share >= P["min_share"]:
                slug = f"hp_{sess}_{'_'.join(c.lower() for c in combo)}"
                hp = st["patterns"]["hidden_patterns"]
                if slug not in hp:
                    hp[slug] = {"rule": f"{' + '.join(combo)} precede big moves in {sess}", "proposed_by": "precursor_mining",
                                "instances": k, "share": round(share, 3), "backtest": "pending", "research_ref": ""}
                    found.append({"slug": slug, "share": round(share, 3), "n_moves": len(moves)})
    return found



