#!/usr/bin/env python3
"""Popup Dictionary QA & Repair — verify and fix hover dictionary entries using ModelScope Qwen.

Simulates mouse-hover word lookups (`findLongestMatchingWordAt` from dictionary.js) across the
book's Chinese text, evaluates the resulting popup cards (headword, pinyin,
definitions, and "In this book" example snippet) using ModelScope Qwen, writes
`dictionary_qa_report.json`, and optionally applies Qwen's verified fixes to
`i18n/dict.json`.

Usage:
    python3 scripts/verify_dictionary.py                        # verify top/sampled 120 hover entries
    python3 scripts/verify_dictionary.py --sample 200           # verify 200 entries
    python3 scripts/verify_dictionary.py --all                  # verify all entries in i18n/dict.json
    python3 scripts/verify_dictionary.py --resume               # resume from existing report
    python3 scripts/verify_dictionary.py --apply                # apply Qwen fixes from report to i18n/dict.json
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from verify_translations import (
    MODELSCOPE_MODEL,
    ROOT,
    InsufficientBalanceError,
    chat_json,
    strip_html,
)

HTML_PATH = ROOT / "index.html"
DICT_JSON_PATH = ROOT / "i18n" / "dict.json"
DEFAULT_DICT_QA_REPORT_PATH = ROOT / "dictionary_qa_report.json"

CHINESE_CHAR_PATTERN = re.compile(r"[\u3400-\u9FFF\uF900-\uFAFF]")
CHINESE_COLUMN_PATTERN = re.compile(r'<div class="col zh">(.*?)</div>', re.DOTALL)

DICT_QA_SYSTEM_PROMPT = """\
You are a senior Chinese-English lexicographer and classical/Ming-vernacular scholar auditing
the hover popup dictionary for a bilingual edition of Zhang Yingyu's 《騙經》 / 《江湖歷覽杜騙新書》
(*The Book of Swindles*, c. 1617), whose text includes both Traditional and Simplified characters.

When a reader hovers their mouse over Chinese characters in the book, a popup card displays:
- "word": the matched Chinese headword
- "pinyin": the pronunciation with tone marks
- "defs": up to 4 concise English definitions
- "in_this_book": an example sentence snippet from the book

For every item in the input `items` array, evaluate the quality of the popup card
and return ONLY valid JSON (no markdown fences) with this exact structure:
{
  "results": [
    {
      "word": "<exact input word>",
      "score": <integer 1-10>,
      "pinyin_ok": <boolean>,
      "defs_ok": <boolean>,
      "context_ok": <boolean>,
      "issues": ["specific issue 1", "specific issue 2"],
      "suggested_pinyin": "<corrected tone-marked lowercase pinyin, or empty string if score >= 9>",
      "suggested_defs": ["<concise definition 1>", "<concise definition 2>"]
    }
  ]
}

Scoring rubric:
- 9-10: Excellent. Pinyin is the right reading for how the word is used in late-Ming/classical prose, definitions are clear and accurate, and they fit `in_this_book`.
- 7-8: Good/usable, but has minor clutter (e.g. raw CEDICT `Trad|Simp[pin1 yin1]` brackets, overly verbose cross-references, modern-only glosses missing the classical sense, or secondary reading listed first).
- 4-6: Flawed. Truncated definition (cut off mid-word/sentence), wrong heteronym pinyin for how the word is used in `in_this_book` (e.g. `shuì` instead of `shuō` for 說/说, capitalized surname `Dū` for 都, `wéi` vs `wèi` swapped improperly, `bó` vs `bái`), or missing the primary Ming/classical sense used in `in_this_book`.
- 1-3: Broken. Only gives a surname/archaic variant for a common non-surname word (e.g. 那 as "surname Na", 單 as "surname Shan", 干 as "old variant / surname Gan", 相 as "surname Xiang"), or completely wrong meaning. (Note: For characters primarily used as surnames in the book such as 鄧/邓, 廖, 蔡, 劉/刘, 潘, 鄒/邹, 郝, capitalized surname pinyin like `Dèng` with `surname Deng` is accurate and should NOT be penalized or lowercased.)

Rules for `suggested_pinyin` and `suggested_defs`:
- Use standard tone marks (e.g. `dōu`, `shuō`, `gàn`, `nà`, `dān`, `yín`). Multi-syllable words should preserve proper spacing/capitalization (only capitalize proper nouns: people, places, dynasties, book titles).
- Provide 1 to 3 concise, reader-friendly English definitions (each under 110 characters, never truncated mid-sentence, no raw `Trad|Simp[pinyin]` brackets).
- Put the most common / contextually relevant meaning in `in_this_book` FIRST so the reader immediately understands the word in the text.
"""


def find_longest_matching_word_at(
    text: str, char_index: int, entries: dict, max_word_length: int = 8
) -> str:
    """Exact port of dictionary.js findLongestMatchingWordAt(text, charIndex)."""
    if (
        char_index < 0
        or char_index >= len(text)
        or not CHINESE_CHAR_PATTERN.match(text[char_index])
    ):
        return ""
    longest_match = ""
    min_start = max(0, char_index - max_word_length + 1)
    for start in range(min_start, char_index + 1):
        max_end = min(len(text), start + max_word_length)
        for end in range(char_index + 1, max_end + 1):
            candidate = text[start:end]
            if len(candidate) >= len(longest_match) and candidate in entries:
                longest_match = candidate
    return longest_match


def simulate_book_hover_frequencies(
    html_path: Path, entries: dict, max_word_length: int = 8
) -> Counter:
    """Simulate hovering over every Chinese character in the book and count matched words."""
    html_content = html_path.read_text(encoding="utf-8")
    word_hover_counts: Counter = Counter()
    for column_html in CHINESE_COLUMN_PATTERN.findall(html_content):
        plain_text = strip_html(column_html).replace("■", "").replace("□", "")
        compact_text = re.sub(r"\s+", "", plain_text)
        for char_index, char in enumerate(compact_text):
            if CHINESE_CHAR_PATTERN.match(char):
                matched_word = find_longest_matching_word_at(
                    compact_text, char_index, entries, max_word_length
                )
                if matched_word:
                    word_hover_counts[matched_word] += 1
    return word_hover_counts


def compute_heuristic_risk_score(
    word: str, entry_row: list, hover_count: int
) -> int:
    """Prioritize entries most likely to have hover quality issues or high visibility."""
    pinyin = entry_row[0] if len(entry_row) > 0 else ""
    definitions = entry_row[1] if len(entry_row) > 1 else []
    risk_points = 0

    if any(len(d) >= 138 for d in definitions):
        risk_points += 100
    if any(
        "surname " in d.lower()
        or "old variant of" in d.lower()
        or "used in " in d.lower()
        for d in definitions
    ):
        risk_points += 80
    if pinyin and pinyin[0].isupper() and len(word) == 1:
        risk_points += 70
    if any("|" in d or "[" in d for d in definitions):
        risk_points += 40
    if len(word) == 1 and hover_count >= 5:
        risk_points += 30

    return risk_points * 1000 + min(hover_count, 999)


def select_candidate_entries_for_qa(
    entries: dict, hover_counts: Counter, sample_size: int, verify_all: bool
) -> list[dict]:
    all_candidates = []
    for word, row in entries.items():
        hover_count = hover_counts.get(word, 0)
        ex_list = row[2] if len(row) > 2 and row[2] else []
        first_ex = ex_list[0][0] if ex_list and isinstance(ex_list[0], list) else (ex_list[0] if ex_list else "")
        all_candidates.append(
            {
                "word": word,
                "pinyin": row[0] if len(row) > 0 else "",
                "defs": row[1] if len(row) > 1 else [],
                "in_this_book": first_ex,
                "hover_count": hover_count,
                "risk_key": compute_heuristic_risk_score(word, row, hover_count),
            }
        )

    sorted_by_frequency = sorted(
        all_candidates, key=lambda item: (-item["hover_count"], item["word"])
    )
    if verify_all or sample_size <= 0:
        return sorted_by_frequency

    sorted_by_risk = sorted(
        all_candidates, key=lambda item: (-item["risk_key"], item["word"])
    )
    half_sample = sample_size // 2
    selected_words: set[str] = set()
    selected_items: list[dict] = []

    for item in sorted_by_frequency:
        if len(selected_items) >= half_sample:
            break
        selected_items.append(item)
        selected_words.add(item["word"])

    for item in sorted_by_risk:
        if len(selected_items) >= sample_size:
            break
        if item["word"] not in selected_words:
            selected_items.append(item)
            selected_words.add(item["word"])

    return selected_items


def normalize_string_list(raw_value: object, max_items: int | None = None) -> list[str]:
    if isinstance(raw_value, str):
        raw_value = [raw_value]
    if not isinstance(raw_value, list):
        return []
    cleaned = [str(item).strip() for item in raw_value if str(item).strip()]
    return cleaned[:max_items] if max_items else cleaned


def validate_qwen_batch_response(
    response_data: dict, expected_words: list[str]
) -> list[dict]:
    raw_results = response_data.get("results")
    if not isinstance(raw_results, list) or not raw_results:
        raise ValueError("Missing or empty 'results' array in Qwen response")

    results_by_word = {
        str(item.get("word", "")).strip(): item
        for item in raw_results
        if isinstance(item, dict)
    }

    validated_items: list[dict] = []
    for index, word in enumerate(expected_words):
        raw_item = results_by_word.get(word)
        if (
            not raw_item
            and index < len(raw_results)
            and isinstance(raw_results[index], dict)
        ):
            raw_item = raw_results[index]
        if not raw_item:
            raise ValueError(f"Missing word '{word}' in Qwen batch response")

        score = int(raw_item.get("score", 0))
        if not 1 <= score <= 10:
            raise ValueError(f"Invalid score {score} for word '{word}'")

        validated_items.append(
            {
                "word": word,
                "score": score,
                "pinyin_ok": bool(raw_item.get("pinyin_ok", score >= 8)),
                "defs_ok": bool(raw_item.get("defs_ok", score >= 8)),
                "context_ok": bool(raw_item.get("context_ok", score >= 8)),
                "issues": normalize_string_list(raw_item.get("issues")),
                "suggested_pinyin": str(raw_item.get("suggested_pinyin") or "").strip(),
                "suggested_defs": normalize_string_list(
                    raw_item.get("suggested_defs"), max_items=3
                ),
            }
        )
    return validated_items


def evaluate_dictionary_batch_with_qwen(
    batch_items: list[dict], model: str | None = None
) -> list[dict]:
    expected_words = [item["word"] for item in batch_items]
    request_payload = {
        "items": [
            {
                "word": item["word"],
                "pinyin": item["pinyin"],
                "defs": item["defs"],
                "in_this_book": item["in_this_book"],
            }
            for item in batch_items
        ]
    }
    user_message = json.dumps(request_payload, ensure_ascii=False)
    scored_items = chat_json(
        DICT_QA_SYSTEM_PROMPT,
        user_message,
        lambda data: validate_qwen_batch_response(data, expected_words),
        model=model,
        max_tokens=4096,
    )

    metadata_by_word = {item["word"]: item for item in batch_items}
    merged_results = []
    for scored in scored_items:
        original = metadata_by_word[scored["word"]]
        merged_results.append(
            {
                "word": scored["word"],
                "hover_count": original["hover_count"],
                "pinyin": original["pinyin"],
                "defs": original["defs"],
                "in_this_book": original["in_this_book"],
                **scored,
            }
        )
    return merged_results


def load_existing_dictionary_qa_results(report_path: Path) -> dict[str, dict]:
    if not report_path.is_file():
        return {}
    previous_report = json.loads(report_path.read_text(encoding="utf-8"))
    return {
        item["word"]: item
        for item in previous_report.get("results", [])
        if "word" in item and "score" in item
    }


def save_dictionary_qa_report(
    results_by_word: dict[str, dict],
    report_path: Path,
    model: str,
    total_dict_entries: int,
    total_hover_positions: int,
) -> None:
    scored_items = [item for item in results_by_word.values() if "score" in item]
    scores = [item["score"] for item in scored_items]
    flagged_items = [item for item in scored_items if item["score"] <= 7]
    count = len(scored_items)

    report_payload = {
        "model": model,
        "total_dict_entries": total_dict_entries,
        "total_book_hover_positions": total_hover_positions,
        "total_entries_scored": count,
        "summary": {
            "avg_score": round(sum(scores) / count, 2) if count else 0,
            "pinyin_accuracy_rate": (
                round(sum(1 for item in scored_items if item.get("pinyin_ok")) / count * 100, 1)
                if count
                else 0
            ),
            "defs_quality_rate": (
                round(sum(1 for item in scored_items if item.get("defs_ok")) / count * 100, 1)
                if count
                else 0
            ),
            "context_match_rate": (
                round(sum(1 for item in scored_items if item.get("context_ok")) / count * 100, 1)
                if count
                else 0
            ),
            "score_distribution": {str(s): scores.count(s) for s in range(1, 11)},
            "flagged_count": len(flagged_items),
        },
        "flagged_entries": sorted(
            flagged_items, key=lambda item: (item["score"], -item.get("hover_count", 0))
        ),
        "results": sorted(
            scored_items, key=lambda item: (-item.get("hover_count", 0), item["word"])
        ),
    }

    temp_path = Path(str(report_path) + ".tmp")
    temp_path.write_text(
        json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(report_path)


def print_dictionary_qa_summary(report_path: Path) -> None:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    summary = report["summary"]
    print(
        f"\n{'='*65}\n"
        f"DICTIONARY QA SUMMARY ({report['total_entries_scored']} entries verified)\n"
        f"{'='*65}\n"
        f"  Average Score        : {summary['avg_score']}/10\n"
        f"  Pinyin Accuracy Rate : {summary['pinyin_accuracy_rate']}%\n"
        f"  Defs Quality Rate    : {summary['defs_quality_rate']}%\n"
        f"  Context Match Rate   : {summary['context_match_rate']}%\n"
        f"  Flagged (<=7/10)     : {summary['flagged_count']}\n"
        f"  Report saved to      : {report_path}\n"
    )


def apply_qwen_fixes_to_dictionary(
    report_path: Path, dict_path: Path, max_score_to_patch: int = 8
) -> list[str]:
    if not report_path.is_file():
        sys.exit(f"Report not found: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    dict_payload = json.loads(dict_path.read_text(encoding="utf-8"))
    entries = dict_payload.get("entries", {})

    updated_words: list[str] = []
    for item in report.get("results", []):
        word = item.get("word")
        if not word or word not in entries:
            continue
        if item.get("score", 10) > max_score_to_patch:
            continue

        entry_row = entries[word]
        was_modified = False
        suggested_pinyin = (item.get("suggested_pinyin") or "").strip()
        suggested_defs = item.get("suggested_defs") or []

        if suggested_pinyin and suggested_pinyin != entry_row[0]:
            entry_row[0] = suggested_pinyin
            was_modified = True
        if suggested_defs and suggested_defs != entry_row[1]:
            entry_row[1] = suggested_defs
            was_modified = True

        if was_modified:
            entries[word] = entry_row
            updated_words.append(word)

    backup_path = Path(str(dict_path) + ".bak")
    if not backup_path.exists():
        shutil.copy2(dict_path, backup_path)
    dict_path.write_text(
        json.dumps(dict_payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(
        f"Applied {len(updated_words)} Qwen-verified fixes to {dict_path} (backup at {backup_path})."
    )
    return updated_words


def run_dictionary_qa_batches(
    batches: list[list[dict]],
    existing_results: dict[str, dict],
    report_path: Path,
    model: str,
    workers: int,
    total_dict_entries: int,
    total_hover_positions: int,
) -> None:
    lock = threading.Lock()
    stop_event = threading.Event()
    completed_batches = 0

    def process_batch(batch: list[dict]) -> list[dict]:
        if stop_event.is_set():
            return []
        return evaluate_dictionary_batch_with_qwen(batch, model=model)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_batch = {
            executor.submit(process_batch, batch): batch for batch in batches
        }
        for future in as_completed(future_to_batch):
            batch = future_to_batch[future]
            try:
                batch_results = future.result()
            except InsufficientBalanceError as err:
                stop_event.set()
                print(
                    f"\n[Insufficient balance: {err}. Saving progress...]",
                    file=sys.stderr,
                )
                continue
            except Exception as err:
                sample_words = [item["word"] for item in batch[:3]]
                print(f"  [Batch error ({sample_words}): {err}]", file=sys.stderr)
                continue

            if not batch_results:
                continue

            with lock:
                completed_batches += 1
                for item in batch_results:
                    existing_results[item["word"]] = item
                save_dictionary_qa_report(
                    existing_results,
                    report_path,
                    model,
                    total_dict_entries,
                    total_hover_positions,
                )
                batch_avg = sum(item["score"] for item in batch_results) / len(
                    batch_results
                )
                flagged_labels = [
                    f"{item['word']}({item['score']})"
                    for item in batch_results
                    if item["score"] <= 7
                ]
                print(
                    f"[{completed_batches}/{len(batches)}] scored {len(batch_results)} entries "
                    f"(batch avg {batch_avg:.1f}/10) | flagged: {', '.join(flagged_labels) or 'none'}"
                )


def parse_cli_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify popup dictionary quality using ModelScope Qwen"
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=120,
        help="Number of dictionary entries to verify (default: 120, 0 = all)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Verify all entries in i18n/dict.json",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=8,
        help="Entries per Qwen API request (default: 8)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="Concurrent API workers (default: 2)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from existing dictionary_qa_report.json",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply Qwen's suggested fixes to i18n/dict.json",
    )
    parser.add_argument(
        "--max-score-to-patch",
        type=int,
        default=8,
        help="Patch entries with score <= this threshold when --apply is set (default: 8)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_DICT_QA_REPORT_PATH,
        help="Output report JSON path",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help=f"ModelScope model (default: {MODELSCOPE_MODEL})",
    )
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


def main() -> None:
    args = parse_cli_arguments()

    if args.apply:
        apply_qwen_fixes_to_dictionary(
            args.output, DICT_JSON_PATH, max_score_to_patch=args.max_score_to_patch
        )
        return

    random.seed(args.seed)
    if not HTML_PATH.is_file():
        sys.exit(f"ERROR: {HTML_PATH} not found.")
    if not DICT_JSON_PATH.is_file():
        sys.exit(f"ERROR: {DICT_JSON_PATH} not found.")

    dict_payload = json.loads(DICT_JSON_PATH.read_text(encoding="utf-8"))
    entries = dict_payload.get("entries", {})
    max_word_length = dict_payload.get("maxLen", 8)

    print(f"Simulating mouse hover (`findLongestMatchingWordAt`) across {HTML_PATH.name}...")
    hover_counts = simulate_book_hover_frequencies(HTML_PATH, entries, max_word_length)
    total_hover_positions = sum(hover_counts.values())
    print(
        f"Simulated {total_hover_positions:,} character hover lookups -> "
        f"{len(hover_counts):,} distinct popup words triggered."
    )

    existing_results = (
        load_existing_dictionary_qa_results(args.output) if args.resume else {}
    )
    if existing_results:
        print(f"Resuming: {len(existing_results)} entries already scored.")

    selected_candidates = select_candidate_entries_for_qa(
        entries, hover_counts, args.sample, args.all
    )
    pending_candidates = [
        item for item in selected_candidates if item["word"] not in existing_results
    ]
    model = args.model or MODELSCOPE_MODEL
    batches = [
        pending_candidates[i : i + args.batch_size]
        for i in range(0, len(pending_candidates), args.batch_size)
    ]

    print(
        f"\n{'='*65}\n"
        f"Popup Dictionary QA | {len(pending_candidates)} entries in {len(batches)} batches | model: {model}\n"
        f"{'='*65}"
    )

    run_dictionary_qa_batches(
        batches=batches,
        existing_results=existing_results,
        report_path=args.output,
        model=model,
        workers=args.workers,
        total_dict_entries=len(entries),
        total_hover_positions=total_hover_positions,
    )

    save_dictionary_qa_report(
        existing_results, args.output, model, len(entries), total_hover_positions
    )
    print_dictionary_qa_summary(args.output)


if __name__ == "__main__":
    main()
