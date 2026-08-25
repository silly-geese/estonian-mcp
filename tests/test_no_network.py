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
import socket
import sys
import tempfile
import time
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
_real_getaddrinfo = socket.getaddrinfo

# Every blocked attempt is RECORDED here before the exception is raised.
# Raising alone is not enough: the code under test catches broad exceptions
# by design, which silently converts "a connection was attempted" into "the
# test saw nothing" and passes. The earlier version of this file did
# exactly that and passed against a live privacy violation.
ATTEMPTS: list[str] = []


def _arm() -> None:
    ATTEMPTS.clear()

    def _blocked(*a, **k):
        target = a[1] if len(a) > 1 else (a[0] if a else "?")
        ATTEMPTS.append(repr(target))
        raise NetworkBlocked(f"outbound connection attempted to {target!r}")

    def _blocked_dns(host, *a, **k):
        ATTEMPTS.append(f"DNS {host!r}")
        raise NetworkBlocked(f"DNS lookup attempted for {host!r}")

    socket.socket.connect = _blocked
    socket.create_connection = _blocked
    socket.getaddrinfo = _blocked_dns          # DNS is egress too


def _disarm() -> None:
    socket.socket.connect = _real_connect
    socket.create_connection = _real_create
    socket.getaddrinfo = _real_getaddrinfo


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

    # ATTEMPTS is the real assertion: a tool that swallows NetworkBlocked
    # still leaves a record here.
    check(f"all {len(TOOL_CALLS)} tools ran without outbound connections",
          not ATTEMPTS and not attempted,
          f"recorded={ATTEMPTS[:5]} raised={attempted[:3]}")
    # Guard against the list silently drifting out of sync with the server.
    registered = {t.name for t in _registered_tools()}
    missing = registered - set(TOOL_CALLS)
    check("every registered tool is covered by this test",
          not missing, f"uncovered: {sorted(missing)}")


def _registered_tools():
    import asyncio
    return asyncio.run(server.mcp.list_tools())


def availability_probe_never_reaches_out() -> None:
    """The probe must answer from disk in the two states where EstNLTK
    would otherwise fetch its resources index over HTTPS:

      A. index STALE — the production steady state. EstNLTK refreshes any
         index older than INDEX_TIMEOUT (2 h), so a server up longer than
         that made an outbound call on the next lookup. This is the case
         the previous version of this file never covered.
      B. index ABSENT.

    Uses ESTNLTK_RESOURCES to point at a temp dir rather than moving the
    developer's venv copy aside, so a killed process cannot corrupt it.
    """
    print("_wordnet_available never reaches the network")
    import estnltk.resource_utils as ru

    # A. stale index, real resources dir.
    idx = Path(ru.get_resources_dir()) / "resources_index.json"
    if idx.exists():
        original = idx.stat().st_mtime
        stale = time.time() - 100_000        # far beyond any INDEX_TIMEOUT
        os.utime(idx, (stale, stale))
        _arm()
        try:
            server._wordnet_available()
        except NetworkBlocked:
            pass
        finally:
            _disarm()
            os.utime(idx, (original, original))
        check("stale index: no outbound call", not ATTEMPTS, str(ATTEMPTS[:3]))
    else:
        print("  SKIP stale-index case (no local index present)")

    # B. absent index, via an isolated resources dir.
    with tempfile.TemporaryDirectory() as tmp:
        prev = os.environ.get("ESTNLTK_RESOURCES")
        os.environ["ESTNLTK_RESOURCES"] = tmp
        _arm()
        try:
            result = server._wordnet_available()
        except NetworkBlocked:
            result = None
        finally:
            _disarm()
            if prev is None:
                os.environ.pop("ESTNLTK_RESOURCES", None)
            else:
                os.environ["ESTNLTK_RESOURCES"] = prev
        check("absent index: no outbound call", not ATTEMPTS, str(ATTEMPTS[:3]))
        check("absent index: reports unavailable rather than raising",
              result is False, repr(result))


def index_refresh_is_pinned() -> None:
    """_forbid_resource_downloads must neutralise both auto-fetch paths."""
    print("resource auto-download is structurally refused")
    import estnltk.resource_utils as ru
    check("EstNLTK index refresh is pinned (no periodic re-fetch)",
          ru.INDEX_TIMEOUT > 10 ** 9, str(ru.INDEX_TIMEOUT))

    import nltk.downloader as nd
    raised = None
    try:
        nd.download("punkt_tab")
    except Exception as e:
        raised = e
    check("nltk.downloader.download refuses instead of fetching",
          isinstance(raised, RuntimeError), f"{type(raised).__name__}: {raised}")
    check("refusal message is actionable",
          raised is not None and "fetch_resources.py" in str(raised), str(raised))


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
index_refresh_is_pinned()
server_module_has_no_http_client()

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print(f"\nall no-network tests passed ({os.path.basename(__file__)})")
