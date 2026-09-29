"""Detector objects for the Chart Packet (plan section 4). Deterministic, no lookahead past `end`.

Every function takes UTC M1 bars (columns time, open, high, low, close, tick_volume) already cut at the
packet's end, and returns plain dicts. Ids are assigned by the caller (build_packet.py) in time order.
Thresholds come from config/detectors.json.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TF_RULE = {"M1": None, "M5": "5min", "M15": "15min"}


def fmt(t: pd.Timestamp) -> str:
    return t.strftime("%Y-%m-%d %H:%M")


# ------------------------------------------------------------------ bars

def resample(m1: pd.DataFrame, tf: str) -> pd.DataFrame:
    if tf == "M1":
        return m1[["time", "open", "high", "low", "close", "tick_volume"]].reset_index(drop=True)
    g = m1.set_index("time").resample(TF_RULE[tf], label="left", closed="left")
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "tick_volume": g["tick_volume"].sum()}).dropna(subset=["open"])
    return out.reset_index().rename(columns={"index": "time"})


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=1).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    out = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    out = out.where(dn > 0, np.where(up > 0, 100.0, 50.0))  # no down moves: 100 (or 50 if flat)
    return out


def macd_hist(close: pd.Series, fast=12, slow=26, sig=9) -> pd.Series:
    m = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    return m - m.ewm(span=sig, adjust=False).mean()


def tick_delta(df: pd.DataFrame) -> pd.Series:
    """Cumulative signed tick volume: + on up bars, - on down bars (orderflow proxy)."""
    return (np.sign(df["close"] - df["open"]) * df["tick_volume"]).cumsum()


# ------------------------------------------------------------------ structure

def swings(df: pd.DataFrame, n: int = 3) -> list[dict]:
    """Fractal swings: strictly above (below) the n bars on each side. Confirmed n bars later."""
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    out = []
    for i in range(n, len(df) - n):
        if h[i] > h[i - n:i].max() and h[i] > h[i + 1:i + n + 1].max():
            out.append({"type": "high", "i": i, "price": float(h[i]), "time": df["time"].iat[i], "confirm_i": i + n})
        if l[i] < l[i - n:i].min() and l[i] < l[i + 1:i + n + 1].min():
            out.append({"type": "low", "i": i, "price": float(l[i]), "time": df["time"].iat[i], "confirm_i": i + n})
    return out


def structure(df: pd.DataFrame, sw: list[dict]) -> list[dict]:
    """BOS / CHoCH: a close beyond the latest confirmed, unbroken swing. With the trend = BOS, against = CHoCH."""
    c = df["close"].to_numpy()
    by_confirm: dict[int, list[dict]] = {}
    for s in sw:
        by_confirm.setdefault(s["confirm_i"], []).append(s)
    last_hi = last_lo = None
    trend = None
    out = []
    for i in range(len(df)):
        for s in by_confirm.get(i, []):
            if s["type"] == "high":
                last_hi = s
            else:
                last_lo = s
        if last_hi is not None and c[i] > last_hi["price"] and i > last_hi["i"]:
            out.append({"event": ("CHoCH_up" if trend == "down" else "BOS_up"), "i": i, "time": df["time"].iat[i],
                        "price": last_hi["price"], "broke": last_hi})
            trend, last_hi = "up", None
        elif last_lo is not None and c[i] < last_lo["price"] and i > last_lo["i"]:
            out.append({"event": ("CHoCH_down" if trend == "up" else "BOS_down"), "i": i, "time": df["time"].iat[i],
                        "price": last_lo["price"], "broke": last_lo})
            trend, last_lo = "down", None
    return out


def displacement(df: pd.DataFrame, a: pd.Series, min_range_atr: float, min_body: float) -> list[dict]:
    rng = df["high"] - df["low"]
    body = (df["close"] - df["open"]).abs()
    prev_atr = a.shift(1).bfill()
    out = []
    for i in np.flatnonzero(((rng >= min_range_atr * prev_atr) & (body >= min_body * rng) & (rng > 0)).to_numpy()):
        out.append({"i": int(i), "time": df["time"].iat[i], "dir": "up" if df["close"].iat[i] > df["open"].iat[i] else "down",
                    "range_atr": round(float(rng.iat[i] / prev_atr.iat[i]), 2)})
    return out


# ------------------------------------------------------------------ imbalance

def fvgs(df: pd.DataFrame, a: pd.Series, min_size_atr: float) -> list[dict]:
    """3-bar gaps. `time` = the middle (displacement) bar. filled_pct / inverse tracked to the last bar."""
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    out = []
    for i in range(2, len(df)):
        thr = min_size_atr * a.iat[i - 1]
        for d, top, bot in (("bull", l[i], h[i - 2]), ("bear", l[i - 2], h[i])):
            if top - bot < max(thr, 1e-12):
                continue
            after = slice(i + 1, len(df))
            if d == "bull":
                deepest = l[after].min() if i + 1 < len(df) else top
                filled = (top - deepest) / (top - bot)
                inverse = bool((c[after] < bot).any())
            else:
                deepest = h[after].max() if i + 1 < len(df) else bot
                filled = (deepest - bot) / (top - bot)
                inverse = bool((c[after] > top).any())
            out.append({"dir": d, "i": i - 1, "time": df["time"].iat[i - 1], "top": float(top), "bottom": float(bot),
                        "mid": float((top + bot) / 2), "filled_pct": int(round(100 * min(max(filled, 0.0), 1.0))),
                        "inverse": inverse})
    return out


def order_blocks(df: pd.DataFrame, disp: list[dict], st: list[dict], lookback: int, within: int) -> list[dict]:
    """Last opposite-colour bar before a displacement that breaks a swing (a structure event within `within` bars)."""
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    ev_idx = {(e["i"], e["event"].endswith("up")) for e in st}
    out, seen = [], set()
    for d in disp:
        up = d["dir"] == "up"
        if not any((j, up) in ev_idx for j in range(d["i"], d["i"] + within + 1)):
            continue
        for k in range(d["i"] - 1, max(d["i"] - lookback, 0) - 1, -1):
            if (c[k] < o[k]) if up else (c[k] > o[k]):
                break
        else:
            continue
        if k in seen:
            continue
        seen.add(k)
        top, bot = float(h[k]), float(l[k])
        after = slice(d["i"] + 1, len(df))
        if up:
            deepest = l[after].min() if d["i"] + 1 < len(df) else top
            mitig = (top - deepest) / (top - bot) if top > bot else 0
            broke = np.flatnonzero(c[after] < bot)
        else:
            deepest = h[after].max() if d["i"] + 1 < len(df) else bot
            mitig = (deepest - bot) / (top - bot) if top > bot else 0
            broke = np.flatnonzero(c[after] > top)
        breaker = False
        if len(broke):
            b0 = d["i"] + 1 + broke[0]
            later = slice(b0 + 1, len(df))
            breaker = bool((h[later] >= bot).any()) if up else bool((l[later] <= top).any())
        out.append({"dir": "bull" if up else "bear", "i": k, "time": df["time"].iat[k], "top": top, "bottom": bot,
                    "mitigated_pct": int(round(100 * min(max(mitig, 0.0), 1.0))), "breaker": breaker, "disp_i": d["i"]})
    return out


# ------------------------------------------------------------------ liquidity

def equal_pools(sw: list[dict], tol: float) -> list[dict]:
    """Clusters of >= 2 swing highs (lows) within `tol` of each other."""
    out = []
    for typ in ("high", "low"):
        pts = sorted([s for s in sw if s["type"] == typ], key=lambda s: s["price"])
        cluster: list[dict] = []
        for s in pts + [None]:
            if s is not None and cluster and s["price"] - cluster[0]["price"] <= tol:
                cluster.append(s)
                continue
            if len(cluster) >= 2:
                price = max(p["price"] for p in cluster) if typ == "high" else min(p["price"] for p in cluster)
                out.append({"type": "equal_highs" if typ == "high" else "equal_lows", "price": price,
                            "touches": len(cluster), "formed": max(p["time"] for p in cluster), "members": cluster})
            cluster = [s] if s is not None else []
    return out


def sweep_state(m1: pd.DataFrame, pool: dict, side: str, since: pd.Timestamp, reclaim_bars: int) -> dict:
    """First M1 bar after `since` trading through the pool; reclaimed = a close back inside within N bars."""
    after = m1[m1["time"] > since]
    hit = after[after["high"] > pool["price"]] if side == "high" else after[after["low"] < pool["price"]]
    if hit.empty:
        return {"swept": False, "swept_time": None, "reclaimed": None}
    j = hit.index[0]
    nxt = m1.loc[j:j + reclaim_bars]
    back = (nxt["close"] < pool["price"]).any() if side == "high" else (nxt["close"] > pool["price"]).any()
    return {"swept": True, "swept_time": m1.at[j, "time"], "reclaimed": bool(back)}


# ------------------------------------------------------------------ retracement / divergence

def retracements(df: pd.DataFrame, sw: list[dict], a: pd.Series, levels: list[float], min_leg_atr: float, max_legs: int) -> list[dict]:
    zz = zigzag_from_swings(sw)
    out = []
    for p, q in zip(zz, zz[1:]):
        leg = q["price"] - p["price"]
        if abs(leg) < min_leg_atr * a.iat[q["i"]]:
            continue
        lv = {str(x): float(q["price"] - x * leg) for x in levels}
        after = df.iloc[q["i"] + 1:]
        touched = [k for k, v in lv.items() if (after["low"] <= v).any()] if leg > 0 else \
                  [k for k, v in lv.items() if (after["high"] >= v).any()]
        out.append({"leg": (p, q), "dir": "up" if leg > 0 else "down", "levels": lv, "touched": touched, "time": q["time"]})
    return out[-max_legs:]


def zigzag_from_swings(sw: list[dict]) -> list[dict]:
    """Alternate highs and lows, keeping the more extreme of consecutive same-type swings."""
    zz: list[dict] = []
    for s in sorted(sw, key=lambda s: s["i"]):
        if zz and zz[-1]["type"] == s["type"]:
            better = s["price"] > zz[-1]["price"] if s["type"] == "high" else s["price"] < zz[-1]["price"]
            if better:
                zz[-1] = s
        else:
            zz.append(s)
    return zz


def divergences(df: pd.DataFrame, sw: list[dict], min_apart: int, max_apart: int, rsi_n: int, macd_p) -> list[dict]:
    oscs = {"RSI14": rsi(df["close"], rsi_n), "MACD": macd_hist(df["close"], *macd_p), "TICKDELTA": tick_delta(df)}
    out = []
    for typ in ("high", "low"):
        pts = [s for s in sw if s["type"] == typ]
        for a_, b_ in zip(pts, pts[1:]):
            if not (min_apart <= b_["i"] - a_["i"] <= max_apart):
                continue
            for name, o in oscs.items():
                oa, ob = float(o.iat[a_["i"]]), float(o.iat[b_["i"]])
                kind = None
                if typ == "high":
                    if b_["price"] > a_["price"] and ob < oa:
                        kind = "regular_bearish"
                    elif b_["price"] < a_["price"] and ob > oa:
                        kind = "hidden_bearish"
                else:
                    if b_["price"] < a_["price"] and ob > oa:
                        kind = "regular_bullish"
                    elif b_["price"] > a_["price"] and ob < oa:
                        kind = "hidden_bullish"
                if kind:
                    out.append({"osc": name, "kind": kind, "a": a_, "b": b_, "osc_a": round(oa, 4), "osc_b": round(ob, 4),
                                "time": b_["time"], "confirm_i": b_["confirm_i"]})
    return out


# ------------------------------------------------------------------ big moves

def zigzag_m1(m1: pd.DataFrame, reversal: float) -> list[dict]:
    """Price zigzag on M1 highs/lows with an absolute reversal threshold."""
    h, l, t = m1["high"].to_numpy(), m1["low"].to_numpy(), m1["time"].to_numpy()
    if len(m1) == 0:
        return []
    pts = [{"type": "low", "i": 0, "price": float(l[0])}]
    direction, ext_i, ext_p = None, 0, None
    hi_i, lo_i = 0, 0
    for i in range(1, len(m1)):
        if direction is None:
            if h[i] > h[hi_i]:
                hi_i = i
            if l[i] < l[lo_i]:
                lo_i = i
            if h[hi_i] - l[lo_i] >= reversal:
                if hi_i > lo_i:
                    pts = [{"type": "low", "i": lo_i, "price": float(l[lo_i])}]
                    direction, ext_i, ext_p = "up", hi_i, float(h[hi_i])
                else:
                    pts = [{"type": "high", "i": hi_i, "price": float(h[hi_i])}]
                    direction, ext_i, ext_p = "down", lo_i, float(l[lo_i])
            continue
        if direction == "up":
            if h[i] > ext_p:
                ext_i, ext_p = i, float(h[i])
            elif ext_p - l[i] >= reversal:
                pts.append({"type": "high", "i": ext_i, "price": ext_p})
                direction, ext_i, ext_p = "down", i, float(l[i])
        else:
            if l[i] < ext_p:
                ext_i, ext_p = i, float(l[i])
            elif h[i] - ext_p >= reversal:
                pts.append({"type": "low", "i": ext_i, "price": ext_p})
                direction, ext_i, ext_p = "up", i, float(h[i])
    if direction is not None:
        pts.append({"type": "high" if direction == "up" else "low", "i": ext_i, "price": ext_p})
    for p in pts:
        p["time"] = pd.Timestamp(t[p["i"]]).tz_localize("UTC") if pd.Timestamp(t[p["i"]]).tzinfo is None else pd.Timestamp(t[p["i"]])
    return pts


def big_moves(m1: pd.DataFrame, atr_m15: float, cfg: dict) -> list[dict]:
    zz = zigzag_m1(m1, cfg["zigzag_reversal_atr_m15"] * atr_m15)
    out = []
    for p, q in zip(zz, zz[1:]):
        dur = (q["time"] - p["time"]).total_seconds() / 60
        if abs(q["price"] - p["price"]) >= cfg["min_move_atr_m15"] * atr_m15 and 0 < dur <= cfg["max_minutes"]:
            out.append({"start": p["time"], "end": q["time"], "start_price": p["price"], "end_price": q["price"],
                        "dir": "up" if q["price"] > p["price"] else "down"})
    return out


# ------------------------------------------------------------------ cross-pair

def lead_lag(e: pd.Series, g: pd.Series, max_lag: int) -> int:
    """Lag L maximising corr(e[t], g[t+L]); L < 0 means GBPUSD moved first."""
    best, best_c = 0, -2.0
    for L in range(-max_lag, max_lag + 1):
        c = e.corr(g.shift(-L))
        if pd.notna(c) and c > best_c + 1e-9:
            best, best_c = L, c
    return best
