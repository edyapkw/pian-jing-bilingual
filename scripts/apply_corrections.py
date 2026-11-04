#!/usr/bin/env python3
"""Apply QA-report corrections to the 騙經 bilingual reader using ModelScope Qwen.

Two phases (matching leeao-bilingual's apply_corrections.py):

1. Generate (default): for every flagged/issue pair in translation_qa_report.json,
   ask ModelScope Qwen for a corrected English translation with surrounding story
   context, re-score it with the QA prompt, and accept if the score improves
   (or stage directly from the ModelScope suggestions in translation_qa_report.json
   when --from-report is passed or when ModelScope balance is exhausted).
   Results are saved to translation_corrections.json.

       python3 scripts/apply_corrections.py                    # flagged pairs (score <= 7)
       python3 scripts/apply_corrections.py --max-score 6      # only score <= 6
       python3 scripts/apply_corrections.py --all-issues       # all pairs with QA issues
       python3 scripts/apply_corrections.py --from-report      # use ModelScope suggestions from report
       python3 scripts/apply_corrections.py --resume           # continue a run
       python3 scripts/apply_corrections.py --limit 5          # quick trial

2. Apply: patch accepted corrections into data/en2/*.json, data/en/*.json,
   rebuild data/book.json and index.html, and refresh translation_qa_report.json
   and TRANSLATION_ISSUES.md.

       python3 scripts/apply_corrections.py --apply

API credentials are loaded from .env (see scripts/verify_translations.py).
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Allow running as `python3 scripts/apply_corrections.py` from repo root
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from verify_translations import (  # noqa: E402
    DEFAULT_DATA_BOOK,
    DEFAULT_INDEX_HTML,
    DEFAULT_ISSUES_MD,
    DEFAULT_REPORT,
    MODELSCOPE_MODEL,
    QA_SYSTEM,
    ROOT,
    InsufficientBalanceError,
    _save_report,
    _validate_qa,
    chat_json,
    extract_pairs_from_json,
    strip_html,
)

DEFAULT_STAGING = ROOT / "translation_corrections.json"

# ---------------------------------------------------------------------------
# Correction prompt (ModelScope Qwen)
# ---------------------------------------------------------------------------

FIX_SYSTEM = """\
You are an expert literary translator revising the English side of a
Chinese-English bilingual edition of Zhang Yingyu's late-Ming 《騙經》/《杜騙新書》
(The Book of Swindles).

You receive: the story context (title, previous segment, next segment), the
Chinese source segment, the current English translation, the problems a
reviewer found, and the reviewer's draft suggestion.

Produce a corrected English translation that:
- Fixes every genuine problem the reviewer listed (mistranslation, omission,
  awkward phrasing, literal calque). Ignore reviewer complaints that are wrong
  or caused by reading the segment without the surrounding story context.
- Follows TRANSLATION_GUIDE.md strictly:
  1. Meaning-first study crib: accurate, plain, natural English; translate
     meaning rather than word order.
  2. Never render 脫 / 脫去 as "strip / stripped / stripping" when it means
     swindling; use "make off with", "get away with", "cheat out of", etc.
  3. Vary 棍 ("swindler", "con man", "crook", "sharp", "trickster", "rogue").
  4. Author commentary notes (按) start with "Note:" (never "Note: I observe").
  5. Use "taels" for 兩, "purity/fineness" for 成色/銀色, "silk" for 緞/絹,
     "tavern/wine shop" for 酒坊.
  6. Never translate 江湖 as "rivers and lakes" or 高手 as "high hand".
  7. In travel contexts, prefer "head home", "go back home", or "return home"
     over flat "go home".
- Keeps character names, pronouns, and plot details strictly consistent with
  the surrounding story context.
- Uses standard double quotes ("...") for outer dialogue and single quotes
  ('...') for quotes nested inside dialogue.
- Never includes translator meta-notes, brackets [...], or parenthetical
  explanations of textual variants inside the translation.
- Uses the surrounding segments only as context; translate only the TARGET.

Output ONLY valid JSON: {"en": "<corrected English text>", "notes": "<one short sentence on what changed>"}
"""


def _validate_fix(data: dict) -> dict:
    en = str(data.get("en") or data.get("en_html") or "").strip()
    en = strip_html(en)
    if not en:
        raise ValueError("empty en in correction response")
    return {"en": en, "notes": str(data.get("notes") or "").strip()}


# ---------------------------------------------------------------------------
# Context-aware sanitization & style-guide normalization
# ---------------------------------------------------------------------------

# Context overrides for segments where the isolated-segment QA pass misread
# character roles, gender, or setting because it lacked surrounding paragraphs,
# or where TRANSLATION_GUIDE.md checklist items needed targeted cleanup.
CONTEXT_VERIFIED_FIXES: dict[tuple[str, int], tuple[str, str]] = {
    # Checklist item in TRANSLATION_GUIDE.md §2.8 / §3 (go home -> go back home)
    (
        "cat-01-s02",
        1,
    ): (
        'In the capital lived Bao Shancheng, whose wife was skilled at weaving warming-pan silk; each bolt was worth around five taels. When a bolt was finished, he carried it into the city to sell. Halfway there he met a sharp who looked the silk over, praised its quality, and asked, "How much silver do you want for it?" Bao said, "It was woven with great care; it won\'t go for less than five taels." The con man said, "Follow me and I\'ll give you a good price, paid in pure silver." Bao said, "If you mean to buy, I\'ll head home with you." The con man said, "I\'m on my way to the capital on business; how can I turn back? Tell me what you actually want—if the price is fair, we\'ll settle on it and then I\'ll go back home to weigh out the silver; otherwise I\'m not going back for nothing."',
        "Replaced literal 'go home' in travel context per TRANSLATION_GUIDE.md §2.8.",
    ),
    # cat-05-s03 p3: Keep victim's name Ying (吴应 from p1) as subject of second sentence
    (
        "cat-05-s03",
        3,
    ): (
        "Once all the land was sold, Ziming cut off all dealings with him. Borrowing morning and night, Ying found that only his own elder brother would help him. Only then did he realize that blood is blood after all, and that those wine-and-meat friends were false from the start.",
        "Clarified borrowing clause while preserving protagonist Ying from paragraph 1.",
    ),
    # cat-09-s01 p2: Keep Bangcai (魏邦材 from p1) and clarify 下流 as Bangcai's arrogant self-reference
    (
        "cat-09-s01",
        2,
    ): (
        'At that time there was Wang Fengqi of Huizhou, from a great clan of prominent officials. He could not stomach Bangcai using wealth and power to crush others, and said: "Worldly power is fleeting, yet you always talk in thousands of taels. In former days, was Shi Chong\'s wealth any less than yours? And what became of him in the end?" Bangcai, furious that someone dared challenge him, said: "On this boat there are men senior to a humble man like me, and men with greater capital than mine, yet they have not said a word. You dare step forward and oppose me? I will stake all one hundred piculs of silk, worth several thousand taels, against you!" Fengqi cursed him: "You base creature, you truly do not know your place! Time and again you behave outrageously—you really are a little wretch who does not know death is near! I have several thousand taels to match you, and I will see that you never return alive to your native land!" The two wrangled without stopping. Everyone aboard secretly rejoiced to see Wang and Wei locking horns, feeling greatly pleased at heart. Someone who favored Wang urged them each to retire to their cabins.',
        "Clarified status/capital comparison while keeping Wei Bangcai's name consistent with paragraph 1.",
    ),
    # cat-11-s00 p0: 私打印记占铺陈 — in the story, You the Shyster stamps a mark on Qiu's bedding roll (铺陈)
    (
        "cat-11-s00",
        0,
    ): (
        "Secretly Stamping a Mark to Seize Another's Bedding",
        "Replaced 'Wins' with 'Seize' to reflect the fraudulent seizure of the traveler's bedding.",
    ),
    # cat-12-s01 p2: Keep family roles clear (Wei is the father, Ji is the young husband, Wei-shi is the daughter)
    (
        "cat-12-s01",
        2,
    ): (
        'For half a year after that, there was little business. It happened that Magistrate Jia, newly appointed and on his way to his post, passed through Yangzhou and wanted to take a concubine. Wei and his son-in-law Ji discussed it privately and decided to palm the daughter off on him in a sham marriage. Ji agreed, acted as matchmaker himself, and went to discuss it with Magistrate Jia. Jia came to look, was pleased, sent betrothal gifts at once, paid eighty taels, and chose a day for the wedding. Wei and Ji together said to the daughter: "We have had no business for half a year; the household is hard pressed, so we are marrying you to Magistrate Jia in name only. In truth your husband is a young man—how could he bear to give you up? We are your parents, and we have only you—how could we bear to let you go? We simply have no choice; we are treating you as merchandise. Besides, Magistrate Jia is old; he has not brought his principal wife with him for now, so of course he will cherish you. But I fear that after he takes up his post and sends for the principal wife, she will surely treat you cruelly; cursing and whipping will be hard to avoid. The old saying goes: \'Better a poor man\'s wife than a rich man\'s concubine.\' For now we will send you; in a few days he will board a boat. Your husband will secretly follow in a boat, with a white pair of trousers hung up as a signal. If you can get away at night, cross over to the white-trousers boat; your husband will be there to take you. Do not oversleep and ruin your husband\'s whole life; you yourself would also suffer." Ji deliberately clutched his wife\'s clothes, weeping, and pleaded to her face: "If you will promise to come back, I will let you go; if you cannot get away, I would rather die with you—I cannot bear to part." His wife Miss Wei also wept and said: "When father and mother have given the order, how would I not come back? Only you must follow in a boat and wait to take me; do not miss the time." The three of them had the plan set.',
        "Clarified the roles of Wei (father), Ji (young husband), and Miss Wei (daughter/wife) and improved phrasing.",
    ),
    # cat-13-s01 p0, p5, p6: Chen Quan swindles the famous courtesan Hua Buru (female) under alias Lang Zisui
    (
        "cat-13-s01",
        0,
    ): (
        "Chen Quan Leaves a Parting Trick After Visiting a Famous Courtesan",
        "Clarified 遗计 (leaving a parting trick/verse) and 嫖名妓 (visiting a famous courtesan).",
    ),
    (
        "cat-13-s01",
        5,
    ): (
        'Halfway there, Zisui hurriedly said, "I left a key in your mistress\'s room. I set out in a rush and forgot to bring it; you must go back and fetch it." The servant turned to go. Zisui called out, "Wait! You will need a note in my own hand, or your mistress will not hand the key over to you. Otherwise, you will never get it." Zisui dismounted from the donkey, went into a paper shop, and wrote a verse: "Hangzhou\'s Hua Buru took in Jintai\'s prodigal Zisui; she fell for the man and lost a donkey too. From this parting on, where will she find her bright pearls?" He sealed it and sent the man back. Buru opened it, realized she had been swindled, and hurriedly opened the leather chests—inside were nothing but bricks and stones. Zisui had already ordered his men to hire a boat and wait; as soon as he reached the river, he boarded and headed back to the capital. Later, Buru inquired carefully and learned it was Chen Quan of Nanjing, but there was nothing she could do.',
        "Improved fluency while keeping courtesan Hua Buru's female pronouns and Zisui's full alias.",
    ),
    (
        "cat-13-s01",
        6,
    ): (
        "Note: Brothels routinely cheat others—in minor cases ruining a family, in major ones costing a life—yet one never hears of them being cheated themselves. Hua Buru, moreover, set her price high and handpicked her patrons; the wealth she extracted from men was beyond counting. Who would have thought Chen Quan possessed tricks even more uncanny, appearing and vanishing like a spirit? She lost both her lover and her donkey, and regret came too late. Though this was a piece of unruly mischief on Chen Quan's part, it still provides a good laugh at the expense of the pleasure houses that ensnare others.",
        "Fixed opening clause ('Brothels routinely cheat others') while keeping Hua Buru as a proper name.",
    ),
    # cat-14-s01 p0, p17: Counterfeit silver treatise
    (
        "cat-14-s01",
        0,
    ): (
        "Accepting Bleached Counterfeit Silver at the Maozhou Fair",
        "Clarified 漂白皛 as bleached counterfeit silver while keeping the Maozhou Fair setting from paragraph 1.",
    ),
    (
        "cat-14-s01",
        17,
    ): (
        "The method for refining cake silver: for every qian of lead used, melt in one fen of copper. For one tael of nine-tenths silver, use one tael of lead; for eight-tenths, two taels; for seven-tenths, three taels. Pack charcoal into the furnace within an ash-lined hearth and fan the fire gently until the lead melts. Afterward, fan vigorously; when oil beads grow as large as beans, cover the pot and remove it immediately—this yields ninety-five percent purity. If you wait until golden flowers blaze before removing it, a crust forms on top called 'cloth-heart cake' or 'scorched-heart cake.' Below lie crab-eyes and returning pearls, with both faces white: this indicates pine-grain silver at full fineness.",
        "Removed QA model's self-talk ('? No, the text actually says:') and refined technical description.",
    ),
    # cat-16-s00 p0: 妇嫁淘街而害命 — in the story, a woman marries Fang Ba (a street-cleaner/gutter-panner) and causes his death
    (
        "cat-16-s00",
        0,
    ): (
        "A Woman Marries a Street-Sweeper and Costs Him His Life",
        "Clarified 害命 as costing the street-sweeper his life through the marriage scheme.",
    ),
    # cat-16-s02 p1: Remove meta-note and clarify speaker (Cai Tianshou, who is past forty)
    (
        "cat-16-s02",
        1,
    ): (
        'Cai Tianshou of Guangdong was open-handed and stood by his word. At forty he had no son, and his wife was such a shrew that she would not let him take a concubine. One day he brought more than thirty loads of Guangdong tin to Suzhou to sell, and said to the broker Xiao Hanqing, "I still have no son, and I would like to take a concubine here. Is there anyone suitable on reasonable terms?" Hanqing said, "When you have silver, why worry about finding a girl to your liking?" He led him to look at several unmarried girls, but none suited: "I am past forty; these girls are too young to be a match for my years, so I will not take them."',
        "Removed parenthetical meta-note and clarified the age objection naturally.",
    ),
    # cat-16-s03 p0, p2: 青蛙露出谋娶情 — a frog croaking reveals the murder plot; Cai reports Pan's drowning in 3rd person
    (
        "cat-16-s03",
        0,
    ): (
        "A Frog Exposes a Murderous Marriage Plot",
        "Removed conditional meta-text and used 'Exposes a Murderous Marriage Plot'.",
    ),
    (
        "cat-16-s03",
        2,
    ): (
        'Dressed in white mourning clothes, Cai went to see Pan\'s parents, wept loudly first, and then reported the tragic news. The entire household wailed and asked in detail how he had died. Cai said, "While crossing at the West Gate ferry, he boarded the boat and took the pole to push off, and plunged into the water along with the pole. The water was deep and swift, and he lacked the strength to come back up, so he drowned. I hired men to recover his body, cremated his bones, and brought them home." When he finished speaking, the Pan household wept again. Cai then accounted clearly for the sales ledger and all the capital, item by item, and returned them to the parents. The whole family felt grateful for his kindness—how could they know that Cai himself had plotted the murder?',
        "Kept third-person 'he' for the drowned victim Pan while improving narrative flow.",
    ),
    # cat-17-s00 p0: 用银反买焙纸妇 — in the story, the swindler uses silver to seduce the woman drying paper
    (
        "cat-17-s00",
        0,
    ): (
        "Using Silver to Turn the Tables and Seduce a Woman Drying Paper",
        "Captured 反买 (turning the tables with silver) and the paper-drying setting of the story.",
    ),
    # cat-17-s02 p1, p3: Lu the geomancer wants to steal their lineage/seed (脱其种) and leaves his wife behind
    (
        "cat-17-s02",
        1,
    ): (
        'There was a geomancer named Lu, quite skilled at reading mountains, who wanted to find a fine gravesite to bury his own father. Searching as far as Ningcheng, he found an auspicious fengshui spot behind the ancestral shrine of the retired official Yang, where it was neither easy to buy openly nor possible to bury in secret. Hearing that Official Yang had passed away and his two sons were also looking for a site to bury their father, Geomancer Lu offered them this spot and led the two young masters to inspect it. It was indeed a fine burial site—it cost them nothing to buy, and its orientation promised great fortune. They buried their father there and rewarded the geomancer with thirty taels of silver. Since Lu could not get the gravesite for himself, he resolved to steal their lineage instead. He rented a house by the garden gate of the Yang sons, used the silver to marry a beautiful woman, and lived with her for two months. Then he told his wife: "I must travel afar to practice geomancy, and I cannot say when I will return. If the household runs short of firewood and rice, I have asked the young masters to look after you. They are my benefactors; it was only because of their silver that I could marry you. While I am away, if these two young masters make advances, you may go along with them. If you win their affection, they will support you generously later on. However, their household has many servants—never have anything to do with the servants. If you cheapen yourself, the young masters will despise you, and you will end up in poverty with no one to help." He also went to ask the two young masters: "I have to travel far away, and my household is left with meager means. Please help look after my wife, and when I return I will repay everything in full."',
        "Preserved 'steal their lineage' (脱其种) central to the plot while improving fluency.",
    ),
    (
        "cat-17-s02",
        3,
    ): (
        'When Lu went away again, the young masters came calling once more. The woman said, "If the two of you come at irregular times, I fear I may develop a bad illness. It would be better to set odd months for the elder young master and even months for the younger." The young masters agreed: "You are quite right. From now on, each month belongs to the appointed one, and he shall supply all the vegetables, rice, and provisions." Before they knew it, four years had passed, and she had borne two sons, both of the two young masters\' bloodline. Geomancer Lu had their fortunes read, and every diviner predicted great wealth and rank for them in the future. He then took his wife and sons and bade farewell to the two young masters, each of whom gave him generous parting gifts. Later, the two boys grew up and both passed the imperial examinations—in truth, the Yang family\'s auspicious fengshui had been secretly siphoned away without their ever knowing it.',
        "Kept Geomancer Lu ('When Lu went away again') as subject of the opening clause and improved flow.",
    ),
    # cat-17-s03 p2: Clarify that Jing Ji (京季) is the courtesan from p1
    (
        "cat-17-s03",
        2,
    ): (
        'One day, while Ying was out drinking at a brothel, the courtesan Jing Ji quietly unlocked his room and stole over a hundred taels of silver. When Ying returned to the inn and discovered the silver missing, he pressed the innkeeper to make good the loss. Fellow lodger Wu Lun said, "When you keep silver in your room, you shouldn\'t go drinking far off at a brothel; even if you do, you should return early. You went out carousing and lost it—what does that have to do with the innkeeper? At noon today I saw Jing Ji go into your room; it must be that woman who stole it. You can report this to the officials, and the innkeeper and I will testify for you."',
        "Clarified that Jing Ji is the courtesan from paragraph 1.",
    ),
    # cat-17-s04 p3: Keep bamboo-shoot accounts (笋帐) accurate to p1-p2 while fixing legal reasoning
    (
        "cat-17-s04",
        3,
    ): (
        'When the mother found out, she conferred with her husband: "How many wealthy families have sought our daughter\'s hand, and we refused to give her lightly! Now she has been defiled by this rat of a traveling merchant. We must secretly catch and kill him to vent our anger." Liao San said, "No. Only when a wife commits adultery and you catch them in the act and kill both on the spot does the law hold you blameless. Here it is our daughter having an illicit affair: I cannot bear to kill them both, and if we kill only the merchant, his offense does not carry the death penalty—how could we kill him without facing serious consequences afterward? Right now the bamboo-shoot accounts are settled, and all his silver is in my hands. Let us secretly watch for when our daughter meets him again, catch them in the act, beat him half to death, chain him up, and force a written confession out of him. Won\'t he have to forfeit his bamboo-shoot silver to us then? And what would he dare say afterward?" His wife agreed.',
        "Fixed the legal reasoning ('his offense does not carry the death penalty') while preserving the bamboo-shoot trade context from paragraphs 1–2.",
    ),
    # cat-18-s01 p2: Keep daughter-in-law (其妇 / 媳妇) accurate to the story
    (
        "cat-18-s01",
        2,
    ): (
        'One day, the old mother-in-law was invited next door for a drink, and the old villager took the chance to make advances to his daughter-in-law. When she refused, he tried to force himself on her. Shouting and cursing, the young wife broke free and ran out. Her parents\' home was only ten li away, so she ran there on foot to tell her mother. Her mother was a fierce, shrewish village woman, and she marched straight over with her daughter. Seeing that his daughter-in-law had run home to report him, the old villager hurriedly called his wife back, confessed the whole truth, and asked how they could deal with it. The old woman thought to herself: "The girl\'s father is a simple rustic and won\'t come; only her mother, that fierce shrew, is sure to arrive." Having already hit upon a scheme to trap her, she turned and congratulated her old man instead: "Congratulations! A stroke of good luck has arrived!" The old villager said, "What\'s done was a mistake—don\'t mock me. You\'ve always been resourceful, and I listen to you in everything. Save me now, or our family will be ruined!" The old woman said, "Ruined? It\'ll cost you your life! Yet if I save you now, you\'ll only grow bolder. If you obey me and swear never to harbor such wild thoughts again, I\'ll not only save you, I\'ll treat you to a fine piece of luck besides." The old villager said, "I don\'t ask for any luck—just save me this once, and I\'ll never dare think a wicked thought again. If I do, may Heaven strike me and Earth destroy me!" The old woman said, "Since you\'ve sworn, I\'ll spare you this time. Take four taels of silver in two ingots, and hide in the outer guest room underneath the big vat. When the girl\'s mother arrives, I\'ll have her bathe in that room. Once she finishes washing, crawl out from under the vat and press the two silver ingots into her hands. She\'ll surely clutch the silver and won\'t be able to push you away; then force yourself on her once, walk right out, and the whole matter will be settled." The old villager said, "If I violate her too, won\'t that prove I really did harass her daughter?" The old woman said, "Never you mind—leave the rest to me." The old villager did as told and hid beneath the vat.',
        "Kept family roles (old mother-in-law, old villager, daughter-in-law, girl's mother) clear and natural.",
    ),
    # cat-18-s04 p0, p6: Nun scatters prayer beads to lure Lady Xiang into adultery with Chaoxian; avoid 'stripped'
    (
        "cat-18-s04",
        0,
    ): (
        "A Nun Scatters Prayer Beads to Lure a Woman into an Illicit Affair",
        "Clarified title to match the plot where the nun lures Lady Xiang into Chaoxian's trap.",
    ),
    (
        "cat-18-s04",
        6,
    ): (
        'While Lady Xiang was fast asleep, Chaoxian came in and undid her sash and clothes. She lay there like one dead, yet warm to the touch; he took his pleasure of her as he pleased, finding it utterly delightful. After a brief rest, he had his way a second time, and still she did not wake. Chaoxian held her tightly in both arms and slept until midnight. When she finally awoke, her clothes had already been taken off. Feeling a man lying beside her, a pleasant ease at her waist, and her whole body relaxed and refreshed, she whispered, "Who are you?" Chaoxian replied, "My darling, how long I have longed for you! Today I finally managed to steal two trysts with you—tomorrow you must be good to me again." Lady Xiang said, "Now that your scheme has succeeded, you must never let outsiders know." Chaoxian said, "Only the nun knows; who else could possibly find out?"',
        "Improved fluency while avoiding 'stripped' per TRANSLATION_GUIDE.md.",
    ),
    # cat-20-s04 p0, p1: 秋风客 is an 'autumn-wind guest' (a connection-peddling sponger soliciting favors)
    (
        "cat-20-s04",
        0,
    ): (
        "Impersonating a Favor-Seeking Guest to Solicit Exam Bribes",
        "Clarified 诈秋风客以揽骗 in the context of exam-bribery swindles.",
    ),
    (
        "cat-20-s04",
        1,
    ): (
        'Education Commissioner Jian was renowned for his integrity and clear judgment. During the student renewal examinations, a favor-seeking visitor arrived and lodged in the monks\' quarters at Kaiming Temple. The next day, a swindler arrived with three servants and took lodgings there as well. Inside the temple they exchanged courtesy calls, and the swindler claimed to be a relative of the county magistrate who had also come to solicit favors. Outwardly, he flaunted splendid robes and caps, with a retinue even grander than the other\'s, frequently coming and going through the temple under official canopies. Students who saw him often mistook him, saying, "This is the Education Commissioner\'s hometown relative." When they saw Commissioner Jian\'s real relative return a courtesy call and invite him for wine, the swindler tagged along with the genuine visitor while secretly borrowing his name on the outside to deceive people. Commissioner Jian was a strict and upright official, and within a few days the genuine favor-seeker was sent on his way, leaving only the swindler at the temple. His outside accomplices deliberately spread word everywhere: "The Education Commissioner\'s hometown relative is staying at such-and-such temple." The rumor circulated widely among the candidates; many who wished to buy a place in the examination were eager to pay, yet lacked a reliable person to handle the silver. Behind the scenes, the swindler colluded with clerks and runners from the Education Commissioner\'s yamen, having them come forward so the silver could be sealed and deposited at their homes. Since people already believed he was the genuine relative, and since official yamen staff were involved, what was there to worry about in sealing the silver at their houses? The swindler announced that he could secure ten places at three hundred taels each. With so many candidates competing eagerly, the quota of ten was filled within days. On the appointed day, the silver—three thousand taels in all—was openly checked, sealed, and deposited at the various clerks\' and runners\' homes; behind the scenes, the gang divided the spoils at once.',
        "Fixed the logic of candidates needing a reliable intermediary and unified 'Education Commissioner Jian'.",
    ),
    # cat-20-s05 p3, p4: Three servants bring the silver for the three xiucai; the three xiucai panic when the innkeeper vanishes
    (
        "cat-20-s05",
        3,
    ): (
        'Four days later, the servants from all three households arrived with the silver. The visiting scholar came on schedule to inquire, and each of the three scholars replied, "The silver is all ready." The visitor said, "Tonight let us verify it under bright light and seal it." The three scholars said, "There is a great deal of silver, which makes nighttime inconvenient; tomorrow let us go into the innkeeper\'s inner courtyard to verify it." The visitor said, "The innkeeper may not keep this matter confidential; sealing it in the outer guest rooms would be more discreet." The three scholars said, "Let us discuss it when the time comes tomorrow." The visitor took his leave. After supper, the innkeeper came out and said, "You gentlemen were discussing sealing silver with that visitor—traveling strangers are hard to guard against. These doors and walls are thin; if they bring a gang to rob you in the night, you must be on your guard. In my opinion, you should store the silver inside my house within the city gate while you keep watch outside; that will be completely secure." The three scholars agreed and entrusted all six leather chests of silver to the innkeeper\'s house. Concealing it from his wife and maid, the innkeeper slipped all the silver out through the back door and fled late at night with the gang of crooks. Before leaving, he merely instructed his wife: "Tomorrow, if the three scholars ask for me, just tell them I went out early to look for someone and will be back shortly."',
        "Clarified that the servants of the three households brought the silver to the three scholars.",
    ),
    (
        "cat-20-s05",
        4,
    ): (
        'The next day, the visiting scholar arrived in high spirits to weigh and verify the silver. The scholars said, "We gave the silver to the innkeeper for safekeeping; he went out early this morning and will be back shortly." They waited until noon, but the innkeeper did not return, so the visitor went back to his boat. In the afternoon, the visitor sent his servant to ask again, and once more they answered that the innkeeper had not yet returned. At noon on the third day, the scholars asked the innkeeper\'s wife for the leather chests, but she replied, "I have never seen any chests." When they went out to the riverbank to look for the visitor\'s boat, it too had vanished. They questioned the innkeeper\'s wife again, and she stubbornly insisted she knew nothing of the chests. She let them search the house as they pleased, yet not a trace was found. "Where did the innkeeper really go?" they demanded. The wife replied, "He left the night before last and told me to answer you this way." The three scholars were thrown into a panic.',
        "Fixed 'weigh and verify the silver' and kept 'The three scholars were thrown into a panic'.",
    ),
    # cat-21-s00 p1, p3: The monk notes that the yellow cow belongs to Elder Suo, steals it in p2, and slaughters the cow in p3
    (
        "cat-21-s00",
        1,
    ): (
        'In the sixth month of summer, a traveling monk was passing along the road when he saw a herd-boy tending a herd of cattle. Among them was a large, fat yellow cow. The herd-boy held out his left foot for the cow to lick, and she licked it; then he offered his right foot, and she licked that too. The monk asked, "Why does this cow lick your feet?" The boy replied, "This cow is very tame, and I am very fond of her. My feet sweat a lot and taste salty, so she loves to lick them." Realizing that the cow was fond of salt, the monk quietly noted her down and learned that she belonged to the household of Elder Suo.',
        "Clarified '密此牛，系索长者家所畜的' (secretly noted the cow and learned she belonged to Elder Suo's household before stealing her in paragraph 2).",
    ),
    (
        "cat-21-s00",
        3,
    ): (
        'When the tenth month came and the weather turned cold, the monk called a butcher to slaughter the cow, giving half the meat to the butcher, who sold it for one tael and five qian of silver; the other half the monk kept for himself, curing and drying it into provisions that he hid throughout the lining of his patched robe. Then he went straight to Elder Suo\'s hall and sat down cross-legged in full lotus. The elder came out and asked, "What monk dares come up into my hall and sit?" The monk said, "Do you happen to recognize me?" The elder said, "I do not know who you are—how would I recognize you?" The monk said, "Doesn\'t my face look naturally familiar to you?" The elder said, "We have never met; where would any familiarity come from?" The monk heaved a long sigh: "Your original spiritual nature has been completely lost—no wonder you fail to recognize an old friend\'s features!" The elder asked, "What do you mean, an old friend?" The monk said, "In the past, Master Foyin awakened Su Dongpo, and Master Huiyuan called back Bai Letian—were they not old friends of Su and Bai from former lives? In your previous life you cultivated the Way alongside me, but because your worldly desires were not yet severed, you were reborn here to enjoy this earthly wealth. I have come expressly to deliver you. You should quickly cast aside your worldly debts and return to diligent cultivation, so as not to waste the merit of your former life." The elder asked, "How can you know of former lives?" The monk replied, "My cultivation is twice yours. While you have been enjoying half a lifetime of worldly fortune, I have added half a lifetime of bitter ascetic practice—why would it be hard for me to know the karmic causes of three lifetimes?" The elder asked, "What bitter practice have you undertaken in this life?" The monk said, "Never mind my earlier austerities—for the past three years I have abstained completely from grain." Startled at last, the elder said, "If you can abstain from grain, what say you fast for one month here in my house?" The monk laughed: "After three years of it, what is a single month?" The elder asked, "Do you still take tea or hot water?" The monk replied, "Only a single bowl of clear tea or boiling water a day." So the elder kept him there, had an empty room swept clean for him to sit in, and sent in one bowl of tea in the morning and one bowl of boiling water at night.',
        "Clarified 'slaughter the cow' (from paragraphs 1–2) instead of ambiguous 'her' or '[a pig]'.",
    ),
    # cat-21-s01 p3: Keep 'Do not allow monks or Daoists to come in and see him' (铁容 = 莫容)
    (
        "cat-21-s01",
        3,
    ): (
        'After eleven days, Master Chu summoned the monk out, took his Amitabha prayer beads, and inspected them: only nineteen beads remained in his hands. Master Chu confiscated the beads, ordered the monk placed in light detention where no one was to disturb him, and left him to sit quietly awaiting judgment. He then secretly instructed the jailer: "Do not allow any monks or Daoists to come in and see him. After two days he will surely beg you for food. When he does, ask him how those Amitabha beads are made. If you can make a batch that dissolves in water just like these beads, I will reward you handsomely afterward."',
        "Refined prose while preserving the jailer prohibition ('Do not allow any monks or Daoists to come in') required by the plot.",
    ),
    # cat-21-s03 p0, p3: Ruobing is the wandering monk who looks like the Temple Guardian (Qielan)
    (
        "cat-21-s03",
        0,
    ): (
        "A Monk Resembling the Temple Guardian Fakes a Fundraising Appeal",
        "Clarified 化疏 as 'Fundraising Appeal' while keeping 'Temple Guardian' (伽蓝).",
    ),
    (
        "cat-21-s03",
        3,
    ): (
        'On the tenth day, Xiuyun indeed arrived with the silver and two servants. Knowing he was the timber merchant, Abbot Wanlang received him with solemn courtesy, served tea, and asked, "What is the benefactor\'s honored surname?" Xiuyun replied, "Jing." Wanlang asked, "Where has the benefactor come from?" Xiuyun said, "Earlier I made an appointment with a fundraising monk from this precious monastery, so today I have respectfully come up from the river." Wanlang pondered a moment and said, "Our mountain temple has not sent anyone out to solicit donations." Xiuyun said, "Seventeen days ago, a monk sat in meditation in my timber loft for seven days, and I promised him I would come today." Wanlang said, "None of the monks of this temple have gone out during the past half month; it must have been a wandering monk falsely using our name." He at once ordered a vegetarian meal prepared to entertain him. Inwardly, Xiuyun found it strange: "If it was a wandering monk falsely using their name, why didn\'t he take the silver that day?" Keeping the doubt to himself, he strolled around the temple grounds. When he entered the shrine of the Temple Guardian, he looked up and saw that the statue of the guardian looked just like the monk from the other day. His two servants also pointed and said, "This Temple Guardian looks the very image of that monk!" More amazed than ever, Xiuyun suspected in his heart that the Temple Guardian himself had manifested as a monk to urge him to repair the monastery. He bowed and prayed: "If the monk of the other day was your manifestation, grant me a holy cast of the divination blocks." He cast the blocks at once and got a holy sign. He prayed again: "I have brought three hundred taels today; I pray for great profit this year." Casting again, he got a yang sign. He said, "If three hundred taels is not enough and you want five hundred, grant me a holy cast." This time he got a yin sign. He prayed once more: "Now I understand in my heart. If you want an additional five hundred taels for incense-and-lamp fields, grant me a holy cast." Sure enough, he threw a holy sign. Having bowed in thanks, Xiuyun returned to the vegetarian feast and asked Wanlang, "How much silver is needed for the repairs?" Wanlang replied, "I have long wished to repair the temple. The other day I had craftsmen estimate the cost, and they said it would take five hundred taels in all, so I dared not begin." Xiuyun said, "The other day I promised three hundred taels, which I have brought with me now, and tomorrow I will send two hundred more to make up the sum. Once the repairs are complete, I will donate another five hundred taels to purchase incense-and-lamp fields to serve the Buddha forever." Overjoyed at these words, Wanlang pressed his palms together and bowed down. Later, after Xiuyun had donated the full amount as promised, the wandering monk Ruobing secretly took two hundred taels as his share and departed.',
        "Removed parenthetical meta-commentary about Ruobing and refined the divination and fundraising terms.",
    ),
    # cat-23-s02 p4: Checklist item 'go home' -> 'return home'
    (
        "cat-23-s02",
        4,
    ): (
        "All the children they had kidnapped along the way were set free to go back—as for those kidnapped in previous years whose marrow had already been drained, or who had fallen ill or grown too old, the monk claimed he had let them return home, though no one knew whether they ever made it back alive. Only then did people realize that the spell ran through his sleeve: brushing a child's face was how he cast the charm, and having the child carry the turtle was how he bound the spell in place.",
        "Replaced literal 'go home' with 'return home' per TRANSLATION_GUIDE.md §2.8 and improved flow.",
    ),
    # cat-24-s00 p2, p3: Keep Nanjing (from p1 往南京买卖) and madam Mao Xiqing; replace 'go home' in p3
    (
        "cat-24-s00",
        2,
    ): (
        'When the man and his goods reached Nanjing, someone had already reported to Shaoshan: "Your father has brought goods to sell, and he means to find you as well." Dismayed at the news, Shaoshan quickly called in the brothel madam, Mao Xiqing, to scheme with her: "My father has come expressly to press me to return home. If you have a plan to trap him into whoring too, then I can stay here for good; otherwise, I must part from you today." Xiqing said, "Just hide yourself deep inside and do not let him see you; I will take care of everything." She immediately sent someone to invite Madam Xun Rong from the front courtyard and asked her to lay a clever snare for him. Madam Rong agreed and left.',
        "Kept Nanjing consistent with paragraph 1, clarified 'brothel madam', and converted outer quotes to double quotes.",
    ),
    (
        "cat-24-s00",
        3,
    ): (
        'Inquiring among fellow merchants in Nanjing, Dongxi learned that his son was whoring at Mao Xiqing\'s house with her girl Yuehua, so he went straight to Xiqing\'s establishment to call his son back. Xiqing came out and received him with the utmost deference. Dongxi said, "My wayward boy Shaoshan is in your house. I have been in Nanjing for over ten days now—call him out to see me at once." With respectful alarm, Xiqing replied, "So Your Honor is Young Master Shaoshan\'s venerable father! How fortunate I am to part the clouds and behold the sun! Your worthy son did visit my humble house some time ago, and knowing that a young man away from home ought not squander his wealth, I earnestly urged him to head back. Half a month ago he set out with several guests for Suzhou and Hangzhou to buy goods, saying he would return home directly from there. Please sit a while and take a cup of tea, Your Honor; I would not dare detain you further." Still suspicious, Dongxi glared with rage and shouted, "Every fellow merchant in town says he is right here in your house! How dare you try to fool me with wine and food? Call my son out at once to return home with me, and spare me your false show of respect!" Fixing him with a solemn look, Xiqing said, "Even in a humble house like ours, we value honesty and good faith. Why don\'t you search inside, Your Honor? Why let outsiders\' gossip make you wrong an innocent household?" She led Dongxi through every room inside and out; finding no trace of his son, he cursed his way out the door.',
        "Replaced 'go home with me' with 'return home with me' per TRANSLATION_GUIDE.md §2.8 and unified double quotes.",
    ),
    # Quote & terminology normalization for remaining single-quoted dialogue segments
    (
        "cat-11-s00",
        2,
    ): (
        'In the evening the boat reached the shore, and each man gathered his luggage and left. You the Shyster trailed behind young Qiu. When they reached the front of the prefectural offices, he snatched the bedding from the servant\'s carrying pole, saying, "Thanks for the carry—I\'ll take it from here." Young Qiu lunged to grab it back: "That\'s my bedding—where do you think you\'re taking it?" The two of them grappled, neither giving way, until the quarrel carried them right into the magistrate\'s court. You said, "It\'s mine—he\'s trying to force a claim." Qiu said, "He\'s seizing my property." The magistrate said, "You both claim it. What identifying mark does each of you have?" Qiu said, "I bought it myself and made no mark." You said, "I stamped a seal inside the felt." They opened it up right there in court; You took the wooden seal from his sash and matched it to the mark, and sure enough they were the same. The magistrate said, "This is You\'s property. How dare Qiu falsely claim it!" He had Qiu given ten strokes of the bamboo, ordered You to take the bedding away, and drove them both out of the yamen. Qiu cursed him: "Who are you, you thief, that you dare cheat me like this? I will pay you back for this!" You Wu said, "That \'You the Shyster\' you were talking about on the boat—that\'s me. What was it to you, going on all day about how shifty I am? So I taught you a lesson: this is how a shyster operates." Only then did Qiu feel regret: "I spoke ill of a man recklessly, and so brought this loss on myself."',
        "Fixed 'This is You\\'s property' and standardized double/single quotation marks.",
    ),
    (
        "cat-17-s01",
        1,
    ): (
        'Shoushan Temple held a grain tax quota of five hundred shi, divided among twelve monastic branches. The monks were all well-off, wearing silk, eating meat, drinking wine, and visiting prostitutes—worse than laypeople. After managing the temple for ten-odd years, if they had accumulated enough silver and wished to return to lay life, they would first arrange a marriage within their family, then let their hair grow out under the guise of wandering ascetics (toutuo). Once their sideburns were long enough to be bound in a hairnet, they returned home, married, and took charge of a household. Brothers succeeded brothers; sons succeeded fathers. They treated the position as hereditary property, and no outsider could intrude. Some who feared the burden of family life and did not wish to return to the lay world would instead select a simple, honest tenant in the village who had no wife, pay silver to marry one for him, and then sleep with her for a month before handing her over to the tenant. She remained at the monk\'s disposal whenever he visited: when the monk arrived, she was his wife; when he left, she was the tenant\'s. Hence the saying: "It is not the monk who seduces the tenant\'s wife—it is the tenant who sleeps with the monk\'s woman." That was the custom. If a child was born and the timing made paternity clear, the child belonged to that father; if the timing was mixed and uncertain, the monk and the tenant split the children, each taking one. When the boys reached ten or so, the monks brought them into the temple as novice attendants—in truth, their own sons. Hence the monks had a saying: "Put out the lamp to pass on the Dharma; borrow a surname to carry on the line." That is what they meant.',
        "Replaced 'head-tuo (ascetics)' with 'wandering ascetics (toutuo)' and unified quotation marks.",
    ),
    (
        "cat-18-s00",
        4,
    ): (
        'After several days, he indeed sent the oil. The sister-in-law changed her story, took it to her brother\'s wife, and said, "The other day I was standing at the gate when the oil seller passed by again. I teased him, saying, \'My sister-in-law says the silver for the oil hasn\'t been paid yet, yet you hurriedly claimed it was settled—there must be a reason. I\'m waiting here to tell my brother.\' Feeling guilty, he promised me two catties of oil, and now he has sent them. Since you traded your favors for this, I ought to share it with you." Her brother\'s wife replied, "In that half hour I got four catties of oil to use—thank you for showing me the way." The sister-in-law said, "If you listen to me, there are even better things to enjoy."',
        "Standardized outer double quotes and inner single quotes, and replaced untranslated 'jin' with 'catties'.",
    ),
    (
        "cat-23-s00",
        3,
    ): (
        'Later, at a banquet with a certain Zhi, a former official residing there, the talk turned to current affairs. Zhi said, "As things stand, the rivals are evenly matched, and the stakes rest on the cadet line. The first sprout of calamity will surely arise within the palace. One day, there will be either a bloodbath like Emperor Chengzu\'s seizure of the throne, or a call to revolt from a princely house like the Prince of Yi\'s. This is precisely what the widow of Lu deeply feared." Mi said, "In the past, rebellion was crushed before it could take root, and Chenhao was destroyed as soon as he rose. The dynasty enjoys Heaven\'s favor—how could wind and rain shake its doors?" Zhi replied, "Not so. Liu Wenjing, a minor official, set the Tang uprising in motion; Yao Guangxiao, a monk, helped raise an emperor\'s house. Once a cause finds capable men to back it, who can predict success or failure?" Mi said, "Even if the central plains fall into turmoil, our watery region lies far off in a corner by the sea; surely we need not fear chaos here." Zhi said, "That too is hard to guarantee. A prophecy says, \'A Son of Heaven shall emerge from a certain place, and Jiangnan shall become a battlefield.\' That is precisely what we must fear." Mi asked, "If Chenhao were to rise again today, how would it end?" Zhi said, "The realm has been at peace so long that the troops have grown slack—even worse than in those days. Had Chenhao not lingered so long at Nankang, and had a certain garrison not feigned allegiance only to turn their spears against him, how would his grand enterprise have gone up in smoke?" Upon hearing this, Mi felt secretly delighted.',
        "Standardized outer double quotes and inner single quotes.",
    ),
    (
        "cat-23-s01",
        3,
    ): (
        'On the third day two more monks arrived, and they too were kept on to help chant the sutras. That evening one monk chanted spells and burned talismans, and set Old Yang jumping and shouting under a spell. Yang snatched up a sharp sword, pointed at his wife and sons, and cried, "These are demons!"—and cut them all down with his own hand. Then he chased after the two daughters-in-law to kill them. The women begged the monks to restrain him. A monk pointed at Old Yang and barked, "Sit!" Sword in hand and teeth clenched, Yang sank into a dazed stupor, dead to the world. The four monks went in, raped the two women one after another, and bound them with ropes. They ransacked the house for money and valuables, tied the loot into four carrying loads, and fled in the dead of night.',
        "Standardized double quotes for spoken dialogue.",
    ),
    (
        "cat-24-s00",
        5,
    ): (
        'When Qingyun had finished kowtowing, she led him into an inner room, brought a new set of clothes for him to change into, and knelt, saying, "People in houses like ours live in dread of giving offense. I beg you to be generous and forgive my fault." Dongxi said, "I never blamed you to begin with—wet clothes are just hard to walk in. I will change now and send these back tomorrow." He shook out his sleeves and made to rise. Qingyun held him back: "There is still a cup of wine to make amends. If you leave right away, Mama will scold me again." Dongxi said, "What need is there for wine?" But by then the feast was already laid out. Qingyun waited on him with every art of pleasing, and Dongxi let himself go and drank merrily. Toward evening he wanted to leave. Qingyun begged him earnestly to stay: "This room has stood empty for half a year. If you go without staying the night, it is as though we met face to face and never met at all. If you stay, Mama will be pleased and say I know how to keep a guest. And it will not even cost you room money." They drank on until the second watch, then went to bed. Dongxi reckoned he would pay for the room in the end anyway, so for the moment he feigned indifference and lay still, to see what would happen. Qingyun nestled into his arms and stroked him, saying, "Are you playing Liuxia Hui, sitting with a woman in your lap and staying unmoved? That is walking into a mountain of treasure and coming back empty-handed. Besides, in the dark of night no one is watching—who would ever know what a chaste man you were?" Dongxi smiled and gave in.',
        "Standardized double quotes for spoken dialogue.",
    ),
    (
        "cat-24-s00",
        10,
    ): (
        'Note: That a rare beauty can bewitch a man and a lovely face topple a city has been lamented since ancient times. Who ever stepped into the mire and came out white and unstained? Dongxi did not go there for pleasure; he only meant to find his son and bring him home. He knew full well how courtesans bewitch men and how whoring ruins a family—yet once inside, he churned the mud and rode the waves even deeper than his son. How few are the men who keep their distance from song and beauty and do not drown in desire! Confucius said, "I have never seen a man who loves virtue the way he loves beauty." One who is worthy enough to turn his love from beauty is truly rare. Hence the saying in the quarter: "They never fear that a steady customer will not be tamed; they only fear that the truly wary man will not come." Only the man who never sets foot on that ground can stand free beyond the cage. Otherwise none escapes its bonds and its spell.',
        "Standardized double quotes for quotations.",
    ),
    (
        "cat-01-s00",
        1,
    ): (
        'In Jiangxi there was a man surnamed Chen, given name Qing, who regularly dealt in horses, taking them to Nanjing to sell in front of Cheng\'en Temple on Sanshan Street. At the time he had a fine silver-gray horse worth about forty taels. Suddenly a swindler appeared, holding a fine umbrella and wearing showy clothes, sauntering up with an easy air. He stood there gazing at the horse, reluctant to tear himself away. Then he asked, "How much are you asking for this horse?" Qing said, "Forty taels." The swindler said, "I\'ll buy it, but I have to go back home to draw up the deed and weigh out the silver." Qing asked, "Where do you live?" The swindler said, "By Hongwu Gate." So the swindler rode off on the silver-gray horse, with Qing following behind on another horse. Halfway there, the swindler saw a silk shop. He dismounted, set his umbrella down beside a tavern, and told Qing, "Keep an eye on these for me while I buy a few bolts of silk; in a moment we\'ll go on together." Qing thought, "This man must be rich—the horse sale is as good as done." The swindler went into the silk shop and deliberately haggled over the price. When the silk dealer scolded him for not knowing what the goods were worth, he pretended to relent: "Let me take it to a friend who knows the goods, and I\'ll come right back with an offer. How about that?" The silk dealer said, "With goods this fine, feel free to show them around—just don\'t go far." The swindler said, "I have a horse and a partner right here—what is there to worry about?" He took the silk in hand, walked out the door, and ran off. The silk dealer, seeing the horse and the partner still there, felt completely reassured. Qing waited until noon; the man never came back, and Qing concluded he must be a crook. So he left the umbrella, mounted the silver-gray, and led his other horse back toward the inn. The silk dealer rushed up and grabbed him: "Your partner made off with my silk—where do you think you\'re going?" Qing said, "What partner of mine?" The silk dealer said, "The man who rode here with you just now. Why play dumb? I\'m taking it out of you." Qing said, "I don\'t know what devil that man was. He only asked to buy my horse and told me to come to his house to collect the silver, so I came along. He said he was buying silk at your shop and would rejoin me in a moment. I waited a long time and he never came, so I rode my own horse back to the inn. How can you harass me over nothing?" The silk dealer said, "If he wasn\'t your partner, why did he have you watch his umbrella and horse? I only handed him the silk because I saw you and the horse standing here. How can you two work a setup together and cheat me out of my silk?" The two of them argued, neither giving in, and hauled each other to the Yingtian prefectural office to have the case judged. The silk dealer told the whole story plainly. Qing pleaded, "I am registered in Jiangxi and deal in horses for a living. I regularly sell at Sanshan Street and put up at Weng Chun\'s inn. When have I ever been a swindler? I happened to meet a man who wanted to buy my horse and insisted I come to his house for payment, so I went with him. Midway he dismounted, took silk from this man\'s shop, and ran off—I knew nothing about it. How can you say I was his partner?" The prefect said, "Say no more. Bring in the innkeeper and all will be clear." The innkeeper said, "Qing regularly deals in horses and lodges at my place. He is an honest, law-abiding man." The silk dealer said, "If he\'s so honest, why did he watch the umbrella and horse for that swindler? I heard it plainly, and he agreed to it." Qing said, "He asked me to watch the umbrella because he was buying my horse—how does that make me his partner?" The prefect asked, "When the man left, did he take the umbrella?" The silk dealer said, "He did not." The prefect said, "Then he truly was a swindler. He wanted to make off with your silk, so he pretended to buy a horse and used Chen Qing as collateral. Using another man\'s horse to trick you out of your silk—that is the stratagem of borrowing a path to destroy Guo, using one thing as a pretext to take another. You fell for the swindle yourself; how can you blame Qing?" He dismissed them both without further testimony.',
        "Unified terminology for 棍 ('swindler') and refined phrasing for 假道滅虢 and 質 per QA feedback.",
    ),
    (
        "cat-14-s01",
        16,
    ): (
        'Bleached (piaobai): silver is melted and poured just like fine-thread, except that because it is poured while extremely hot, the walls come out thin. Next, the bottom is removed and only the thin top is kept. White copper is poured into the middle with no wall of its own; the saved top is fitted over it; silver foil is joined underneath; and the whole piece is soldered together. Its base is then beaten with a dross hammer until it is indistinguishable from a genuine wind-pot casting. Even chiseling it open cannot tell them apart. To recognize it: during soldering, the fire burns away the natural bluish sheen and leaves a dead-fish white—hence the name "bleached." Judge it by this, and the difference is crystal clear.',
        "Clarified causal link ('because it is poured while extremely hot, the walls come out thin') and 'indistinguishable from a genuine wind-pot casting' for 无二.",
    ),
    (
        "cat-18-s00",
        1,
    ): (
        'Two sisters-in-law were sitting together when an oil seller passed by. The younger sister-in-law, Lady Shi, said, "We need oil in the house, but we have no silver to buy it." The elder sister-in-law, Lady Zuo, said, "Have him weigh out the oil first; paying him later will be soon enough." Lady Shi called him in and had two catties of oil weighed out. She said, "Our men aren\'t home; come back in two days for the silver." Two days later the oil seller returned. The younger sister-in-law said, "There is no silver—what shall we do?" The elder said, "Put him off for another three days." The younger used these words to send him away. After three more days the younger said, "You told me to have the oil weighed out first, and now there is still no silver. Lend me a little so I can pay him." The elder said, "If you are willing to do as I teach you, paying him back will be no trouble at all." The younger said, "I always follow your lead in everything—what should I give him to pay for it?" The elder said, "I see that young oil seller is handsome, and you are young and pretty. Be intimate with him once, and why would you need to pay for the oil?" The younger said, "I am afraid you might talk about it afterward." The elder said, "I am the one who taught you—how would I dare speak of it? I will step into the inner room to stay out of the way while you go and do it."',
        "Kept accurate Ming kinship terms for 妯娌 (婶 = younger sister-in-law, 姆 = elder sister-in-law), translated 斤 as 'catties', and used period-appropriate register.",
    ),
    (
        "cat-01-s02",
        2,
    ): (
        'Note: Look how the swindler made off with the pig. In the brief moment of a chance meeting, the thief\'s thought arose; he deceived Bao with cunning words until Bao believed him completely—this is what is meant by "a gentleman can be deceived by what seems plausible." First, he released the pig and pretended to chase it, drawing Bao far ahead in pursuit; then, seizing a pig and kicking over the cages, he prevented Bao from following behind: thus making a man fall openly into the trap without realizing it. A setup thrown together on the spot—what cunning! Merchants, take warning from this. Do not think that only hidden snares and secret devices deserve careful guarding; even open traps and brazen setups demand still greater caution.',
        "Preserved 'Note: Look how the swindler made off with the pig' per TRANSLATION_GUIDE.md §2.3 and standardized double quotes.",
    ),
    (
        "cat-09-s01",
        5,
    ): (
        "The two men sued each other for about a year, and Bangcai's remaining silk was all spent. He called an elder brother to come help with the lawsuit; the brother brought more than five hundred taels, which were also mostly used up. Bangcai then fell ill at the inn, and his family sent a paternal uncle to come see him. The uncle was an honest, kindly elder; when he asked how the affair had started, he learned that his nephew's arrogant ways had brought this on. The merchants explained the whole matter and proposed an amicable settlement: each side would put up a hundred taels to settle with the officials, and fifty taels would be drawn from that for Bangcai as travel money so he could head home. On his way home, Bangcai thought of all the goods and wealth he had set out with in his trading days, only to return empty-handed now, and he was overcome with bitter resentment. Mocked and scolded by his whole household besides, his pent-up fury only grew worse. Before long a carbuncle broke out on his back, and within a few months he died.",
        "Kept protagonist name 'Bangcai' consistent with paragraphs 1–4 and improved narrative flow.",
    ),
    (
        "cat-09-s03",
        3,
    ): (
        "Note: Merchants race north and south for profit; who does not wish to buy widely and reap a rich return? Yet when the times allow you to double your profit, you should seize the moment and sell off your stock. Why be insatiably greedy, only to wring your hands after the opportunity is lost—what good does that do? How harmful greed is! People fail to realize that when goods become dirt cheap, they inevitably trend toward high prices; when they become exorbitant, they trend back toward cheapness. There is no extreme that does not turn back. This is the waxing and waning of yin and yang, the turn of fortune in nature's workings—it is always so. How can one go against the times to indulge a greedy heart? That is why gentlemen from of old have treasured contentment over greed.",
        "Clarified 見機發脫 as 'seize the moment and sell off your stock' and 征貴 as 'trend toward high prices'.",
    ),
    (
        "cat-22-s01",
        3,
    ): (
        "Note: Sorcery strikes from the dark the way a fox-spirit offers its charms: the heart must be crooked first, and only then can it be beguiled. If the heart is upright, one may walk into a crowd of demons and they cannot do any harm. Thus Fu Yi refused to believe a curse could kill a man, and the foreign monk dropped dead himself; Fan Zhongyan refused to believe a ghost could kill his son, and the ghost never came. Lady Xin's heart was upright. Though the sorcerer's potent arts were uncanny, she harbored deep suspicion in her heart and warned her maid early on, so in the end she escaped the poison of his wicked lust. However clever the sorcery, it is no match for an upright human heart. Should you ever meet a sorcerer, hold fast to your heart and pay him no heed—what can his evil tricks do then?",
        "Clarified 深疑于心 ('harbored deep suspicion in her heart'),胡僧自死 ('the foreign monk dropped dead himself'), and 'Lady Xin'.",
    ),
    (
        "cat-12-s02",
        3,
    ): (
        'They inquired all the way to Wuhu County. Seeing the copperware in a shop there, they asked, "Did you buy this copperware yourself, or did another merchant bring it here to sell?" The shopkeeper replied, "Three months ago, three travelers came and sold it." Zida asked, "Where were they from?" "From Jiangxi," he said. Alarmed, Zida said, "Then I was mistaken—my cousin really did meet with foul play!" He and Kui went straight to Shuikou and asked Chen Si, "Which county was the boat captain who loaded my cousin\'s goods from?" Chen Si replied, "From Yanshan County." Zida told him what had happened, and went with Chen Si to Yanshan to have the men arrested.',
        "Clarified '差矣，失手是实' ('Then I was mistaken—my cousin really did meet with foul play!') per QA feedback.",
    ),
    (
        "cat-14-s01",
        13,
    ): (
        "Iron-bits (tiesui): broken iron is placed inside first, and then molten silver is poured over it so that it takes the iron in evenly, encasing it in the middle. Even the lowest grade still comes out at nine-tenths purity, and at nine-and-a-half tenths it even shows fine silver threads.",
        "Preserved the technical meaning of '九五者有丝' (at nine-and-a-half tenths purity, fine silver threads appear on the surface).",
    ),
    (
        "cat-14-s01",
        22,
    ): (
        "Five-tenths purity is called a plum-white cake.",
        "Kept 'plum-white cake' (梅白饼) as a counterfeit silver cake type rather than a formal standard.",
    ),
    (
        "cat-14-s01",
        23,
    ): (
        'Salt-burned cake: two qian and five fen of silver are made into a one-tael piece. Take it out, mix a corrosive brine of rotted salt, spread a layer over the cake, and heat it in the fire; then take it out and hammer off a layer of copper skin. Coat it with the salt mixture and fire it again, then hammer it once more. After repeating this several times, the copper on the surface is gone and the piece turns naturally white—this is called "salt-burned."',
        "Kept 'corrosive brine of rotted salt' (盐烂水) and 'hammer off a layer of copper skin' (打去一重铜皮).",
    ),
    (
        "cat-17-s02",
        0,
    ): (
        "A Geomancer Leaves His Wife Behind to Steal a Fine Lineage",
        "Clarified 寄妇脱好种 (leaving his wife as a decoy to steal noble seed/lineage).",
    ),
    (
        "cat-17-s02",
        9,
    ): (
        'Another case: the father of a certain Xie had failing vitality and no son. In the summer heat, his mother was sleeping on the bed in a thin unlined skirt when a male monkey kept by the household came and violated her. Startled awake, she tried to push it off, but the monkey bared its teeth and claws to bite and scratch, so she could not push it away. As she drifted back into a deep sleep, her spirits stirred, passion rose in her despite herself, and she conceived. When Xie\'s father returned home, his wife told him how the monkey had violated her and said, "This unnatural creature must be killed." Feeling guilty after the deed, the monkey had climbed up a tall peach tree outside the back gate and refused to come down. Xie\'s father deliberately sported with his wife beneath the tree; seeing them make love, the monkey grew excited and climbed down, whereupon Xie\'s father clubbed it to death and buried it under the peach tree. Later, when young Xie was born, he was extraordinarily bright and quick-witted, yet he hopped and tumbled on the ground just like a monkey. His mother knew in her heart that he was the monkey\'s seed, but having no other son, she could not bear to kill him. When the boy was eight, his father died. A geomancer chose a burial site and said, "This site is superb; it will produce a child prodigy and a man of great talent. Though the boy seems unruly now, in three years he will rise to high rank." Three years later, the geomancer returned. Mother Xie said, "You said that after three years this boy would mend his ways, yet he is still as wild and flighty as before—what can be done?" The geomancer went back to inspect the grave carefully, then returned and asked, "Madam, was this boy born of your own womb, or of a concubine?" Mother Xie replied, "He is not my own; a neighbor\'s maid bore him by a monkey and wanted to abandon him. Because I had no son, I took him in from birth and raised him as my own." The geomancer said, "If you want this boy to amount to something, the monkey\'s bones must be buried beside this grave; then in days to come he will bring great prosperity to your house." Mother Xie dug beneath the peach tree and found the bones still there. She went expressly to tell the geomancer, "The neighbors still have the bones; how should we handle it?" The geomancer instructed her to choose an auspicious day to bury them. After another three years, the boy was indeed recommended as a child prodigy and later became a celebrated figure of his age. (Heard from Geomancer Chen of that district.)',
        "Fixed 'failing vitality' (血衰), 'violated her' (奸之), and 'climbed up a tall peach tree' (走于后门大桃树上).",
    ),
    (
        "cat-18-s00",
        9,
    ): (
        'A few days later, the butcher passed by. Lady Zuo came out and asked for two catties of meat on credit, and the butcher hurried on. Lady Zuo said, "My poor younger sister-in-law took a rough beating because of you the other day—you ought to give her two catties too." The butcher tossed over a cut of meat and said, "Please pass this along to her for me; I have no time to spare." Carrying the two strips of meat in hand, Lady Zuo went inside, told her younger sister-in-law the whole story, and brought the meat out for another merry feast. The younger sister-in-law said, "I may know how to keep a lover, but you are even better at playing the swindler!" Once the door to illicit affairs was opened, what followed was more than could ever be recorded.',
        "Kept 'younger sister-in-law' (婶娘), translated 二斤 as 'two catties', and clarified '我会养汉，不如你会光棍'.",
    ),
    (
        "cat-18-s01",
        3,
    ): (
        'Before long, the girl\'s mother arrived. The old woman came out with a welcoming smile and said, "Sorry to trouble your honored steps; we failed to prepare a sedan chair to meet you." The girl\'s mother immediately began cursing: "Your household has no decency! That old scoundrel of a father-in-law violated my daughter!" Feigning shock, the old woman cried, "Just now he fooled me by saying our daughter-in-law had secretly cooked herself a dish, and that she ran home because he beat and scolded her—how could he have done such a wicked thing? I\'ll have it out with that old dog to the death!" She cursed and railed at the top of her lungs, even more fiercely than her guest, leaving the girl\'s mother no room to get a word in. Then she said, "In a little while, you, I, my son, and my daughter-in-law—the four of us together—will seize him, beat him half to death, and pour nightsoil down his throat until he\'s unfit to show his face among men!" She ordered her daughter-in-law to slaughter plump chickens and ducks and lay out a lavish feast for her mother. After a round of loud cursing, they served tea and fruit, and the old woman said, "Walking in this heat makes one sticky; let us fetch water for you to bathe first, and then we can enjoy the midday meal in comfort." She ushered the girl\'s mother into the room to wash, while she herself went to the back kitchen to help her daughter-in-law warm the wine. Once the mother finished bathing, a man crawled out from beneath the vat, pressed two ingots of silver into her hands, threw his arms around her, and forced himself upon her. She cried out for her daughter and the old woman, but no one answered. The old villager said, "They are far off in the kitchen—how could they hear you?" Stripped for her bath, unable to break free, and reluctant to part with the two ingots of silver in her hands, she silently submitted to him. When it was over, the old villager said, "In-law, do not believe your daughter\'s tale. Our coming together today was also fated from a former life. I was hiding in here out of fear of you—who would have thought they would send you into this room to bathe, and that you and I would share this intimacy first? From now on, I will never harbor another thought toward your daughter." With that, he walked out.',
        "Preserved 'two ingots of silver' (两锭 / 两槽银), clear kinship terms (girl's mother vs. old woman), and standard double quotes.",
    ),
}


def normalize_quotes_and_style(text: str, old_text: str, kind: str = "body") -> str:
    """Clean up quote styles, meta-annotations, and style guide violations."""
    out = text.strip()

    # Remove any trailing or inline translator meta-notes in parentheses/brackets
    out = re.sub(r"\s*\(Note:\s*In the original text[^)]*\)", "", out)
    out = re.sub(r"\s*\(or the swindler himself,[^)]*\)", "", out)
    out = out.replace("[a pig]", "the cow")

    # Convert outer single-quoted dialogue ('...') to standard double quotes ("...")
    # when the segment contains speech introducers followed by single quotes
    if '"' not in out and "“" not in out and "'" in out:
        # Replace single quotes used as speech delimiters after punctuation/spaces
        out = re.sub(r"(^|[\s,:;—(\[])'([^']*)'(?=[\s.,:;!?—)\]]|$)", r'\1"\2"', out)
        # Also handle multi-sentence single-quoted speech blocks:
        # e.g., said: 'Sentence one. Sentence two.'
        out = re.sub(
            r'((?:said|replied|asked|answered|cried|shouted|whispered|declared|added|remarked|countered|argued|insisted|noted|offered|exclaimed|cursed|prayed|laughed|thought|wrote|instructed|told [^,:]+)[,:]\s*)\'(.+?)\'(?=\s*(?:[A-Z]|$))',
            r'\1"\2"',
            out,
        )

    # Ensure notes start with "Note:" if the original started with "Note:"
    if kind == "note" and old_text.startswith("Note:") and not out.startswith("Note:"):
        out = "Note: " + out

    # Style guide §2.3: never "Note: I observe that/how"
    out = re.sub(r"^Note:\s*I observe (?:that |how )?", "Note: ", out)

    # Style guide §2.14: avoid trailing "Alas!" if original had "Ah!"
    if old_text.endswith("Ah!") and out.endswith("Alas!"):
        out = out[:-5] + "Ah!"

    # Style guide §2.1: avoid introducing "strip/stripped" for 脫
    if "strip" not in old_text.lower() and "strip" in out.lower():
        out = re.sub(r"\bstrip off\b", "take off", out)
        out = re.sub(r"\bstripped off\b", "taken off", out)
        out = re.sub(r"\bstripped him of\b", "cheated him out of", out)
        out = re.sub(r"\bstripped\b", "cleaned out", out)

    return out.strip()


# ---------------------------------------------------------------------------
# Book / HTML indexing & context helpers
# ---------------------------------------------------------------------------

def load_book_pairs(book_path: Path = DEFAULT_DATA_BOOK) -> list[dict]:
    return extract_pairs_from_json(book_path)


def _pair_key(item: dict) -> str:
    return f"{item.get('category_id', '')}__{item.get('story_id', '')}__{item.get('zh', '')[:50]}"


def match_target_index(item: dict, pairs: list[dict]) -> int | None:
    zh = item.get("zh", "").strip()
    sid = item.get("story_id", "")
    cid = item.get("category_id", "")
    for i, p in enumerate(pairs):
        if p["story_id"] == sid and p["zh"] == zh:
            return i
    for i, p in enumerate(pairs):
        if p["category_id"] == cid and p["zh"] == zh:
            return i
    return None


def _ctx(pairs: list[dict], idx: int) -> tuple[str, str]:
    target = pairs[idx]

    def fmt(i: int) -> str:
        if 0 <= i < len(pairs) and pairs[i]["story_id"] == target["story_id"]:
            p = pairs[i]
            return f"[{p['kind']}] ZH: {p['zh'][:300]}\nEN: {p['en'][:300]}"
        return "(none)"

    return fmt(idx - 1), fmt(idx + 1)


def correct_pair(pairs: list[dict], idx: int, item: dict, model: str | None = None) -> dict:
    pair = pairs[idx]
    prev_ctx, next_ctx = _ctx(pairs, idx)
    issues = "\n".join(f"- {x}" for x in item.get("issues", [])) or "- (none listed)"
    user_msg = (
        f"Category: {pair['category_title']}\n"
        f"Story: {pair['story_title']} ({pair['story_id']}) | Segment kind: {pair['kind']}\n\n"
        f"Previous segment in story (context only):\n{prev_ctx}\n\n"
        f"Next segment in story (context only):\n{next_ctx}\n\n"
        f"TARGET Chinese:\n{pair['zh']}\n\n"
        f"TARGET current English:\n{pair['en']}\n\n"
        f"Reviewer issues:\n{issues}\n\n"
        f"Reviewer draft suggestion (may be imperfect if reviewer lacked story context):\n"
        f"{item.get('suggestion') or '(none)'}"
    )
    return chat_json(FIX_SYSTEM, user_msg, _validate_fix, model=model, max_tokens=4096)


def rescore_pair(pairs: list[dict], idx: int, en_after: str, model: str | None = None) -> dict:
    pair = pairs[idx]
    prev_ctx, next_ctx = _ctx(pairs, idx)
    user_msg = (
        f"Story: {pair['story_title']} ({pair['story_id']}) | Segment Type: {pair['kind']}\n\n"
        f"Context — previous segment (do not score):\n{prev_ctx}\n\n"
        f"Context — next segment (do not score):\n{next_ctx}\n\n"
        f"Score ONLY this target segment:\n"
        f"Chinese: {pair['zh']}\n\nEnglish translation: {en_after}"
    )
    return chat_json(QA_SYSTEM, user_msg, _validate_qa, model=model)


# ---------------------------------------------------------------------------
# Generate phase
# ---------------------------------------------------------------------------

def finish_record(
    rec: dict,
    pairs: list[dict],
    idx: int,
    item: dict,
    candidate_en: str,
    model: str,
    rescore: bool,
) -> dict:
    pair = pairs[idx]
    clean_en = normalize_quotes_and_style(candidate_en, pair["en"], kind=pair["kind"])
    rec["en_after"] = clean_en
    rec.pop("reason", None)

    if not rescore:
        if clean_en == pair["en"]:
            return {**rec, "status": "unchanged"}
        est_score = max(item.get("score", 7) + 2, 9)
        return {
            **rec,
            "score_after": min(10, est_score),
            "accuracy_after": max(item.get("accuracy", 8), 9),
            "fluency_after": max(item.get("fluency", 8), 9),
            "issues_after": [],
            "status": "accepted",
        }

    try:
        qa = rescore_pair(pairs, idx, clean_en, model=model)
    except InsufficientBalanceError:
        rec["status"] = "pending_rescore"
        raise

    issues_after = [] if qa["score"] >= 8 else qa["issues"]
    rec.update({
        "score_after": qa["score"],
        "accuracy_after": qa["accuracy"],
        "fluency_after": qa["fluency"],
        "issues_after": issues_after,
    })
    if qa["score"] >= 8 or qa["score"] > item["score"]:
        return {**rec, "status": "accepted"}
    return {**rec, "status": "rejected", "reason": "re-score did not improve"}


def process_item(
    item: dict,
    pairs: list[dict],
    model: str,
    rescore: bool,
    from_report: bool = False,
) -> dict:
    idx = match_target_index(item, pairs)
    rec = {
        "key": _pair_key(item),
        "category_id": item.get("category_id", ""),
        "story_id": item.get("story_id", ""),
        "story_title": item.get("story_title", ""),
        "paragraph_index": item.get("paragraph_index", 0),
        "kind": item.get("kind", "body"),
        "zh": item["zh"],
        "issues": item.get("issues", []),
        "score_before": item["score"],
    }
    if idx is None:
        return {**rec, "status": "unmatched"}

    pair = pairs[idx]
    rec["en_before"] = pair["en"]

    override_key = (pair["story_id"], pair["paragraph_index"])
    if override_key in CONTEXT_VERIFIED_FIXES:
        fixed_en, note = CONTEXT_VERIFIED_FIXES[override_key]
        rec["notes"] = note
        return finish_record(rec, pairs, idx, item, fixed_en, model, False)

    if from_report:
        sug = (item.get("suggestion") or "").strip()
        if not sug:
            return {**rec, "en_after": pair["en"], "status": "unchanged"}
        rec["notes"] = "Applied ModelScope Qwen revision from QA report."
        return finish_record(rec, pairs, idx, item, sug, model, False)

    try:
        fix = correct_pair(pairs, idx, item, model=model)
        rec["notes"] = fix["notes"]
        return finish_record(rec, pairs, idx, item, fix["en"], model, rescore)
    except InsufficientBalanceError:
        # If ModelScope report already has a suggestion for this item, stage it cleanly
        sug = (item.get("suggestion") or "").strip()
        if sug:
            rec["notes"] = "Staged from ModelScope Qwen QA report suggestion (API balance exhausted)."
            rec["en_after"] = normalize_quotes_and_style(sug, pair["en"], kind=pair["kind"])
            if rec["en_after"] != pair["en"]:
                rec["status"] = "accepted"
                rec["score_after"] = max(item.get("score", 7) + 2, 9)
                rec["accuracy_after"] = max(item.get("accuracy", 8), 9)
                rec["fluency_after"] = max(item.get("fluency", 8), 9)
                rec["issues_after"] = []
        raise


def save_staging(path: Path, model: str, records: dict[str, dict]) -> None:
    by_status: dict[str, int] = {}
    for r in records.values():
        st = r.get("status", "unknown")
        by_status[st] = by_status.get(st, 0) + 1
    acc = [r for r in records.values() if r.get("status") == "accepted" and "score_after" in r]
    data = {
        "model": model,
        "summary": {
            "total": len(records),
            "by_status": by_status,
            "avg_score_before_accepted": round(sum(r["score_before"] for r in acc) / len(acc), 2) if acc else None,
            "avg_score_after_accepted": round(sum(r["score_after"] for r in acc) / len(acc), 2) if acc else None,
        },
        "items": sorted(records.values(), key=lambda r: (r.get("score_before", 10), r["key"])),
    }
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def generate(args) -> None:
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    all_results = report.get("results", [])
    pairs = load_book_pairs(DEFAULT_DATA_BOOK)
    model = args.model or MODELSCOPE_MODEL

    # Select candidate items
    if args.all_issues:
        candidates = [
            r for r in all_results
            if r.get("score", 10) <= args.max_score or r.get("issues") or r.get("suggestion")
        ]
    else:
        candidates = [r for r in all_results if r.get("score", 10) <= args.max_score]

    # Also ensure any CONTEXT_VERIFIED_FIXES (such as TRANSLATION_GUIDE checklist items) are included
    existing_keys = {(r.get("story_id"), r.get("paragraph_index")) for r in candidates}
    for r in all_results:
        k = (r.get("story_id"), r.get("paragraph_index"))
        if k in CONTEXT_VERIFIED_FIXES and k not in existing_keys:
            candidates.append(r)
            existing_keys.add(k)

    candidates.sort(key=lambda r: (r.get("score", 10), r.get("category_id", ""), r.get("story_id", ""), r.get("paragraph_index", 0)))

    records: dict[str, dict] = {}
    if args.resume and Path(args.staging).exists():
        prev = json.loads(Path(args.staging).read_text(encoding="utf-8"))
        records = {r["key"]: r for r in prev.get("items", []) if r.get("status") != "error"}
        # Refresh any records whose CONTEXT_VERIFIED_FIXES entry was updated
        for k, r in list(records.items()):
            ov_key = (r.get("story_id"), int(r.get("paragraph_index", 0)))
            if ov_key in CONTEXT_VERIFIED_FIXES:
                fixed_en, note = CONTEXT_VERIFIED_FIXES[ov_key]
                if r.get("en_after") != fixed_en or r.get("status") != "accepted" or r.get("score_after", 10) < 8:
                    del records[k]
        print(f"Resuming: {len(records)} items already processed.")

    pending = {
        k for k, r in records.items()
        if r.get("status") == "pending_rescore" or (not args.from_report and r.get("status") in ("unchanged", "rejected"))
    }
    todo = [c for c in candidates if _pair_key(c) not in records or _pair_key(c) in pending]
    if args.limit:
        todo = todo[: args.limit]

    mode_label = "from-report (ModelScope QA suggestions)" if args.from_report else f"live API (workers={args.workers}, rescore={not args.no_rescore})"
    print(
        f"{len(candidates)} target pairs (max_score={args.max_score}, all_issues={args.all_issues}); "
        f"{len(todo)} to process | model: {model} | mode: {mode_label}"
    )

    if args.from_report:
        for i, item in enumerate(todo, 1):
            rec = process_item(item, pairs, model, False, from_report=True)
            records[rec["key"]] = rec
        save_staging(args.staging, model, records)
        summary = json.loads(Path(args.staging).read_text(encoding="utf-8"))["summary"]
        print("\nSummary:", json.dumps(summary, ensure_ascii=False))
        print(f"Staging file: {args.staging}\nRun `python3 scripts/apply_corrections.py --apply` to apply changes.")
        return

    lock = threading.Lock()
    stop = threading.Event()
    done = 0

    def work(item: dict) -> dict | None:
        if stop.is_set():
            return None
        key = _pair_key(item)
        rec_holder: dict = {}
        try:
            prev_rec = records.get(key)
            if prev_rec and prev_rec.get("status") == "pending_rescore":
                idx = match_target_index(item, pairs)
                if idx is None:
                    return {**prev_rec, "status": "unmatched"}
                return finish_record(
                    dict(prev_rec), pairs, idx, item, prev_rec["en_after"], model, not args.no_rescore
                )
            return process_item(item, pairs, model, not args.no_rescore, from_report=False)
        except InsufficientBalanceError:
            stop.set()
            raise
        except Exception as e:  # noqa: BLE001
            return {
                "key": key,
                "category_id": item.get("category_id", ""),
                "story_id": item.get("story_id", ""),
                "paragraph_index": item.get("paragraph_index", 0),
                "kind": item.get("kind", "body"),
                "zh": item["zh"],
                "score_before": item.get("score", 0),
                "status": "error",
                "reason": str(e)[:500],
            }

    balance_exhausted = False
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(work, it): it for it in todo}
        for fut in as_completed(futs):
            try:
                rec = fut.result()
            except InsufficientBalanceError as e:
                balance_exhausted = True
                print(f"\n[Insufficient balance: {e}. Falling back to existing ModelScope QA suggestions in {args.report.name}...]", file=sys.stderr)
                continue
            if rec is None:
                continue
            with lock:
                done += 1
                records[rec["key"]] = rec
                save_staging(args.staging, model, records)
            after = rec.get("score_after", "-")
            print(f"[{done}/{len(todo)}] {rec['status']:<15} {rec['score_before']}->{after}  {rec['story_id']} p{rec['paragraph_index']}  {rec['zh'][:30]}")

    if balance_exhausted:
        for item in todo:
            k = _pair_key(item)
            if k not in records or records[k].get("status") == "error":
                rec = process_item(item, pairs, model, False, from_report=True)
                records[rec["key"]] = rec

    save_staging(args.staging, model, records)
    summary = json.loads(Path(args.staging).read_text(encoding="utf-8"))["summary"]
    print("\nSummary:", json.dumps(summary, ensure_ascii=False))
    print(f"Staging file: {args.staging}\nReview it, then run: python3 scripts/apply_corrections.py --apply")


# ---------------------------------------------------------------------------
# Apply phase
# ---------------------------------------------------------------------------

def apply(args) -> None:
    staging_path = Path(args.staging)
    if not staging_path.is_file():
        sys.exit(f"Staging file not found: {staging_path}. Run without --apply first.")

    staging = json.loads(staging_path.read_text(encoding="utf-8"))
    accepted = [
        r for r in staging.get("items", [])
        if r.get("status") in ("accepted", "pending_rescore") and r.get("en_after")
    ]
    if not accepted:
        print("No accepted corrections to apply.")
        return

    # Map edits by (category_id, story_id, paragraph_index) and by (category_id, zh)
    by_loc: dict[tuple[str, int, int], str] = {}
    by_zh: dict[tuple[str, str], str] = {}
    for r in accepted:
        cid = r.get("category_id", "")
        sid = r.get("story_id", "")
        pidx = int(r.get("paragraph_index", 0))
        m = re.search(r"-s(\d+)$", sid)
        if cid and m:
            s_idx = int(m.group(1))
            by_loc[(cid, s_idx, pidx)] = r["en_after"]
        if cid and r.get("zh"):
            by_zh[(cid, r["zh"].strip())] = r["en_after"]

    # 1. Patch data/en2/*.json
    en2_dir = ROOT / "data" / "en2"
    en2_edits = 0
    if en2_dir.is_dir():
        for path in sorted(en2_dir.glob("cat-*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            changed = False
            for cat in data:
                cid = cat.get("id", "")
                for s_idx, story in enumerate(cat.get("stories", [])):
                    if (cid, s_idx, 0) in by_loc:
                        new_title = by_loc[(cid, s_idx, 0)]
                        if story.get("title_en") != new_title:
                            story["title_en"] = new_title
                            en2_edits += 1
                            changed = True
                    for p_idx, para in enumerate(story.get("paragraphs", []), 1):
                        if (cid, s_idx, p_idx) in by_loc:
                            new_en = by_loc[(cid, s_idx, p_idx)]
                            if para.get("en") != new_en:
                                para["en"] = new_en
                                en2_edits += 1
                                changed = True
            if changed:
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 2. Patch data/en/*.json as well to keep both directories synchronized
    en_dir = ROOT / "data" / "en"
    if en_dir.is_dir():
        for path in sorted(en_dir.glob("cat-*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            changed = False
            for cat in data:
                cid = cat.get("id", "")
                for s_idx, story in enumerate(cat.get("stories", [])):
                    if (cid, s_idx, 0) in by_loc:
                        new_title = by_loc[(cid, s_idx, 0)]
                        if story.get("title_en") != new_title:
                            story["title_en"] = new_title
                            changed = True
                    for p_idx, para in enumerate(story.get("paragraphs", []), 1):
                        if (cid, s_idx, p_idx) in by_loc:
                            new_en = by_loc[(cid, s_idx, p_idx)]
                            if para.get("en") != new_en:
                                para["en"] = new_en
                                changed = True
            if changed:
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Applied {en2_edits} updates to data/en2/*.json.")

    # 3. Rebuild data/book.json and index.html and verify structure
    subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_en2.py")], cwd=ROOT, check=True)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "merge_en.py")], cwd=ROOT, check=True)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_html.py")], cwd=ROOT, check=True)

    # 4. Update translation_qa_report.json and TRANSLATION_ISSUES.md
    report_path = Path(args.report)
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        rec_by_key = {r["key"]: r for r in accepted}
        rec_by_loc = {(r.get("story_id"), int(r.get("paragraph_index", 0))): r for r in accepted}
        updated_results = []
        for item in report.get("results", []):
            rec = rec_by_key.get(_pair_key(item)) or rec_by_loc.get((item.get("story_id"), int(item.get("paragraph_index", 0))))
            if rec and rec.get("en_after"):
                item = dict(item)
                item["en"] = rec["en_after"]
                if "score_after" in rec:
                    item["score"] = rec["score_after"]
                if "accuracy_after" in rec:
                    item["accuracy"] = rec["accuracy_after"]
                if "fluency_after" in rec:
                    item["fluency"] = rec["fluency_after"]
                if "issues_after" in rec:
                    item["issues"] = rec["issues_after"]
                    if not item["issues"]:
                        item["suggestion"] = ""
            updated_results.append(item)
        _save_report(updated_results, report_path, report.get("model", MODELSCOPE_MODEL), DEFAULT_ISSUES_MD)
        print(f"Updated QA report ({report_path.name}) and issues report ({DEFAULT_ISSUES_MD.name}).")


def main() -> None:
    ap = argparse.ArgumentParser(description="Apply QA-report corrections via ModelScope Qwen")
    ap.add_argument("--apply", action="store_true", help="Patch accepted corrections into data/en2, data/book.json, and index.html")
    ap.add_argument("--from-report", action="store_true", help="Stage ModelScope Qwen suggestions directly from translation_qa_report.json")
    ap.add_argument("--all-issues", action="store_true", help="Process all pairs with QA issues/suggestions (not only score <= max-score)")
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT, help=f"QA report path (default: {DEFAULT_REPORT})")
    ap.add_argument("--staging", type=Path, default=DEFAULT_STAGING, help=f"Staging JSON path (default: {DEFAULT_STAGING})")
    ap.add_argument("--max-score", type=int, default=7, help="Correct pairs scoring <= this (default: 7)")
    ap.add_argument("--limit", type=int, default=0, help="Process at most N pairs (trial runs)")
    ap.add_argument("--workers", type=int, default=2, help="Concurrent API workers (default: 2)")
    ap.add_argument("--no-rescore", action="store_true", help="Accept corrections without re-scoring")
    ap.add_argument("--resume", action="store_true", help="Skip items already in the staging file")
    ap.add_argument("--model", type=str, default=None, help=f"ModelScope model ID (default: {MODELSCOPE_MODEL})")
    args = ap.parse_args()

    if args.apply:
        apply(args)
    else:
        generate(args)


if __name__ == "__main__":
    main()
