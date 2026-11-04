# 騙經 — Bilingual Reader

Password-free bilingual HTML for Zhang Yingyu’s late-Ming *杜騙新書* (*A New Book for Foiling Swindlers, Based on Worldly Experience*), also called *騙經*.

Layout follows the leeao-bilingual reader: Chinese on the left, meaning-first English on the right, with Contents, Side by side / Stacked, and font size.

## Open locally

```bash
cd /home/edmund/pian-jing-bilingual
python3 -m http.server 8766
# then open http://localhost:8766/
```

Or open `index.html` in a browser.

## Deploy (Vercel)

Static site — no build step, no password. Config matches [leeao-bilingual](https://github.com/edyapkw/leeao-bilingual) (`vercel.json` security headers + `robots.txt`).

**Live:** https://pian-jing-bilingual.vercel.app

```bash
vercel --prod
```

## Build from source

```bash
python3 scripts/parse_source.py
python3 scripts/verify_en2.py
python3 scripts/merge_en.py
python3 scripts/build_html.py
```

## Translation QA & Corrections (ModelScope Qwen)

Similar to the `leeao-bilingual` QA workflow, verify translation quality against the style guide in `TRANSLATION_GUIDE.md`:

```bash
# Evaluate a random sample of 50 pairs (default)
python3 scripts/verify_translations.py

# Evaluate a specific sample size
python3 scripts/verify_translations.py --sample 20

# Evaluate only author commentary notes (按語)
python3 scripts/verify_translations.py --notes-only

# Evaluate a specific category (e.g. cat-01 or c01)
python3 scripts/verify_translations.py --category cat-01

# Score all pairs in the book (resuming from existing progress)
python3 scripts/verify_translations.py --all --resume
```

Reports are saved to `translation_qa_report.json` and `TRANSLATION_ISSUES.md` with accuracy/fluency metrics and flagged items (score <= 6).

To stage and apply ModelScope Qwen corrections across `data/en2/*.json`, `data/book.json`, and `index.html`:

```bash
# Stage corrections into translation_corrections.json (queries ModelScope Qwen API,
# or falls back to ModelScope suggestions in translation_qa_report.json if quota is exhausted)
python3 scripts/apply_corrections.py --all-issues

# Apply staged corrections and rebuild data/book.json, index.html, and QA reports
python3 scripts/apply_corrections.py --apply
```

## Source

- Chinese: public-domain Wikisource [騙經](https://zh.wikisource.org/wiki/騙經)
- Catalogue page: [ctext.org wiki res=136159](https://ctext.org/wiki.pl?if=en&res=136159)
- English: original meaning-first study rendering (not the 2017 Columbia University Press selection)
