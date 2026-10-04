"""Tests for the domain glossary behind check_domain_terms (issue #67).

Most of the decision is pure functions, tested here WITHOUT the fastText
model: the compound dicts below have the shape _check_compound_familiarity
produces on check_domain_terms' path, reduced to the fields the decision
reads. `_domain_part_of` uses Vabamorf, which ships inside the EstNLTK wheel.

The last section runs the real tools end to end, so it needs the resources
(`uv run python scripts/fetch_resources.py`), as CI's smoke job has them.

Run via:

    uv run python tests/test_domain_terms.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server

failures: list[str] = []
GLOSSARY_ENV = "ESTNLTK_MCP_DOMAIN_GLOSSARY"
IDENTIFIERS_ENV = "ESTNLTK_MCP_DOMAIN_IDENTIFIERS"
CUTOFF_ENV = "ESTNLTK_MCP_DOMAIN_SUGGEST_CUTOFF"


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS {label}")
    else:
        failures.append(f"{label}: {detail}")
        print(f"  FAIL {label} {detail}")


def compound(lemma: str, attested: bool, forms=(), parts=(), suspect=False) -> dict:
    return {"word": lemma, "lemma": lemma, "position": 0, "attested": attested,
            "in_wordnet": False, "forms": sorted({lemma, *forms}),
            "parts": list(parts), "is_suspect": suspect, "reasons": []}


def reset(**env: str | None) -> None:
    """Set or clear the domain env vars and drop the cached files."""
    for name in (GLOSSARY_ENV, IDENTIFIERS_ENV, CUTOFF_ENV):
        os.environ.pop(name, None)
    for name, value in env.items():
        if value is not None:
            os.environ[name] = value
    server._domain_glossary.cache_clear()
    server._domain_identifiers.cache_clear()


def term_file(content: str | bytes) -> str:
    data = content.encode("utf-8") if isinstance(content, str) else content
    with tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False) as f:
        f.write(data)
    return f.name


GLOSSARY = frozenset({"vagunireis", "veosegment", "üleandmisleht", "ladustamiskoht"})


def off_by_default() -> None:
    print("off by default: no glossary, no new field")
    reset()
    c = compound("vagunireis", False)
    server._domain_mark(c, {"vagunireis"}, frozenset())
    check("no in_domain_glossary without a glossary", "in_domain_glossary" not in c, str(c))
    check("unset env var loads an empty glossary", server._domain_glossary() == frozenset())
    reset(**{GLOSSARY_ENV: "   "})
    check("a blank env var is unset too", server._domain_glossary() == frozenset())


def the_stamp() -> None:
    print("with a glossary, the stamp on check_compound_familiarity")
    c = server._domain_mark(compound("vagunireis", False), {"vagunireis"}, GLOSSARY)
    check("listed lemma marked True", c.get("in_domain_glossary") is True, str(c))
    c = server._domain_mark(compound("vagunisõit", False), {"vagunisõit"}, GLOSSARY)
    check("unlisted lemma marked False", c.get("in_domain_glossary") is False, str(c))
    # Vabamorf's FIRST reading of `ladustamiskohad` is `ladustamiskoha`.
    c = server._domain_mark(compound("ladustamiskoha", False), {"ladustamiskoha", "ladustamiskoht", "ladustamiskohad"}, GLOSSARY)
    check("a later lemma reading counts", c.get("in_domain_glossary") is True, str(c))
    c = compound("vagunireis", False, suspect=True)
    c["reasons"] = ["weak neighbour"]
    server._domain_mark(c, {"vagunireis"}, GLOSSARY)
    check("a listed compound is never suspect",
          c["is_suspect"] is False and c["reasons"] == [], str(c))


def the_decision() -> None:
    print("check_domain_terms: what is reported")
    out = server._domain_terms_from([compound("vagunireis", False)], GLOSSARY, GLOSSARY)
    check("unattested but listed passes", out == [], str(out))
    out = server._domain_terms_from(
        [compound("ladustamiskoha", False, forms={"ladustamiskoht", "ladustamiskohad"})],
        GLOSSARY, GLOSSARY)
    check("listed under a later reading passes (acceptance b)", out == [], str(out))
    out = server._domain_terms_from([compound("koolimaja", True)], GLOSSARY, GLOSSARY)
    check("attested passes", out == [], str(out))

    out = server._domain_terms_from([compound("vagunisõit", False)], GLOSSARY, GLOSSARY)
    check("an unlisted compound is reported", len(out) == 1, str(out))
    sugg = out[0]["suggestions"] if out else []
    check("with the glossary term as a suggestion (acceptance c)",
          [s["term"] for s in sugg][:1] == ["vagunireis"], str(sugg))
    check("and its similarity", sugg and sugg[0]["similarity"] == 0.7, str(sugg))
    check("the entry has the documented keys",
          out and set(out[0]) == {"word", "lemma", "position", "in_wordnet",
                                  "contains_glossary_term", "suggestions"}, str(out))

    out = server._domain_terms_from([compound("mõtteliin", False)], GLOSSARY, GLOSSARY)
    check("nothing close: reported, no suggestions",
          len(out) == 1 and out[0]["suggestions"] == [], str(out))

    print("the threshold is configurable")
    out = server._domain_terms_from([compound("vagunisõit", False)], GLOSSARY, GLOSSARY, cutoff=0.75)
    check("0.75 drops a 0.70 look-alike", out and out[0]["suggestions"] == [], str(out))
    out = server._domain_terms_from([compound("vagunisõit", False)], GLOSSARY, GLOSSARY, cutoff=0.7)
    check("0.70 keeps it", out and len(out[0]["suggestions"]) == 1, str(out))

    print("identifiers: allowed, matched as written, never suggested")
    ids = frozenset({"tellimusread", "kaupkood"})
    allowed = GLOSSARY | ids
    out = server._domain_terms_from([compound("kaupkood", False)], GLOSSARY, allowed)
    check("an identifier passes", out == [], str(out))
    out = server._domain_terms_from(
        [compound("tellimusrida", False, forms={"tellimusread"})], GLOSSARY, allowed)
    check("an identifier matches the word as written", out == [], str(out))
    out = server._domain_terms_from([compound("kaupkoodid", False)], GLOSSARY, allowed)
    check("an identifier is never a suggestion",
          out and all(s["term"] != "kaupkood" for s in out[0]["suggestions"]), str(out))

    print("a compound built on a glossary term")
    c = compound("vagunireisitabel", False, parts=["vaguni", "reisi", "tabel"])
    out = server._domain_terms_from([c], GLOSSARY, GLOSSARY)
    check("names the term it is built on",
          out and out[0]["contains_glossary_term"] == "vagunireis", str(out))
    check("and does not offer it as a replacement",
          out and all(s["term"] != "vagunireis" for s in out[0]["suggestions"]), str(out))
    c = compound("andmevagunireis", False, parts=["andme", "vaguni", "reis"])
    out = server._domain_terms_from([c], GLOSSARY, GLOSSARY)
    check("the term can be the head",
          out and out[0]["contains_glossary_term"] == "vagunireis", str(out))
    c = compound("vagunisõit", False, parts=["vaguni", "sõit"])
    out = server._domain_terms_from([c], GLOSSARY, GLOSSARY)
    check("a look-alike is not built on the term",
          out and out[0]["contains_glossary_term"] is None, str(out))

    c = compound("eid-kaardilugeja", False, parts=["eID", "kaardi", "lugeja"])
    out = server._domain_terms_from([c], frozenset({"eid-kaart"}), frozenset({"eid-kaart"}))
    check("a part that keeps its capitals still cuts (eID)",
          out and out[0]["contains_glossary_term"] == "eid-kaart"
          and out[0]["suggestions"] == [], str(out))
    c = compound("kliendikontoleht", False, parts=["kliendi", "konto", "leht"])
    terms = frozenset({"kliendikonto", "kontoleht"})
    out = server._domain_terms_from([c], terms, terms)
    check("built on two terms: the longest is named",
          out and out[0]["contains_glossary_term"] == "kliendikonto", str(out))
    check("and neither is offered as a replacement",
          out and out[0]["suggestions"] == [], str(out))

    terms = frozenset({"vagunireisa", "vagunireisb", "vagunireisc", "vagunireisd"})
    out = server._domain_terms_from([compound("vagunireise", False)], terms, terms)
    check("at most three suggestions", out and len(out[0]["suggestions"]) == 3, str(out))
    check("best first, as difflib orders them",
          out and [s["term"] for s in out[0]["suggestions"]] == ["vagunireisd", "vagunireisc", "vagunireisb"],
          str(out))

    print("suggestions are capped, reports are not")
    out = server._domain_terms_from(
        [compound("vagunisõit", False), compound("vaguniretk", False)],
        GLOSSARY, GLOSSARY, cap=1)
    check("both reported", len(out) == 2, str(out))
    check("the first has suggestions, the second None",
          out and out[0]["suggestions"] and out[1]["suggestions"] is None, str(out))
    out = server._domain_terms_from([compound("vagunisõit", False)], GLOSSARY, GLOSSARY, cap=1)
    check("exactly at the cap, still suggested", out and out[0]["suggestions"] is not None, str(out))
    out = server._domain_terms_from(
        [compound("vagunisõit", False), compound("vaguniretk", False)],
        GLOSSARY, GLOSSARY, seconds=0)
    check("past the time budget: reported without suggestions",
          len(out) == 2 and all(u["suggestions"] is None and u["contains_glossary_term"] is None for u in out),
          str(out))


def the_file() -> None:
    print("the glossary file")
    path = term_file("# EVR terms\r\nVagunireis\r\n\r\nveosegment  # step 4.7\r\n")
    terms = server._read_term_file(path)
    check("comments, blanks, case and CRLF",
          terms == frozenset({"vagunireis", "veosegment"}), str(sorted(terms)))
    os.unlink(path)

    path = term_file(b"\xef\xbb\xbfvagunireis\n")
    terms = server._read_term_file(path)
    check("a UTF-8 BOM does not swallow the first term",
          terms == frozenset({"vagunireis"}), repr(sorted(terms)))
    os.unlink(path)

    path = term_file(unicodedata.normalize("NFD", "üleandmisleht\n"))
    terms = server._read_term_file(path)
    check("an NFD file matches NFC text", terms == frozenset({"üleandmisleht"}), repr(sorted(terms)))
    os.unlink(path)

    path = term_file("vagunireis\n")
    reset(**{GLOSSARY_ENV: path})
    check("the env var loads the file", server._domain_glossary() == frozenset({"vagunireis"}))
    os.unlink(path)

    print("a file that cannot be read")
    missing = "/nonexistent/secret-org/terms.txt"
    reset(**{GLOSSARY_ENV: missing})
    try:
        server._domain_glossary()
        check("raises", False, "no error")
    except server._DomainVocabUnreadable as e:
        check("raises, naming the env var", GLOSSARY_ENV in str(e), str(e))
        check("and not the path, which can reach callers", missing not in str(e), str(e))
    check("the older tools fall back to no glossary",
          server._domain_glossary_or_empty() == frozenset())
    try:
        server._load_domain_vocab(public=False)
        check("startup refuses it", False, "no exit")
    except SystemExit as e:
        check("startup refuses it with exit 2", e.code == 2, str(e.code))
    reset(**{GLOSSARY_ENV: "~nosuchuserxyz/terms.txt"})
    try:
        server._load_domain_vocab(public=False)
        check("startup refuses an unknown ~user path", False, "no exit")
    except SystemExit as e:
        check("startup refuses an unknown ~user path with exit 2", e.code == 2, str(e.code))
    path = term_file(b"\xff\xfev\x00a\x00")  # UTF-16, not UTF-8
    reset(**{IDENTIFIERS_ENV: path})
    try:
        server._load_domain_vocab(public=False)
        check("startup refuses a non-UTF-8 identifier list", False, "no exit")
    except SystemExit as e:
        check("startup refuses a non-UTF-8 identifier list", e.code == 2, str(e.code))
    os.unlink(path)
    reset()
    try:
        server._load_domain_vocab(public=True)
        check("startup with nothing configured passes", True)
    except SystemExit as e:
        check("startup with nothing configured passes", False, str(e.code))

    print("startup logs counts, never terms, and warns in public mode")
    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Keep()
    server.log.addHandler(handler)
    path = term_file("salajanevagunireis\n")
    try:
        reset(**{GLOSSARY_ENV: path})
        server._load_domain_vocab(public=False)
        messages = [r.getMessage() for r in records]
        check("no warning outside public mode",
              not any("public mode" in m for m in messages), str(messages))
        check("the count is logged", any(": 1 terms" in m for m in messages), str(messages))
        server._load_domain_vocab(public=True)
        messages = [r.getMessage() for r in records]
        check("a warning in public mode", any("public mode" in m for m in messages), str(messages))
        check("no term reaches the log",
              not any("salajanevagunireis" in m for m in messages), str(messages))
    finally:
        server.log.removeHandler(handler)
        os.unlink(path)
        reset()

    print("main() reads the vocabulary before serving, on both transports")
    for name in ("ESTNLTK_MCP_PUBLIC_MODE", "ESTNLTK_MCP_TRANSPORT"):
        os.environ.pop(name, None)
    seen: list[bool] = []
    order: list[str] = []
    saved = (server._load_domain_vocab, server._run_http, server.mcp.run)
    server._load_domain_vocab = lambda public: (seen.append(public), order.append("vocab"))
    server._run_http = lambda *a, **k: order.append("serve")
    server.mcp.run = lambda *a, **k: order.append("serve")
    try:
        server.main(["--transport", "stdio"])
        server.main(["--transport", "http", "--public"])
        check("vocabulary before serving", order == ["vocab", "serve", "vocab", "serve"], str(order))
        check("public only for public HTTP", seen == [False, True], str(seen))
    finally:
        server._load_domain_vocab, server._run_http, server.mcp.run = saved


def the_cutoff() -> None:
    print("ESTNLTK_MCP_DOMAIN_SUGGEST_CUTOFF")
    reset()
    check("default 0.6", server._domain_suggest_cutoff() == 0.6)
    reset(**{CUTOFF_ENV: "0.75"})
    check("read from the env", server._domain_suggest_cutoff() == 0.75)
    reset(**{CUTOFF_ENV: " 0.4 "})
    check("the floor itself is allowed", server._domain_suggest_cutoff() == 0.4)
    for bad in ("abc", "1.5", "-0.1", "nan", "0.3", "0"):
        reset(**{CUTOFF_ENV: bad})
        try:
            server._domain_suggest_cutoff()
            check(f"{bad!r} is refused", False, "accepted")
        except ValueError:
            check(f"{bad!r} is refused", True)
    reset(**{CUTOFF_ENV: "x"})
    try:
        server._load_domain_vocab(public=False)
        check("startup refuses a bad cutoff", False, "no exit")
    except SystemExit as e:
        check("startup refuses a bad cutoff with exit 2", e.code == 2, str(e.code))
    reset()


def the_call_glossary() -> None:
    print("a per-call glossary is bounded and normalised")
    terms = server._call_glossary([" Vagunireis ", "", "  ", unicodedata.normalize("NFD", "Üleandmisleht")])
    check("normalised, blanks dropped",
          terms == frozenset({"vagunireis", "üleandmisleht"}), repr(sorted(terms)))
    check("None is empty", server._call_glossary(None) == frozenset())
    try:
        server._call_glossary(["a"] * (server.MAX_GLOSSARY_TERMS + 1))
        check("too many terms are refused", False, "accepted")
    except ValueError:
        check("too many terms are refused", True)
    try:
        server._call_glossary(["a" * (server.MAX_WORD_CHARS + 1)])
        check("an overlong term is refused", False, "accepted")
    except ValueError:
        check("an overlong term is refused", True)
    try:
        server._call_glossary(["İ" * server.MAX_WORD_CHARS])
        check("a term that lengthens when lowered is refused", False, "accepted")
    except ValueError:
        check("a term that lengthens when lowered is refused", True)
    check("the limits themselves are allowed",
          len(server._call_glossary([f"termin{i}" for i in range(server.MAX_GLOSSARY_TERMS)])) == server.MAX_GLOSSARY_TERMS
          and server._call_glossary(["a" * server.MAX_WORD_CHARS]) == frozenset({"a" * server.MAX_WORD_CHARS}))

    print("and extends the server's for that call only")
    reset()
    calls: list[tuple[str, bool]] = []

    def fake_familiarity(text: str, *, neighbours: bool = True) -> dict:
        calls.append((text, neighbours))
        return {"compounds_analysed": 1, "wordnet_checked": True,
                "all_compounds": [compound("veosegment", False)]}

    real = server._check_compound_familiarity
    server._check_compound_familiarity = fake_familiarity
    try:
        out = server._check_domain_terms("veosegment", None)
        check("without it, the term is reported", len(out["unlisted_compounds"]) == 1, str(out))
        check("and the neighbour search is skipped", calls and calls[-1][1] is False, str(calls))
        out = server._check_domain_terms("veosegment", ["Veosegment"])
        check("with it, the term passes", out["unlisted_compounds"] == [], str(out))
        check("glossary_size counts the call's terms", out["glossary_size"] == 1, str(out))
        out = server._check_domain_terms("veosegment", None)
        check("and nothing is kept for the next call",
              out["glossary_size"] == 0 and len(out["unlisted_compounds"]) == 1, str(out))
        for bad in (2.0, 0.1):
            try:
                server._check_domain_terms("veosegment", None, suggest_cutoff=bad)
                check(f"a cutoff of {bad} is refused", False, "accepted")
            except ValueError:
                check(f"a cutoff of {bad} is refused", True)

        print("the cutoff reaches the decision")
        unlisted = {"compounds_analysed": 1, "wordnet_checked": True,
                  "all_compounds": [compound("vagunisõit", False)]}
        server._check_compound_familiarity = lambda text, *, neighbours=True: unlisted
        out = server._check_domain_terms("x", ["vagunireis"], suggest_cutoff=0.75)
        check("per call: 0.75 drops a 0.70 look-alike",
              out["suggest_cutoff"] == 0.75 and out["unlisted_compounds"][0]["suggestions"] == [], str(out))
        reset(**{CUTOFF_ENV: "0.75"})
        out = server._check_domain_terms("x", ["vagunireis"])
        check("from the env: 0.75 drops it", out["unlisted_compounds"][0]["suggestions"] == [], str(out))
        out = server._check_domain_terms("x", ["vagunireis"], suggest_cutoff=0.6)
        check("per call overrides the env", len(out["unlisted_compounds"][0]["suggestions"]) == 1, str(out))
        reset()

        print("suggestions_capped")
        many = {"compounds_analysed": 201, "wordnet_checked": True,
                "all_compounds": [compound(f"vagunisõit{i}", False) for i in range(server._DOMAIN_SUGGEST_CAP + 1)]}
        server._check_compound_familiarity = lambda text, *, neighbours=True: many
        out = server._check_domain_terms("x", ["vagunireis"])
        check("true past the cap", out["suggestions_capped"] is True
              and out["unlisted_compounds"][-1]["suggestions"] is None, str(out["suggestions_capped"]))
        many["all_compounds"] = many["all_compounds"][:server._DOMAIN_SUGGEST_CAP]
        out = server._check_domain_terms("x", ["vagunireis"])
        check("false at the cap", out["suggestions_capped"] is False, str(out["suggestions_capped"]))
    finally:
        server._check_compound_familiarity = real


def the_summary() -> None:
    print("summary_estonian")
    s = server._domain_terms_summary(0, [], True)
    check("no compounds says so", s == "Liitsõnanimisõnu analüüsiks ei leitud.", s)
    s = server._domain_terms_summary(3, [{"suggestions": []}], False)
    check("no glossary says so and keeps the caveat",
          "Valdkonna sõnastikku pole antud." in s and "tavaline" in s, s)
    s = server._domain_terms_summary(3, [], True)
    check("all listed", s.startswith("Kõik leitud"), s)
    s = server._domain_terms_summary(3, [{"suggestions": [{"term": "x"}]}, {"suggestions": []}], True)
    check("counts the look-alikes", "Neist 1 jaoks" in s and "2 neist" in s, s)


def the_preference() -> None:
    print("check_term_consistency: a listed variant is preferred")

    def group(*lemmas: str) -> dict:
        return {"variants": [{"lemma": w, "count": 1, "position": 0} for w in lemmas],
                "dominant": lemmas[0], "explanation": "E."}

    g = server._domain_prefer(group("andmestik", "teadusandmestik"), {}, frozenset({"x"}))
    check("none listed: no preferred", "preferred" not in g and g["explanation"] == "E.", str(g))
    check("each variant is marked",
          all(v["in_domain_glossary"] is False for v in g["variants"]), str(g))
    g = server._domain_prefer(group("andmestik", "teadusandmestik"), {}, frozenset({"teadusandmestik"}))
    check("one listed: it is preferred", g.get("preferred") == "teadusandmestik", str(g))
    check("dominant stays the frequency answer", g["dominant"] == "andmestik", str(g))
    check("and the explanation names it", "'teadusandmestik'" in g["explanation"], g["explanation"])
    g = server._domain_prefer(group("andmestik", "teadusandmestik"), {},
                              frozenset({"andmestik", "teadusandmestik"}))
    check("two listed: distinct concepts, none preferred", "preferred" not in g, str(g))
    g = server._domain_prefer(group("ladustamiskoha", "koht"),
                              {"ladustamiskoha": {"ladustamiskoha", "ladustamiskoht"}},
                              frozenset({"ladustamiskoht"}))
    check("matched by a later reading, the glossary form is preferred",
          g.get("preferred") == "ladustamiskoht", str(g))
    check("the explanation keeps the condition",
          g["explanation"].endswith("Kui valid ühe, eelista valdkonna sõnastiku terminit 'ladustamiskoht'."),
          g["explanation"])


def the_schema() -> None:
    print("the MCP schema bounds the per-call glossary")

    async def run() -> None:
        tools = {t.name: t for t in await server.mcp.list_tools()}
        for name in ("check_domain_terms", "check_term_consistency"):
            props = tools[name].inputSchema["properties"]
            schema = str(props["glossary"])
            check(f"{name}: maxItems advertised", f"'maxItems': {server.MAX_GLOSSARY_TERMS}" in schema, schema)
            check(f"{name}: maxLength advertised", f"'maxLength': {server.MAX_WORD_CHARS}" in schema, schema)
            try:
                await server.mcp.call_tool(name, {
                    "text": "x", "glossary": ["a"] * (server.MAX_GLOSSARY_TERMS + 1)})
                check(f"{name}: too many terms rejected before the body runs", False, "accepted")
            except Exception as e:
                check(f"{name}: too many terms rejected before the body runs",
                      "validation error" in str(e).lower(), str(e)[:120])
        cutoff = str(tools["check_domain_terms"].inputSchema["properties"]["suggest_cutoff"])
        check("suggest_cutoff is bounded 0.4 to 1",
              "'minimum': 0.4" in cutoff and "'maximum': 1.0" in cutoff, cutoff)

    asyncio.run(run())


def end_to_end() -> None:
    print("end to end, through the real tools (needs the resources)")
    reset()
    plain = server._check_compound_familiarity("Vagunireisi andmed ja mõtteliin.")
    check("no glossary: check_compound_familiarity has no new keys",
          all("in_domain_glossary" not in c and "forms" not in c for c in plain["all_compounds"]),
          str(plain["all_compounds"]))

    path = term_file("vagunireis\nladustamiskoht\n")
    ids = term_file("tellimusread\n")
    reset(**{GLOSSARY_ENV: path, IDENTIFIERS_ENV: ids})
    try:
        out = server.check_domain_terms(
            "Vagunireisi andmed. Ladustamiskohad ja tellimusread on korras. See on vagunisõit.")
        lemmas = [u["lemma"] for u in out["unlisted_compounds"]]
        check("an inflected glossary term is not reported", "vagunireis" not in lemmas, str(lemmas))
        check("nor one listed under its second reading",
              not any(w.startswith("ladustamiskoh") for w in lemmas), str(lemmas))
        check("nor an identifier written as named", "tellimusrida" not in lemmas, str(lemmas))
        hit = [u for u in out["unlisted_compounds"] if u["lemma"] == "vagunisõit"]
        check("an unlisted compound is reported with the term as a suggestion",
              hit and hit[0]["suggestions"][:1] and hit[0]["suggestions"][0]["term"] == "vagunireis",
              str(out["unlisted_compounds"]))
        fam = server.check_compound_familiarity("Vagunireisi andmed.")
        check("check_compound_familiarity stamps in_domain_glossary",
              fam["all_compounds"] and fam["all_compounds"][0].get("in_domain_glossary") is True,
              str(fam["all_compounds"]))
        check("without exposing the forms",
              all("forms" not in c for c in fam["all_compounds"]), str(fam["all_compounds"]))
    finally:
        os.unlink(path)
        os.unlink(ids)

    reset()
    tc_plain = server.check_term_consistency("Andmestik ja teadusandmestik.")
    reset(**{GLOSSARY_ENV: "/nonexistent/terms.txt"})
    fam = server.check_compound_familiarity("Vagunireisi andmed ja mõtteliin.")
    check("a bad path leaves check_compound_familiarity as it was", fam == plain, "output differs")
    check("and check_term_consistency",
          server.check_term_consistency("Andmestik ja teadusandmestik.") == tc_plain, "output differs")
    try:
        server.check_domain_terms("Vagunireisi andmed.")
        check("and makes check_domain_terms fail closed", False, "no error")
    except server._DomainVocabUnreadable:
        check("and makes check_domain_terms fail closed", True)
    reset()

    print("the older tools without a glossary")
    dump = json.dumps(tc_plain, ensure_ascii=False)
    check("check_term_consistency has no glossary keys",
          "in_domain_glossary" not in dump and '"preferred"' not in dump, dump[:200])
    check("nor a glossary sentence in its note", "domain glossary" not in tc_plain["note"])
    check("check_compound_familiarity has none in its note", "domain glossary" not in plain["note"])
    check("mõtteliin is suspect without a glossary", len(plain["suspect_compounds"]) == 1, str(plain["suspect_compounds"]))

    print("check_term_consistency with a glossary, end to end")
    out = server.check_term_consistency("Andmestik ja teadusandmestik.", glossary=["Teadusandmestik"])
    check("a per-call listed variant is preferred",
          out["groups"] and out["groups"][0].get("preferred") == "teadusandmestik", str(out["groups"]))
    out = server.check_term_consistency("Ladustamiskohad, kogumiskohad ja laadimiskohad.", glossary=["ladustamiskoht"])
    check("matched under a later reading, the glossary form is preferred",
          any(g.get("preferred") == "ladustamiskoht" for g in out["groups"]), str(out["groups"]))
    out = server.check_term_consistency("Andmestik ja teadusandmestik.")
    check("nothing is kept for the next call", out == tc_plain, str(out["groups"]))

    print("a listed compound is not suspect")
    path = term_file("mõtteliin\nvagunireis\n")
    reset(**{GLOSSARY_ENV: path})
    try:
        fam = server.check_compound_familiarity("See on mõtteliin. Vagunireisi andmed.")
        check("not in suspect_compounds", fam["suspect_compounds"] == [], str(fam["suspect_compounds"]))
        check("not counted as worth a look", "lähinaaber nõrk" not in fam["summary_estonian"], fam["summary_estonian"])
        check("counted as listed",
              fam["summary_estonian"].endswith("Sõnavarast ja tesaurusest puuduvatest on 2 valdkonna sõnastikus."),
              fam["summary_estonian"])
        check("and the note explains in_domain_glossary", "in_domain_glossary" in fam["note"])
    finally:
        os.unlink(path)
        reset()

    print("check_domain_terms skips the neighbour search")
    real = server._embeddings
    kv = real()
    searched: list[str] = []

    class Spy:
        key_to_index = kv.key_to_index

        def most_similar(self, *a, **k):
            searched.append(a[0])
            return kv.most_similar(*a, **k)

    server._embeddings = lambda: Spy()
    try:
        server.check_domain_terms("See on vagunisõit ja mõtteliin.")
        check("no most_similar on check_domain_terms' path", searched == [], str(searched))
        server.check_compound_familiarity("See on mõtteliin.")
        check("check_compound_familiarity still searches", searched != [], str(searched))
    finally:
        server._embeddings = real


off_by_default()
the_stamp()
the_decision()
the_file()
the_cutoff()
the_call_glossary()
the_summary()
the_preference()
the_schema()
end_to_end()

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nall passed")
