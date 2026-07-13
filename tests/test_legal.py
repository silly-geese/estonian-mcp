"""Tests for the legal-Estonian tools (check_legalese, check_defined_terms).

Both are pure (Vabamorf morphology + regex, no fastText/WordNet), so they run
locally without the model artifacts. The check_compound_familiarity legal
de-noise assertion lives in test_smoke.py (it needs the fastText model, which
only CI has).

Run via:

    uv run python tests/test_legal.py
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


def legalese_tests() -> None:
    print("check_legalese")
    para = ("Käesolevas seaduses sätestatut kohaldatakse võlasuhetele. "
            "Juhul kui võlgnik ei täida kohustust, tekib võlausaldaja ees "
            "vastutus ning õigus nõuda kahjuhüvitist.")
    r = server.check_legalese(para)
    rules = {i["rule"] for i in r["issues"]}
    check("flags 'käesolev' archaic filler", "archaic-filler" in rules, str(r["issues"]))
    check("flags 'juhul kui' phrase", "legalese-phrase" in rules, str(r["issues"]))
    # käesolev suggestion is the plain 'see'
    kaes = next((i for i in r["issues"] if i["rule"] == "archaic-filler"), {})
    check("käesolev → see", kaes.get("suggestion") == "see", str(kaes))
    # terms of art are surfaced and NOT flagged as filler
    lemmas = {t["lemma"] for t in r["terms_of_art"]}
    check("protects legal term võlgnik", "võlgnik" in lemmas, str(lemmas))
    check("protects several terms of art", len(r["terms_of_art"]) >= 4, str(lemmas))
    check("no legal term is flagged as filler",
          not any(server._is_legal_term(i.get("word", "")) for i in r["issues"] if "word" in i),
          str(r["issues"]))

    # Plain modern Estonian: no archaic-filler flags, no terms of art.
    clean = server.check_legalese("See on lühike ja selge lause.")
    check("plain text → no archaic-filler",
          not any(i["rule"] == "archaic-filler" for i in clean["issues"]), str(clean["issues"]))

    # Over-long sentence → complex-sentence flag.
    long_s = ("Pool kohustub, arvestades kõiki asjaolusid, tingimusi, tähtaegu "
              "ja erandeid, mis tulenevad seadusest, lepingust, tavast ning "
              "kohtupraktikast, täitma oma kohustused nõuetekohaselt, õigel ajal "
              "ja täies ulatuses ilma põhjendamatu viivituseta.")
    ls = server.check_legalese(long_s)
    check("flags over-long sentence", any(i["rule"] == "complex-sentence" for i in ls["issues"]),
          str([i["rule"] for i in ls["issues"]]))


def defined_terms_tests() -> None:
    print("check_defined_terms")
    contract = ('Käesoleva lepingu (edaspidi «Leping») pooled on AS Müük '
                '(edaspidi «Müüja») ja OÜ Ostja (edaspidi «Ostja»). Müüja annab '
                'kauba üle. Ostja tasub hinna vastavalt Lepingu punkt 3 ja § 5 lg 2. '
                'Pooled (edaspidi «Pooled») kinnitavad, et Leping jõustub.')
    d = server.check_defined_terms(contract)
    defined = {t["term"] for t in d["defined_terms"]}
    check("extracts «Leping»", "Leping" in defined, str(defined))
    check("extracts «Müüja» and «Ostja»", {"Müüja", "Ostja"} <= defined, str(defined))
    xrefs = {x["reference"] for x in d["cross_references"]}
    check("finds 'punkt 3' cross-ref", any("punkt 3" in x for x in xrefs), str(xrefs))
    check("finds '§ 5' cross-ref", any(x.startswith("§") for x in xrefs), str(xrefs))
    # every defined term here is used again → no unused issues
    check("used terms → no defined-but-unused",
          not any(i["rule"] == "defined-but-unused" for i in d["issues"]), str(d["issues"]))

    # defined-but-unused detection
    unused = server.check_defined_terms("Käesolev kokkulepe (edaspidi «Kokkulepe») jõustub täna.")
    check("flags defined-but-unused",
          any(i["rule"] == "defined-but-unused" for i in unused["issues"]), str(unused["issues"]))

    # duplicate definition
    dup = server.check_defined_terms(
        "Pool (edaspidi «Müüja») ja teine (edaspidi «Müüja»). Müüja tegutseb.")
    check("flags duplicate-definition",
          any(i["rule"] == "duplicate-definition" for i in dup["issues"]), str(dup["issues"]))

    # long document under the raised cap does not raise
    big = "Käesoleva lepingu punkt 1. " * 20000  # ~540k? keep under cap
    big = big[:server.MAX_DOC_CHARS - 10]
    try:
        server.check_defined_terms(big)
        check("accepts long doc under MAX_DOC_CHARS", True)
    except Exception as e:
        check("accepts long doc under MAX_DOC_CHARS", False, str(e))
    # over the doc cap raises
    try:
        server.check_defined_terms("a" * (server.MAX_DOC_CHARS + 1))
        check("over MAX_DOC_CHARS raises", False, "no exception")
    except ValueError:
        check("over MAX_DOC_CHARS raises", True)


def is_legal_term_tests() -> None:
    print("_is_legal_term")
    check("solidaarvõlgnik is legal", server._is_legal_term("solidaarvõlgnik"))
    check("inflected võlasuhetele is legal", server._is_legal_term("võlasuhetele"))
    check("hagiavaldus (compound head) is legal", server._is_legal_term("hagiavaldus"))
    check("ordinary word 'koer' is not legal", not server._is_legal_term("koer"))
    check("ordinary word 'ilus' is not legal", not server._is_legal_term("ilus"))


def common_legal_usage_tests() -> None:
    print("common_legal_usage")
    r = server.common_legal_usage("kohustus")
    check("known legal term found", r["found"] is True, str(r.get("summary_estonian")))
    check("has a frequency", isinstance(r["frequency"], int) and r["frequency"] > 0, str(r["frequency"]))
    check("common_after is list of {word,count}",
          all("word" in x and "count" in x for x in r["common_after"]), str(r["common_after"]))
    after = {x["word"] for x in r["common_after"]}
    check("canonical 'kohustuse täitmine' present", "täitmine" in after, str(after))

    r2 = server.common_legal_usage("hagi")
    before = {x["word"] for x in r2["common_before"]}
    check("hagi has collocations", r2["found"] and (before or r2["common_after"]), str(r2))

    # Non-legal word → not found, empty lists, no crash.
    nf = server.common_legal_usage("koer")
    check("non-legal word → found False", nf["found"] is False, str(nf))
    check("not-found → empty collocations", nf["common_before"] == [] and nf["common_after"] == [])

    # Single-word contract enforced.
    try:
        server.common_legal_usage("kaks sõna")
        check("rejects whitespace", False, "no exception")
    except ValueError:
        check("rejects whitespace", True)


legalese_tests()
defined_terms_tests()
is_legal_term_tests()
common_legal_usage_tests()

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nall legal-tool tests passed")
