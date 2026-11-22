#!/usr/bin/env python3
"""Build a compact i18n/dict.json from the book's Chinese text + CC-CEDICT.

Indexes both Traditional and Simplified Chinese headwords from CC-CEDICT so
all sections of 《騙經》 (Pian Jing) match accurately, collects up to 2 short
in-book example snippets with paragraph IDs, and keeps the dictionary separate
from index.html so it loads lazily via Web Worker on first hover/selection.
"""

from __future__ import annotations

import gzip
import html
import io
import json
import re
import urllib.request
import zipfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
READER_HTML_PATH = ROOT_DIR / "index.html"
DICT_OUTPUT_PATH = ROOT_DIR / "i18n" / "dict.json"
CACHE_DIR = ROOT_DIR / ".cache"

CEDICT_CACHE_CANDIDATES = [
    CACHE_DIR / "cedict_ts.u8",
    ROOT_DIR.parent / "leeao-bilingual" / ".cache" / "cedict_ts.u8",
    ROOT_DIR.parent / "converted-book-app" / ".cache" / "cedict_ts.u8",
]

CEDICT_DOWNLOAD_URLS = [
    "https://www.mdbg.net/chinese/export/cedict/cedict_1_0_ts_utf-8_mdbg.zip",
    "https://github.com/tsroten/cidict/raw/master/cedict_ts.u8",
]

CHINESE_CHAR_PATTERN = re.compile(r"[\u3400-\u9FFF\uF900-\uFAFF]")
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
PAIR_CONTAINER_PATTERN = re.compile(
    r'<div class="pair[^"]*"(?:\s+id="([^"]*)")?(?:\s+data-loc="([^"]*)")?[^>]*>\s*<div class="col zh"[^>]*>(.*?)</div>',
    re.DOTALL,
)
SENTENCE_BOUNDARY_PATTERN = re.compile(r"(?<=[。！？；\n])")
CEDICT_ENTRY_LINE_PATTERN = re.compile(r"^(\S+)\s+(\S+)\s+\[([^\]]+)\]\s+/(.+)/$")

TRAD_SIMP_WITH_PINYIN_PATTERN = re.compile(
    r"[\u3400-\u9FFF\uF900-\uFAFF]+\|([\u3400-\u9FFF\uF900-\uFAFF]+)\[([^\]]+)\]"
)
TRAD_SIMP_PAIR_PATTERN = re.compile(
    r"[\u3400-\u9FFF\uF900-\uFAFF]+\|([\u3400-\u9FFF\uF900-\uFAFF]+)"
)
HANZI_WITH_PINYIN_PATTERN = re.compile(r"([\u3400-\u9FFF\uF900-\uFAFF]+)\[([^\]]+)\]")
CLASSIFIER_TAG_PATTERN = re.compile(r"\s*\(?CL:[^)]+\)?")

VOWEL_TONE_MARKS = {
    "a": "āáǎàa",
    "e": "ēéěèe",
    "i": "īíǐìi",
    "o": "ōóǒòo",
    "u": "ūúǔùu",
    "ü": "ǖǘǚǜü",
}

MAX_WORD_LENGTH = 8
MAX_DEFINITIONS_PER_WORD = 4
MAX_EXAMPLES_PER_WORD = 2
MAX_EXAMPLE_SNIPPET_CHARS = 42
MAX_DEFINITION_CHAR_LENGTH = 150

LOW_PRIORITY_DEFINITION_PREFIXES = (
    "surname ",
    "old variant of",
    "variant of",
    "archaic variant of",
    "erhua variant of",
    "see ",
    "also written ",
    "used in ",
    "abbr. for ",
    "short name for ",
    "old name for ",
    "alternative name for ",
    "taiwan pr.",
    "pr. [",
    "also pr. [",
)

# High-frequency polyphonic characters (Traditional + Simplified) whose primary
# classical / Ming-vernacular reading should be preferred over rare surnames or alternates.
PREFERRED_SINGLE_CHAR_READINGS: dict[str, str] = {
    "的": "de", "一": "yī", "是": "shì", "不": "bù", "了": "le",
    "在": "zài", "人": "rén", "有": "yǒu", "我": "wǒ", "他": "tā",
    "這": "zhè", "这": "zhè", "中": "zhōng", "大": "dà", "來": "lái", "来": "lái",
    "上": "shàng", "個": "gè", "个": "gè", "到": "dào", "說": "shuō", "说": "shuō",
    "們": "men", "们": "men", "為": "wèi", "爲": "wèi", "为": "wèi",
    "子": "zǐ", "和": "hé", "你": "nǐ", "地": "dì", "出": "chū",
    "道": "dào", "也": "yě", "時": "shí", "时": "shí", "年": "nián",
    "得": "de", "就": "jiù", "那": "nà", "要": "yào", "下": "xià",
    "以": "yǐ", "生": "shēng", "會": "huì", "会": "huì", "自": "zì",
    "著": "zhe", "着": "zhe", "去": "qù", "之": "zhī", "過": "guò", "过": "guò",
    "家": "jiā", "學": "xué", "学": "xué", "對": "duì", "对": "duì",
    "可": "kě", "她": "tā", "裡": "lǐ", "裏": "lǐ", "里": "lǐ",
    "後": "hòu", "后": "hòu", "小": "xiǎo", "麼": "me", "么": "me",
    "心": "xīn", "多": "duō", "天": "tiān", "而": "ér", "能": "néng",
    "好": "hǎo", "都": "dōu", "然": "rán", "沒": "méi", "没": "méi",
    "日": "rì", "於": "yú", "于": "yú", "起": "qǐ", "還": "hái", "还": "hái",
    "發": "fā", "发": "fā", "成": "chéng", "事": "shì", "只": "zhǐ",
    "作": "zuò", "當": "dāng", "当": "dāng", "想": "xiǎng", "看": "kàn",
    "文": "wén", "無": "wú", "无": "wú", "開": "kāi", "开": "kāi",
    "手": "shǒu", "十": "shí", "用": "yòng", "主": "zhǔ", "行": "xíng",
    "方": "fāng", "又": "yòu", "如": "rú", "前": "qián", "所": "suǒ",
    "本": "běn", "見": "jiàn", "见": "jiàn", "經": "jīng", "经": "jīng",
    "頭": "tóu", "头": "tóu", "面": "miàn", "公": "gōng", "同": "tóng",
    "三": "sān", "已": "yǐ", "老": "lǎo", "從": "cóng", "从": "cóng",
    "動": "dòng", "动": "dòng", "兩": "liǎng", "两": "liǎng",
    "長": "cháng", "长": "cháng", "把": "bǎ", "被": "bèi",
    "干": "gàn", "單": "dān", "单": "dān", "相": "xiāng",
    "與": "yǔ", "与": "yǔ", "其": "qí", "者": "zhě", "將": "jiāng", "将": "jiāng",
    "應": "yīng", "应": "yīng", "傳": "chuán", "传": "chuán",
    "難": "nán", "难": "nán", "處": "chù", "处": "chù",
    "更": "gèng", "正": "zhèng", "少": "shǎo", "間": "jiān", "间": "jiān",
    "重": "zhòng", "便": "biàn", "數": "shù", "数": "shù",
    "南": "nán", "餘": "yú", "馀": "yú", "余": "yú",
    "藏": "cáng", "度": "dù", "假": "jiǎ", "舍": "shě", "稱": "chēng", "称": "chēng",
    "量": "liàng", "省": "shěng", "差": "chāi", "樂": "lè", "乐": "lè",
    "惡": "è", "恶": "è", "切": "qiè", "宿": "sù", "乘": "chéng",
    "盛": "shèng", "興": "xīng", "兴": "xīng", "分": "fēn", "盡": "jìn", "尽": "jìn",
    "解": "jiě", "調": "tiáo", "调": "tiáo", "覺": "jué", "觉": "jué",
    "折": "zhé", "參": "cān", "参": "cān", "落": "luò", "供": "gōng",
    "背": "bèi", "轉": "zhuǎn", "转": "zhuǎn", "教": "jiāo",
    "令": "lìng", "約": "yuē", "约": "yuē", "別": "bié", "别": "bié",
    "倒": "dǎo", "空": "kōng", "銀": "yín", "银": "yín", "錢": "qián", "钱": "qián",
    "曰": "yuē", "矣": "yǐ", "乎": "hū", "焉": "yān", "哉": "zāi", "耳": "ěr",
}

# Ming-era / Book of Swindles specialized terms missing or poorly glossed in CC-CEDICT
MING_GLOSSARY_ENTRIES: dict[str, dict] = {
    "歇家": {"p": "xiē jiā", "d": ["inn; lodging house", "innkeeper; host who lodges travelers or litigants"]},
    "學道": {"p": "xué dào", "d": ["Provincial Education Commissioner (Ming/Qing official overseeing prefectural and county school examinations)"]},
    "学道": {"p": "xué dào", "d": ["Provincial Education Commissioner (Ming/Qing official overseeing prefectural and county school examinations)"]},
    "保辜": {"p": "bǎo gū", "d": ["Ming/Qing legal observation period for assault injuries to determine whether the victim recovers or dies (assigning homicide liability)"]},
    "保辜狀": {"p": "bǎo gū zhuàng", "d": ["injury-liability complaint filed to start the legal observation period in an assault case"]},
    "保辜状": {"p": "bǎo gū zhuàng", "d": ["injury-liability complaint filed to start the legal observation period in an assault case"]},
    "牙人": {"p": "yá rén", "d": ["broker; licensed commercial middleman or commission agent"]},
    "打秋風": {"p": "dǎ qiū fēng", "d": ["to mooch; to solicit gifts or hospitality from wealthy acquaintances or officials"]},
    "打秋风": {"p": "dǎ qiū fēng", "d": ["to mooch; to solicit gifts or hospitality from wealthy acquaintances or officials"]},
    "分上": {"p": "fèn shàng", "d": ["personal influence; pull or connections used with officials", "out of regard for someone; for someone's sake"]},
    "鄉官": {"p": "xiāng guān", "d": ["retired local official living in his home district"]},
    "乡官": {"p": "xiāng guān", "d": ["retired local official living in his home district"]},
    "禁子": {"p": "jìn zǐ", "d": ["jailer; prison turnkey"]},
    "條絲": {"p": "tiáo sī", "d": ["fine-thread silver (wensi/tiaosi), pure silver ingots marked with fine ridges"]},
    "条丝": {"p": "tiáo sī", "d": ["fine-thread silver (wensi/tiaosi), pure silver ingots marked with fine ridges"]},
    "水絲": {"p": "shuǐ sī", "d": ["water-thread silver, lower-grade silver alloyed with lead or copper (80–90% fineness)"]},
    "水丝": {"p": "shuǐ sī", "d": ["water-thread silver, lower-grade silver alloyed with lead or copper (80–90% fineness)"]},
    "包銷": {"p": "bāo xiāo", "d": ["silver-wrapped copper/lead (baoxiao), a counterfeit ingot technique coating base metal in silver"]},
    "包销": {"p": "bāo xiāo", "d": ["silver-wrapped copper/lead (baoxiao), a counterfeit ingot technique coating base metal in silver"]},
    "車殼": {"p": "chē ké", "d": ["turned-shell silver (cheke), hollow silver shell filled with copper and lead then spun on a lathe"]},
    "车壳": {"p": "chē ké", "d": ["turned-shell silver (cheke), hollow silver shell filled with copper and lead then spun on a lathe"]},
    "倒毛": {"p": "dào máo", "d": ["inverted-thatch cake (daomao), layered counterfeit silver ingot with copper sandwiched inside"]},
    "天砂": {"p": "tiān shā", "d": ["natural abrasive sand (tiansha), fine sand mixed with silver during casting to fake weight"]},
    "兩": {"p": "liǎng", "d": ["tael (traditional Chinese silver/weight unit, ~37g in Ming China)", "two; both; a few"]},
    "两": {"p": "liǎng", "d": ["tael (traditional Chinese silver/weight unit, ~37g in Ming China)", "two; both; a few"]},
    "錢": {"p": "qián", "d": ["qian / mace (1/10 of a tael of silver)", "copper cash coin; money"]},
    "钱": {"p": "qián", "d": ["qian / mace (1/10 of a tael of silver)", "copper cash coin; money"]},
    "銀合": {"p": "yín hé", "d": ["silver-gray (horse coat color)"]},
    "银合": {"p": "yín hé", "d": ["silver-gray (horse coat color)"]},
    "馀": {"p": "yú", "d": ["extra; surplus; remaining", "odd (excess over a round number, e.g. 十馀两 'ten-odd taels')"]},
    # High-frequency classical / late-Ming vernacular & false-compound fixes from Qwen QA
    "后": {"p": "hòu", "d": ["after; later; afterward", "behind; back; rear", "empress; queen (rare)"]},
    "同妻": {"p": "tóng qī", "d": ["together with one's wife", "accompanied by one's wife"]},
    "六家": {"p": "liù jiā", "d": ["six households; six families", "six household servants (e.g. 六家仆)"]},
    "一甲": {"p": "yī jiǎ", "d": ["first jia (ten-household tax or corvée unit in the Ming lijia system)", "first rank (imperial exams)"]},
    "打": {"p": "dǎ", "d": ["to hit; to strike; to beat", "to make; to fetch; to perform (an action)"]},
    "得": {"p": "dé", "d": ["to obtain; to get; to succeed; to be able to", "de: structural particle linking verb to complement; must"]},
    "还": {"p": "huán", "d": ["to return; to go/give back; to repay", "hái: still; yet; even more"]},
    "還": {"p": "huán", "d": ["to return; to go/give back; to repay", "hái: still; yet; even more"]},
    "于": {"p": "yú", "d": ["at; in; to; from; by (classical preposition)", "than (comparative)"]},
    "於": {"p": "yú", "d": ["at; in; to; from; by (classical preposition)", "than (comparative)"]},
    "按": {"p": "àn", "d": ["Author's note / comment: (editorial commentary marker)", "to press down; to suppress", "according to; following"]},
    "本": {"p": "běn", "d": ["capital; principal (money)", "root; origin; foundation", "this; one's own"]},
    "即": {"p": "jí", "d": ["at once; immediately; promptly", "namely; that is", "to approach; even if"]},
    "故": {"p": "gù", "d": ["therefore; so; thus", "deliberately; on purpose", "former; old; cause"]},
    "同": {"p": "tóng", "d": ["together with; along with", "same; identical; alike"]},
    "所": {"p": "suǒ", "d": ["that which (nominalizer)", "place; building", "classifier for houses/buildings"]},
    "或": {"p": "huò", "d": ["someone; some", "perhaps; maybe", "or"]},
    "奈": {"p": "nài", "d": ["what to do; how can one deal with (usu. 奈何)", "alas; unfortunately; helpless (無奈)"]},
    "胥": {"p": "xū", "d": ["clerk; yamen clerk; minor government runner/functionary"]},
    "估": {"p": "gū", "d": ["to appraise; to assess value; to estimate"]},
    "方": {"p": "fāng", "d": ["just then; only then; at that moment", "method; prescription; direction", "square"]},
    "你": {"p": "nǐ", "d": ["you (second-person pronoun)"]},
    "他": {"p": "tā", "d": ["he; him; his", "other; another (classical sense)"]},
    "入手": {"p": "rù shǒu", "d": ["to obtain; to come into one's hands", "to start with; to begin"]},
    "妇": {"p": "fù", "d": ["woman; married woman; wife"]},
    "婦": {"p": "fù", "d": ["woman; married woman; wife"]},
    "妻": {"p": "qī", "d": ["wife", "qì: to marry off (one's daughter)"]},
    "农": {"p": "nóng", "d": ["farmer; peasant", "agriculture; to farm"]},
    "農": {"p": "nóng", "d": ["farmer; peasant", "agriculture; to farm"]},
    "自": {"p": "zì", "d": ["self; oneself; one's own", "from; since", "naturally; of course"]},
    "时": {"p": "shí", "d": ["time; period; season; era", "at that time; then", "frequently"]},
    "時": {"p": "shí", "d": ["time; period; season; era", "at that time; then", "frequently"]},
    "好": {"p": "hǎo", "d": ["good; fine; fitting; suitable", "easy to; very", "hào: to be fond of"]},
    "被": {"p": "bèi", "d": ["passive marker (by; subjected to)", "quilt; bedding", "to cover"]},
    "归": {"p": "guī", "d": ["to return; to go back home", "to belong to; to hand over to"]},
    "歸": {"p": "guī", "d": ["to return; to go back home", "to belong to; to hand over to"]},
    "氏": {"p": "shì", "d": ["clan/maiden surname suffix (e.g. 鄧氏 'Mistress Deng')", "family; lineage"]},
    "更": {"p": "gèng", "d": ["even more; all the more; further", "how much more (rhetorical)", "gēng: to change; night watch"]},
    "行": {"p": "xíng", "d": ["to walk; to travel; to go", "to do; to carry out; to practice", "háng: trade; shop; row"]},
    "佛": {"p": "Fó", "d": ["Buddha; Buddhism", "fú: seemingly (彷彿)"]},
    "顾": {"p": "gù", "d": ["to look back; to look after", "but; however; only (classical conjunction)", "surname Gu"]},
    "顧": {"p": "gù", "d": ["to look back; to look after", "but; however; only (classical conjunction)", "surname Gu"]},
    "骨": {"p": "gǔ", "d": ["bone; skeleton; frame"]},
    "庐": {"p": "lú", "d": ["hut; cottage", "used in 呼廬 (ancient dice game)", "Lu Prefecture (Luzhou)"]},
    "廬": {"p": "lú", "d": ["hut; cottage", "used in 呼廬 (ancient dice game)", "Lu Prefecture (Luzhou)"]},
    "凉": {"p": "liáng", "d": ["cool; cold", "forlorn; desolate"]},
    "刘": {"p": "Liú", "d": ["surname Liu", "to kill (archaic)"]},
    "劉": {"p": "Liú", "d": ["surname Liu", "to kill (archaic)"]},
    "潘": {"p": "Pān", "d": ["surname Pan"]},
    "邹": {"p": "Zōu", "d": ["surname Zou", "ancient vassal state of Zou"]},
    "鄒": {"p": "Zōu", "d": ["surname Zou", "ancient vassal state of Zou"]},
    "郝": {"p": "Hǎo", "d": ["surname Hao", "ancient place name"]},
    "荀": {"p": "Xún", "d": ["surname Xun", "archaic plant name"]},
    "吴": {"p": "Wú", "d": ["surname Wu", "ancient state of Wu; Jiangnan region"]},
    "吳": {"p": "Wú", "d": ["surname Wu", "ancient state of Wu; Jiangnan region"]},
    "仆": {"p": "pú", "d": ["servant; attendant", "pū: to fall forward; to fall prostrate"]},
    "僕": {"p": "pú", "d": ["servant; attendant", "humble first-person pronoun (I; your servant)"]},
    "术": {"p": "shù", "d": ["art; skill; technique; method", "trick; stratagem"]},
    "術": {"p": "shù", "d": ["art; skill; technique; method", "trick; stratagem"]},
    "褚": {"p": "Chǔ", "d": ["surname Chu", "padding (in garment)"]},
    "便": {"p": "biàn", "d": ["at once; immediately; then", "convenient; ordinary", "pián: advantageous; cheap"]},
    "黄": {"p": "huáng", "d": ["yellow; sallow", "surname Huang"]},
    "黃": {"p": "huáng", "d": ["yellow; sallow", "surname Huang"]},
    "言": {"p": "yán", "d": ["to say; to speak", "words; speech; remark"]},
    "董": {"p": "Dǒng", "d": ["surname Dong", "to supervise; to direct"]},
    "吕": {"p": "Lǚ", "d": ["surname Lü", "pitchpipe (music)"]},
    "呂": {"p": "Lǚ", "d": ["surname Lü", "pitchpipe (music)"]},
    "徐": {"p": "Xú", "d": ["surname Xu", "xú: slowly; calmly; gently"]},
    "刁": {"p": "diāo", "d": ["wicked; artful; cunning", "to bully; to make difficulties (刁蹬)", "surname Diao"]},
    "荻": {"p": "dí", "d": ["reed; silvergrass (used in plant and personal/place names)"]},
    "施": {"p": "shī", "d": ["to apply; to carry out; to bestow; to give alms", "surname Shi"]},
    "中": {"p": "zhōng", "d": ["within; among; in; middle", "zhòng: to hit (target); to fall into (a trap)"]},
    "有": {"p": "yǒu", "d": ["to have; to possess", "there is; there are"]},
    "到": {"p": "dào", "d": ["to arrive; to reach; to go to", "up until"]},
    "付": {"p": "fù", "d": ["to hand over; to deliver", "to pay; to entrust"]},
    "民": {"p": "mín", "d": ["the people; commoners; subjects"]},
    "林": {"p": "lín", "d": ["woods; forest", "surname Lin"]},
    "在此": {"p": "zài cǐ", "d": ["here; at this place", "right here; at this moment"]},
    "多": {"p": "duō", "d": ["many; much; a lot of", "mostly; for the most part"]},
}


def download_url_bytes(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "pian-jing-bilingual/dict-builder"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def decode_cedict_archive_bytes(raw_bytes: bytes) -> str:
    if raw_bytes[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zip_archive:
            member_name = next(
                name for name in zip_archive.namelist() if not name.endswith("/")
            )
            member_bytes = zip_archive.read(member_name)
            if member_bytes[:2] == b"\x1f\x8b":
                member_bytes = gzip.decompress(member_bytes)
            return member_bytes.decode("utf-8")
    if raw_bytes[:2] == b"\x1f\x8b":
        return gzip.decompress(raw_bytes).decode("utf-8")
    return raw_bytes.decode("utf-8")


def load_cedict_source_text() -> str:
    for cache_path in CEDICT_CACHE_CANDIDATES:
        if cache_path.is_file() and cache_path.stat().st_size > 100_000:
            print(f"Using CC-CEDICT cache {cache_path}")
            return cache_path.read_text(encoding="utf-8")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    primary_cache_path = CEDICT_CACHE_CANDIDATES[0]
    last_error: Exception | None = None

    for url in CEDICT_DOWNLOAD_URLS:
        print(f"Downloading CC-CEDICT from {url}")
        try:
            raw_bytes = download_url_bytes(url)
        except Exception as err:
            last_error = err
            print(f"  failed: {err}")
            continue
        decoded_text = decode_cedict_archive_bytes(raw_bytes)
        if decoded_text.count("\n") > 1000:
            primary_cache_path.write_text(decoded_text, encoding="utf-8")
            return decoded_text
        last_error = RuntimeError("Downloaded file did not look like CC-CEDICT")

    raise RuntimeError(f"Could not download CC-CEDICT: {last_error}")


def find_tone_vowel_index(lowercase_letters: str) -> int | None:
    for primary_vowel in "aoe":
        position = lowercase_letters.find(primary_vowel)
        if position >= 0:
            return position
    if "iu" in lowercase_letters:
        return lowercase_letters.rfind("iu") + 1
    if "ui" in lowercase_letters:
        return lowercase_letters.rfind("ui") + 1
    for index, char in enumerate(lowercase_letters):
        if char in VOWEL_TONE_MARKS:
            return index
    return None


def apply_tone_mark_to_syllable(numbered_syllable: str) -> str:
    normalized = (
        numbered_syllable.replace("u:", "ü").replace("U:", "Ü").replace("v", "ü")
    )
    match = re.match(r"^([^1-5]+)([1-5])$", normalized)
    if not match:
        return normalized.replace("5", "")

    letters, tone_number = match.group(1), int(match.group(2))
    if tone_number == 5:
        return letters

    vowel_index = find_tone_vowel_index(letters.lower())
    if vowel_index is None:
        return letters

    target_vowel = letters[vowel_index].lower()
    lookup_key = "ü" if target_vowel in "üv" else target_vowel
    accented_vowel = VOWEL_TONE_MARKS[lookup_key][tone_number - 1]
    if letters[vowel_index].isupper():
        accented_vowel = accented_vowel.upper()

    return letters[:vowel_index] + accented_vowel + letters[vowel_index + 1 :]


def convert_numbered_pinyin_to_tone_marks(numbered_pinyin: str) -> str:
    tokens = numbered_pinyin.replace("u:", "ü").split()
    return " ".join(apply_tone_mark_to_syllable(token) for token in tokens)


def truncate_definition_at_word_boundary(definition_text: str) -> str:
    if len(definition_text) <= MAX_DEFINITION_CHAR_LENGTH:
        return definition_text
    prefix = definition_text[: MAX_DEFINITION_CHAR_LENGTH - 5]
    boundary_index = max(prefix.rfind("; "), prefix.rfind(", "), prefix.rfind(" "))
    if boundary_index > 60:
        prefix = prefix[:boundary_index].rstrip(" ,;(")
    return prefix + "…"


def clean_cedict_definition(raw_definition: str) -> str:
    """Clean raw CC-CEDICT cross-references and avoid mid-word truncation."""
    definition = raw_definition.strip()
    if not definition or definition.startswith("CL:"):
        return ""
    lower = definition.lower()
    if lower.startswith(("taiwan pr.", "also pr.", "pr. [")):
        return ""
    definition = re.sub(r"\s*\(?CL:[^)]*\)?", "", definition)
    definition = re.sub(r"\s*\((?:Taiwan pr\.|also pr\.|Note:)[^)]*\)", "", definition, flags=re.I)
    definition = definition.strip()
    if not definition:
        return ""

    def format_trad_simp_pinyin(match: re.Match) -> str:
        simplified = match.group(1)
        pinyin = convert_numbered_pinyin_to_tone_marks(match.group(2))
        return f"{simplified} ({pinyin})"

    def format_hanzi_pinyin(match: re.Match) -> str:
        hanzi = match.group(1)
        pinyin = convert_numbered_pinyin_to_tone_marks(match.group(2))
        return f"{hanzi} ({pinyin})"

    definition = TRAD_SIMP_WITH_PINYIN_PATTERN.sub(format_trad_simp_pinyin, definition)
    definition = TRAD_SIMP_PAIR_PATTERN.sub(r"\1", definition)
    definition = HANZI_WITH_PINYIN_PATTERN.sub(format_hanzi_pinyin, definition)
    definition = CLASSIFIER_TAG_PATTERN.sub("", definition).strip()
    return truncate_definition_at_word_boundary(definition)


def is_secondary_or_surname_definition(definition: str) -> bool:
    lowercase_def = definition.lower()
    return any(
        lowercase_def.startswith(prefix) for prefix in LOW_PRIORITY_DEFINITION_PREFIXES
    )


def rank_cedict_sense_priority(
    headword: str, pinyin: str, definitions: list[str]
) -> tuple[int, int, int]:
    preferred_reading = PREFERRED_SINGLE_CHAR_READINGS.get(headword)
    matches_preferred = (
        0 if (preferred_reading and pinyin.lower() == preferred_reading.lower()) else 1
    )
    has_primary_meaning = (
        0 if any(not is_secondary_or_surname_definition(d) for d in definitions) else 1
    )
    is_capitalized_proper_noun = 1 if (pinyin and pinyin[0].isupper()) else 0
    return (matches_preferred, has_primary_meaning, is_capitalized_proper_noun)


def select_best_entry_for_headword(
    headword: str, sense_group: list[tuple[str, list[str]]]
) -> dict | None:
    sorted_senses = sorted(
        sense_group,
        key=lambda item: rank_cedict_sense_priority(headword, item[0], item[1]),
    )
    selected_pinyin = sorted_senses[0][0]
    if len(headword) == 1 and selected_pinyin and selected_pinyin[0].isupper():
        for pinyin_candidate, _ in sorted_senses:
            if pinyin_candidate and pinyin_candidate[0].islower():
                selected_pinyin = pinyin_candidate
                break

    primary_definitions: list[str] = []
    secondary_definitions: list[str] = []
    for _, definitions in sorted_senses:
        for definition in definitions:
            if is_secondary_or_surname_definition(definition):
                if definition not in secondary_definitions:
                    secondary_definitions.append(definition)
            else:
                if definition not in primary_definitions:
                    primary_definitions.append(definition)

    merged_definitions = (primary_definitions + secondary_definitions)[
        :MAX_DEFINITIONS_PER_WORD
    ]
    if not merged_definitions:
        return None
    return {"p": selected_pinyin, "d": merged_definitions}


def parse_cedict_entries(cedict_text: str) -> dict[str, dict]:
    """Index both Traditional and Simplified headwords from CC-CEDICT."""
    grouped_senses: dict[str, list[tuple[str, list[str]]]] = {}

    for raw_line in cedict_text.splitlines():
        if not raw_line or raw_line.startswith("#"):
            continue
        match = CEDICT_ENTRY_LINE_PATTERN.match(raw_line.strip())
        if not match:
            continue
        traditional_word = match.group(1)
        simplified_word = match.group(2)

        pinyin = convert_numbered_pinyin_to_tone_marks(match.group(3))
        cleaned_defs = [
            clean_cedict_definition(part) for part in match.group(4).split("/")
        ]
        usable_defs = [
            d
            for d in cleaned_defs
            if d
            and not d.lower().startswith("variant of")
            and not d.lower().startswith("old variant of")
        ]
        if not usable_defs:
            continue

        for headword in dict.fromkeys((traditional_word, simplified_word)):
            if CHINESE_CHAR_PATTERN.search(headword) and len(headword) <= MAX_WORD_LENGTH:
                grouped_senses.setdefault(headword, []).append((pinyin, usable_defs))

    resolved_entries: dict[str, dict] = {}
    for headword, sense_group in grouped_senses.items():
        best_entry = select_best_entry_for_headword(headword, sense_group)
        if best_entry:
            resolved_entries[headword] = best_entry
    for term, custom in MING_GLOSSARY_ENTRIES.items():
        resolved_entries[term] = custom
    return resolved_entries


def find_reader_html_path() -> Path:
    if READER_HTML_PATH.is_file() and READER_HTML_PATH.stat().st_size > 10_000:
        if 'class="col zh"' in READER_HTML_PATH.read_text(encoding="utf-8"):
            return READER_HTML_PATH
    raise SystemExit("Reader HTML not found (index.html).")


def extract_book_chinese_sentences(
    html_content: str,
) -> list[tuple[str, str, str]]:
    sentences: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    for match in PAIR_CONTAINER_PATTERN.finditer(html_content):
        pair_id = html.unescape(match.group(1) or "")
        loc_label = html.unescape(match.group(2) or "")
        column_html = match.group(3)
        plain_text = HTML_TAG_PATTERN.sub("", column_html)
        plain_text = (
            plain_text.replace("&nbsp;", " ")
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("■", "")
            .replace("□", "")
        )
        compact_text = re.sub(r"\s+", "", plain_text)
        if not CHINESE_CHAR_PATTERN.search(compact_text):
            continue
        for raw_sentence in SENTENCE_BOUNDARY_PATTERN.split(compact_text):
            sentence = raw_sentence.strip()
            if len(sentence) < 2 or not CHINESE_CHAR_PATTERN.search(sentence):
                continue
            if sentence not in seen:
                seen.add(sentence)
                sentences.append((sentence, pair_id, loc_label))
    return sentences


def build_context_snippet_around_word(sentence: str, target_word: str) -> str:
    if len(sentence) <= MAX_EXAMPLE_SNIPPET_CHARS:
        return sentence
    word_index = sentence.find(target_word)
    start_index = max(0, word_index - 8)
    excerpt = sentence[start_index : start_index + MAX_EXAMPLE_SNIPPET_CHARS]
    if start_index > 0:
        excerpt = "…" + excerpt
    if start_index + MAX_EXAMPLE_SNIPPET_CHARS < len(sentence):
        excerpt += "…"
    return excerpt


def collect_in_book_word_examples(
    sentences: list[tuple[str, str, str]], cedict_entries: dict[str, dict]
) -> dict[str, list[list[str]]]:
    """Map each book word to its in-book example tuples [snippet, pair_id, loc_label]."""
    examples_by_word: dict[str, list[list[str]]] = {}
    for sentence, pair_id, loc_label in sentences:
        is_concise_sentence = len(sentence) <= 180
        char_index = 0
        sentence_length = len(sentence)

        while char_index < sentence_length:
            if not CHINESE_CHAR_PATTERN.match(sentence[char_index]):
                char_index += 1
                continue

            matched_span = 1
            max_span = min(MAX_WORD_LENGTH, sentence_length - char_index)
            for candidate_length in range(max_span, 0, -1):
                candidate_word = sentence[char_index : char_index + candidate_length]
                if candidate_word in cedict_entries:
                    matched_span = candidate_length
                    break

            matched_word = sentence[char_index : char_index + matched_span]
            if matched_word in cedict_entries:
                existing_examples = examples_by_word.setdefault(matched_word, [])
                if is_concise_sentence and len(existing_examples) < MAX_EXAMPLES_PER_WORD:
                    snippet_text = build_context_snippet_around_word(
                        sentence, matched_word
                    )
                    example_item = [snippet_text, pair_id, loc_label]
                    if not any(ex[0] == snippet_text for ex in existing_examples):
                        existing_examples.append(example_item)
            char_index += matched_span

    return examples_by_word


def build_compact_dictionary_payload(
    cedict_entries: dict[str, dict], examples_by_word: dict[str, list[list[str]]]
) -> dict:
    serialized_entries = {}
    sorted_words = sorted(examples_by_word, key=lambda word: (-len(word), word))

    for word in sorted_words:
        entry_info = cedict_entries[word]
        compact_row = [entry_info["p"], entry_info["d"]]
        word_examples = examples_by_word[word][:MAX_EXAMPLES_PER_WORD]
        if word_examples:
            compact_row.append(word_examples)
        serialized_entries[word] = compact_row

    return {
        "_meta": {
            "source": "CC-CEDICT (CC BY-SA 4.0) + in-book examples",
            "terms": len(serialized_entries),
            "maxLen": MAX_WORD_LENGTH,
            "format": "[pinyin, definitions, examples?]",
        },
        "maxLen": MAX_WORD_LENGTH,
        "entries": serialized_entries,
    }


def main() -> None:
    html_path = find_reader_html_path()
    print(f"HTML: {html_path}")
    html_content = html_path.read_text(encoding="utf-8")

    sentences = extract_book_chinese_sentences(html_content)
    print(f"Book sentences: {len(sentences)}")

    cedict_entries = parse_cedict_entries(load_cedict_source_text())
    print(f"CEDICT entries kept: {len(cedict_entries)}")

    examples_by_word = collect_in_book_word_examples(sentences, cedict_entries)
    print(f"Book terms: {len(examples_by_word)}")

    payload = build_compact_dictionary_payload(cedict_entries, examples_by_word)
    DICT_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DICT_OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    size_kb = DICT_OUTPUT_PATH.stat().st_size // 1024
    print(
        f"Wrote {DICT_OUTPUT_PATH} ({size_kb} KB, {len(payload['entries'])} entries)"
    )


if __name__ == "__main__":
    main()
