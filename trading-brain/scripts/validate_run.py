#!/usr/bin/env python3
"""Check an agent's output before the next step of /cycle runs.

    python scripts/validate_run.py runs/2026-09-29_london/1_analyst.json --packet data/packets/2026-09-29_london.json
    python scripts/validate_run.py data/packets/2026-09-29_london.json          # a packet against its schema

Fails (exit 1, one line saying why) when the file is missing or not JSON, breaks its schema, says ABORT,
carries a different packet hash, or cites an object id that is not in the packet. Prints OK otherwise.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
ID = re.compile(r"\b(?:EU|GB)-[A-Z]+-\d+\b|\bX-SMT-\d+\b")


def schema_for(path: Path) -> Path:
    name = path.stem
    if path.parent.name == "packets":
        return ROOT / "schemas" / "packet.schema.json"
    return ROOT / "schemas" / f"{name}.schema.json"


def packet_ids(packet: dict) -> set[str]:
    return set(ID.findall(json.dumps(packet)))


def validate(path: Path, packet_path: Path | None = None) -> str | None:
    if not path.exists():
        return f"{path} missing"
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return f"{path.name} is not valid JSON: {e.msg} at line {e.lineno}"
    sp = schema_for(path)
    if not sp.exists():
        return f"no schema for {path.name}"
    try:
        jsonschema.validate(doc, json.loads(sp.read_text()))
    except jsonschema.ValidationError as e:
        where = "/".join(str(p) for p in e.absolute_path) or "(root)"
        return f"{path.name} breaks its schema at {where}: {e.message[:200]}"
    if doc.get("status") == "ABORT":
        return f"{path.name} says ABORT: {doc.get('reason') or doc.get('abort_reason') or ''}".rstrip(": ")
    if packet_path is not None:
        packet = json.loads(packet_path.read_text())
        if "packet_sha256" in doc and doc["packet_sha256"] != packet.get("sha256"):
            return f"{path.name} packet_sha256 differs from the packet's sha256"
        unknown = sorted(set(ID.findall(json.dumps(doc))) - packet_ids(packet))
        if unknown:
            return f"{path.name} cites ids that are not in the packet: {', '.join(unknown[:10])}"
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", type=Path)
    ap.add_argument("--packet", type=Path)
    a = ap.parse_args(argv)
    err = validate(a.file, a.packet)
    print("OK" if err is None else f"INVALID: {err}")
    return 0 if err is None else 1


if __name__ == "__main__":
    sys.exit(main())
