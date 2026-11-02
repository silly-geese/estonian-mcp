"""Tool calls that arrive together must all succeed, and the server must live.

mcp 2.x runs a sync tool on a pool of worker threads, where 1.x ran every
tool on the event loop's own thread. So from 1.1.0 calls ran side by side:
`synonyms` failed on every thread but the one that had opened WordNet's
sqlite3 connection, `check_term_consistency` reported itself degraded, and
within seconds of parallel calls the process died of a segmentation fault.
Every other suite calls one tool at a time, and none of them could see it.
Since 1.1.1 every tool runs on one thread (_TOOL_EXECUTOR in server.py).

This file calls tools the way a busy client does: many at once, through the
MCP layer and from plain threads, and compares each answer with the same
call made alone. A regression fails a check here, or ends the process with
a segmentation fault, whose stacks faulthandler prints.

Run via:

    uv run python tests/test_parallel_calls.py
"""
from __future__ import annotations

import asyncio
import faulthandler
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server

faulthandler.enable()

# A glossary exported in the developer's shell would change what these
# tools return; the suite checks the server as it ships.
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


TEXT = ("Käesolevaga teatame, et vastavalt lepingule viiakse läbi kontroll. "
        "Tallinna Ülikooli teadlased analüüsisid andmeid ja leidsid, et "
        "riskihindamine on vajalik. Peeter Kask kohtus Jaan Tammega Tartus. "
        "Andmekaitse on oluline ja andmekaitse nõuded kehtivad kõigile.")

# WordNet's three users first (the sqlite3 path that failed and crashed),
# then Vabamorf and the EstNLTK taggers.
CALLS: list[tuple[str, dict]] = [
    ("synonyms", {"word": "kohv"}),
    ("synonyms", {"word": "maja"}),
    ("synonyms", {"word": "korpus", "max_synsets": 3}),
    ("check_term_consistency", {"text": TEXT}),
    ("check_compound_familiarity", {"text": TEXT}),
    ("analyze_morphology", {"text": TEXT}),
    ("spell_check", {"text": TEXT}),
    ("paradigm", {"word": "maja"}),
    ("lemmatize", {"text": TEXT}),
    ("pos_tag", {"text": TEXT}),
    ("syllabify", {"word": "riskihindamine"}),
    ("named_entities", {"text": TEXT}),
    ("check_style", {"text": TEXT}),
    ("check_officialese", {"text": TEXT}),
    ("classify_register", {"text": TEXT}),
    ("check_object_case", {"text": TEXT}),
]
ROUNDS = 4


def answer(r) -> tuple[bool, str]:
    return (bool(r.is_error), r.content[0].text if r.content else "")


async def run() -> None:
    print("resources the WordNet calls need")
    check("WordNet is installed", server._wordnet_available(),
          "fetch it with: uv run python scripts/fetch_resources.py")

    print("each call made alone")
    alone: dict[int, tuple[bool, str]] = {}
    for i, (name, args) in enumerate(CALLS):
        alone[i] = answer(await server.mcp.call_tool(name, args))
        check(f"{name} answers", not alone[i][0], alone[i][1][:200])
    tc = alone[[name for name, _ in CALLS].index("check_term_consistency")][1]
    check("check_term_consistency ran its WordNet rule", '"degraded":false' in tc, tc[:200])

    print(f"{len(CALLS) * ROUNDS} calls at once, through the MCP layer")
    jobs = [i for _ in range(ROUNDS) for i in range(len(CALLS))]
    results = await asyncio.gather(
        *(server.mcp.call_tool(CALLS[i][0], CALLS[i][1]) for i in jobs))
    wrong = [(CALLS[i][0], answer(r)[1][:200]) for i, r in zip(jobs, results, strict=True)
             if answer(r) != alone[i]]
    check("every answer is the one the call gets alone", not wrong,
          f"{len(wrong)} differ, first: {wrong[:1]}")


asyncio.run(run())

print("the same calls from plain threads, as a script makes them")
by_name = {name: getattr(server, name) for name, _ in CALLS}
with ThreadPoolExecutor(8) as pool:
    first = [pool.submit(by_name[name], **args) for name, args in CALLS]
    again = [pool.submit(by_name[name], **args) for name, args in CALLS]
    errors = []
    for (name, _), a, b in zip(CALLS, first, again, strict=True):
        try:
            if a.result() != b.result():
                errors.append(f"{name}: two answers differ")
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
check("every call succeeds, the same twice", not errors, "; ".join(errors[:3]))

# A helper called outside any tool runs on the caller's thread, as
# tests/test_domain_terms.py does, and needs a WordNet of its own there.
print("a WordNet helper on this thread, outside any tool")
direct = server._check_compound_familiarity(TEXT)
check("asks WordNet", any(c["in_wordnet"] is not None for c in direct["all_compounds"]),
      str(direct["all_compounds"])[:200])
check("and answers as the tool does", direct == server.check_compound_familiarity(TEXT))

print("one tool thread")
tool_threads = [t.name for t in threading.enumerate()
                if t.name.startswith("estonian-mcp-tool")]
check("all calls ran on a single thread", len(tool_threads) == 1, str(tool_threads))

# A tool called on the tool thread must run in place: queued behind
# itself, it would wait for ever, and so would this process at exit.
# Last, so a hang cannot hold up the checks above.
print("a call made on the tool thread")
nested = server._TOOL_EXECUTOR.submit(server.tokenize, "Tere. Head aega.")
try:
    nested.result(timeout=60)
    check("runs in place, without waiting for itself", True)
except Exception as e:
    check("runs in place, without waiting for itself", False, f"{type(e).__name__}: {e}")

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.stdout.flush()
    # A deadlocked tool thread would keep the interpreter from exiting.
    os._exit(1)
print("\nall parallel-call tests passed")
