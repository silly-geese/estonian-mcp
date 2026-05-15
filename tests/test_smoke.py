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
    any(m["word"] in {"jook", "õlu", "piim", "alkohol", "tee"} for m in related["matches"]),
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
