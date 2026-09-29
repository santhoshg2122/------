"""One test per detector on hand-built bars with a known answer."""
from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.lib import detectors as D
from scripts import build_packet as B

P = 0.0001


def bars(ohlc, start="2026-09-29 07:00", vol=100):
    t = pd.date_range(start, periods=len(ohlc), freq="1min", tz="UTC")
    o, h, l, c = zip(*ohlc)
    return pd.DataFrame({"time": t, "open": o, "high": h, "low": l, "close": c, "tick_volume": vol})


def path(closes, wick=0.2 * P):
    """Near-doji bars on a close path (open = close), so each bar's extreme is its own close +/- wick."""
    return bars([(c, c + wick, c - wick, c) for c in closes])


def test_swings_fractal():
    df = path([1.1 + x * P for x in [0, 1, 2, 3, 6, 3, 2, 1, 0, 1, 2]])
    sw = D.swings(df, 3)
    assert [(s["type"], s["i"]) for s in sw] == [("high", 4)]
    assert sw[0]["confirm_i"] == 7


def test_structure_bos_then_choch():
    # up leg with swing high at 6, pullback swing low at 10, break of the high (BOS_up), then break of the low (CHoCH_down)
    xs = [0, 2, 4, 6, 8, 10, 12, 10, 8, 6, 4, 6, 8, 10, 11, 14, 16, 12, 8, 4, 2, 0, -2]
    df = path([1.1 + x * P for x in xs])
    st = D.structure(df, D.swings(df, 3))
    assert [e["event"] for e in st] == ["BOS_up", "CHoCH_down"]
    assert st[1]["price"] == min(df["low"].iloc[8:13])


def test_fvg_bull_fill_and_inverse():
    rows = [(1.1000, 1.1002, 1.0999, 1.1001),   # bar 1: high 1.1002
            (1.1001, 1.1020, 1.1001, 1.1019),   # displacement
            (1.1019, 1.1025, 1.1010, 1.1024),   # bar 3: low 1.1010 > 1.1002 -> bull gap 1.1002-1.1010
            (1.1024, 1.1026, 1.1006, 1.1008),   # fills half
            (1.1008, 1.1009, 1.0995, 1.0996)]   # closes below the gap -> inverse
    df = bars(rows)
    a = pd.Series([2 * P] * len(df))
    f = [x for x in D.fvgs(df, a, 0.4) if x["dir"] == "bull"]
    assert len(f) == 1
    g = f[0]
    assert (round(g["bottom"], 5), round(g["top"], 5), g["i"]) == (1.1002, 1.1010, 1)
    assert g["filled_pct"] == 100 and g["inverse"] is True


def test_displacement_and_order_block():
    xs = [0, 1, 2, 3, 2, 1, 0, 1, 0]                 # swing high at 3 (i=3), then a small down bar (i=8)
    closes = [1.1 + x * P for x in xs]
    df = path(closes)
    df.loc[8, ["open", "high"]] = [closes[8] + 0.6 * P, closes[8] + 0.8 * P]  # a real down bar: the order block
    big = (closes[-1], closes[-1] + 12 * P, closes[-1] - 0.1 * P, closes[-1] + 11.5 * P)  # displacement up through the high
    df = pd.concat([df, bars([big], start=str(df["time"].iat[-1].tz_localize(None) + pd.Timedelta(minutes=1)))], ignore_index=True)
    a = D.atr(df, 14)
    disp = D.displacement(df, a, 2.0, 0.7)
    assert [d["i"] for d in disp] == [9] and disp[0]["dir"] == "up"
    st = D.structure(df, D.swings(df, 3))
    assert st and st[-1]["event"] == "BOS_up" and st[-1]["i"] == 9
    obs = D.order_blocks(df, disp, st, 10, 3)
    assert len(obs) == 1 and obs[0]["dir"] == "bull" and obs[0]["i"] == 8  # the last down bar before the displacement


def test_equal_pools_and_sweep_reclaim():
    sw = [{"type": "high", "price": 1.10500, "time": pd.Timestamp("2026-09-29 07:05", tz="UTC"), "i": 5},
          {"type": "high", "price": 1.10510, "time": pd.Timestamp("2026-09-29 07:40", tz="UTC"), "i": 40},
          {"type": "high", "price": 1.10600, "time": pd.Timestamp("2026-09-29 07:50", tz="UTC"), "i": 50}]
    pools = D.equal_pools(sw, 1.5 * P)
    assert len(pools) == 1 and pools[0]["touches"] == 2 and pools[0]["price"] == 1.10510
    m1 = bars([(1.1049, 1.1050, 1.1048, 1.1049)] * 45 + [(1.1049, 1.1053, 1.1048, 1.1049), (1.1049, 1.1050, 1.1047, 1.1048)])
    s = D.sweep_state(m1, pools[0], "high", pools[0]["formed"], 3)
    assert s["swept"] and s["reclaimed"] is True and s["swept_time"] == m1["time"].iat[45]


def test_regular_bearish_divergence_rsi():
    up = list(np.linspace(0, 30, 16))                        # strong rise -> high RSI at the first peak
    xs = up + [26, 22, 18, 15, 13, 12, 14, 17, 20, 23, 26, 29, 31, 32, 28, 24, 20, 16]  # slower rise to a higher high
    df = path([1.1 + x * P for x in xs])
    sw = [s for s in D.swings(df, 3) if s["type"] == "high"]
    assert len(sw) == 2 and sw[1]["price"] > sw[0]["price"]
    dv = [d for d in D.divergences(df, sw, 5, 60, 14, (12, 26, 9)) if d["osc"] == "RSI14"]
    assert dv and dv[0]["kind"] == "regular_bearish" and dv[0]["osc_b"] < dv[0]["osc_a"]


def test_big_move_zigzag():
    xs = [0, 1, 0, 1, 0] + list(range(0, 41, 2)) + [38, 39, 38]   # a 40-pip leg over 20 minutes
    df = path([1.1 + x * P for x in xs])
    mv = D.big_moves(df, 6 * P, {"zigzag_reversal_atr_m15": 1.0, "min_move_atr_m15": 2.5, "max_minutes": 90})
    assert len(mv) == 1 and mv[0]["dir"] == "up"
    assert round((mv[0]["end_price"] - mv[0]["start_price"]) / P) >= 40


def test_lead_lag_gbp_first():
    rng = np.random.default_rng(1)
    g = pd.Series(rng.normal(0, 1, 500))
    e = g.shift(1).fillna(0) + rng.normal(0, 0.1, 500)       # EURUSD follows GBPUSD by one minute
    assert D.lead_lag(e, g, 5) == -1


def test_smt_reading():
    t = pd.Timestamp("2026-09-29 09:34", tz="UTC")
    lq = lambda i, typ, swept, ts: {"id": i, "type": typ, "swept": swept, "_swept_t": ts, "_formed": t - pd.Timedelta(hours=2)}  # noqa: E731
    packs = {"EURUSD": {"liquidity": [lq("EU-LQ-4", "equal_highs", False, None)]},
             "GBPUSD": {"liquidity": [lq("GB-LQ-2", "equal_highs", True, t)]}}
    idx = pd.date_range("2026-09-29 07:00", periods=300, freq="1min", tz="UTC")
    rng = np.random.default_rng(2)
    bars_ = {p: pd.DataFrame({"time": idx, "close": 1.1 + np.cumsum(rng.normal(0, P, 300))}) for p in packs}
    import json
    dcfg = json.loads((B.ROOT / "config" / "detectors.json").read_text())
    x = B.cross_pair(bars_, idx[0], idx[-1] + pd.Timedelta(minutes=1), packs, "2026-09-29", dcfg)
    assert x["smt"] == [{"id": "X-SMT-1", "time": "09:34", "swept": "GB-LQ-2", "failed": "EU-LQ-4",
                         "reading": "GBPUSD took the equal highs, EURUSD did not"}]
