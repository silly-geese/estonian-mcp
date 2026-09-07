"""Every tool's real output must satisfy the schema it advertises.

This exists because `paradigm` answered `isError` to every MCP client for
two weeks and nothing here noticed. The tool functions were fine, the
tests called them directly, and the failure lived in the layer between:
the MCP server builds its structured payload from the tool's output
schema and fills every DECLARED BUT ABSENT key with None before
validating. A field typed `str` in a `total=False` TypedDict then fails
as "None is not of type 'string'", and the caller gets an error instead
of a paradigm.

Calling the python function proves nothing about that, and neither does
`mcp.call_tool`, which does not validate. So this test does what a client
does: read the advertised schema, run the tool, and check the structured
payload against it.

Run via:

    uv run python tests/test_output_schemas.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jsonschema

import server

failures: list[str] = []
skipped: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS {label}")
    else:
        failures.append(f"{label}: {detail}")
        print(f"  FAIL {label} {detail}")


# One representative call per tool, plus the shapes that take a different
# path through the code. `paradigm` gets six: the bug that prompted this
# file only appeared for words whose result OMITS an optional key, which
# is most of them, and the omitted set differs per word.
CALLS: list[tuple[str, dict]] = [
    ("tokenize", {"text": "Tere maailm. Teine lause."}),
    ("lemmatize", {"text": "Koerad jooksevad."}),
    ("pos_tag", {"text": "Koerad jooksevad."}),
    ("analyze_morphology", {"text": "Läksin majja."}),
    ("spell_check", {"text": "Tere maailm"}),
    ("syllabify", {"word": "koerad"}),
    ("named_entities", {"text": "Tallinn on Eesti pealinn."}),
    ("paradigm", {"word": "maja"}),          # single paradigm, most keys absent
    ("paradigm", {"word": "kott"}),          # two paradigms, key + others present
    ("paradigm", {"word": "kaunis"}),        # promoted reading
    ("paradigm", {"word": "kasutama"}),      # verb table
    ("paradigm", {"word": "ja"}),            # does not inflect
    ("paradigm", {"word": "qwertyxyz"}),     # not analysable
    ("classify_register", {"text": "Käesoleva lepingu alusel sätestatakse kohustused."}),
    ("check_style", {"text": "Süsteem kasutab andmeid. Andmed töödeldakse."}),
    ("check_officialese", {"text": "Aruandeperioodil koguti ja valideeriti andmestik."}),
    ("check_term_consistency", {"text": "Andmestik ja teadusandmestik."}),
    ("check_compounds", {"text": "Kooli maja on suur."}),
    ("check_punctuation", {"text": "Ma tean et see on hea."}),
    ("check_capitalization", {"text": "Olen Eestlane."}),
    ("check_numbers", {"text": "Pi on 3.14."}),
    ("check_redundancy", {"text": "Samuti ka."}),
    ("check_object_case", {"text": "Ma ei näen koera."}),
    ("check_abbreviation_hyphenation", {"text": "MCPst tuleb abi."}),
    ("check_compound_familiarity", {"text": "See on mõtteliin."}),
    ("check_hyphenation", {"word": "koerad"}),
    ("check_legalese", {"text": "Käesolev leping."}),
    ("check_defined_terms", {"text": 'Müüja (edaspidi «Müüja») müüb kauba.'}),
    ("common_legal_usage", {"word": "hagi"}),
    ("synonyms", {"word": "kohv"}),                 # needs WordNet
    ("find_related_words", {"word": "kohv"}),       # needs fastText
]

# These need a downloaded resource. Absent, the tool raises on purpose and
# that is the resource tests' business, not this file's.
NEEDS_RESOURCES = {"synonyms", "find_related_words"}


async def run() -> None:
    tools = {t.name: t for t in await server.mcp.list_tools()}

    print("every registered tool has an output schema")
    for name, tool in sorted(tools.items()):
        check(f"{name} advertises one", tool.outputSchema is not None)

    print("and the structured payload a client receives satisfies it")
    for name, args in CALLS:
        tool = tools.get(name)
        if tool is None:
            check(f"{name} is registered", False, "not in list_tools()")
            continue
        label = f"{name}({next(iter(args.values()))!r})"
        try:
            _content, structured = await server.mcp.call_tool(name, args)
        except Exception as e:
            if name in NEEDS_RESOURCES:
                skipped.append(f"{label}: {type(e).__name__}")
                print(f"  SKIP {label} (needs a downloaded resource)")
                continue
            check(label, False, f"raised {type(e).__name__}: {str(e)[:90]}")
            continue
        try:
            jsonschema.validate(structured, tool.outputSchema)
            print(f"  PASS {label}")
        except jsonschema.ValidationError as e:
            path = "/".join(str(p) for p in e.absolute_path) or "(root)"
            check(label, False, f"schema violation at {path}: {e.message[:90]}")

    print("no tool answers isError for a valid call")
    # The layer turns a schema violation into an error RESULT rather than an
    # exception, which is why this was invisible: the server stayed up, the
    # tests stayed green, and every caller got an error string.
    for name, args in CALLS:
        if name in NEEDS_RESOURCES:
            continue
        content, _structured = await server.mcp.call_tool(name, args)
        text = ""
        if content and getattr(content[0], "text", None):
            text = content[0].text
        check(f"{name} returns a result, not an error string",
              "Output validation error" not in text, text[:100])

    print("every tool in the registry is exercised here")
    registered = set(tools)
    covered = {name for name, _ in CALLS}
    missing = registered - covered
    check("no tool is left untested", not missing, str(sorted(missing)))


asyncio.run(run())

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
if skipped:
    print(f"\n{len(skipped)} call(s) skipped for missing resources:")
    for s in skipped:
        print(" -", s)
print("\nall output-schema tests passed")
