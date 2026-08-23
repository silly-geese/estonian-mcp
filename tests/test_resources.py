"""Tests for resource-availability handling (issues #37 and #38).

The defect these pin down: `check_term_consistency` returned
"Ebajärjekindlat terminikasutust ei tuvastatud" — a confident negative —
while one of its two rules was switched off because Estonian WordNet was
missing. The only signal was a `rules_run` flag a caller had to know to
read. A partial run that reads as a clean bill of health is worse than a
crash, so degradation must be visible in the human-facing summary.

Also pins the privacy-relevant half: the running server must never attempt
to DOWNLOAD a missing resource. PRIVACY.md promises no outbound HTTP calls,
and under stdio transport EstNLTK's download prompt goes to stdout, which
is the MCP protocol channel.

Runs with or without WordNet installed — the degraded path is exercised by
monkeypatching availability, not by requiring a broken environment.

Run via:

    uv run python tests/test_resources.py
"""
from __future__ import annotations

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


DOC = "Andmestik ja teadusandmestik ning pildiandmestik. Andmestik on avalik."


def _with_wordnet(available: bool):
    """Force _wordnet_available to a given answer, restoring afterwards.

    Swaps the module attribute rather than clearing the lru_cache, because
    the production code resolves `_wordnet_available` through a module
    global at call time — so replacing the attribute is what actually takes
    effect, and it leaves the real cache untouched for later tests.
    """
    real_avail = server._wordnet_available
    real_wn = server._wordnet

    class _Ctx:
        def __enter__(self):
            server._wordnet_available = lambda: available
            if not available:
                def _boom():
                    raise AssertionError(
                        "_wordnet() must NOT be called when the resource is "
                        "unavailable — that is what triggers a download "
                        "attempt and the stdout prompt"
                    )
                server._wordnet = _boom
            return self

        def __exit__(self, *a):
            server._wordnet_available = real_avail
            server._wordnet = real_wn
            return False

    return _Ctx()


def degraded_is_visible() -> None:
    """Issue #38: a half-strength run must say so where a human will see it."""
    print("check_term_consistency — degraded run is visible")

    with _with_wordnet(False):
        r = server.check_term_consistency(DOC)

    check("rules_run reports the WordNet rule did not run",
          r["rules_run"]["shared-wordnet-synset"] is False, str(r["rules_run"]))
    check("compound-head rule still ran",
          r["rules_run"]["shared-compound-head"] is True, str(r["rules_run"]))
    check("top-level degraded flag is set",
          r.get("degraded") is True, str(r.get("degraded")))
    summary = r["summary_estonian"]
    check("summary warns the result is partial",
          "osaline" in summary, summary)
    check("summary names the missing resource",
          "WordNet" in summary, summary)
    check("summary tells the caller how to fix it",
          "fetch_resources.py" in summary, summary)
    # The original bug: a clean-sounding negative with no caveat attached.
    check("a no-findings degraded run is NOT a bare clean bill of health",
          not summary.strip().endswith("ei tuvastatud."), summary)


def healthy_run_is_not_polluted() -> None:
    """The warning must appear ONLY when degraded — otherwise it is noise
    and callers learn to ignore it."""
    print("check_term_consistency — healthy run stays clean")

    with _with_wordnet(True):
        r = server.check_term_consistency(DOC)

    check("degraded is False when WordNet is available",
          r.get("degraded") is False, str(r.get("degraded")))
    check("no partial-result warning in the summary",
          "osaline" not in r["summary_estonian"], r["summary_estonian"])
    check("rules_run shows both rules ran",
          r["rules_run"] == {"shared-compound-head": True,
                             "shared-wordnet-synset": True},
          str(r["rules_run"]))
    # The compound-head rule is independent of WordNet, so the real finding
    # must survive either way.
    check("the andmestik group is still found",
          any(g["rule"] == "shared-compound-head" for g in r["groups"]),
          str(r["groups"]))


def no_download_attempt_at_runtime() -> None:
    """PRIVACY.md: the running server makes no outbound HTTP calls.

    _with_wordnet(False) installs a _wordnet() that raises if called, so
    this test fails loudly if the degraded path ever goes back to
    "call it and catch the fallout".
    """
    print("no resource download is attempted while serving")

    with _with_wordnet(False):
        r = server.check_term_consistency(DOC)  # must not raise
    check("degraded run completes without calling _wordnet()",
          r["rules_run"]["shared-wordnet-synset"] is False)

    with _with_wordnet(False):
        raised = None
        try:
            server.synonyms("korpus")
        except Exception as e:
            raised = e
    check("synonyms raises rather than downloading",
          isinstance(raised, RuntimeError), f"{type(raised).__name__}: {raised}")
    msg = str(raised)
    check("synonyms error names the fix",
          "fetch_resources.py" in msg, msg)
    check("synonyms error explains the no-download policy",
          "PRIVACY.md" in msg, msg)


def availability_probe_is_local_only() -> None:
    """_wordnet_available must answer from disk, never from the network."""
    print("_wordnet_available — pure local probe")
    v = server._wordnet_available()
    check("returns a bool", isinstance(v, bool), repr(v))
    # Cached, so repeated calls cannot turn into repeated probes.
    check("is cached", server._wordnet_available() is v)
    check("has a cache_clear (lru_cache applied)",
          hasattr(server._wordnet_available, "cache_clear"))


def schema_advertises_degraded() -> None:
    print("output schema")
    ann = server._TermConsistencyResult.__annotations__
    check("degraded is in the TypedDict", "degraded" in ann, str(sorted(ann)))
    # server.py uses `from __future__ import annotations`, so annotations are
    # ForwardRef/str rather than the type object.
    check("degraded is typed bool",
          "bool" in str(ann.get("degraded")), str(ann.get("degraded")))


degraded_is_visible()
healthy_run_is_not_polluted()
no_download_attempt_at_runtime()
availability_probe_is_local_only()
schema_advertises_degraded()

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nall resource-handling tests passed")
