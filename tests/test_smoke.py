"""Smoke tests for the EstNLTK MCP tools.

Calls each tool's underlying function directly (no MCP transport) to
prove the EstNLTK wiring is correct. Run via:

    uv run python tests/test_smoke.py

Exits non-zero on any failure. CI uses this as the gate.
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


SAMPLE = "Mina sõinn täna hommikul Tallinnas putru."

print("tokenize")
tok = server.tokenize(SAMPLE)
check("returns sentences+words", "sentences" in tok and "words" in tok)
check("non-empty words", len(tok["words"]) >= 6)

print("lemmatize")
lem = server.lemmatize("Mina sõin täna hommikul Tallinnas putru.")
lemmas = {row["lemma"] for row in lem}
check("recognises 'sööma'", "sööma" in lemmas, str(lemmas))
check("recognises 'Tallinn'", "Tallinn" in lemmas, str(lemmas))
check("recognises 'puder'", "puder" in lemmas, str(lemmas))

print("pos_tag")
pos = server.pos_tag(SAMPLE)
check("first tokens are non-empty POS", all(row["partofspeech"] for row in pos[:5]))

print("analyze_morphology")
morph = server.analyze_morphology("Tere maailm!")
check("compound 'maailm' splits", any(row.get("root_tokens") == ["maa", "ilm"] for row in morph))
check(
    "ambiguity surfaced via analyses_count + is_ambiguous fields",
    all("analyses_count" in row and "is_ambiguous" in row for row in morph),
)
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
try:
    server.paradigm("two words")
    check("paradigm rejects whitespace", False, "no exception")
except ValueError:
    check("paradigm rejects whitespace", True)

print("spell_check")
sp = server.spell_check(SAMPLE)
bad = [row for row in sp if not row["spelling"]]
check("flags 'sõinn' as misspelled", any(row["text"] == "sõinn" for row in bad))
suggestions = next((row["suggestions"] for row in bad if row["text"] == "sõinn"), [])
check("suggests 'sõin'", "sõin" in suggestions, str(suggestions))

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
# similarity ~0.536 — exactly the AI failure mode this tool catches.
r = server.check_compound_familiarity(
    "See on mõtteliin, mis viib eesmärgini."
)
check("mõtteliin analysed as compound",
      any(c["lemma"] == "mõtteliin" for c in r["all_compounds"]),
      str([c["lemma"] for c in r["all_compounds"]]))
check("mõtteliin flagged as suspect",
      any(c["lemma"] == "mõtteliin" and c["is_suspect"] for c in r["all_compounds"]),
      str(r["suspect_compounds"]))
r = server.check_compound_familiarity("Käisin raamatukogus ja koolimajas.")
check("real compounds analysed", r["compounds_analysed"] == 2)
check("real compounds NOT suspect", len(r["suspect_compounds"]) == 0,
      str(r["suspect_compounds"]))
r = server.check_compound_familiarity("Eile käisin poes ja ostsin leiba.")
check("no compounds → empty analysis", r["compounds_analysed"] == 0)
r = server.check_compound_familiarity(
    "See on mõtteliin."
)
for c in r["all_compounds"]:
    if c["lemma"] == "mõtteliin":
        check("neighbours list populated", len(c["neighbours"]) > 0, str(c))
        check("top_score is float", isinstance(c["top_score"], float))
        break

print("check_abbreviation_hyphenation")
r = server.check_abbreviation_hyphenation(
    "Jooksutasin teksti Estonian MCPst läbi."
)
check("flags MCPst → MCP-st",
      any(i["word"] == "MCPst" and i["suggestion"] == "MCP-st"
          for i in r["issues"]), str(r["issues"]))
r = server.check_abbreviation_hyphenation(
    "APIga ühendamine ja MCPst lugemine on lihtne."
)
check("flags multiple abbreviations", len(r["issues"]) == 2, str(r["issues"]))
clean = server.check_abbreviation_hyphenation(
    "Saatsin kirja OÜ-le ja kasutasin API-t."
)
check("hyphenated forms not flagged", len(clean["issues"]) == 0, str(clean["issues"]))
nom = server.check_abbreviation_hyphenation("Tegu on MCP serveriga.")
check("bare nominative MCP not flagged", len(nom["issues"]) == 0, str(nom["issues"]))
plain = server.check_abbreviation_hyphenation("Käisin poes ja ostsin leiba.")
check("plain text: no flags", len(plain["issues"]) == 0, str(plain["issues"]))
r = server.check_abbreviation_hyphenation("Lugesin MCPst.")
check("rule_estonian populated",
      all(i.get("rule_estonian") for i in r["issues"]),
      str([i.get("rule_estonian") for i in r["issues"]]))

print("check_object_case")
# Negation rule
r = server.check_object_case("Ma ei söönud leib.")
check("negation flags non-partitive object",
      any(i["word"] == "leib" and i["rule"] == "negation-requires-partitive"
          for i in r["issues"]), str(r["issues"]))
clean = server.check_object_case("Ma ei söönud leiba.")
check("negation + correct partitive: no flag", len(clean["issues"]) == 0)
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

print("check_redundancy")
# The exact case from the field: "Samuti ka suvesärgid" — samuti + ka
# both mean "also", a tautology.
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
# Idiomatic — must NOT flag
clean = server.check_redundancy("See on kõige parim lahendus. Nüüd ka suvesärgid.")
check("'kõige parim' + lone 'ka' not flagged", len(clean["issues"]) == 0, str(clean["issues"]))
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

print("classify_register")
formal = server.classify_register(
    "Käesoleva lepingu alusel sätestatakse poolte kohustused vastavalt "
    "määratletud tingimustele."
)
check("formal text → formal tier", formal["tier"] == "formal", formal["tier"])
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

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nall smoke tests passed")
