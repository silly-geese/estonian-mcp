"""Unit tests for the compound-attestation verdict.

`_familiarity_verdict` is the pure decision behind check_compound_familiarity.
Splitting it out means the heuristic can be tested WITHOUT loading the 33 MB
fastText model: we feed it real nearest-neighbour data captured from the
production model and assert the verdict.

Every fixture below is verbatim output from the deployed fastText-et-medium
model (queried via the production /mcp endpoint), so these tests pin the
heuristic against the model's actual behaviour, not a guess. The tool returns
eight neighbours and shows five; the fixtures carry the five, which changes
the reported `scrape_junk` count but not a single verdict. The junk tail has
no vote; only the top neighbour and the score do.

THE CALIBRATION SET is the point of this file. 25 compounds, every one of
them real production data:

  - The attested-but-out-of-vocabulary band, 0.536 to 0.670. Ordinary
    Estonian, none of it in a 100K-word vocabulary, NONE of it may flag.
  - Coinages. The two that sit below the gate must flag; the ones that land
    inside the attested band are documented misses, not bugs.

The bands overlap: attested `pilveteenus` and coined `mõtteliin` both score
0.536. That overlap is why the gate sits under the attested floor and why
recall is deliberately low. See the comment on _FAMILIARITY_SUSPECT_SCORE.

Run via:

    uv run python tests/test_familiarity.py
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


# (word, in_vocab, top_score, parts, neighbours[(word, score)],
#  attested_elsewhere, expect_suspect)
# Captured from production fastText-et-medium.
ATTESTED = [
    # ------------------------------------------------------------------
    # The two words that started this. Both ordinary business Estonian,
    # both reported to a client as invented compounds.
    # ------------------------------------------------------------------
    # lisakäive: every neighbour is a scrape-artifact token and the scores
    # are a flat plateau (0.670 / 0.669 / 0.665 / 0.660 / 0.658), the
    # shape of a fastText hub seen from the inside. The old rule read the
    # junk top neighbour as decisive evidence of invention.
    ("lisakäive", False, 0.670, ["lisa", "käive"], [
        ("kihtFliisidPluusidPüksid", 0.67), ("pluusidPolosärgidPüksid", 0.669),
        ("särgidT-särgidKampsunidDressipluusidTeksasedPüksid", 0.665),
        ("ajakiriHead", 0.66), ("loodTänane", 0.658),
    ], False, False),
    # klikkimismäär: clean, coherent neighbourhood, both morphemes
    # represented, and it was flagged purely for scoring 0.586 under a
    # 0.60 gate.
    ("klikkimismäär", False, 0.586, ["klikkimis", "määr"], [
        ("klikkides", 0.586), ("klikkida", 0.529), ("kliki", 0.526),
        ("määr", 0.515), ("klikki", 0.513),
    ], False, False),
    # ------------------------------------------------------------------
    # The rest of the attested band, in score order. Everything here is
    # unremarkable Estonian and every one of them flagged under the old
    # 0.60 gate or the old junk-tail gate.
    # ------------------------------------------------------------------
    # pilveteenus: the attested floor, 0.536, the SAME score as the
    # canonical coinage mõtteliin. Nothing in the neighbourhood separates
    # them; Estonian WordNet does, which is why it is consulted.
    ("pilveteenus", False, 0.536, ["pilve", "teenus"], [
        ("makseteenuse", 0.536), ("teenus", 0.536),
        ("mobiilirakendus", 0.526), ("Eraklient", 0.522),
        ("püsikliendiprogramm", 0.522),
    ], True, False),
    ("hinnakujundus", False, 0.563, ["hinna", "kujundus"], [
        ("müügihind", 0.563), ("kujundus", 0.559),
        ("ÜhisgümnaasiumKudumidPolosärgidPüksid", 0.546),
        ("turuhind", 0.537), ("logogaPüksid", 0.527),
    ], False, False),
    ("kliendibaas", False, 0.566, ["kliendi", "baas"], [
        ("klienditugi", 0.566), ("püsikliendiprogramm", 0.55),
        ("ettevõte", 0.546), ("kliendid", 0.525), ("klientide", 0.523),
    ], False, False),
    ("katusekorter", False, 0.577, ["katuse", "korter"], [
        ("maja", 0.577), ("korter", 0.569), ("külaliskorter", 0.565),
        ("katus", 0.548), ("esik", 0.522),
    ], True, False),
    ("konversioonimäär", False, 0.589, ["konversiooni", "määr"], [
        ("inversioon", 0.589), ("versioon", 0.49), ("MuISi", 0.468),
        ("tootlikkus", 0.467), ("alammäär", 0.464),
    ], False, False),
    ("koolitoit", False, 0.590, ["kooli", "toit"], [
        ("toit", 0.59), ("lennukitoit", 0.583), ("kiirtoit", 0.546),
        ("koolipäev", 0.542), ("kooli", 0.54),
    ], False, False),
    ("majapidamistarve", False, 0.593, ["maja", "pidamis", "tarve"], [
        ("majapidamine", 0.593), ("majapidamist", 0.551),
        ("majapidamiste", 0.55), ("majapidamistes", 0.534),
        ("majapidamise", 0.531),
    ], False, False),
    ("rehvivahetus", False, 0.596, ["rehvi", "vahetus"], [
        ("rehvid", 0.596), ("suverehvid", 0.59), ("rehvide", 0.578),
        ("talverehvid", 0.559), ("rehve", 0.551),
    ], False, False),
    ("tervisetõend", False, 0.598, ["tervise", "tõend"], [
        ("tervisekontrolli", 0.598), ("terviseedendus", 0.589),
        ("terviseteenused", 0.581), ("tervisedendus", 0.574),
        ("saatekiri", 0.566),
    ], False, False),
    # müügilehter: 0.599. One thousandth under the old gate.
    ("müügilehter", False, 0.599, ["müügi", "lehter"], [
        ("müügile", 0.599), ("ÖKOtooded", 0.545),
        ("lehvidSpordiriidedTeklidPõlvikud", 0.541), ("müügil", 0.536),
        ("seelikAksessuaaridTeklidSpordiriidedPõlvikud", 0.534),
    ], False, False),
    # otsingureklaam: the lisakäive failure mode again, junk top neighbour
    # at 0.670, with a real word (`reklaam`, 0.655) sitting right behind it.
    ("otsingureklaam", False, 0.670, ["otsingu", "reklaam"], [
        ("otsingVaruosaotsingTagasisideRehvide", 0.67), ("reklaam", 0.655),
        ("otsingVaruosaotsingRehvide", 0.654), ("otsingMuuseum", 0.641),
        ("0383Onlinereklaam", 0.631),
    ], False, False),
    # The false-positive trap from the original fixture set: real, rare,
    # OOV, and its neighbours all share the head morpheme, so a naive
    # echo rule would flag it.
    ("tervisekindlustus", False, 0.71, ["tervise", "kindlustus"], [
        ("ravikindlustus", 0.71), ("elukindlustus", 0.68),
        ("kindlustus", 0.66), ("ravikindlustuse", 0.65),
        ("töötuskindlustuse", 0.64),
    ], False, False),
    ("allalaadimisnupp", False, 0.66, ["allalaadimis", "nupp"], [
        ("allalaadimise", 0.66), ("allalaadimiseks", 0.65),
        ("allalaadimine", 0.64), ("allalaaditav", 0.63),
        ("PortaalUudisedHaridusAjaluguKeskkond", 0.62),
    ], False, False),
    # In-vocab: attested by the corpus itself, never suspect even at a
    # modest score.
    ("mõttekäik", True, 0.563, ["mõtte", "käik"], [
        ("tõdemus", 0.563), ("mõte", 0.55), ("üldistus", 0.54),
        ("kontekst", 0.53), ("arutlus", 0.52),
    ], False, False),
    ("raudteejaam", True, 0.767, ["raudtee", "jaam"], [
        ("Raudteejaam", 0.767), ("raudteejaama", 0.75),
        ("raudteejaa", 0.74), ("raudteejaamast", 0.73),
        ("raudteejaamas", 0.72),
    ], False, False),
]

COINAGES = [
    # CAUGHT: top neighbour is a real word (kaitseliin) and the score is
    # still under the gate. The canonical target: literal English "train
    # of thought"; real Estonian is mõttekäik.
    ("mõtteliin", False, 0.536, ["mõtte", "liin"], [
        ("kaitseliin", 0.536),
        ("KoolKudumidPolosärgidTriiksärgid", 0.5),
        ("GümnaasiumKudumidPolosärgidTriiksärgid", 0.49),
        ("HumanitaargümnaasiumKudumidPolosärgidTriiksärgid", 0.48),
        ("PõhikoolKudumidPolosärgidTriiksärgid", 0.47),
    ], False, True),
    # CAUGHT: 0.547, clean neighbourhood, and not one neighbour has
    # anything to do with `janu`.
    ("andmejanu", False, 0.547, ["andme", "janu"], [
        ("andmevahetuse", 0.547), ("andmemahu", 0.531), ("andmeid", 0.525),
        ("andmetöötluse", 0.521), ("andmesubjekti", 0.513),
    ], False, True),
    # ------------------------------------------------------------------
    # DOCUMENTED MISSES. Each one scores inside the attested band above.
    # Flagging them means flagging ordinary Estonian, so they pass.
    # ------------------------------------------------------------------
    # toortõlkeoht, 0.571: the coinage the gate was raised to 0.60 to
    # catch. It sits eight thousandths above attested `hinnakujundus`
    # (0.563) and fifteen below attested `koolitoit` (0.590). No
    # threshold holds one and releases the other.
    ("toortõlkeoht", False, 0.571, ["toor", "tõlke", "oht"], [
        ("masintõlke", 0.571), ("toormaterjali", 0.556), ("tõlke", 0.523),
        ("kihtFliisidPluusidPüksid", 0.52), ("toorainet", 0.517),
    ], False, False),
    # sõnumivedur, 0.593: a neighbourhood indistinguishable from attested
    # klikkimismäär's: head present, sibling compound present, one junk
    # token.
    ("sõnumivedur", False, 0.593, ["sõnumi", "vedur"], [
        ("vedur", 0.593), ("sõnumit", 0.586), ("sõnum", 0.574),
        ("auruvedur", 0.548), ("sõnumeid", 0.527),
    ], False, False),
    # klõpsusild, 0.656 with a junk top neighbour: the lisakäive shape
    # exactly. Real and coined words are the same picture here, so the
    # verdict is "no signal" for both.
    ("klõpsusild", False, 0.656, ["klõpsu", "sild"], [
        ("pluusidPolosärgidPüksid", 0.656),
        ("ÜhisgümnaasiumKudumidPolosärgidPüksid", 0.655),
        ("TallinnOtseEsilehtMuusikaPoodUudisedHaridusAjalugu", 0.648),
        ("option.heading", 0.646), ("KeskkoolKudumidPolosärgidPluusid", 0.644),
    ], False, False),
    # kliendivalu, 0.559 but behind a junk top neighbour: no signal, not
    # a verdict.
    ("kliendivalu", False, 0.559, ["kliendi", "valu"], [
        ("esitamineKauba", 0.559), ("loodTänane", 0.558),
        ("lehvidSukkpüksidPolosärgidTeklidSpordiriidedVanalinna", 0.554),
        ("100SetoMaa", 0.553),
        ("särgidT-särgidKampsunidDressipluusidTeksasedPüksid", 0.549),
    ], False, False),
]


def verdict_cases() -> None:
    for label, cases in (("attested", ATTESTED), ("coinages", COINAGES)):
        print(f"familiarity verdict, {label} (captured production data)")
        for word, in_vocab, top, parts, nbrs, elsewhere, expect in cases:
            is_suspect, reasons, quality = server._familiarity_verdict(
                in_vocab, top, nbrs, parts, attested_elsewhere=elsewhere
            )
            check(f"{word}: is_suspect == {expect}", is_suspect == expect,
                  f"got {is_suspect}, reasons={reasons}")
            if is_suspect:
                check(f"{word}: suspect has reasons", len(reasons) > 0,
                      str(reasons))
            else:
                check(f"{word}: not-suspect has no reasons", reasons == [],
                      str(reasons))
            check(f"{word}: quality counts present",
                  quality["neighbours"] == len(nbrs)
                  and "scrape_junk" in quality
                  and "subword_echoes" in quality
                  and quality["signal"] in ("none", "usable"),
                  str(quality))


def no_attested_word_flags() -> None:
    """The property the whole calibration set exists to defend: not one
    attested compound may be reported as suspect."""
    print("no attested compound flags")
    flagged = [
        word for word, in_vocab, top, parts, nbrs, elsewhere, _ in ATTESTED
        if server._familiarity_verdict(
            in_vocab, top, nbrs, parts, attested_elsewhere=elsewhere)[0]
    ]
    check("zero false positives over the attested band", flagged == [],
          str(flagged))


def gate_sits_under_the_attested_floor() -> None:
    """The gate is only defensible while it stays below every attested
    compound the model can score. If a future fixture lands under it, this
    fails and the gate, not the fixture, has to move."""
    print("gate calibration")
    scorable = [
        (word, top) for word, in_vocab, top, parts, nbrs, elsewhere, _
        in ATTESTED
        if not in_vocab and not elsewhere
        and nbrs and not server._looks_like_scrape_junk(nbrs[0][0])
    ]
    floor = min(top for _, top in scorable)
    lowest = min(scorable, key=lambda p: p[1])[0]
    check(f"attested floor ({lowest}, {floor}) is above the gate "
          f"({server._FAMILIARITY_SUSPECT_SCORE})",
          floor >= server._FAMILIARITY_SUSPECT_SCORE,
          f"{lowest} at {floor} would be flagged")


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
    # lisakäive: junk top neighbour → signal "none", no verdict, whatever
    # the score says.
    _, reasons, q = server._familiarity_verdict(
        False, 0.670, ATTESTED[0][4], ATTESTED[0][3])
    check("lisakäive: signal is none", q["signal"] == "none", str(q))
    check("lisakäive: no reasons offered", reasons == [], str(reasons))
    # mõtteliin: real top neighbour, so the score counts and it flags,
    # even though most of its tail is junk too.
    is_suspect, reasons, q = server._familiarity_verdict(
        False, 0.536, COINAGES[0][4], COINAGES[0][3])
    check("mõtteliin: signal is usable", q["signal"] == "usable", str(q))
    check("mõtteliin: junk tail counted anyway", q["scrape_junk"] >= 2, str(q))
    check("mõtteliin: flagged with a reason",
          is_suspect is True and len(reasons) == 1, str(reasons))
    # tervisekindlustus: every neighbour echoes the head morpheme. Echoes
    # are counted and never vote.
    is_suspect, _, q = server._familiarity_verdict(
        False, 0.71, ATTESTED[13][4], ATTESTED[13][3])
    check("tervisekindlustus: zero junk", q["scrape_junk"] == 0, str(q))
    check("tervisekindlustus: echoes present but not flagged",
          q["subword_echoes"] >= 1 and is_suspect is False, str(q))
    # A lemma WordNet or the legal list vouches for is attested, and the
    # score never gets a say.
    is_suspect, reasons, _ = server._familiarity_verdict(
        False, 0.30, [("teenus", 0.30)], ["pilve", "teenus"],
        attested_elsewhere=True)
    check("attested elsewhere beats any score",
          is_suspect is False and reasons == [], str(reasons))
    # No neighbours at all is no evidence, not evidence against.
    is_suspect, _, q = server._familiarity_verdict(False, 0.0, [], ["a", "b"])
    check("empty neighbourhood is no signal, not suspect",
          is_suspect is False and q["signal"] == "none", str(q))


verdict_cases()
no_attested_word_flags()
gate_sits_under_the_attested_floor()
junk_detector()
specific_signals()

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nall familiarity verdict tests passed")
