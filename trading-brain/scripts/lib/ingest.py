"""MT5 CSV ingest: any MT5 export layout -> clean UTC M1 bars.

Steps: parse the CSV -> take the timezone from the file (ISO 'Z'/offset or epoch) or, for naive broker-clock
files, detect the clock from where the weekly open lands (needs >= 2 weekends) -> drop duplicates ->
repair inconsistent OHLC -> flag and clip bad ticks -> flag zero-range bars. Every change is counted.
Carried over from the tested Stage 0 of the earlier research project.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .timeutil import NY, trading_date

# Broker clocks we can recognise. "NY+7" is the usual MT5 server clock (EET with US DST switch dates),
# i.e. server midnight == 17:00 New York all year round.
TZ_CANDIDATES = ["NY+7", "America/New_York", "UTC", "UTC+1", "UTC+2", "UTC+3",
                 "Europe/London", "Europe/Berlin", "Europe/Athens"]
WEEKLY_OPEN_NY_MIN = 17 * 60          # Sunday 17:00 NY
WEEKLY_OPEN_TOL_MIN = 15              # some feeds print their first bar at 17:05
TZ_PASS_SCORE = 0.90

SYMBOL_PATTERNS = [("DXY", re.compile(r"(DXY|USDX|DX[-_ ]?Y|DOLLAR)", re.I)), ("EURUSD", re.compile(r"EURUSD", re.I)),
                   ("GBPUSD", re.compile(r"GBPUSD", re.I))]


@dataclass
class IngestResult:
    symbol: str
    files: list[str]
    bars: pd.DataFrame
    timeframe_min: int
    tz_detected: str
    tz_scores: dict
    sunday_open_hist: dict
    counts: dict = field(default_factory=dict)
    events: pd.DataFrame = field(default_factory=pd.DataFrame)   # per-bar anomalies found while cleaning
    tz_forced: bool = False


# ---------------------------------------------------------------- parsing

def _sniff_delim(line: str) -> str:
    return max(["\t", ",", ";"], key=line.count)


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


_DT_FORMATS = ["%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
               "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M",
               "%Y%m%d %H:%M:%S", "%Y%m%d %H%M%S", "%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M"]


def _parse_datetime(s: pd.Series) -> tuple[pd.Series, bool]:
    """Returns (datetimes, tz_embedded). Naive unless the strings carry an offset."""
    s = s.astype(str).str.strip()
    sample = s.head(500)
    if sample.map(_is_number).all():
        v = s.astype("int64")
        unit = "ms" if v.iloc[0] > 10**11 else "s"
        # Epoch values are UTC by definition.
        return pd.to_datetime(v, unit=unit, utc=True), True
    if sample.str.contains(r"(?:[+-]\d{2}:?\d{2}|Z)$").all():
        return pd.to_datetime(s, utc=True, format="ISO8601"), True
    for fmt in _DT_FORMATS:
        try:
            pd.to_datetime(sample, format=fmt)
        except (ValueError, TypeError):
            continue
        return pd.to_datetime(s, format=fmt), False
    raise ValueError(f"unrecognised timestamp format, e.g. {sample.iloc[0]!r}")


def read_mt5_csv(path: Path) -> tuple[pd.DataFrame, bool]:
    """Read an MT5 export in any of its common layouts. Returns (df[ts, open, high, low, close, tick_volume], tz_embedded)."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        first = fh.readline().strip()
    delim = _sniff_delim(first)
    tokens = [t.strip() for t in first.split(delim)]
    has_header = not (_is_number(tokens[-1]) and _is_number(tokens[-2]))
    raw = pd.read_csv(path, sep=delim, header=0 if has_header else None, dtype=str,
                      encoding="utf-8-sig", skipinitialspace=True)
    if has_header:
        raw.columns = [re.sub(r"[<>\s]", "", str(c)).lower() for c in raw.columns]
    else:
        n = raw.shape[1]
        second_is_time = raw.iloc[:5, 1].astype(str).str.match(r"^\d{1,2}:\d{2}").all()
        if second_is_time:
            names = ["date", "time", "open", "high", "low", "close", "tickvol", "vol", "spread"][:n]
        else:
            names = ["datetime", "open", "high", "low", "close", "tickvol", "vol", "spread"][:n]
        raw.columns = names + [f"x{i}" for i in range(n - len(names))]

    cols = set(raw.columns)
    if {"date", "time"} <= cols:
        ts_str = raw["date"].astype(str) + " " + raw["time"].astype(str)
    else:
        tcol = next((c for c in ["datetime", "timestamp", "time", "date", "gmttime", "localtime"] if c in cols), None)
        if tcol is None:
            raise ValueError(f"{path.name}: no timestamp column in {sorted(cols)}")
        ts_str = raw[tcol]
    ts, embedded = _parse_datetime(ts_str)

    def pick(*names):
        for n in names:
            if n in cols:
                return pd.to_numeric(raw[n], errors="coerce")
        return None

    vol = pick("tickvol", "tick_volume", "tickvolume", "volume", "vol")
    df = pd.DataFrame({
        "ts": ts,
        "open": pick("open", "o"), "high": pick("high", "h"),
        "low": pick("low", "l"), "close": pick("close", "c"),
        "tick_volume": vol if vol is not None else np.nan,
    })
    if df[["open", "high", "low", "close"]].isna().all().any():
        raise ValueError(f"{path.name}: missing OHLC columns in {sorted(cols)}")
    return df, embedded


def symbol_of(path: Path) -> str | None:
    for sym, pat in SYMBOL_PATTERNS:
        if pat.search(path.stem):
            return sym
    return None


# ---------------------------------------------------------------- timezone

def to_ny(naive: pd.Series, zone: str) -> pd.Series:
    """Interpret naive server timestamps as `zone`, return tz-aware America/New_York."""
    if zone == "NY+7":
        return (naive - pd.Timedelta(hours=7)).dt.tz_localize(NY, ambiguous="NaT", nonexistent="NaT")
    if zone.startswith("UTC+"):
        return (naive - pd.Timedelta(hours=int(zone[4:]))).dt.tz_localize("UTC").dt.tz_convert(NY)
    if zone == "UTC":
        return naive.dt.tz_localize("UTC").dt.tz_convert(NY)
    return naive.dt.tz_localize(zone, ambiguous="NaT", nonexistent="NaT").dt.tz_convert(NY)


def weekly_open_index(ts: pd.Series) -> np.ndarray:
    """Positions of the first bar after each weekend (a gap of > 24h)."""
    gaps = ts.diff() > pd.Timedelta(hours=24)
    idx = np.flatnonzero(gaps.to_numpy())
    return idx


def detect_timezone(naive: pd.Series) -> tuple[str, dict, dict]:
    """Score every candidate clock by the share of weekly opens landing on Sunday 17:00 NY (+15 min).

    Weekly closes (Friday last bar in 16:30-17:00 NY) are a second, independent check; the score is the mean.
    The EU/US DST mismatch weeks (March, Oct/Nov) are what separate NY+7 from a fixed or EU-DST clock.
    """
    opens = weekly_open_index(naive)
    if len(opens) < 2:
        raise ValueError("need at least two weekends of data to verify the broker clock")
    open_ts = naive.iloc[opens].reset_index(drop=True)
    close_ts = naive.iloc[opens - 1].reset_index(drop=True)
    scores, hists = {}, {}
    for z in TZ_CANDIDATES:
        o = to_ny(open_ts, z).dropna()
        c = to_ny(close_ts, z).dropna()
        om = o.dt.hour * 60 + o.dt.minute
        ok_open = (o.dt.dayofweek == 6) & (om >= WEEKLY_OPEN_NY_MIN) & (om < WEEKLY_OPEN_NY_MIN + WEEKLY_OPEN_TOL_MIN)
        cm = c.dt.hour * 60 + c.dt.minute
        ok_close = (c.dt.dayofweek == 4) & (cm >= 16 * 60 + 30) & (cm < 17 * 60)
        scores[z] = round(float((ok_open.mean() + ok_close.mean()) / 2), 4)
        hists[z] = (o.dt.day_name().str[:3] + " " + o.dt.strftime("%H:%M")).value_counts().head(6).to_dict()
    best = max(scores, key=scores.get)
    return best, scores, hists[best]


# ---------------------------------------------------------------- cleaning

def flag_bad_ticks(df: pd.DataFrame, mult: float = 8.0, revert_mult: float = 2.0) -> pd.Series:
    """Spikes > `mult` x ATR beyond the previous close that fully revert within one bar.

    ATR = rolling median true range of the preceding 60 bars (median so a spike cannot inflate its own yardstick),
    floored at 0.5 pip. Reverted = the spike bar's close, or the next bar's close, is back within
    `revert_mult` x ATR of the previous close and the next bar does not trade back out to the spike.
    Returns +1 (up spike), -1 (down spike), 0.
    """
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = tr.rolling(60, min_periods=10).median().shift(1).clip(lower=0.00005)
    nc, nh, nl = c.shift(-1), h.shift(-1), l.shift(-1)
    up_exc = h - pc
    dn_exc = pc - l
    up = (up_exc > mult * atr) & (((c - pc).abs() <= revert_mult * atr) | ((nc - pc).abs() <= revert_mult * atr)) \
        & (nh < pc + 0.5 * up_exc)
    dn = (dn_exc > mult * atr) & (((c - pc).abs() <= revert_mult * atr) | ((nc - pc).abs() <= revert_mult * atr)) \
        & (nl > pc - 0.5 * dn_exc)
    return (up.astype(int) - dn.astype(int)).fillna(0).astype(int)


def clean(df: pd.DataFrame, zone: str, symbol: str, embedded_tz: bool) -> tuple[pd.DataFrame, dict, pd.DataFrame, int]:
    counts: dict = {"raw_rows": int(len(df))}
    events = []

    # timestamps -> NY
    if embedded_tz:
        ts_ny = df["ts"].dt.tz_convert(NY)
    else:
        ts_ny = to_ny(df["ts"], zone)
    df = df.assign(ts=ts_ny)
    bad_ts = df["ts"].isna()
    counts["unconvertible_ts_dropped"] = int(bad_ts.sum())
    df = df[~bad_ts]
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df.sort_values("ts", kind="stable").reset_index(drop=True)

    # duplicates: identical rows vs conflicting rows sharing a timestamp (keep the first, report both)
    dup_mask = df["ts"].duplicated(keep=False)
    if dup_mask.any():
        d = df[dup_mask]
        g = d.groupby("ts")[["open", "high", "low", "close"]].nunique().max(axis=1)
        conflicting = g[g > 1].index
        for t in d["ts"].drop_duplicates():
            events.append({"type": "duplicate_conflicting" if t in conflicting else "duplicate_identical",
                           "ts_ny": t, "detail": f"{int((d['ts'] == t).sum())} rows"})
    counts["duplicate_rows_dropped"] = int(df["ts"].duplicated().sum())
    df = df[~df["ts"].duplicated(keep="first")].reset_index(drop=True)

    # off-session bars (Sat, or Sun before 17:00, or Fri at/after 17:00) belong to no trading day
    dow, mod = df["ts"].dt.dayofweek, df["ts"].dt.hour * 60 + df["ts"].dt.minute
    off = (dow == 5) | ((dow == 6) & (mod < 17 * 60)) | ((dow == 4) & (mod >= 17 * 60))
    counts["off_session_bars_dropped"] = int(off.sum())
    for t in df.loc[off, "ts"].dt.floor("h").drop_duplicates():
        events.append({"type": "off_session_bars", "ts_ny": t, "detail": "bars outside Sun 17:00-Fri 17:00 NY dropped"})
    df = df[~off].reset_index(drop=True)

    # timeframe
    tf_min = int(round(df["ts"].diff().dt.total_seconds().div(60).median()))
    counts["timeframe_min"] = tf_min

    # OHLC consistency
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    incons = (h < np.maximum(o, c)) | (l > np.minimum(o, c)) | (h < l) | (df[["open", "high", "low", "close"]] <= 0).any(axis=1)
    counts["ohlc_inconsistent_repaired"] = int(incons.sum())
    for t in df.loc[incons, "ts"]:
        events.append({"type": "ohlc_inconsistent", "ts_ny": t, "detail": "high/low widened to contain open/close"})
    df.loc[incons, "high"] = df.loc[incons, ["open", "high", "low", "close"]].max(axis=1)
    df.loc[incons, "low"] = df.loc[incons, ["open", "high", "low", "close"]].min(axis=1)

    # bad ticks: flag, keep the raw extreme, clip the spike back to the bar body
    spikes = flag_bad_ticks(df)
    df["bad_tick"] = spikes != 0
    df["raw_high"] = np.where(spikes == 1, df["high"], np.nan)
    df["raw_low"] = np.where(spikes == -1, df["low"], np.nan)
    pc = df["close"].shift(1)
    for i in np.flatnonzero(spikes.to_numpy()):
        s = int(spikes.iat[i])
        prev = pc.iat[i]
        if s == 1:
            # a spiked close/open is replaced by the previous close before clipping
            for col in ("open", "close"):
                if df[col].iat[i] - prev > 0.5 * (df["high"].iat[i] - prev):
                    df.at[i, col] = prev
            new_hi = max(df["open"].iat[i], df["close"].iat[i])
            events.append({"type": "bad_tick", "ts_ny": df["ts"].iat[i],
                           "detail": f"up spike high {df['high'].iat[i]:.5f} -> {new_hi:.5f} (prev close {prev:.5f})"})
            df.at[i, "high"] = new_hi
        else:
            for col in ("open", "close"):
                if prev - df[col].iat[i] > 0.5 * (prev - df["low"].iat[i]):
                    df.at[i, col] = prev
            new_lo = min(df["open"].iat[i], df["close"].iat[i])
            events.append({"type": "bad_tick", "ts_ny": df["ts"].iat[i],
                           "detail": f"down spike low {df['low'].iat[i]:.5f} -> {new_lo:.5f} (prev close {prev:.5f})"})
            df.at[i, "low"] = new_lo
        df.at[i, "high"] = max(df["high"].iat[i], df["open"].iat[i], df["close"].iat[i])
        df.at[i, "low"] = min(df["low"].iat[i], df["open"].iat[i], df["close"].iat[i])
    counts["bad_ticks_repaired"] = int(df["bad_tick"].sum())

    # zero-range bars: flagged, kept (common in quiet Asian minutes); runs of >= 5 suggest a stale feed
    df["zero_range"] = df["high"] == df["low"]
    counts["zero_range_bars"] = int(df["zero_range"].sum())
    run_id = (df["zero_range"] != df["zero_range"].shift()).cumsum()
    runs = df[df["zero_range"]].groupby(run_id[df["zero_range"]])["ts"].agg(["first", "count"])
    for _, r in runs[runs["count"] >= 5].iterrows():
        events.append({"type": "zero_range_run", "ts_ny": r["first"], "detail": f"{int(r['count'])} consecutive zero-range bars"})

    df["trading_date"] = trading_date(df["ts"])
    df["symbol"] = symbol
    counts["clean_rows"] = int(len(df))
    ev = pd.DataFrame(events, columns=["type", "ts_ny", "detail"])
    return df, counts, ev, tf_min


# ---------------------------------------------------------------- driver

def ingest_symbol(symbol: str, files: list[Path], zone: str | None = None) -> IngestResult:
    parts, embedded_any = [], []
    for f in sorted(files):
        d, emb = read_mt5_csv(f)
        parts.append(d)
        embedded_any.append(emb)
    if len(set(embedded_any)) > 1:
        raise ValueError(f"{symbol}: files mix embedded-timezone and naive timestamps")
    embedded = embedded_any[0]
    raw = pd.concat(parts, ignore_index=True)

    if embedded:
        tz, scores, hist = "embedded (UTC/offset in file)", {"embedded": 1.0}, {}
    else:
        naive = raw["ts"].sort_values().drop_duplicates().reset_index(drop=True)
        tz, scores, hist = detect_timezone(naive)
        if zone:  # explicit override from the command line, still scored and reported
            tz = zone
    bars, counts, events, tf = clean(raw, tz, symbol, embedded)
    return IngestResult(symbol, [f.name for f in files], bars, tf, tz, scores, hist, counts, events,
                        tz_forced=bool(zone) and not embedded)


def load_utc(path: Path, symbol: str, zone: str | None = None) -> tuple[pd.DataFrame, dict]:
    """One CSV -> UTC bars [time, open, high, low, close, tick_volume, bad_tick, zero_range] + cleaning counts.

    Files with timestamps that carry their zone (the exporter's ISO 'Z') need no detection; naive files use
    `zone` if given, else the detected broker clock.
    """
    res = ingest_symbol(symbol, [Path(path)], zone=zone)
    b = res.bars
    out = pd.DataFrame({"time": b["ts"].dt.tz_convert("UTC"), "open": b["open"], "high": b["high"], "low": b["low"],
                        "close": b["close"], "tick_volume": b["tick_volume"].fillna(0),
                        "bad_tick": b["bad_tick"], "zero_range": b["zero_range"]})
    meta = {"file": str(path), "clock": res.tz_detected, "clock_scores": res.tz_scores, "timeframe_min": res.timeframe_min,
            **res.counts, "events": len(res.events)}
    return out.reset_index(drop=True), meta
