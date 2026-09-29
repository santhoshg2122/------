"""Clock helpers. Packets and the Brain are UTC (CLAUDE.md); New York time is used only to recognise a broker's clock."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
NY = "America/New_York"


def load_config(name: str) -> dict:
    return json.loads((ROOT / "config" / f"{name}.json").read_text())


def trading_date(ts: pd.Series) -> pd.Series:
    """FX trading day of a tz-aware NY series: a bar at/after 17:00 NY belongs to the next calendar date."""
    return (ts.dt.tz_localize(None) + pd.Timedelta(hours=7)).dt.normalize().dt.date
