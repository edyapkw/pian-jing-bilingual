#!/usr/bin/env python3
"""Translation QA — verify Chinese→English quality using ModelScope Qwen.

Similar to leeao-bilingual's QA pipeline, tailored for 騙經 (Pian Jing) Bilingual Reader.
Extracts ZH/EN pairs from data/book.json (or index.html), evaluates them using
ModelScope Qwen with criteria aligned with TRANSLATION_GUIDE.md, and saves a scored
JSON report + prints summary statistics.

Usage:
    python scripts/verify_translations.py               # Sample default 50 pairs
    python scripts/verify_translations.py --sample 20   # Sample 20 pairs
    python scripts/verify_translations.py --all         # Score all pairs in the book
    python scripts/verify_translations.py --resume      # Skip already-scored pairs
    python scripts/verify_translations.py --category c01 # Score pairs in category c01
    python scripts/verify_translations.py --notes-only  # Focus on author commentary (按語)
    python scripts/verify_translations.py --output report.json

API credentials are loaded from .env in the project root.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Ensure UTF-8 output on Windows terminals to prevent charmap UnicodeEncodeErrors
if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Optional requests library, fall back to urllib
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


class InsufficientBalanceError(RuntimeError):
    """Raised when ModelScope balance is exhausted."""
    pass


# ---------------------------------------------------------------------------
# Config / .env loader
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(env_file=None):
    """Load KEY=VALUE pairs from .env without overriding existing environment variables."""
    path = env_file or (ROOT / ".env")
    if not Path(path).is_file():
        return
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()

MODELSCOPE_SDK_TOKEN = (
    os.environ.get("MODELSCOPE_SDK_TOKEN") or os.environ.get("MODELSCOPE_API_KEY") or ""
).strip()
MODELSCOPE_BASE_URL = os.environ.get(
    "MODELSCOPE_BASE_URL", "https://api-inference.modelscope.ai/v1"
).rstrip("/")
MODELSCOPE_MODEL = os.environ.get("MODELSCOPE_MODEL", "Qwen/Qwen3.5-35B-A3B").strip()

DEFAULT_SAMPLE = int(os.environ.get("QA_SAMPLE", "50"))
DEFAULT_DELAY = float(os.environ.get("QA_DELAY", "3.0"))
DEFAULT_REPORT = ROOT / os.environ.get("QA_REPORT", "translation_qa_report.json")
DEFAULT_ISSUES_MD = ROOT / os.environ.get("QA_ISSUES_MD", "TRANSLATION_ISSUES.md")
DEFAULT_DATA_BOOK = ROOT / "data" / "book.json"
DEFAULT_INDEX_HTML = ROOT / "index.html"


# ---------------------------------------------------------------------------
# Pair extraction
# ---------------------------------------------------------------------------

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def strip_html(html_text: str) -> str:
    text = TAG_RE.sub("", html_text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    return WS_RE.sub(" ", text).strip()


def extract_pairs_from_json(book_path: Path) -> list[dict]:
    """Extract all ZH/EN pairs directly from data/book.json."""
    data = json.loads(book_path.read_text(encoding="utf-8"))
    pairs = []

    for cat in data.get("categories", []):
        cat_id = cat.get("id", "")
        cat_zh = cat.get("title_zh", "")
        cat_en = cat.get("title_en", "")

        for i, story in enumerate(cat.get("stories", [])):
            sid = f"{cat_id}-s{i:02d}"
            title_zh = story.get("title_zh", "").strip()
            title_en = story.get("title_en", "").strip()

            if title_zh and title_en:
                pairs.append({
                    "pair_type": "pair-entry-title",
                    "category_id": cat_id,
                    "category_title": f"{cat_zh} {cat_en}".strip(),
                    "story_id": sid,
                    "story_title": title_zh,
                    "paragraph_index": 0,
                    "kind": "title",
                    "zh": title_zh,
                    "en": title_en,
                })

            for p_idx, para in enumerate(story.get("paragraphs", []), 1):
                zh = (para.get("zh") or "").strip()
                en = (para.get("en") or "").strip()
                kind = para.get("kind", "body")
                pair_type = "pair-note" if kind == "note" else "pair-body"

                if not zh or not en or (len(zh) < 2 and len(en) < 2):
                    continue

                pairs.append({
                    "pair_type": pair_type,
                    "category_id": cat_id,
                    "category_title": f"{cat_zh} {cat_en}".strip(),
                    "story_id": sid,
                    "story_title": title_zh,
                    "paragraph_index": p_idx,
                    "kind": kind,
                    "zh": zh,
                    "en": en,
                })

    return pairs


def extract_pairs_from_html(html_path: Path) -> list[dict]:
    """Fallback extractor from index.html using regex, compatible with leeao-bilingual."""
    text = html_path.read_text(encoding="utf-8")

    # Match chapter and section headings
    SEC_RE = re.compile(r'<section[^>]*id="([^"]+)"[^>]*>.*?<h2>(.*?)</h2>', re.DOTALL)
    section_spans = []
    for m in SEC_RE.finditer(text):
        section_spans.append((m.start(), m.group(1), strip_html(m.group(2))))

    def section_at(pos):
        best = ("", "")
        for start, sec_id, sec_title in section_spans:
            if start <= pos:
                best = (sec_id, sec_title)
        return best

    PAIR_RE = re.compile(
        r'<div class="pair\s*([^"]*)">\s*'
        r'<div class="col zh">(.*?)</div>\s*'
        r'<div class="col en">(.*?)</div>\s*'
        r'</div>',
        re.DOTALL,
    )

    pairs = []
    for idx, m in enumerate(PAIR_RE.finditer(text)):
        pair_type = m.group(1).strip() or "pair-body"
        zh = strip_html(m.group(2)).strip()
        en = strip_html(m.group(3)).strip()
        if not zh or not en or (len(zh) < 2 and len(en) < 2):
            continue
        sec_id, sec_title = section_at(m.start())
        kind = "note" if "note" in pair_type else ("title" if "title" in pair_type else "body")
        pairs.append({
            "pair_type": pair_type,
            "category_id": sec_id,
            "category_title": sec_title,
            "story_id": "",
            "story_title": "",
            "paragraph_index": idx,
            "kind": kind,
            "zh": zh,
            "en": en,
        })
    return pairs


# ---------------------------------------------------------------------------
# Qwen Evaluation Prompt & Client
# ---------------------------------------------------------------------------

QA_SYSTEM = """\
You are a professional Chinese-English translation quality evaluator specialized in classical and late-Ming vernacular Chinese literature (specifically Zhang Yingyu's 《騙經》/《杜騙新書》 Book of Swindlers).

The project style guide (TRANSLATION_GUIDE.md) establishes these core translation principles:
1. "Meaning-first study crib" — accurate, plain, natural English to read alongside the Chinese; translate meaning rather than word order.
2. Ming vernacular idiom:
   - 脫 / 脫去 means "get away with / make off with / cheat out of" (NOT literal "strip").
   - 棍 refers to "swindler / con man / sharp / trickster / crook" (avoid robotic overuse).
   - 按 / 吾觀 commentary is moral commentary with a direct, wry voice (not stiff "Note: I observe").
   - 江湖 means "the road / traveling merchants / drifting life" (NOT literal "rivers and lakes").
   - 高手 means "real master / top hand / pro" (NOT "high hand").
   - 兩 / 銀 uses "taels" for 兩 and "pure/fineness" for 成色.
   - 緞 is heavy luxury "silk" (not modern shiny "satin").
3. Do not penalize natural idioms that convey the true Ming meaning faithfully.

Given a Chinese source segment and its English translation, evaluate the quality.

Output ONLY valid JSON (no markdown fences, no commentary outside JSON) with this exact shape:
{
  "score": <integer 1-10>,
  "accuracy": <integer 1-10>,
  "fluency": <integer 1-10>,
  "issues": ["list of specific issues, or empty array"],
  "suggestion": "improved English translation, or empty string if score >= 9"
}

Scoring guide (score):
  10 — Perfect. Accurate, fluent, captures Ming meaning and register.
  8-9 — Very good. Faithful, natural, minor style preference only.
  6-7 — Good but has a noticeable issue (awkward phrasing, slight distortion, literalism).
  4-5 — Acceptable but has a real error, omission, or stiff calque.
  2-3 — Poor. Significant mistranslation, hallucination, or wrong meaning.
  1   — Completely wrong or unrelated.
"""


def _post_json(url: str, payload: dict, headers: dict, timeout: int = 60) -> tuple[int, str]:
    """Execute HTTP POST with JSON body via requests or urllib."""
    if HAS_REQUESTS:
        r = requests.post(url, json=payload, headers=headers, timeout=(10, timeout))
        return r.status_code, r.text

    # Urllib fallback
    data_bytes = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data_bytes, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, body
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, body
    except URLError as e:
        raise RuntimeError(f"Network error connecting to {url}: {e.reason}") from e


def _extract_json(text: str) -> dict:
    """Robust JSON extraction from LLM response with fallback repair for truncation."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Heuristic auto-repair if output was cut off mid-string
        repaired = text.strip()
        if not repaired.endswith("}"):
            if repaired.count('"') % 2 != 0:
                repaired += '"'
            if not repaired.endswith("}"):
                repaired += "}"
        return json.loads(repaired)


def _validate_qa(data: dict) -> dict:
    """Validate and sanitize QA response fields."""
    score = int(data.get("score", 7))
    score = max(1, min(10, score))

    accuracy = int(data.get("accuracy", score))
    accuracy = max(1, min(10, accuracy))

    fluency = int(data.get("fluency", score))
    fluency = max(1, min(10, fluency))

    issues = data.get("issues")
    if not isinstance(issues, list):
        issues = [str(issues)] if issues else []
    issues = [str(x).strip() for x in issues if str(x).strip()]

    suggestion = str(data.get("suggestion", "")).strip()

    return {
        "score": score,
        "accuracy": accuracy,
        "fluency": fluency,
        "issues": issues,
        "suggestion": suggestion,
    }


def qa_pair(zh: str, en: str, kind: str = "body", model: str | None = None) -> dict:
    """Score one ZH/EN pair via ModelScope Qwen."""
    if not MODELSCOPE_SDK_TOKEN:
        raise RuntimeError(
            "Missing MODELSCOPE_SDK_TOKEN. Add it to .env:\n"
            "  MODELSCOPE_SDK_TOKEN=ms-xxxx\n"
            "Get a token at https://www.modelscope.ai/my/myaccesstoken"
        )

    user_msg = f"Segment Type: {kind}\n\nChinese: {zh}\n\nEnglish translation: {en}"
    payload = {
        "model": model or MODELSCOPE_MODEL,
        "messages": [
            {"role": "system", "content": QA_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.1,
        "max_tokens": 2048,
        "stream": False,
        "response_format": {"type": "json_object"},
        "enable_thinking": False,
    }
    headers = {
        "Authorization": f"Bearer {MODELSCOPE_SDK_TOKEN}",
        "Content-Type": "application/json",
    }
    url = f"{MODELSCOPE_BASE_URL}/chat/completions"

    last_err = None
    for attempt in range(6):
        try:
            status_code, response_text = _post_json(url, payload, headers, timeout=60)
        except Exception as e:
            last_err = e
            time.sleep(2 ** attempt)
            continue

        if status_code >= 400:
            if "insufficient" in response_text.lower() or "balance" in response_text.lower():
                raise InsufficientBalanceError(f"ModelScope HTTP {status_code}: {response_text[:300]}")

            if status_code in (429, 502, 503, 504):
                # Generous exponential backoff for rate limits (8s, 16s, 32s, 64s...)
                wait = 8 * (2 ** min(attempt, 3))
                print(f"  [Rate limit HTTP {status_code}, backing off for {wait}s (attempt {attempt+1}/6)]", file=sys.stderr)
                time.sleep(wait)
                continue

            raise RuntimeError(f"ModelScope HTTP {status_code}: {response_text[:400]}")

        try:
            data = json.loads(response_text)
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Unexpected response payload: {response_text[:300]}") from e

        if not content.strip():
            last_err = RuntimeError("Empty content returned")
            time.sleep(2 ** attempt)
            continue

        try:
            parsed = _extract_json(content)
            return _validate_qa(parsed)
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(2 ** attempt)

    raise RuntimeError(f"Failed to score pair after 6 attempts: {last_err}")


# ---------------------------------------------------------------------------
# Runner and Reporting
# ---------------------------------------------------------------------------

def _pair_key(item: dict) -> str:
    return f"{item.get('category_id', '')}__{item.get('story_id', '')}__{item.get('zh', '')[:50]}"


def _top_issues(issues: list, n: int = 10) -> list[dict]:
    counts: dict[str, int] = {}
    for issue in issues:
        key = str(issue).strip().lower()
        if key:
            counts[key] = counts.get(key, 0) + 1
    return [
        {"issue": k, "count": v}
        for k, v in sorted(counts.items(), key=lambda x: -x[1])[:n]
    ]


def _save_report(results: list[dict], path: Path, model: str):
    scored = [r for r in results if "score" in r]
    scores = [r["score"] for r in scored]
    accuracy = [r.get("accuracy", r["score"]) for r in scored]
    fluency = [r.get("fluency", r["score"]) for r in scored]
    all_issues = [issue for r in scored for issue in r.get("issues", [])]
    flagged = [r for r in scored if r["score"] <= 6]

    report = {
        "model": model,
        "total_pairs_scored": len(scored),
        "errors": len(results) - len(scored),
        "summary": {
            "avg_score": round(sum(scores) / len(scores), 2) if scores else 0,
            "avg_accuracy": round(sum(accuracy) / len(accuracy), 2) if accuracy else 0,
            "avg_fluency": round(sum(fluency) / len(fluency), 2) if fluency else 0,
            "score_distribution": {str(s): scores.count(s) for s in range(1, 11)},
            "flagged_count": len(flagged),
            "common_issues": _top_issues(all_issues),
        },
        "flagged_pairs": [
            {
                "category_id": r.get("category_id"),
                "story_id": r.get("story_id"),
                "story_title": r.get("story_title"),
                "kind": r.get("kind"),
                "score": r["score"],
                "accuracy": r.get("accuracy"),
                "fluency": r.get("fluency"),
                "zh": r["zh"],
                "en": r["en"],
                "issues": r.get("issues", []),
                "suggestion": r.get("suggestion", ""),
            }
            for r in sorted(flagged, key=lambda x: x["score"])
        ],
        "results": results,
    }
def _save_markdown_issues(results: list[dict], path: Path, model: str):
    """Write an actionable Markdown document listing all flagged translation issues and suggestions."""
    scored = [r for r in results if "score" in r]
    if not scored:
        return
    scores = [r["score"] for r in scored]
    accuracy = [r.get("accuracy", r["score"]) for r in scored]
    fluency = [r.get("fluency", r["score"]) for r in scored]
    all_issues = [issue for r in scored for issue in r.get("issues", [])]
    # Filter for segments with issues or score <= 7
    problem_pairs = [r for r in scored if r["score"] <= 7 or r.get("issues")]

    lines = [
        "# 騙經 (Pian Jing) Translation QA — Issues & Feedback Report",
        "",
        f"- **Evaluator Model**: `{model}`",
        f"- **Total Scored**: {len(scored)} segments",
        f"- **Average Overall Score**: {sum(scores)/len(scores):.2f} / 10",
        f"- **Average Accuracy**: {sum(accuracy)/len(accuracy):.2f} / 10",
        f"- **Average Fluency**: {sum(fluency)/len(fluency):.2f} / 10",
        f"- **Flagged Segments (Score <= 6)**: {len([r for r in scored if r['score'] <= 6])}",
        f"- **Total Segments with Notes/Issues**: {len(problem_pairs)}",
        "",
        "## Top Recurring Issues",
        "",
    ]

    top = _top_issues(all_issues, n=20)
    if top:
        for item in top:
            lines.append(f"- **({item['count']}x)** {item['issue']}")
    else:
        lines.append("No common issues recorded.")
    lines.extend(["", "---", "", "## Flagged Translation Issues & Recommendations", ""])

    if not problem_pairs:
        lines.append("No translation issues found. All scored segments are 8/10 or higher with zero reported issues.")
    else:
        for idx, r in enumerate(sorted(problem_pairs, key=lambda x: (x["score"], x.get("accuracy", 10))), 1):
            cat = r.get("category_title") or r.get("category_id", "")
            sid = r.get("story_id", "")
            stitle = r.get("story_title", "")
            kind = r.get("kind", "body")
            score = r["score"]
            acc = r.get("accuracy", score)
            flu = r.get("fluency", score)

            lines.append(f"### {idx}. [{score}/10] {cat} — {stitle} ({sid}) `[{kind}]`")
            lines.append(f"- **Score**: **{score}/10** (Accuracy: **{acc}/10**, Fluency: **{flu}/10**)")
            lines.append("- **Chinese Source**:")
            lines.append(f"  > {r['zh']}")
            lines.append("- **Current English**:")
            lines.append(f"  > {r['en']}")
            if r.get("issues"):
                lines.append("- **Identified Issues**:")
                for issue in r["issues"]:
                    lines.append(f"  - {issue}")
            if r.get("suggestion"):
                lines.append("- **Recommended Revision**:")
                lines.append(f"  > {r['suggestion']}")
            lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _save_report(results: list[dict], path: Path, model: str, issues_path: Path | None = None):
    scored = [r for r in results if "score" in r]
    scores = [r["score"] for r in scored]
    accuracy = [r.get("accuracy", r["score"]) for r in scored]
    fluency = [r.get("fluency", r["score"]) for r in scored]
    all_issues = [issue for r in scored for issue in r.get("issues", [])]
    flagged = [r for r in scored if r["score"] <= 6]

    report = {
        "model": model,
        "total_pairs_scored": len(scored),
        "errors": len(results) - len(scored),
        "summary": {
            "avg_score": round(sum(scores) / len(scores), 2) if scores else 0,
            "avg_accuracy": round(sum(accuracy) / len(accuracy), 2) if accuracy else 0,
            "avg_fluency": round(sum(fluency) / len(fluency), 2) if fluency else 0,
            "score_distribution": {str(s): scores.count(s) for s in range(1, 11)},
            "flagged_count": len(flagged),
            "common_issues": _top_issues(all_issues),
        },
        "flagged_pairs": [
            {
                "category_id": r.get("category_id"),
                "story_id": r.get("story_id"),
                "story_title": r.get("story_title"),
                "kind": r.get("kind"),
                "score": r["score"],
                "accuracy": r.get("accuracy"),
                "fluency": r.get("fluency"),
                "zh": r["zh"],
                "en": r["en"],
                "issues": r.get("issues", []),
                "suggestion": r.get("suggestion", ""),
            }
            for r in sorted(flagged, key=lambda x: x["score"])
        ],
        "results": results,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")

    # Also save human-readable Markdown issues file
    target_issues_path = issues_path or (path.parent / "TRANSLATION_ISSUES.md")
    _save_markdown_issues(results, target_issues_path, model)


def _print_summary(results: list[dict]):
    scored = [r for r in results if "score" in r]
    if not scored:
        print("\nNo pairs were scored successfully.")
        return
    scores = [r["score"] for r in scored]
    flagged = [r for r in scored if r["score"] <= 6]
    print(
        f"\n{'='*64}\n"
        f"TRANSLATION QA SUMMARY\n"
        f"{'='*64}\n"
        f"  Total pairs scored : {len(scored)}\n"
        f"  Average Score      : {sum(scores)/len(scores):.2f}/10\n"
        f"  Average Accuracy   : {sum(r.get('accuracy', r['score']) for r in scored)/len(scored):.2f}/10\n"
        f"  Average Fluency    : {sum(r.get('fluency', r['score']) for r in scored)/len(scored):.2f}/10\n"
        f"  Flagged (<=6/10)   : {len(flagged)} ({100*len(flagged)//len(scored)}%)\n"
    )
    if flagged:
        print("  Top flagged items:")
        for r in sorted(flagged, key=lambda x: x["score"])[:5]:
            zh_preview = r["zh"][:50].replace("\n", " ")
            en_preview = r["en"][:50].replace("\n", " ")
            print(f"    [{r['score']}/10] ({r.get('kind', 'body')}) {zh_preview} -> {en_preview}")
            if r.get("issues"):
                print(f"           Issues: {'; '.join(r['issues'][:2])}")
            if r.get("suggestion"):
                print(f"           Suggestion: {r['suggestion'][:80]}")


def run_qa(
    pairs: list[dict],
    sample: int | None = None,
    report_path: Path = DEFAULT_REPORT,
    issues_path: Path = DEFAULT_ISSUES_MD,
    resume: bool = False,
    model: str | None = None,
    delay: float = DEFAULT_DELAY,
):
    """Run QA loop over selected pairs with incremental checkpointing."""
    existing: dict[str, dict] = {}
    if resume and report_path.is_file():
        try:
            prev = json.loads(report_path.read_text(encoding="utf-8"))
            for item in prev.get("results", []):
                if "score" in item:
                    existing[_pair_key(item)] = item
            print(f"Resuming: {len(existing)} pairs already scored in {report_path.name}")
        except Exception as e:
            print(f"Warning: could not load existing report for resume: {e}", file=sys.stderr)

    # Sampling strategy across categories & kinds
    if sample is not None and sample > 0 and sample < len(pairs):
        by_kind: dict[str, list[dict]] = {}
        for p in pairs:
            by_kind.setdefault(p.get("kind", "body"), []).append(p)

        selected = []
        per_kind = max(1, sample // len(by_kind))
        for kind_name, kind_pairs in by_kind.items():
            random.shuffle(kind_pairs)
            selected.extend(kind_pairs[:per_kind])

        if len(selected) < sample:
            remainder = [p for p in pairs if p not in selected]
            random.shuffle(remainder)
            selected.extend(remainder[: sample - len(selected)])
        random.shuffle(selected)
        target_pairs = selected[:sample]
    else:
        target_pairs = list(pairs)

    if resume:
        target_pairs = [p for p in target_pairs if _pair_key(p) not in existing]
        print(f"Target to score: {len(target_pairs)} new pairs.")

    total = len(target_pairs)
    results = list(existing.values()) if resume else []
    active_model = model or MODELSCOPE_MODEL

    print(
        f"\n{'='*64}\n"
        f"Translation QA | {total} pairs | Model: {active_model} | Delay: {delay}s\n"
        f"{'='*64}"
    )

    for i, pair in enumerate(target_pairs, 1):
        zh_snip = pair["zh"][:60].replace("\n", " ")
        en_snip = pair["en"][:60].replace("\n", " ")
        story_info = f" | {pair['story_id']}" if pair.get("story_id") else ""
        print(f"\n[{i}/{total}] ({pair.get('kind', 'body')}) {pair.get('category_id', '')}{story_info}")
        print(f"  ZH: {zh_snip}")
        print(f"  EN: {en_snip}")

        try:
            qa = qa_pair(pair["zh"], pair["en"], kind=pair.get("kind", "body"), model=active_model)
            result = {**pair, **qa}
            results.append(result)

            issues_str = "; ".join(qa["issues"]) if qa["issues"] else "none"
            print(
                f"  Score: {qa['score']}/10  (Accuracy: {qa['accuracy']}, Fluency: {qa['fluency']})"
            )
            print(f"  Issues: {issues_str}")
            if qa.get("suggestion"):
                print(f"  -> Suggestion: {qa['suggestion'][:90]}")

        except Exception as e:
            err_msg = str(e)
            print(f"  ERROR: {err_msg}", file=sys.stderr)
            if isinstance(e, InsufficientBalanceError) or "insufficient" in err_msg.lower() or "balance" in err_msg.lower():
                print(
                    "\n[Insufficient balance detected. Stopping task immediately.]\n",
                    file=sys.stderr,
                )
                _save_report(results, report_path, active_model, issues_path)
                _print_summary(results)
                print(f"\nReport saved to: {report_path}")
                print(f"Issues saved to: {issues_path}")
                _auto_git_sync(f"Update translation QA report and issues ({len([r for r in results if 'score' in r])} pairs scored)")
                sys.exit(0)
            results.append({**pair, "error": err_msg})
            time.sleep(max(6.0, delay * 2))

        _save_report(results, report_path, active_model, issues_path)
        # Polite spacing to avoid rate limits
        sleep_time = max(0.5, delay) + random.uniform(0.1, 0.6)
        time.sleep(sleep_time)

    _print_summary(results)
    print(f"\nReport saved to: {report_path}")
    print(f"Issues saved to: {issues_path}")
    _auto_git_sync("Complete translation QA evaluation for all pairs")


def _auto_git_sync(message: str):
    """Automatically commit and push issues and script changes if inside git repo."""
    try:
        import subprocess
        if not (ROOT / ".git").is_dir():
            return
        subprocess.run(["git", "add", "TRANSLATION_ISSUES.md", "README.md", "scripts/verify_translations.py"], cwd=ROOT, check=False)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", message], cwd=ROOT, check=False)
            env = dict(os.environ)
            local_bin = str(Path.home() / ".local" / "bin")
            if local_bin not in env.get("PATH", ""):
                env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"
            res = subprocess.run(["git", "push", "origin", "main"], cwd=ROOT, env=env, check=False, capture_output=True, text=True)
            if res.returncode == 0:
                print(f"[Auto-git] Pushed changes to GitHub: {message}")
            else:
                print(f"[Auto-git] Git push note: {res.stderr.strip()}", file=sys.stderr)
    except Exception as e:
        print(f"[Auto-git] Note: could not auto-push to git: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Verify Chinese->English translation quality for 騙經 (Pian Jing) using ModelScope Qwen"
    )
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                        help=f"Number of pairs to sample and score (default: {DEFAULT_SAMPLE}). 0 = all.")
    parser.add_argument("--all", action="store_true",
                        help="Score every pair in the book (overrides --sample).")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help=f"Seconds to pause between API calls (default: {DEFAULT_DELAY}s).")
    parser.add_argument("--category", type=str, default=None,
                        help="Filter by category ID (e.g. c01, c02, etc.)")
    parser.add_argument("--notes-only", action="store_true",
                        help="Evaluate only author commentary notes (按語)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip pairs already scored in the output report.")
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT,
                        help=f"JSON report output path (default: {DEFAULT_REPORT})")
    parser.add_argument("--issues-file", type=Path, default=DEFAULT_ISSUES_MD,
                        help=f"Markdown issues file path (default: {DEFAULT_ISSUES_MD})")
    parser.add_argument("--model", type=str, default=None,
                        help=f"ModelScope model ID (default: {MODELSCOPE_MODEL})")
    parser.add_argument("--json", type=Path, default=None,
                        help="Path to book.json (auto-detected by default)")
    parser.add_argument("--html", type=Path, default=None,
                        help="Path to index.html to extract from instead of book.json")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for sampling (default: 42)")

    args = parser.parse_args()
    random.seed(args.seed)

    # 1. Extraction source
    if args.html:
        print(f"Extracting pairs from HTML: {args.html}")
        pairs = extract_pairs_from_html(args.html)
    else:
        json_path = args.json or DEFAULT_DATA_BOOK
        if json_path.is_file():
            print(f"Extracting pairs from JSON: {json_path}")
            pairs = extract_pairs_from_json(json_path)
        elif DEFAULT_INDEX_HTML.is_file():
            print(f"Extracting pairs from HTML: {DEFAULT_INDEX_HTML}")
            pairs = extract_pairs_from_html(DEFAULT_INDEX_HTML)
        else:
            sys.exit(f"ERROR: Could not find {DEFAULT_DATA_BOOK} or {DEFAULT_INDEX_HTML}")

    # 2. Filters
    if args.category:
        cat_filter = args.category.strip().lower()
        pairs = [p for p in pairs if p.get("category_id", "").lower() == cat_filter]
        print(f"Filtered by category '{args.category}': {len(pairs)} pairs")

    if args.notes_only:
        pairs = [p for p in pairs if p.get("kind") == "note"]
        print(f"Filtered to notes only: {len(pairs)} pairs")

    if not pairs:
        sys.exit("No pairs found matching criteria.")

    print(f"Total candidate pairs: {len(pairs)}")

    sample = 0 if args.all else args.sample
    run_qa(
        pairs=pairs,
        sample=sample if sample > 0 else None,
        report_path=args.output,
        issues_path=args.issues_file,
        resume=args.resume,
        model=args.model,
        delay=args.delay,
    )


if __name__ == "__main__":
    main()


