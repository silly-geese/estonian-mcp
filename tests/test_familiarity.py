"""Unit tests for the compound-familiarity verdict.

`_familiarity_verdict` is the pure decision behind check_compound_familiarity.
Splitting it out means the coinage heuristic can be tested WITHOUT loading
the 33 MB fastText model — we feed it real nearest-neighbour data captured
from the production model and assert the verdict.

The fixtures below are verbatim output from the deployed fastText-et-medium
model (queried via the production /mcp endpoint), so these tests pin the
heuristic against the model's actual behaviour, not a guess.

Run via:

    uv run python tests/test_familiarity.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS {label}")
    else:
        failures.append(f"{label}: {detail}")
        print(f"  FAIL {label} {detail}")


# (word, in_vocab, top_score, parts, neighbours[(word, score)], expect_suspect)
# Captured from production fastText-et-medium.
CASES = [
    # COINAGE that the old 0.55 gate MISSED (top 0.571) — the bug that
    # motivated this change. OOV, neighbours are subword echoes + a junk
    # token. Must flag.
    ("toortõlkeoht", False, 0.571, ["toor", "tõlke", "oht"], [
        ("masintõlke", 0.571), ("toormaterjali", 0.556), ("tõlke", 0.523),
        ("kihtFliisidPluusidPüksid", 0.52), ("toorainet", 0.517),
    ], True),
    # CALQUE — weak score AND mostly scrape-junk neighbours. Must flag.
    ("mõtteliin", False, 0.536, ["mõtte", "liin"], [
        ("kaitseliin", 0.536),
        ("KoolKudumidPolosärgidTriiksärgid", 0.5),
        ("GümnaasiumKudumidPolosärgidTriiksärgid", 0.49),
        ("HumanitaargümnaasiumKudumidPolosärgidTriiksärgid", 0.48),
        ("PõhikoolKudumidPolosärgidTriiksärgid", 0.47),
    ], True),
    # REAL word, in-vocab — never suspect even at a modest 0.563 score.
    ("mõttekäik", True, 0.563, ["mõtte", "käik"], [
        ("tõdemus", 0.563), ("mõte", 0.55), ("üldistus", 0.54),
        ("kontekst", 0.53), ("arutlus", 0.52),
    ], False),
    # REAL but RARE compound, OOV — high score + clean sibling-compound
    # neighbours. Must NOT flag (the false-positive trap: its neighbours
    # all share the head morpheme 'kindlustus', so a naive echo rule would
    # wrongly flag it).
    ("tervisekindlustus", False, 0.71, ["tervise", "kindlustus"], [
        ("ravikindlustus", 0.71), ("elukindlustus", 0.68),
        ("kindlustus", 0.66), ("ravikindlustuse", 0.65),
        ("töötuskindlustuse", 0.64),
    ], False),
    # REAL, in-vocab — neighbours are its own inflections. Not suspect.
    ("raudteejaam", True, 0.767, ["raudtee", "jaam"], [
        ("Raudteejaam", 0.767), ("raudteejaama", 0.75),
        ("raudteejaa", 0.74), ("raudteejaamast", 0.73),
        ("raudteejaamas", 0.72),
    ], False),
    # REAL-ish UI compound, OOV but score 0.66 (>= 0.60) and only one junk
    # neighbour (< 40%). Not flagged — correct, and it sits comfortably
    # above the gate.
    ("allalaadimisnupp", False, 0.66, ["allalaadimis", "nupp"], [
        ("allalaadimise", 0.66), ("allalaadimiseks", 0.65),
        ("allalaadimine", 0.64), ("allalaaditav", 0.63),
        ("PortaalUudisedHaridusAjaluguKeskkond", 0.62),
    ], False),
]


def verdict_cases() -> None:
    print("familiarity verdict (captured production fastText data)")
    for word, in_vocab, top, parts, nbrs, expect in CASES:
        is_suspect, reasons, quality = server._familiarity_verdict(
            in_vocab, top, nbrs, parts
        )
        check(f"{word}: is_suspect == {expect}", is_suspect == expect,
              f"got {is_suspect}, reasons={reasons}")
        if is_suspect:
            check(f"{word}: suspect has reasons", len(reasons) > 0, str(reasons))
        else:
            check(f"{word}: not-suspect has no reasons", reasons == [], str(reasons))
        check(f"{word}: quality counts present",
              quality["neighbours"] == len(nbrs)
              and "scrape_junk" in quality and "subword_echoes" in quality,
              str(quality))


def junk_detector() -> None:
    print("scrape-junk detector")
    check("camelCase token is junk",
          server._looks_like_scrape_junk("KoolKudumidPolosärgid") is True)
    check("internal-capital token is junk",
          server._looks_like_scrape_junk("kihtFliisidPüksid") is True)
    check("normal lowercase word is not junk",
          server._looks_like_scrape_junk("ravikindlustus") is False)
    check("leading-capital proper noun is not junk",
          server._looks_like_scrape_junk("Raudteejaam") is False)


def specific_signals() -> None:
    print("specific signal checks")
    # mõtteliin: 4/5 neighbours are scrape junk → junk-ratio gate fires.
    _, reasons, q = server._familiarity_verdict(
        False, 0.536, CASES[1][4], CASES[1][3])
    check("mõtteliin: junk counted (>=2)", q["scrape_junk"] >= 2, str(q))
    check("mõtteliin: a junk-neighbour reason exists",
          any("scrape-artifact" in r for r in reasons), str(reasons))
    # tervisekindlustus: real sibling compounds → 0 junk, echoes counted
    # but MUST NOT flag (proves echoes don't trigger).
    is_suspect, _, q = server._familiarity_verdict(
        False, 0.71, CASES[3][4], CASES[3][3])
    check("tervisekindlustus: zero junk", q["scrape_junk"] == 0, str(q))
    check("tervisekindlustus: echoes present but not flagged",
          q["subword_echoes"] >= 1 and is_suspect is False, str(q))


verdict_cases()
junk_detector()
specific_signals()

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nall familiarity verdict tests passed")
