#!/usr/bin/env python3
"""Build a password-free bilingual HTML reader in the leeao-bilingual style."""
from __future__ import annotations

import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "book.json"
if not DATA.exists():
    DATA = ROOT / "data" / "book.zh.json"
OUT = ROOT / "index.html"


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def pair(zh: str, en: str, extra: str = "", inner_zh: str = "p", inner_en: str = "p", cls_zh: str = "", cls_en: str = "") -> str:
    zc = f' class="{cls_zh}"' if cls_zh else ""
    ec = f' class="{cls_en}"' if cls_en else ""
    return (
        f'<div class="pair {extra}">\n'
        f'  <div class="col zh"><{inner_zh}{zc}>{esc(zh)}</{inner_zh}></div>\n'
        f'  <div class="col en"><{inner_en}{ec}>{esc(en)}</{inner_en}></div>\n'
        f"</div>"
    )


def render_story(story: dict, sid: str) -> str:
    title_zh = story["title_zh"]
    title_en = story.get("title_en") or title_zh
    parts = [
        f'<article class="entry" id="{sid}">',
        pair(title_zh, title_en, "pair-entry-title", "h3", "h3", "entry-title", "entry-title"),
    ]
    for para in story["paragraphs"]:
        zh = para["zh"]
        en = para.get("en") or ""
        kind = para.get("kind", "body")
        if kind == "note":
            parts.append(pair(zh, en, "pair-note", cls_zh="note", cls_en="note"))
        else:
            parts.append(pair(zh, en))
    parts.append("</article>")
    return "\n".join(parts)


def main() -> None:
    book = json.loads(DATA.read_text(encoding="utf-8"))
    toc_items = []
    chapters = []
    for cat in book["categories"]:
        story_links = []
        stories_html = []
        for i, story in enumerate(cat["stories"]):
            sid = f'{cat["id"]}-s{i:02d}'
            title_en = story.get("title_en") or story["title_zh"]
            story_links.append(
                f'<li><a href="#{esc(sid)}">{esc(story["title_zh"])}'
                f'<span class="toc-en">{esc(title_en)}</span></a></li>'
            )
            stories_html.append(render_story(story, sid))
        toc_items.append(
            f'<li class="toc-cat">'
            f'<a class="toc-cat-link" href="#{esc(cat["id"])}">{esc(cat["title_zh"])}'
            f'<span class="toc-en">{esc(cat["title_en"])}</span></a>'
            f'<ol class="toc-stories">{"".join(story_links)}</ol></li>'
        )
        chapters.append(
            f'<section class="chapter" id="{esc(cat["id"])}">'
            f'<div class="chapter-head"><h2>{esc(cat["title_zh"])}</h2>'
            f'<p class="chapter-en">{esc(cat["title_en"])}</p></div>'
            + "".join(stories_html)
            + "</section>"
        )

    css = r'''
:root {
  --bg0: #f3efe6;
  --bg1: #ebe4d6;
  --ink: #1c1914;
  --ink-soft: #4a4338;
  --zh: #1a1712;
  --en: #2a332c;
  --line: rgba(40, 32, 20, 0.12);
  --accent: #0f5c4c;
  --accent-soft: #d7ebe4;
  --panel: rgba(255, 252, 245, 0.88);
  --fs: 1.05rem;
  --lh: 1.75;
  --max: 1180px;
  --gap: 1.5rem;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  color: var(--ink);
  font-family: "Source Serif 4", "Noto Serif SC", "Songti SC", "SimSun", Georgia, serif;
  font-size: var(--fs);
  line-height: var(--lh);
  background:
    radial-gradient(1200px 600px at 10% -10%, #dff0ea 0%, transparent 55%),
    radial-gradient(900px 500px at 100% 0%, #f0e2c8 0%, transparent 50%),
    linear-gradient(180deg, var(--bg0), var(--bg1));
  min-height: 100vh;
}
a { color: var(--accent); text-underline-offset: 0.15em; }
.hero {
  padding: 4.5rem 1.25rem 2.5rem;
  border-bottom: 1px solid var(--line);
  background:
    linear-gradient(180deg, rgba(255,252,245,0.55), transparent),
    repeating-linear-gradient(90deg, transparent, transparent 47px, rgba(15,92,76,0.035) 48px);
}
.hero-inner { max-width: var(--max); margin: 0 auto; }
.brand {
  margin: 0 0 0.75rem;
  font-family: "DM Sans", "Noto Sans SC", sans-serif;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  font-size: 0.78rem;
  color: var(--accent);
  font-weight: 600;
}
.hero h1 {
  margin: 0;
  font-size: clamp(2.2rem, 5vw, 3.6rem);
  line-height: 1.15;
  letter-spacing: 0.04em;
  font-weight: 700;
}
.hero-sub {
  margin: 0.85rem 0 0;
  max-width: 42rem;
  color: var(--ink-soft);
  font-size: 1.15rem;
}
.hero-meta {
  margin: 1.4rem 0 0;
  font-family: "DM Sans", "Noto Sans SC", sans-serif;
  font-size: 0.86rem;
  color: var(--ink-soft);
}
.chrome {
  position: sticky;
  top: 0;
  z-index: 20;
}
.toolbar {
  backdrop-filter: blur(10px);
  background: rgba(243, 239, 230, 0.92);
  border-bottom: 1px solid var(--line);
}
.toolbar-inner {
  max-width: var(--max);
  margin: 0 auto;
  padding: 0.65rem 1.25rem;
  display: flex;
  flex-wrap: wrap;
  gap: 0.6rem;
  align-items: center;
  justify-content: space-between;
}
.btn {
  font-family: "DM Sans", "Noto Sans SC", sans-serif;
  font-size: 0.85rem;
  border: 1px solid var(--line);
  background: var(--panel);
  color: var(--ink);
  padding: 0.45rem 0.8rem;
  border-radius: 999px;
  cursor: pointer;
}
.btn:hover, .btn.active {
  background: var(--accent);
  border-color: var(--accent);
  color: #f7fffc;
}
.seg { display: flex; gap: 0.35rem; }
.toc {
  border-bottom: 1px solid var(--line);
  background: rgba(255,252,245,0.97);
  box-shadow: 0 16px 28px rgba(28, 25, 20, 0.1);
  max-height: min(70vh, 36rem);
  overflow: auto;
}
.toc[hidden] { display: none !important; }
.toc-inner {
  max-width: var(--max);
  margin: 0 auto;
  padding: 1rem 1.25rem 1.4rem;
}
.toc h2 {
  margin: 0 0 0.6rem;
  font-size: 1rem;
  font-family: "DM Sans", "Noto Sans SC", sans-serif;
}
.toc-cats {
  margin: 0;
  padding: 0;
  list-style: none;
  columns: 2;
  column-gap: 2rem;
}
.toc-cat {
  break-inside: avoid;
  margin: 0 0 0.85rem;
  padding-bottom: 0.55rem;
  border-bottom: 1px dashed var(--line);
}
.toc-cat-link {
  display: block;
  font-weight: 600;
  text-decoration: none;
  line-height: 1.35;
}
.toc-stories {
  margin: 0.35rem 0 0;
  padding-left: 1.1rem;
  font-size: 0.88rem;
}
.toc-stories a {
  display: block;
  text-decoration: none;
  padding: 0.12rem 0;
  color: var(--ink-soft);
  line-height: 1.35;
}
.toc a:hover { text-decoration: underline; }
.toc-en {
  display: block;
  font-family: "DM Sans", "Noto Sans SC", sans-serif;
  font-size: 0.78em;
  font-weight: 400;
  color: var(--ink-soft);
}
.chapter, .entry {
  scroll-margin-top: 4.25rem;
}
.reader {
  max-width: var(--max);
  margin: 0 auto;
  padding: 1.5rem 1.25rem 4rem;
}
.chapter { margin: 2.5rem 0 3.5rem; }
.chapter-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.75rem 1.25rem;
  margin-bottom: 1.4rem;
  padding-bottom: 0.7rem;
  border-bottom: 2px solid var(--accent);
}
.chapter-head h2 {
  margin: 0;
  font-size: 1.65rem;
  letter-spacing: 0.06em;
}
.chapter-en {
  margin: 0;
  font-family: "DM Sans", "Noto Sans SC", sans-serif;
  color: var(--ink-soft);
  font-size: 0.95rem;
}
.entry {
  margin: 1.75rem 0 2.25rem;
  padding: 1.1rem 0 0.2rem;
  border-top: 1px dashed var(--line);
}
.pair {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--gap);
  margin: 0.55rem 0;
  align-items: start;
}
body.layout-stack .pair {
  grid-template-columns: 1fr;
  gap: 0.25rem;
  margin: 0.9rem 0;
  padding-bottom: 0.7rem;
  border-bottom: 1px solid var(--line);
}
.col { min-width: 0; }
.col.zh { color: var(--zh); }
.col.en {
  color: var(--en);
  font-family: "Source Serif 4", Georgia, "Times New Roman", serif;
}
.col p, .col h3, .col h4 { margin: 0; }
.entry-title {
  font-size: 1.12rem;
  margin: 0.2rem 0 0.55rem !important;
  line-height: 1.4;
}
.note {
  font-size: 0.92rem;
  color: var(--ink-soft);
  border-left: 3px solid var(--accent-soft);
  padding-left: 0.8rem;
}
.site-footer {
  max-width: var(--max);
  margin: 0 auto;
  padding: 1.5rem 1.25rem 3rem;
  color: var(--ink-soft);
  font-family: "DM Sans", "Noto Sans SC", sans-serif;
  font-size: 0.88rem;
  border-top: 1px solid var(--line);
}
@media (max-width: 860px) {
  .pair { grid-template-columns: 1fr; gap: 0.2rem; }
  .toc-cats { columns: 1; }
}
'''

    js = r'''
(function () {
  const root = document.documentElement;
  const toc = document.getElementById('toc');
  const tocToggle = document.getElementById('tocToggle');
  let fs = 1.05;
  function setTocOpen(open) {
    if (open) toc.removeAttribute('hidden');
    else toc.setAttribute('hidden', '');
    tocToggle.classList.toggle('active', open);
    tocToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  tocToggle.addEventListener('click', (e) => {
    e.stopPropagation();
    setTocOpen(toc.hasAttribute('hidden'));
  });
  toc.querySelectorAll('a').forEach(a => a.addEventListener('click', () => setTocOpen(false)));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') setTocOpen(false);
  });
  document.addEventListener('click', (e) => {
    if (toc.hasAttribute('hidden')) return;
    if (!toc.contains(e.target) && e.target !== tocToggle) setTocOpen(false);
  });
  document.querySelectorAll('.layout-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.layout-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      document.body.classList.toggle('layout-stack', btn.dataset.layout === 'stack');
    });
  });
  function applyFs() { root.style.setProperty('--fs', fs + 'rem'); }
  document.getElementById('fontUp').addEventListener('click', () => { fs = Math.min(1.45, +(fs + 0.06).toFixed(2)); applyFs(); });
  document.getElementById('fontDown').addEventListener('click', () => { fs = Math.max(0.9, +(fs - 0.06).toFixed(2)); applyFs(); });
})();
'''

    page = f'''<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(book["short_zh"])} — Bilingual Reader</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,600;0,9..40,700;1,9..40,400&family=Noto+Serif+SC:wght@400;600;700&family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;0,8..60,700;1,8..60,400&display=swap" rel="stylesheet">
<style>{css}</style>
</head>
<body>
<header class="hero" id="top">
  <div class="hero-inner">
    <p class="brand">明 · 張應俞 · Bilingual Reader</p>
    <h1>{esc(book["short_zh"])}</h1>
    <p class="hero-sub">{esc(book["title_en"])} — {esc(book["author_en"])}’s late-Ming collection of fraud stories, with a moral note after each tale.</p>
    <p class="hero-meta">Chinese source: <a href="{esc(book["source"])}" target="_blank" rel="noopener">Wikisource 騙經</a> · also <a href="{esc(book["ctext"])}" target="_blank" rel="noopener">ctext.org</a> · Chinese left · English right · no password</p>
  </div>
</header>
<div class="chrome">
<nav class="toolbar" aria-label="Reading controls">
  <div class="toolbar-inner">
    <button type="button" id="tocToggle" class="btn" aria-controls="toc" aria-expanded="false">Contents</button>
    <div class="seg" role="group" aria-label="Layout">
      <button type="button" class="btn layout-btn active" data-layout="side">Side by side</button>
      <button type="button" class="btn layout-btn" data-layout="stack">Stacked</button>
    </div>
    <div class="seg" role="group" aria-label="Text size">
      <button type="button" class="btn" id="fontDown" aria-label="Smaller text">A−</button>
      <button type="button" class="btn" id="fontUp" aria-label="Larger text">A+</button>
    </div>
  </div>
</nav>
<aside class="toc" id="toc" hidden>
  <div class="toc-inner">
    <h2>目录 / Contents</h2>
    <ol class="toc-cats">
      {"".join(toc_items)}
    </ol>
  </div>
</aside>
</div>
<main class="reader" id="reader">
{"".join(chapters)}
</main>
<footer class="site-footer">
  <p>Original Chinese by 張應俞 (Zhang Yingyu), published around 1617 as 《江湖歷覽杜騙新書》. The Chinese text is public domain. English is an original meaning-first study rendering, not the 2017 Columbia University Press selection.</p>
  <p><a href="#top">Back to top</a></p>
</footer>
<script>{js}</script>
</body>
</html>
'''
    OUT.write_text(page, encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
