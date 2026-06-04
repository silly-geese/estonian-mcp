"""Benchmark estonian-mcp's morphology engine against TalTechNLP's
`inflection_et` dataset — the noun-phrase inflection task from EKI's
Keelemudelite mõõdupuu (https://moodupuu.eki.ee/).

estonian-mcp is a tool server, not a language model, so it can't be
"ranked on the leaderboard". Instead this scores our morphological
synthesis — the Vabamorf engine exposed via the `paradigm` tool —
directly against the benchmark's gold data: given a base noun phrase
+ target number + case, can we produce the correct inflected form?

The phrase is inflected word-by-word with Vabamorf.synthesize (the same
call `paradigm` uses) and the words are rejoined, which also tests
adjective–noun agreement.

Two scores are reported:
  - any-candidate : the engine produced a correct form among its
    candidates (covers the gold variants Vabamorf can generate)
  - first-candidate : the engine's single top output is correct
    (the stricter "what a user actually gets" number)

Run from repo root:
  uv run python scripts/eval_inflection.py

Requires the `datasets` library (dev-only):  uv pip install datasets
"""

from __future__ import annotations

import itertools
import sys
from collections import defaultdict

from datasets import load_dataset
from estnltk.vabamorf.morf import Vabamorf

# Reuse the MCP's own indeclinability knowledge so the score reflects a
# real capability the server has, not an eval-only special case.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from server import _is_indeclinable_attr  # noqa: E402

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


def synth(vm, word: str, form: str) -> list[str]:
    try:
        return vm.synthesize(word, form) or []
    except Exception:
        return []


def main() -> None:
    vm = Vabamorf.instance()
    ds = load_dataset("TalTechNLP/inflection_et", split="train")

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

        # Candidate surfaces per word, across all acceptable form codes.
        # Indeclinable attributives (täis, -tud/-nud participles) keep
        # their base form — the MCP knows this via _is_indeclinable_attr.
        per_word: list[set[str]] = []
        for w in words:
            if _is_indeclinable_attr(w):
                per_word.append({w})
                continue
            cands: set[str] = set()
            for form in forms:
                cands.update(synth(vm, w, form))
            per_word.append(cands or {w})

        predicted = {" ".join(combo) for combo in itertools.product(*per_word)}
        if predicted & gold:
            any_ok += 1
            by_key_any[key] += 1
        else:
            if len(misses) < 15:
                misses.append((phrase, row["plurality"], row["case"],
                               sorted(gold)[:2], sorted(predicted)[:2]))

        # First-candidate: top synth of the first (primary) form per word
        # (indeclinables kept in base form).
        first = " ".join(
            w if _is_indeclinable_attr(w) else (synth(vm, w, forms[0]) or [w])[0]
            for w in words
        )
        if first in gold:
            first_ok += 1

    print(f"\n=== estonian-mcp vs inflection_et ({n} items) ===")
    print(f"any-candidate accuracy : {any_ok}/{n} = {100*any_ok/n:.1f}%")
    print(f"first-candidate accuracy: {first_ok}/{n} = {100*first_ok/n:.1f}%")
    print("\nper (number, case) — any-candidate:")
    for key in sorted(by_key_total):
        t, a = by_key_total[key], by_key_any[key]
        print(f"  {key[0]:8} {key[1]:12} {a:4}/{t:<4} = {100*a/t:5.1f}%")

    print("\nsample misses (phrase | num | case | gold | predicted):")
    for m in misses[:10]:
        print(f"  {m[0]!r} | {m[1]} {m[2]} | gold={m[3]} | pred={m[4]}")


if __name__ == "__main__":
    sys.exit(main())
