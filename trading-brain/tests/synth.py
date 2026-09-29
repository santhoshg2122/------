"""Synthetic MT5 exports with a known broker clock, for Stage 0 tests."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

NY = "America/New_York"


def ny_minutes(first_sunday: str, weeks: int) -> pd.DatetimeIndex:
    """Every M1 bar open from Sun 17:00 to Fri 16:59 NY for `weeks` weeks (DST-correct)."""
    parts = []
    sun = pd.Timestamp(first_sunday)
    for w in range(weeks):
        s = pd.Timestamp(sun + pd.Timedelta(days=7 * w) + pd.Timedelta(hours=17)).tz_localize(NY)
        e = pd.Timestamp(sun + pd.Timedelta(days=7 * w + 5) + pd.Timedelta(hours=17)).tz_localize(NY)
        parts.append(pd.date_range(s, e, freq="1min", inclusive="left"))
    return parts[0].append(parts[1:]) if len(parts) > 1 else parts[0]


def bars_for(idx: pd.DatetimeIndex, seed: int = 7, start: float = 1.10) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 0.00008, len(idx))
    close = start + np.cumsum(steps)
    open_ = np.r_[start, close[:-1]]
    wick = np.abs(rng.normal(0, 0.00005, (2, len(idx))))
    return pd.DataFrame({"ts": idx, "open": open_, "high": np.maximum(open_, close) + wick[0],
                         "low": np.minimum(open_, close) - wick[1], "close": close,
                         "tick_volume": rng.integers(5, 200, len(idx))})


def server_clock(ts_ny: pd.Series, zone: str) -> pd.Series:
    if zone == "NY+7":
        return ts_ny.dt.tz_localize(None) + pd.Timedelta(hours=7)
    if zone == "UTC":
        return ts_ny.dt.tz_convert("UTC").dt.tz_localize(None)
    return ts_ny.dt.tz_convert(zone).dt.tz_localize(None)


def write_mt5(df: pd.DataFrame, path: Path, zone: str, layout: str = "tab") -> Path:
    t = server_clock(df["ts"], zone)
    if layout == "tab":  # MT5 "Export bars" dialog
        out = pd.DataFrame({"<DATE>": t.dt.strftime("%Y.%m.%d"), "<TIME>": t.dt.strftime("%H:%M:%S"),
                            "<OPEN>": df["open"].round(5), "<HIGH>": df["high"].round(5), "<LOW>": df["low"].round(5),
                            "<CLOSE>": df["close"].round(5), "<TICKVOL>": df["tick_volume"], "<VOL>": 0, "<SPREAD>": 1})
        out.to_csv(path, sep="\t", index=False)
    else:  # script export: "2021.03.01 00:00,o,h,l,c,v", no header
        out = pd.DataFrame({"t": t.dt.strftime("%Y.%m.%d %H:%M"), "o": df["open"].round(5), "h": df["high"].round(5),
                            "l": df["low"].round(5), "c": df["close"].round(5), "v": df["tick_volume"]})
        out.to_csv(path, index=False, header=False)
    return path
