#!/usr/bin/env python3
"""Merge category English fragments into book.json."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZH = ROOT / "data" / "book.zh.json"
EN_DIR = ROOT / "data" / "en2" if (ROOT / "data" / "en2").is_dir() else ROOT / "data" / "en"
OUT = ROOT / "data" / "book.json"


def main() -> None:
    book = json.loads(ZH.read_text(encoding="utf-8"))
    by_id = {}
    for path in sorted(EN_DIR.glob("*.json")):
        frag = json.loads(path.read_text(encoding="utf-8"))
        for cat in frag:
            by_id[cat["id"]] = cat
    filled = 0
    total = 0
    for cat in book["categories"]:
        enc = by_id.get(cat["id"])
        if not enc:
            continue
        if enc.get("title_en"):
            cat["title_en"] = enc["title_en"]
        stories = {s["title_zh"]: s for s in enc.get("stories", [])}
        for story in cat["stories"]:
            es = stories.get(story["title_zh"])
            if not es:
                continue
            if es.get("title_en"):
                story["title_en"] = es["title_en"]
            ep = es.get("paragraphs") or []
            for i, para in enumerate(story["paragraphs"]):
                total += 1
                if i < len(ep) and ep[i].get("en"):
                    para["en"] = ep[i]["en"]
                    filled += 1
    OUT.write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT} filled={filled} total_paras={sum(len(s['paragraphs']) for c in book['categories'] for s in c['stories'])}")


if __name__ == "__main__":
    main()
