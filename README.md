# 騙經 — Bilingual Reader

Password-free bilingual HTML for Zhang Yingyu’s late-Ming *杜騙新書* (*A New Book for Foiling Swindlers, Based on Worldly Experience*), also called *騙經*.

Layout follows the leeao-bilingual reader: Chinese on the left, meaning-first English on the right, with Contents, Side by side / Stacked, and font size.

## Open locally

```bash
cd /home/edykw/pian-jing-bilingual
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


```bash
python3 scripts/parse_source.py
python3 scripts/merge_en.py
python3 scripts/build_html.py
```

## Source

- Chinese: public-domain Wikisource [騙經](https://zh.wikisource.org/wiki/騙經)
- Catalogue page: [ctext.org wiki res=136159](https://ctext.org/wiki.pl?if=en&res=136159)
- English: original meaning-first study rendering (not the 2017 Columbia University Press selection)
