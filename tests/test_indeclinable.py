"""Tests for attributive indeclinability (issue #42).

`_is_indeclinable_attr` decides whether an attribute stays in base form
under adjective-noun agreement. Two defects are pinned here.

1. The reported one: the `-mata` form was omitted. EKI is explicit that it
   is the tud-participle's negative counterpart and "jääb alati
   käändumatuks" — `täitmata lepingute reserv`, not *täitmatute. The old
   ending test listed only -tud/-dud/-nud, so every `-mata` attribute was
   reported as declinable.

2. One the reporter's own citation hints at without stating: -tu caritive
   adjectives DO agree, and their nominative plural also ends in -tud
   (`õnnetu` → `õnnetud`, `lugematu` → `lugematud`). An ending test cannot
   separate those from participles, so it froze them too, yielding
   *`õnnetud laste` where the correct form is `õnnetute laste`.

The fix consults Vabamorf: a frozen attributive is an adjective carrying
NO case/number form, a declining one carries `sg n` / `pl n`. The ending
list survives as a fallback, because Vabamorf sometimes misanalyses these
as nouns (`hajutatud` → S/pl n/`hajutatu`) and the ending is right there.

NOTE ON THE BENCHMARK: `scripts/eval_inflection.py` calls this function,
but inflection_et contains zero `-mata` phrases and no -tu caritive
plurals among its 200 noun phrases, so it exercises neither defect. It
cannot catch a regression here. That is what this file is for.

Run via:

    uv run python tests/test_indeclinable.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS {label}")
    else:
        failures.append(f"{label}: {detail}")
        print(f"  FAIL {label} {detail}")


def mata_forms_are_invariant() -> None:
    """Issue #42. These are everywhere in the legal/administrative register
    the editorial tools target."""
    print("-mata forms are indeclinable")
    for w in ("täitmata", "kirjutamata", "avaldamata", "allkirjastamata",
              "esitamata", "tasumata", "kooskõlastamata"):
        check(f"{w}", server._is_indeclinable_attr(w) is True)


def tu_caritives_still_agree() -> None:
    """The trap an ending test cannot see: -tu plurals also end in -tud."""
    print("-tu caritive adjectives still decline")
    for w in ("õnnetu", "lugematu", "kasutu", "abitu"):
        check(f"{w} (singular)", server._is_indeclinable_attr(w) is False)
    for w in ("õnnetud", "lugematud", "kasutud", "abitud"):
        check(f"{w} (nominative plural, ends in -tud)",
              server._is_indeclinable_attr(w) is False,
              "an ending test wrongly freezes this")


def participles_unchanged() -> None:
    """The behaviour that was already correct must stay correct."""
    print("participles remain indeclinable")
    for w in ("tuntud", "ettenähtud", "läbimõeldud", "rafineeritud",
              "surnud", "närbunud", "hajutatud",
              # Regressions an ordering-dependent lookup introduced;
              # lugupeetud is the standard Estonian salutation.
              "lugupeetud", "mahajäetud", "joobnud", "väljavenitatud"):
        check(f"{w}", server._is_indeclinable_attr(w) is True)
    # hajutatud is the documented Vabamorf misanalysis (S/pl n/hajutatu).
    # It must come out True via the ending fallback, not the adjective path.
    # Exercise the fallback directly rather than asserting on Vabamorf's
    # current tagging: a pinned-version canary turns CI red on an upstream
    # bump even when this code is still correct.
    check("noun-tagged participle still freezes (fallback path)",
          server._is_indeclinable_attr(
              "hajutatud", (("S", "pl n", "hajutatu"),)) is True)
    check("noun-tagged ordinary plural does not (same path, real lemma)",
          server._is_indeclinable_attr(
              "raamatud", (("S", "pl n", "raamat"),)) is False)


def ordinary_adjectives_and_lexical_indeclinables() -> None:
    print("everything else is unchanged")
    for w in ("suur", "ilus", "rahuldav", "kiire", "sinine"):
        check(f"{w} declines", server._is_indeclinable_attr(w) is False)
    # Explicit words, not `sorted(_INDECLINABLE_ADJ_ET)[:5]` — deriving the
    # cases from the set under test means the assertion cannot fail.
    for w in ("täis", "eri", "väärt", "alasti", "purjus"):
        check(f"lexical indeclinable {w}",
              server._is_indeclinable_attr(w) is True,
              "expected in _INDECLINABLE_ADJ_ET")


def caller_supplied_analyses() -> None:
    """analyze_morphology passes the analyses it already has, to avoid a
    second Vabamorf pass. Both paths must agree."""
    print("in-context verdict matches the isolated one")
    # Comparing _is_indeclinable_attr(w, _attr_analyses(w)) against
    # _is_indeclinable_attr(w) would be the same computation twice and
    # could not fail. analyze_morphology derives its analyses from SENTENCE
    # CONTEXT, so this is the comparison that can actually catch a
    # divergence between the two callers.
    SENTENCES = [
        ("Täitmata kohustused jäid sahtlisse.", "Täitmata"),
        ("Saatsin kirja lugupeetud kolleegile.", "lugupeetud"),
        ("Õnnetud lapsed said abi.", "Õnnetud"),
        ("Haruldased linnud lendasid üle.", "linnud"),
        ("Suur maja seisis tänaval.", "Suur"),
    ]
    for sentence, target in SENTENCES:
        toks = {t["word"]: t["indeclinable"]
                for t in server.analyze_morphology(sentence)}
        in_context = toks[target]
        isolated = server._is_indeclinable_attr(target)
        check(f"{target!r}: context={in_context} isolated={isolated}",
              in_context == isolated,
              f"the two callers disagree for {target!r}")

    # Only adjective readings with a case/number form => it declines.
    check("A/'pl n' alone declines even with a -tud ending",
          server._is_indeclinable_attr("õnnetud", (("A", "pl n", "õnnetu"),)) is False)
    check("an A/'' reading anywhere freezes it",
          server._is_indeclinable_attr(
              "tuntud", (("A", "pl n", "tuntud"), ("A", "", "tuntud"))) is True)
    # Ordering must not decide the verdict — that was the fragility of
    # taking only Vabamorf's first analysis.
    check("verdict is order-independent",
          server._is_indeclinable_attr("tuntud", (("A", "", "tuntud"), ("V", "tud", "tundma")))
          == server._is_indeclinable_attr("tuntud", (("V", "tud", "tundma"), ("A", "", "tuntud"))))
    # A lexical indeclinable wins regardless of what morphology says.
    check("lexical list beats supplied analyses",
          server._is_indeclinable_attr("täis", (("A", "sg n", "täis"),)) is True)


def abessive_nouns_are_not_frozen() -> None:
    """Estonian abessive is genitive + -ta, so a noun whose genitive ends
    in -ma produces a genuine -mata form: teema -> teemata. Those are
    inflected nouns, not the mata-form, and adding 'mata' to the ending
    list froze them.

    Only -ma stems qualify. `skeem` gives `skeemita`, not *skeemata, so
    strings like that are guesser artifacts rather than Estonian words and
    are deliberately not asserted on here."""
    print("-mata abessive nouns still decline")
    for w in ("teemata", "kliimata", "draamata"):
        check(f"{w} (genuine abessive of a -ma stem)",
              server._is_indeclinable_attr(w) is False,
              "the -mata ending must not freeze an inflected noun")
    # ...while a real mata-form that the guesser also tags abessive must
    # survive. The guard is narrowed to NOUN abessives for exactly this.
    for w in ("võltsimata", "kontrollimata"):
        check(f"{w} (real mata-form, guesser also offers an abessive)",
              server._is_indeclinable_attr(w) is True)


def ordinary_plural_nouns_are_not_frozen() -> None:
    """Any Estonian noun whose nominative plural ends -tud/-dud/-nud was
    frozen by the bare ending test: raamatud, linnud, kohtud. An agent
    following that would write *`paksude raamatud` for `paksude
    raamatute`. The lemma check separates them: `hajutatu` is deverbal,
    `raamat` is not."""
    print("ordinary plural nouns still decline")
    for w in ("raamatud", "linnud", "laenud", "kohtud", "toidud",
              "säästud", "juustud", "sõidud"):
        check(f"{w}", server._is_indeclinable_attr(w) is False,
              "an ordinary plural noun must not be frozen")
    got = {t["word"]: t["indeclinable"]
           for t in server.analyze_morphology("haruldased linnud lendasid")}
    check(f"in context: {got}", got.get("linnud") is False, str(got))


def known_limits_are_documented() -> None:
    """These stay frozen and the docstring says so. Asserted to keep the
    documented behaviour and the real behaviour in step: if a future change
    fixes them, this test fails and the docstring gets updated with it."""
    print("documented known limits")
    for w in ("töötud", "korratud", "maitsetud"):
        check(f"{w}: caritive whose lemma is indistinguishable from deverbal",
              server._is_indeclinable_attr(w) is True,
              "documented limit — lemma -tu matches both classes")
    for w in ("nõutud", "kaalutud"):
        check(f"{w}: participle/caritive homograph resolves to the participle",
              server._is_indeclinable_attr(w) is True,
              "documented limit, needs semantics not morphology")

    # Context resolves what isolation cannot. This is the honest shape of
    # the limit: the TOOL gets töötud right, the bare-word fallback does
    # not, and callers with a sentence should pass their analyses.
    in_context = {t["word"]: t["indeclinable"]
                  for t in server.analyze_morphology("töötud inimesed said abi")}
    check("context resolves töötud correctly where isolation cannot",
          in_context["töötud"] is False,
          f"{in_context} — analyze_morphology should decline this caritive")
    check("...and that is a genuine isolated/contextual divergence",
          server._is_indeclinable_attr("töötud") is True,
          "if isolation now agrees, tighten the docstring's KNOWN LIMITS")


def end_to_end_through_the_tool() -> None:
    print("analyze_morphology reports it correctly")
    for phrase, expected in (
        ("täitmata kohustused", {"täitmata": True, "kohustused": False}),
        ("õnnetud lapsed", {"õnnetud": False, "lapsed": False}),
        ("tuntud laulja", {"tuntud": True, "laulja": False}),
    ):
        got = {t["word"]: t["indeclinable"] for t in server.analyze_morphology(phrase)}
        check(f"{phrase!r} -> {got}", got == expected, f"expected {expected}")


def probe_is_cached_and_safe() -> None:
    print("_attr_analyses")
    check("returns a tuple of (pos, form) pairs",
          isinstance(server._attr_analyses("suur"), tuple))
    check("is cached", hasattr(server._attr_analyses, "cache_clear"))
    # Capitalisation must not change the verdict. The probe is looked up
    # from the lowercased word, so 'Täitmata' and 'täitmata' agree and
    # share one cache entry.
    for w in ("täitmata", "tuntud", "õnnetud", "teemata", "suur"):
        check(f"{w}: verdict is case-stable",
              server._is_indeclinable_attr(w)
              == server._is_indeclinable_attr(w.capitalize())
              == server._is_indeclinable_attr(w.upper()))
    # Junk input must degrade to the ending fallback rather than raise.
    for junk in ("", "   ", "123", "!!!", "x" * 300):
        try:
            server._is_indeclinable_attr(junk)
            check(f"{junk[:12]!r} handled", True)
        except Exception as e:
            check(f"{junk[:12]!r} handled", False, f"{type(e).__name__}: {e}")


mata_forms_are_invariant()
tu_caritives_still_agree()
participles_unchanged()
ordinary_adjectives_and_lexical_indeclinables()
caller_supplied_analyses()
abessive_nouns_are_not_frozen()
ordinary_plural_nouns_are_not_frozen()
known_limits_are_documented()
end_to_end_through_the_tool()
probe_is_cached_and_safe()

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nall indeclinable-attribute tests passed")
