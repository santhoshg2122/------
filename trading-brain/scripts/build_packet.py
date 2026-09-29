#!/usr/bin/env python3
"""Build the Chart Packet (plan section 3) for one session.

    python scripts/build_packet.py --session london --date 2026-09-29

Reads data/EURUSD_M1.csv and data/GBPUSD_M1.csv (UTC), cuts them at the session end (nothing after it is
ever read), detects every object with scripts/lib/detectors.py and writes data/packets/<date>_<session>.json.
A packet is always written; `status` is OK, STALE, GAP or MISSING and /cycle runs only on OK.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.lib import detectors as D  # noqa: E402
from scripts.lib.ingest import load_utc  # noqa: E402
from scripts.lib.timeutil import ROOT, load_config  # noqa: E402

PAIRS = ("EURUSD", "GBPUSD")


def r5(x):
    return None if x is None else round(float(x), 5)


def hm(t: pd.Timestamp, date: str) -> str:
    """HH:MM on the packet date, 'YYYY-MM-DD HH:MM' otherwise."""
    return t.strftime("%H:%M") if t.strftime("%Y-%m-%d") == date else t.strftime("%Y-%m-%d %H:%M")


def window(date: str, session: str, scfg: dict) -> tuple[pd.Timestamp, pd.Timestamp]:
    s = scfg["sessions"][session]
    return (pd.Timestamp(f"{date} {s['start']}", tz="UTC"), pd.Timestamp(f"{date} {s['end']}", tz="UTC"))


def sha256_files(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(p.read_bytes())
    return h.hexdigest()


def check_status(bars: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, scfg: dict) -> tuple[str, str]:
    w = bars[(bars["time"] >= start) & (bars["time"] < end)]
    if w.empty:
        return "MISSING", "no bars inside the session window"
    last_close = w["time"].iat[-1] + pd.Timedelta(minutes=1)
    if (end - last_close) > pd.Timedelta(minutes=scfg["stale_after_close_min"]):
        return "STALE", f"last bar {w['time'].iat[-1]:%H:%M} is more than {scfg['stale_after_close_min']} min before the close"
    t = pd.concat([pd.Series([start - pd.Timedelta(minutes=1)]), w["time"]], ignore_index=True)
    missing = (t.diff() - pd.Timedelta(minutes=1)).dt.total_seconds().div(60).fillna(0)
    if (missing > scfg["gap_max_min"]).any():
        k = int(missing.idxmax())
        return "GAP", f"{int(missing.max())} missing minutes before {t.iat[k]:%H:%M}"
    return "OK", ""


class Ids:
    def __init__(self, prefix: str):
        self.prefix, self.n = prefix, {}

    def __call__(self, kind: str) -> str:
        self.n[kind] = self.n.get(kind, 0) + 1
        return f"{self.prefix}-{kind}-{self.n[kind]}"


def pair_objects(m1: pd.DataFrame, start, end, date: str, session: str, pair: str, scfg: dict, dcfg: dict) -> dict:
    """All detector objects for one pair. `m1` holds context + window bars, all < end."""
    pip = scfg["pairs"][pair]["pip"]
    ids = Ids(scfg["pairs"][pair]["prefix"])
    n = dcfg["swing_bars_each_side"]
    win = m1[m1["time"] >= start].reset_index(drop=True)
    tfs = {tf: D.resample(m1, tf) for tf in dcfg["timeframes"]}
    atrs = {tf: D.atr(df, dcfg["atr_period"]) for tf, df in tfs.items()}

    def in_scope(tf, t):
        return t >= start if (tf == "M1" and dcfg["m1_objects_in_window_only"]) else True

    raw = []  # (time, kind, tf, payload) — ids are handed out in time order afterwards
    sw_by_tf = {}
    for tf, df in tfs.items():
        sw = D.swings(df, n)
        sw_by_tf[tf] = sw
        st = D.structure(df, sw)
        dp = D.displacement(df, atrs[tf], dcfg["displacement"]["min_range_atr"], dcfg["displacement"]["min_body_frac"])
        fv = D.fvgs(df, atrs[tf], dcfg["fvg"]["min_size_atr"])
        ob = D.order_blocks(df, dp, st, dcfg["order_block"]["lookback_bars"], dcfg["order_block"]["displacement_within_bars"])
        for kind, items in (("SW", sw), ("ST", st), ("DP", dp), ("FVG", fv), ("OB", ob)):
            for it in items:
                if in_scope(tf, it["time"]):
                    raw.append((it["time"], kind, tf, it))
        if tf in dcfg["divergence"]["tfs"]:
            dv = D.divergences(df, sw, dcfg["divergence"]["min_bars_apart"], dcfg["divergence"]["max_bars_apart"],
                               dcfg["divergence"]["rsi_period"], dcfg["divergence"]["macd"])
            for it in dv:
                raw.append((it["time"], "DV", tf, it))
        if tf == dcfg["retracement"]["tf"]:
            rc = dcfg["retracement"]
            for it in D.retracements(df, sw, atrs[tf], rc["levels"], rc["min_leg_atr"], rc["max_legs"]):
                raw.append((it["time"], "RT", tf, it))
    raw.sort(key=lambda r: (r[0], ["SW", "ST", "DP", "FVG", "OB", "DV", "RT"].index(r[1])))

    key = {}  # id(payload) -> object id
    for _, kind, _, it in raw:
        key[id(it)] = ids(kind)

    def sid(s):
        return key.get(id(s))

    out = {k: [] for k in ("swings", "structure", "displacement", "fvg", "order_blocks", "divergences", "retracements")}
    fvg_objs = []
    for t, kind, tf, it in raw:
        oid = key[id(it)]
        if kind == "SW":
            out["swings"].append({"id": oid, "tf": tf, "type": it["type"], "time": hm(t, date), "price": r5(it["price"]), "_t": t})
        elif kind == "ST":
            out["structure"].append({"id": oid, "tf": tf, "event": it["event"], "time": hm(t, date), "price": r5(it["price"]),
                                     "broke": sid(it["broke"]), "_t": t})
        elif kind == "DP":
            out["displacement"].append({"id": oid, "tf": tf, "time": hm(t, date), "dir": it["dir"], "range_atr": it["range_atr"],
                                        "created": [], "_i": it["i"], "_t": t})
        elif kind == "FVG":
            o = {"id": oid, "tf": tf, "dir": it["dir"], "top": r5(it["top"]), "bottom": r5(it["bottom"]), "mid": r5(it["mid"]),
                 "time": hm(t, date), "filled_pct": it["filled_pct"], "inverse": it["inverse"], "_i": it["i"], "_t": t}
            out["fvg"].append(o)
            fvg_objs.append((tf, o))
        elif kind == "OB":
            out["order_blocks"].append({"id": oid, "tf": tf, "dir": it["dir"], "top": r5(it["top"]), "bottom": r5(it["bottom"]),
                                        "time": hm(t, date), "mitigated_pct": it["mitigated_pct"], "breaker": it["breaker"], "_t": t})
        elif kind == "DV":
            out["divergences"].append({"id": oid, "tf": tf, "osc": it["osc"], "kind": it["kind"], "swing_a": sid(it["a"]),
                                       "swing_b": sid(it["b"]), "osc_a": it["osc_a"], "osc_b": it["osc_b"], "_t": t})
        elif kind == "RT":
            p, q = it["leg"]
            out["retracements"].append({"id": oid, "leg": [sid(p), sid(q)], "dir": it["dir"],
                                        "levels": {k: r5(v) for k, v in it["levels"].items()}, "touched": it["touched"], "_t": t})
    # displacement -> the FVG it created (FVG middle bar == displacement bar, same timeframe)
    for d in out["displacement"]:
        d["created"] = [o["id"] for tf, o in fvg_objs if tf == d["tf"] and o["_i"] == d["_i"]]

    # session levels and liquidity pools
    lv = session_levels(m1, start, end, date, session, scfg)
    pools = []
    reclaim = dcfg["liquidity"]["reclaim_within_bars"]
    m1r = m1.reset_index(drop=True)
    for name in ("asia_high", "asia_low", "pdh", "pdl", "prev_session_high", "prev_session_low"):
        if lv.get(name) is None:
            continue
        side = "high" if name.endswith(("high", "h")) else "low"
        st_ = D.sweep_state(m1r, {"price": lv[name]}, side, start - pd.Timedelta(minutes=1), reclaim)
        pools.append({"type": name, "price": lv[name], "touches": 1, "formed": start, "side": side, **st_})
    eq = D.equal_pools(sw_by_tf[dcfg["liquidity"]["equal_tf"]], dcfg["liquidity"]["equal_tol_pips"] * pip)
    for p in eq:
        side = "high" if p["type"] == "equal_highs" else "low"
        st_ = D.sweep_state(m1r, p, side, p["formed"], reclaim)
        pools.append({"type": p["type"], "price": p["price"], "touches": p["touches"], "formed": p["formed"], "side": side,
                      "members": [sid(s) for s in p["members"]], **st_})
    pools.sort(key=lambda p: (p["formed"], p["type"]))
    all_sw = [s for tf in ("M1", "M5") for s in sw_by_tf.get(tf, []) if sid(s)]
    liq = []
    for p in pools:
        swept_by = None
        if p["swept"]:
            cands = [s for s in all_sw if s["type"] == p["side"] and abs((s["time"] - p["swept_time"]).total_seconds()) <= 300
                     and ((s["price"] > p["price"]) if p["side"] == "high" else (s["price"] < p["price"]))]
            if cands:
                swept_by = sid(min(cands, key=lambda s: abs((s["time"] - p["swept_time"]).total_seconds())))
        o = {"id": ids("LQ"), "type": p["type"], "price": r5(p["price"]), "touches": p["touches"], "swept": p["swept"],
             "swept_time": hm(p["swept_time"], date) if p["swept"] else None, "reclaimed": p["reclaimed"], "swept_by": swept_by,
             "_side": p["side"], "_swept_t": p["swept_time"], "_formed": p["formed"]}
        if "members" in p:
            o["members"] = p["members"]
        liq.append(o)

    # big moves (window only)
    atr15 = float(atrs["M15"].iat[-1]) if len(atrs["M15"]) else 0.0
    bms = []
    if len(win) and atr15 > 0:
        pre0, pre1 = dcfg["big_move"]["preceded_window_min"]
        maxrisk = scfg["pairs"][pair]["max_risk_pips"]
        timed = ([(o["_t"], o["id"]) for o in out["fvg"] + out["order_blocks"] + out["divergences"]
                  + out["displacement"] + out["structure"]] +
                 [(o["_swept_t"], o["id"]) for o in liq if o["swept"]])
        for mv in D.big_moves(win, atr15, dcfg["big_move"]):
            lo_t, hi_t = mv["start"] + pd.Timedelta(minutes=pre0), mv["start"] + pd.Timedelta(minutes=pre1)
            pre = [i for t, i in sorted(timed) if lo_t <= t <= hi_t]
            want = "bull" if mv["dir"] == "up" else "bear"
            entries = [o for o in out["fvg"] + out["order_blocks"]
                       if o["dir"] == want and mv["start"] <= o["_t"] <= mv["end"]]
            entries.sort(key=lambda o: o["_t"])
            first, risk = None, None
            if entries:
                e = entries[0]
                if "mid" in e:
                    px = e["mid"]
                else:
                    third = (e["top"] - e["bottom"]) / 3
                    px = e["top"] - third if want == "bull" else e["bottom"] + third
                first, risk = e["id"], round(abs(px - mv["start_price"]) / pip, 1)
            bms.append({"id": ids("BM"), "start": hm(mv["start"], date), "end": hm(mv["end"], date),
                        "pips": round((mv["end_price"] - mv["start_price"]) / pip, 1), "preceded_by": pre,
                        "first_entry_object": first, "entry_risk_pips": risk,
                        "entry_was_available": bool(risk is not None and risk <= maxrisk),
                        "_start": mv["start"], "_end": mv["end"], "_dir": mv["dir"]})

    prune(out, liq, bms, start, dcfg)

    def rows(df, lo=start, hi=end, fmt="%H:%M"):
        w = df[(df["time"] >= lo) & (df["time"] < hi)]
        return [[t.strftime(fmt), r5(o), r5(h), r5(l), r5(c), int(v)]
                for t, o, h, l, c, v in zip(w["time"], w["open"], w["high"], w["low"], w["close"], w["tick_volume"])]

    ctx_lo = start - pd.Timedelta(hours=scfg["context_hours"])

    return {
        "pip": pip,
        # the agents read M1 candles only (B012): the session window, and the context before it with full timestamps.
        # M5/M15 are still built above for the detectors; they are not given to the agents as candles.
        "bars_m1": rows(tfs["M1"]),
        "bars_m1_context": rows(tfs["M1"], ctx_lo, start, "%Y-%m-%d %H:%M"),
        "atr14": {tf.lower(): r5(a.iat[-1]) for tf, a in atrs.items() if len(a)},
        "session_levels": {k: r5(v) for k, v in lv.items()},
        **out, "liquidity": liq, "big_moves": bms,
    }


def prune(out: dict, liq: list, bms: list, start, dcfg: dict) -> None:
    """Drop context objects that no longer matter at the close, keeping anything another object cites.

    Kept from before the window: live FVGs/OBs (not fully filled/mitigated, or breakers), unswept pools and pools
    swept inside the window, structure and swings from the last `context_keep_hours`, the last retracement legs.
    Divergences and displacement only when they complete inside the window.
    """
    keep_from = start - pd.Timedelta(hours=dcfg.get("context_keep_hours", 8))
    rules = {
        "fvg": lambda o: o["_t"] >= start or (o["filled_pct"] < 100 and not o["inverse"]),
        "order_blocks": lambda o: o["_t"] >= start or o["mitigated_pct"] < 100 or o["breaker"],
        "divergences": lambda o: o["_t"] >= start,
        "displacement": lambda o: o["_t"] >= start,
        "structure": lambda o: o["_t"] >= keep_from,
        "retracements": lambda o: True,
    }
    for k, f in rules.items():
        out[k] = [o for o in out[k] if f(o)]
    liq[:] = [o for o in liq if not o["swept"] or o["_swept_t"] >= start]
    cited = set()
    for coll in list(out.values()) + [liq, bms]:
        for o in coll:
            for key in ("broke", "swing_a", "swing_b", "swept_by"):
                if o.get(key):
                    cited.add(o[key])
            for key in ("leg", "members", "created", "preceded_by"):
                cited.update(x for x in (o.get(key) or []) if x)
            if o.get("first_entry_object"):
                cited.add(o["first_entry_object"])
    out["swings"] = [o for o in out["swings"] if o["id"] in cited or pd.Timestamp(o["_t"]) >= keep_from]


def session_levels(m1: pd.DataFrame, start, end, date: str, session: str, scfg: dict) -> dict:
    d0 = pd.Timestamp(date, tz="UTC")
    lv = {"open": float(m1[m1["time"] >= start]["open"].iat[0]) if (m1["time"] >= start).any() else None}
    if session != "asia":
        a = scfg["sessions"]["asia"]
        w = m1[(m1["time"] >= pd.Timestamp(f"{date} {a['start']}", tz="UTC")) &
               (m1["time"] < min(pd.Timestamp(f"{date} {a['end']}", tz="UTC"), start))]
        if len(w):
            lv["asia_high"], lv["asia_low"] = float(w["high"].max()), float(w["low"].min())
    prev = m1[m1["time"] < d0]
    if len(prev):
        pd_day = prev["time"].iat[-1].normalize()
        w = prev[prev["time"] >= pd_day]
        lv["pdh"], lv["pdl"] = float(w["high"].max()), float(w["low"].min())
    ps = scfg["sessions"][session]
    pw_date = (d0 + pd.Timedelta(days=ps["prev_day_offset"])).strftime("%Y-%m-%d")
    pstart, pend = window(pw_date, ps["prev"], scfg)
    w = m1[(m1["time"] >= pstart) & (m1["time"] < min(pend, start))]
    if len(w) and not (session == "london" and ps["prev"] == "asia"):  # London's previous session *is* Asia
        lv["prev_session_high"], lv["prev_session_low"] = float(w["high"].max()), float(w["low"].min())
    return lv


def cross_pair(bars: dict, start, end, packs: dict, date: str, dcfg: dict) -> dict:
    e = bars["EURUSD"].set_index("time")["close"]
    g = bars["GBPUSD"].set_index("time")["close"]
    j = pd.concat([e, g], axis=1, keys=["e", "g"]).dropna()
    j = j[(j.index >= start) & (j.index < end)]
    out = {"corr_m5_60": None, "lead_lag_min": 0, "leader": "none", "eurgbp_session": "flat", "smt": []}
    if len(j) < 10:
        return out
    m5 = j.resample("5min").last().dropna().pct_change().dropna().tail(dcfg["cross"]["corr_m5_bars"])
    if len(m5) > 3:
        out["corr_m5_60"] = round(float(m5["e"].corr(m5["g"])), 3)
    r = j.pct_change().dropna()
    L = D.lead_lag(r["e"], r["g"], dcfg["cross"]["max_lag_min"])
    out["lead_lag_min"] = int(L)
    out["leader"] = "GBPUSD" if L < 0 else "EURUSD" if L > 0 else "none"
    ratio = (j["e"] / j["g"])
    ch = 100 * (ratio.iat[-1] / ratio.iat[0] - 1)
    flat = dcfg["cross"]["eurgbp_flat_pct"]
    out["eurgbp_session"] = "up" if ch > flat else "down" if ch < -flat else "flat"
    # SMT: one pair sweeps a pool, the equivalent pool on the other pair is not swept within N minutes
    tol = pd.Timedelta(minutes=dcfg["smt"]["max_minutes_apart"])
    match_tol = pd.Timedelta(minutes=dcfg["smt"]["equal_pool_match_minutes"])
    n = 0
    for a, b in (("EURUSD", "GBPUSD"), ("GBPUSD", "EURUSD")):
        for p in packs[a]["liquidity"]:
            if not p["swept"] or p["_swept_t"] < start:
                continue
            twins = [q for q in packs[b]["liquidity"] if q["type"] == p["type"]]
            if p["type"] in ("equal_highs", "equal_lows"):
                twins = [q for q in twins if abs(q["_formed"] - p["_formed"]) <= match_tol]
                twins.sort(key=lambda q: abs(q["_formed"] - p["_formed"]))
            if not twins:
                continue
            twin = twins[0]
            took = twin["swept"] and twin["_swept_t"] is not None and abs(twin["_swept_t"] - p["_swept_t"]) <= tol
            if not took and not (twin["swept"] and twin["_swept_t"] < p["_swept_t"] - tol):
                n += 1
                out["smt"].append({"id": f"X-SMT-{n}", "time": hm(p["_swept_t"], date), "swept": p["id"], "failed": twin["id"],
                                   "reading": f"{a} took the {p['type'].replace('_', ' ')}, {b} did not"})
    out["smt"].sort(key=lambda s: s["time"])
    for i, s in enumerate(out["smt"], 1):
        s["id"] = f"X-SMT-{i}"
    out["swing_divergence"] = swing_divergence(packs, start, date, dcfg)
    return out


def swing_divergence(packs: dict, start, date: str, dcfg: dict) -> list[dict]:
    """EUR vs GBP swing divergence: at matching consecutive swings (same type, within N minutes of each other),
    one pair makes a higher high while the other makes a lower high (or the mirror for lows). Session only."""
    cfg = dcfg.get("swing_divergence", {"tf": "M5", "max_minutes_apart": 10})
    tol = pd.Timedelta(minutes=cfg["max_minutes_apart"])
    sw = {p: [s for s in packs[p].get("swings", []) if s["tf"] == cfg["tf"]] for p in ("EURUSD", "GBPUSD")}
    out = []
    for typ in ("high", "low"):
        e = [s for s in sw["EURUSD"] if s["type"] == typ]
        g = [s for s in sw["GBPUSD"] if s["type"] == typ]
        for a, b in zip(e, e[1:]):
            if b["_t"] < start:
                continue
            ga = min(g, key=lambda s: abs(s["_t"] - a["_t"]), default=None)
            gb = min(g, key=lambda s: abs(s["_t"] - b["_t"]), default=None)
            if ga is None or gb is None or ga is gb or abs(ga["_t"] - a["_t"]) > tol or abs(gb["_t"] - b["_t"]) > tol:
                continue
            e_up, g_up = b["price"] > a["price"], gb["price"] > ga["price"]
            if e_up == g_up:
                continue
            word = ("higher high", "lower high") if typ == "high" else ("higher low", "lower low")
            out.append({"time": hm(b["_t"], date), "type": typ, "EURUSD": [a["id"], b["id"]], "GBPUSD": [ga["id"], gb["id"]],
                        "reading": f"EURUSD {word[0] if e_up else word[1]}, GBPUSD {word[0] if g_up else word[1]}",
                        "_t": b["_t"]})
    out.sort(key=lambda x: x["_t"])
    for i, x in enumerate(out, 1):
        x["id"] = f"X-SDV-{i}"
    return [{"id": x["id"], **{k: v for k, v in x.items() if k != "id"}} for x in out]


def news_for(start, end, dcfg: dict, cal_path: Path) -> tuple[list, list]:
    if not cal_path.exists():
        return [], []
    try:
        cal = json.loads(cal_path.read_text())
    except json.JSONDecodeError:
        return [], []
    bo = pd.Timedelta(minutes=dcfg["news"]["blackout_minutes"])
    news, blackouts = [], []
    for ev in cal:
        t = pd.Timestamp(ev["time"]).tz_convert("UTC") if pd.Timestamp(ev["time"]).tzinfo else pd.Timestamp(ev["time"], tz="UTC")
        if start - bo <= t <= end + bo:
            news.append({"time": t.strftime("%H:%M"), "currency": ev.get("currency"), "impact": ev.get("impact"), "title": ev.get("title")})
            if ev.get("impact") in dcfg["news"]["impact"]:
                blackouts.append([(t - bo).strftime("%H:%M"), (t + bo).strftime("%H:%M")])
    return news, blackouts


def strip_private(o):
    if isinstance(o, dict):
        return {k: strip_private(v) for k, v in o.items() if not k.startswith("_")}
    if isinstance(o, list):
        return [strip_private(v) for v in o]
    return o


def dump(o, ind: int = 0) -> str:
    """JSON with one object/row per line: readable by an agent, bar rows kept compact."""
    pad = " " * ind
    if isinstance(o, dict):
        if not o:
            return "{}"
        if all(not isinstance(v, (dict, list)) or (isinstance(v, list) and all(not isinstance(x, (dict, list)) for x in v))
               for v in o.values()) and ind > 2:
            return json.dumps(o, separators=(", ", ": "))
        items = [f'{pad} {json.dumps(k)}: {dump(v, ind + 1)}' for k, v in o.items()]
        return "{\n" + ",\n".join(items) + f"\n{pad}}}"
    if isinstance(o, list):
        if not o:
            return "[]"
        if all(not isinstance(x, (dict, list)) for x in o):
            return json.dumps(o, separators=(",", ":"))
        if all(isinstance(x, list) for x in o):  # bar rows, 10 per line
            chunks = [",".join(json.dumps(x, separators=(",", ":")) for x in o[i:i + 10]) for i in range(0, len(o), 10)]
            return "[\n" + ",\n".join(f"{pad} {c}" for c in chunks) + f"\n{pad}]"
        return "[\n" + ",\n".join(f"{pad} {json.dumps(x, separators=(', ', ': '))}" for x in o) + f"\n{pad}]"
    return json.dumps(o)


def load_history(path: Path, pair: str) -> pd.DataFrame:
    """A year of M1 is slow to parse per session: cache the ingested bars as parquet next to the CSV."""
    cache = path.with_suffix(".parquet")
    if cache.exists() and cache.stat().st_mtime >= path.stat().st_mtime:
        return pd.read_parquet(cache)
    b, _ = load_utc(path, pair)
    b.to_parquet(cache, index=False)
    return b


def build(session: str, date: str, data_dir: Path = ROOT / "data", out_dir: Path | None = None,
          history: bool = False) -> Path:
    """history=True reads data/history/<PAIR>_M1.csv (replay, B013); the cut at the close is the same."""
    scfg, dcfg = load_config("sessions"), load_config("detectors")
    out_dir = out_dir or data_dir / "packets"
    out_dir.mkdir(parents=True, exist_ok=True)
    start, end = window(date, session, scfg)
    src = data_dir / "history" if history else data_dir
    paths = [src / f"{p}_M1.csv" for p in PAIRS]
    pkt = {"session": session, "date": date, "window_utc": [start.strftime("%H:%M"), end.strftime("%H:%M")],
           "sha256": None, "status": "OK", "status_reason": ""}
    out_path = out_dir / f"{date}_{session}.json"
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        pkt.update(status="MISSING", status_reason=f"not found: {', '.join(missing)}")
        out_path.write_text(dump(pkt) + "\n")
        return out_path
    if not history:
        pkt["sha256"] = sha256_files(paths)
    ctx0 = start - pd.Timedelta(hours=scfg["context_hours"])
    ctx0 = min(ctx0, (pd.Timestamp(date, tz="UTC") - pd.Timedelta(days=3)))
    bars = {}
    for pair, p in zip(PAIRS, paths):
        try:
            b = load_history(p, pair) if history else load_utc(p, pair)[0]
        except (ValueError, IndexError, KeyError) as e:  # empty or unreadable export: skip the session, never crash
            pkt.update(status="MISSING", status_reason=f"{pair}: {p.name} unreadable ({type(e).__name__})")
            out_path.write_text(dump(pkt) + "\n")
            return out_path
        bars[pair] = b[(b["time"] >= ctx0) & (b["time"] < end)].reset_index(drop=True)  # never read past the close
        st, why = check_status(bars[pair], start, end, scfg)
        if st != "OK":
            pkt.update(status=st, status_reason=f"{pair}: {why}")
            out_path.write_text(dump(pkt) + "\n")
            return out_path
    if history:  # a year-long file hashes the same for every session: hash the bars this packet is built from
        h = hashlib.sha256()
        for pair in PAIRS:
            h.update(bars[pair].to_csv(index=False).encode())
        pkt["sha256"] = h.hexdigest()
    packs = {pair: pair_objects(bars[pair], start, end, date, session, pair, scfg, dcfg) for pair in PAIRS}
    pkt["pairs"] = packs
    pkt["cross"] = cross_pair(bars, start, end, packs, date, dcfg)
    pkt["news"], pkt["news_blackouts"] = news_for(start, end, dcfg, data_dir / "calendar.json")
    out_path.write_text(dump(strip_private(pkt)) + "\n")
    return out_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", required=True, choices=["asia", "london", "newyork"])
    ap.add_argument("--date", required=True)
    ap.add_argument("--data", type=Path, default=ROOT / "data")
    ap.add_argument("--history", action="store_true", help="build from data/history/ (replay)")
    a = ap.parse_args(argv)
    p = build(a.session, a.date, a.data, history=a.history)
    st = json.loads(p.read_text())
    print(f"{p} status={st['status']} {st.get('status_reason', '')}".rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
