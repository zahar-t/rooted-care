#!/usr/bin/env python3
"""Validate the on-disk n8n workflow JSON files.

Independent of the generator, so it still holds after manual import fixes:
  * each file is valid JSON with nodes[] + connections{}
  * node names are unique (n8n keys connections by name)
  * every connection source and target references an existing node

Exit 0 = all good; exit 1 = a problem (prints which file/node).
"""

import json
import sys
from pathlib import Path

WF_DIR = Path(__file__).resolve().parent.parent / "workflows"


def validate_file(path: Path) -> list[str]:
    errs: list[str] = []
    try:
        w = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [f"invalid JSON: {e}"]

    nodes = w.get("nodes")
    conns = w.get("connections")
    if not isinstance(nodes, list) or not isinstance(conns, dict):
        return ["missing nodes[] or connections{}"]

    names = [n.get("name") for n in nodes]
    for n in {x for x in names if names.count(x) > 1}:
        errs.append(f"duplicate node name: {n!r}")
    nameset = set(names)

    for src, conn in conns.items():
        if src not in nameset:
            errs.append(f"connection source {src!r} is not a node")
        for slot in conn.get("main", []):
            for link in slot or []:
                tgt = link.get("node")
                if tgt not in nameset:
                    errs.append(f"connection target {tgt!r} (from {src!r}) is not a node")
    return errs


def main() -> int:
    files = sorted(WF_DIR.glob("*.json"))
    if not files:
        print(f"no workflow JSON files in {WF_DIR}", file=sys.stderr)
        return 1
    ok = True
    for path in files:
        errs = validate_file(path)
        if errs:
            ok = False
            print(f"FAIL  {path.name}")
            for e in errs:
                print(f"        - {e}")
        else:
            w = json.loads(path.read_text(encoding="utf-8"))
            print(f"OK    {path.name}  ({len(w['nodes'])} nodes)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
