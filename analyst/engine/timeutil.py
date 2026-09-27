"""Session clock helpers. Everything is America/New_York (SPEC §1); no UTC offset is hard-coded."""
from __future__ import annotations

import json
from datetime import date, time, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
NY = "America/New_York"


def load_killzones(path: Path | None = None) -> dict:
    return json.loads((path or ROOT / "config" / "killzones.json").read_text())


def hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def minute_of_day(ts: pd.Series) -> pd.Series:
    """Minutes since NY midnight for a tz-aware NY series."""
    return ts.dt.hour * 60 + ts.dt.minute


def window_mask(ts: pd.Series, start: str, end: str) -> pd.Series:
    """Bars whose NY open time falls in [start, end). Handles windows that wrap midnight (Asian 19:00-02:00)."""
    s, e = hhmm(start), hhmm(end)
    m = minute_of_day(ts)
    s_m, e_m = s.hour * 60 + s.minute, e.hour * 60 + e.minute
    if s_m < e_m:
        return (m >= s_m) & (m < e_m)
    return (m >= s_m) | (m < e_m)


def window_minutes(start: str, end: str) -> int:
    s, e = hhmm(start), hhmm(end)
    d = (e.hour * 60 + e.minute) - (s.hour * 60 + s.minute)
    return d if d > 0 else d + 1440


def trading_date(ts: pd.Series) -> pd.Series:
    """The FX trading day: a bar at/after 17:00 NY belongs to the next calendar date (Sun 17:00 -> Monday)."""
    return (ts.dt.tz_localize(None) + pd.Timedelta(hours=7)).dt.normalize().dt.date


def easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def fx_holidays(year: int) -> set[date]:
    """Trading dates on which FX data is legitimately thin or absent. Missing data here is info, not an anomaly."""
    return {date(year, 1, 1), date(year, 12, 24), date(year, 12, 25), date(year, 12, 26),
            date(year, 12, 31), easter(year) - timedelta(days=2)}


def expected_trading_dates(year: int) -> list[date]:
    """Mon-Fri trading dates, minus Jan 1 and Dec 25 (the only days every broker is closed)."""
    d, out = date(year, 1, 1), []
    while d.year == year:
        if d.weekday() < 5 and not (d.month == 1 and d.day == 1) and not (d.month == 12 and d.day == 25):
            out.append(d)
        d += timedelta(days=1)
    return out


def validate_killzones(cfg: dict) -> None:
    """Fail loudly on a config edit that would make setup scoring ambiguous."""
    def m(s: str) -> int:
        t = hhmm(s)
        return t.hour * 60 + t.minute

    for group in ("windows", "pre_open_windows"):
        for k, w in cfg.get(group, {}).items():
            if not k.startswith("_"):
                hhmm(w["start"]), hhmm(w["end"])
    for kz, sc in cfg["setup_scoring"].items():
        if kz.startswith("_"):
            continue
        w = cfg["windows"][kz]
        start, end = m(w["start"]), m(w["end"])
        entry, primary = m(sc["entry_until"]), m(sc["primary_until"])
        if not (start < entry <= end):
            raise ValueError(f"{kz}: entry_until {sc['entry_until']} must be inside the killzone")
        if not (entry <= primary <= 17 * 60):
            raise ValueError(f"{kz}: primary_until must be between entry_until and the 17:00 day boundary")
        for s in sc["secondary_until"]:
            if not (entry <= m(s) <= 17 * 60):
                raise ValueError(f"{kz}: secondary horizon {s} outside entry_until..17:00")
