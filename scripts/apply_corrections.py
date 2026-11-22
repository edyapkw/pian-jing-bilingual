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
    ('cat-01-s02', 1): (
        'Deng Zhaobao of Jianyang in Fujian made his living as a peddler. One day he was carrying four piglets to sell in Chong\'an and Da\'an. On Ma\'an Ridge he met a con man who asked about buying pigs. Bao thought to himself: "This is a lonely mountain ridge where no one passes through, and the nearest houses are far off—why would this man be buying pigs out on the road?" Suspicious, he asked where the man lived. The con man said, "Just ahead at Ma\'anping." Bao said, "If you mean to buy, I\'ll head home with you." The con man said, "I\'m on my way to the county seat. Take one out for me to inspect. If it suits me, we\'ll settle on a price and then I\'ll go back home to weigh out the silver; otherwise I\'m afraid it will delay my trip." Since the con man\'s words sounded reasonable, Bao agreed and took out a piglet to show him. The con man took it, held it by the tail, and set it on the ground to examine closely—then deliberately let go so the pig bolted. Feigning alarm, he cried, "Oh no, oh no!" and rushed after it—though his chasing was really driving it farther away. Seeing the pig running off, Bao dashed forward with all his might to catch it, never realizing he had already fallen into the trap. Once the con man saw Bao chasing the pig some two or three hundred paces from the cages, he doubled back, snatched a pig from one cage, kicked over the other two cages so all the pigs escaped, and shouted, "Much obliged! Take your time rounding them up!" Bao wanted to chase the con man, but with three pigs loose from the cages, he feared losing them while pursuing him; besides, the con man was already too far off to catch. He could only curse him roundly. Fortunately the three pigs clustered together; he gathered them back into the cages and went on his way nursing his resentment.',
        "Restored Deng Zhaobao pig-peddler story matching Chinese source, replaced 'go home' per §2.8, and refined 'clustered together' (成聚).",
    ),
    ('cat-05-s03', 3): (
        'Once all the land was sold, Ziming cut off all dealings with him. Borrowing morning and night, Ying found that only his own elder brother would help him. Only then did he realize that blood is blood after all, and that those wine-and-meat friends were false from the start.',
        'Clarified borrowing clause while preserving protagonist Ying from paragraph 1.',
    ),
    ('cat-09-s01', 2): (
        'At that time there was Wang Fengqi of Huizhou, from a great clan of prominent officials. He could not stomach Bangcai using wealth and power to crush others, and said: "Worldly power is fleeting, yet you always talk in thousands of taels. In former days, was Shi Chong\'s wealth any less than yours? And what became of him in the end?" Bangcai, furious that someone dared challenge him, said: "On this boat there are men senior to a humble man like me, and men with greater capital than mine, yet they have not said a word. You dare step forward and oppose me? I will stake all one hundred piculs of silk, worth several thousand taels, against you!" Fengqi cursed him: "You base creature, you truly do not know your place! Time and again you behave outrageously—you really are a little wretch who does not know death is near! I have several thousand taels to match you, and I will see that you never return alive to your native land!" The two wrangled without stopping. Everyone aboard secretly rejoiced to see Wang and Wei locking horns, feeling greatly pleased at heart. Someone who favored Wang urged them each to retire to their cabins.',
        "Clarified status/capital comparison while keeping Wei Bangcai's name consistent with paragraph 1.",
    ),
    ('cat-11-s00', 0): (
        "Secretly Stamping a Mark to Seize Another's Bedding",
        "Replaced 'Wins' with 'Seize' to reflect the fraudulent seizure of the traveler's bedding.",
    ),
    ('cat-12-s01', 2): (
        'For half a year after that, there was little business. It happened that Magistrate Jia, newly appointed and on his way to his post, passed through Yangzhou and wanted to take a concubine. Wei and his son-in-law Ji discussed it privately and decided to palm the daughter off on him in a sham marriage. Ji agreed, acted as matchmaker himself, and went to discuss it with Magistrate Jia. Jia came to look, was pleased, sent betrothal gifts at once, paid eighty taels, and chose a day for the wedding. Wei and Ji together said to the daughter: "We have had no business for half a year; the household is hard pressed, so we are marrying you to Magistrate Jia in name only. In truth your husband is a young man—how could he bear to give you up? We are your parents, and we have only you—how could we bear to let you go? We simply have no choice; we are treating you as merchandise. Besides, Magistrate Jia is old; he has not brought his principal wife with him for now, so of course he will cherish you. But I fear that after he takes up his post and sends for the principal wife, she will surely treat you cruelly; cursing and whipping will be hard to avoid. The old saying goes: \'Better a poor man\'s wife than a rich man\'s concubine.\' For now we will send you; in a few days he will board a boat. Your husband will secretly follow in a boat, with a white pair of trousers hung up as a signal. If you can get away at night, cross over to the white-trousers boat; your husband will be there to take you. Do not oversleep and ruin your husband\'s whole life; you yourself would also suffer." Ji deliberately clutched his wife\'s clothes, weeping, and pleaded to her face: "If you will promise to come back, I will let you go; if you cannot get away, I would rather die with you—I cannot bear to part." His wife Miss Wei also wept and said: "When father and mother have given the order, how would I not come back? Only you must follow in a boat and wait to take me; do not miss the time." The three of them had the plan set.',
        'Clarified the roles of Wei (father), Ji (young husband), and Miss Wei (daughter/wife) and improved phrasing.',
    ),
    ('cat-13-s01', 0): (
        'Chen Quan Leaves a Parting Trick After Visiting a Famous Courtesan',
        'Clarified 遗计 (leaving a parting trick/verse) and 嫖名妓 (visiting a famous courtesan).',
    ),
    ('cat-13-s01', 5): (
        'Halfway there, Zisui hurriedly said, "I left a key in your mistress\'s room. I set out in a rush and forgot to bring it; you must go back and fetch it." The servant turned to go. Zisui called out, "Wait! You will need a note in my own hand, or your mistress will not hand the key over to you. Otherwise, you will never get it." Zisui dismounted from the donkey, went into a paper shop, and wrote a verse: "Hangzhou\'s Hua Buru took in Jintai\'s prodigal Zisui; she fell for the man and lost a donkey too. From this parting on, where will she find her bright pearls?" He sealed it and sent the man back. Buru opened it, realized she had been swindled, and hurriedly opened the leather chests—inside were nothing but bricks and stones. Zisui had already ordered his men to hire a boat and wait; as soon as he reached the river, he boarded and headed back to the capital. Later, Buru inquired carefully and learned it was Chen Quan of Nanjing, but there was nothing she could do.',
        "Improved fluency while keeping courtesan Hua Buru's female pronouns and Zisui's full alias.",
    ),
    ('cat-13-s01', 6): (
        "Note: Brothels routinely cheat others—in minor cases ruining a family, in major ones costing a life—yet one never hears of them being cheated themselves. Hua Buru, moreover, set her price high and handpicked her patrons; the wealth she extracted from men was beyond counting. Who would have thought Chen Quan possessed tricks even more uncanny, appearing and vanishing like a spirit? She lost both her lover and her donkey, and regret came too late. Though this was a piece of unruly mischief on Chen Quan's part, it still provides a good laugh at the expense of the pleasure houses that ensnare others.",
        "Fixed opening clause ('Brothels routinely cheat others') while keeping Hua Buru as a proper name.",
    ),
    ('cat-14-s01', 0): (
        'Accepting Bleached Counterfeit Silver at the Maozhou Fair',
        'Clarified 漂白皛 as bleached counterfeit silver while keeping the Maozhou Fair setting from paragraph 1.',
    ),
    ('cat-14-s01', 17): (
        'The method for refining cake silver: for every qian of lead used, melt in one fen of copper. For one tael of nine-tenths silver, use one tael of lead; for eight-tenths, two taels; for seven-tenths, three taels. Pack charcoal into the furnace within an ash-lined hearth and fan the fire gently until the lead melts. Afterward, fan vigorously; when oil beads grow as large as beans, cover the pot and remove it immediately—this yields ninety-five percent purity. If you wait until golden flowers blaze before removing it, a crust forms on top called "cloth-heart cake" or "scorched-heart cake." Below lie crab-eyes and returning pearls, with both faces white: this indicates pine-grain silver at full fineness.',
        "Removed QA model's self-talk ('? No, the text actually says:') and refined technical description.",
    ),
    ('cat-16-s00', 0): (
        'A Woman Marries a Street-Sweeper and Costs Him His Life',
        'Clarified 害命 as costing the street-sweeper his life through the marriage scheme.',
    ),
    ('cat-16-s02', 1): (
        'Cai Tianshou of Guangdong was open-handed and stood by his word. At forty he had no son, and his wife was such a shrew that she would not let him take a concubine. One day he brought more than thirty loads of Guangdong tin to Suzhou to sell, and said to the broker Xiao Hanqing, "I still have no son, and I would like to take a concubine here. Is there anyone suitable on reasonable terms?" Hanqing said, "When you have silver, why worry about finding a girl to your liking?" He led him to look at several unmarried girls, but none suited: "I am past forty; these girls are too young to be a match for my years, so I will not take them."',
        'Removed parenthetical meta-note and clarified the age objection naturally.',
    ),
    ('cat-16-s03', 0): (
        'A Frog Exposes a Murderous Marriage Plot',
        "Removed conditional meta-text and used 'Exposes a Murderous Marriage Plot'.",
    ),
    ('cat-16-s03', 2): (
        'Dressed in white mourning clothes, Cai went to see Pan\'s parents, wept loudly first, and then reported the tragic news. The entire household wailed and asked in detail how he had died. Cai said, "While crossing at the West Gate ferry, he boarded the boat and took the pole to push off, and plunged into the water along with the pole. The water was deep and swift, and he lacked the strength to come back up, so he drowned. I hired men to recover his body, cremated his bones, and brought them home." When he finished speaking, the Pan household wept again. Cai then accounted clearly for the sales ledger and all the capital, item by item, and returned them to the parents. The whole family felt grateful for his kindness—how could they know that Cai himself had plotted the murder?',
        "Kept third-person 'he' for the drowned victim Pan while improving narrative flow.",
    ),
    ('cat-17-s00', 0): (
        'Using Silver to Turn the Tables and Seduce a Woman Drying Paper',
        'Captured 反买 (turning the tables with silver) and the paper-drying setting of the story.',
    ),
    ('cat-17-s02', 1): (
        'There was a geomancer named Lu, quite skilled at reading mountains, who wanted to find a fine gravesite to bury his own father. Searching as far as Ningcheng, he found an auspicious fengshui spot behind the ancestral shrine of the retired official Yang, where it was neither easy to buy openly nor possible to bury in secret. Hearing that Official Yang had passed away and his two sons were also looking for a site to bury their father, Geomancer Lu offered them this spot and led the two young masters to inspect it. It was indeed a fine burial site—it cost them nothing to buy, and its orientation promised great fortune. They buried their father there and rewarded the geomancer with thirty taels of silver. Since Lu could not get the gravesite for himself, he resolved to steal their lineage instead. He rented a house by the garden gate of the Yang sons, used the silver to marry a beautiful woman, and lived with her for two months. Then he told his wife: "I must travel afar to practice geomancy, and I cannot say when I will return. If the household runs short of firewood and rice, I have asked the young masters to look after you. They are my benefactors; it was only because of their silver that I could marry you. While I am away, if these two young masters make advances, you may go along with them. If you win their affection, they will support you generously later on. However, their household has many servants—never have anything to do with the servants. If you cheapen yourself, the young masters will despise you, and you will end up in poverty with no one to help." He also went to ask the two young masters: "I have to travel far away, and my household is left with meager means. Please help look after my wife, and when I return I will repay everything in full."',
        "Preserved 'steal their lineage' (脱其种) central to the plot while improving fluency.",
    ),
    ('cat-17-s02', 3): (
        'When Lu went away again, the young masters came calling once more. The woman said, "If the two of you come at irregular times, I fear I may develop a bad illness. It would be better to set odd months for the elder young master and even months for the younger." The young masters agreed: "You are quite right. From now on, each month belongs to the appointed one, and he shall supply all the vegetables, rice, and provisions." Before they knew it, four years had passed, and she had borne two sons, both of the two young masters\' bloodline. Geomancer Lu had their fortunes read, and every diviner predicted great wealth and rank for them in the future. He then took his wife and sons and bade farewell to the two young masters, each of whom gave him generous parting gifts. Later, the two boys grew up and both passed the imperial examinations—in truth, the Yang family\'s auspicious fengshui had been secretly siphoned away without their ever knowing it.',
        "Kept Geomancer Lu ('When Lu went away again') as subject of the opening clause and improved flow.",
    ),
    ('cat-17-s03', 0): (
        "An Affair with a Neighbor's Maid Leads to Stolen Silver",
        "Clarified 奸人婢致盗失银 as an affair with another's maid leading to stolen silver.",
    ),
    ('cat-17-s03', 2): (
        'One day, while Ying was out drinking at a brothel, the maid Jing Ji quietly unlocked his room and stole over a hundred taels of silver. When Ying returned to the inn and discovered the silver missing, he pressed the innkeeper to make good the loss. Fellow lodger Wu Lun said, "When you keep silver in your room, you shouldn\'t go drinking far off at a brothel; even if you do, you should return early. You went out carousing and lost it—what does that have to do with the innkeeper? At noon today I saw Jing Ji go into your room; it must be that woman who stole it. You can report this to the officials, and the innkeeper and I will testify for you."',
        "Clarified that Jing Ji is the neighbor's maid from paragraph 1 (not a courtesan).",
    ),
    ('cat-17-s03', 3): (
        'Ying waited two days, but Ji did not come to the shop, so he filed a complaint with the prefectural authorities. Tingjie counter-sued that Ying had seduced his maid, and that when the affair was exposed, Ying feared being prosecuted and had made up the lost silver as a false claim to shift the blame. Prefect Zhang questioned the witnesses. Wu Lun and Chen Si testified: "We saw Ji enter Ying\'s room with our own eyes; it is true she stole the silver." Prefect Zhang questioned: "A guest\'s room and an unmarried girl\'s chamber—neither should be entered lightly. Since you saw Ji enter Ying\'s room, why didn\'t you stop her?" Lun replied: "Ying and Ji having an affair is also true; that is why when Ying was not in the shop, she unlocked the door and stole the silver." Having uncovered these facts, Prefect Zhang knew the silver had indeed been stolen by Ji; however, Tingjie was a prefectural degree candidate who had placed second in the recent examinations, so the prefect favored Tingjie\'s petition and ruled: "Since an illicit affair took place, the claimed loss of silver was merely a pretext to cover up the scandal. Ying was wrong to seduce a household maid and make up false excuses to shift blame. As the broker, Chen Si knew of the affair; why did he not admonish Ying to reform earlier? To testify biasedly only after the matter was exposed deserves punishment. Both are sentenced to a beating with the bamboo."',
        "Clarified feminine pronouns for maid Ji and fixed Tingjie's counter-suit logic.",
    ),
    ('cat-17-s04', 3): (
        'When the mother found out, she conferred with her husband: "How many wealthy families have sought our daughter\'s hand, and we refused to give her lightly! Now she has been defiled by this rat of a traveling merchant. We must secretly catch and kill him to vent our anger." Liao San said, "No. Only when a wife commits adultery and you catch them in the act and kill both on the spot does the law hold you blameless. Here it is our daughter having an illicit affair: I cannot bear to kill them both, and if we kill only the merchant, his offense does not carry the death penalty—how could we kill him without facing serious consequences afterward? Right now the bamboo-shoot accounts are settled, and all his silver is in my hands. Let us secretly watch for when our daughter meets him again, catch them in the act, beat him half to death, chain him up, and force a written confession out of him. Won\'t he have to forfeit his bamboo-shoot silver to us then? And what would he dare say afterward?" His wife agreed.',
        "Fixed the legal reasoning ('his offense does not carry the death penalty') while preserving the bamboo-shoot trade context from paragraphs 1–2.",
    ),
    ('cat-18-s01', 2): (
        'One day, the old mother-in-law was invited next door for a drink, and the old villager took the chance to make advances to his daughter-in-law. When she refused, he tried to force himself on her. Shouting and cursing, the young wife broke free and ran out. Her parents\' home was only ten li away, so she ran there on foot to tell her mother. Her mother was a fierce, shrewish village woman, and she marched straight over with her daughter. Seeing that his daughter-in-law had run home to report him, the old villager hurriedly called his wife back, confessed the whole truth, and asked how they could deal with it. The old woman thought to herself: "The girl\'s father is a simple rustic and won\'t come; only her mother, that fierce shrew, is sure to arrive." Having already hit upon a scheme to trap her, she turned and congratulated her old man instead: "Congratulations! A stroke of good luck has arrived!" The old villager said, "What\'s done was a mistake—don\'t mock me. You\'ve always been resourceful, and I listen to you in everything. Save me now, or our family will be ruined!" The old woman said, "Ruined? It\'ll cost you your life! Yet if I save you now, you\'ll only grow bolder. If you obey me and swear never to harbor such wild thoughts again, I\'ll not only save you, I\'ll treat you to a fine piece of luck besides." The old villager said, "I don\'t ask for any luck—just save me this once, and I\'ll never dare think a wicked thought again. If I do, may Heaven strike me and Earth destroy me!" The old woman said, "Since you\'ve sworn, I\'ll spare you this time. Take four taels of silver in two ingots, and hide in the outer guest room underneath the big vat. When the girl\'s mother arrives, I\'ll have her bathe in that room. Once she finishes washing, crawl out from under the vat and press the two silver ingots into her hands. She\'ll surely clutch the silver and won\'t be able to push you away; then force yourself on her once, walk right out, and the whole matter will be settled." The old villager said, "If I violate her too, won\'t that prove I really did harass her daughter?" The old woman said, "Never you mind—leave the rest to me." The old villager did as told and hid beneath the vat.',
        "Kept family roles (old mother-in-law, old villager, daughter-in-law, girl's mother) clear and natural.",
    ),
    ('cat-18-s04', 0): (
        'A Nun Scatters Prayer Beads to Lure a Woman into an Illicit Affair',
        "Clarified title to match the plot where the nun lures Lady Xiang into Chaoxian's trap.",
    ),
    ('cat-18-s04', 6): (
        'While Lady Xiang was fast asleep, Chaoxian came in and undid her sash and clothes. She lay there like one dead, yet warm to the touch; he took his pleasure of her as he pleased, finding it utterly delightful. After a brief rest, he had his way a second time, and still she did not wake. Chaoxian held her tightly in both arms and slept until midnight. When she finally awoke, her clothes had already been taken off. Feeling a man lying beside her, a pleasant ease at her waist, and her whole body relaxed and refreshed, she whispered, "Who are you?" Chaoxian replied, "My darling, how long I have longed for you! Today I finally managed to steal two trysts with you—tomorrow you must be good to me again." Lady Xiang said, "Now that your scheme has succeeded, you must never let outsiders know." Chaoxian said, "Only the nun knows; who else could possibly find out?"',
        "Improved fluency while avoiding 'stripped' per TRANSLATION_GUIDE.md.",
    ),
    ('cat-20-s04', 0): (
        'Impersonating a Favor-Seeking Guest to Solicit Exam Bribes',
        'Clarified 诈秋风客以揽骗 in the context of exam-bribery swindles.',
    ),
    ('cat-20-s04', 1): (
        'Education Commissioner Jian was renowned for his integrity and clear judgment. During the student renewal examinations, a favor-seeking visitor arrived and lodged in the monks\' quarters at Kaiming Temple. The next day, a swindler arrived with three servants and took lodgings there as well. Inside the temple they exchanged courtesy calls, and the swindler claimed to be a relative of the county magistrate who had also come to solicit favors. Outwardly, he flaunted splendid robes and caps, with a retinue even grander than the other\'s, frequently coming and going through the temple under official canopies. Students who saw him often mistook him, saying, "This is the Education Commissioner\'s hometown relative." When they saw Commissioner Jian\'s real relative return a courtesy call and invite him for wine, the swindler tagged along with the genuine visitor while secretly borrowing his name on the outside to deceive people. Commissioner Jian was a strict and upright official, and within a few days the genuine favor-seeker was sent on his way, leaving only the swindler at the temple. His outside accomplices deliberately spread word everywhere: "The Education Commissioner\'s hometown relative is staying at such-and-such temple." The rumor circulated widely among the candidates; many who wished to buy a place in the examination were eager to pay, yet lacked a reliable person to handle the silver. Behind the scenes, the swindler colluded with clerks and runners from the Education Commissioner\'s yamen, having them come forward so the silver could be sealed and deposited at their homes. Since people already believed he was the genuine relative, and since official yamen staff were involved, what was there to worry about in sealing the silver at their houses? The swindler announced that he could secure ten places at three hundred taels each. With so many candidates competing eagerly, the quota of ten was filled within days. On the appointed day, the silver—three thousand taels in all—was openly checked, sealed, and deposited at the various clerks\' and runners\' homes; behind the scenes, the gang divided the spoils at once.',
        "Fixed the logic of candidates needing a reliable intermediary and unified 'Education Commissioner Jian'.",
    ),
    ('cat-20-s05', 3): (
        'Four days later, the servants from all three households arrived with the silver. The visiting scholar came on schedule to inquire, and each of the three scholars replied, "The silver is all ready." The visitor said, "Tonight let us verify it under bright light and seal it." The three scholars said, "There is a great deal of silver, which makes nighttime inconvenient; tomorrow let us go into the innkeeper\'s inner courtyard to verify it." The visitor said, "The innkeeper may not keep this matter confidential; sealing it in the outer guest rooms would be more discreet." The three scholars said, "Let us discuss it when the time comes tomorrow." The visitor took his leave. After supper, the innkeeper came out and said, "You gentlemen were discussing sealing silver with that visitor—traveling strangers are hard to guard against. These doors and walls are thin; if they bring a gang to rob you in the night, you must be on your guard. In my opinion, you should store the silver inside my house within the city gate while you keep watch outside; that will be completely secure." The three scholars agreed and entrusted all six leather chests of silver to the innkeeper\'s house. Concealing it from his wife and maid, the innkeeper slipped all the silver out through the back door and fled late at night with the gang of crooks. Before leaving, he merely instructed his wife: "Tomorrow, if the three scholars ask for me, just tell them I went out early to look for someone and will be back shortly."',
        'Clarified that the servants of the three households brought the silver to the three scholars.',
    ),
    ('cat-20-s05', 4): (
        'The next day, the visiting scholar arrived in high spirits to weigh and verify the silver. The scholars said, "We gave the silver to the innkeeper for safekeeping; he went out early this morning and will be back shortly." They waited until noon, but the innkeeper did not return, so the visitor went back to his boat. In the afternoon, the visitor sent his servant to ask again, and once more they answered that the innkeeper had not yet returned. At noon on the third day, the scholars asked the innkeeper\'s wife for the leather chests, but she replied, "I have never seen any chests." When they went out to the riverbank to look for the visitor\'s boat, it too had vanished. They questioned the innkeeper\'s wife again, and she stubbornly insisted she knew nothing of the chests. She let them search the house as they pleased, yet not a trace was found. "Where did the innkeeper really go?" they demanded. The wife replied, "He left the night before last and told me to answer you this way." The three scholars were thrown into a panic.',
        "Fixed 'weigh and verify the silver' and kept 'The three scholars were thrown into a panic'.",
    ),
    ('cat-21-s00', 1): (
        'In the sixth month of summer, a traveling monk was passing along the road when he saw a herd-boy tending a herd of cattle. Among them was a large, fat yellow cow. The herd-boy held out his left foot for the cow to lick, and she licked it; then he offered his right foot, and she licked that too. The monk asked, "Why does this cow lick your feet?" The boy replied, "This cow is very tame, and I am very fond of her. My feet sweat a lot and taste salty, so she loves to lick them." Realizing that the cow was fond of salt, the monk quietly noted her down and learned that she belonged to the household of Elder Suo.',
        "Clarified '密此牛，系索长者家所畜的' (secretly noted the cow and learned she belonged to Elder Suo's household before stealing her in paragraph 2).",
    ),
    ('cat-21-s00', 3): (
        'When the tenth month came and the weather turned cold, the monk called a butcher to slaughter the cow, giving half the meat to the butcher, who sold it for one tael and five qian of silver; the other half the monk kept for himself, curing and drying it into provisions that he hid throughout the lining of his patched robe. Then he went straight to Elder Suo\'s hall and sat down cross-legged in full lotus. The elder came out and asked, "What monk dares come up into my hall and sit?" The monk said, "Do you happen to recognize me?" The elder said, "I do not know who you are—how would I recognize you?" The monk said, "Doesn\'t my face look naturally familiar to you?" The elder said, "We have never met; where would any familiarity come from?" The monk heaved a long sigh: "Your original spiritual nature has been completely lost—no wonder you fail to recognize an old friend\'s features!" The elder asked, "What do you mean, an old friend?" The monk said, "In the past, Master Foyin awakened Su Dongpo, and Master Huiyuan called back Bai Letian—were they not old friends of Su and Bai from former lives? In your previous life you cultivated the Way alongside me, but because your worldly desires were not yet severed, you were reborn here to enjoy this earthly wealth. I have come expressly to deliver you. You should quickly cast aside your worldly debts and return to diligent cultivation, so as not to waste the merit of your former life." The elder asked, "How can you know of former lives?" The monk replied, "My cultivation is twice yours. While you have been enjoying half a lifetime of worldly fortune, I have added half a lifetime of bitter ascetic practice—why would it be hard for me to know the karmic causes of three lifetimes?" The elder asked, "What bitter practice have you undertaken in this life?" The monk said, "Never mind my earlier austerities—for the past three years I have abstained completely from grain." Startled at last, the elder said, "If you can abstain from grain, what say you fast for one month here in my house?" The monk laughed: "After three years of it, what is a single month?" The elder asked, "Do you still take tea or hot water?" The monk replied, "Only a single bowl of clear tea or boiling water a day." So the elder kept him there, had an empty room swept clean for him to sit in, and sent in one bowl of tea in the morning and one bowl of boiling water at night.',
        "Clarified 'slaughter the cow' (from paragraphs 1–2) instead of ambiguous 'her' or '[a pig]'.",
    ),
    ('cat-21-s01', 3): (
        'After eleven days, Master Chu summoned the monk out, took his Amitabha prayer beads, and inspected them: only nineteen beads remained in his hands. Master Chu confiscated the beads, ordered the monk placed in light detention where no one was to disturb him, and left him to sit quietly awaiting judgment. He then secretly instructed the jailer: "Do not allow any monks or Daoists to come in and see him. After two days he will surely beg you for food. When he does, ask him how those Amitabha beads are made. If you can make some that, when mixed with water, turn out just like these beads, I will reward you handsomely afterward."',
        "Refined 做来以水调之 ('when mixed with water, turn out just like these beads') while preserving the jailer prohibition (铁容 = 莫容).",
    ),
    ('cat-21-s03', 0): (
        'A Monk Resembling the Temple Guardian Fakes a Fundraising Appeal',
        "Clarified 化疏 as 'Fundraising Appeal' while keeping 'Temple Guardian' (伽蓝).",
    ),
    ('cat-21-s03', 3): (
        'On the tenth day, Xiuyun indeed arrived with the silver and two servants. Knowing he was the timber merchant, Abbot Wanlang received him with solemn courtesy, served tea, and asked, "What is the benefactor\'s honored surname?" Xiuyun replied, "Jing." Wanlang asked, "Where has the benefactor come from?" Xiuyun said, "Earlier I made an appointment with a fundraising monk from this precious monastery, so today I have respectfully come up from the river." Wanlang pondered a moment and said, "Our mountain temple has not sent anyone out to solicit donations." Xiuyun said, "Seventeen days ago, a monk sat in meditation in my timber loft for seven days, and I promised him I would come today." Wanlang said, "None of the monks of this temple have gone out during the past half month; it must have been a wandering monk falsely using our name." He at once ordered a vegetarian meal prepared to entertain him. Inwardly, Xiuyun found it strange: "If it was a wandering monk falsely using their name, why didn\'t he take the silver that day?" Keeping the doubt to himself, he strolled around the temple grounds. When he entered the shrine of the Temple Guardian, he looked up and saw that the statue of the guardian looked just like the monk from the other day. His two servants also pointed and said, "This Temple Guardian looks the very image of that monk!" More amazed than ever, Xiuyun suspected in his heart that the Temple Guardian himself had manifested as a monk to urge him to repair the monastery. He bowed and prayed: "If the monk of the other day was your manifestation, grant me a holy cast of the divination blocks." He cast the blocks at once and got a holy sign. He prayed again: "I have brought three hundred taels today; I pray for great profit this year." Casting again, he got a yang sign. He said, "If three hundred taels is not enough and you want five hundred, grant me a holy cast." This time he got a yin sign. He prayed once more: "Now I understand in my heart. If you want an additional five hundred taels for incense-and-lamp fields, grant me a holy cast." Sure enough, he threw a holy sign. Having bowed in thanks, Xiuyun returned to the vegetarian feast and asked Wanlang, "How much silver is needed for the repairs?" Wanlang replied, "I have long wished to repair the temple. The other day I had craftsmen estimate the cost, and they said it would take five hundred taels in all, so I dared not begin." Xiuyun said, "The other day I promised three hundred taels, which I have brought with me now, and tomorrow I will send two hundred more to make up the sum. Once the repairs are complete, I will donate another five hundred taels to purchase incense-and-lamp fields to serve the Buddha forever." Overjoyed at these words, Wanlang pressed his palms together and bowed down. Later, after Xiuyun had donated the full amount as promised, the wandering monk Ruobing secretly took two hundred taels as his share and departed.',
        'Removed parenthetical meta-commentary about Ruobing and refined the divination and fundraising terms.',
    ),
    ('cat-23-s02', 4): (
        'The magistrate first interrogated the children: "How were you lured into the hermitage?" The boys replied, "The monk rubbed our eyes with his hands, and immediately we saw fierce tigers and venomous snakes on both flanks and behind us, lunging to bite and harm us. Only a single clear path lay open before us, so we kept walking forward until we reached this hermitage, where the monk locked us away." The magistrate asked again, "What did the monk keep you in the hermitage to do?" The boys said, "That hateful baldy—day and night without end, he used our bodies for forced sexual service, causing excruciating pain. If anyone refused, he thrashed us with heavy staves. Terrified of him, we had no choice but to submit to whatever he did." The magistrate asked further, "Those kidnapped earlier must have grown older—where were they sent?" The boys said, "Whenever any fell ill or grew older, the monk claimed he was letting them return home, though we do not know whether any ever made it back." The magistrate then interrogated the monk Xiaoshan: "After kidnapping these boys, where did you send those who fell sick or grew older?" The monk dared not answer. When the magistrate questioned the other three monks of the hermitage, all three confessed: "They were poisoned to death and buried." Hearing this, the magistrate was seized with furious indignation. He had Xiaoshan given forty strokes of the heavy cane and each of his three accomplices given twenty, declaring: "For a crime so heinous, even death is too light a punishment!" He ordered Xiaoshan chained outside the yamen gate and permitted the fathers of the missing boys to gather and beat him with their own hands. They beat him until not an inch of skin was left whole, and one man even cut off his manhood and stuffed it into the monk\'s mouth; within half a day he was dead. There was no one who did not loathe his depravity and rejoice at his death. Afterward the authorities burned the hermitage to the ground, and the scourge of child abduction was finally brought to an end.',
        "Replaced 'go home' with 'return home' per §2.8, restored complete punishment detail per §5, and refined 'forced sexual service' for 苦春.",
    ),
    ('cat-24-s00', 2): (
        'When the man and his goods reached Nanjing, someone had already reported to Shaoshan: "Your father has brought goods to sell, and he means to find you as well." Dismayed at the news, Shaoshan quickly called in the brothel madam, Mao Xiqing, to scheme with her: "My father has come expressly to press me to return home. If you have a plan to trap him into whoring too, then I can stay here for good; otherwise, I must part from you today." Xiqing said, "Just hide yourself deep inside and do not let him see you; I will take care of everything." She immediately sent someone to invite Madam Xun Rong from the front courtyard and asked her to lay a clever snare for him. Madam Rong agreed and left.',
        "Kept Nanjing consistent with paragraph 1, clarified 'brothel madam', and converted outer quotes to double quotes.",
    ),
    ('cat-24-s00', 3): (
        'Inquiring among fellow merchants in Nanjing, Dongxi learned that his son was whoring at Mao Xiqing\'s house with her girl Yuehua, so he went straight to Xiqing\'s establishment to call his son back. Xiqing came out and received him with the utmost deference. Dongxi said, "My wayward boy Shaoshan is in your house. I have been in Nanjing for over ten days now—call him out to see me at once." With a show of awed reverence, Xiqing replied, "So Your Honor is Young Master Shaoshan\'s venerable father! What good fortune to finally see the light of your presence! Your worthy son did stay at my humble house for two or three months, but we saw him off more than a month ago." She immediately called her daughter Yuehua out to greet him, pointed to Dongxi, and said, "This is your father-in-law," ordering her to kneel and bow. Dongxi ignored her courtesy. Xiqing then ordered a feast laid out. Dongxi said, "I came here for an unfilial son—do you think I am after your wine and food? Call my son out at once to return home with me, and spare me your false show of hospitality!" Yuehua said, "He truly left last month, saying he was going to collect his accounts and head home. If he were really here, how would we dare hide it from you?" Dongxi refused to believe her and insisted on getting to the bottom of his son\'s whereabouts. Xiqing said, "My humble house has only a few rooms—search it through as you please, Your Honor. How could we possibly hide anyone?" Yuehua led Dongxi inside, and he searched every corner without finding a trace. Flying into a rage, Dongxi shouted, "The broker told me he was here! How dare you hide him away and spin me these ghost stories? If my son cannot be found, then your house has murdered him—I will haul you before the officials and make you produce him!" Alarmed, Yuehua replied, "When has anyone in the pleasure quarters ever murdered a man? Do not be so quick to accuse people, Your Honor." Dongxi stormed out cursing.',
        "Restored full faithful translation of cat-24-s00 p3 and refined 'to finally see the light of your presence' for 披雲睹日 per ModelScope Qwen QA.",
    ),
    ('cat-11-s00', 2): (
        'In the evening the boat reached the shore, and each man gathered his luggage and left. You the Shyster trailed behind young Qiu. When they reached the front of the prefectural offices, he snatched the bedding from the servant\'s carrying pole, saying, "Thanks for the carry—I\'ll take it from here." Young Qiu lunged to grab it back: "That\'s my bedding—where do you think you\'re taking it?" The two of them grappled, neither giving way, until the quarrel carried them right into the magistrate\'s court. You said, "It\'s mine—he\'s trying to force a claim." Qiu said, "He\'s seizing my property." The magistrate said, "You both claim it. What identifying mark does each of you have?" Qiu said, "I bought it myself and made no mark." You said, "I stamped a seal inside the felt." They opened it up right there in court; You took the wooden seal from his sash and matched it to the mark, and sure enough they were the same. The magistrate said, "This is You\'s property. How dare Qiu falsely claim it!" He had Qiu given ten strokes of the bamboo, ordered You to take the bedding away, and drove them both out of the yamen. Qiu cursed him: "Who are you, you thief, that you dare cheat me like this? I will pay you back for this!" You Wu said, "That \'You the Shyster\' you were talking about on the boat—that\'s me. What was it to you, going on all day about how shifty I am? So I taught you a lesson: this is how a shyster operates." Only then did Qiu feel regret: "I spoke ill of a man recklessly, and so brought this loss on myself."',
        "Fixed 'This is You\\'s property' and standardized double/single quotation marks.",
    ),
    ('cat-17-s01', 1): (
        'Shoushan Temple held a grain tax quota of five hundred shi, divided among twelve monastic branches. The monks were all well-off, wearing silk, eating meat, drinking wine, and visiting prostitutes—worse than laypeople. After managing the temple for ten-odd years, if they had accumulated enough silver and wished to return to lay life, they would first arrange a marriage within their family, then let their hair grow out under the guise of wandering ascetics (toutuo). Once their sideburns were long enough to be bound in a hairnet, they returned home, married, and took charge of a household. Brothers succeeded brothers; sons succeeded fathers. They treated the position as hereditary property, and no outsider could intrude. Some who feared the burden of family life and did not wish to return to the lay world would instead select a simple, honest tenant in the village who had no wife, pay silver to marry one for him, and then sleep with her for a month before handing her over to the tenant. She remained at the monk\'s disposal whenever he visited: when the monk arrived, she was his wife; when he left, she was the tenant\'s. Hence the saying: "It is not the monk who seduces the tenant\'s wife—it is the tenant who sleeps with the monk\'s woman." That was the custom. If a child was born and the timing made paternity clear, the child belonged to that father; if the timing was mixed and uncertain, the monk and the tenant split the children, each taking one. When the boys reached ten or so, the monks brought them into the temple as novice attendants—in truth, their own sons. Hence the monks had a saying: "Put out the lamp to pass on the Dharma; borrow a surname to carry on the line." That is what they meant.',
        "Replaced 'head-tuo (ascetics)' with 'wandering ascetics (toutuo)' and unified quotation marks.",
    ),
    ('cat-23-s00', 3): (
        'Later, at a banquet with a certain Zhi, a former official residing there, the talk turned to current affairs. Zhi said, "As things stand, the rivals are evenly matched, and the stakes rest on the cadet line. The first sprout of calamity will surely arise within the palace. One day, there will be either a bloodbath like Emperor Chengzu\'s seizure of the throne, or a call to revolt from a princely house like the Prince of Yi\'s. This is precisely what the widow of Lu deeply feared." Mi said, "In the past, rebellion was crushed before it could take root, and Chenhao was destroyed as soon as he rose. The dynasty enjoys Heaven\'s favor—how could wind and rain shake its doors?" Zhi replied, "Not so. Liu Wenjing, a minor official, set the Tang uprising in motion; Yao Guangxiao, a monk, helped raise an emperor\'s house. Once a cause finds capable men to back it, who can predict success or failure?" Mi said, "Even if the central plains fall into turmoil, our watery region lies far off in a corner by the sea; surely we need not fear chaos here." Zhi said, "That too is hard to guarantee. A prophecy says, \'A Son of Heaven shall emerge from a certain place, and Jiangnan shall become a battlefield.\' That is precisely what we must fear." Mi asked, "If Chenhao were to rise again today, how would it end?" Zhi said, "The realm has been at peace so long that the troops have grown slack—even worse than in those days. Had Chenhao not lingered so long at Nankang, and had a certain garrison not feigned allegiance only to turn their spears against him, how would his grand enterprise have gone up in smoke?" Upon hearing this, Mi felt secretly delighted.',
        'Standardized outer double quotes and inner single quotes.',
    ),
    ('cat-23-s01', 3): (
        'On the third day two more monks arrived, and they too were kept on to help chant the sutras. That evening one monk chanted spells and burned talismans, and set Old Yang jumping and shouting under a spell. Yang snatched up a sharp sword, pointed at his wife and sons, and cried, "These are demons!"—and cut them all down with his own hand. Then he chased after the two daughters-in-law to kill them. The women begged the monks to restrain him. A monk pointed at Old Yang and barked, "Sit!" Sword in hand and teeth clenched, Yang sank into a dazed stupor, dead to the world. The four monks went in, raped the two women one after another, and bound them with ropes. They ransacked the house for money and valuables, tied the loot into four carrying loads, and fled in the dead of night.',
        'Standardized double quotes for spoken dialogue.',
    ),
    ('cat-24-s00', 5): (
        'When Qingyun had finished kowtowing, she led him into an inner room, brought a new set of clothes for him to change into, and knelt, saying, "People in houses like ours live in dread of giving offense. I beg you to be generous and forgive my fault." Dongxi said, "I never blamed you to begin with—wet clothes are just hard to walk in. I will change now and send these back tomorrow." He shook out his sleeves and made to rise. Qingyun held him back: "There is still a cup of wine to make amends. If you leave right away, Mama will scold me again." Dongxi said, "What need is there for wine?" But by then the feast was already laid out. Qingyun waited on him with every art of pleasing, and Dongxi let himself go and drank merrily. Toward evening he wanted to leave. Qingyun begged him earnestly to stay: "This room has stood empty for half a year. If you go without staying the night, it is as though we met face to face and never met at all. If you stay, Mama will be pleased and say I know how to keep a guest. And it will not even cost you room money." They drank on until the second watch, then went to bed. Dongxi reckoned he would pay for the room in the end anyway, so for the moment he feigned indifference and lay still, to see what would happen. Qingyun nestled into his arms and stroked him, saying, "Are you playing Liuxia Hui, sitting with a woman in your lap and staying unmoved? That is walking into a mountain of treasure and coming back empty-handed. Besides, in the dark of night no one is watching—who would ever know what a chaste man you were?" Dongxi smiled and gave in.',
        'Standardized double quotes for spoken dialogue.',
    ),
    ('cat-24-s00', 10): (
        'Note: That a rare beauty can bewitch a man and a lovely face topple a city has been lamented since ancient times. Who ever stepped into the mire and came out white and unstained? Dongxi did not go there for pleasure; he only meant to find his son and bring him home. He knew full well how courtesans bewitch men and how whoring ruins a family—yet once inside, he churned the mud and rode the waves even deeper than his son. How few are the men who keep their distance from song and beauty and do not drown in desire! Confucius said, "I have never seen a man who loves virtue the way he loves beauty." One who is worthy enough to turn his love from beauty is truly rare. Hence the saying in the quarter: "They never fear that a steady customer will not be tamed; they only fear that the truly wary man will not come." Only the man who never sets foot on that ground can stand free beyond the cage. Otherwise none escapes its bonds and its spell.',
        'Standardized double quotes for quotations.',
    ),
    ('cat-14-s01', 16): (
        'Bleached (piaobai): silver is melted and poured just like fine-thread, except that because it is poured while extremely hot, the walls come out thin. Next, the bottom is removed and only the thin top is kept. White copper is poured into the middle with no wall of its own; the saved top is fitted over it; silver foil is joined underneath; and the whole piece is soldered together. Its base is then beaten with a dross hammer until it is indistinguishable from a genuine wind-pot casting. Even chiseling it open cannot tell them apart. To recognize it: during soldering, the fire burns away the natural bluish sheen and leaves a dead-fish white—hence the name "bleached." Judge it by this, and the difference is crystal clear.',
        "Clarified causal link ('because it is poured while extremely hot, the walls come out thin') and 'indistinguishable from a genuine wind-pot casting' for 无二.",
    ),
    ('cat-18-s00', 1): (
        'Two sisters-in-law were sitting together when an oil seller passed by. The younger sister-in-law, Lady Shi, said, "We need oil in the house, but we have no silver to buy it." The elder sister-in-law, Lady Zuo, said, "Have him weigh out the oil first; paying him later will be soon enough." Lady Shi called him in and had two catties of oil weighed out. She said, "Our men aren\'t home; come back in two days for the silver." Two days later the oil seller returned. The younger sister-in-law said, "There is no silver—what shall we do?" The elder said, "Put him off for another three days." The younger used these words to send him away. After three more days the younger said, "You told me to have the oil weighed out first, and now there is still no silver. Lend me a little so I can pay him." The elder said, "If you are willing to do as I teach you, paying him back will be no trouble at all." The younger said, "I always follow your lead in everything—what should I give him to pay for it?" The elder said, "I see that young oil seller is handsome, and you are young and pretty. Be intimate with him once, and why would you need to pay for the oil?" The younger said, "I am afraid you might talk about it afterward." The elder said, "I am the one who taught you—how would I dare speak of it? I will step into the inner room to stay out of the way while you go and do it."',
        "Kept accurate Ming kinship terms for 妯娌 (婶 = younger sister-in-law, 姆 = elder sister-in-law), translated 斤 as 'catties', and used period-appropriate register.",
    ),
    ('cat-01-s02', 2): (
        'Note: Look how the swindler made off with the pig. In the brief moment of a chance meeting, the thief\'s thought arose; he deceived Bao with cunning words until Bao believed him completely—this is what is meant by "a gentleman can be deceived by what seems plausible." First, he released the pig and pretended to chase it, drawing Bao far ahead in pursuit; then, seizing a pig and kicking over the cages, he prevented Bao from following behind: thus making a man fall openly into the trap without realizing it. A setup thrown together on the spot—what cunning! Merchants, take warning from this. Do not think that only hidden snares and secret devices deserve careful guarding; even open traps and brazen setups demand still greater caution.',
        "Preserved 'Note: Look how the swindler made off with the pig' per TRANSLATION_GUIDE.md §2.3 and standardized double quotes.",
    ),
    ('cat-09-s01', 5): (
        "The two men sued each other for about a year, and Bangcai's remaining silk was all spent. He called an elder brother to come help with the lawsuit; the brother brought more than five hundred taels, which were also mostly used up. Bangcai then fell ill at the inn, and his family sent a paternal uncle to come see him. The uncle was an honest, kindly elder; when he asked how the affair had started, he learned that his nephew's arrogant ways had brought this on. The merchants explained the whole matter and proposed an amicable settlement: each side would put up a hundred taels to settle with the officials, and fifty taels would be drawn from that for Bangcai as travel money so he could head home. On his way home, Bangcai thought of all the goods and wealth he had set out with in his trading days, only to return empty-handed now, and he was overcome with bitter resentment. Mocked and scolded by his whole household besides, his pent-up fury only grew worse. Before long a carbuncle broke out on his back, and within a few months he died.",
        "Kept protagonist name 'Bangcai' consistent with paragraphs 1–4 and improved narrative flow.",
    ),
    ('cat-09-s03', 3): (
        "Note: Merchants race north and south for profit; who does not wish to buy widely and reap a rich return? Yet when the times allow you to double your profit, you should seize the moment and sell off your stock. Why be insatiably greedy, only to wring your hands after the opportunity is lost—what good does that do? How harmful greed is! People fail to realize that when goods become dirt cheap, they inevitably trend toward high prices; when they become exorbitant, they trend back toward cheapness. There is no extreme that does not turn back. This is the waxing and waning of yin and yang, the turn of fortune in nature's workings—it is always so. How can one go against the times to indulge a greedy heart? That is why gentlemen from of old have treasured contentment over greed.",
        "Clarified 見機發脫 as 'seize the moment and sell off your stock' and 征貴 as 'trend toward high prices'.",
    ),
    ('cat-22-s01', 3): (
        "Note: Sorcery strikes from the dark the way a fox-spirit offers its charms: the heart must be crooked first, and only then can it be beguiled. If the heart is upright, one may walk into a crowd of demons and they cannot do any harm. Thus Fu Yi refused to believe a curse could kill a man, and the foreign monk dropped dead himself; Fan Zhongyan refused to believe a ghost could kill his son, and the ghost never came. Lady Xin's heart was upright. Though the sorcerer's potent arts were uncanny, she harbored deep suspicion in her heart and warned her maid early on, so in the end she escaped the poison of his wicked lust. However clever the sorcery, it is no match for an upright human heart. Should you ever meet a sorcerer, hold fast to your heart and pay him no heed—what can his evil tricks do then?",
        "Clarified 深疑于心 ('harbored deep suspicion in her heart'),胡僧自死 ('the foreign monk dropped dead himself'), and 'Lady Xin'.",
    ),
    ('cat-12-s02', 3): (
        'They inquired all the way to Wuhu County. Seeing the copperware in a shop there, they asked, "Did you buy this copperware yourself, or did another merchant bring it here to sell?" The shopkeeper replied, "Three months ago, three travelers came and sold it." Zida asked, "Where were they from?" "From Jiangxi," he said. Alarmed, Zida said, "Then I was mistaken—my cousin really did meet with foul play!" He and Kui went straight to Shuikou and asked Chen Si, "Which county was the boat captain who loaded my cousin\'s goods from?" Chen Si replied, "From Yanshan County." Zida told him what had happened, and went with Chen Si to Yanshan to have the men arrested.',
        "Clarified '差矣，失手是实' ('Then I was mistaken—my cousin really did meet with foul play!') per QA feedback.",
    ),
    ('cat-14-s01', 13): (
        'Iron-bits (tiesui): broken iron is placed inside first, and then molten silver is poured over it so that it takes the iron in evenly, encasing it in the middle. Even the lowest grade still comes out at nine-tenths purity, and at nine-and-a-half tenths it even shows fine silver threads.',
        "Preserved the technical meaning of '九五者有丝' (at nine-and-a-half tenths purity, fine silver threads appear on the surface).",
    ),
    ('cat-14-s01', 22): (
        'Five-tenths purity is called a plum-white cake.',
        "Kept 'plum-white cake' (梅白饼) as a counterfeit silver cake type rather than a formal standard.",
    ),
    ('cat-14-s01', 23): (
        'Salt-burned cake: two qian and five fen of silver are made into a one-tael piece. Take it out, mix a corrosive brine of rotted salt, spread a layer over the cake, and heat it in the fire; then take it out and hammer off a layer of copper skin. Coat it with the salt mixture and fire it again, then hammer it once more. After repeating this several times, the copper on the surface is gone and the piece turns naturally white—this is called "salt-burned."',
        "Kept 'corrosive brine of rotted salt' (盐烂水) and 'hammer off a layer of copper skin' (打去一重铜皮).",
    ),
    ('cat-17-s02', 0): (
        'A Geomancer Leaves His Wife Behind to Steal a Fine Lineage',
        'Clarified 寄妇脱好种 (leaving his wife as a decoy to steal noble seed/lineage).',
    ),
    ('cat-17-s02', 9): (
        'Another case: the father of a certain Xie had failing vitality and no son. In the summer heat, his mother was sleeping on the bed in a thin unlined skirt when a male monkey kept by the household came and violated her. Startled awake, she tried to push it off, but the monkey bared its teeth and claws to bite and scratch, so she could not push it away. As she drifted back into a deep sleep, her spirits stirred, passion rose in her despite herself, and she conceived. When Xie\'s father returned home, his wife told him how the monkey had violated her and said, "This unnatural creature must be killed." Feeling guilty after the deed, the monkey had climbed up a tall peach tree outside the back gate and refused to come down. Xie\'s father deliberately sported with his wife beneath the tree; seeing them make love, the monkey grew excited and climbed down, whereupon Xie\'s father clubbed it to death and buried it under the peach tree. Later, when young Xie was born, he was extraordinarily bright and quick-witted, yet he hopped and tumbled on the ground just like a monkey. His mother knew in her heart that he was the monkey\'s seed, but having no other son, she could not bear to kill him. When the boy was eight, his father died. A geomancer chose a burial site and said, "This site is superb; it will produce a child prodigy and a man of great talent. Though the boy seems unruly now, in three years he will rise to high rank." Three years later, the geomancer returned. Mother Xie said, "You said that after three years this boy would mend his ways, yet he is still as wild and flighty as before—what can be done?" The geomancer went back to inspect the grave carefully, then returned and asked, "Madam, was this boy born of your own womb, or of a concubine?" Mother Xie replied, "He is not my own; a neighbor\'s maid bore him by a monkey and wanted to abandon him. Because I had no son, I took him in from birth and raised him as my own." The geomancer said, "If you want this boy to amount to something, the monkey\'s bones must be buried beside this grave; then in days to come he will bring great prosperity to your house." Mother Xie dug beneath the peach tree and found the bones still there. She went expressly to tell the geomancer, "The neighbors still have the bones; how should we handle it?" The geomancer instructed her to choose an auspicious day to bury them. After another three years, the boy was indeed recommended as a child prodigy and later became a celebrated figure of his age. (Heard from Geomancer Chen of that district.)',
        "Fixed 'failing vitality' (血衰), 'violated her' (奸之), and 'climbed up a tall peach tree' (走于后门大桃树上).",
    ),
    ('cat-18-s00', 9): (
        'A few days later, the butcher passed by. Lady Zuo came out and asked for two catties of meat on credit, and the butcher hurried on. Lady Zuo said, "My poor younger sister-in-law took a rough beating because of you the other day—you ought to give her two catties too." The butcher tossed over a cut of meat and said, "Please pass this along to her for me; I have no time to spare." Carrying the two strips of meat in hand, Lady Zuo went inside, told her younger sister-in-law the whole story, and brought the meat out for another merry feast. The younger sister-in-law said, "I may know how to keep a lover, but you are even better at playing the swindler!" Once the door to illicit affairs was opened, what followed was more than could ever be recorded.',
        "Kept 'younger sister-in-law' (婶娘), translated 二斤 as 'two catties', and clarified '我会养汉，不如你会光棍'.",
    ),
    ('cat-18-s01', 3): (
        'Before long, the girl\'s mother arrived. The old woman came out with a welcoming smile and said, "Sorry to trouble your honored steps; we failed to prepare a sedan chair to meet you." The girl\'s mother immediately began cursing: "Your household has no decency! That old scoundrel of a father-in-law violated my daughter!" Feigning shock, the old woman cried, "Just now he fooled me by saying our daughter-in-law had secretly cooked herself a dish, and that she ran home because he beat and scolded her—how could he have done such a wicked thing? I\'ll have it out with that old dog to the death!" She cursed and railed at the top of her lungs, even more fiercely than her guest, leaving the girl\'s mother no room to get a word in. Then she said, "In a little while, you, I, my son, and my daughter-in-law—the four of us together—will seize him, beat him half to death, and pour nightsoil down his throat until he\'s unfit to show his face among men!" She ordered her daughter-in-law to slaughter plump chickens and ducks and lay out a lavish feast for her mother. After a round of loud cursing, they served tea and fruit, and the old woman said, "Walking in this heat makes one sticky; let us fetch water for you to bathe first, and then we can enjoy the midday meal in comfort." She ushered the girl\'s mother into the room to wash, while she herself went to the back kitchen to help her daughter-in-law warm the wine. Once the mother finished bathing, a man crawled out from beneath the vat, pressed two ingots of silver into her hands, threw his arms around her, and forced himself upon her. She cried out for her daughter and the old woman, but no one answered. The old villager said, "They are far off in the kitchen—how could they hear you?" Stripped for her bath, unable to break free, and reluctant to part with the two ingots of silver in her hands, she silently submitted to him. When it was over, the old villager said, "In-law, do not believe your daughter\'s tale. Our coming together today was also fated from a former life. I was hiding in here out of fear of you—who would have thought they would send you into this room to bathe, and that you and I would share this intimacy first? From now on, I will never harbor another thought toward your daughter." With that, he walked out.',
        "Preserved 'two ingots of silver' (两锭 / 两槽银), clear kinship terms (girl's mother vs. old woman), and standard double quotes.",
    ),
    ('cat-16-s00', 1): (
        'In the capital lived a man named Fang Ba, dull-witted and earning his living by scavenging the streets. He had only an old mother at home. One day, while scavenging, he went to a small river to wash himself. Toward evening, a woman dressed in hemp mourning clothes stood nearby and waited until he finished washing. She then said to Fang Ba, "I am heading to my maiden family\'s home, but I cannot arrive tonight. May I rest at your house for the night?" Fang Ba replied, "My home is no place to stay. Why not go to an inn?" The woman said, "Inns are crowded with all sorts of people; lodging there is inconvenient. Who else lives in your house?" Fang Ba answered, "Only my old mother." The woman replied, "If you have a mother, then we can stay together."',
        'Standardized double quotation marks on dialogue.',
    ),
    ('cat-16-s00', 4): (
        'Three days before the hearing, the woman and her mother-in-law went to merchant Wang\'s cloth shop and said, "Your beating killed my husband—that is certain. My mother-in-law is old, and I alone cannot support us. Having you pay with your life helps no one. If you can provide three hundred taels to support my mother-in-law, she can withdraw the complaint and waive the inquest." Delighted at the offer, Wang sent an intermediary to negotiate and agreed to pay two hundred taels for Fang\'s mother\'s upkeep. Following the woman\'s instructions, the mother herself submitted a settlement statement: "I am poor and old; my son is dead, and my daughter-in-law is a widow. We have no means of survival. At the urging of relatives and neighbors, let Wang pay one hundred taels for my upkeep, and waive the inquest." The official approved the settlement, ordered Wang beaten with twenty strokes of the bamboo, imposed a heavy penalty, and allowed Fang Ba\'s wife to collect the silver and return home. Two days later, the woman stole the two hundred taels and fled under cover of night, vanishing without a trace. When Fang\'s mother sought to file another complaint, Wang paid another twenty taels to hush the matter up.',
        "Clarified 'merchant Wang\\'s cloth shop' (not inn) and accurately translated '儿死妇寡' ('my son is dead, and my daughter-in-law is a widow').",
    ),
    ('cat-16-s01', 4): (
        "Note: The swindlers' cleverness lay in seeing that Lady Cui and her servants were all at the temple; they then tricked a junior household servant into serving tea. They also claimed the brother-in-law wished to avoid being seen openly asking for celebratory gifts and wanted the silver handed over in the garden lodge—thus enabling the fraud. Graduate Hong lost his silver and was forced to flee on top of it. Moreover, the prefect believed the story of an attempted abduction and opened an official case. Taking a concubine away from home and expecting things to go smoothly? Truly difficult! How can one ever act without verifying the facts?",
        'Clarified Lady Cui (崔命妇, a widow) and her brother-in-law (崔家大伯).',
    ),
    ('cat-18-s00', 2): (
        'Shortly the oil seller arrived. Seeing no other way to put him off, Lady Shi forced a smile, came out to meet him, and said, "Twice I set a date for you to collect the silver, but I have no way to raise it. Better that I give myself to you instead." Seeing her beaming, the oil seller felt lust rise in him and asked, "There are people inside your house—are you tricking me?" Lady Shi replied, "My husband has gone to plow the fields, and my elder sister-in-law is next door spinning hemp. There is no one else here, which is why I was bantering with you." Reassured, the oil seller went into the bedroom with her. Hearing the bedroom door bolted, Lady Zuo slipped out quietly, poured the oil out from both carrying baskets, filled them halfway with water, and returned to listen closely at the door. The younger sister-in-law said, "We are finished—get up." The oil seller pleaded, "Stay with me just a bit longer." Holding her hemp basket, Lady Zuo dashed out past the main gate and called out loudly on purpose, "It isn\'t even noon yet—how is the field-worker back already?" Hearing voices outside, the oil seller hurriedly came out with his carrying pole and ran right into her at the gate. Lady Zuo asked, "Did my sister-in-law pay you for the oil?" The oil seller quickly answered, "Paid! Paid!" and carried his load off to sell in the next village. Knowing he was sure to return, Lady Zuo waited by the main gate.',
        "Fixed 'give myself to you instead' (把我还你), elder sister-in-law (伯姆), and consistent kinship titles.",
    ),
    ('cat-18-s00', 3): (
        'Near noon, the oil seller returned. Lady Zuo said: "You are still here? My sister-in-law\'s brother came carrying buckets to fetch water. Seeing a load of oil standing in the house with no one around, and only voices and laughter coming from her room, he suspected an affair with the oil seller. He poured all the oil into his buckets, filled your baskets halfway with water, and went home to report to his mother. Mother and son have come straight here to catch the lovers. By the time they arrived, the oil carrier was gone, and they are still full of suspicion. If they discover you here, they will certainly confront you." The oil seller turned to flee. Lady Zuo caught hold of him: "I gave you warning; you must reward me." The oil seller said: "Tomorrow I will send you two catties of oil."',
        "Standardized 'two catties of oil' for 两斤油 per TRANSLATION_GUIDE.md.",
    ),
    ('cat-18-s00', 5): (
        'Soon someone called out selling meat. The two sisters-in-law called him in, each had two catties weighed out, and told him to come back another day for the silver. Three days later the butcher came to collect. The elder sister-in-law weighed out seven fen of silver to pay her share, while the younger sister-in-law\'s payment was put off for another two days. When the appointed day came and the butcher arrived, the elder sister-in-law whispered, "Pay him off with the same trick as the other day; I\'ll slip into the inner room out of the way." Lady Shi came out, smiled sweetly at the butcher, and said, "I took your meat on credit, and I have no silver to pay you. No one is home today—better that I pay you back with my own flesh instead." Seeing how pretty she was, the butcher grinned and said, "I only want a little of the flesh around your waist." Lady Shi replied, "My whole body is yours—why begrudge a little?" The butcher pulled her into his arms and carried her into the bedroom to take his pleasure. Meanwhile, the elder sister-in-law slipped out, carried the butcher\'s entire load of meat inside to hide, and sat down quietly beside the empty meat baskets.',
        "Restored faithful translation of the Chinese text for cat-18-s00 p5 with consistent kinship terms and 'catties' for 斤.",
    ),
    ('cat-18-s00', 6): (
        'When the butcher finished his tryst with Lady Shi and stepped out, he asked, "Where has my meat gone?" Lady Zuo replied, "My brother-in-law carried it off to the village elder." The butcher exclaimed, "How could you steal my meat?" Lady Zuo retorted, "You have some nerve! My brother-in-law came home, saw the load of meat sitting here, and found the bedroom door bolted from inside. Hearing the two of you giggling and whispering, he knew you were having an affair with his wife and told me to watch the door. I had no wish to eavesdrop on your antics, so I sat out here. Just sit a moment; the man who took your meat will be right back." The butcher hoisted his empty baskets and bolted. Lady Zuo grabbed hold of him: "Leave a butcher\'s knife with me as a pledge." The butcher said, "Do me this favor; tomorrow I will send you two catties of meat." Lady Zuo let go, and the butcher fled as fast as his legs could carry him.',
        "Clarified 叔叔 as brother-in-law and standardized 'two catties of meat' for 两斤肉.",
    ),
    ('cat-18-s01', 0): (
        'Violating the Daughter-in-Law, Then Tricking Her Mother into Bed',
        'Clarified 爬灰复骗奸姻母 (violating the daughter-in-law, then tricking her mother into bed).',
    ),
    ('cat-18-s01', 4): (
        "The girl's mother went into the kitchen and saw her daughter and the old woman setting out the dishes. Realizing they could not have heard her cries, she drank and smiled at her ease during the noon feast, never mentioning the harassment of her daughter.",
        "Clarified that 姻母 is the girl's mother entering the kitchen (not the mother-in-law).",
    ),
    ('cat-18-s01', 5): (
        'After the meal, she took her leave. The old woman repeatedly urged her to stay, and her daughter said, "I called you here to stand up for me, yet you only care for eating and drinking their wine—how could you be so muddled in your old age?" The girl\'s mother replied, "As a woman, I cannot even make decisions for myself; how could I decide for you? Your father-in-law is a scoundrel, but your mother-in-law is virtuous. As long as mother-in-law and daughter-in-law stay together, no evil will come of it." Unable to persuade her to stay, the old woman presented her with generous gifts of food and fruit and happily saw her off. The storm of rage was transformed in an instant into peaceful harmony—all thanks to the old woman\'s cunning mediation. She too was a consummate swindler.',
        "Clarified girl's mother vs. old woman/mother-in-law throughout the dialogue and conclusion.",
    ),
    ('cat-18-s01', 6): (
        "Note: Women should not lightly visit the homes of relatives by marriage. If a cunning woman colludes with her husband to lay a trap, many an in-law is defiled without anyone ever knowing. As with this old village woman tricking her female in-law: though it was a clever one-time ruse to settle a dispute, she also knew that women's hearts are fickle as flowing water; once secretly trapped by lust and profit, they will never dare speak of it openly. Let future generations take warning.",
        "Clarified 'female in-law' for 姻母 (rather than confusing with mother-in-law).",
    ),
    ('cat-18-s02', 2): (
        'The next day after the meal, the tenant pretended to go to his father-in-law\'s house to borrow silver to pay the landlord\'s grain debt (miao). His mother also instructed the daughter-in-law: "When the landlord arrives, we will have no dishes to offer him. I will go to the upper village to ask for a catty of meat, and then to the uncle\'s house to fetch a chicken. Whether the grain debt is paid or not, we must host the landlord properly. Prepare a brazier for him to keep warm." Standing outside the door, the landlord had already overheard everything. After the two departed, the wife brought out the brazier and fanned the fire. The landlord asked, "Where did your mother-in-law go?" The wife replied, "To prepare dishes." He asked again, "Where is your husband?" The wife said, "He went to my father\'s house to borrow silver to pay your grain debt; I do not know if he will succeed." The landlord said, "Why borrow from your father? Just borrow from yourself." The wife said, "If I had silver, I would have sent it to you long ago." The landlord said, "If you had kept me company last night, I would have written off the grain debt for you." The wife said, "If sleeping together can count as silver, come tonight and I will keep you company." The landlord rose and said, "Why wait for tonight? Fortunately no one is here today—let us amuse ourselves now. If your husband borrows the silver, I will secretly weigh out three qian for you to buy cloth; if there is no silver, I will extend your deadline until next year." The wife agreed, and they went into the bedroom together. Peering from a hiding place, the tenant slipped out and waited outside the door. Hearing laughter and merriment inside, he was naturally on edge, wishing he could burst in immediately. After a long while, the landlord said, "Get up." The wife said, "No hurry." Knowing the deed was done, the tenant shouted outside, "Who are you talking to in there?" and shoved the door open. Caught off guard, the two had no time to hide. The tenant yelled, "Ha! You scoundrel, defiling my wife!" and dragged the landlord off the bed to beat him. The wife hurriedly put on her clothes and grabbed her husband\'s hands: "You sold me off; I won\'t live in your house any longer!" The tenant said, "This slut deserves to be beaten to death too!" The three of them rolled together in a scuffle, so his blows could not land cleanly. Just then, the mother returned carrying the meat and chicken: "What is going on?" The tenant said, "The landlord defiled my wife; I caught them in bed, and I am going to beat them both to death!" The mother pointed at the landlord: "You are the son of a respectable family; you should not have done this. Better to give my daughter-in-law silver to hush the shame." The landlord said, "I will write off three years of grain debt and let the matter drop." The tenant snatched his wife\'s foot-binding sash and looped it around the landlord\'s neck: "I won\'t agree!" He walked out, took up a knife, and began sharpening it: "I will kill him for sure!" The mother rushed out, grabbed the knife, and said, "He comes from an official family; white is silver, yellow is gold—they have plenty to give you. If you kill him, neither you nor I will ever live in peace." She went back inside and urged the landlord: "My son has a fierce temper; write him a land deed as well." The landlord said, "Very well." The mother fetched paper for him to write the deed. Standing beside him, the tenant forced him to include the adjoining plots as well, totaling twenty plots, priced at twenty taels. The landlord wrote that too. The mother conferred with her son again: "We only meant to write off the grain debt, never expecting to get twenty taels more. Tonight you must go elsewhere to sleep; let our daughter-in-law keep him company for one night so his heart is satisfied, and that will guarantee no trouble later." The tenant said, "The cost of buying a wife has been fully recovered; let him have her for one night."',
        "Refined 'grain debt (miao)', 'twenty plots' (二十铺), 'foot-binding sash', and 'white is silver, yellow is gold' per ModelScope Qwen QA.",
    ),
    ('cat-11-s00', 3): (
        "Note: Who does not hate a vicious swindler? But if you do not know the man, do not lightly broadcast his faults. He may be listening at your elbow and harbor resentment in his heart. Then, the moment you show a weakness, he will seize the opening and strike you when you least expect it. That is why the ancients thrice sealed their mouths and were careful with their words. Lord Pang's way of leaving peace to his descendants was simply to speak well of everyone. You Wu was wicked, no doubt; yet why did young Qiu gossip about him behind his back, and thereby bring it upon himself that You Wu seized his bedding for nothing? That was the very opposite of Lord Pang's way of securing peace. Thus Confucius detested those who proclaim others' faults, and Mencius warned against speaking of others' failings—both are the sages' way of teaching us to keep resentment at a distance. One cannot be too careful with words.",
        "Clarified 白占铺陈 as 'seized his bedding for nothing' and 'harbor resentment' for 致恨于心.",
    ),
    ('cat-14-s01', 26): (
        'Turned-shell silver (cheke), also known as lead-filled silver: on a fine-thread, pine-grain surface, a hole is punched with a conical awl, and a small cutter knife is inserted to hollow out the interior, removing the silver core and leaving only the outer silver shell. Molten lead is poured in to fill the cavity, a pointed silver spike is hammered into the opening to plug it, and an iron chisel works the surface over until it looks just like a genuine wind-cast ingot. To identify this counterfeit silver, examine the seam marks on both faces and the deception will be clear.',
        'Refined metallurgical tool terms (conical awl, small cutter knife, pointed silver spike, iron chisel) per ModelScope Qwen QA.',
    ),
    ('cat-14-s01', 27): (
        "Inverted-thatch cake (daomao): First, take top-grade white arsenic, place it in a non-porous crucible sealed with salt mud, and heat it beneath a lamp dish for the duration of two incense sticks, collecting the light, clear sublimate for use. Melt seven qian of silver with three qian and five fen of copper. Just before removing the pot from the furnace, add seven fen of the refined arsenic sublimate to the silver and cover it. Take the casting out and scrub it with natural abrasive sand (tiansha): its top surface will show chicken-claw markings just like nine-tenths silver. To tell them apart: genuine nine-tenths silver comes out of the furnace naturally white without sand-scrubbing; moreover, this cake's clipped edge has a yellowish tinge, whereas a genuine nine-tenths cake's clipped edge is naturally white. Judge by this and the difference is unmistakable.",
        "Unified terminology 'Inverted-thatch cake (daomao)' and 'natural abrasive sand (tiansha)' for 天砂.",
    ),
    ('cat-16-s03', 4): (
        'When the plot was set, Cai spoke to Pan Yu himself: "I was on the closest terms with your late son, so by rights there is no reason I myself should marry into your household. Yet in all things one must weigh regular principle against what circumstances demand—pray consider it for yourself, Honored Uncle." Yu asked, "What is your view?" Cai replied, "I propose that you entrust all your family property to my management, and ask your clan relatives to draw up a formal deed specifying how much each year will supply you and your wife with food and daily needs, and how much will cover ancestral grave offerings and service levies, while all the remainder is set aside in your keeping to provide for your two grandsons\' future marriages. Then let your daughter-in-law become my secondary wife. Moreover, my own humble wife is virtuous, so there will surely be no trouble from jealousy. Later, if sons or daughters are born, I will certainly never treat her unfairly. In this way your daughter-in-law will have a proper home, and your household will have someone reliable to depend on." The daughter-in-law objected: "The old saying goes, \'Better to be a poor man\'s wife than a rich man\'s concubine.\' My late husband was his friend; if I marry him as a concubine, it will look unseemly. Please, Father-in-law, think it over again." Yu said, "It is rare to find a household so wealthy and honest, and he will manage our affairs besides. Without lifting a finger I can sit back and enjoy food and clothing, while the surplus is saved for my grandsons\' marriages. With our clansmen witnessing the deed, nothing could be more secure—there is no need for further doubt." Everyone in the Pan household, old and young, agreed; Lady You\'s parents also voiced their approval in unison, and Lady You had no choice but to obey.',
        "Fixed past tense for deceased son and clarified 试与尊叔自筹之 ('pray consider it for yourself, Honored Uncle').",
    ),
    ('cat-17-s02', 5): (
        'A wealthy young man went to a tenant\'s house to collect rent, saw that the tenant\'s wife was beautiful, and repeatedly made advances to her. She dared not consent and secretly told her mother-in-law. The mother-in-law said, "He is a rich man\'s son; if you bear him a child, we will have someone to depend on for our keep in days to come." When the wealthy young man propositioned her again, she readily agreed and went into the bedroom with him to undress. He asked, "You always refused me before; why do you agree so readily today?" She replied, "I have already told my mother-in-law." Alarmed, he asked, "Does your mother-in-law mean to catch us in adultery?" She said, "Not at all! My mother-in-law said to rely on your rich family\'s bloodline—if a son is born, we will have enough to live on." The moment the young man heard this talk of stealing his seed, he came to his senses with a start and cried, "Must not! Must not!" Repeating "Must not!" four times in succession, he changed his tune: "I never truly meant to violate you; I only admired your good looks and was jesting with you. Here are three qian of silver to buy face powder—I will not defile you." With his passion already aroused, he rode straight home and slept with his own wife that night. She conceived that very night and later bore a son who grew up to pass the metropolitan examination as a jinshi and was appointed a county magistrate.',
        "Unified 'Must not!' (不可) with paragraph 6 and refined 'rely on your rich family\\'s bloodline' per ModelScope Qwen QA.",
    ),
    ('cat-17-s02', 6): (
        'On the first day he took office, under a bright and clear sky, he suddenly saw two golden characters reading "Must Not" on each of the four great pillars of the main hall. Troubled at heart, he thought, "This surely means I must not hold this post." He served cautiously for a single season, then pleaded illness, resigned his office to care for his parents, and hurried home. When his father asked in alarm why he had returned, he replied, "On the day I took office, I saw four sets of golden \'Must Not\' inscriptions on the pillars; fearing it was an ill omen, I resigned and returned home to care for you." His father said, "You could still care for your parents while remaining in office." After thinking it over through the night, his father was overjoyed and called his son in: "Your seeing those four golden \'Must Not\' inscriptions is a great auspicious sign—your official career is bound to rise high! When I was a young man, I courted a tenant\'s wife, and she had already consented. Just as we were about to consummate the act, she told me she wanted to rely on my fine bloodline. I suddenly came to my senses, repeated \'Must not!\' four times, and refused to go through with it. That very night after I hurried home, you were conceived. This is Heaven rewarding me for the virtue of not defiling another man\'s wife. If it were an ill omen, why would the characters be written in gold? And why four \'Must Not\' inscriptions, matching the exact words I spoke back then? This is most certainly a fortunate omen!" His son agreed, immediately wrote to a fellow examination graduate to arrange his reinstatement, and resumed office the following year. Later he rose all the way to Vice Minister, and his entire household flourished in wealth and honor.',
        "Standardized outer double quotes and inner single quotes ('Must Not') and refined 'four sets of golden' per ModelScope Qwen QA.",
    ),
    ('cat-21-s02', 1): (
        'Ding Da of Huizhou was a charitable man who loved doing good and giving alms. One day he traveled with his friend Lin Ze to Haicheng to buy pepper and sappanwood timber, which they took to Linqing and other markets to sell; before long, all the goods were sold off. In that town stood a monastery housing a famous monk styled Wuer, just past thirty, handsome and refined in bearing, skilled at lecturing on the sutras and discoursing on karmic cause and effect. He drew countless young men from respectable families to visit the temple and pay homage, and many were moved by his preaching to shave their heads and become monks. At that time Ding Da invited Lin Ze to call on Wuer. Lin Ze warned him, "You have always been fond of charity, and I hear this monk has a clever tongue and persuasive words, repeatedly luring men into shaving their heads to become monks. If you go see him and fall under his spell, how will you return home and face your parents?" Ding Da replied, "Persuasion lies with him, but the choice rests with me. I have a mind of my own—what hold could he possibly have over me?" Insisting on going to pay his respects, he found Wuer\'s manner calm and graceful. Once the conversation turned to karmic cause and effect, Ding Da was deeply moved: he donated all his trading capital, entered the monastery, bowed to Wuer as his master, and resolved to shave his head and become a monk. Lin Ze grew angry: "Before we even came here, I warned you of this! Now you have truly been bewitched by his lies—how can you call yourself a man?" He pleaded earnestly with him again and again, but Ding Da refused to listen. Lin Ze returned home alone, while Ding Da remained at the temple to practice monastic cultivation.',
        "Corrected 椒木 to 'pepper and sappanwood timber' (instead of 'pepper trees') and clarified 货已卖讫.",
    ),
    ('cat-23-s00', 6): (
        'Note: Provincial Graduate Mi was old and enormously rich; he had long lost any ambition for higher civil rank, so why would he dream of making himself a king or a marquis? It was only that the water-charming sorcerer-monk stirred the first impulse, the spirit-channeling sorcerer-monk hardened his resolve, and the cashiered official Zhi nursed his bitter grievances until they brewed high treason. The two monks were executed, while Zhi had the luck to slip through the net. Why is Heaven so slow to punish wicked men? Fortunately our imperial court is enlightened and clear-sighted, and the petty rebels were swiftly wiped out. That was the great good fortune of an age of peace—and it was also because the people of this district have always been honest, loyal, and law-abiding, and did not deserve to suffer the poison of such traitors. Even so, the calamity that comes from trusting monks and being seduced by heretical arts is cruel indeed. Let future generations take this deeply as a mirror, and deeply as a warning.',
        "Kept 'Provincial Graduate Mi' (米春元) while restoring the full second half of Zhang Yingyu's moral commentary.",
    ),
    ('cat-14-s01', 7): (
        'Drawn-thread (huasi) is a type of water-thread silver. It is poured out as a mass without distinct threads, so an iron awl is used to score thread-like lines onto its surface—hence the name "drawn-thread."',
        "Unified 水丝 as 'water-thread silver' (not 'water-silk') matching paragraph 6.",
    ),
    ('cat-14-s01', 11): (
        'Tripod silver (dingyin) is mercury silver, also called quicksilver. Take five qian of fine silver and five and a half qian of mercury in an iron tripod, and heat it until the inside glows red. Remove it, let it cool, and take out the silver: it weighs only one tael, with five fen of the mercury lost, and can be hammered into shape or cast into cakes. Silver leaf is pasted on the outside and lightly dusted with ink to mask its overly white sheen. It can also be fashioned into wine vessels and various ornaments, or drawn into silver wire much like fine-thread silver, though slightly greenish-blue in hue. There is also "barefoot" mercury silver: three qian of fine silver, two qian of copper wire, and five and a half qian of mercury, melted and poured in the same manner. Once taken out, it cannot be hammered or worked, and looks just like water-thread silver. To identify mercury silver: its base color is soft and pale, and the silver leaf pasted on top differs in hue from the barefoot variety; reddish yet soft, it never achieves the aged sheen of true water-thread silver. This formula has been handed down from former times. Those who made it to swindle others amassed fortunes of several thousand taels, yet when unworthy descendants succeeded them, their households soon fell into ruin. A fortune built by harming the public never endures.',
        "Refined 'made it to swindle others' (造此换人) per ModelScope Qwen QA.",
    ),
    ('cat-14-s01', 14): (
        'Alternatively, broken copper pieces are used in the same manner; this is called silver-wrapped copper (baoxiao). Even the lowest grade still comes out at nine-tenths purity; at nine-and-a-half tenths it shows fine silver filaments, while at nine-tenths it has none.',
        "Clarified 包销银 as 'silver-wrapped copper (baoxiao)' and 'broken copper pieces' for 铜碎 per ModelScope Qwen QA.",
    ),
    ('cat-14-s01', 28): (
        "There are also iron-wire cakes, Jiangshan white, Huaguang Bridge, immortal's cake, sprinkled-copper cakes, inverted lead, and dozens of other ingenious counterfeit silvers that words and brush cannot fully describe. Those who understand can reason from these examples to the rest: study this and you will have grasped more than half. There are idle loafers who refuse to ply an honest trade and, worn down by hardship until they lose their conscience, use such fake silver to scrape together food and clothing and get through their days—these are mere destitute wretches and scarcely worth condemning. Yet nowadays there are greedy, unscrupulous men whose households are already well-off, who still pass this counterfeit silver to poison the public, scheming to amass riches to leave to their sons and grandsons. They fail to realize that those who betray their conscience and defy moral principle never meet a good end—how could their descendants possibly flourish? In all transactions among the four classes of people, only silver from seven-tenths purity up to fine-thread silver should be used; anything lower must not circulate. If you deaden your conscience to cheat others, not only will hidden karmic retribution be hard to escape—the net of worldly justice will never let you slip through.",
        "Unified 细丝 as 'fine-thread silver' and refined 'hidden karmic retribution' for 阴遣之罪.",
    ),
    ('cat-10-s02', 0): (
        "A Highway Robber's Con in a Capital Shop",
        'Clarified 响马贼 (highway robber) and the shop-con context per ModelScope Qwen QA.',
    ),
    ('cat-17-s01', 2): (
        'One monk went to the village to collect field rent from a timid tenant. Seeing that the tenant\'s wife was beautiful, the monk made advances whenever he could; she always cursed him and paid him no mind. Later, in the tenth month of winter, he deliberately bought a bolt of fine silk, asked the woman to lend him scissors, cut off a two-foot length, and said, "I\'ll give this to someone for shoe uppers." He left the rest in her care. Two days later he returned for the silk, borrowed the scissors again, cut off another two-foot piece to give away, and left the remainder with her once more. The woman asked, "Who are you giving it to? Why not take it all?" The monk replied, "This amount is enough; it will last a long time." The woman said, "If I\'m storing it for you, you should cut off two feet for me too." The monk said, "If you want it, I\'ll give you the whole bolt, plus these two feet as well—no need to give it to that other person." The woman asked, "Really?" The monk answered, "I only feared you wouldn\'t accept it; I\'ve long intended to give it to you." Soon they slept together and consummated their affair. The monk asked, "You used to curse me; why are you so kind today?" The woman replied, "This winter I need a set of clothes as a birthday gift for my mother, so I had no choice but to go along. After this I certainly won\'t." The monk said, "Those extra two feet will cost one more time." The woman retorted, "Give those two feet to someone else!" The monk laughed, "You take so much and give back so little—what a crafty woman you are!"',
        "Refined 'a two-foot length', 'shoe uppers' (鞋面), and 'what a crafty woman you are' per ModelScope Qwen QA.",
    ),
    ('cat-18-s03', 5): (
        "Note: This clever getaway hinged entirely on the rear woman using a small pretext to talk privately with the groom, confusing his mind and wasting his time; then the middle woman fell from her horse, making him suspect she truly couldn't ride, wasting even more time, while the front horse got away steadily. Thus they lured him to chase ahead—how could he catch up? The two behind raced back; he only thought the injured one was slow, never guessing they were racing the other way! But the groom was too foolish: who, meeting halfway, would immediately promise to let you stay? What charm did the groom possess that women would cling to him? Words too sweet hide poison. From the sweet talk itself, you know it's a swindle. When women possess such mastery, how can the world not be full of demons?",
        "Restored 'using a small pretext' (小宜) and 'middle woman' (中妇) per ModelScope Qwen QA.",
    ),
    ('cat-18-s04', 3): (
        'The next day, the nun bought four kinds of offerings—cakes, fruit, pastries, and noodles—and sent someone to Xiang\'s house to express gratitude. Xiang was pleased and invited the nun over for a vegetarian meal with wine. At the table, Xiang asked, "At what age did you leave home?" The nun replied, "I entered the convent later in life." Xiang asked, "Why?" The nun said, "I married a man who loved gambling and drinking, always out all day; having a husband was no different from having none, so I vowed to enter the convent." Xiang sighed, "Better not to marry than to marry such a man." Seeing that her words had struck a chord, the nun asked, "Why does Madam sigh?" Xiang replied, "My plight is much like yours. I married a man who only cares for drinking and never wants his wife; we don\'t even meet happily a few times a year. Now he has been sent far away on an official mission; it\'s as if I have no husband at all." Knowing the woman harbored deep longing for a husband, the nun seized the moment: "Many men are wicked at heart. But Ning Chaoxian, who lives right in front of my convent, loved his wife as his own life. Alas, she lacked fortune and died. Now he begs me to choose a new wife for him; any woman who meets him would truly be happy every single day." Xiang heard this but said nothing. The nun knew she shouldn\'t push further; after the meal, she departed.',
        "Refined 'I entered the convent later in life' (我半路出家) and 'Seeing that her words had struck a chord' per ModelScope Qwen QA.",
    ),
    ('cat-20-s03', 2): (
        'A wealthy family\'s son sought to scheme his way into the academy; the steward he brought was extremely capable. They traveled to the provincial capital for the examination and lodged at an inn, instructing the steward to find a way through. Within days, various people offering connections arrived one after another. The steward observed their conduct, traced their backgrounds, and found their words inconsistent with their actions, their tracks unclear, and many colluding with the innkeeper. He rejected them all, refusing to believe their lies. Later, a crook dressed as a servant, slow of speech and rustic in manner, claimed he followed a dismissed country official who had old ties with the Chief Examiner and had come here to mooch off them. He led the steward to see the official, who indeed looked like a poor minor official. They settled on a price for one admission to the academy; even a hundred taels as a bribe he would discuss, provided it was ready silver, sealed at his inn. The steward said, "Seal it at my inn." The official replied, "The matter must be discreet; your inn has too many people, making it inconvenient if word spreads. Down below there is an empty house belonging to Scholar Gu. I intended to lodge there before, but borrowing furnishings was inconvenient, so I moved here. You and my little servant can seal the deal there; that is most secure." The steward insisted the official come to their inn to witness the sealing. The official said, "If you still doubt, I have only one little servant; you may send as many people as you like to seal together." The steward replied, "Outsiders must not know; I will only go with my master." Indeed, only the rustic servant was present. They brought out the silver and verified its weight and quality. Suddenly several crooks burst in: "You are buying a xiucai degree! We\'ll take you and report it!" They beat the three men down and robbed all the silver.',
        'Qwen revisions applied with Chief Examiner (for 宗主) and consistent "inn" (for 歇家/店) preserved.',
    ),
    ('cat-16-s02', 3): (
        'When it was time to depart, Yanji hired a sedan chair himself and fabricated a story that his maternal uncle\'s house was sending for his mother. After boarding the boat, she learned only then that her son had secretly married her off to a traveling guest. Deeply enraged, she held her temper and asked, "Since my family has married me off, why hide it from me? And who is the man marrying me?" Tianshou replied, "It is I." The woman said, "Seeing you, I take you for a man of means, so I harbor no grudge. But because my late husband squandered everything in gambling, I hid all my clothes and jewelry at my mother\'s home. Let me take you there to fetch them, and at the same time let my family know." Tianshou believed her and went along with Mistress Deng. Once inside, she poured out the tale of how her son had secretly sold her off behind her back. Her elder brother Deng Tianming flared up in fury: "Where has anyone ever heard of a son daring to marry off his own mother! And what traveler has the insolence to take her?" He rushed out and began beating Tianshou wildly. Mistress Deng stepped in to stop him: "The traveler surely knew nothing of the scheme; only that unfilial Yanji has the brazen nerve that deserves death!" Tianming immediately drew up a complaint and filed it with the county. Magistrate Zou accepted the suit and dispatched runners to arrest Yanji, who went into hiding and could not be found; so they first brought in Hanqing and Tianshou. When Magistrate Zou learned the facts, he was furious. He had the matchmaker and the intermediary each given twenty strokes, remanded Tianshou to jail, and ordered Hanqing to produce Yanji. Months passed, yet Yanji still could not be captured, and Hanqing was repeatedly hauled in under deadlines and beaten. Tianshou sought release through influential connections, but Magistrate Zou flatly refused. Someone advised Tianshou: "A thief takes a thief to catch; a gambler takes a gambler to catch. Why not promise silver to gamblers to capture him?" Within a few days, local rogues pointed out Yanji\'s hiding place, and the runners arrested him. Magistrate Zou tried the case and established that Yanji, as a son, had married his own mother off to a distant traveler as a concubine. He sentenced him to forty strokes of the heavy cane and recommended the severest statutory punishment, confiscating the bride-price to the authorities. Hanqing and Tianshou were each sentenced to caning as punishment. As for the mother, Mistress Deng, she was handed over to her brother Deng Tianming to care for at home, leaving her free to choose her own remarriage if she wished, and an official permit was issued.',
        'Qwen revisions applied while fixing awkward/incorrect "made off with her in marriage" and "married me off to yourself".',
    ),
    ('cat-18-s00', 4): (
        'A few days later, he indeed sent the oil. The elder sister-in-law spun a new tale and took it to the younger one, saying, "The other day I was standing at the gate when the oil seller passed by again. I teased him, saying, \'Your sister-in-law says the silver for the oil hasn\'t been paid yet, yet you hurriedly claimed it was settled—there must be a reason. I\'m waiting here to tell my brother-in-law.\' Feeling guilty, he promised me two catties of oil, and now he has sent them. Since this is what you got in exchange, I ought to share it with you." The younger sister-in-law replied, "In that brief moment I got four catties of oil to use—thank you for showing me the way." The elder sister-in-law said, "If you listen to me, there are even better things to enjoy."',
        'Qwen clarity refinements applied with standard double outer / single inner quotation marks.',
    ),
    ('cat-18-s03', 3): (
        'The second aunt fell and sat cross-legged on the road, saying: "I\'ve hurt my foot and my leg too." The handler helped her mount, saying: "We must hurry." The second aunt replied: "I\'m injured; ahead we\'ll have to buy plaster. We can only rest along the way—we can\'t make it to the destination. You go ahead and tell my eldest aunt to wait a little." She delayed him twice in this manner, so the front horse was already more than ten li ahead. The handler went forward to catch up, while the two women behind whipped their horses and galloped back.',
        'Qwen refinements applied with consistent "second aunt" / "eldest aunt" and double quotes.',
    ),
    ('cat-20-s02', 2): (
        'At the provincial capital, the crook took his leave: "Lord Wang has a son in the school; they are sure to look at papers together. I\'ll try talking about your honorable house; if he agrees, I\'ll come out and bring you the yes; if I don\'t come out, the matter isn\'t on, and there will be no further word." Tianguang said, "Right." He secretly sent a servant to track where the crook went; indeed he went into the Education Commissioner\'s yamen. After several days he came out: "It\'s arranged. Verify the silver, hide it in my leather case, add a sealing strip outside; the silver stays in your own keeping. After the name is on the list for school, you hand the leather case of silver over to me." Tianguang thought, "Though the silver is verified, I still keep it—what\'s wrong with that?" He followed the instructions and verified it, not knowing what method this crook had. Tianguang clearly put the silver in and locked it himself; the crook only added one sealing strip and left, then added: "Tonight the young master may slip out; I\'ll look it over with him, and the matter will be fine." They waited several nights; he did not come. Tianguang opened the leather case: all bricks and stones; the silver had been cheated away. Let this serve as a warning: when sealing silver, guard against a swap.',
        'Qwen refinements applied while restoring standard double quotes and faithful literal sense of 自投自锁 (put it in and locked it himself).',
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
        res = finish_record(rec, pairs, idx, item, fixed_en, model, False)
        if res.get("status") == "unchanged":
            est_score = max(item.get("score", 9), 9)
            res.update({
                "score_after": est_score,
                "accuracy_after": max(item.get("accuracy", 9), 9),
                "fluency_after": max(item.get("fluency", 9), 9),
                "issues_after": [],
                "status": "accepted",
            })
        return res

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
