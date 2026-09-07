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
skipped: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS {label}")
    else:
        failures.append(f"{label}: {detail}")
        print(f"  FAIL {label} {detail}")


def case_forms(result: dict) -> list[dict]:
    """The 28 number-and-case slots, without the short illative.

    `adt` (lühike sisseütlev) exists only for the words that have one:
    maja gives majja, raamat gives nothing. Counting it would make the
    expected total depend on the word, so the assertions below count the
    fixed 28 and the short illative is checked on its own.
    """
    return [e for e in result.get("forms", []) if e.get("form") != "adt"]


def surfaces(entry: dict) -> list[str]:
    s = entry["surface"]
    return [s] if isinstance(s, str) else list(s)


def mine_only(by_form: list[dict], i: int, code: str) -> set:
    """Surfaces of slot `code` that ONLY table i produces."""
    others = set()
    for j, t in enumerate(by_form):
        if j != i:
            others |= t.get(code, set())
    return by_form[i].get(code, set()) - others


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
        check(f"{word} ({pos}) has 28 case forms", len(case_forms(r)) == 28,
              f"got {len(case_forms(r))}: {r.get('summary_estonian')}")
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
    if not HAVE_CORPUS:
        # Promotion is gated on corpus attestation (see
        # a_rare_reading_is_not_promoted), so with no model there is
        # nothing to assert here beyond the degraded path, which that
        # test covers.
        print("  SKIP  no fastText model installed, promotion not exercised")
        skipped.append("inflecting_reading_is_found")
        return
    r = server._paradigm("kaunis")
    check("kaunis has a paradigm", len(case_forms(r)) == 28,
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
        # The property that actually matters, checked exactly rather than
        # by a prefix heuristic (a prefix test misses `kotta` leaking into
        # the `koti` table, since it starts with neither key): every table
        # must be EXACTLY what strict hinted synthesis produces. If any
        # relaxation ever creeps back into _synthesize, this fails.
        for key, entries in named:
            for e in entries:
                expect = server._synthesize(word, e["form"], r["partofspeech"], key)
                got = surfaces(e)
                # As a SET: the property here is that no surface enters or
                # leaves a table, and a slot's variants are deliberately
                # ordered by corpus attestation now, which
                # `variants_are_ordered_by_attestation` checks separately.
                check(f"{word}/{key} {e['form']}: exactly the hinted synthesis",
                      sorted(got) == sorted(expect), f"table={got} strict={expect}")


def corpus_attestation_ranks_the_common_word_first() -> None:
    print("the paradigm Estonian actually uses is ranked first")
    if not HAVE_CORPUS:
        # Not a pass. CI's smoke job runs fetch_resources.py first, so this
        # branch only happens on a local run with no model, and it must be
        # visible as a gap rather than counted as a green check.
        print("  SKIP  no fastText model installed, ranking assertions not run")
        skipped.append("corpus_attestation_ranks_the_common_word_first")
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
    # The constraint is honoured strictly. Relaxing it on an empty result
    # spliced another lexeme into the table: the pronoun `iga` has no
    # plural, and the relaxed call filled it in from the noun `iga` "age".
    check("a POS that cannot make the form returns nothing, not another word's",
          server._synthesize("kaunis", "sg g", "D") == [],
          str(server._synthesize("kaunis", "sg g", "D")))
    check("iga has no plural under its own POS",
          server._synthesize("iga", "pl n", "P") == [],
          str(server._synthesize("iga", "pl n", "P")))
    check("and the relaxed call is what would have invented one",
          "ead" in server._synthesize("iga", "pl n", ""),
          "if this fails the example in the docstring is stale, not the code")
    check("free variants inside ONE paradigm are kept",
          set(server._synthesize("kaunis", "pl g", "A")) == {"kaunite", "kauniste"},
          str(server._synthesize("kaunis", "pl g", "A")))
    check("unknown input returns a list, not an exception",
          isinstance(server._synthesize("qwertyxyz", "sg g", "S"), list))

    print("no form is silently dropped from a table")
    # 39 for the verb: 0.6.0 added the umbisikuline finite forms, the
    # des- and maks-vorm and the 2nd person singular imperative, all of
    # which Vabamorf generates and none of which the table carried.
    cases = [("kott", 28), ("maitse", 28), ("esimene", 28), ("kasutama", 39)]
    if HAVE_CORPUS:
        cases.append(("kaunis", 28))   # reaches 28 only once promoted
    for word, n in cases:
        r = server._paradigm(word)
        # Verbs have no short illative, so case_forms() is a no-op there.
        check(f"{word} has {n} forms", len(case_forms(r)) == n,
              f"got {len(case_forms(r))}")

    print("_paradigm_hints")
    check("an unambiguous lemma needs no hint",
          server._paradigm_hints("maitse", "S", "sg g") == ([""], False),
          str(server._paradigm_hints("maitse", "S", "sg g")))
    hints, _ = server._paradigm_hints("kott", "S", "sg g")
    check("an ambiguous lemma yields one hint per paradigm",
          sorted(hints) == ["kota", "koti"], str(hints))


def no_form_is_invented_for_a_word_that_lacks_it() -> None:
    """The regression the first cut of this change shipped.

    `_synthesize` relaxed its POS constraint whenever a form came up
    empty, which fills a real gap with another lexeme's forms. `iga` is a
    pronoun with no plural; the noun `iga` "age" has one, so the table
    grew `pl n: ead`. That is exactly the splice this module separates
    inflection types to prevent. Numbers below are `origin/master`'s, which
    was right here.
    """
    print("a word that lacks a form does not get one invented")
    for word, n_forms in (("iga", 14), ("keegi", 14), ("midagi", 14),
                          ("kogu", 1), ("ei", 0)):
        r = server._paradigm(word)
        check(f"{word} has {n_forms} forms", len(r.get("forms", [])) == n_forms,
              f"got {len(r.get('forms', []))}: "
              f"{[(e['form'], e['surface']) for e in r.get('forms', [])[:4]]}")
    r = server._paradigm("iga")
    check("iga has no plural at all",
          not [e for e in r["forms"] if e["form"].startswith("pl")],
          str([e["form"] for e in r["forms"] if e["form"].startswith("pl")]))
    check("and specifically not the noun iga's plural",
          "ead" not in {s for e in r["forms"] for s in surfaces(e)})


def verb_free_variants_are_not_two_inflection_types() -> None:
    """`öelda` / `ütelda` are rööpvormid of one lexeme, not two muuttüüpi.

    Splitting on the da-infinitive produced `paradigm_count: 2`, a
    byte-identical duplicate table in `other_paradigms`, and an Estonian
    sentence asserting a distinction that does not exist.
    """
    print("verbs are not split on free-variant infinitives")
    for verb in ("ütlema", "mõtlema", "jooksma", "kasutama"):
        r = server._paradigm(verb)
        check(f"{verb} is one paradigm", r.get("paradigm_count") == 1,
              f"count={r.get('paradigm_count')} keys="
              f"{[o['paradigm_key'] for o in r.get('other_paradigms', [])]}")
        check(f"{verb} makes no ambiguity claim", "ambiguity_estonian" not in r,
              str(r.get("ambiguity_estonian")))
    # The free variants are still both there, inside the one table.
    forms = {e["form"]: surfaces(e) for e in server._paradigm("ütlema")["forms"]}
    check("both da-forms are kept as variants of the one paradigm",
          set(forms.get("da", [])) == {"öelda", "ütelda"}, str(forms.get("da")))


def a_shared_form_does_not_select_a_type() -> None:
    """`kotti` is the sg partitive of `koti` AND the pl partitive of `kota`.

    Taking the first table containing the input answered `paradigm("kotti")`
    with `kota` whenever corpus ranking was unavailable, while claiming the
    input had decided it.
    """
    print("an input form shared by both types decides nothing")
    r = server._paradigm("kott")
    named = ([(r["paradigm_key"], r["forms"])]
             + [(o["paradigm_key"], o["forms"]) for o in r["other_paradigms"]])
    sets = [{s.lower() for e in entries for s in surfaces(e)} for _k, entries in named]
    check("kotti really is in both tables (the premise)",
          "kotti" in sets[0] and "kotti" in sets[1], str(sorted(sets[0] & sets[1])))

    original = server._corpus_ranks
    server._corpus_ranks = lambda: None
    try:
        r = server._paradigm("kotti")
        check("a shared form does not claim to have decided",
              "sinu antud vorm" not in (r.get("ambiguity_estonian") or ""),
              str(r.get("ambiguity_estonian")))
        check("a shared form leaves ranked_by_corpus_frequency alone",
              r.get("ranked_by_corpus_frequency") is False)
        for word, key in (("koti", "koti"), ("kotta", "kota"), ("kotisse", "koti")):
            r = server._paradigm(word)
            check(f"{word} (unique to one type) still selects {key}",
                  r.get("paradigm_key") == key, str(r.get("paradigm_key")))
            check(f"{word} says the input decided",
                  "sinu antud vorm" in (r.get("ambiguity_estonian") or ""))
    finally:
        server._corpus_ranks = original


def a_rare_reading_is_not_promoted() -> None:
    """`kohe` ("immediately") has an adjective reading of its own lemma,
    `kohe : koheda`, that almost nobody means. Corpus attestation is what
    separates it from `kaunis`, whose adjective reading is the common one."""
    print("only corpus-attested readings are promoted")
    r = server._paradigm("kohe")
    check("kohe is not promoted to the koheda adjective",
          r.get("forms") == [] and r.get("partofspeech") == "D",
          f"pos={r.get('partofspeech')} forms={len(r.get('forms', []))}")
    if HAVE_CORPUS:
        ranks = server._corpus_ranks()
        check("the premise: kauni is attested, koheda is not",
              "kauni" in ranks and "koheda" not in ranks)
        check("kaunis IS promoted", len(case_forms(server._paradigm("kaunis"))) == 28)

    original = server._corpus_ranks
    server._corpus_ranks = lambda: None
    try:
        r = server._paradigm("kaunis")
        check("without corpus data nothing is promoted", r.get("forms") == [],
              f"got {len(r.get('forms', []))} forms")
    finally:
        server._corpus_ranks = original


def the_verb_table_covers_what_people_write() -> None:
    """The table listed `vat`, which does not occur in the corpus
    vocabulary at all, and `tavat`, which is rank 24,225, and omitted
    `takse`, which is rank 232.

    Estonian officialese is written largely in the umbisikuline
    tegumood, and the table carried that voice's participles and its
    quotative but none of its indicative or conditional forms: no
    kasutatakse, no kasutati, no kasutataks. An agent asking for a verb's
    paradigm could not find the form the text in front of it was using.
    """
    print("the verb table carries the umbisikuline finite forms")
    r = server._paradigm("kasutama")
    got = {e["form"]: (e["surface"], e["form_estonian"]) for e in r["forms"]}
    for code, surface in (("takse", "kasutatakse"), ("ti", "kasutati"),
                          ("taks", "kasutataks"), ("ta", "kasutata"),
                          ("tagu", "kasutatagu")):
        check(f"{code} -> {surface}", got.get(code, ("",))[0] == surface, str(got.get(code)))
        check(f"{code} is labelled umbisikuline",
              "umbisikuline" in (got.get(code, ("", ""))[1] or ""), str(got.get(code)))

    print("and the forms an ordinary writer reaches for")
    for code, surface in (("des", "kasutades"), ("maks", "kasutamaks"), ("o", "kasuta"),
                          ("nuks", "kasutanuks")):
        check(f"{code} -> {surface}", got.get(code, ("",))[0] == surface, str(got.get(code)))

    print("participle labels name the voice, and name it the same way twice")
    for code in ("v", "nud", "tav", "tud"):
        check(f"{code}: {_EXPECTED_VERB_LABELS[code]}",
              got.get(code, ("", ""))[1] == _EXPECTED_VERB_LABELS[code], str(got.get(code)))
    labels = " ".join(server._VERB_LABELS_ET.values())
    check("the coined 'tegumoeline' is gone", "tegumoeline" not in labels,
          "the file uses 'umbisikuline tegumood' everywhere else")

    print("and every slot the paradigm hands back is pinned to its exact name")
    # Pinned by surface alone, a slot could be relabelled to nonsense and
    # the whole suite stayed green.
    for code, expected in _EXPECTED_VERB_LABELS.items():
        check(f"{code}: {expected}", got.get(code, ("", ""))[1] == expected, str(got.get(code)))
    check("the table lists exactly the pinned slots",
          set(server._VERB_LABELS_ET) == set(_EXPECTED_VERB_LABELS),
          str(set(server._VERB_LABELS_ET) ^ set(_EXPECTED_VERB_LABELS)))

    print("a code covering two slots names both, rather than picking one")
    # `kasuta` is the imperative AND the form after `ei`; `kasutagu` is
    # 3rd person of either number; `kasutaks` serves every person without
    # an ending. Naming only the first reading tells an agent that
    # "ei tea" is a command and that "nad tulgu" is singular.
    for code in ("o", "gu", "ks"):
        check(f"{code} names both readings", " / " in _EXPECTED_VERB_LABELS[code],
              _EXPECTED_VERB_LABELS[code])

    print("every verb form still synthesises, so nothing was added on faith")
    for form in server._VERB_FORMS:
        ok = any(server._synthesize(w, form, "V") for w in ("kasutama", "olema", "tegema"))
        check(f"{form!r} synthesises", ok, "added to the table but generates nothing")


# The 14 cases in each number, plus the short illative, which has no
# number prefix. `paradigm` labels these and `analyze_morphology` reads
# the same table, so a wrong one is wrong in both.
_EXPECTED_CASE_LABELS = {
    "sg n": "ainsuse nimetav", "sg g": "ainsuse omastav",
    "sg p": "ainsuse osastav", "sg ill": "ainsuse sisseütlev",
    "adt": "ainsuse lühike sisseütlev", "sg in": "ainsuse seesütlev",
    "sg el": "ainsuse seestütlev", "sg all": "ainsuse alaleütlev",
    "sg ad": "ainsuse alalütlev", "sg abl": "ainsuse alaltütlev",
    "sg tr": "ainsuse saav", "sg ter": "ainsuse rajav",
    "sg es": "ainsuse olev", "sg ab": "ainsuse ilmaütlev",
    "sg kom": "ainsuse kaasaütlev",
    "pl n": "mitmuse nimetav", "pl g": "mitmuse omastav",
    "pl p": "mitmuse osastav", "pl ill": "mitmuse sisseütlev",
    "pl in": "mitmuse seesütlev", "pl el": "mitmuse seestütlev",
    "pl all": "mitmuse alaleütlev", "pl ad": "mitmuse alalütlev",
    "pl abl": "mitmuse alaltütlev", "pl tr": "mitmuse saav",
    "pl ter": "mitmuse rajav", "pl es": "mitmuse olev",
    "pl ab": "mitmuse ilmaütlev", "pl kom": "mitmuse kaasaütlev",
}


# Every label the two tools can hand back, stated here independently of
# the tables that produce them, so relabelling one entry fails the suite.
_EXPECTED_VERB_LABELS = {
    "ma": "ma-tegevusnimi", "da": "da-tegevusnimi", "des": "des-vorm",
    "maks": "maks-vorm", "vat": "vat-vorm", "tavat": "tavat-vorm",
    "mas": "mas-vorm", "mast": "mast-vorm", "mata": "mata-vorm",
    "n": "olevik 1.p ainsus", "d": "olevik 2.p ainsus", "b": "olevik 3.p ainsus",
    "me": "olevik 1.p mitmus", "te": "olevik 2.p mitmus", "vad": "olevik 3.p mitmus",
    "sin": "lihtminevik 1.p ainsus", "sid": "lihtminevik 2.p ainsus / 3.p mitmus",
    "s": "lihtminevik 3.p ainsus", "sime": "lihtminevik 1.p mitmus",
    "site": "lihtminevik 2.p mitmus",
    "ksin": "tingiv 1.p ainsus", "ksid": "tingiv 2.p ainsus / 3.p mitmus",
    "ks": "tingiv 3.p ainsus / pöördelõputa vorm", "ksime": "tingiv 1.p mitmus",
    "ksite": "tingiv 2.p mitmus",
    "nud": "isikuline mineviku kesksõna", "tud": "umbisikuline mineviku kesksõna",
    "v": "isikuline oleviku kesksõna", "tav": "umbisikuline oleviku kesksõna",
    "o": "käskiv 2.p ainsus / isikuline eitav vorm",
    "gu": "käskiv 3.p ainsus / mitmus",
    "gem": "käskiv 1.p mitmus", "ge": "käskiv 2.p mitmus",
    "takse": "umbisikuline olevik", "ti": "umbisikuline lihtminevik",
    "taks": "umbisikuline tingiv", "ta": "umbisikuline eitav vorm",
    "tagu": "umbisikuline käskiv", "nuks": "tingiv minevik",
}

# Codes no paradigm slot lists, so only analysis ever returns them.
_EXPECTED_ANALYSIS_LABELS = {
    "nuksin": "tingiv minevik 1.p ainsus",
    "nuksid": "tingiv minevik 2.p ainsus / 3.p mitmus",
    "nuksime": "tingiv minevik 1.p mitmus",
    "nuksite": "tingiv minevik 2.p mitmus",
    "nuvat": "nuvat-vorm", "tuvat": "tuvat-vorm",
    "tuks": "umbisikuline tingiv minevik",
    "tama": "umbisikuline ma-tegevusnimi",
}

# One word each. `neg o` is all three, which is why the lemma decides.
_EXPECTED_NEG_LABELS = {
    ("ära", "o"): "eitav käskiv 2.p ainsus",
    ("ära", "ge"): "eitav käskiv 2.p mitmus",
    ("ära", "gem"): "eitav käskiv 1.p mitmus",
    ("ära", "me"): "eitav käskiv 1.p mitmus",
    ("ära", "gu"): "eitav käskiv",
    ("olema", "o"): "eitav olevik",
    ("olema", "da"): "umbisikuline eitav vorm",
    ("olema", "nud"): "eitav lihtminevik",
    ("olema", "tud"): "umbisikuline eitav lihtminevik",
    ("olema", "ks"): "eitav tingiv",
    ("olema", "nuks"): "eitav tingiv minevik",
    ("olema", "vat"): "eitav vat-vorm",
    ("minema", "o"): "isikuline eitav vorm",
}


def analysis_form_codes_carry_their_estonian_name() -> None:
    """`analyze_morphology` returned raw codes: a caller was told a word
    is `adt` or `takse` and left to guess, in a server whose rule is that
    every English label carries a correct Estonian rendering."""
    print("analyze_morphology glosses its form codes")
    for text, word, code, label in (
        ("Läksin majja.", "majja", "adt", "ainsuse lühike sisseütlev"),
        ("Seda kasutatakse tihti.", "kasutatakse", "takse", "umbisikuline olevik"),
        ("Ma ei tea.", "ei", "neg", "eitussõna"),
    ):
        rec = next(w for w in server.analyze_morphology(text) if w["word"] == word)
        check(f"{word}: {code} -> {label}",
              rec["form"] == code and rec["form_estonian"] == label, str(rec.get("form_estonian")))

    print("a neg code is read against its lemma, not by prefixing 'eitav'")
    # `neg o` is `ära`, and also `pole`, and also `lähe`. Prefixing
    # "eitav" to the ordinary label for the suffix called `pole` a
    # command, `polnud` a participle and `ärme` an indicative. Each of
    # these is a whole word an agent meets in ordinary Estonian, so each
    # goes through the tool both ways round.
    for text, word, lemma, code in (
        ("Ära unusta!", "Ära", "ära", "neg o"),
        ("Ärge unustage!", "Ärge", "ära", "neg ge"),
        ("Ärme mine sinna.", "Ärme", "ära", "neg me"),
        ("Ärgu oldagu pahased.", "Ärgu", "ära", "neg gu"),
        ("Ma pole kodus.", "pole", "olema", "neg o"),
        ("Ma polnud kodus.", "polnud", "olema", "neg nud"),
        ("Ma poleks tulnud.", "poleks", "olema", "neg ks"),
        ("Seda polda tehtud.", "polda", "olema", "neg da"),
        ("Seda poldud tehtud.", "poldud", "olema", "neg tud"),
        ("Ta polevat kodus.", "polevat", "olema", "neg vat"),
        ("Ma ei lähe koju.", "lähe", "minema", "neg o"),
    ):
        expected = _EXPECTED_NEG_LABELS[(lemma, code[4:])]
        rec = next(w for w in server.analyze_morphology(text) if w["word"] == word)
        check(f"{word}: {code} ({lemma}) -> {expected}",
              rec["form"] == code and rec["lemma"] == lemma
              and rec["form_estonian"] == expected,
              f"{rec['lemma']!r} {rec['form']!r} -> {rec['form_estonian']!r}")
        alt = next(a for w in server.analyze_morphology(text, all_analyses=True)
                   if w["word"] == word for a in w["analyses"] if a["form"] == code)
        check(f"{word}: same under all_analyses", alt["form_estonian"] == expected,
              str(alt["form_estonian"]))

    print("the case table is pinned the same way")
    nominal = {e["form"]: e["form_estonian"] for e in server._paradigm("maja")["forms"]}
    for code, expected in _EXPECTED_CASE_LABELS.items():
        check(f"{code}: {expected}", nominal.get(code) == expected, str(nominal.get(code)))
    check("the case table lists exactly the pinned cases",
          server._CASE_LABELS_ET == _EXPECTED_CASE_LABELS,
          str(set(server._CASE_LABELS_ET) ^ set(_EXPECTED_CASE_LABELS)))

    print("and the tables say exactly what they are pinned to say")
    check("the neg table holds exactly the pinned entries",
          {(fam, c): lab for fam, d in server._NEG_LABELS_ET.items()
           for c, lab in d.items()} == _EXPECTED_NEG_LABELS,
          str(server._NEG_LABELS_ET))
    check("the analysis-only table holds exactly the pinned entries",
          server._ANALYSIS_FORM_LABELS_ET == _EXPECTED_ANALYSIS_LABELS,
          str(server._ANALYSIS_FORM_LABELS_ET))

    print("and a code with no label says so rather than echoing itself")
    check("unknown codes give None", server._form_et("zzz") is None, str(server._form_et("zzz")))
    check("an empty code gives None", server._form_et("") is None)
    # Composition invented "eitav ainsuse nimetav" for this. A lookup cannot.
    check("a fabricated neg code is not named", server._form_et("neg sg n") is None,
          str(server._form_et("neg sg n")))
    check("a neg code without its lemma is not guessed at",
          server._form_et("neg o") is None, str(server._form_et("neg o")))

    print("every form code Vabamorf declares has a name")
    # EstNLTK ships Vabamorf's own inventory. Reading it here means a
    # code we have never seen still fails the test rather than reaching a
    # caller unnamed.
    from estnltk.taggers.standard.morph_analysis.morf_common import (
        VABAMORF_NOUN_FORMS,
        VABAMORF_VERB_FORMS,
    )
    plain = [c for c in VABAMORF_VERB_FORMS if not c.startswith("neg")]
    unnamed = [c for c in plain if server._form_et(c) is None]
    check(f"all {len(plain)} plain verb codes are named", not unnamed, str(unnamed))
    negs = [c for c in VABAMORF_VERB_FORMS if c.startswith("neg ")] + ["neg da"]
    unnamed = [c for c in negs
               if not any(server._form_et(c, lm)
                          for lm in ("ära", "olema", "minema"))]
    check(f"all {len(negs)} neg codes are named for their lemma", not unnamed, str(unnamed))
    cases = [c for c in VABAMORF_NOUN_FORMS if c not in ("sg", "pl")]
    unnamed = [c if c == "adt" else f"{n} {c}"
               for n in ("sg", "pl") for c in cases
               if server._form_et(c if c == "adt" else f"{n} {c}") is None]
    check(f"all {len(cases) * 2 - 1} nominal codes are named", not unnamed, str(unnamed))

    print("coverage over ordinary prose")
    text = ("Käesoleva lepingu alusel sätestatakse poolte kohustused. Andmeid töödeldakse "
            "ja säilitatakse seaduses ettenähtud korras. Ma ei tea, kas seda kasutati "
            "varem. Kasutades neid vahendeid, tuleb olla ettevaatlik. Ära unusta lepingut "
            "allkirjastada! Seda ei kasutatud ja poleks pidanudki kasutama.")
    words = [w for w in server.analyze_morphology(text) if w["form"]]
    missing = [(w["word"], w["form"]) for w in words if w["form_estonian"] is None]
    check(f"every form code in {len(words)} words is named", not missing, str(missing))


def eki_corrections_apply_only_where_they_still_fit() -> None:
    """`scripts/apply_eki_corrections.py` rebuilds inflection_et with the
    13 adjudicated rows fixed.

    The dataset carries no licence, so the corrections live here and the
    data does not: the script fetches the original and patches a local
    copy. The property that matters is that it patches exactly the rows
    it recorded a dispute against, and refuses a row whose gold has moved
    upstream, because a correction written against data that has since
    changed is not a correction any more.
    """
    print("EKI corrections apply to exactly the disputed rows")
    sys.path.insert(0, str(_ROOT / "scripts"))
    import apply_eki_corrections as aec

    doc = json.loads((_ROOT / "data" / "inflection_et_eki_disputes.json").read_text("utf-8"))
    disputes = doc["disputes"]
    # A stand-in dataset: every disputed row as recorded, plus one row
    # nobody disputes.
    rows = [{"noun_phrase": d["noun_phrase"], "plurality": d["plurality"],
             "case": d["case"], "inflection": list(d["dataset_gold"])} for d in disputes]
    rows.append({"noun_phrase": "kollane päevalill", "plurality": "ainsuse",
                 "case": "omastav", "inflection": ["kollase päevalille"]})

    out, applied, stale = aec.apply_corrections(rows, disputes)
    check("every dispute applies", len(applied) == len(disputes), f"{len(applied)}")
    check("nothing is stale", stale == [], str(stale))
    check("no row is added or lost", len(out) == len(rows), f"{len(out)} vs {len(rows)}")
    check("the undisputed row is untouched", out[-1] == rows[-1], str(out[-1]))
    check("each corrected row carries the EKI forms",
          all(o["inflection"] == d["eki_forms"]
              for o, d in zip(out, disputes, strict=False)),
          str([o["inflection"] for o in out[:2]]))

    print("and a row that moved upstream is left alone")
    moved = [dict(r) for r in rows]
    moved[0]["inflection"] = ["something the authors changed"]
    out2, applied2, stale2 = aec.apply_corrections(moved, disputes)
    check("the moved row keeps the authors' gold",
          out2[0]["inflection"] == ["something the authors changed"], str(out2[0]))
    check("and its dispute is reported stale", len(stale2) == 1, str(len(stale2)))
    check("while the rest still apply", len(applied2) == len(disputes) - 1, str(len(applied2)))


def variants_are_ordered_by_the_paradigm_stem() -> None:
    """A slot with two surfaces must lead with the one this paradigm
    builds on its own genitive stem.

    Estonian forms its oblique plural cases on the genitive plural, and
    Vabamorf returns variants in lexicon order: raamat came back as
    pl all: raamatuile, raamatutele. `raamatute` is the genitive plural,
    so `raamatutele` is the form built on this paradigm's stem;
    `raamatuile` is the i-plural, real but literary, built on another.

    Measured on 11,011 rows of real Estonian: first-candidate 88.0%
    before, 99.3% after, any-candidate 99.9%.
    """
    print("a slot's variants lead with the form built on this paradigm's stem")
    for word, form, expected_first in (
        ("raamat", "pl all", "raamatutele"),
        ("raamat", "pl el", "raamatutest"),
        ("küsimus", "pl all", "küsimustele"),
        ("inimene", "pl el", "inimestest"),
    ):
        got = form_of(server._paradigm(word), form)
        check(f"{word} {form} leads with {expected_first}",
              got and got[0] == expected_first, str(got))
        check(f"and still offers the other variant for {word} {form}",
              len(got) > 1, str(got))

    # Ranking these by CORPUS FREQUENCY instead scored 5 points lower and
    # promoted homographs, because a surface can be frequent as another
    # word entirely. Both of these regressed under that rule.
    print("and a homograph of another word does not win the slot")
    for word, form, expected_first, homograph in (
        ("käsi", "pl ad", "kätel", "käsil"),      # käsil is a lexicalised adverb
        ("soov", "pl p", "soove", "soovisid"),    # soovisid is 2sg past of soovima
    ):
        got = form_of(server._paradigm(word), form)
        check(f"{word} {form} leads with {expected_first}, not {homograph}",
              got and got[0] == expected_first, str(got))

    print("a slot the stem cannot decide keeps Vabamorf's order")
    # Neither pl p variant of `soov` starts with the pl g stem `soovide`,
    # so the rule must not touch the order rather than guess at one.
    strict = server._synthesize("soov", "pl p", "S")
    check("undecidable slots are returned unchanged",
          form_of(server._paradigm("soov"), "pl p") == strict, str(strict))

    print("and the ranking needs no model at all")
    calls = []
    original = server._corpus_ranks
    server._corpus_ranks = lambda: (calls.append(1), original())[1]
    try:
        for w in ("raamat", "küsimus", "inimene", "ilus", "arvuti"):
            server._paradigm(w)
        check("no corpus lookup for ordinary nouns with variants", calls == [],
              f"{len(calls)} lookups; ranking must stay model-free")
    finally:
        server._corpus_ranks = original

    print("ranking never invents, drops or duplicates a surface")
    for word in ("auto", "maja", "raamat", "käsi"):
        r = server._paradigm(word)
        for e in r["forms"]:
            strict = server._synthesize(word, e["form"], r["partofspeech"],
                                        r.get("paradigm_key") or "")
            if not strict:
                continue
            check(f"{word} {e['form']} is exactly its synthesis, reordered",
                  sorted(surfaces(e)) == sorted(strict),
                  f"table={surfaces(e)} strict={strict}")


def unambiguous_words_never_touch_the_model() -> None:
    """The model is 34 MB and lru_cache does not serialise misses, so a
    cold burst on the cheapest tool in the server could load it once per
    caller. Ranking is meaningless below two candidates, so it must not
    even be consulted there.

    "Something to rank" covers two cases now: a word with several
    PARADIGMS (kott: koti or kota), and a slot with several SURFACES
    (raamat pl all: raamatutele or raamatuile). The second was added in
    0.5.10 because leading with the unattested variant was costing 11.7
    points of first-candidate accuracy on real Estonian. A word with
    neither still must not load anything.
    """
    print("the corpus model is consulted only when there is something to rank")
    calls = []
    original = server._corpus_ranks

    def counting():
        calls.append(1)
        return original()

    server._corpus_ranks = counting
    try:
        # Variant ordering is model-free, so an ordinary word never
        # reaches the model at all now, whether or not its slots have
        # variants.
        for word in ("auto", "kasutama", "maja", "raamat", "esimene", "ilus"):
            server._paradigm(word)
        check("no lookup for any single-paradigm word", calls == [], f"{len(calls)} lookups")
        # There IS a second call site, and it is not this one: a word
        # whose isolated reading does not inflect goes through
        # _inflecting_reading, which ranks candidate readings.
        server._paradigm("kohe")
        check("the promotion path still consults it", calls != [],
              "if this stops being true, _inflecting_reading changed")
        server._paradigm("kott")
        check("but there is one when a lemma has two types", len(calls) >= 1)
    finally:
        server._corpus_ranks = original


def every_return_path_carries_paradigm_count() -> None:
    """CI itself reads r["paradigm_count"]; the short-circuit returns used
    to omit it, so any particle KeyError'd."""
    print("paradigm_count is on every return path")
    for word in ("ja", "väga", "qwertyuiopasdf", "kott", "kasutama", "esimene"):
        r = server._paradigm(word)
        check(f"{word} has paradigm_count", "paradigm_count" in r, str(sorted(r)))


def estonian_labels_accompany_every_pos_code() -> None:
    """Project rule: an English or tagset label never ships without a
    correct Estonian rendering."""
    print("POS codes are glossed in Estonian")
    for word, pos, et in (("kott", "S", "nimisõna"), ("ilus", "A", "omadussõna"),
                          ("esimene", "O", "järgarvsõna"),
                          ("parem", "C", "omadussõna keskvõrdes"),
                          ("parim", "U", "omadussõna ülivõrdes"),
                          ("kasutama", "V", "tegusõna"),
                          ("ja", "J", "sidesõna"), ("väga", "D", "määrsõna")):
        r = server._paradigm(word)
        check(f"{word}: {pos} = {et}",
              r.get("partofspeech") == pos and r.get("partofspeech_estonian") == et,
              f"pos={r.get('partofspeech')} et={r.get('partofspeech_estonian')}")
    check("word_class is glossed too",
          server._paradigm("kott")["word_class_estonian"] == "käändsõna")
    check("and for verbs",
          server._paradigm("kasutama")["word_class_estonian"] == "tegusõna")
    missing = (server._NOMINAL_POS | {"V"}) - set(server._POS_LABELS_ET)
    check("every code we can emit has an Estonian name", not missing, str(sorted(missing)))
    # "käändeline vorm" is the term for a verb's nominal forms, so asking
    # for one would be asking for a participle. The word wanted is
    # "käändevorm".
    et = server._paradigm("kott").get("ambiguity_estonian") or ""
    check("asks for a käändevorm, not a käändeline vorm",
          "käändevorm" in et and "käändeline" not in et, et)


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


def the_short_illative_is_in_the_table() -> None:
    """`adt`, the lühike sisseütlev: majja beside majasse.

    The table used to stop at the long form. For these words the short one
    is what Estonians write, so a caller reading the table would "correct"
    a correct majja into majasse — the exact failure this server exists to
    prevent, and reported by a reader who had measured how much parallel
    forms cost single-reference scoring.
    """
    print("the short illative (aditiiv) is generated")
    expected = {
        "maja": "majja", "tuba": "tuppa", "käsi": "kätte", "meri": "merre",
        "kivi": "kivvi", "jõgi": "jõkke", "vesi": "vette", "suur": "suurde",
    }
    for word, short in expected.items():
        r = server._paradigm(word)
        got = form_of(r, "adt")
        check(f"{word} -> {short}", short in got, f"got {got}")
        # The long form has to survive alongside it, not be replaced.
        check(f"{word} keeps its long illative", bool(form_of(r, "sg ill")),
              str(form_of(r, "sg ill")))

    print("and is absent, not empty, where the word has none")
    for word in ("raamat", "auto", "arvuti", "töö"):
        r = server._paradigm(word)
        forms = [e["form"] for e in r.get("forms", [])]
        check(f"{word} has no adt slot", "adt" not in forms, str(form_of(r, "adt")))
        check(f"{word} still has its long illative", bool(form_of(r, "sg ill")))

    print("the slot carries its Estonian name")
    r = server._paradigm("maja")
    labels = [e["form_estonian"] for e in r["forms"] if e["form"] == "adt"]
    check("adt is labelled in Estonian", labels == ["ainsuse lühike sisseütlev"], str(labels))
    check("the note names the slot and warns against 'correcting' it",
          "lühike sisseütlev" in r["note"] and "corrected" in r["note"],
          r["note"][-200:])
    # For most words that have one, the short illative is spelled exactly
    # like the singular partitive (vend: venda is both), so a note that
    # only said "both are correct" would tell an agent to leave a real
    # case error alone.
    check("and says the surface does not confirm the case",
          "singular partitive" in r["note"] and "does NOT confirm" in r["note"],
          r["note"][-200:])
    homographs = [w for w in ("vend", "sõber", "president", "kool")
                  if form_of(server._paradigm(w), "adt") == form_of(server._paradigm(w), "sg p")]
    check("the homograph the warning is about is real", len(homographs) >= 3, str(homographs))


def every_form_string_is_one_vabamorf_accepts() -> None:
    """A form string the synthesizer does not recognise generates nothing,
    silently, for every word.

    That is how the short illative went missing twice: absent from the
    nominal table here, and present but misspelled as "sg adt" in
    scripts/eval_inflection.py, where it produced no forms at all while
    looking like support. A form nobody can generate should fail a test,
    not sit in a tuple.
    """
    print("every form string in the paradigm tables can actually be synthesized")
    probes_nominal = ("maja", "kott", "vesi", "suur", "raamat", "kaunis", "esimene")
    for form in server._NOMINAL_FORMS:
        got = any(server._synthesize(w, form, "S") or server._synthesize(w, form, "A")
                  for w in probes_nominal)
        check(f"nominal form {form!r} synthesizes", got,
              "no probe word produced this form; is the tag spelled the way Vabamorf spells it?")
    probes_verb = ("kasutama", "olema", "tegema")
    for form in server._VERB_FORMS:
        got = any(server._synthesize(w, form, "V") for w in probes_verb)
        check(f"verb form {form!r} synthesizes", got, "no probe verb produced this form")

    print("and so can the ones the benchmark harness builds")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    try:
        import eval_inflection
    except Exception as e:   # pragma: no cover - the harness is dev-only
        check("the eval harness imports without its dataset dependency", False, str(e))
        return
    # Through forms_for(), the function the scoring loop itself calls.
    # Checking the constant beside it would not have caught the original
    # defect: the constant was right and the string built from it was not.
    for case in eval_inflection._CASE:
        for num in ("sg", "pl"):
            built = eval_inflection.forms_for(case, num)
            check(f"harness builds codes for {num} {case}", bool(built))
            for form in built:
                got = any(server._synthesize(w, form, "S") for w in probes_nominal)
                check(f"harness form {form!r} ({case}) synthesizes", got,
                      "the harness asks Vabamorf for a code it answers nothing to")

    print("and the order the harness builds is the order it scores on")
    # word_surfaces() takes form_codes[0] as the first candidate, so the
    # long illative has to come first. Reversing this leaves every other
    # check green and drops published first-candidate accuracy to 87.9%.
    check("the long illative is the first candidate",
          eval_inflection.forms_for("sisseütlev", "sg") == ["sg ill", "adt"],
          str(eval_inflection.forms_for("sisseütlev", "sg")))
    for case in eval_inflection._CASE:
        for num in ("sg", "pl"):
            built = eval_inflection.forms_for(case, num)
            check(f"{num} {case} leads with the numbered code",
                  built[0] == f"{num} {eval_inflection._CASE[case][0]}", str(built))

    print("and the short illative reaches the words that have one")
    sg_ill = eval_inflection.forms_for("sisseütlev", "sg")
    produced = {surface for f in sg_ill for surface in server._synthesize("maja", f, "S")}
    check("a singular illative row generates majja as well as majasse",
          produced == {"majja", "majasse"}, str(produced))
    check("a plural illative row does not ask for a short form",
          all(f != eval_inflection._SHORT_ILLATIVE_FORM
              for f in eval_inflection.forms_for("sisseütlev", "pl")),
          str(eval_inflection.forms_for("sisseütlev", "pl")))
    check("no other case asks for one either",
          all(eval_inflection._SHORT_ILLATIVE_FORM not in eval_inflection.forms_for(c, "sg")
              for c in eval_inflection._CASE if c != "sisseütlev"))


ordinals_comparatives_superlatives_inflect()
no_form_is_invented_for_a_word_that_lacks_it()
verb_free_variants_are_not_two_inflection_types()
a_shared_form_does_not_select_a_type()
a_rare_reading_is_not_promoted()
variants_are_ordered_by_the_paradigm_stem()
the_verb_table_covers_what_people_write()
analysis_form_codes_carry_their_estonian_name()
eki_corrections_apply_only_where_they_still_fit()
unambiguous_words_never_touch_the_model()
every_return_path_carries_paradigm_count()


estonian_labels_accompany_every_pos_code()
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
the_short_illative_is_in_the_table()
every_form_string_is_one_vabamorf_accepts()

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
if skipped:
    print(f"\n{len(skipped)} group(s) skipped for missing resources:")
    for g in skipped:
        print(" -", g)
print("\nall paradigm tests passed")
