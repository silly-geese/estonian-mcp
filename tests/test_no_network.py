"""PRIVACY.md says the running server makes no outbound HTTP calls. This
test enforces it instead of trusting it.

Every tool is invoked with `socket.socket.connect` and
`socket.create_connection` replaced by a raiser, so any attempt to open an
outbound connection fails loudly and names the tool that tried.

Why this exists: issues #37 and #38 were both symptoms of resource
handling that reached for the network when something was missing. The old
`check_term_consistency` called `Wordnet()` and caught the fallout, which
meant EstNLTK would try to FETCH the resource — and print an interactive
prompt to stdout, which under stdio transport is the MCP protocol channel.
A promise in a markdown file did not stop that; a test does.

Two scenarios are covered for the availability probe specifically:
  A. EstNLTK's local resources index is present (the normal case).
  B. The index is absent — EstNLTK would want to fetch it from
     RESOURCES_INDEX_URL. The probe must still answer locally and return
     False rather than reaching out.

Run via:

    uv run python tests/test_no_network.py
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
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


class NetworkBlocked(AssertionError):
    """Raised in place of any outbound connection attempt."""


_real_connect = socket.socket.connect
_real_create = socket.create_connection


def _arm() -> None:
    def _blocked(*a, **k):
        target = a[1] if len(a) > 1 else (a[0] if a else "?")
        raise NetworkBlocked(f"outbound connection attempted to {target!r}")
    socket.socket.connect = _blocked
    socket.create_connection = _blocked


def _disarm() -> None:
    socket.socket.connect = _real_connect
    socket.create_connection = _real_create


# One representative call per tool. Args are deliberately small: this test
# is about network behaviour, not linguistic correctness.
TOOL_CALLS: dict[str, tuple] = {
    "tokenize": ("Tere maailm. Teine lause.",),
    "lemmatize": ("Koerad jooksevad.",),
    "pos_tag": ("Koerad jooksevad.",),
    "analyze_morphology": ("Koerad jooksevad.",),
    "spell_check": ("Tere maailm",),
    "syllabify": ("koerad",),
    "named_entities": ("Tallinn on Eesti pealinn.",),
    "paradigm": ("koer",),
    "synonyms": ("kohv",),
    "find_related_words": ("kohv",),
    "classify_register": ("Käesoleva lepingu alusel sätestatakse kohustused.",),
    "check_style": ("Süsteem kasutab andmeid. Andmed töödeldakse.",),
    "check_officialese": ("Aruandeperioodil koguti ja valideeriti andmestik.",),
    "check_term_consistency": ("Andmestik ja teadusandmestik.",),
    "check_compounds": ("Kooli maja on suur.",),
    "check_punctuation": ("Ma tean et see on hea.",),
    "check_capitalization": ("Olen Eestlane.",),
    "check_numbers": ("Pi on 3.14.",),
    "check_redundancy": ("Samuti ka.",),
    "check_object_case": ("Ma ei näen koera.",),
    "check_abbreviation_hyphenation": ("MCPst tuleb abi.",),
    "check_compound_familiarity": ("See on mõtteliin.",),
    "check_hyphenation": ("koerad",),
    "check_legalese": ("Käesolev leping.",),
    "check_defined_terms": ('Müüja (edaspidi «Müüja») müüb kauba.',),
    "common_legal_usage": ("hagi",),
}


def _warm() -> None:
    """Load the lazy models BEFORE arming, so a genuine local disk read is
    not conflated with a network call. Each is best-effort: on a machine
    without a given resource the tool is expected to fail, and that is what
    the resource tests cover."""
    for name, args in (
        ("spell_check", ("Tere",)),
        ("named_entities", ("Tallinn on linn.",)),
        ("synonyms", ("kohv",)),
        ("find_related_words", ("kohv",)),
    ):
        try:
            getattr(server, name)(*args)
        except Exception:
            pass


def every_tool_is_offline() -> None:
    print("no tool opens an outbound connection")
    _warm()
    attempted: list[str] = []
    _arm()
    try:
        for name, args in TOOL_CALLS.items():
            try:
                getattr(server, name)(*args)
            except NetworkBlocked as e:
                attempted.append(f"{name}: {e}")
            except Exception:
                # Any other failure (missing resource, bad input) is out of
                # scope here — only network behaviour is under test.
                pass
    finally:
        _disarm()

    check(f"all {len(TOOL_CALLS)} tools ran without outbound connections",
          not attempted, "; ".join(attempted))
    # Guard against the list silently drifting out of sync with the server.
    registered = {t.name for t in _registered_tools()}
    missing = registered - set(TOOL_CALLS)
    check("every registered tool is covered by this test",
          not missing, f"uncovered: {sorted(missing)}")


def _registered_tools():
    import asyncio
    return asyncio.run(server.mcp.list_tools())


def availability_probe_never_reaches_out() -> None:
    """The probe must answer from disk even when EstNLTK's local resources
    index is missing — that is precisely when EstNLTK would want to fetch
    it from RESOURCES_INDEX_URL."""
    print("_wordnet_available never reaches the network")
    from estnltk.resource_utils import get_resources_dir

    idx = Path(get_resources_dir()) / "resources_index.json"

    _arm()
    try:
        try:
            a = server._wordnet_available()
            check("index present: answers locally", isinstance(a, bool), repr(a))
        except NetworkBlocked as e:
            check("index present: answers locally", False, str(e))
    finally:
        _disarm()

    if not idx.exists():
        print("  SKIP index-missing case (no local index to move aside)")
        return

    bak = idx.with_suffix(".json.testbak")
    shutil.move(str(idx), str(bak))
    _arm()
    try:
        b = server._wordnet_available()
        check("index missing: still answers locally, no fetch",
              b is False, repr(b))
    except NetworkBlocked as e:
        check("index missing: still answers locally, no fetch", False, str(e))
    finally:
        _disarm()
        shutil.move(str(bak), str(idx))
    check("resources index restored", idx.exists())


def server_module_has_no_http_client() -> None:
    """A cheap structural guard: the served module should not import an
    HTTP client. urllib is fine to have available, but the server must not
    be reaching for requests/httpx to talk to anyone."""
    print("server module imports no HTTP client")
    src = Path(server.__file__).read_text(encoding="utf-8")
    for mod in ("import requests", "import httpx", "from requests", "from httpx"):
        check(f"no `{mod}` in server.py", mod not in src)


every_tool_is_offline()
availability_probe_never_reaches_out()
server_module_has_no_http_client()

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print(f"\nall no-network tests passed ({os.path.basename(__file__)})")
