"""Tests for the editorial tools: impersonal-voice counting,
check_officialese, and check_term_consistency.

These pin the behaviour against a real Estonian R&D report paragraph and
the plain-language rewrite a native speaker produced from it — the
bureaucratic original must flag and the human rewrite must come back
clean. That pair is also what the thresholds in server.py are calibrated
on, so a threshold change that breaks the separation fails here.

Runs without fastText; the WordNet rule in check_term_consistency
degrades to off when the resource is missing (asserted via `rules_run`
rather than assumed).

Run via:

    uv run python tests/test_officialese.py
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


# The bureaucratic original (an EMTA-facing R&D report) and the rewrite a
# native Estonian speaker produced from it.
ORIG_P1 = (
    "Aruandeperioodil koguti, annoteeriti, valideeriti ja avaldati "
    "ettevõttesiseselt teadusandmestik, mis koosneb 27 137 "
    "kinnisvarakuulutuse pildist. Kogu korpus märgendati ühe läbimisega "
    "Claude'i mudeliga sonnet-4-6 pärast metoodika väljatöötamist, mille "
    "käigus võrreldi mitme teenusepakkuja multimodaalseid mudeleid ning "
    "kasutati nende lahknevusi taksonoomia ja viiba täiustamiseks."
)

ORIG_P2 = (
    "Märgendid on mudeli loodud ja inimesed ei kontrollinud neid "
    "ükshaaval, mistõttu mõõdavad kõik täpsusnäitajad kooskõla "
    "annoteerimisprotsessiga, mitte inimeste antud etalonmärgenditega. "
    "Lukustatud hindamisosas lahendasid projektimeeskonna liikmed "
    "pimehindamise korras 179 lahknevust mudeli ennustuse ja märgendi "
    "vahel, mis pärinesid kahe märgendusvaldkonna kõige vaieldavamatest "
    "klastritest; vastusevõtmeid hindajatele ei avaldatud ning hindamist "
    "täiendas eraldi 60 pildi sisuaudit. Selles sihilikult keerulises "
    "valimis oli korpuse märgend 32% juhtudest vale ja veel 25% juhtudest "
    "tegelikult mitmeti tõlgendatav; kogu korpust hõlmava veamäära kohta "
    "väidet ei esitata. Märgendite määratlusi muudeti ning käimas on "
    "uuesti annoteerimine."
)

HUMAN_REWRITE = (
    "Märgendid lõi mudel ise. Inimesed ei kontrollinud neid ükshaaval. "
    "Seetõttu ei näita täpsusnäitajad, kui õiged märgendid tegelikult on, "
    "vaid ainult seda, kui hästi need annoteerimisprotsessiga kokku "
    "lähevad. Selle kontrollimiseks valiti ette kindlaks eraldi valim: "
    "kahe märgendusvaldkonna kõige vaieldavamad rühmad. Selles valimis "
    "lahendasid projektimeeskonna liikmed pimehindamise korras 179 "
    "juhtumit, kus mudeli ennustus ja märgend erinesid. Hindajad ei "
    "näinud õigeid vastuseid ette. Lisaks vaadati eraldi üle 60 pildi "
    "sisu. Sellest teadlikult raskest valimist osutus 32% märgenditest "
    "valeks ja veel 25% tegelikult mitut moodi tõlgendatavaks. Märgendite "
    "määratlusi täpsustati ja andmestikku annoteeritakse praegu uuesti."
)


def _imp(text: str) -> dict:
    Text = server._Text()
    t = Text(text)
    t.tag_layer(["morph_analysis"])
    return server._impersonal_voice(list(t.morph_analysis))


def impersonal_voice_corrections() -> None:
    """The four counting bugs, each pinned by the construction that
    exposed it on real text."""
    print("impersonal voice — counting corrections")

    # (1) `ei` is tagged pos=V form=neg; counting it inflated total_verbs
    # and deflated the ratio.
    r = _imp("Inimesed ei kontrollinud neid.")
    check("`ei` not counted as a verb", r["total_verbs"] == 1,
          f"total_verbs={r['total_verbs']}")

    # (2) `ta` form after a negation is the impersonal present negative.
    r = _imp("Veamäära kohta väidet ei esitata.")
    check("`ei esitata` counted as impersonal", r["passive_count"] == 1,
          str(r))
    # …but a bare da-infinitive is NOT impersonal.
    r = _imp("Meeskond otsustas esitada aruande.")
    check("bare da-infinitive not counted", r["passive_count"] == 0, str(r))

    # (3) `ei avaldatud` gets tagged pos=A, not V — was skipped entirely.
    r = _imp("Vastusevõtmeid hindajatele ei avaldatud.")
    check("`ei avaldatud` counted despite pos=A tag",
          r["passive_count"] == 1, str(r))

    # (4) attributive -tud participle is a modifier, not a predicate.
    r = _imp("Lukustatud hindamisosas lahendasid liikmed juhtumeid.")
    check("attributive -tud not counted as impersonal",
          r["passive_count"] == 0, str(r))
    check("attributive -tud reported separately",
          "Lukustatud" in r["attributive_excluded"], str(r))
    # …but with an olema-form in front it IS an impersonal predicate.
    r = _imp("Andmeid on kasutatud mitmel korral.")
    check("`on kasutatud` counted as impersonal", r["passive_count"] == 1,
          str(r))

    # `mitte` negates a noun phrase, not a verb — must not trigger.
    r = _imp("Mõõdeti kooskõla, mitte inimeste antud märgenditega.")
    check("`mitte X antud` not counted as impersonal predicate",
          "antud" not in r["examples"], str(r))

    # Core case: the report paragraph is overwhelmingly impersonal.
    r = _imp(ORIG_P1)
    check("bureaucratic paragraph reads as heavily impersonal",
          r["ratio"] >= 0.8, str(r))


def officialese_separation() -> None:
    """The gates must separate the bureaucratic original from the human
    rewrite — that separation IS the calibration."""
    print("check_officialese — original vs human rewrite")

    o1 = server.check_officialese(ORIG_P1)
    o2 = server.check_officialese(ORIG_P2)
    human = server.check_officialese(HUMAN_REWRITE)

    check("original P1 flags issues", len(o1["issues"]) > 0,
          str(o1["summary_estonian"]))
    check("original P2 flags issues", len(o2["issues"]) > 0,
          str(o2["summary_estonian"]))
    check("human rewrite comes back clean", len(human["issues"]) == 0,
          str([i["rule"] for i in human["issues"]]))

    rules1 = {i["rule"] for i in o1["issues"]}
    check("P1 flags impersonal voice", "impersonal-voice" in rules1, str(rules1))
    check("P1 flags nominalisation", "nominalisation" in rules1, str(rules1))
    check("P1 flags a long sentence", "long-sentence" in rules1, str(rules1))

    # Metrics move in the direction the human edit moved them.
    check("nominalisation density drops across the rewrite",
          human["metrics"]["nominalisations_per_100_words"]
          < o1["metrics"]["nominalisations_per_100_words"],
          f"{o1['metrics']['nominalisations_per_100_words']} → "
          f"{human['metrics']['nominalisations_per_100_words']}")
    check("impersonal ratio drops across the rewrite",
          human["metrics"]["impersonal_voice"]["ratio"]
          < o1["metrics"]["impersonal_voice"]["ratio"],
          f"{o1['metrics']['impersonal_voice']['ratio']} → "
          f"{human['metrics']['impersonal_voice']['ratio']}")

    # Every issue carries an Estonian label (no English-only output).
    for label, res in (("P1", o1), ("P2", o2)):
        check(f"{label}: every issue has rule_estonian",
              all(i.get("rule_estonian") for i in res["issues"]),
              str([i.get("rule_estonian") for i in res["issues"]]))
        check(f"{label}: every issue has a suggestion",
              all(i.get("suggestion") for i in res["issues"]))

    # -mine nouns come back with the verb to swap in.
    noms = {n["lemma"]: n["verb"] for n in o1["metrics"]["nominalisations"]}
    check("nominalisation carries its verb (väljatöötamine → väljatöötama)",
          noms.get("väljatöötamine") == "väljatöötama", str(noms))


def officialese_lexicons() -> None:
    print("check_officialese — filler, phrases, poolt-calque, clause stacking")
    probe = (
        "Projekti raames viidi läbi analüüs, mis kujutab endast olulist "
        "etappi. Mudeli poolt loodud märgendid omavad tähtsust. "
        "Analüüs teostati lähtuvalt metoodikast."
    )
    r = server.check_officialese(probe)
    rules = {i["rule"] for i in r["issues"]}
    sugg = {(i.get("word") or i.get("phrase", "")).lower(): i["suggestion"]
            for i in r["issues"]}

    check("flags 'viidi läbi'", "viidi läbi" in sugg, str(sorted(sugg)))
    check("flags 'kujutab endast'", "kujutab endast" in sugg, str(sorted(sugg)))
    check("flags the poolt-calque", "poolt-calque" in rules, str(rules))
    check("flags 'omama'", any(k.startswith("oma") for k in sugg), str(sorted(sugg)))
    check("flags 'teostama'", any(k.startswith("teosta") for k in sugg),
          str(sorted(sugg)))

    # Surface-matched filler: the inflected form is the marker, while the
    # lemma (eesmärk / vahendus) is an ordinary word that must stay quiet.
    r = server.check_officialese(
        "Analüüsi eesmärgil kasutati andmeid. Süsteemi vahendusel saadeti teade."
    )
    words = {(i.get("word") or "").lower() for i in r["issues"]}
    check("flags 'eesmärgil'", "eesmärgil" in words, str(sorted(words)))
    check("flags 'vahendusel'", "vahendusel" in words, str(sorted(words)))
    r = server.check_officialese("Meie eesmärk on selge ja vahendus toimis hästi.")
    check("plain 'eesmärk' / 'vahendus' not flagged",
          not [i for i in r["issues"] if i["rule"] == "officialese-filler"],
          str([i.get("word") for i in r["issues"]]))

    # 'hääletas poolt' is "in favour", not the passive-agent calque.
    r = server.check_officialese("Enamik liikmeid hääletas poolt ja ettepanek kinnitati.")
    check("'hääletas poolt' not flagged as a calque",
          "poolt-calque" not in {i["rule"] for i in r["issues"]},
          str([i["rule"] for i in r["issues"]]))

    # Clause stacking beats raw length as the reason for the flag.
    stacked = (
        "Töötati välja metoodika, mille käigus võrreldi mudeleid, mis "
        "erinesid taksonoomia poolest, kuna varasem lahendus ei sobinud."
    )
    r = server.check_officialese(stacked)
    check("clause stacking flagged",
          "clause-stacking" in {i["rule"] for i in r["issues"]},
          str([i["rule"] for i in r["issues"]]))

    # Plain, short Estonian must stay silent.
    r = server.check_officialese("Eile käisin kinos. Film oli hea ja näitlejad mängisid hästi.")
    check("plain prose flags nothing", len(r["issues"]) == 0,
          str([i["rule"] for i in r["issues"]]))


def term_consistency() -> None:
    print("check_term_consistency")
    doc = (
        "Aruandeperioodil avaldati teadusandmestik. Pildiandmestik koosneb "
        "27137 pildist. Andmestik on ettevõttesisene. Treeningandmestik "
        "valmis mais."
    )
    r = server.check_term_consistency(doc)
    check("rules_run reports the head rule ran",
          r["rules_run"]["shared-compound-head"] is True, str(r["rules_run"]))

    head_groups = [g for g in r["groups"] if g["rule"] == "shared-compound-head"]
    check("the andmestik variants are grouped", len(head_groups) >= 1,
          str(r["groups"]))
    if head_groups:
        g = head_groups[0]
        lemmas = {v["lemma"] for v in g["variants"]}
        check("group holds all four variants",
              {"andmestik", "teadusandmestik", "pildiandmestik",
               "treeningandmestik"} <= lemmas, str(lemmas))
        check("group has an Estonian rule label",
              g.get("rule_estonian") == "sama põhisõna", str(g.get("rule_estonian")))
        check("group names a dominant term", bool(g.get("dominant")), str(g))

    # Two compounds sharing a head, with no bare head, is deliberately
    # NOT enough — those are usually genuinely different things.
    r = server.check_term_consistency(
        "Tegevusvaldkond on lai. Märgendusvaldkond on kitsam."
    )
    check("two compounds sharing a head alone do not flag",
          not [g for g in r["groups"] if g["rule"] == "shared-compound-head"],
          str(r["groups"]))

    # Consistent prose stays quiet.
    r = server.check_term_consistency(
        "Andmestik valmis mais. Andmestik sisaldab pilte. Andmestik on avalik."
    )
    check("consistent terminology flags nothing", len(r["groups"]) == 0,
          str(r["groups"]))


def familiarity_decisiveness_guard() -> None:
    """The junk-tail gate must not fire when a real, >= 0.60 top neighbour
    vouches for the compound. Captured production fastText data."""
    print("familiarity — junk-tail decisiveness guard")

    # pildiandmestik: ordinary compound, top neighbour is its own head at
    # 0.71, but 5/8 of the tail is scrape junk. Must NOT flag.
    is_suspect, reasons, _q = server._familiarity_verdict(
        False, 0.71, [
            ("andmestik", 0.71), ("andmestiku", 0.59), ("juhtmestik", 0.526),
            ("graafikaKunst", 0.506), ("kunstitarbedMõõdutabelidTellimise", 0.496),
            ("ÜhisgümnaasiumKudumidPolosärgidPüksid", 0.49),
            ("ErakoolKudumidPolosärgidPluusid", 0.48),
            ("kihtFliisidPluusidPüksid", 0.47),
        ], ["pildi", "andmestik"])
    check("pildiandmestik not flagged (real top neighbour vouches)",
          is_suspect is False, str(reasons))

    # sisuaudit: 0.605 top score but the top neighbour is ITSELF junk —
    # the tail is decisive here, so it must still flag.
    is_suspect, reasons, _q = server._familiarity_verdict(
        False, 0.605, [
            ("ÜhisgümnaasiumKudumidPolosärgidPüksid", 0.605),
            ("põhjalTripAdvisori", 0.603), ("terviktekstRedaktsiooni", 0.603),
            ("SOTSIAALMEEDIASMOBIILIRAKENDUS", 0.598),
            ("ErakoolKudumidPolosärgidPluusid", 0.595),
            ("kihtFliisidPluusidPüksid", 0.59),
            ("graafikaKunst", 0.588), ("PortaalUudised", 0.585),
        ], ["sisu", "audit"])
    check("sisuaudit still flagged (top neighbour is junk)",
          is_suspect is True, str(reasons))


impersonal_voice_corrections()
officialese_separation()
officialese_lexicons()
term_consistency()
familiarity_decisiveness_guard()

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nall officialese / term-consistency tests passed")
