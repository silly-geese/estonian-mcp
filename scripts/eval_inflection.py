"""Benchmark estonian-mcp's morphology engine against TalTechNLP's
`inflection_et` dataset — a noun-phrase inflection benchmark
(Lillepalu & Alumäe, https://arxiv.org/abs/2510.21193v2).

estonian-mcp is a tool server, not a language model, so it can't be
"ranked on the leaderboard". Instead this scores our morphological
synthesis, the Vabamorf engine exposed via the `paradigm` tool,
directly against the benchmark's gold data: given a base noun phrase
+ target number + case, can we produce the correct inflected form?

WHAT IS SERVER CODE AND WHAT IS HARNESS
---------------------------------------
Server code, imported not copied, so the published number measures the
product: `_paradigm_hints` (which inflection type a lemma belongs to),
`_synthesize` (POS-constrained synthesis with a fallback) and
`_is_indeclinable_attr` (which attributes stay in base form).

Harness only: splitting the phrase, and rejoining the inflected words.
There is no phrase-level tool, so agreement across a noun phrase is the
harness's own loop. Read the score as "the engine behind `paradigm`",
not as "a tool that inflects phrases".

TWO ACCURACY SCORES
-------------------
  - any-candidate : a correct form is among the candidates the engine
    produced (covers the gold variants Vabamorf can generate)
  - first-candidate : the engine's single top output is correct
    (the stricter "what a user actually gets" number)

THE EKI ADJUDICATION
--------------------
13 of the 1400 gold rows contradict EKI's rules, in both directions: 12
decline a `-tud` participle that is invariant as a pre-modifier
(`läbimõeldu plaani` for `läbimõeldud plaani`), and 1 leaves a `-v`
participle undeclined where it must agree (`rahuldav tulemuse` for
`rahuldava tulemuse`). The dataset appears to have been generated
word-by-word with a morphological synthesizer, which is exactly the
operation that has no way to know either rule.

`data/inflection_et_eki_disputes.json` records all 13 with the EKI form
and the rule cited. This script reports the raw score against gold AND
an EKI-adjudicated score in which a disputed row counts as correct when
our output matches the EKI form.

The adjudication self-invalidates. Every dispute is re-checked against
the live dataset before it is honoured: if the gold no longer matches
what we recorded, because the dataset has been corrected upstream,
that dispute is reported as stale and awarded nothing. So the adjudicated
number cannot silently keep claiming credit for a fixed row.

Run from repo root:
  uv run python scripts/eval_inflection.py
  uv run python scripts/eval_inflection.py --report-disputes   # markdown

Requires the `datasets` library (dev-only):  uv pip install datasets
"""

from __future__ import annotations

import itertools
import json
import sys
from collections import defaultdict
from pathlib import Path

from datasets import load_dataset

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from server import (
    _is_indeclinable_attr,
    _paradigm_hints,
    _synthesize,
    _vabamorf,
)

DISPUTES_PATH = _ROOT / "data" / "inflection_et_eki_disputes.json"

# Estonian number / case names (as used in inflection_et) → Vabamorf
# form codes. The illative maps to BOTH the long (ill) and the short /
# aditiiv (adt) form, since the dataset accepts both as gold.
_NUM = {"ainsuse": "sg", "mitmuse": "pl"}
_CASE = {
    "nimetav": ["n"],      # nominative
    "omastav": ["g"],      # genitive
    "osastav": ["p"],      # partitive
    "sisseütlev": ["ill", "adt"],  # illative (long + short)
}
_KEY_FORM = "sg g"   # the form Estonian reads the inflection type off


def load_disputes() -> dict:
    return json.loads(DISPUTES_PATH.read_text(encoding="utf-8"))


def _pos_of(word: str) -> str:
    """A POS constraint for this word that Vabamorf can actually synthesize
    under, or "" for no constraint.

    Only the POS. The dataset supplies base forms, so the word IS its own
    lemma here and re-lemmatising it can only lose: `ergas` (adjective,
    `ergas : erksa`) comes back as the past tense of a verb `ergama`, and a
    verb lemma has no singular genitive at all.

    THE CONSTRAINT IS DROPPED WHEN IT CANNOT PRODUCE THE KEY FORM. That is
    a harness rule, not a server one, and it is sound only because of a
    fact about this dataset: every input is a nominal base form. So a POS
    that yields no singular genitive is a misanalysis of a known nominal
    (`ergas` again, tagged V), not a word that genuinely lacks one.
    `_synthesize` refuses to make that inference for the server, and it is
    right to: there `iga` really is a pronoun with no plural, and relaxing
    the POS would splice the noun `iga` "age" into its table.
    """
    try:
        a = _vabamorf().analyze([word], disambiguate=True)[0].get("analysis") or []
    except Exception:
        a = []
    pos = a[0]["partofspeech"] if a else ""
    if pos and not _synthesize(word, _KEY_FORM, pos):
        return ""
    return pos


def word_surfaces(word: str, form_codes: list[str]) -> tuple[str, set[str]]:
    """(top surface, every candidate surface) for one word.

    An indeclinable attribute keeps its base form. Otherwise the word's
    inflection type is resolved once, and the top surface comes from that
    type, which is what stops `kott` yielding `kota` (a different word
    that happens to share a nominative) as its first genitive.

    The candidate set still spans EVERY type, so `any-candidate` coverage
    does not depend on the ranking being right, or on the corpus data that
    ranks it being installed.
    """
    if _is_indeclinable_attr(word):
        return (word, {word})
    lemma, pos = word, _pos_of(word)
    hints, _ranked_by_corpus = _paradigm_hints(lemma, pos, _KEY_FORM)

    primary: list[str] = []
    every: set[str] = set()
    for hint in hints:
        for form in form_codes:
            got = _synthesize(lemma, form, pos, hint)
            every.update(got)
            if hint == hints[0] and form == form_codes[0]:
                primary = got
    return (primary[0] if primary else word, every or {word})


def main() -> None:
    argv = sys.argv[1:]
    doc = load_disputes()
    if "--report-disputes" in argv:
        print(render_disputes(doc))
        return

    ds = load_dataset("TalTechNLP/inflection_et", split="train")

    # Disputes, keyed for lookup. A dispute is honoured only if the gold
    # in the live dataset still matches what we recorded against.
    disputes = {
        (d["noun_phrase"], d["plurality"], d["case"]): d for d in doc["disputes"]
    }
    stale: list[tuple] = []
    seen_disputes: set[tuple] = set()
    adjudicated_any = 0
    adjudicated_first = 0

    n = 0
    any_ok = 0
    first_ok = 0
    by_key_total: dict[tuple, int] = defaultdict(int)
    by_key_any: dict[tuple, int] = defaultdict(int)
    misses: list[tuple] = []

    for row in ds:
        phrase = row["noun_phrase"]
        gold = set(row["inflection"])
        num = _NUM[row["plurality"]]
        forms = [f"{num} {c}" for c in _CASE[row["case"]]]
        words = phrase.split()
        key = (row["plurality"], row["case"])
        n += 1
        by_key_total[key] += 1

        tops: list[str] = []
        per_word: list[set[str]] = []
        for w in words:
            top, every = word_surfaces(w, forms)
            tops.append(top)
            per_word.append(every)

        predicted = {" ".join(combo) for combo in itertools.product(*per_word)}
        first = " ".join(tops)

        hit_any = bool(predicted & gold)
        hit_first = first in gold
        if hit_any:
            any_ok += 1
            by_key_any[key] += 1
        elif len(misses) < 15:
            misses.append((phrase, row["plurality"], row["case"],
                           sorted(gold)[:2], sorted(predicted)[:2]))
        if hit_first:
            first_ok += 1

        dkey = (phrase, row["plurality"], row["case"])
        d = disputes.get(dkey)
        if d is None:
            continue
        seen_disputes.add(dkey)
        if sorted(d["dataset_gold"]) != sorted(gold):
            stale.append((phrase, row["plurality"], row["case"],
                          d["dataset_gold"], sorted(gold)))
            continue
        eki = set(d["eki_forms"])
        if not hit_any and (predicted & eki):
            adjudicated_any += 1
        if not hit_first and first in eki:
            adjudicated_first += 1

    print(f"\n=== estonian-mcp vs inflection_et ({n} items) ===")
    print("raw, scored against the dataset's gold:")
    print(f"  any-candidate accuracy  : {any_ok}/{n} = {100*any_ok/n:.1f}%")
    print(f"  first-candidate accuracy: {first_ok}/{n} = {100*first_ok/n:.1f}%")

    # A dispute whose row is GONE from the dataset never came up in the
    # loop, so it can't have been compared. Without this it would sit in
    # the file for ever, uncounted and unreported, while the header kept
    # announcing 13 disputed rows.
    for key in disputes:
        if key not in seen_disputes:
            stale.append((key[0], key[1], key[2], disputes[key]["dataset_gold"],
                          ["(row no longer present in the dataset)"]))

    live = len(doc["disputes"]) - len(stale)
    adj_any = any_ok + adjudicated_any
    adj_first = first_ok + adjudicated_first
    print(f"\nEKI-adjudicated ({live} of {len(doc['disputes'])} recorded disputes "
          f"still match the dataset; {adjudicated_any} any / {adjudicated_first} "
          f"first awarded here):")
    print(f"  any-candidate accuracy  : {adj_any}/{n} = {100*adj_any/n:.1f}%")
    print(f"  first-candidate accuracy: {adj_first}/{n} = {100*adj_first/n:.1f}%")

    if stale:
        print(f"\n!! {len(stale)} recorded dispute(s) no longer match the dataset.")
        print("   The data has changed upstream. Re-audit and update")
        print(f"   {DISPUTES_PATH.relative_to(_ROOT)}. No credit was awarded for these.")
        for s in stale:
            print(f"   - {s[0]!r} {s[1]} {s[2]}: recorded {s[3]}, dataset now {s[4]}")

    print("\nper (number, case), any-candidate (raw):")
    for key in sorted(by_key_total):
        t, a = by_key_total[key], by_key_any[key]
        print(f"  {key[0]:8} {key[1]:12} {a:4}/{t:<4} = {100*a/t:5.1f}%")

    print("\nsample misses (phrase | num | case | gold | predicted):")
    for m in misses[:10]:
        print(f"  {m[0]!r} | {m[1]} {m[2]} | gold={m[3]} | pred={m[4]}")
    if not misses:
        print("  (none)")


def render_disputes(doc: dict) -> str:
    """Markdown table of the disputed gold rows, for reporting upstream."""
    lines = [
        f"### {len(doc['disputes'])} rows in `{doc['dataset']}` that contradict EKI",
        "",
        f"Dataset revision `{doc['dataset_revision']}`, {doc['dataset_rows']} rows.",
        "",
        "| # | noun phrase | number | case | dataset gold | EKI form | rule |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, d in enumerate(doc["disputes"], 1):
        lines.append(
            f"| {i} | {d['noun_phrase']} | {d['plurality']} | {d['case']} | "
            f"{' / '.join(d['dataset_gold'])} | {' / '.join(d['eki_forms'])} | "
            f"`{d['rule']}` |"
        )
    lines.append("")
    for name, r in doc["rules"].items():
        lines.append(f"**`{name}`**: {r['rule_estonian']} ({r['rule_english']}) "
                     f"Näide: {r['example']}")
        lines.append("")
    lines.append("Sources:")
    for s in doc["sources"]:
        lines.append(f"- {s}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
