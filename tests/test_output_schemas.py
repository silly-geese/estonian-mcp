"""Every tool's real output must satisfy the schema it advertises.

This exists because `paradigm` answered `isError` to every MCP client for
two weeks and nothing here noticed. The tool functions were fine, the
tests called them directly, and the failure lived in the layer between:
the SDK built the structured payload from the tool's output schema and
filled every DECLARED BUT ABSENT key with None before validating. A field
typed `str` in a `total=False` TypedDict then failed as "None is not of
type 'string'", and the caller got an error instead of a paradigm. Since
0.8.0 the server keeps only the keys a tool returned (_EstonianMCP), and
this file also checks that wire format: one compact text block that says
what structuredContent says, and no null a tool did not write.

Calling the python function proves nothing about that, and neither does
`mcp.call_tool` on its own. So this test does what a client does: read the
advertised schema, run the tool, and check the structured payload against
it, then does it again through a real client session, which validates
structuredContent against the advertised schema itself.

Run via:

    uv run python tests/test_output_schemas.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jsonschema
from mcp import Client
from mcp.types.version import LATEST_MODERN_VERSION

import server

# A glossary exported in the developer's shell would change what these
# tools return; the suite checks the server as it ships. The
# glossary is read on first use, so this is early enough.
for _name in ("ESTNLTK_MCP_DOMAIN_GLOSSARY", "ESTNLTK_MCP_DOMAIN_IDENTIFIERS",
              "ESTNLTK_MCP_DOMAIN_SUGGEST_CUTOFF"):
    os.environ.pop(_name, None)

failures: list[str] = []
skipped: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS {label}")
    else:
        failures.append(f"{label}: {detail}")
        print(f"  FAIL {label} {detail}")


# One representative call per tool, plus the shapes that take a different
# path through the code. `paradigm` gets ten: the bug that prompted this
# file only appeared for words whose result OMITS an optional key, which
# is most of them, and the omitted set differs per word.
CALLS: list[tuple[str, dict]] = [
    ("tokenize", {"text": "Tere maailm. Teine lause."}),
    ("lemmatize", {"text": "Koerad jooksevad."}),
    ("pos_tag", {"text": "Koerad jooksevad."}),
    ("analyze_morphology", {"text": "Läksin majja."}),
    ("analyze_morphology", {"text": "Ta on kodus.", "all_analyses": True}),
    ("spell_check", {"text": "Tere maailmm"}),   # a misspelled row
    ("spell_check", {"text": "Tere maailm."}),   # clean: an empty list
    ("syllabify", {"word": "koerad"}),
    ("named_entities", {"text": "Tallinn on Eesti pealinn."}),
    ("paradigm", {"word": "maja"}),          # single paradigm, most keys absent
    ("paradigm", {"word": "kott"}),          # two paradigms, key + others present
    ("paradigm", {"word": "kaunis"}),        # promoted reading
    ("paradigm", {"word": "kasutama"}),      # verb table
    ("paradigm", {"word": "ja"}),            # does not inflect
    ("paradigm", {"word": "ei"}),            # verb path, no forms, a summary
    ("paradigm", {"word": "qwertyxyz"}),     # guessed noun
    ("paradigm", {"word": "Google"}),        # unknown proper noun, no forms, a summary
    ("paradigm", {"word": "Franz"}),         # a name part, no forms, a summary
    ("paradigm", {"word": "peeter"}),        # lowercase name, lemma None, a summary
    ("classify_register", {"text": "Käesoleva lepingu alusel sätestatakse kohustused."}),
    ("check_style", {"text": "Süsteem kasutab andmeid. Andmed töödeldakse."}),
    ("check_officialese", {"text": "Aruandeperioodil koguti ja valideeriti andmestik."}),
    ("check_translationese", {"text": "Palun saatke meile teie andmed."}),
    ("check_term_consistency", {"text": "Andmestik ja teadusandmestik."}),
    ("check_term_consistency", {"text": "Andmestik ja teadusandmestik.",
                                "glossary": ["teadusandmestik"]}),  # preferred
    ("check_compounds", {"text": "Kooli maja on suur."}),
    ("check_punctuation", {"text": "Ma tean et see on hea."}),
    ("check_capitalization", {"text": "Olen Eestlane."}),
    ("check_numbers", {"text": "Pi on 3.14."}),
    ("check_redundancy", {"text": "Samuti ka."}),
    ("check_object_case", {"text": "Ma ei näen koera."}),
    ("check_abbreviation_hyphenation", {"text": "OÜle tuleb abi."}),
    ("check_compound_familiarity", {"text": "See on mõtteliin."}),
    ("check_domain_terms", {"text": "See on mõtteliin."}),
    ("check_domain_terms", {"text": "Vagunisõit ja vagunireisitabel.",
                            "glossary": ["vagunireis"]}),  # suggestions
    ("check_hyphenation", {"word": "koerad"}),
    ("check_legalese", {"text": "Käesolev leping."}),
    ("check_defined_terms", {"text": 'Müüja (edaspidi «Müüja») müüb kauba.'}),
    ("common_legal_usage", {"word": "hagi"}),
    ("synonyms", {"word": "kohv"}),                 # needs WordNet
    ("find_related_words", {"word": "kohv"}),       # needs fastText
    # A string with no known subword has a zero vector, so every score is
    # NaN; the text block must still be JSON and say what structured says.
    ("find_related_words", {"word": "xqzzyv", "n": 3}),
]

# These need a downloaded resource. Absent, the tool fails on purpose and
# that is the resource tests' business, not this file's.
NEEDS_RESOURCES = {"synonyms", "find_related_words"}


async def run() -> None:
    tools = {t.name: t for t in await server.mcp.list_tools()}

    print("every registered tool has an output schema")
    for name, tool in sorted(tools.items()):
        check(f"{name} advertises one", tool.output_schema is not None)

    print("and the structured payload a client receives satisfies it")
    for name, args in CALLS:
        tool = tools.get(name)
        if tool is None:
            check(f"{name} is registered", False, "not in list_tools()")
            continue
        label = f"{name}({next(iter(args.values()))!r})"
        r = await server.mcp.call_tool(name, args)
        if r.is_error:
            text = r.content[0].text if r.content else ""
            if name in NEEDS_RESOURCES:
                skipped.append(f"{label}: {text[:60]}")
                print(f"  SKIP {label} (needs a downloaded resource)")
                continue
            check(label, False, f"answered an error: {text[:90]}")
            continue
        try:
            jsonschema.validate(r.structured_content, tool.output_schema)
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
        r = await server.mcp.call_tool(name, args)
        text = ""
        if r.content and getattr(r.content[0], "text", None):
            text = r.content[0].text
        check(f"{name} returns a result, not an error string",
              not r.is_error and "Output validation error" not in text, text[:100])

    print("the wire format is lean")
    # Every caller pays for these bytes in tokens; see _EstonianMCP. One
    # compact block, saying exactly what structuredContent says, checked
    # through a real client session, which also validates structuredContent
    # against the advertised outputSchema. Both eras: the initialize
    # handshake, and 2026-07-28, which has none.
    def strict(text):
        # json.loads accepts a bare NaN; a strict client does not.
        return json.loads(text, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))

    for mode in ("legacy", LATEST_MODERN_VERSION):
        print(f"  over a {mode} client session")
        async with Client(server.mcp, mode=mode) as client:
            for name, args in CALLS:
                label = f"{name}({next(iter(args.values()))!r})"
                if any(s.startswith(label) for s in skipped):
                    continue
                try:
                    r = await client.call_tool(name, args)
                except RuntimeError as e:  # the client's own schema check
                    check(f"{label} satisfies its outputSchema", False, str(e)[:120])
                    continue
                check(f"{label} is not an error", not r.is_error, str(r.content)[:100])
                text = r.content[0].text if r.content else ""
                check(f"{label} answers in one text block", len(r.content) == 1, str(len(r.content)))
                try:
                    payload = strict(text)
                except ValueError as e:
                    check(f"{label} text is strict JSON", False, f"{e}: {text[:80]}")
                    continue
                check(f"{label} text is compact JSON",
                      text == json.dumps(payload, ensure_ascii=False, separators=(",", ":")), text[:80])
                # List tools are wrapped as {"result": [...]} in structuredContent,
                # which the outputSchema requires; the text is the bare list.
                structured = r.structured_content
                expected = structured["result"] if isinstance(payload, list) else structured
                check(f"{label} text equals structuredContent", payload == expected, text[:80])

            print("argument errors are short and do not echo the input")
            r = await client.call_tool("tokenize", {"text": 5})
            msg = r.content[0].text if r.content else ""
            check("a wrong type is one short clause",
                  r.is_error and msg == "Input validation error: text: Input should be a valid string",
                  msg[:160])
            r = await client.call_tool("check_domain_terms",
                                       {"text": "x", "glossary": ["a" * 201] * 40})
            msg = r.content[0].text if r.content else ""
            check("forty bad glossary terms name five and count the rest",
                  r.is_error and msg.count("glossary.") == server._MAX_ARG_ERRORS
                  and msg.endswith(f"and {40 - server._MAX_ARG_ERRORS} more") and "aaaa" not in msg,
                  msg[:200])

    print("other failures keep the reason, as under mcp 1.x")
    # 2.x answers an exception the tool did not raise as "Error executing
    # tool <name>" alone; the limit and the missing resource are the part
    # the caller can act on.
    r = await server.mcp.call_tool("spell_check", {"text": "a" * (server.MAX_TEXT_CHARS + 1)})
    msg = r.content[0].text if r.content else ""
    check("an over-long text names the limit",
          r.is_error and msg == f"Error executing tool spell_check: text length "
          f"{server.MAX_TEXT_CHARS + 1} exceeds limit {server.MAX_TEXT_CHARS}; "
          "split the input into smaller chunks", msg[:160])
    r = await server.mcp.call_tool("no_such_tool", {})
    msg = r.content[0].text if r.content else ""
    check("an unknown tool is named", r.is_error and msg == "Unknown tool: no_such_tool", msg[:160])

    # The SDK default filled each declared-but-absent key with null, which
    # Claude Code shows the model. paradigm('maja') omits several keys.
    structured = (await server.mcp.call_tool("paradigm", {"word": "maja"})).structured_content
    raw = server.paradigm("maja")
    check("structuredContent carries only the keys the tool returned",
          set(structured) == set(raw), str(sorted(set(structured) ^ set(raw))))
    # spell_check lists only misspelled words, so clean text, its most
    # common result, is an empty list. A client that reads only the text
    # must still get an answer, not a blank result.
    r = await server.mcp.call_tool("spell_check", {"text": "Tere maailm."})
    check("clean spell_check text answers one '[]' block",
          [c.text for c in r.content] == ["[]"] and r.structured_content == {"result": []},
          f"{[c.text for c in r.content]} {r.structured_content}")
    structured = (await server.mcp.call_tool("spell_check", {"text": "Tere maailmm"})).structured_content
    check("a misspelling comes back as one populated row",
          [r["text"] for r in structured["result"]] == ["maailmm"]
          and structured["result"][0]["spelling"] is False, str(structured))

    print("the issue-list checks advertise only what they return")
    # They dropped the input echo `text` and the static `note`; a key left
    # in _CheckResult would still be advertised to every client.
    for name in ("check_compounds", "check_punctuation", "check_capitalization",
                 "check_numbers", "check_redundancy", "check_object_case",
                 "check_abbreviation_hyphenation", "check_translationese"):
        props = set((tools[name].output_schema or {}).get("properties", {}))
        check(f"{name} outputSchema is issues and summary_estonian",
              props == {"issues", "summary_estonian"}, str(sorted(props)))

    print("advertised schemas carry no title noise")

    def titles(node, path=""):
        # A property NAMED "title" is data; only the annotation counts.
        if isinstance(node, list):
            return [t for i, x in enumerate(node) for t in titles(x, f"{path}/{i}")]
        if not isinstance(node, dict):
            return []
        found = [path] if isinstance(node.get("title"), str) else []
        for key, value in node.items():
            if key in ("properties", "$defs") and isinstance(value, dict):
                found += [t for n, sub in value.items() for t in titles(sub, f"{path}/{key}/{n}")]
            elif key != "title":
                found += titles(value, f"{path}/{key}")
        return found

    for name, tool in sorted(tools.items()):
        left = titles(tool.input_schema) + titles(tool.output_schema or {})
        check(f"{name} schemas have no title", not left, str(left[:3]))
        # pydantic makes a TypedDict's docstring the schema's description;
        # those are notes for maintainers, not for the model.
        check(f"{name} outputSchema has no description",
              "description" not in (tool.output_schema or {}))

    print("descriptions arrive unwrapped and dedented")
    # Hard wrapping and docstring indentation cost tokens and say nothing.
    # A line break inside a paragraph is allowed only before a list item.
    for name, tool in sorted(tools.items()):
        bad = [line for block in tool.description.split("\n\n")
               for line in block.split("\n")[1:] if not server._LIST_ITEM.match(line)]
        bad += [line for line in tool.description.split("\n") if line != line.strip()]
        check(f"{name} description is reflowed", not bad, str(bad[:2]))

    print("the server instructions carry the rules the tools no longer repeat")
    # Per-tool notes and docstrings stopped repeating these in 0.8.0, on the
    # strength of saying each once here. Losing one loses the steering.
    instr = server.SERVER_INSTRUCTIONS
    for needle in ("_estonian", "verbatim", "heuristics", "empty list",
                   "does not apply is omitted", f"{server.MAX_TEXT_CHARS:,}",
                   f"{server.MAX_DOC_CHARS:,}", "check_compound_familiarity",
                   "word for word", "longer text", "check_translationese"):
        check(f"instructions mention {needle!r}", needle in instr)
    check("instructions stay well under Claude Code's 4,096-character cut",
          len(instr) < 3000, str(len(instr)))
    # Steering that left the instructions in 0.8.0 lives only in the
    # description of the tool it governs, which a client loads with it.
    moved = {
        "paradigm": ("ambiguity_estonian", "inflected form"),
        "check_compound_familiarity": ("not evidence of invention",),
        "check_domain_terms": ("not synonyms", "not necessarily coined"),
        "synonyms": ("`definition` against the context",),
        "spell_check": ("check_compound_familiarity",),
        "check_officialese": ("check_legalese",),
    }
    for name, needles in moved.items():
        desc = " ".join(tools[name].description.split())
        for needle in needles:
            check(f"{name} description keeps {needle!r}", needle in desc, desc[:120])

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
