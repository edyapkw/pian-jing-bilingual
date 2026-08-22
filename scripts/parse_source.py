#!/usr/bin/env python3
"""Parse Wikisource 騙經 chapter files into structured JSON."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "source"
OUT = ROOT / "data" / "book.zh.json"

CATALOG = [
    ("脫剝騙", "01", "第一類 脫剝騙", "Category 1 · Stripping Goods"),
    ("丢包騙", "02", "第二類 丟包騙", "Category 2 · Dropped Parcels"),
    ("換銀騙", "03", "第三類 換銀騙", "Category 3 · Exchanging Silver"),
    ("詐哄騙", "04", "第四類 詐哄騙", "Category 4 · Coaxing and Pretence"),
    ("偽交騙", "05", "第五類 偽交騙", "Category 5 · False Friendship"),
    ("牙行騙", "06", "第六類 牙行騙", "Category 6 · Brokers"),
    ("引賭騙", "07", "第七類 引賭騙", "Category 7 · Enticing to Gamble"),
    ("露財騙", "08", "第八類 露財騙", "Category 8 · Displaying Wealth"),
    ("謀財騙", "09", "第九類 謀財騙", "Category 9 · Scheming for Wealth"),
    ("盜劫騙", "10", "第十類 盜劫騙", "Category 10 · Theft and Robbery"),
    ("搶劫騙", "11", "第十一類 搶劫騙", "Category 11 · Forcible Seizure"),
    ("在船騙", "12", "第十二類 在船騙", "Category 12 · On the Boat"),
    ("詩詞騙", "13", "第十三類 詩詞騙", "Category 13 · Verse and Wit"),
    ("假銀騙", "14", "第十四類 假銀騙", "Category 14 · Counterfeit Silver"),
    ("衙役騙", "15", "第十五類 衙役騙", "Category 15 · Yamen Runners"),
    ("婚娶騙", "16", "第十六類 婚娶騙", "Category 16 · Marriage"),
    ("奸情騙", "17", "第十七類 姦情騙", "Category 17 · Illicit Affairs"),
    ("夫人騙", "18", "第十八類 婦人騙", "Category 18 · Women"),
    ("拐帶騙", "19", "第十九類 拐帶騙", "Category 19 · Abduction"),
    ("買學騙", "20", "第二十類 買學騙", "Category 20 · Buying Degrees"),
    ("僧道騙", "21", "第二十一類 僧道騙", "Category 21 · Monks and Daoists"),
    ("煉丹騙", "22", "第二十二類 煉丹騙", "Category 22 · Alchemy"),
    ("法術騙", "23", "第二十三類 法術騙", "Category 23 · Sorcery"),
    ("引嫖騙", "24", "第二十四類 引嫖騙", "Category 24 · Enticing to Brothels"),
]

TEMPLATE_RE = re.compile(r"\{\{[^}]*\}\}")
HTML_RE = re.compile(r"</?(?:div|onlyinclude)[^>]*>", re.I)
LINK_RE = re.compile(r"\[\[(?:[^|\]]+\|)?([^\]]+)\]\]")
BOLD_RE = re.compile(r"'{2,}")
HEADING_RE = re.compile(r"^==+\s*(.+?)\s*==+\s*$", re.M)


def clean_markup(text: str) -> str:
    text = re.sub(r"<onlyinclude>|</onlyinclude>", "", text)
    text = HTML_RE.sub("", text)
    text = TEMPLATE_RE.sub("", text)
    text = re.sub(r"\[\[Category:[^\]]+\]\]", "", text, flags=re.I)
    text = LINK_RE.sub(r"\1", text)
    text = BOLD_RE.sub("", text)
    text = text.replace("{{*|", "").replace("}}", "")
    text = re.sub(r"-{([^}]+)}-", r"\1", text)
    return text


def split_paragraphs(body: str) -> list[str]:
    body = body.replace("\u3000", "").strip()
    chunks = re.split(r"\n\s*\n", body)
    paras = []
    for chunk in chunks:
        line = re.sub(r"\s+", "", chunk)
        if not line:
            continue
        if line.startswith("按") or line.startswith("吾觀") or line.startswith("噫") or line.startswith("語云"):
            kind = "note"
        else:
            kind = "body"
        paras.append({"kind": kind, "zh": line})
    return paras


def parse_file(path: Path) -> list[dict]:
    raw = clean_markup(path.read_text(encoding="utf-8"))
    matches = list(HEADING_RE.finditer(raw))
    stories = []
    for i, m in enumerate(matches):
        title = re.sub(r"\s+", "", m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = raw[start:end]
        paras = split_paragraphs(body)
        if not paras:
            continue
        stories.append({"title_zh": title, "paragraphs": paras})
    return stories


def main() -> None:
    categories = []
    missing = []
    for slug, num, title_zh, title_en in CATALOG:
        path = SOURCE / f"{slug}.wiki"
        if not path.exists():
            missing.append(slug)
            continue
        stories = parse_file(path)
        categories.append({
            "id": f"cat-{num}",
            "slug": slug,
            "title_zh": title_zh,
            "title_en": title_en,
            "stories": stories,
        })
        print(f"{num} {slug}: {len(stories)} stories")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    book = {
        "title_zh": "江湖歷覽杜騙新書",
        "title_en": "A New Book for Foiling Swindlers, Based on Worldly Experience",
        "short_zh": "騙經",
        "short_en": "The Book of Swindles",
        "author_zh": "張應俞",
        "author_en": "Zhang Yingyu",
        "source": "https://zh.wikisource.org/wiki/騙經",
        "ctext": "https://ctext.org/wiki.pl?if=en&res=136159",
        "categories": categories,
    }
    OUT.write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")
    n_stories = sum(len(c["stories"]) for c in categories)
    n_paras = sum(len(s["paragraphs"]) for c in categories for s in c["stories"])
    print(f"wrote {OUT}  categories={len(categories)} stories={n_stories} paras={n_paras}")
    if missing:
        print("MISSING:", ", ".join(missing))


if __name__ == "__main__":
    main()
