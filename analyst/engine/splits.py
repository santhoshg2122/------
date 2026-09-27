"""Calendar split (SPEC §2) and the only sanctioned way to load bars.

A split may read every bar up to its own end date (earlier data is the past, so lookback into an earlier split
is allowed). Train can never see Validation dates; nothing can see Holdout dates unless
config/HOLDOUT_UNLOCKED exists (HUMAN GATE H3).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from .timeutil import ROOT

SPLITS_PATH = ROOT / "config" / "splits.json"
UNLOCK_PATH = ROOT / "config" / "HOLDOUT_UNLOCKED"
BARS_DIR = ROOT / "data" / "bars"
SPLITS = ("train", "validation", "holdout")


class HoldoutLocked(PermissionError):
    pass


class SplitViolation(ValueError):
    pass


def compute_splits(complete_years: list[int]) -> dict:
    """Last ten complete years -> 7 train / 2 validation / 1 holdout.

    With fewer than ten complete years the 70/20/10 proportions are kept (at least one year each) and the
    shortfall is recorded in the file; that is a question for H1, not something decided silently.
    """
    years = sorted(complete_years)
    if len(years) < 3:
        raise ValueError(f"need >= 3 complete years for a train/validation/holdout split, have {years}")
    years = years[-10:]
    n = len(years)
    n_hold = 1
    n_val = max(1, round(0.2 * n))
    n_train = n - n_val - n_hold
    tr, va, ho = years[:n_train], years[n_train:n_train + n_val], years[n_train + n_val:]
    for a, b in zip(years, years[1:]):
        if b != a + 1:
            raise ValueError(f"complete years are not contiguous: {years}")

    def block(ys):
        return {"years": ys, "start": f"{ys[0]}-01-01", "end": f"{ys[-1]}-12-31"}

    return {
        "basis": "trading_date (17:00 NY boundary), calendar years, never shuffled",
        "train": block(tr), "validation": block(va), "holdout": block(ho),
        "standard_10y": n == 10,
        "note": "" if n == 10 else f"only {n} complete years; 70/20/10 proportions used — confirm at H1",
    }


def write_splits(splits: dict, force: bool = False, path: Path = SPLITS_PATH) -> None:
    """Boundaries are fixed once written. Moving them after research has started would leak, so it needs --force."""
    if path.exists():
        old = json.loads(path.read_text())
        same = all(old.get(k) == splits.get(k) for k in SPLITS)
        if not same and not force:
            raise SplitViolation(f"{path} already holds different boundaries; re-split only with --force "
                                 f"(old train {old['train']['years']}, new {splits['train']['years']})")
        if same:
            return
    out = dict(splits, written_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    path.write_text(json.dumps(out, indent=2) + "\n")


def load_splits(path: Path = SPLITS_PATH) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"{path} missing — run `python run.py stage0` first")
    return json.loads(path.read_text())


def _d(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


def holdout_unlocked(unlock_path: Path = UNLOCK_PATH) -> bool:
    return unlock_path.exists()


def check_access(split: str, start: date | str | None, end: date | str | None, *,
                 splits: dict | None = None, unlock_path: Path = UNLOCK_PATH) -> tuple[date, date]:
    """Return the (start, end) trading-date range a caller in `split` may read, or raise."""
    if split not in SPLITS:
        raise SplitViolation(f"split must be one of {SPLITS}, got {split!r}")
    sp = splits or load_splits()
    hold_start = _d(sp["holdout"]["start"])
    if split == "holdout" and not holdout_unlocked(unlock_path):
        raise HoldoutLocked("holdout is sealed: config/HOLDOUT_UNLOCKED is absent (HUMAN GATE H3)")
    limit = _d(sp[split]["end"])
    s = _d(start) if start is not None else date(1900, 1, 1)
    e = _d(end) if end is not None else limit
    if e > limit:
        raise SplitViolation(f"split {split!r} may read up to {limit}, asked for {e}")
    if e >= hold_start and not holdout_unlocked(unlock_path):
        raise HoldoutLocked(f"{e} is inside the sealed holdout (from {hold_start})")
    if s > e:
        raise SplitViolation(f"start {s} after end {e}")
    return s, e


def load_bars(symbol: str, split: str, start=None, end=None, *, bars_dir: Path = BARS_DIR,
              splits: dict | None = None, unlock_path: Path = UNLOCK_PATH) -> pd.DataFrame:
    """Clean NY-time bars for trading dates in [start, end], subject to the split rules above."""
    s, e = check_access(split, start, end, splits=splits, unlock_path=unlock_path)
    frames = []
    for p in sorted(bars_dir.glob(f"{symbol}_*.parquet")):
        y = int(p.stem.rsplit("_", 1)[1])
        if s.year <= y <= e.year:
            frames.append(pd.read_parquet(p))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    td = pd.to_datetime(df["trading_date"]).dt.date
    return df[(td >= s) & (td <= e)].reset_index(drop=True)
