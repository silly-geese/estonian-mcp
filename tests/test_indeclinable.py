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
    pos, _form = server._attr_morphology("hajutatud")
    check("hajutatud is still misanalysed as a noun (fallback is exercised)",
          pos != "A", f"pos={pos!r} — if this is now 'A', the fallback is untested here")


def ordinary_adjectives_and_lexical_indeclinables() -> None:
    print("everything else is unchanged")
    for w in ("suur", "ilus", "rahuldav", "kiire", "sinine"):
        check(f"{w} declines", server._is_indeclinable_attr(w) is False)
    for w in sorted(server._INDECLINABLE_ADJ_ET)[:5]:
        check(f"lexical indeclinable {w}", server._is_indeclinable_attr(w) is True)


def caller_supplied_morphology() -> None:
    """analyze_morphology passes the analysis it already has, to avoid a
    second Vabamorf pass. Both paths must agree."""
    print("caller-supplied pos/form matches the looked-up answer")
    for w in ("täitmata", "tuntud", "õnnetud", "suur"):
        pos, form = server._attr_morphology(w)
        supplied = server._is_indeclinable_attr(w, pos, form)
        looked_up = server._is_indeclinable_attr(w)
        check(f"{w}: supplied == looked up ({supplied})", supplied == looked_up,
              f"supplied={supplied} looked_up={looked_up}")

    # An adjective carrying a case/number form declines, whatever it ends in.
    check("A + 'pl n' declines even with a -tud ending",
          server._is_indeclinable_attr("õnnetud", "A", "pl n") is False)
    check("A + empty form is frozen",
          server._is_indeclinable_attr("tuntud", "A", "") is True)
    # A lexical indeclinable wins regardless of what morphology says.
    check("lexical list beats a supplied form",
          server._is_indeclinable_attr("täis", "A", "sg n") is True)


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
    print("_attr_morphology")
    check("returns a (pos, form) tuple",
          isinstance(server._attr_morphology("suur"), tuple))
    check("is cached", hasattr(server._attr_morphology, "cache_clear"))
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
caller_supplied_morphology()
end_to_end_through_the_tool()
probe_is_cached_and_safe()

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nall indeclinable-attribute tests passed")
