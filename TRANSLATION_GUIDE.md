# Translation Style Guide — 騙經 Bilingual Reader

Purpose: keep the English faithful to Zhang Yingyu’s Ming stories, but read like natural modern English. Use this when editing `data/book.json` or re-translating any chapter.

The English is a **meaning-first study crib**, not a literary prize translation. It should be accurate, plain, and easy to scan next to the Chinese.

---

## 1. Core principle

- **Translate meaning, not word order.** Chinese often stacks verbs and clauses; English should be broken into normal sentences.
- **Keep the author’s voice.** The 按 / 吾觀 notes are moral commentary, not neutral narration. They should sound like a Ming essayist warning merchants.
- **Be concrete.** Prefer “took the cloth and ran” over “stripped the cloth.”

---

## 2. Recurring problems found in the current draft, with fixes

### 2.1 脫 / 脫去 — stop saying “strip”

**Problem.** 脫 is rendered as “strip / stripped / stripping” 40+ times. In English that sounds like undressing, not swindling.

**Fix.** Use the natural verb for the situation.

| Chinese | Awkward | Better |
|---|---|---|
| 脫緞 | strip satin | get away with the satin / make off with the satin |
| 脫人緞去 | strip me of my satin | cheat me out of my satin |
| 脫豬 | stripped him of a pig | got away with a pig |
| 脫布 | strip cloth | walk off with the cloth |
| 脫茶壺 | stripping a teapot | getting away with the teapot |

**How to apply.** Search `data/book.json` for `strip`, `stripped`, `stripping`. Replace with “make off with,” “walk off with,” “cheat … out of,” or “get away with,” depending on whether the emphasis is theft or deception.

---

### 2.2 棍 — “swindler” is overused and flat

**Problem.** 棍 is almost always “swindler” (56 times). It becomes repetitive and loses the flavor of 光棍 / 棍徒.

**Fix.** Vary by context, but keep it simple.

| Context | Better |
|---|---|
| Generic | the swindler, the con man, the crook |
| 光棍 | a smooth operator, a sharp |
| 棍徒 | a swindler, a trickster |
| 老棍 | an old hand, a veteran con man |
| 雛棍 | a green swindler, a novice con man |

**How to apply.** Do not replace every “swindler.” Keep it when it is the clearest word, but vary with “con man,” “crook,” “sharp,” “trickster” so the text does not drone.

---

### 2.3 按 / 吾觀 — “Note:” is fine, but drop “I observe”

**Problem.** 82 notes start with “Note:”, which is good. But several then say “I observe that…” / “I observe how…”, which is stiff and not idiomatic English.

**Fix.** Keep “Note:” as the marker, then write the comment directly.

| Awkward | Better |
|---|---|
| Note: I observe that swindlers have many tricks. | Note: Swindlers have many tricks. |
| Note: I observe how the swindler stripped him of a pig. | Note: Look how the swindler got away with the pig. |

**How to apply.** Search for `Note: I observe`. Delete “I observe that / I observe how” and start with the point.

---

### 2.4 江湖 — not “rivers and lakes”

**Problem.** 江湖 is rendered literally as “rivers and lakes” (3 times). In English that is meaningless here.

**Fix.** Use “the road,” “the traveling life,” “the trade,” or “the world of traveling merchants.”

| Awkward | Better |
|---|---|
| an old hand of the rivers and lakes | an old hand on the road |
| gentlemen of the rivers and lakes | traveling merchants |
| wandering the rivers and lakes | drifting from place to place |

**How to apply.** Search for `rivers and lakes`. Replace with the phrase that fits the sentence.

---

### 2.5 高手 — not “high hand”

**Problem.** 高手 is rendered “high hand” (2 times). That is not English.

**Fix.** Use “a real master,” “a top hand,” “a real pro,” or “a master of the game.”

| Awkward | Better |
|---|---|
| truly a high hand | a real master |
| this swindler is truly a high hand | this swindler is a real pro |

**How to apply.** Search for `high hand`. Replace.

---

### 2.6 兩 / 銀 — “taels” is fine, but be consistent

**Problem.** 兩 is usually “taels,” which is correct and clear. A few places use “liang” or mix.

**Fix.** Use “taels” for 兩, “qian” for 錢, “cash” for 文 / 錢 when it means copper coins.

| Chinese | Use |
|---|---|
| 四十兩 | forty taels |
| 二錢 | two qian |
| 五六千錢 | five or six thousand cash |

**How to apply.** Search for `liang` and replace with `taels`. Keep `taels` everywhere for 兩.

---

### 2.7 緞 / 布 / 絹 — “satin” vs “silk” vs “cloth”

**Problem.** 緞 is translated “satin,” but in Ming usage it is a heavy silk fabric, not the modern glossy “satin.” 布 is cloth, 絹 is silk.

**Fix.** Use “silk” for 緞 and 絹 when it is the luxury fabric; use “cloth” for 布. Use “bolt” for 匹.

| Chinese | Use |
|---|---|
| 緞 | silk (or silk fabric) |
| 布 | cloth |
| 絹 | silk |
| 一匹 | a bolt |

**How to apply.** Search for `satin`. Replace with `silk` unless the modern word “satin” is clearly intended.

---

### 2.8 回家 / 歸 — “go home” is often too literal

**Problem.** 回家 / 歸 is often “go home,” which is fine, but in travel contexts it sounds flat.

**Fix.** Use “head home,” “go back,” “return home,” or “set off for home” when the journey matters.

| Awkward | Better |
|---|---|
| go home to write a deed | go back home to draw up the deed |
| packed up to go home | packed up and headed home |
| go home to weigh out the silver | go back home to weigh out the silver |

**How to apply.** Search for `go home`. Replace with “head home,” “go back home,” or “return home” when the trip is part of the story.

---

### 2.9 對銀 / 作帳 — “match silver” and “make accounts”

**Problem.** 對銀 is rendered “match silver,” 作帳 “make accounts.” Neither is natural.

**Fix.** Use “weigh out the silver,” “settle the silver,” “check the silver,” or “settle accounts.”

| Awkward | Better |
|---|---|
| match the silver | weigh out the silver / settle the payment |
| making accounts and matching silver | settling accounts and weighing out silver |

**How to apply.** Search for `match silver`, `make accounts`. Replace with “settle the silver,” “weigh out the silver,” “settle accounts.”

---

### 2.10 店 / 鋪 — “wineshop,” “shop,” “inn”

**Problem.** 酒坊 is rendered “wineshop,” which is odd. 店 can be shop, inn, or tavern depending on context.

**Fix.** Use the natural word.

| Chinese | Use |
|---|---|
| 酒坊 | wineshop → tavern / wine shop |
| 店 | shop, inn, or tavern (choose by context) |
| 鋪 | shop |

**How to apply.** Search for `wineshop`. Replace with `tavern` or `wine shop`.

---

### 2.11 里長 / 甲首 / 鄰約 — avoid jargon

**Problem.** 里長 is “lizhang (village head),” 甲首 “jia-head,” 鄰約 “neighborhood compact.” These are accurate but heavy.

**Fix.** Use a short English equivalent with the Chinese in parentheses once, then the English after.

| Chinese | Use |
|---|---|
| 里長 | village head (lizhang) |
| 甲首 | tithing head (jiashou) |
| 鄰約 | neighborhood elders |

**How to apply.** First mention: “village head (lizhang).” After that: “the village head.”

---

### 2.12 官員 titles — keep simple

**Problem.** 府尹, 同知, 推官, 御史 are rendered with full titles like “Acting Vice Prefect.”

**Fix.** Use a simple title and add the Chinese once if needed.

| Chinese | Use |
|---|---|
| 府尹 | the prefect |
| 同知 | the vice prefect |
| 推官 | the judge |
| 御史 | the censor / the inspector |

**How to apply.** Search for `Acting Vice Prefect`, `Vice Prefect`, `Prefect`. Simplify to “the prefect,” “the vice prefect,” “the judge.”

---

### 2.13 按語 tone — keep the moral, drop the stiffness

**Problem.** Some notes sound like a textbook: “A gentleman is kind to people and loves living things.”

**Fix.** Keep the moral point, but write it as a Ming essayist would sound in English: direct, a little wry.

| Awkward | Better |
|---|---|
| A gentleman is kind to people and loves living things; the first to receive kindness should be neighbors; among things to love, even geese are one. | A gentleman is kind to people and cares for living things. Neighbors come first, and even geese count. |
| If he had treated neighbors with kindness and had enough measure to tolerate things, how would he have come to this loss? | If he had treated his neighbor kindly and been a little more tolerant, he would not have lost his cloth. |

**How to apply.** Read each note aloud. If it sounds like a proverb book, loosen it.

---

### 2.14 虛詞 — 矣, 哉, 也, 耳

**Problem.** These are often over-translated as “Alas!”, “indeed,” “that is all.”

**Fix.** Usually omit them. If the tone needs marking, use a short phrase.

| Chinese | Use |
|---|---|
| 矣 | (omit) or “now” |
| 哉 | (omit) or “indeed” |
| 也 | (omit) |
| 耳 | (omit) or “that’s all” |
| 嘻 / 噫 | “Alas” or “Ah” (sparingly) |

**How to apply.** Search for `Alas!`. Keep only where the sigh is real. Otherwise delete.

---

### 2.15 成語 / 典故 — translate the point, not the image

**Problem.** 假道滅虢, 行人得牛, 欲取姑與 are translated literally and become confusing.

**Fix.** Translate the meaning, and if the image is famous, keep it short.

| Chinese | Awkward | Better |
|---|---|---|
| 假道滅虢 | borrow a path to destroy Guo | the “borrow a path to destroy Guo” trick (using one thing as a pretext to take another) |
| 行人得牛 | when a traveler gets an ox | if a passerby picks up an ox |
| 欲取姑與 | give in order to take | give a little to take a lot |

**How to apply.** For each idiom, ask: what is the point? Say that first. Add the literal image only if it helps.

---

### 2.16 自稱 / 佯稱 — “pretended to have diarrhea”

**Problem.** 佯稱瀉病 is rendered “pretended to have diarrhea.” That is clear but blunt.

**Fix.** Use “pretended to be sick,” “pretended to have an upset stomach,” or “faked diarrhea.”

| Awkward | Better |
|---|---|
| pretended to have diarrhea | pretended to be sick / faked an upset stomach |

**How to apply.** Search for `diarrhea`. Replace with a milder phrase unless the bluntness is needed.

---

### 2.17 銀色 / 成色 — “silver quality”

**Problem.** 銀色 / 成色 is rendered “silver quality,” “not of good quality.”

**Fix.** Use “purity,” “fineness,” or “good silver.”

| Awkward | Better |
|---|---|
| the silver was not of good quality | the silver was not pure |
| at this price I need it exchanged for good silver | at this price I need pure silver |

**How to apply.** Search for `not of good quality`, `silver quality`. Replace with “purity,” “fineness,” “pure silver.”

---

### 2.18 小二 / 店主 / 歇家 — “innkeeper”

**Problem.** 店主 is sometimes “shopkeeper,” sometimes “innkeeper.”

**Fix.** Use “innkeeper” when it is a lodging, “shopkeeper” when it is a store.

**How to apply.** Check each 店主. If the story is about lodging, use “innkeeper.”

---

### 2.19 小二 / 僕 — “servant”

**Problem.** 僕 is “servant,” which is fine, but sometimes “boy” or “man” is more natural.

**Fix.** Use “servant” for formal, “man” or “boy” for casual.

---

### 2.20 小二 / 客人 — “customer” vs “guest”

**Problem.** 客人 is sometimes “customer,” sometimes “guest.”

**Fix.** Use “customer” in a shop, “guest” in a lodging or home.

---

## 3. Quick checklist for editors

Before finalizing a chapter, run these searches in `data/book.json`:

- [ ] `strip` / `stripped` / `stripping` → replace with natural verb
- [ ] `Note: I observe` → delete “I observe”
- [ ] `rivers and lakes` → replace
- [ ] `high hand` → replace
- [ ] `liang` → `taels`
- [ ] `satin` → `silk` (if 緞)
- [ ] `go home` → `head home` / `go back home` (if travel)
- [ ] `match silver` / `make accounts` → `settle the silver` / `settle accounts`
- [ ] `wineshop` → `tavern` / `wine shop`
- [ ] `lizhang` / `jia-head` → `village head` / `tithing head`
- [ ] `Acting Vice Prefect` → `the vice prefect`
- [ ] `Alas!` → keep only if the sigh is real
- [ ] `diarrhea` → `upset stomach` / `sick`
- [ ] `not of good quality` → `not pure` / `impure`

---

## 4. How to apply this guide

1. **Edit `data/book.json` directly.** The HTML is generated from it.
2. **Rebuild after editing:**
   ```bash
   cd /home/edykw/pian-jing-bilingual
   python3 scripts/build_html.py
   ```
3. **If you re-translate a chapter**, update the matching file in `data/en/`, then run:
   ```bash
   python3 scripts/merge_en.py
   python3 scripts/build_html.py
   ```

---

## 5. What not to change

- Do not copy Christopher Rea / Bruce Rusk / Columbia University Press wording.
- Do not modernize the morals into slang. Keep them plain and direct.
- Do not add plot or soften the crimes. The original condemns them; the English should too.
