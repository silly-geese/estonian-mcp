"""Coverage probes for two TalTechNLP benchmark datasets that do NOT map
to a clean accuracy score (unlike inflection_et).

- grammar_et is full-sentence CORRECTION (original -> correct), mixing
  compound, case, word-choice and spelling fixes. Our tools DETECT
  errors, they don't emit corrected sentences. So we report detection
  recall: on what fraction of erroneous sentences does at least one
  check_* / spell_check tool flag something? (Detection, not a fix —
  and bounded by our lexicons, so read it as a floor.)

- word_meanings_et is word -> free-text DEFINITION. Our `synonyms` tool
  returns WordNet synsets + definitions; matching free text is fuzzy,
  so we report vocabulary coverage: for what fraction of target words
  does WordNet have an entry at all?

Both are honest "how much of this can our tools even touch" numbers,
not capability scores. Run from repo root (sample size as argv[1]):

  uv run python scripts/eval_coverage.py 500
"""

from __future__ import annotations

import sys
from datasets import load_dataset

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import server  # noqa: E402


def grammar_detection_recall(n: int) -> None:
    ds = load_dataset("TalTechNLP/grammar_et", split="train").select(range(n))
    detected = 0
    for row in ds:
        text = row["original"]
        hit = False
        try:
            if any(not w["spelling"] for w in server.spell_check(text)):
                hit = True
            if not hit and server.check_compounds(text)["issues"]:
                hit = True
            if not hit and server.check_capitalization(text)["issues"]:
                hit = True
            if not hit and server.check_object_case(text)["issues"]:
                hit = True
            if not hit and server.check_punctuation(text)["issues"]:
                hit = True
            if not hit and server.check_abbreviation_hyphenation(text)["issues"]:
                hit = True
            if not hit and server.check_redundancy(text)["issues"]:
                hit = True
        except Exception:
            pass
        detected += int(hit)
    print(f"grammar_et detection recall: {detected}/{n} = {100*detected/n:.1f}%")
    print("  (= at least one check_* tool flagged an error in the "
          "erroneous sentence; detection, not correction; floor-bounded "
          "by our lexicons)")


def wordnet_vocab_coverage(n: int) -> None:
    ds = load_dataset("TalTechNLP/word_meanings_et", split="train").select(range(n))
    found = 0
    for row in ds:
        word = row["words"][0] if row.get("words") else ""
        try:
            if word and server.synonyms(word)["synset_count"] > 0:
                found += 1
        except Exception:
            pass
    print(f"word_meanings_et WordNet coverage: {found}/{n} = {100*found/n:.1f}%")
    print("  (= target word has an Estonian WordNet entry via `synonyms`; "
          "vocabulary coverage, not definition-match accuracy)")


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    print(f"=== coverage probes (sample n={n} per dataset) ===")
    grammar_detection_recall(n)
    wordnet_vocab_coverage(n)


if __name__ == "__main__":
    sys.exit(main())
