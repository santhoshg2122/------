#!/usr/bin/env python3
"""Split one large MT5/CSV bar file into per-year files (GitHub rejects files > 100 MB).

    python tools/split_by_year.py "C:\\Users\\Windows\\Documents\\EURUSD_data\\EURUSD_1m_NY.csv"
    python tools/split_by_year.py <file> --symbol EURUSD --tf M1 --out data/raw

Streams line by line (no pandas, any size). The year is the first 19xx/20xx token on each line, or an
epoch-seconds/ms first field. The header, if any, is repeated in every output file. Prints the first lines
and per-year row counts so the format can be checked before Stage 0.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def year_of(line: str) -> str | None:
    m = YEAR.search(line)
    if m:
        return m.group(1)
    first = re.split(r"[,;\t]", line, maxsplit=1)[0].strip()
    if first.isdigit():
        v = int(first)
        return str(datetime.fromtimestamp(v / 1000 if v > 10**11 else v, tz=timezone.utc).year)
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path)
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--tf", default="M1")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "raw")
    a = ap.parse_args(argv)

    a.out.mkdir(parents=True, exist_ok=True)
    handles, counts, header, preview, last, skipped = {}, {}, None, [], "", 0
    with open(a.src, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        for i, line in enumerate(fh):
            if not line.strip():
                continue
            if i < 4:
                preview.append(line.rstrip("\r\n"))
            y = year_of(line)
            if y is None:
                if i == 0:
                    header = line
                else:
                    skipped += 1
                continue
            if y not in handles:
                h = open(a.out / f"{a.symbol}_{a.tf}_{y}.csv", "w", encoding="utf-8", newline="")
                if header:
                    h.write(header)
                handles[y], counts[y] = h, 0
            handles[y].write(line)
            counts[y] += 1
            last = line.rstrip("\r\n")
    for h in handles.values():
        h.close()

    print("First lines:")
    for p in preview:
        print("  " + p)
    print("Last line:\n  " + last)
    print(f"Header: {'yes' if header else 'no'} · unparseable lines skipped: {skipped}")
    for y in sorted(counts):
        f = a.out / f"{a.symbol}_{a.tf}_{y}.csv"
        print(f"  {f.name}: {counts[y]:,} rows, {f.stat().st_size / 1e6:.1f} MB")
    return 0 if counts else 1


if __name__ == "__main__":
    sys.exit(main())
