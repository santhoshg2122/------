"""MT5 ingest (kept from Stage 0): broker-clock detection, DST conversion, cleaning, UTC output."""
from __future__ import annotations

import pandas as pd
import pytest

from scripts.lib import ingest
from scripts.lib.timeutil import trading_date
from tests.synth import bars_for, ny_minutes, write_mt5

# Spring 2021: US DST starts 14 Mar, EU DST 28 Mar -> two "mismatch" weeks that separate the candidate clocks.
SPRING = ("2021-02-28", 7)


def _ny_bars():
    return bars_for(ny_minutes(*SPRING))


@pytest.mark.parametrize("zone", ["NY+7", "UTC", "Europe/Athens", "America/New_York"])
def test_clock_detected_and_weekly_open_at_17_ny(tmp_path, zone):
    df = _ny_bars()
    f = write_mt5(df, tmp_path / "EURUSD_M1.csv", zone)
    res = ingest.ingest_symbol("EURUSD", [f])
    assert res.tz_detected == zone
    assert res.tz_scores[zone] == 1.0
    assert sorted(res.tz_scores.values())[-2] < 0.9, "the runner-up must not also verify"
    ts = res.bars["ts"]
    opens = ts[ts.diff() > pd.Timedelta(hours=24)]
    assert (opens.dt.dayofweek == 6).all() and (opens.dt.strftime("%H:%M") == "17:00").all()
    assert ts.reset_index(drop=True).equals(df["ts"].reset_index(drop=True))


def test_load_utc_round_trip(tmp_path):
    df = _ny_bars()
    f = write_mt5(df, tmp_path / "GBPUSD_M1.csv", "NY+7")
    out, meta = ingest.load_utc(f, "GBPUSD")
    assert meta["clock"] == "NY+7"
    assert str(out["time"].dt.tz) == "UTC"
    assert out["time"].reset_index(drop=True).equals(df["ts"].dt.tz_convert("UTC").reset_index(drop=True))


def test_iso_z_file_needs_no_weekend(tmp_path):
    """The exporter's 3-day UTC files may contain no weekend; the zone in the timestamps is enough."""
    idx = pd.date_range("2026-09-29 07:00", periods=120, freq="1min", tz="UTC")
    b = bars_for(idx)
    p = tmp_path / "EURUSD_M1.csv"
    pd.DataFrame({"time": b["ts"].dt.strftime("%Y-%m-%dT%H:%M:%SZ"), "open": b["open"], "high": b["high"],
                  "low": b["low"], "close": b["close"], "tick_volume": b["tick_volume"]}).to_csv(p, index=False)
    out, meta = ingest.load_utc(p, "EURUSD")
    assert len(out) == 120 and out["time"].iat[0] == pd.Timestamp("2026-09-29 07:00", tz="UTC")


def test_script_layout_without_header(tmp_path):
    df = _ny_bars()
    f = write_mt5(df, tmp_path / "eurusd_export.csv", "NY+7", layout="script")
    res = ingest.ingest_symbol("EURUSD", [f])
    assert res.tz_detected == "NY+7" and res.timeframe_min == 1 and len(res.bars) == len(df)


def test_trading_date_boundary():
    ts = pd.Series(pd.to_datetime(["2021-03-14 16:59", "2021-03-14 17:00", "2021-03-15 16:59", "2021-03-15 17:00"])
                   .tz_localize("America/New_York"))
    assert [str(d) for d in trading_date(ts)] == ["2021-03-14", "2021-03-15", "2021-03-15", "2021-03-16"]


def test_bad_tick_and_duplicate(tmp_path):
    df = _ny_bars()
    t = df["ts"].dt.tz_localize(None)
    i = int(t[t == "2021-03-18 08:15"].index[0])
    df.loc[i, "high"] = df.loc[i - 1, "close"] + 0.0030
    j = int(t[t == "2021-03-19 09:00"].index[0])
    dup = df.loc[[j]].assign(close=df.loc[j, "close"] + 0.0001)
    df = pd.concat([df, dup]).sort_values("ts", kind="stable").reset_index(drop=True)
    f = write_mt5(df, tmp_path / "EURUSD.csv", "NY+7")
    res = ingest.ingest_symbol("EURUSD", [f])
    bt = res.bars[res.bars["bad_tick"]]
    assert bt["ts"].dt.strftime("%Y-%m-%d %H:%M").tolist() == ["2021-03-18 08:15"]
    assert bt["high"].iat[0] < bt["raw_high"].iat[0] - 0.002
    assert res.counts["duplicate_rows_dropped"] == 1
    assert set(res.events["type"]) >= {"bad_tick", "duplicate_conflicting"}
