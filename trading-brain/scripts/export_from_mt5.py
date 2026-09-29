#!/usr/bin/env python3
"""Export EURUSD and GBPUSD M1 bars from MetaTrader 5 into the data contract (plan section 3), in UTC.

    python scripts/export_from_mt5.py                       # last 3 days -> data/<PAIR>_M1.csv, 60 days -> data/history/
    python scripts/export_from_mt5.py --broker-tz NY+7      # skip clock detection
    python scripts/export_from_mt5.py --from-csv "C:\\...\\EURUSD_1m_NY.csv" --pair EURUSD   # seed history from a file

MT5 returns bar times on the broker's server clock labelled as if UTC. The clock is taken from --broker-tz, else
from the Trading Journal's exporter config (`broker_tz`), else detected from where the weekly opens land
(scripts/lib/ingest.py, the tested Stage 0 detector) on the 60-day pull. Needs `pip install MetaTrader5` and a
running, logged-in terminal. Your own Trading Journal exporter (mt5_export.py) can replace the MT5 part: it only
has to leave the same two files per pair.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.lib import ingest  # noqa: E402
from scripts.lib.timeutil import ROOT  # noqa: E402

JOURNAL = Path.home() / "Documents" / "Trading Journal"


def journal_broker_tz() -> str | None:
    for name in ("config.json", "mt5_config.json", "settings.json"):
        p = JOURNAL / name
        try:
            tz = json.loads(p.read_text()).get("broker_tz")
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        if tz:
            return str(tz)
    return None


def to_contract(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"time": df["time"].dt.strftime("%Y-%m-%dT%H:%M:%SZ"), "open": df["open"].round(5),
                         "high": df["high"].round(5), "low": df["low"].round(5), "close": df["close"].round(5),
                         "tick_volume": df["tick_volume"].astype("int64")})


def write(pair: str, utc: pd.DataFrame, days: int, history_days: int, out: Path) -> None:
    (out / "history").mkdir(parents=True, exist_ok=True)
    end = utc["time"].max()
    to_contract(utc[utc["time"] > end - pd.Timedelta(days=history_days)]).to_csv(out / "history" / f"{pair}_M1.csv", index=False)
    to_contract(utc[utc["time"] > end - pd.Timedelta(days=days)]).to_csv(out / f"{pair}_M1.csv", index=False)
    print(f"{pair}: {len(utc):,} bars to {end:%Y-%m-%d %H:%M} UTC")


def naive_to_utc(naive: pd.Series, zone: str) -> pd.Series:
    if zone.startswith("embedded"):
        return naive
    return ingest.to_ny(naive, zone).dt.tz_convert("UTC")


def from_mt5(pairs: list[str], history_days: int, zone: str | None) -> dict:
    import MetaTrader5 as mt5  # only on the PC with the terminal

    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        out = {}
        now = pd.Timestamp.now(tz="UTC").tz_localize(None) + pd.Timedelta(hours=14)  # beyond any server clock
        for pair in pairs:
            mt5.symbol_select(pair, True)
            r = mt5.copy_rates_range(pair, mt5.TIMEFRAME_M1, (now - pd.Timedelta(days=history_days + 3)).to_pydatetime(),
                                     now.to_pydatetime())
            if r is None or len(r) == 0:
                raise SystemExit(f"{pair}: no bars from MT5 ({mt5.last_error()})")
            df = pd.DataFrame(r)
            df["time"] = pd.to_datetime(df["time"], unit="s")  # server clock, naive
            out[pair] = df.rename(columns={"tick_volume": "tick_volume"})[["time", "open", "high", "low", "close", "tick_volume"]]
    finally:
        mt5.shutdown()
    z = zone or journal_broker_tz()
    if not z:
        z, scores, _ = ingest.detect_timezone(out[pairs[0]]["time"].sort_values().reset_index(drop=True))
        if scores[z] < ingest.TZ_PASS_SCORE:
            raise SystemExit(f"broker clock not verified (best {z} {scores[z]:.0%}); pass --broker-tz")
        print(f"broker clock detected: {z} ({scores[z]:.0%})")
    for pair, df in out.items():
        df["time"] = naive_to_utc(df["time"], z)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pairs", default="EURUSD,GBPUSD")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--history-days", type=int, default=60)
    ap.add_argument("--broker-tz", choices=ingest.TZ_CANDIDATES)
    ap.add_argument("--from-csv", type=Path, help="seed from an existing CSV instead of MT5 (any layout ingest reads)")
    ap.add_argument("--pair", help="pair of --from-csv")
    ap.add_argument("--out", type=Path, default=ROOT / "data")
    a = ap.parse_args(argv)
    if a.from_csv:
        if not a.pair:
            raise SystemExit("--from-csv needs --pair")
        utc, meta = ingest.load_utc(a.from_csv, a.pair, zone=a.broker_tz)
        print(f"{a.pair}: clock {meta['clock']}")
        write(a.pair, utc, a.days, a.history_days, a.out)
        return 0
    pairs = [p.strip().upper() for p in a.pairs.split(",") if p.strip()]
    for pair, utc in from_mt5(pairs, a.history_days, a.broker_tz).items():
        write(pair, utc, a.days, a.history_days, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
