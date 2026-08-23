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
              "surnud", "närbunud", "hajutatud"):
        check(f"{w}", server._is_indeclinable_attr(w) is True)
    # hajutatud is the documented Vabamorf misanalysis (S/pl n/hajutatu).
    # It must come out True via the ending fallback, not the adjective path.
    analyses = server._attr_analyses("hajutatud")
    check("hajutatud is still misanalysed as a noun (fallback is exercised)",
          not any(p == "A" for p, _f in analyses),
          f"{analyses} — if an 'A' reading appears, the fallback is untested here")


def ordinary_adjectives_and_lexical_indeclinables() -> None:
    print("everything else is unchanged")
    for w in ("suur", "ilus", "rahuldav", "kiire", "sinine"):
        check(f"{w} declines", server._is_indeclinable_attr(w) is False)
    for w in sorted(server._INDECLINABLE_ADJ_ET)[:5]:
        check(f"lexical indeclinable {w}", server._is_indeclinable_attr(w) is True)


def caller_supplied_analyses() -> None:
    """analyze_morphology passes the analyses it already has, to avoid a
    second Vabamorf pass. Both paths must agree."""
    print("caller-supplied analyses match the looked-up answer")
    for w in ("täitmata", "tuntud", "õnnetud", "suur", "teemata"):
        supplied = server._is_indeclinable_attr(w, server._attr_analyses(w))
        looked_up = server._is_indeclinable_attr(w)
        check(f"{w}: supplied == looked up ({supplied})", supplied == looked_up,
              f"supplied={supplied} looked_up={looked_up}")

    # Only adjective readings with a case/number form => it declines.
    check("A/'pl n' alone declines even with a -tud ending",
          server._is_indeclinable_attr("õnnetud", (("A", "pl n"),)) is False)
    check("an A/'' reading anywhere freezes it",
          server._is_indeclinable_attr("tuntud", (("A", "pl n"), ("A", ""))) is True)
    # Ordering must not decide the verdict — that was the fragility of
    # taking only Vabamorf's first analysis.
    check("verdict is order-independent",
          server._is_indeclinable_attr("tuntud", (("A", ""), ("V", "tud"))) ==
          server._is_indeclinable_attr("tuntud", (("V", "tud"), ("A", ""))))
    # A lexical indeclinable wins regardless of what morphology says.
    check("lexical list beats supplied analyses",
          server._is_indeclinable_attr("täis", (("A", "sg n"),)) is True)


def abessive_nouns_are_not_frozen() -> None:
    """A noun whose stem ends in -ma forms its abessive in -mata
    (teema -> teemata). Adding 'mata' to the ending list froze those; the
    abessive guard is what stops it."""
    print("-mata abessive nouns still decline")
    for w in ("teemata", "kliimata", "draamata", "skeemata", "reklaamata"):
        check(f"{w} (abessive of a -ma stem)",
              server._is_indeclinable_attr(w) is False,
              "the -mata ending must not freeze an inflected noun")


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
end_to_end_through_the_tool()
probe_is_cached_and_safe()

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nall indeclinable-attribute tests passed")
