#!/usr/bin/env python3
"""Analogue retrieval (RAG) for one packet: the most similar past cases from brain/library/.

    python scripts/retrieve.py --packet data/packets/2026-09-29_london.json --out runs/2026-09-29_london/0_analogues.json

Only cases whose outcome was known before this packet's close are eligible (no lookahead). Each analogue carries
its date, pair, direction, signature, strategy, thesis, outcome and the last 30 M1 candles before its decision.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.lib import brain, library  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--packet", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--k", type=int, default=8)
    a = ap.parse_args(argv)
    packet = json.loads(a.packet.read_text())
    res = library.retrieve(library.Library(brain.BRAIN), packet, a.k)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=1) + "\n")
    print(f"{a.out}: {len(res['analogues'])} analogues")
    return 0


if __name__ == "__main__":
    sys.exit(main())
