"""Tests for `paradigm` and the synthesis behind it.

These pin the four defects the inflection_et audit turned up, plus the two
EKI rules that the 13 disputed gold rows in that dataset get wrong.

1. WHOLE WORD CLASSES WERE DENIED A PARADIGM. `_NOMINAL_POS` listed only
   S/A/P/N, so every ordinal (`esimene`), comparative (`parem`) and
   superlative (`parim`) got "Sõnaliik 'O' ei käändu ega pöördu —
   paradigmat pole." Those are 1.5% of the 20k most frequent Estonian word
   forms, and Vabamorf synthesizes all of them correctly under their own
   POS code.

2. A NON-INFLECTING READING OF AN INFLECTING WORD. `kaunis` alone is
   disambiguated D (`kaunis hea` = "quite good"), so the tool reported that
   `kaunis : kauni : kaunist` has no paradigm. The rescue is deliberately
   narrow: it must be the word's OWN lemma and an A/C/U/O reading,
   because `veel` also carries a noun reading whose lemma is `vesi`, and
   answering "still" with the paradigm of "water" would be worse than the
   bug.

3. TWO PARADIGMS MERGED INTO ONE TABLE. `kott` inflects as either `koti`
   or `kota`, two different words sharing a nominative. The table used to
   carry `sg g: [kota, koti]`, `sg p: [kotta, kotti]` with nothing saying
   they belong to different words, and Vabamorf's order put the rare one
   first. Now each paradigm is generated separately under its own hint,
   ranked by corpus attestation, and the rest are in `other_paradigms`.

4. THE CALLER'S OWN EVIDENCE WAS THROWN AWAY. `paradigm("koti")` returned
   exactly what `paradigm("kott")` did, although the input said which word
   was meant. The old `note` even advised the opposite of what helps.

Run via:

    uv run python tests/test_paradigm.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import server  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS {label}")
    else:
        failures.append(f"{label}: {detail}")
        print(f"  FAIL {label} {detail}")


def surfaces(entry: dict) -> list[str]:
    s = entry["surface"]
    return [s] if isinstance(s, str) else list(s)


def form_of(result: dict, code: str) -> list[str]:
    for e in result.get("forms", []):
        if e["form"] == code:
            return surfaces(e)
    return []


HAVE_CORPUS = server._corpus_ranks() is not None


# ---------------------------------------------------------------- defect 1

def ordinals_comparatives_superlatives_inflect() -> None:
    print("ordinals, comparatives and superlatives have a paradigm")
    # (word, expected POS, expected sg g, expected sg p)
    for word, pos, gen, part in (
        ("esimene", "O", "esimese", "esimest"),
        ("teine", "O", "teise", "teist"),
        ("kolmas", "O", "kolmanda", "kolmandat"),
        ("viies", "O", "viienda", "viiendat"),
        ("parem", "C", "parema", "paremat"),
        ("suurem", "C", "suurema", "suuremat"),
        ("parim", "U", "parima", "parimat"),
        ("suurim", "U", "suurima", "suurimat"),
    ):
        r = server._paradigm(word)
        check(f"{word} ({pos}) has 28 forms", len(r.get("forms", [])) == 28,
              f"got {len(r.get('forms', []))}: {r.get('summary_estonian')}")
        check(f"{word} sg g = {gen}", gen in form_of(r, "sg g"), str(form_of(r, "sg g")))
        check(f"{word} sg p = {part}", part in form_of(r, "sg p"), str(form_of(r, "sg p")))
        check(f"{word} POS reported as {pos}", r.get("partofspeech") == pos,
              str(r.get("partofspeech")))


def genuinely_uninflecting_words_still_say_so() -> None:
    """The fix must not start inventing paradigms for particles."""
    print("particles and conjunctions still report no paradigm")
    for word in ("ja", "ning", "väga", "et", "aga", "ega"):
        r = server._paradigm(word)
        check(f"{word} has no paradigm", r.get("forms") == [],
              f"got {len(r.get('forms', []))} forms, lemma={r.get('lemma')}")


# ---------------------------------------------------------------- defect 2

def inflecting_reading_is_found() -> None:
    print("an inflecting reading of the word's own lemma is used")
    r = server._paradigm("kaunis")
    check("kaunis has a paradigm", len(r.get("forms", [])) == 28,
          f"{r.get('summary_estonian')}")
    check("kaunis sg g = kauni", "kauni" in form_of(r, "sg g"), str(form_of(r, "sg g")))
    check("kaunis sg p = kaunist", "kaunist" in form_of(r, "sg p"),
          str(form_of(r, "sg p")))
    check("kaunis is reported as A", r.get("partofspeech") == "A",
          str(r.get("partofspeech")))
    check("the D reading is disclosed, not hidden",
          "D" in (r.get("reading_estonian") or ""), str(r.get("reading_estonian")))


def wrong_lemma_readings_are_not_rescued() -> None:
    """The guard that keeps this from being worse than the bug."""
    print("readings belonging to a DIFFERENT lemma are not rescued")
    # veel (adverb "still") also analyses as a form of vesi (water).
    r = server._paradigm("veel")
    check("veel is not given the paradigm of vesi",
          "vee" not in form_of(r, "sg g") and r.get("lemma") != "vesi",
          f"lemma={r.get('lemma')} sg g={form_of(r, 'sg g')}")
    # koos / enne / poole carry rare NOUN readings of their own lemma;
    # S is deliberately not promotable.
    for word in ("koos", "enne", "poole"):
        r = server._paradigm(word)
        check(f"{word} is not promoted to a noun paradigm",
              r.get("partofspeech") != "S" or r.get("forms") == [],
              f"pos={r.get('partofspeech')} forms={len(r.get('forms', []))}")


# ---------------------------------------------------------------- defect 3

def homonym_paradigms_are_separated() -> None:
    print("homonymous paradigms are separated, not merged")
    r = server._paradigm("kott")
    check("kott reports 2 paradigms", r.get("paradigm_count") == 2,
          str(r.get("paradigm_count")))
    check("the other paradigm is present",
          len(r.get("other_paradigms", [])) == 1, str(r.get("other_paradigms")))
    check("the ambiguity is stated in Estonian",
          "muuttüüpi" in (r.get("ambiguity_estonian") or ""),
          str(r.get("ambiguity_estonian")))

    keys = [r.get("paradigm_key")] + [o["paradigm_key"] for o in r["other_paradigms"]]
    check("both paradigms are named by their sg g", sorted(keys) == ["kota", "koti"],
          str(keys))

    # Each table must be internally consistent: no picking sg g from one
    # word and sg p from the other, which is what the merged table did.
    def table(entries):
        return {e["form"]: surfaces(e) for e in entries}

    tables = [table(r["forms"])] + [table(o["forms"]) for o in r["other_paradigms"]]
    for t in tables:
        stem = "koti" if "koti" in t["sg g"] else "kota"
        expect = {"koti": ("kotti", "kotid"), "kota": ("kotta", "kotad")}[stem]
        check(f"{stem} table is internally consistent",
              expect[0] in t["sg p"] and expect[1] in t["pl n"],
              f"sg p={t['sg p']} pl n={t['pl n']}")

    # And no form anywhere in a table may come from the other type. The
    # POS fallback inside _synthesize drops constraints when a form comes
    # up empty, and dropping the hint too would silently re-merge them.
    print("no form leaks between the tables")
    for word in ("kott", "pilk", "päike", "pööre", "väike"):
        r = server._paradigm(word)
        named = ([(r["paradigm_key"], r["forms"])]
                 + [(o["paradigm_key"], o["forms"]) for o in r["other_paradigms"]])
        for key, entries in named:
            foreign = [k for k, _ in named if k != key]
            leaked = [
                (e["form"], s)
                for e in entries for s in surfaces(e)
                for k in foreign
                if s == k or (s.startswith(k) and not s.startswith(key))
            ]
            check(f"{word}/{key}: no form from another type", not leaked, str(leaked[:3]))


def corpus_attestation_ranks_the_common_word_first() -> None:
    print("the paradigm Estonian actually uses is ranked first")
    if not HAVE_CORPUS:
        check("SKIPPED: fastText model not installed", True)
        return
    for word, expected in (("kott", "koti"), ("pilk", "pilgu")):
        r = server._paradigm(word)
        check(f"{word} leads with {expected}", r.get("paradigm_key") == expected,
              f"got {r.get('paradigm_key')}")
        check(f"{word} says the ranking used corpus data",
              r.get("ranked_by_corpus_frequency") is True,
              str(r.get("ranked_by_corpus_frequency")))


def degrades_without_the_corpus_model() -> None:
    """No corpus data must mean "unranked", never "fewer paradigms"."""
    print("without the fastText model: no crash, nothing lost, and it says so")
    original = server._corpus_ranks
    server._corpus_ranks = lambda: None
    try:
        r = server._paradigm("kott")
        check("still reports both paradigms", r.get("paradigm_count") == 2,
              str(r.get("paradigm_count")))
        check("says the ranking is not corpus-backed",
              r.get("ranked_by_corpus_frequency") is False,
              str(r.get("ranked_by_corpus_frequency")))
        check("the Estonian text says so too",
              "sagedusandmeid ei olnud" in (r.get("ambiguity_estonian") or ""),
              str(r.get("ambiguity_estonian")))
        # The caller's own evidence still works with no model at all.
        r = server._paradigm("koti")
        check("an inflected input still selects its paradigm",
              r.get("paradigm_key") == "koti", str(r.get("paradigm_key")))
    finally:
        server._corpus_ranks = original


# ---------------------------------------------------------------- defect 4

def an_inflected_input_selects_its_paradigm() -> None:
    print("the caller's inflected form picks the paradigm")
    for word, key, gen, part in (
        ("koti", "koti", "koti", "kotti"),
        ("kota", "kota", "kota", "kotta"),
        ("kotisse", "koti", "koti", "kotti"),
        ("pilgu", "pilgu", "pilgu", "pilku"),
    ):
        r = server._paradigm(word)
        check(f"{word} -> {key} paradigm", r.get("paradigm_key") == key,
              f"got {r.get('paradigm_key')}")
        check(f"{word} sg g = {gen}", form_of(r, "sg g") == [gen], str(form_of(r, "sg g")))
        check(f"{word} sg p = {part}", form_of(r, "sg p") == [part],
              str(form_of(r, "sg p")))
        check(f"{word}: the reason given is the input form",
              f"'{word}'" in (r.get("ambiguity_estonian") or ""),
              str(r.get("ambiguity_estonian")))


# ---------------------------------------------------- synthesis invariants

def synthesis_invariants() -> None:
    print("_synthesize: dedup, POS fallback, no dropped forms")
    check("duplicate lexicon entries collapse",
          server._synthesize("hall", "sg g", "A") == ["halli"],
          str(server._synthesize("hall", "sg g", "A")))
    check("a wrong POS falls back instead of returning nothing",
          server._synthesize("kaunis", "sg g", "D") == ["kauni"],
          str(server._synthesize("kaunis", "sg g", "D")))
    check("free variants inside ONE paradigm are kept",
          set(server._synthesize("kaunis", "pl g", "A")) == {"kaunite", "kauniste"},
          str(server._synthesize("kaunis", "pl g", "A")))
    check("unknown input returns a list, not an exception",
          isinstance(server._synthesize("qwertyxyz", "sg g", "S"), list))

    print("no form is silently dropped from a table")
    for word, n in (("kott", 28), ("maitse", 28), ("kaunis", 28), ("esimene", 28),
                    ("kasutama", 30)):
        r = server._paradigm(word)
        check(f"{word} has {n} forms", len(r.get("forms", [])) == n,
              f"got {len(r.get('forms', []))}")

    print("_paradigm_hints")
    check("an unambiguous lemma needs no hint",
          server._paradigm_hints("maitse", "S", "sg g") == ([""], False),
          str(server._paradigm_hints("maitse", "S", "sg g")))
    hints, _ = server._paradigm_hints("kott", "S", "sg g")
    check("an ambiguous lemma yields one hint per paradigm",
          sorted(hints) == ["kota", "koti"], str(hints))


def invariant_words_are_labelled() -> None:
    print("a word whose forms never change says so")
    for word in ("väärt", "eri"):
        r = server._paradigm(word)
        check(f"{word} is marked invariant", r.get("invariant") is True,
              f"forms={form_of(r, 'sg g')}")
    r = server._paradigm("kott")
    check("an ordinary word is not marked invariant", "invariant" not in r)


def junk_input_is_safe() -> None:
    print("junk input")
    for junk in ("", "   ", "123", "!!!", "qwertyuiopasdf", "x" * 199):
        try:
            server._paradigm(junk)
            check(f"{junk[:12]!r} handled", True)
        except ValueError:
            check(f"{junk[:12]!r} handled (rejected cleanly)", True)
        except Exception as e:
            check(f"{junk[:12]!r} handled", False, f"{type(e).__name__}: {e}")
    for bad in ("kaks sõna", "a b"):
        try:
            server._paradigm(bad)
            check(f"{bad!r} rejected", False, "multi-word input was accepted")
        except ValueError:
            check(f"{bad!r} rejected", True)


# -------------------------------------------------- the EKI disputed rules

DISPUTES = json.loads(
    (_ROOT / "data" / "inflection_et_eki_disputes.json").read_text(encoding="utf-8")
)

_NUM = {"ainsuse": "sg", "mitmuse": "pl"}
_CASE = {"nimetav": ["n"], "omastav": ["g"], "osastav": ["p"],
         "sisseütlev": ["ill", "adt"]}


def _inflect_phrase(phrase: str, plurality: str, case: str) -> str:
    """The harness loop scripts/eval_inflection.py runs, on server code.

    Kept short and duplicated on purpose: this file must not import the
    eval script, which needs the `datasets` package and network-fetched
    data that CI does not have.
    """
    num = _NUM[plurality]
    codes = [f"{num} {c}" for c in _CASE[case]]
    out = []
    for w in phrase.split():
        if server._is_indeclinable_attr(w):
            out.append(w)
            continue
        try:
            a = server._vabamorf().analyze([w], disambiguate=True)[0].get("analysis") or []
        except Exception:
            a = []
        pos = a[0]["partofspeech"] if a else ""
        hints, _ = server._paradigm_hints(w, pos, "sg g")
        got = server._synthesize(w, codes[0], pos, hints[0])
        out.append(got[0] if got else w)
    return " ".join(out)


def eki_rules_hold_on_the_disputed_rows() -> None:
    """The 13 rows of inflection_et that contradict EKI.

    Dataset-independent on purpose: it asserts what OUR engine produces for
    those phrases, so it runs in CI with no dataset download, and it fails
    if a change ever makes us agree with the bad gold instead of with EKI.
    """
    print("the 13 EKI-disputed rows: we produce the EKI form")
    for d in DISPUTES["disputes"]:
        got = _inflect_phrase(d["noun_phrase"], d["plurality"], d["case"])
        check(f"{d['noun_phrase']!r} {d['plurality']} {d['case']} -> {got!r}",
              got in d["eki_forms"],
              f"expected one of {d['eki_forms']}, and NOT the gold {d['dataset_gold']}")
        check(f"  ... and not the dataset's gold {d['dataset_gold'][0]!r}",
              got not in d["dataset_gold"], f"got {got!r}")


def the_two_eki_rules_are_encoded() -> None:
    print("the underlying rules")
    # -tud/-dud/-nud is invariant as a pre-modifier.
    for w in ("läbimõeldud", "rafineeritud", "tuntud"):
        check(f"{w} is indeclinable", server._is_indeclinable_attr(w) is True)
    # -v agrees.
    for w in ("rahuldav", "süüdistav", "ekslev"):
        check(f"{w} agrees", server._is_indeclinable_attr(w) is False)
    check("rahuldav sg g = rahuldava",
          server._synthesize("rahuldav", "sg g", "A") == ["rahuldava"],
          str(server._synthesize("rahuldav", "sg g", "A")))


def disputes_file_is_well_formed() -> None:
    print("data/inflection_et_eki_disputes.json")
    ds = DISPUTES["disputes"]
    check("13 disputes recorded", len(ds) == 13, str(len(ds)))
    keys = {(d["noun_phrase"], d["plurality"], d["case"]) for d in ds}
    check("no duplicate rows", len(keys) == len(ds))
    for d in ds:
        label = f"{d['noun_phrase']} {d['plurality']} {d['case']}"
        check(f"{label}: has the dataset gold recorded",
              bool(d["dataset_gold"]) and all(d["dataset_gold"]))
        check(f"{label}: has an EKI form", bool(d["eki_forms"]) and all(d["eki_forms"]))
        check(f"{label}: gold and EKI actually differ",
              sorted(d["dataset_gold"]) != sorted(d["eki_forms"]))
        check(f"{label}: cites a rule that is defined",
              d["rule"] in DISPUTES["rules"], d["rule"])
    for name, r in DISPUTES["rules"].items():
        check(f"rule {name} has an Estonian statement", bool(r.get("rule_estonian")))
        check(f"rule {name} has an English statement", bool(r.get("rule_english")))
    check("the dataset revision is pinned", bool(DISPUTES.get("dataset_revision")))
    check("sources are cited", len(DISPUTES.get("sources", [])) >= 1)


ordinals_comparatives_superlatives_inflect()
genuinely_uninflecting_words_still_say_so()
inflecting_reading_is_found()
wrong_lemma_readings_are_not_rescued()
homonym_paradigms_are_separated()
corpus_attestation_ranks_the_common_word_first()
degrades_without_the_corpus_model()
an_inflected_input_selects_its_paradigm()
synthesis_invariants()
invariant_words_are_labelled()
junk_input_is_safe()
eki_rules_hold_on_the_disputed_rows()
the_two_eki_rules_are_encoded()
disputes_file_is_well_formed()

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nall paradigm tests passed")
