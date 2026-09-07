"""Benchmark the morphology engine against Pert Lomp's käänamiskorpus.

A second, independent inflection benchmark, and a wider one than
`inflection_et`: 11,011 single-word rows covering ALL FOURTEEN cases in
both numbers, drawn from real corpus text (Riigikogu stenographs and ERR
video news) and frequency-weighted, where `inflection_et` is 1,400
synthesised noun phrases across four cases.

Source and credit
-----------------
Pert Lomp (github.com/pertlomp/qwen38-et, huggingface.co/pertai), who
built it while training an open Estonian model and wrote to us about
what he had measured on parallel forms. The same letter is what turned
up the missing short illative fixed in 0.5.8.

The DATA is CC-BY-SA-4.0 per row, conservatively marked because part of
it derives from ERR. This script DOWNLOADS it at run time and never
vendors it into this repository, so nothing here redistributes it and
the server never sees it. Each row carries its own `allikas` and
`litsents`, and the report prints both.

WHY A SECOND BENCHMARK
----------------------
`inflection_et` scores 99.1% here, which says less than it looks like:
it covers nimetav, omastav, osastav and sisseütlev only. Ten cases were
never measured at all, including the ones an Estonian writer gets wrong
most often. A benchmark that agrees with itself is not evidence.

Reads the same engine `paradigm` exposes, through the same helpers the
`inflection_et` harness uses, so the two numbers are comparable.

Usage:

    uv run python scripts/eval_kaanamiskorpus.py
    uv run python scripts/eval_kaanamiskorpus.py --limit 500

The download uses urllib from the standard library, so this needs no
dependency beyond the server's own.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from eval_inflection import _KEY_FORM, _NUM, _pos_of

from server import _paradigm_hints, _rank_surfaces, _synthesize

# Pinned to a commit, not a branch. A moving `main` would mean the score
# printed here cannot be reproduced, which is the failure mode this
# repository already documents for its other benchmark: a number nobody
# can re-derive is a claim, not a measurement.
CORPUS_COMMIT = "c36540636a33a6d25a5657e06f9bab5573ac3597"
CORPUS_URL = (
    "https://raw.githubusercontent.com/pertlomp/qwen38-et/{ref}/"
    "datasets/kaanamiskorpus-avalik.jsonl"
)
EXPECTED_ROWS = 11011
CACHE = Path.home() / ".cache" / "estnltk-mcp" / "kaanamiskorpus-avalik.jsonl"

# Estonian case name -> Vabamorf form code. The names are the corpus's
# own; the codes are the ones `paradigm` generates under.
_CASE_CODE = {
    "nimetav": "n",
    "omastav": "g",
    "osastav": "p",
    "sisseütlev": "ill",
    "seesütlev": "in",
    "seestütlev": "el",
    "alaleütlev": "all",
    "alalütlev": "ad",
    "alaltütlev": "abl",
    "saav": "tr",
    "rajav": "ter",
    "olev": "es",
    "ilmaütlev": "ab",
    "kaasaütlev": "kom",
}

# The short illative has no number prefix and only exists in the
# singular, so it is appended rather than built. Same rule, and the same
# trap, as in eval_inflection.forms_for().
_SHORT_ILLATIVE_FORM = "adt"


def forms_for(case: str, num: str) -> list[str]:
    """Vabamorf form codes for one row. The numbered code leads, because
    the caller takes the first as its single answer."""
    codes = [f"{num} {_CASE_CODE[case]}"]
    if case == "sisseütlev" and num == "sg":
        codes.append(_SHORT_ILLATIVE_FORM)
    return codes


def surfaces_for_word(word: str, form_codes: list[str]) -> tuple[str, set[str]]:
    """(top surface, every candidate) for one standalone word.

    Deliberately NOT eval_inflection.word_surfaces, which first asks
    `_is_indeclinable_attr`. That rule is about a participle used as a
    PRE-MODIFIER inside a phrase (`läbimõeldud plaani`), and this corpus
    is single words: routing `valitud` through it returns `valitud` for
    the singular translative and scores a miss, while the tool itself
    answers the gold `valituks`. Measuring a rule the caller is not
    subject to would understate the engine on 61 rows.
    """
    lemma, pos = word, _pos_of(word)
    hints, _ = _paradigm_hints(lemma, pos, _KEY_FORM)
    primary: list[str] = []
    every: set[str] = set()
    for hint in hints:
        stem = (_synthesize(lemma, f"{form_codes[0][:2]} g", pos, hint) or [""])[0]
        for form in form_codes:
            got = _rank_surfaces(_synthesize(lemma, form, pos, hint), stem)
            every.update(got)
            if hint == hints[0] and form == form_codes[0]:
                primary = got
    return (primary[0] if primary else word, every or {word})


def load_corpus() -> list[dict]:
    """The corpus, cached locally after the first fetch.

    Written through a temporary file: an interrupted download must not
    leave a half corpus in the cache that every later run then scores
    against without noticing.
    """
    if not CACHE.exists():
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        url = CORPUS_URL.format(ref=CORPUS_COMMIT)
        print(f"downloading {url}")
        tmp = CACHE.with_suffix(".part")
        with urllib.request.urlopen(url, timeout=60) as r:
            tmp.write_bytes(r.read())
        tmp.replace(CACHE)
    rows = [json.loads(line) for line in CACHE.read_text(encoding="utf-8").splitlines() if line]
    if len(rows) != EXPECTED_ROWS:
        print(f"!! expected {EXPECTED_ROWS} rows, read {len(rows)}. The corpus has changed "
              f"upstream, so the published score no longer describes this data.")
        print(f"   Delete {CACHE} to refetch, and re-measure before quoting a number.")
    return rows


def main() -> None:
    limit = 0
    if "--limit" in sys.argv:
        i = sys.argv.index("--limit") + 1
        if i >= len(sys.argv):
            sys.exit("--limit needs a number")
        limit = int(sys.argv[i])

    rows = load_corpus()
    if limit and limit < len(rows):
        # Evenly spaced, not the head. The file is ordered, so the first
        # 800 rows over-weight omastav and under-weight alaleütlev: they
        # score 94.5% where the whole corpus scores 99.1%.
        step = len(rows) / limit
        rows = [rows[int(i * step)] for i in range(limit)]

    sources = {r.get("allikas") for r in rows}
    licences = {r.get("litsents") for r in rows}

    n = any_ok = first_ok = 0
    by_case_total: dict[tuple, int] = defaultdict(int)
    by_case_any: dict[tuple, int] = defaultdict(int)
    by_case_first: dict[tuple, int] = defaultdict(int)
    variant_only: list[tuple] = []      # gold reachable, but not our top pick
    misses: list[tuple] = []            # gold not generated at all
    unknown_case: set[str] = set()

    for row in rows:
        case, num_et = row["kaane"], row["arv"]
        if case not in _CASE_CODE or num_et not in _NUM:
            unknown_case.add(f"{num_et} {case}")
            continue
        num = _NUM[num_et]
        gold = row["vorm"]
        key = (num_et, case)
        n += 1
        by_case_total[key] += 1

        top, every = surfaces_for_word(row["sisend"], forms_for(case, num))
        if gold in every:
            any_ok += 1
            by_case_any[key] += 1
            if gold == top:
                first_ok += 1
                by_case_first[key] += 1
            elif len(variant_only) < 12:
                variant_only.append((row["sisend"], num_et, case, gold, top))
        elif len(misses) < 15:
            misses.append((row["sisend"], num_et, case, gold, sorted(every)[:2]))

    print(f"\n=== estonian-mcp vs käänamiskorpus ({n} rows) ===")
    print(f"source   : {', '.join(sorted(s for s in sources if s))}")
    print(f"licence  : {', '.join(sorted(x for x in licences if x))}")
    print("credit   : Pert Lomp, github.com/pertlomp/qwen38-et")
    print(f"\n  any-candidate accuracy  : {any_ok}/{n} = {100 * any_ok / n:.1f}%")
    print(f"  first-candidate accuracy: {first_ok}/{n} = {100 * first_ok / n:.1f}%")
    print(f"\n  reachable but not our first pick: {any_ok - first_ok} "
          f"({100 * (any_ok - first_ok) / n:.1f}%)")
    print(f"  not generated at all            : {n - any_ok} "
          f"({100 * (n - any_ok) / n:.1f}%)")

    print("\nper (number, case), any / first:")
    for key in sorted(by_case_total, key=lambda k: -by_case_total[k]):
        tot = by_case_total[key]
        print(f"  {key[0]:9} {key[1]:14} {by_case_any[key]:5}/{tot:<5} = "
              f"{100 * by_case_any[key] / tot:5.1f}%   "
              f"first {100 * by_case_first[key] / tot:5.1f}%")

    if variant_only:
        print("\nthe engine reaches the gold form but ranks another first")
        print("(this is the parallel-form gap Pert Lomp measured on models):")
        for word, num_et, case, gold, top in variant_only:
            print(f"  {word:16} {num_et} {case:13} gold={gold:18} ours={top}")

    if misses:
        print("\nnot generated at all (sample):")
        for word, num_et, case, gold, got in misses:
            print(f"  {word:16} {num_et} {case:13} gold={gold:18} ours={got}")

    if unknown_case:
        print(f"\n!! rows skipped, unmapped number or case: {sorted(unknown_case)}")
        print("   Those rows scored nothing either way. Map them or say so.")


if __name__ == "__main__":
    main()
