"""Smoke tests for the EstNLTK MCP tools.

Calls each tool's underlying function directly (no MCP transport) to
prove the EstNLTK wiring is correct. Run via:

    uv run python tests/test_smoke.py

Exits non-zero on any failure. CI uses this as the gate.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server

# A glossary exported in the developer's shell would change what these
# tools return; the suite checks the server as it ships. The
# glossary is read on first use, so this is early enough.
for _name in ("ESTNLTK_MCP_DOMAIN_GLOSSARY", "ESTNLTK_MCP_DOMAIN_IDENTIFIERS",
              "ESTNLTK_MCP_DOMAIN_SUGGEST_CUTOFF"):
    os.environ.pop(_name, None)

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS {label}")
    else:
        failures.append(f"{label}: {detail}")
        print(f"  FAIL {label} {detail}")


SAMPLE = "Mina sõinn täna hommikul Tallinnas putru."

print("tokenize")
tok = server.tokenize(SAMPLE)
check("returns sentences+words", "sentences" in tok and "words" in tok)
check("non-empty words", len(tok["words"]) >= 6)
check("keeps punctuation", tok["words"][-1] == ".", str(tok["words"]))

print("lemmatize")
lem = server.lemmatize("Mina sõin täna hommikul Tallinnas putru.")
lemmas = {row["lemma"] for row in lem}
check("recognises 'sööma'", "sööma" in lemmas, str(lemmas))
check("recognises 'Tallinn'", "Tallinn" in lemmas, str(lemmas))
check("recognises 'puder'", "puder" in lemmas, str(lemmas))
check("every entry keeps word and lemma, even when they match",
      all(set(row) == {"word", "lemma"} for row in lem)
      and {"word": "täna", "lemma": "täna"} in lem, str(lem))
check("punctuation is omitted", [row["word"] for row in lem]
      == ["Mina", "sõin", "täna", "hommikul", "Tallinnas", "putru"], str(lem))

print("pos_tag")
pos = server.pos_tag(SAMPLE)
check("first tokens are non-empty POS", all(row["partofspeech"] for row in pos[:5]))
pos = server.pos_tag("Ta pani plehku, aga § 2 jäi :) alles.")
tags = {row["word"]: row["partofspeech"] for row in pos}
check("no punctuation or emoticon entries", not any(t == "Z" for t in tags.values())
      and not {",", ":)", "."} & set(tags), str(pos))
check("a symbol is kept as Y", tags.get("§") == "Y", str(tags))
# Vabamorf tags any word of 40+ characters Z without analysing it. It is a
# word, not punctuation, so the per-token tools must keep it.
LONG = "andmekaitseinspektsioonipeadirektoriasetäitjale"
long_text = f"Kiri saadeti {LONG} eile."
check("a 40+ character word is kept by pos_tag, as Z",
      {"word": LONG, "partofspeech": "Z"} in server.pos_tag(long_text), long_text)
check("and by lemmatize", LONG in [r["word"] for r in server.lemmatize(long_text)])
check("and by analyze_morphology",
      [r["word"] for r in server.analyze_morphology(LONG)] == [LONG])
check("while the full stop after it is still left out",
      "." not in [r["word"] for r in server.lemmatize(long_text)])
check("'plehku' is X, the verb-bound word", tags.get("plehku") == "X", str(tags))

print("analyze_morphology")
morph = server.analyze_morphology("Tere maailm!")
by_word = {row["word"]: row for row in morph}
check("compound 'maailm' splits", by_word.get("maailm", {}).get("root_tokens") == ["maa", "ilm"], str(morph))
check("a one-part root has no root_tokens", "root_tokens" not in by_word.get("Tere", {"root_tokens": 0}),
      str(by_word.get("Tere")))
check("'!' is omitted", list(by_word) == ["Tere", "maailm"], str(morph))
check("form_estonian only where the form has a name",
      by_word["maailm"].get("form_estonian") == "ainsuse nimetav"
      and "form_estonian" not in by_word["Tere"] and by_word["Tere"]["form"] == "", str(morph))
amb = {row["word"]: row.get("analyses_count", 1) for row in server.analyze_morphology("Ta on kodus.")}
check("analyses_count only on ambiguous words, no is_ambiguous",
      amb.get("on", 0) > 1 and amb.get("kodus") == 1
      and not any("is_ambiguous" in row for row in morph), str(amb))
both = server.analyze_morphology("Ta on kodus.", all_analyses=True)
check("all_analyses carries no analyses_count",
      not any("analyses_count" in row or "is_ambiguous" in row for row in both)
      and len(next(row for row in both if row["word"] == "on")["analyses"]) > 1, str(both))
clit = {row["word"]: row.get("clitic") for row in server.analyze_morphology("Kas sinagi tuled?")}
check("clitic only where there is one", clit == {"Kas": None, "sinagi": "gi", "tuled": None}, str(clit))
plehku = next((row for row in server.analyze_morphology("Ta pani plehku.") if row["word"] == "plehku"), {})
check("'plehku' is X and not flagged foreign",
      plehku.get("partofspeech") == "X" and "usage_note" not in plehku
      and "usage_note_estonian" not in plehku, str(plehku))


def _empty_values(node, key=""):
    """Every (key, value) in the output that is None or False, or "" or []
    outside `form`, which is "" for every uninflected word."""
    if isinstance(node, list):
        if not node and key != "form":
            return [(key, node)]
        return [hit for item in node for hit in _empty_values(item, key)]
    if isinstance(node, dict):
        return [hit for k, v in node.items() for hit in _empty_values(v, k)]
    if node is None or node is False or (node == "" and key != "form"):
        return [(key, node)]
    return []


probe = ("Tere maailm! Kas sinagi tuled? Ta pani plehku. Ma ei tea. Okei, tarvitan "
         "täis pudelit ja tuntud laulja. § 2 lõige 3, :) Riigid on suured.")
for mode in (False, True):
    empty = _empty_values(server.analyze_morphology(probe, all_analyses=mode))
    check(f"no null, false or empty value (all_analyses={mode})", not empty, str(empty[:5]))
# indeclinable flag: lexical indeclinable + -tud/-nud participle = True,
# normal declinable adjective = absent.
r = server.analyze_morphology("täis pudel tuntud laulja kollane lill")
flags = {row["word"]: row.get("indeclinable", False) for row in r}
check("'täis' flagged indeclinable", flags.get("täis") is True, str(flags))
check("'tuntud' (-tud participle) flagged indeclinable", flags.get("tuntud") is True, str(flags))
check("'kollane' NOT indeclinable", flags.get("kollane") is False, str(flags))
check("indeclinable present only as true",
      all(row.get("indeclinable", True) is True for row in r)
      and "indeclinable" not in next(row for row in r if row["word"] == "kollane"), str(r))
check("no usage notes on a neutral word",
      not any("usage_note" in row or "usage_note_estonian" in row for row in r), str(r))
# Marked-usage lexicon: 'tarvitama' should be tagged archaic.
r = server.analyze_morphology("Ma tarvitan programmi.")
tarv = next((row for row in r if row.get("lemma") == "tarvitama"), None)
check("'tarvitama' flagged archaic", tarv and tarv.get("usage_note") == "archaic", str(tarv))
check(
    "archaic flag has Estonian rendering",
    tarv and tarv.get("usage_note_estonian", "").startswith("vananenud"),
    tarv.get("usage_note_estonian") if tarv else "no row",
)
# Anglicism: 'okei' flagged as foreign or interjection (POS tag I).
r = server.analyze_morphology("Okei, sõidame!")
okei = next((row for row in r if row.get("word", "").lower() == "okei"), None)
check(
    "'okei' has a usage_note flag",
    okei and okei.get("usage_note") in {"foreign", "interjection"},
    str(okei),
)

print("paradigm")
p = server.paradigm("raamat")
forms_by_code = {f["form"]: f["surface"] for f in p["forms"]}
check("nominal paradigm has sg n", forms_by_code.get("sg n") == "raamat", str(p["forms"][:3]))
check("nominal paradigm has sg p", forms_by_code.get("sg p") == "raamatut")
check("nominal paradigm has pl g", forms_by_code.get("pl g") in {"raamatute", "raamatuid"} or forms_by_code.get("pl g"))
check("all forms have Estonian labels", all("form_estonian" in f for f in p["forms"]))
p = server.paradigm("kasutama")
verb_forms = {f["form"]: f["surface"] for f in p["forms"]}
check("verb paradigm has 3sg present", verb_forms.get("b") == "kasutab", str(p["forms"][:3]))
check("verb paradigm has past 1sg", verb_forms.get("sin") == "kasutasin")
check("verb paradigm has nud-participle", verb_forms.get("nud") == "kasutanud")
p = server.paradigm("ja")
check("non-inflecting word returns empty forms", p["forms"] == [], str(p))
check("and says why", bool(p.get("summary_estonian")), str(p))
try:
    server.paradigm("two words")
    check("paradigm rejects whitespace", False, "no exception")
except ValueError:
    check("paradigm rejects whitespace", True)

print("spell_check")
sp = server.spell_check(SAMPLE)
bad = [row for row in sp if not row["spelling"]]
check("flags 'sõinn' as misspelled", any(row["text"] == "sõinn" for row in bad))
suggestions = next((row.get("suggestions", []) for row in bad if row["text"] == "sõinn"), [])
check("suggests 'sõin'", "sõin" in suggestions, str(suggestions))
check("lists only the misspelled tokens", [row["text"] for row in sp] == ["sõinn"]
      and all(row["spelling"] is False for row in sp), str(sp))
check("clean text gives an empty list", server.spell_check("Tere maailm.") == [],
      str(server.spell_check("Tere maailm.")))
twice = server.spell_check("sõinn ja sõinn")
check("each occurrence is listed", [row["text"] for row in twice] == ["sõinn", "sõinn"], str(twice))
check("no suggestions key when there are none",
      server.spell_check("sõinn", suggestions=False) == [{"text": "sõinn", "spelling": False}],
      str(server.spell_check("sõinn", suggestions=False)))

print("syllabify")
syl = server.syllabify("hommikul")
check("3 syllables", len(syl) == 3)
check("syllable shape", set(syl[0].keys()) == {"syllable", "quantity", "accent"})

print("named_entities")
ner = server.named_entities("Eile käis Jaan Tamm Tallinnas Eesti Panga juures.")
types = {ne["type"] for ne in ner}
check("PER detected", "PER" in types)
check("LOC detected", "LOC" in types)
check("ORG detected", "ORG" in types)

print("find_related_words")
related = server.find_related_words("kohv", n=5)
check("returns 5 matches", len(related["matches"]) == 5)
check("does not echo the input word", set(related) == {"matches"}, str(sorted(related)))
check(
    "match shape",
    all("word" in m and "score" in m for m in related["matches"]),
)
check(
    "scores in [-1, 1]",
    all(-1.0 <= m["score"] <= 1.0 for m in related["matches"]),
)
check(
    "kohv neighbours include a drink",
    any(m["word"] in {
        "jook", "õlu", "piim", "alkohol", "tee", "kakao", "rumm",
        "viski", "vesi", "mahl", "limonaad",
    } for m in related["matches"]),
    str([m["word"] for m in related["matches"]]),
)
try:
    server.find_related_words("two words")
    check("rejects whitespace", False, "no exception")
except ValueError:
    check("rejects whitespace", True)

print("synonyms")
syn = server.synonyms("kasutama")
check("returns synsets", syn["synset_count"] > 0)
flat_lemmas = {lemma for s in syn["synsets"] for lemma in s["lemmas"]}
check("recognises 'tarvitama' as a synonym", "tarvitama" in flat_lemmas, str(flat_lemmas))
check("does not echo the input word", set(syn) == {"synsets", "synset_count"}, str(sorted(syn)))
# The POS is the second-to-last dot segment of `name` (a literal can hold a
# dot itself, "1."), so `pos` is not repeated; `examples` only when WordNet
# has some.
check("synsets carry name and lemmas, no pos",
      all({"name", "lemmas"} <= set(s) and "pos" not in s
          and s["name"].rsplit(".", 2)[1] in {"n", "v", "a", "r"}
          for s in syn["synsets"]),
      str(syn["synsets"][:2]))
# Some synsets have no gloss (lahe.a.02): `definition` is then omitted,
# never null.
lahe = server.synonyms("lahe", max_synsets=20)["synsets"]
check("definition is present when WordNet has a gloss and never null",
      any("definition" in s for s in syn["synsets"])
      and all(s.get("definition", "x") for s in lahe)
      and any("definition" not in s for s in lahe),
      str([(s["name"], s.get("definition")) for s in lahe]))
korpus = server.synonyms("korpus", max_synsets=10)["synsets"]
check("examples is omitted when empty and present when not",
      all(s.get("examples", ["x"]) for s in korpus)
      and any("examples" in s for s in korpus) and any("examples" not in s for s in korpus),
      str([(s["name"], s.get("examples")) for s in korpus]))
try:
    server.synonyms("two words")
    check("synonyms rejects whitespace", False, "no exception")
except ValueError:
    check("synonyms rejects whitespace", True)

print("check_compounds")
r = server.check_compounds("Käisin laste aias ja kooli maja juures.")
suggestions = {i["suggestion"] for i in r["issues"]}
check("flags 'laste aias' → lasteaias", "lasteaias" in suggestions, str(suggestions))
check("flags 'kooli maja' → koolimaja", "koolimaja" in suggestions, str(suggestions))
clean = server.check_compounds("See on tavaline lause kus pole vigu.")
check("clean text → no flags", len(clean["issues"]) == 0, str(clean["issues"]))
check("compounds every issue has rule_estonian",
      all(i.get("rule_estonian") for i in r["issues"]),
      str([i.get("rule_estonian") for i in r["issues"]]))

print("check_punctuation")
r = server.check_punctuation("Ma arvan et töötan kodus sest see on mugav.")
words = {i["word"] for i in r["issues"]}
check("flags missing comma before 'et'", "et" in words, str(words))
check("flags missing comma before 'sest'", "sest" in words, str(words))
clean = server.check_punctuation("Ma arvan, et kõik on hästi.")
check("already-correct comma → no flag", len(clean["issues"]) == 0, str(clean["issues"]))
check("punctuation every issue has rule_estonian",
      all(i.get("rule_estonian") for i in r["issues"]))

print("check_hyphenation")
r = server.check_hyphenation("hommikul")
check("3-syllable word → 2 break points", len(r["breaks"]) == 2, str(r["breaks"]))
check("preferred uses interpunct", "·" in r["preferred"], r["preferred"])
short = server.check_hyphenation("on")
check("too-short word → no breaks", short["breaks"] == [], str(short))
check("hyphenation returns breaks, preferred and syllable_count only",
      set(r) == set(short) == {"breaks", "preferred", "syllable_count"},
      f"{sorted(r)} {sorted(short)}")
check("a too-short word is told apart by its syllable count",
      short["syllable_count"] < 2 and short["preferred"] == "on", str(short))
try:
    server.check_hyphenation("two words")
    check("rejects whitespace", False, "no exception")
except ValueError:
    check("rejects whitespace", True)

print("check_numbers")
r = server.check_numbers("See maksis 3.14 eurot ja linnas elab 1,500,000 inimest.")
rules = {i["rule"] for i in r["issues"]}
check("flags decimal '3.14'", "decimal-separator" in rules, str(rules))
check("flags thousands '1,500,000'", "thousands-separator" in rules, str(rules))
clean = server.check_numbers("Päeval 14.05.2026 maksis pi umbes 3,14.")
check("date pattern + correct decimal → no flag",
      len(clean["issues"]) == 0, str(clean["issues"]))
check("numbers every issue has rule_estonian",
      all(i.get("rule_estonian") for i in r["issues"]))

print("check_compound_familiarity")
# mõtteliin = literal calque from English "train of thought"; real
# Estonian is mõttekäik. OOV in our 100K-vocab medium model, top
# similarity ~0.536: exactly the AI failure mode this tool catches.
r = server.check_compound_familiarity(
    "See on mõtteliin, mis viib eesmärgini."
)
check("mõtteliin analysed as compound",
      any(c["lemma"] == "mõtteliin" for c in r["all_compounds"]),
      str([c["lemma"] for c in r["all_compounds"]]))
check("mõtteliin flagged as suspect",
      any(c["lemma"] == "mõtteliin" and c["is_suspect"] for c in r["all_compounds"]),
      str(r["suspect_compounds"]))
check("wordnet_checked reported", isinstance(r["wordnet_checked"], bool),
      str(r.get("wordnet_checked")))
check("suspect_compounds lists the lemma", r["suspect_compounds"] == ["mõtteliin"],
      str(r["suspect_compounds"]))
check("unattested_compounds lists the lemma", r["unattested_compounds"] == ["mõtteliin"],
      str(r["unattested_compounds"]))
check("the input is not echoed", "text" not in r, str(sorted(r)))
# A note only says WordNet gave no answer for an unattested compound, so
# where WordNet is installed there is none here; where it is not, there is.
if r["wordnet_checked"]:
    check("no note with WordNet consulted", "note" not in r, str(r.get("note")))
else:
    check("WordNet unavailable: a note says so",
          "WordNet was unavailable" in (r.get("note") or ""), str(r.get("note")))
mt = next((c for c in r["all_compounds"] if c["lemma"] == "mõtteliin"), {})
check("no keys that repeat neighbours[0] or the gate",
      mt and not {"top_score", "top_neighbour", "reasons"} & set(mt), str(mt))
check("neighbour_quality carries only the signal",
      mt.get("neighbour_quality") == {"signal": "usable"}, str(mt))
_cap = server._FAMILIARITY_WORDNET_CAP
server._FAMILIARITY_WORDNET_CAP = 0
try:
    r = server.check_compound_familiarity("See on mõtteliin.")
    check("an unconsulted WordNet is noted",
          ("`in_wordnet` is null" if r["wordnet_checked"] else "WordNet was unavailable")
          in (r.get("note") or ""), str(r.get("note")))
    r = server.check_compound_familiarity("Käisin raamatukogus.")
    check("no note when every compound is attested anyway", "note" not in r,
          str(r.get("note")))
finally:
    server._FAMILIARITY_WORDNET_CAP = _cap
_wn = server._wordnet_available
server._wordnet_available = lambda: False
try:
    r = server.check_compound_familiarity("See on mõtteliin.")
    check("WordNet off: the note says it was unavailable",
          r["wordnet_checked"] is False
          and "WordNet was unavailable" in (r.get("note") or ""), str(r.get("note")))
    r = server.check_compound_familiarity("Käisin raamatukogus.")
    check("WordNet off, all attested: no note", "note" not in r, str(r.get("note")))
finally:
    server._wordnet_available = _wn
r = server.check_compound_familiarity("Käisin raamatukogus ja koolimajas.")
check("real compounds analysed", r["compounds_analysed"] == 2)
check("real compounds NOT suspect", len(r["suspect_compounds"]) == 0,
      str(r["suspect_compounds"]))
check("attested compounds carry no neighbours",
      all("neighbours" not in c and "neighbour_quality" not in c
          for c in r["all_compounds"]), str(r["all_compounds"]))
# Regression, the false positives that cost the gate its credibility:
# `lisakäive` (junk top neighbour at 0.670) and `klikkimismäär` (clean
# neighbourhood, 0.586) are both ordinary business Estonian and both were
# reported to a client as invented compounds. Neither may flag. They stay
# unattested, because no 100K-word vocabulary holds them, which is
# exactly the distinction the output now draws.
r = server.check_compound_familiarity(
    "Kampaania tõi lisakäive ja klikkimismäär kasvas."
)
for lemma in ("lisakäive", "klikkimismäär"):
    c = next((x for x in r["all_compounds"] if x["lemma"] == lemma), None)
    check(f"{lemma} analysed", c is not None,
          str([x["lemma"] for x in r["all_compounds"]]))
    if c:
        check(f"{lemma} NOT flagged suspect", c["is_suspect"] is False, str(c))
        check(f"{lemma} reported unattested", c["attested"] is False, str(c))
check("unattested_compounds lists both lemmas",
      r["unattested_compounds"] == ["lisakäive", "klikkimismäär"], str(r["unattested_compounds"]))
check("suspect_compounds empty", r["suspect_compounds"] == [],
      str(r["suspect_compounds"]))
# `lisakäive`'s nearest neighbour is a scrape-artifact token, so the
# similarity score means nothing and the tool says so rather than
# guessing.
lk = next((x for x in r["all_compounds"] if x["lemma"] == "lisakäive"), None)
if lk:
    check("lisakäive: signal reported as none",
          lk["neighbour_quality"]["signal"] == "none", str(lk))
# Documented miss: 'toortõlkeoht' (0.571) sits inside the band attested
# compounds occupy (`hinnakujundus` is 0.563), so it passes. Catching it
# is what put the gate at 0.60 and swept in ordinary Estonian with it.
r = server.check_compound_familiarity("See on tõsine toortõlkeoht.")
tt = next((c for c in r["all_compounds"] if c["lemma"] == "toortõlkeoht"), None)
check("toortõlkeoht analysed", tt is not None, str([c["lemma"] for c in r["all_compounds"]]))
if tt:
    check("toortõlkeoht is a documented miss", tt["is_suspect"] is False, str(tt))
    check("toortõlkeoht reported unattested", tt["attested"] is False, str(tt))
    check("toortõlkeoht has neighbour_quality",
          isinstance(tt.get("neighbour_quality"), dict)
          and set(tt["neighbour_quality"]) == {"signal"}
          and tt["neighbour_quality"].get("signal") in ("none", "usable"),
          str(tt))
r = server.check_compound_familiarity("Eile käisin poes ja ostsin leiba.")
check("no compounds → empty analysis", r["compounds_analysed"] == 0)
# Legal de-noise: solidaarvõlgnik is a real legal term that used to
# false-flag as a coinage (OOV in the general-web fastText vocab). The
# terms-of-art list must suppress that.
r = server.check_compound_familiarity("Vastutab solidaarvõlgnik.")
sv = next((c for c in r["all_compounds"] if c["lemma"] == "solidaarvõlgnik"), None)
check("legal term solidaarvõlgnik analysed", sv is not None,
      str([c["lemma"] for c in r["all_compounds"]]))
if sv:
    check("legal term NOT flagged suspect", sv["is_suspect"] is False, str(sv))
    check("legal de-noise marked in quality",
          sv.get("neighbour_quality", {}).get("legal_term") is True, str(sv))
    check("an attested legal term keeps only that marker",
          sv.get("neighbour_quality") == {"legal_term": True} and "neighbours" not in sv,
          str(sv))
r = server.check_compound_familiarity(
    "See on mõtteliin."
)
for c in r["all_compounds"]:
    if c["lemma"] == "mõtteliin":
        check("neighbours list populated", len(c["neighbours"]) > 0, str(c))
        check("neighbour score is float", isinstance(c["neighbours"][0]["score"], float))
        break

print("check_abbreviation_hyphenation")
# EKI accepts an all-capital abbreviation's case ending with or without a
# hyphen (ELi ~ EL-i, ASidel ~ AS-idel) and prefers the hyphen only before
# an ending that starts with l (THI-l, mitte THIl). Only that is flagged.
r = server.check_abbreviation_hyphenation("Saatsin arve OÜle ja küsisin ÜROlt.")
check("flags OÜle → OÜ-le and ÜROlt → ÜRO-lt",
      [(i["word"], i["suggestion"]) for i in r["issues"]]
      == [("OÜle", "OÜ-le"), ("ÜROlt", "ÜRO-lt")], str(r["issues"]))
check("two hits: the summary counts in the partitive",
      r["summary_estonian"].startswith("Leiti 2 l-algulist käändelõppu,"), r["summary_estonian"])
r = server.check_abbreviation_hyphenation("Otsus jäi THIl tegemata.")
check("flags EKI's own example THIl → THI-l",
      [i["suggestion"] for i in r["issues"]] == ["THI-l"], str(r["issues"]))
check("one hit: the summary keeps the nominative (1 käändelõpp)",
      r["summary_estonian"].startswith("Leiti 1 l-alguline käändelõpp,"), r["summary_estonian"])
# A stem read with a final consonant (ess, ell) takes the stem vowel, as in
# EL-i (EKI "Lühendamine"): ASle is misinflected, not just unhyphenated. A
# digit keeps the bare ending (EKI: MP3-le), and DJ is read dii-džei.
r = server.check_abbreviation_hyphenation("Arve saadeti ASle, toetus saadi ELlt ja SMSl on piir.")
check("consonant-final letter name gets the stem vowel",
      [i["suggestion"] for i in r["issues"]] == ["AS-ile", "EL-ilt", "SMS-il"], str(r["issues"]))
check("the summary names the stem vowel",
      "tüvevokaal i" in r["summary_estonian"], r["summary_estonian"])
r = server.check_abbreviation_hyphenation("Päring läks SQLle, fail PDFle ja uudis ERRle.")
check("SQL, PDF and ERR get the stem vowel",
      [i["suggestion"] for i in r["issues"]] == ["SQL-ile", "PDF-ile", "ERR-ile"], str(r["issues"]))
r = server.check_abbreviation_hyphenation("Fail läks MP3le ja kiri DJle.")
check("a digit and DJ keep the bare ending",
      [i["suggestion"] for i in r["issues"]] == ["MP3-le", "DJ-le"], str(r["issues"]))
# A Roman ordinal before a noun in the same case takes no ending at all
# (EKI "Arvukirjutus": VI peatükis, mitte VI-s peatükis). With no such noun
# it keeps one, hyphenated. CD and MM are abbreviations, not numerals.
r = server.check_abbreviation_hyphenation("Võistlus toimus XIIl korral ja ta osales IVl etapil.")
check("Roman ordinal before a same-case noun: the bare numeral",
      [i["suggestion"] for i in r["issues"]] == ["XII", "IV"], str(r["issues"]))
check("the summary says the numeral drops the ending",
      "käändelõputa" in r["summary_estonian"], r["summary_estonian"])
r = server.check_abbreviation_hyphenation("XIIl sajandil ehitati kirik.")
check("XIIl sajandil → XII", [i["suggestion"] for i in r["issues"]] == ["XII"], str(r["issues"]))
r = server.check_abbreviation_hyphenation("Linn püstitas kuningas Tõnis XIIle ausamba.")
check("Roman numeral with no same-case noun after keeps the ending",
      [i["suggestion"] for i in r["issues"]] == ["XII-le"], str(r["issues"]))
r = server.check_abbreviation_hyphenation("Kiri läks CDle. Ta jäi MMl eelmisel aastal neljandaks.")
check("CD and MM are abbreviations, not Roman numerals",
      [i["suggestion"] for i in r["issues"]] == ["CD-le", "MM-il"], str(r["issues"]))
# Chloride formulas: Vabamorf reads HCl as HC plus the adessive l, but the
# l is the l of Cl. BBCl and PCl have no digit and are not common formulas,
# so they are still flagged, as are ITl and NBAl (I + Tl, N + B + Al).
for ok in ("Soolhape ehk HCl on tugev hape.", "Katses kasutati KCl lahust.",
           "Ammooniumkloriidi valem on NH4Cl.", "Klorometaan CH3Cl on värvitu gaas.",
           "Kloroetaan C2H5Cl on tuntud ka kui etüülkloriid.",
           "Hüpokloorishape HOCl tekib kloori lahustamisel vees.",
           "Joodmonokloriid ICl on punakaspruun vedelik.", "Nitrosüülkloriid NOCl on kollane gaas.",
           "Lahusele lisati HCl ja KCl.", "Katses kasutati 0,1 M HCl lahust.",
           "| HCl lahus | ml | 250 |", "HCl LAHUSE VALMISTAMINE"):
    r = server.check_abbreviation_hyphenation(ok)
    check(f"chloride formula not flagged: {ok}", r["issues"] == [], str(r["issues"]))
r = server.check_abbreviation_hyphenation("Mängin PCl, saade oli BBCl, teade tuli ITl ja mängijad olid NBAl.")
check("an adessive that only looks like a formula is still flagged",
      [i["suggestion"] for i in r["issues"]] == ["PC-l", "BBC-l", "IT-l", "NBA-l"], str(r["issues"]))
for ok in ("ELi liikmesriigid leppisid kokku.", "Ta töötas ELis ja hiljem ÜROs.",
           "APIga ühendamine ja MCPst lugemine on lihtne.", "Lepingu sõlmis ASiga uus partner.",
           "Kaebus esitati ELile ja toetus saadi ELilt.", "Juhatuse liikmeid on ASil kaks.",
           "KOVidele saadeti uued juhised.", "Kasutame APIs uut liidest."):
    r = server.check_abbreviation_hyphenation(ok)
    check(f"EKI-correct hyphenless form not flagged: {ok}", r["issues"] == [], str(r["issues"]))
clean = server.check_abbreviation_hyphenation(
    "Saatsin kirja OÜ-le ja kasutasin API-t."
)
check("hyphenated forms not flagged", len(clean["issues"]) == 0, str(clean["issues"]))
nom = server.check_abbreviation_hyphenation("Tegu on MCP serveriga.")
check("bare nominative MCP not flagged", len(nom["issues"]) == 0, str(nom["issues"]))
plain = server.check_abbreviation_hyphenation("Käisin poes ja ostsin leiba.")
check("plain text: no flags", len(plain["issues"]) == 0, str(plain["issues"]))
r = server.check_abbreviation_hyphenation("Kirjutasin MTÜle.")
check("rule_estonian populated",
      r["issues"] and all(i.get("rule_estonian") for i in r["issues"]),
      str([i.get("rule_estonian") for i in r["issues"]]))
# The description promises this scope: any all-capital stem counts, known
# or not, and a mixed-case stem is never checked. Change both together.
r = server.check_abbreviation_hyphenation("XYZQle tuli abi.")
check("unknown all-caps stem flagged",
      [i["suggestion"] for i in r["issues"]] == ["XYZQ-le"], str(r["issues"]))
r = server.check_abbreviation_hyphenation("Kasutame SaaSile.")
check("mixed-case stem not checked", r["issues"] == [], str(r["issues"]))
# Every hit is one regex on its stem and a look at the next word, so the
# check costs little beyond EstNLTK's own tagging.
_unit = "Arve läks ASle ja OÜle. XIIl sajandil lisati HCl ja NH4Cl. "
_text = (_unit * (100_000 // len(_unit) + 1))[:100_000]
_t = time.perf_counter()
_tagged = server._Text()(_text)
_tagged.tag_layer(["morph_analysis"])
_tagging = time.perf_counter() - _t
_t = time.perf_counter()
server.check_abbreviation_hyphenation(_text)
_dt = time.perf_counter() - _t
check(f"100,000 characters: {_dt:.2f} s, tagging {_tagging:.2f} s",
      _dt < 2 * _tagging + 3, f"{_dt:.2f} s against {_tagging:.2f} s")
# The word after a Roman ordinal is looked up out of context only when
# disambiguation kept no noun reading in the ordinal's case (XIIl korral:
# korral comes out as the postposition), at most _ABBR_LOOKUPS times.
_roots = ["maja", "kooli", "aia", "puu", "kivi", "vee", "tee", "linna", "metsa", "auto"]
_words = {"".join(_roots[(n * 7919 // 10 ** k) % 10] for k in range(5)) for n in range(400)}
_text = "".join(f"XIIl {w} juures. " for w in sorted(_words))
_real, _calls = server._vabamorf, []


class _Counting:
    def analyze(self, **kwargs):
        _calls.append(kwargs["words"])
        return _real().analyze(**kwargs)


server._abbr_noun_forms.cache_clear()
server._vabamorf = _Counting
try:
    r = server.check_abbreviation_hyphenation(_text)
finally:
    server._vabamorf = _real
check(f"{len(_calls)} out-of-context lookups for {len(_words)} new words",
      0 < len(_calls) <= server._ABBR_LOOKUPS and len(r["issues"]) == len(_words),
      f"{len(_calls)} lookups, {len(r['issues'])} issues")

print("check_object_case")
# Negation rule
r = server.check_object_case("Ma ei söönud leib.")
check("negation flags non-partitive object",
      any(i["word"] == "leib" and i["rule"] == "negation-requires-partitive"
          for i in r["issues"]), str(r["issues"]))
clean = server.check_object_case("Ma ei söönud leiba.")
check("negation + correct partitive: no flag", len(clean["issues"]) == 0)
# Copula: predicative after negated 'olema' stays nominative (correct),
# so it must NOT be flagged as an object-case error.
cop = server.check_object_case("See ei ole raamat, vaid ajakiri.")
check("negated copula predicative NOT flagged", len(cop["issues"]) == 0, str(cop["issues"]))
# Partitive-only verb rule
r = server.check_object_case("Ma armastan koogi.")
check("partitive-only verb flags wrong-case object",
      any(i["word"] == "koogi" and i["rule"] == "partitive-only-verb"
          for i in r["issues"]), str(r["issues"]))
clean = server.check_object_case("Ma armastan kooki.")
check("partitive-only verb + correct partitive: no flag", len(clean["issues"]) == 0)
# FP guard: subject before negation should not be flagged
fp = server.check_object_case("Mees ei söönud leiba.")
check("subject before negation: NOT flagged", len(fp["issues"]) == 0,
      str(fp["issues"]))
fp = server.check_object_case("Tüdrukud ei näinud filmi.")
check("plural subject before negation: NOT flagged", len(fp["issues"]) == 0,
      str(fp["issues"]))
# Sentence with both subject and bad object: only object flagged
mixed = server.check_object_case("Mees ei söönud leib.")
check("only the wrong object flagged, not the subject",
      len(mixed["issues"]) == 1 and mixed["issues"][0]["word"] == "leib",
      str(mixed["issues"]))
# Locative case should not trigger
loc = server.check_object_case("Ma ei käinud poes.")
check("locative case not flagged", len(loc["issues"]) == 0, str(loc["issues"]))
# Proper nouns skipped
prop = server.check_object_case("Ma armastan Marit.")
check("proper noun not flagged", len(prop["issues"]) == 0, str(prop["issues"]))
# Estonian rule label present
r = server.check_object_case("Ma ei söönud leib.")
check("rule_estonian present on each issue",
      all(i.get("rule_estonian") for i in r["issues"]))
# The description tells the caller the fix is the partitive of `lemma` in
# the number `form` shows, so both must be there, plural included.
r = server.check_object_case("Ma ei söönud leivad.")
check("plural object: lemma and plural form given",
      [(i["lemma"], i["form"]) for i in r["issues"]] == [("leib", "pl n")],
      str(r["issues"]))
# The description says the negation rule skips a sentence with olema in
# it, and only that rule. Change both together.
r = server.check_object_case("Ma pole söönud leib.")
check("negated olema sentence: negation rule skipped", r["issues"] == [],
      str(r["issues"]))
r = server.check_object_case("Ma ei ole armastanud koogi.")
check("negated olema sentence: partitive-only verb still checked",
      [i["rule"] for i in r["issues"]] == ["partitive-only-verb"],
      str(r["issues"]))

print("check_redundancy")
# The exact case from the field: "Samuti ka suvesärgid" (samuti + ka
# both mean "also", a tautology).
r = server.check_redundancy("Samuti ka suvesärgid.")
check("flags 'samuti ka' doubling",
      any(i["rule"] == "doubled-also" for i in r["issues"]), str(r["issues"]))
# Double superlative (comparative form after kõige)
r = server.check_redundancy("See on kõige optimaalsem lahendus.")
check("flags 'kõige optimaalsem'",
      any(i["rule"] == "double-superlative" for i in r["issues"]), str(r["issues"]))
# Fixed pleonasm
r = server.check_redundancy("Pikk ajaline periood möödus.")
check("flags 'ajaline periood'",
      any(i["rule"] == "fixed-pleonasm" for i in r["issues"]), str(r["issues"]))
# Idiomatic: must NOT flag. "kõige parem" is the everyday analytic
# superlative (far more common than the synthetic "parim"); both are
# correct Estonian and neither is a redundancy, so neither should fire.
for phrase in ("See on kõige parem lahendus. Nüüd ka suvesärgid.",
               "See on kõige parim lahendus."):
    clean = server.check_redundancy(phrase)
    check(f"idiomatic superlative not flagged: {phrase!r}",
          len(clean["issues"]) == 0, str(clean["issues"]))
# Estonian rule label present
r = server.check_redundancy("Samuti ka.")
check("rule_estonian present", all(i.get("rule_estonian") for i in r["issues"]))

print("check_style")
heavy = (
    "Süsteem kasutab andmeid. Süsteemi kasutatakse osakondades. "
    "Andmed töödeldakse ja analüüsitakse. Võib-olla on lahendus "
    "pigem ajutine, ehk midagi tuleks tõenäoliselt vist muuta. "
    "Süsteem on hea."
)
r = server.check_style(heavy)
check("passive voice detected", r["passive_voice"]["passive_count"] >= 2,
      str(r["passive_voice"]))
check("passive ratio computed", 0 < r["passive_voice"]["ratio"] <= 1.0)
check("hedging detected", r["hedging"]["hedge_count"] >= 3,
      str(r["hedging"]))
check("hedging density computed", r["hedging"]["density"] > 0)
check("sentence_length has mean+stddev",
      "mean" in r["sentence_length"] and "stddev" in r["sentence_length"])
check("all four sub-checks have Estonian summary",
      all(r[k].get("summary_estonian") for k in
          ("repetition", "passive_voice", "sentence_length", "hedging")))
check("no input echo or static note", "text" not in r and "note" not in r,
      str(sorted(r)))
lemmas = [x["lemma"] for x in r["repetition"]["repeated_lemmas"]]
check("repetition summary names the lemmas as a plain list",
      lemmas and r["repetition"]["summary_estonian"].endswith(
          ": " + ", ".join(lemmas) + ".")
      and "[" not in r["repetition"]["summary_estonian"],
      r["repetition"]["summary_estonian"])
clean = "Eile käisin kinos. Film oli huvitav ja näitlejad mängisid hästi."
r = server.check_style(clean)
check("clean copy: no passive flagged", r["passive_voice"]["passive_count"] == 0)
check("clean copy: no hedging", r["hedging"]["hedge_count"] == 0)

print("classify_register consistency")
mixed = (
    "Käesoleva lepingu alusel sätestatakse poolte kohustused. "
    "Noh, kuule, see on lahe."
)
r = server.classify_register(mixed)
check("is_mixed flag true on register-mixed text",
      r["consistency"]["is_mixed"] is True,
      str(r["consistency"]))
check("consistency summary cites both marker sides",
      "ametlikke" in r["consistency"]["summary_estonian"]
      and "kõnekeelseid" in r["consistency"]["summary_estonian"])
formal_only = server.classify_register(
    "Käesoleva lepingu alusel sätestatakse kohustused vastavalt määratud korrale."
)
check("formal-only: is_mixed false", formal_only["consistency"]["is_mixed"] is False)

print("check_capitalization")
# The exact AI mistake that motivated the tool: 'Eesti keelt' mid-sentence.
r = server.check_capitalization(
    "AI-agendid ei leiutaks Eesti keelt vastates vigaseid käändvorme."
)
check(
    "flags 'Eesti' before 'keelt' as language-adjective",
    any(i["word"] == "Eesti" and i["rule"] == "language-adjective"
        for i in r["issues"]),
    str([(i["word"], i["rule"]) for i in r["issues"]]),
)
# Weekday + month + nationality, each in its own sentence.
r = server.check_capitalization(
    "Kohtume Esmaspäeval. Loodi Jaanuaris uus seadus. Olen Eestlane."
)
rules_seen = {i["rule"] for i in r["issues"]}
check("flags weekday Esmaspäeval", "weekday" in rules_seen, str(rules_seen))
check("flags month Jaanuaris", "month" in rules_seen, str(rules_seen))
check("flags nationality Eestlane", "nationality" in rules_seen, str(rules_seen))
# Sentence-initial Eesti is fine (could be the country).
clean = server.check_capitalization("Eesti keelt räägitakse Eestis.")
check("sentence start not flagged", len(clean["issues"]) == 0, str(clean["issues"]))
# All-caps acronym not flagged.
acronym = server.check_capitalization("Töötan NATO juures.")
check("acronym not flagged", len(acronym["issues"]) == 0, str(acronym["issues"]))
# Country proper-noun usage (no following culture noun) not flagged.
country = server.check_capitalization("Käisin Eestis suvel.")
check("country proper noun not flagged", len(country["issues"]) == 0, str(country["issues"]))
# rule_estonian must be populated for every issue (no English-only labels).
r = server.check_capitalization("Olen Eestlane ja räägin Eesti keelt Esmaspäeval.")
check(
    "every issue has rule_estonian",
    all(i.get("rule_estonian") for i in r["issues"]),
    str([i.get("rule_estonian") for i in r["issues"]]),
)

print("issue-list checks: result shape")
# A result is `issues` plus `summary_estonian`: no echo of the input, no
# static note. Each issue keeps rule, rule_estonian and suggestion (object
# case: lemma and form instead); only check_redundancy keeps an
# `explanation`, because its Estonian reason is not in any other field.
_SHAPE_CALLS = {
    "check_compounds": "Kooli maja on suur.",
    "check_punctuation": "Ma tean et see on hea.",
    "check_capitalization": "Olen Eestlane.",
    "check_numbers": "See maksis 3.14 eurot.",
    "check_redundancy": "Samuti ka. See on kõige optimaalsem. Pikk ajaline periood.",
    "check_object_case": "Ma ei söönud leib. Ma armastan koogi.",
    "check_abbreviation_hyphenation": "OÜle tuleb abi.",
}
for _name, _text in _SHAPE_CALLS.items():
    r = getattr(server, _name)(_text)
    check(f"{_name}: only issues and summary_estonian",
          set(r) == {"issues", "summary_estonian"}, str(sorted(r)))
    check(f"{_name}: the sample has issues", bool(r["issues"]), str(r))
    check(f"{_name}: every issue has rule and rule_estonian",
          all(i.get("rule") and i.get("rule_estonian") for i in r["issues"]),
          str(r["issues"]))
    check(f"{_name}: no suggestion_hint",
          not any("suggestion_hint" in i for i in r["issues"]), str(r["issues"]))
    if _name == "check_redundancy":
        check(f"{_name}: every issue keeps its Estonian explanation",
              all(i.get("explanation") for i in r["issues"]), str(r["issues"]))
        check(f"{_name}: all three rules in the sample",
              {i["rule"] for i in r["issues"]}
              == {"doubled-also", "double-superlative", "fixed-pleonasm"},
              str(r["issues"]))
    else:
        check(f"{_name}: no explanation",
              not any("explanation" in i for i in r["issues"]), str(r["issues"]))
    if _name == "check_object_case":
        check(f"{_name}: lemma and form on every issue, verb on the verb rule",
              all(i.get("lemma") and i.get("form") for i in r["issues"])
              and all(("verb" in i) == (i["rule"] == "partitive-only-verb")
                      for i in r["issues"]),
              str(r["issues"]))
    else:
        check(f"{_name}: every issue has a suggestion",
              all(i.get("suggestion") for i in r["issues"]), str(r["issues"]))

print("classify_register")
formal = server.classify_register(
    "Käesoleva lepingu alusel sätestatakse poolte kohustused vastavalt "
    "määratletud tingimustele."
)
check("formal text → formal tier", formal["tier"] == "formal", formal["tier"])
check("no static note", "note" not in formal, str(sorted(formal)))
check("formal text → positive score", formal["score"] > 0)
check(
    "formal text → tier_estonian='formaalne' (not the *formalne hallucination)",
    formal["tier_estonian"] == "formaalne",
    formal.get("tier_estonian", "<missing>"),
)
casual = server.classify_register(
    "Noh, kuule, see oli vinge mõnus üritus, ja kohvik oli ka lahe!"
)
check("colloquial text → colloquial tier", casual["tier"] == "colloquial", casual["tier"])
check("colloquial text → negative score", casual["score"] < 0)
check(
    "colloquial text → tier_estonian='kõnekeelne'",
    casual["tier_estonian"] == "kõnekeelne",
    casual.get("tier_estonian", "<missing>"),
)
neutral = server.classify_register(
    "Saadame teile uue uudiskirja, milles tutvustame meie sügisesi tooteid."
)
check("neutral text → neutral tier", neutral["tier"] == "neutral", neutral["tier"])
check(
    "neutral text → tier_estonian='neutraalne'",
    neutral["tier_estonian"] == "neutraalne",
    neutral.get("tier_estonian", "<missing>"),
)

print("input limits")
try:
    server.tokenize("a" * (server.MAX_TEXT_CHARS + 1))
    check("oversized input raises", False, "no exception")
except ValueError:
    check("oversized input raises", True)
try:
    server.syllabify("two words")
    check("syllabify rejects whitespace", False, "no exception")
except ValueError:
    check("syllabify rejects whitespace", True)

print("tool-call counter")
# Every tool exercised above should have registered in _TOOL_CALLS.
check("counter recorded calls", sum(server._TOOL_CALLS.values()) > 0, str(server._TOOL_CALLS))
check("spell_check counted", server._TOOL_CALLS.get("spell_check", 0) > 0)
check("paradigm counted", server._TOOL_CALLS.get("paradigm", 0) > 0)
# Counter increments by exactly one per call.
before = server._TOOL_CALLS.get("tokenize", 0)
server.tokenize("Üks lause.")
check("increments by exactly 1", server._TOOL_CALLS.get("tokenize", 0) == before + 1,
      f"{before} -> {server._TOOL_CALLS.get('tokenize', 0)}")

print("schema quality (Smithery score guard)")
import asyncio as _asyncio  # noqa: E402  (deliberate: guard runs after the tool suite)

_tools = _asyncio.run(server.mcp.list_tools())
_missing_desc = [
    f"{t.name}.{p}"
    for t in _tools
    for p, sch in t.input_schema.get("properties", {}).items()
    if not sch.get("description")
]
_missing_out = [t.name for t in _tools if not t.output_schema]
check("every tool parameter has a description", not _missing_desc, str(_missing_desc))
check("every tool has an output schema", not _missing_out, str(_missing_out))

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nall smoke tests passed")
