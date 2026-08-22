#!/usr/bin/env python3
"""Verify revised translation files in data/en2 against data/en-src."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "en-src"
NEW = ROOT / "data" / "en2"


def main() -> int:
    ok = True
    total_paras = 0
    for sp in sorted(SRC.glob("cat-*.json")):
        np = NEW / sp.name
        if not np.exists():
            print(f"MISSING  {sp.name}")
            ok = False
            continue
        try:
            s = json.loads(sp.read_text(encoding="utf-8"))[0]
            n = json.loads(np.read_text(encoding="utf-8"))[0]
        except Exception as e:  # noqa: BLE001
            print(f"BADJSON  {sp.name}: {e}")
            ok = False
            continue
        problems = []
        if n.get("id") != s.get("id"):
            problems.append(f"id {n.get('id')!r} != {s.get('id')!r}")
        ss, ns = s["stories"], n.get("stories", [])
        if len(ss) != len(ns):
            problems.append(f"stories {len(ns)} != {len(ss)}")
        for i, (a, b) in enumerate(zip(ss, ns)):
            if b.get("title_zh") != a["title_zh"]:
                problems.append(f"story {i} title_zh mismatch")
            if not b.get("title_en"):
                problems.append(f"story {i} missing title_en")
            ap, bp = a["paragraphs"], b.get("paragraphs", [])
            if len(ap) != len(bp):
                problems.append(f"story {i} paras {len(bp)} != {len(ap)}")
            for j, p in enumerate(bp):
                if not p.get("en", "").strip():
                    problems.append(f"story {i} para {j} empty en")
        npar = sum(len(x["paragraphs"]) for x in ns)
        total_paras += npar
        if problems:
            ok = False
            print(f"FAIL     {sp.name}: " + "; ".join(problems[:6]))
        else:
            print(f"OK       {sp.name} ({len(ns)} stories, {npar} paras)")
    print(f"total paras in en2: {total_paras}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
