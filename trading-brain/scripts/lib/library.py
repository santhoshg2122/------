"""Pattern library and analogue retrieval (B013): the Brain's memory of what charts looked like and what followed.

Every graded setup (survivor or shadow) and every big move becomes a *case*: its levels, the agents' reading, the
outcome, and the **candle picture** — the last `window` M1 candles of both pairs before the decision. Each picture
is turned into a shape vector (ATR-normalised path, body and wick ratios, signed tick volume, both pairs), so a new
session can retrieve the most similar past cases with plain numpy cosine similarity. A case is only retrievable once
its outcome was known at the replay clock, so retrieval never looks ahead.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

PAIRS = ("EURUSD", "GBPUSD")
WINDOW = 60
KEEP_ROWS = 30   # candles of the case's own pair shown to the agents per analogue


def _features(bars: pd.DataFrame, window: int = WINDOW) -> np.ndarray:
    """ATR-normalised shape of the last `window` candles: close path, body, upper and lower wick, signed volume."""
    b = bars.tail(window)
    n = len(b)
    out = np.zeros((5, window))
    if n < 2:
        return out.ravel()
    o, h, l, c, v = (b[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "tick_volume"))
    rng = np.maximum(h - l, 1e-9)
    atr = max(float(np.mean(rng)), 1e-9)
    feats = np.vstack([
        (c - c[0]) / atr / np.sqrt(n),                         # path
        (c - o) / rng,                                         # body ratio, signed
        (h - np.maximum(o, c)) / rng,                          # upper wick
        (np.minimum(o, c) - l) / rng,                          # lower wick
        np.cumsum(np.sign(c - o) * v) / max(float(v.sum()), 1.0),  # signed tick-volume path
    ])
    weights = np.array([3.0, 1.0, 1.0, 1.0, 1.5])[:, None]
    out[:, window - n:] = feats * weights
    return out.ravel()


def shape_vector(bars_by_pair: dict[str, pd.DataFrame], window: int = WINDOW) -> np.ndarray:
    v = np.concatenate([_features(bars_by_pair.get(p, pd.DataFrame(columns=["open", "high", "low", "close", "tick_volume"])),
                                  window) for p in PAIRS])
    norm = np.linalg.norm(v)
    return (v / norm if norm > 0 else v).astype(np.float32)


def rows_to_frame(rows: list, date: str) -> pd.DataFrame:
    """Packet rows ['HH:MM' or 'YYYY-MM-DD HH:MM', o, h, l, c, v] -> DataFrame with UTC times."""
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "tick_volume"])
    t = [pd.Timestamp(r[0] if len(r[0]) > 5 else f"{date} {r[0]}", tz="UTC") for r in rows]
    return pd.DataFrame({"time": t, "open": [r[1] for r in rows], "high": [r[2] for r in rows],
                         "low": [r[3] for r in rows], "close": [r[4] for r in rows], "tick_volume": [r[5] for r in rows]})


def packet_bars(packet: dict) -> dict[str, pd.DataFrame]:
    """Context + session M1 candles of both pairs from a packet, in time order."""
    out = {}
    for pair, pk in (packet.get("pairs") or {}).items():
        out[pair] = pd.concat([rows_to_frame(pk.get("bars_m1_context", []), packet["date"]),
                               rows_to_frame(pk.get("bars_m1", []), packet["date"])], ignore_index=True)
    return out


def _snapshot(bars: pd.DataFrame, n: int = KEEP_ROWS) -> list:
    b = bars.tail(n)
    return [[t.strftime("%Y-%m-%d %H:%M"), round(float(o), 5), round(float(h), 5), round(float(lo), 5), round(float(c), 5)]
            for t, o, h, lo, c in zip(b["time"], b["open"], b["high"], b["low"], b["close"])]


class Library:
    def __init__(self, root: Path):
        self.dir = root / "library"
        self.cases_path = self.dir / "cases.jsonl"
        self.vec_path = self.dir / "vectors.npy"

    def load(self) -> tuple[list[dict], np.ndarray]:
        if not self.cases_path.exists():
            return [], np.zeros((0, 2 * 5 * WINDOW), dtype=np.float32)
        cases = [json.loads(x) for x in self.cases_path.read_text().splitlines() if x.strip()]
        vecs = np.load(self.vec_path) if self.vec_path.exists() else np.zeros((0, 2 * 5 * WINDOW), dtype=np.float32)
        if len(vecs) != len(cases):
            raise ValueError(f"library corrupt: {len(cases)} cases but {len(vecs)} vectors")
        return cases, vecs

    def add(self, new: list[tuple[dict, np.ndarray]]) -> int:
        if not new:
            return 0
        self.dir.mkdir(parents=True, exist_ok=True)
        cases, vecs = self.load()
        known = {c["case_id"] for c in cases}
        new = [(c, v) for c, v in new if c["case_id"] not in known]
        if not new:
            return 0
        with self.cases_path.open("a") as fh:
            for c, _ in new:
                fh.write(json.dumps(c) + "\n")
        np.save(self.vec_path, np.vstack([vecs] + [v[None, :] for _, v in new]).astype(np.float32))
        return len(new)


def case_from_instance(inst: dict, bars_by_pair: dict[str, pd.DataFrame], known_at: pd.Timestamp) -> tuple[dict, np.ndarray]:
    """A graded setup -> (case, vector). The picture is the candles before the decision (the session close)."""
    t0 = pd.Timestamp(inst["flagged_at"])
    before = {p: b[b["time"] < t0] for p, b in bars_by_pair.items()}
    case = {"case_id": inst["id"], "kind": "shadow" if inst.get("kind") == "shadow" else "setup",
            "known_at": known_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "decided_at": inst["flagged_at"],
            "date": inst["date"], "session": inst["session"], "pair": inst["pair"], "direction": inst.get("direction"),
            "signature": inst.get("signature"), "strategy_id": inst.get("strategy_id"),
            "candle_basis": inst.get("candle_basis"), "thesis": inst.get("thesis"),
            "entry": inst["entry"], "invalidation": inst["invalidation"], "target": inst["target"],
            "outcome": inst["outcome"], "why": inst.get("why"), "move_pips": inst.get("move_pips"),
            "drop_reason": inst.get("drop_reason"), "critic_verdict": inst.get("critic_verdict"),
            "candles": _snapshot(before.get(inst["pair"], pd.DataFrame(columns=["time", "open", "high", "low", "close"])))}
    return case, shape_vector(before)


def case_from_big_move(key: str, pair: str, bm: dict, packet: dict, known_at: pd.Timestamp) -> tuple[dict, np.ndarray]:
    """A big move -> (case, vector). The picture is the candles before the move started; the move is the 'outcome'."""
    bars = packet_bars(packet)
    start = pd.Timestamp(bm["start"] if len(bm["start"]) > 5 else f"{packet['date']} {bm['start']}", tz="UTC")
    before = {p: b[b["time"] < start] for p, b in bars.items()}
    case = {"case_id": f"{key}:{bm['id']}", "kind": "big_move", "known_at": known_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "decided_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "date": packet["date"], "session": packet["session"],
            "pair": pair, "direction": "long" if bm["pips"] > 0 else "short", "outcome": f"move {bm['pips']:+.1f} pips",
            "move_pips": bm["pips"], "preceded_by": bm.get("preceded_by", []),
            "entry_was_available": bm.get("entry_was_available"),
            "candles": _snapshot(before.get(pair, pd.DataFrame(columns=["time", "open", "high", "low", "close"])))}
    return case, shape_vector(before)


def retrieve(lib: Library, packet: dict, k: int = 8, session_bonus: float = 0.05) -> dict:
    """The k most similar past cases whose outcome was known before this packet's close."""
    cases, vecs = lib.load()
    end = pd.Timestamp(f"{packet['date']} {packet['window_utc'][1]}", tz="UTC")
    q = shape_vector(packet_bars(packet))
    ok = [i for i, c in enumerate(cases) if pd.Timestamp(c["known_at"]) <= end and pd.Timestamp(c["decided_at"]) < end]
    if not ok or not np.any(q):
        return {"query": {"date": packet["date"], "session": packet["session"]}, "analogues": [], "summary": {}}
    sims = vecs[ok] @ q
    sims = sims + np.array([session_bonus if cases[i]["session"] == packet["session"] else 0.0 for i in ok])
    order = np.argsort(-sims)[:k]
    out = []
    for j in order:
        c = dict(cases[ok[j]])
        c["similarity"] = round(float(sims[j]), 3)
        out.append(c)
    summary: dict = {}
    for c in out:
        key = f"{c['pair']} {c.get('direction')}"
        s = summary.setdefault(key, {"hit": 0, "miss": 0, "expired": 0, "big_move": 0})
        o = c["outcome"]
        s["big_move" if c["kind"] == "big_move" else (o if o in ("hit", "miss") else "expired")] += 1
    return {"query": {"date": packet["date"], "session": packet["session"]}, "analogues": out, "summary": summary}
