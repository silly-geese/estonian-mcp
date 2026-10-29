"""Tests for check_translationese: English sentence structure and English
habits of form in Estonian.

Every rule is pinned both ways. The flagged sentences are the wrong forms
the sources rewrite (EKK, EKI teatmik, EKI keelenõuvakk, Dreving and
Mäekivi in EKI's collections, AMSS) or the same pattern as an English
original would carry it. The clean ones are what those sources give as
correct, and the traps a native text sets: a possessor that is not the
subject, a participle's agent, a parenthetical "ma arvan", hakkama saama,
an apposition before ja, an institution's name in a heading.

Run via:

    uv run python tests/test_translationese.py
"""
from __future__ import annotations

import sys
import time
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


def issues(text: str) -> list[dict]:
    return server.check_translationese(text)["issues"]


# (text, rule, suggestion): each must raise exactly that rule, with that
# suggestion, and nothing else.
FLAGGED = [
    # oma-possessive: the subject's own thing is oma (EKK, M 8).
    ("Palun saatke meile teie andmed.", "oma-possessive", "oma andmed / andmed"),
    ("Ma kaotasin minu võtmed.", "oma-possessive", "oma võtmed / võtmed"),
    ("Me täname meie kliente.", "oma-possessive", "oma kliente / kliente"),
    ("Sisestage Teie parool.", "oma-possessive", "oma parool / parool"),
    ("Kas sa võtsid sinu raamatu kaasa?", "oma-possessive", "oma raamatu / raamatu"),
    # pronoun-after-verb (EKI teatmik, Tekstisidusus).
    ("Oma tegevuses lähtume me kliendi vajadustest.", "pronoun-after-verb",
     "lähtume / me lähtume"),
    ("Homme saate te vastuse.", "pronoun-after-verb", "saate / te saate"),
    # verb-second after a fronted subordinate clause (EKK, SÜ 66).
    ("Kui tellimus on kinnitatud, me saadame arve.", "verb-second", "saadame"),
    ("Kui te olete valmis, me saame alustada.", "verb-second", "saame"),
    ("Kui saabub kevad, meeleolu muutub paremaks.", "verb-second", "muutub meeleolu"),
    ("Kuna ilm oli ilus, lapsed mängisid õues.", "verb-second", "mängisid lapsed"),
    # saama-future (EKK, SÜ 27).
    ("See saab olema suurepärane aasta.", "saama-future", "on / tuleb"),
    ("Homme saab siin toimuma ühisistung.", "saama-future", "toimub"),
    ("Me saame pakkuma uut teenust.", "saama-future", "pakume"),
    # maks (EKK, SÜ 100; Dreving 2009).
    ("Tehti ettepanek võtmaks õigusakt vastu.", "maks-infinitive", "ettepanek võtta"),
    ("Meil on võimalus osalemaks konkursil.", "maks-infinitive", "võimalus osaleda"),
    ("Tagamaks parima tulemuse, kontrollime kõike.", "maks-purpose", "et tagada"),
    ("Ene sõitis Pärnu sõlmimaks lepingut.", "maks-purpose", "sõlmima"),
    # mitte-des (EKK, SÜ 100).
    ("Mitte leides teda kodunt, läksin töö juurde.", "mitte-des", "Leidmata"),
    # tanu-negative (Mäekivi 2013; AMSS).
    ("Tänu ressursside piiratusele jääb programm täitmata.", "tanu-negative",
     "ressursside piiratuse tõttu"),
    ("Tänu maavärinale jäid paljud kodust ilma.", "tanu-negative", "maavärina tõttu"),
    # english-phrase (AMSS; Kask 2022).
    ("Minu arvamusel on see hea lahendus.", "english-phrase", "minu arvates / minu meelest"),
    ("Minu vabandused hilinemise pärast.", "english-phrase", "vabandan"),
    # letters (EKI keelenõuvakk 2026, EKI teatmik Koma).
    ("Hea Mari,\n\nsaadan lepingu.", "greeting-punctuation", "Hea Mari!"),
    ("Tere Mari,\n\naitäh kirja eest.", "greeting-punctuation", "Tere, Mari!"),
    ("Tere, Mari,\n\naitäh kirja eest.", "greeting-punctuation", "Tere, Mari!"),
    ("Lugupeetud härra Tamm,\n\nTeatame, et taotlus on rahuldatud.", "greeting-punctuation",
     "Lugupeetud härra Tamm!"),
    ("Tere Mari!\nAitäh kirja eest.", "greeting-punctuation", "Tere, Mari"),
    ("Saadan lepingu.\n\nLugupidamisega,\nMari Tamm", "closing-comma", "Lugupidamisega"),
    ("Saadan lepingu.\n\nParimate soovidega, Mari", "closing-comma", "Parimate soovidega"),
    # serial-comma (EKI teatmik, Koma).
    ("Pakume kohvi, teed, ja küpsiseid.", "serial-comma", "teed ja"),
    ("Kohal olid Mari, Jüri, ja Peeter.", "serial-comma", "Jüri ja"),
    # title-case (EKI teatmik, Pealkiri; Microsoft Estonian Style Guide).
    ("## Meie Teenused Ja Hinnad\n\nPakume tõlkeid.", "title-case", "Meie teenused ja hinnad"),
    ("Kuidas Me Saame Aidata\n\nKirjutage meile.", "title-case", "Kuidas me saame aidata"),
    ("Korduma Kippuvad Küsimused\n\nSiit leiate vastused.", "title-case",
     "Korduma kippuvad küsimused"),
]

# Correct Estonian: must raise nothing.
CLEAN = [
    # oma-possessive: the possessor is not the subject, or the possessed
    # thing is the subject (EKK); a participle's agent; adverbials; titles.
    "Ma andsin Jürile tema artikli tagasi.",
    "Teie tellimus on teel.",
    "Minu nimi on Mari.",
    "Saatke andmed, et saaksime teie tellimust töödelda.",
    "Saadame teile teie arve.",
    "Minu arvates on see hea.",
    "Tulge meie juurde!",
    "Vaadake meie pakkumisi.",
    "Ma loen raamatut „Minu Eesti“.",
    "Kas te saite meie kirja kätte?",
    "Me oleme teie partner.",
    "Kinnitame, et tasume ravi eest meie hüvitatava osa.",
    "Mulle meeldib minu töö.",
    "Palun saatke meile oma andmed.",
    # pronoun-after-verb: a question, a long stressed pronoun.
    "Olete te kindel?",
    "Teeme seda meie, mitte nemad.",
    # verb-second: siis, a question, a demonstrative, a negation, a long
    # contrastive pronoun, a parenthetical, V2 already, a pronoun subject
    # after an adverb (EKK: "üsna tavaline").
    "Kui sa tuled, siis me läheme kinno.",
    "Kui sa tuled, kas me läheme kinno?",
    "Kui ta tuli, see oli tore.",
    "Kui me jõudsime, me ei leidnud kedagi.",
    "Kui sa tuled, mina ootan sind kodus.",
    "Kui me võrdleme andmeid, oleme, ma arvan, esikolmikus.",
    "Kuna ilm oli ilus, läksime randa.",
    "Kui saabub kevad, muutub ka meeleolu paremaks.",
    "Et vältida segadust, kasutame ühte terminit.",
    "Kui mees, kes seal seisis, lahkus, läksime koju.",
    "Eile ma läksin poodi.",
    "Kui palju see maksab?",
    # saama: possibility, idioms, the past.
    "Seda saab teha homme.",
    "Varas sai minema.",
    "Ülejäänuga me saame hakkama.",
    "Ta sai õppima ülikooli.",
    # maks after et (EKI accepts it to keep two et apart), mitte in a
    # contrast, tänu with a good cause (ÕS 2025, EKI 2026).
    "Ta ütles, et sõbrad jäid koju, lugemaks raamatuid.",
    "Õpid mitte lugedes, vaid tehes.",
    "Tänu sinule pääsesime.",
    "Kihnus on lambatõug säilinud tänu saare eraldatusele.",
    # Accepted phrases (ÕS 2025, EKI ühendsõnastik 2026).
    "Õnnetus leidis aset maanteel.",
    "Raha ei mängi tema elus erilist rolli.",
    "Mis puudutab su välimust, siis see on korras.",
    "Laps sai viis aastat vanaks.",
    "Poisse on rohkem kui tüdrukuid.",
    "Ta palus mul end asjade käiguga kursis hoida.",
    "Kasutades seda tööriista, säästad aega.",
    # serial-comma: a clause, an apposition, an inserted remark, an
    # address, a comma shift (EKI teatmik, Koma), and a plain list.
    "Võid teha, mida tahad, ja minna, kuhu tahad.",
    "Sõna võtsid António Costa, Portugali peaminister, ja Jean-Claude Juncker, "
    "komisjoni president.",
    "Korjasin seeni, nagu riisikaid ja pilvikuid, ning nautisin loodust.",
    "Tulge siia, lapsed, ja vaadake, mis ma leidsin!",
    "Ta ütles, et tuleb homme, ja tuligi.",
    "Juhtisin projekti, mis kestis kaks aastat, ja koolitasin uusi töötajaid.",
    "Ostsin leiba, mis oli värske, ja piima.",
    "Mari, Jüri ja Peeter tulid.",
    # Letters as EKI writes them.
    "Hea Mari!\n\nSaadan lepingu.",
    "Hea kolleeg Tiina!\n\nSaadan lepingu.",
    "Tere, Mari!\n\nAitäh kirja eest.",
    "Lugupeetud koostööpartner\n\nTeatame, et taotlus on rahuldatud.",
    "Saadan lepingu.\n\nLugupidamisega\nMari Tamm",
    "Tervitades Aino",
    "Hea meel,\nkui tulete.",
    "Tere tulemast Tallinna!",
    "Tänades teid, lõpetan.",
    # Headings: sentence case, names and institutions.
    "## Meie teenused ja hinnad\n\nPakume tõlkeid.",
    "Eesti Keele Instituut\n\nTekst.",
    "Riigi Infosüsteemi Amet\n\nTekst.",
    "Tartu Ülikooli Raamatukogu\n\nTekst.",
    "Keskkonnaameti ning Maksu- ja Tolliameti ülesanded\n\nTekst.",
    "Noorte- ja lastefilmide festival Just Film avalikustas võistlusprogrammid\n\nTekst.",
    'Maret Maripuu: "Kas Sina märkad?"\n\nTekst.',
    # A first-person text that drops the pronoun where it can.
    "Loodan, et saate aidata. Ma tulen homme. Arvan, et see sobib. Ootan vastust.",
]


def flagged() -> None:
    print("the wrong forms are flagged with the native rewrite")
    for text, rule, suggestion in FLAGGED:
        found = issues(text)
        got = [(i["rule"], i["suggestion"]) for i in found]
        check(f"{rule}: {text[:50]!r}", got == [(rule, suggestion)], str(got))


def clean() -> None:
    print("correct Estonian raises nothing")
    for text in CLEAN:
        found = issues(text)
        check(f"clean: {text[:60]!r}", not found,
              str([(i["rule"], i.get("phrase") or i.get("word")) for i in found]))


def subject_pronoun() -> None:
    print("subject-pronoun counts sentences, reported once")
    text = ("Ma kirjutan teile, et küsida pakkumist. Ma vajan kolme tõlget. "
            "Ma loodan, et saate aidata. Ma ootan vastust.")
    found = issues(text)
    check("four 'Ma' openings are one issue at the first", [(i["rule"], i["position"])
          for i in found] == [("subject-pronoun", 0)], str(found))
    check("the explanation counts them", "4 lauset 4-st" in found[0]["explanation"],
          found[0]["explanation"])
    check("two of five is below the bar",
          not issues("Ma tulen. Arvan nii. Ma lähen. Loodan. Ootan."), "")


def issue_shape() -> None:
    print("every issue is quotable and points at its text")
    text = ("Tere Mari,\n\nKui te olete valmis, me saame alustada. See saab olema hea. "
            "Palun saatke meile teie andmed, tagamaks sujuva alguse. Meie tiim, meie "
            "partnerid, ja meie kliendid ootavad.\n\nParimate soovidega,\nAnna")
    result = server.check_translationese(text)
    rules = [i["rule"] for i in result["issues"]]
    check("a translated letter raises seven issues", rules == [
        "greeting-punctuation", "verb-second", "saama-future", "oma-possessive",
        "maks-purpose", "serial-comma", "closing-comma"], str(rules))
    for i in result["issues"]:
        quoted = i.get("phrase") or i.get("word")
        check(f"{i['rule']} has an Estonian label, explanation and suggestion",
              all(i.get(k) for k in ("rule_estonian", "explanation", "suggestion")), str(i))
        check(f"{i['rule']} quotes the text at its position",
              text[i["position"]:i["position"] + len(quoted)] == quoted, str(i))
    check("the summary counts them", result["summary_estonian"]
          == f"Leiti {len(rules)} kohta, mis kõlavad tõlkena.", result["summary_estonian"])
    check("clean text says so", server.check_translationese("Tere!")["summary_estonian"]
          == "Tõlkekeele tunnuseid ei leitud.", "")


def linear_time() -> None:
    print("linear time on 100,000 characters: about the cost of tagging them")
    texts = {
        "a translated letter, repeated": ("Kui te olete valmis, me saame alustada. "
                                          "Palun saatke meile teie andmed. " * 1500)[:100_000],
        "one long list": ("kohvi, " * 15_000)[:100_000],
        "heading lines": ("Meie Teenused Ja Hinnad\n\nPakume tõlkeid.\n\n" * 2500)[:100_000],
        "greeting lines": ("Tere Mari,\n\n" * 9000)[:100_000],
    }
    server.check_translationese("Kui sa tuled, me läheme.")   # warm the models
    for name, text in texts.items():
        t = time.perf_counter()
        tagged = server._Text()(text)
        tagged.tag_layer(["sentences", "morph_analysis"])
        tagging = time.perf_counter() - t
        t = time.perf_counter()
        server.check_translationese(text)
        dt = time.perf_counter() - t
        check(f"{name}: {dt:.1f} s, tagging {tagging:.1f} s", dt < 2 * tagging + 3,
              f"{dt:.1f} s")


flagged()
clean()
subject_pronoun()
issue_shape()
linear_time()

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nall check_translationese tests passed")
