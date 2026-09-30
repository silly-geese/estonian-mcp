"""Unit tests for the domain glossary behind check_domain_terms.

The decision (`_domain_terms_from`) and the stamp on check_compound_familiarity
(`_domain_mark`) are pure functions, so they are tested here WITHOUT the
fastText model: the compound dicts below have the shape
_check_compound_familiarity produces, reduced to the fields the decision reads.

Run via:

    uv run python tests/test_domain_terms.py
"""
from __future__ import annotations

import os
import sys
import tempfile
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


def compound(lemma: str, attested: bool) -> dict:
    return {"word": lemma, "lemma": lemma, "position": 0, "attested": attested}


GLOSSARY = frozenset({"vagunireis", "veosegment", "üleandmisleht"})

print("off by default: no glossary, no new field")
c = compound("vagunireis", False)
server._domain_mark(c, frozenset())
check("no in_domain_glossary without a glossary", "in_domain_glossary" not in c, str(c))
os.environ.pop("ESTNLTK_MCP_DOMAIN_GLOSSARY", None)
server._domain_glossary.cache_clear()
check("unset env var loads an empty glossary", server._domain_glossary() == frozenset())

print("with a glossary, the field is stamped")
c = compound("vagunireis", False)
server._domain_mark(c, GLOSSARY)
check("listed lemma marked True", c.get("in_domain_glossary") is True, str(c))
c = compound("vagunireisiräsi", False)
server._domain_mark(c, GLOSSARY)
check("unlisted lemma marked False", c.get("in_domain_glossary") is False, str(c))

print("a glossary term is never reported")
out = server._domain_terms_from([compound("vagunireis", False)], GLOSSARY, GLOSSARY)
check("unattested but listed passes", out == [], str(out))

print("an attested compound is never reported")
out = server._domain_terms_from([compound("koolimaja", True)], GLOSSARY, GLOSSARY)
check("attested passes", out == [], str(out))

print("an unattested, unlisted compound is reported with a suggestion")
out = server._domain_terms_from([compound("vagunireisid", False)], GLOSSARY, GLOSSARY)
check("reported", len(out) == 1, str(out))
check("suggests the glossary term", out and out[0]["suggestions"][:1] == ["vagunireis"], str(out))

print("nothing close means no suggestion, still reported")
out = server._domain_terms_from([compound("mõtteliin", False)], GLOSSARY, GLOSSARY)
check("reported without suggestions", len(out) == 1 and out[0]["suggestions"] == [], str(out))

print("identifiers are allowed but never suggested")
ids = frozenset({"kaupkood"})
out = server._domain_terms_from([compound("kaupkood", False)], GLOSSARY, GLOSSARY | ids)
check("identifier passes", out == [], str(out))

print("the glossary file: comments, blanks and case")
with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
    f.write("# EVR terms\nVagunireis\n\nveosegment  # step 4.7\n")
    path = f.name
terms = server._read_term_file(path)
check("parsed", terms == frozenset({"vagunireis", "veosegment"}), str(sorted(terms)))
os.environ["ESTNLTK_MCP_DOMAIN_GLOSSARY"] = path
server._domain_glossary.cache_clear()
check("env var loads the file", server._domain_glossary() == terms)
os.environ.pop("ESTNLTK_MCP_DOMAIN_GLOSSARY", None)
server._domain_glossary.cache_clear()
os.unlink(path)

print("a per-call glossary extends the server one")
calls: list[str] = []


def fake_familiarity(text: str) -> dict:
    calls.append(text)
    return {"compounds_analysed": 1, "all_compounds": [compound("veosegment", False)]}


real = server._check_compound_familiarity
server._check_compound_familiarity = fake_familiarity
try:
    out = server._check_domain_terms("veosegment", None)
    check("without it, the term is reported", len(out["unlisted_compounds"]) == 1, str(out))
    out = server._check_domain_terms("veosegment", ["Veosegment"])
    check("with it, the term passes", out["unlisted_compounds"] == [], str(out))
    check("glossary_size counts the call's terms", out["glossary_size"] == 1, str(out))
finally:
    server._check_compound_familiarity = real

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nall passed")
