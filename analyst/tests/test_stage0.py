"""Stage 0: broker-clock detection, DST conversion, anomaly detection, split enforcement."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from engine import ingest, integrity, splits
from engine.timeutil import load_killzones, trading_date
from tests.synth import bars_for, ny_minutes, write_mt5

# Spring 2021: US DST starts 14 Mar, EU DST 28 Mar -> two "mismatch" weeks that separate the candidate clocks.
SPRING = ("2021-02-28", 7)


def _ny_bars():
    idx = ny_minutes(*SPRING)
    return bars_for(idx)


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
    assert len(opens) == SPRING[1] - 1
    assert (opens.dt.dayofweek == 6).all() and (opens.dt.strftime("%H:%M") == "17:00").all()
    # exactly the original NY timestamps come back, across the DST change
    assert ts.reset_index(drop=True).equals(df["ts"].reset_index(drop=True))


def test_script_layout_without_header(tmp_path):
    df = _ny_bars()
    f = write_mt5(df, tmp_path / "eurusd_export.csv", "NY+7", layout="script")
    res = ingest.ingest_symbol("EURUSD", [f])
    assert res.tz_detected == "NY+7" and res.timeframe_min == 1
    assert len(res.bars) == len(df)


def test_trading_date_boundary():
    ts = pd.Series(pd.to_datetime(["2021-03-14 16:59", "2021-03-14 17:00", "2021-03-15 16:59", "2021-03-15 17:00"])
                   .tz_localize("America/New_York"))
    assert [str(d) for d in trading_date(ts)] == ["2021-03-14", "2021-03-15", "2021-03-15", "2021-03-16"]


def _with_anomalies():
    df = _ny_bars()
    t = df["ts"].dt.tz_localize(None)
    # 20-minute hole inside the London killzone on Tue 16 Mar (first week of US DST)
    hole = (t >= "2021-03-16 03:00") & (t < "2021-03-16 03:20")
    # rollover break 16:58-17:05 on Wed 17 Mar
    roll = (t >= "2021-03-17 16:58") & (t < "2021-03-17 17:06")
    df = df[~hole & ~roll].reset_index(drop=True)
    t = df["ts"].dt.tz_localize(None)
    # 30-pip one-bar wick spike at Thu 18 Mar 08:15
    i = int(t[t == "2021-03-18 08:15"].index[0])
    df.loc[i, "high"] = df.loc[i - 1, "close"] + 0.0030
    # conflicting duplicate at Fri 19 Mar 09:00
    j = int(t[t == "2021-03-19 09:00"].index[0])
    dup = df.loc[[j]].assign(close=df.loc[j, "close"] + 0.0001)
    df = pd.concat([df, dup]).sort_values("ts", kind="stable").reset_index(drop=True)
    return df


def test_anomalies_found_and_classified(tmp_path):
    df = _with_anomalies()
    f = write_mt5(df, tmp_path / "EURUSD.csv", "NY+7")
    res = ingest.ingest_symbol("EURUSD", [f])
    cov, an = integrity.run_checks(res, load_killzones())
    kinds = an.groupby("type").size().to_dict()
    kzg = an[an["type"] == "gap_killzone"]
    assert len(kzg) == 1 and int(kzg["missing_min"].iat[0]) == 20 and int(kzg["kz_missing_min"].iat[0]) == 20
    assert kinds.get("gap_rollover") == 1
    assert kinds.get("duplicate_conflicting") == 1
    assert kinds.get("bad_tick") == 1
    bt = res.bars[res.bars["bad_tick"]]
    assert bt["ts"].dt.strftime("%Y-%m-%d %H:%M").tolist() == ["2021-03-18 08:15"]
    assert bt["high"].iat[0] < bt["raw_high"].iat[0] - 0.002, "spike clipped, raw extreme retained"
    assert not an["type"].eq("gap_session").any()
    assert res.counts["duplicate_rows_dropped"] == 1

    status, reasons = integrity.evaluate(res, cov, an[an["type"].str.startswith("gap_")], None)
    md = tmp_path / "r.md"
    overall = integrity.write_report({"EURUSD": res}, {"EURUSD": cov}, {"EURUSD": an},
                                     {"EURUSD": (status, reasons)}, None, md, tmp_path / "r.csv")
    text = md.read_text()
    assert "gap_killzone" in text and "NY+7" in text and overall in ("PASS", "WARN", "FAIL")


def test_unverifiable_clock_fails(tmp_path):
    df = _ny_bars()
    # shift every week by a random number of hours -> no candidate can verify
    t = df["ts"].dt.tz_localize(None) + pd.Timedelta(hours=7)
    wk = (t.diff() > pd.Timedelta(hours=24)).cumsum()
    t = t + pd.to_timedelta((wk * 5) % 11, unit="h")
    out = pd.DataFrame({"<DATE>": t.dt.strftime("%Y.%m.%d"), "<TIME>": t.dt.strftime("%H:%M:%S"),
                        "<OPEN>": df["open"], "<HIGH>": df["high"], "<LOW>": df["low"], "<CLOSE>": df["close"],
                        "<TICKVOL>": df["tick_volume"]})
    f = tmp_path / "EURUSD.csv"
    out.to_csv(f, sep="\t", index=False)
    res = ingest.ingest_symbol("EURUSD", [f])
    cov, an = integrity.run_checks(res, load_killzones())
    status, reasons = integrity.evaluate(res, cov, an, None)
    assert status == "FAIL" and "broker clock not verified" in reasons[0]


# ---------------------------------------------------------------- splits

def test_compute_splits_ten_years():
    sp = splits.compute_splits(list(range(2015, 2026)))  # 11 complete -> last 10
    assert sp["train"]["years"] == list(range(2016, 2023))
    assert sp["validation"]["years"] == [2023, 2024]
    assert sp["holdout"]["years"] == [2025]
    assert sp["standard_10y"]


def test_compute_splits_short_history_flagged():
    sp = splits.compute_splits([2020, 2021, 2022, 2023, 2024])
    assert sp["holdout"]["years"] == [2024] and sp["validation"]["years"] == [2023]
    assert not sp["standard_10y"] and "confirm at H1" in sp["note"]


def test_compute_splits_rejects_holes():
    with pytest.raises(ValueError):
        splits.compute_splits([2016, 2017, 2019, 2020])


@pytest.fixture
def sealed(tmp_path):
    sp = splits.compute_splits(list(range(2016, 2026)))
    p = tmp_path / "splits.json"
    splits.write_splits(sp, path=p)
    return json.loads(p.read_text()), tmp_path / "HOLDOUT_UNLOCKED", p


def test_holdout_sealed_without_unlock(sealed):
    sp, unlock, _ = sealed
    with pytest.raises(splits.HoldoutLocked):
        splits.check_access("holdout", None, None, splits=sp, unlock_path=unlock)
    unlock.write_text("approved at H3")
    s, e = splits.check_access("holdout", "2025-01-01", None, splits=sp, unlock_path=unlock)
    assert str(e) == "2025-12-31"


def test_train_cannot_read_validation(sealed):
    sp, unlock, _ = sealed
    with pytest.raises(splits.SplitViolation):
        splits.check_access("train", "2022-06-01", "2023-01-05", splits=sp, unlock_path=unlock)
    # validation may look back into train (it is the past)
    splits.check_access("validation", "2022-06-01", "2023-01-05", splits=sp, unlock_path=unlock)
    with pytest.raises(splits.SplitViolation):
        splits.check_access("nonsense", None, None, splits=sp, unlock_path=unlock)


def test_split_boundaries_frozen(sealed):
    _, _, p = sealed
    moved = splits.compute_splits(list(range(2015, 2025)))
    with pytest.raises(splits.SplitViolation):
        splits.write_splits(moved, path=p)
    splits.write_splits(moved, path=p, force=True)


def test_load_bars_respects_split(tmp_path, sealed):
    sp, unlock, _ = sealed
    bars_dir = tmp_path / "bars"
    bars_dir.mkdir()
    for y in (2022, 2023, 2025):
        idx = pd.date_range(f"{y}-03-02 02:00", periods=5, freq="1min", tz="America/New_York")
        b = bars_for(idx)
        b["trading_date"] = trading_date(b["ts"])
        b.to_parquet(bars_dir / f"EURUSD_{y}.parquet")
    got = splits.load_bars("EURUSD", "train", start="2022-01-01", bars_dir=bars_dir, splits=sp, unlock_path=unlock)
    assert len(got) == 5 and {d.year for d in got["trading_date"]} == {2022}
    with pytest.raises(splits.HoldoutLocked):
        splits.load_bars("EURUSD", "holdout", bars_dir=bars_dir, splits=sp, unlock_path=unlock)


def test_ambiguous_clock_fails_unless_forced(tmp_path):
    # January only: no DST-mismatch week, so NY+7 and Europe/Athens are indistinguishable
    df = bars_for(ny_minutes("2021-01-03", 4))
    f = write_mt5(df, tmp_path / "EURUSD.csv", "NY+7")
    res = ingest.ingest_symbol("EURUSD", [f])
    cov, an = integrity.run_checks(res, load_killzones())
    status, reasons = integrity.evaluate(res, cov, an, None)
    assert status == "FAIL" and any("ambiguous" in r for r in reasons)
    forced = ingest.ingest_symbol("EURUSD", [f], zone="NY+7")
    assert forced.tz_forced
    status, reasons = integrity.evaluate(forced, cov, an, None)
    assert not any("ambiguous" in r for r in reasons)


def test_partial_year_is_not_complete(tmp_path):
    df = _ny_bars()  # Feb-Apr 2021 only
    f = write_mt5(df, tmp_path / "EURUSD.csv", "NY+7")
    res = ingest.ingest_symbol("EURUSD", [f])
    cov, _ = integrity.run_checks(res, load_killzones())
    row = cov[cov["year"] == 2021].iloc[0]
    assert row["kz_coverage"] == 1.0 and not row["full_year"] and not row["complete"]
    assert row["missing_days"] == ""
