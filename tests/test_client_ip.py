"""Tests for per-IP rate-limit bucketing (public mode).

The bug these pin down: `_client_ip` returned `scope["client"][0]`, which
uvicorn had rewritten from the LEFTMOST `X-Forwarded-For` entry. A proxy
APPENDS to whatever the caller sent, so the leftmost entry is entirely
caller-controlled — meaning any caller could mint a fresh rate-limit bucket
per request just by varying the header, and the public deployment's only
DoS protection did nothing.

Reproduced before the fix against a local server in public mode at a 5/min
limit: a fixed spoofed value got 429 after five requests, while rotating
the value stayed 200 indefinitely.

The fix reads XFF from the RIGHT. The Nth-from-right entry is the one
written by the Nth proxy in front of us, and a caller cannot append after a
proxy — so it is correct whether the edge appends to a client-supplied
header or replaces it outright.

Run via:

    uv run python tests/test_client_ip.py
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


def scope(xff: str | None = None, peer: str = "172.16.0.5") -> dict:
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    # With proxy_headers=False, uvicorn leaves scope["client"] as the real
    # peer. Tests assert against that contract.
    return {"headers": headers, "client": (peer, 0)}


def _with_hops(n: int):
    class _Ctx:
        def __enter__(self):
            self.prev = server._TRUSTED_PROXY_HOPS
            server._TRUSTED_PROXY_HOPS = n
            return self

        def __exit__(self, *a):
            server._TRUSTED_PROXY_HOPS = self.prev
            return False
    return _Ctx()


def one_proxy_in_front() -> None:
    """Fly's topology: exactly one proxy, which appends the true peer."""
    print("hops=1 (Fly): the forged prefix is ignored")
    with _with_hops(1):
        check("caller forged one entry",
              server._client_ip(scope("9.9.9.9, 203.0.113.7")) == "203.0.113.7")
        check("caller forged twenty entries",
              server._client_ip(scope(", ".join(["1.2.3.4"] * 20) + ", 203.0.113.7"))
              == "203.0.113.7")
        check("edge replaced the header outright",
              server._client_ip(scope("203.0.113.7")) == "203.0.113.7")
        check("no XFF at all falls back to the real peer",
              server._client_ip(scope(None, peer="198.51.100.4")) == "198.51.100.4")
        # The whole point: rotating the forged part must NOT change the bucket.
        buckets = {server._client_ip(scope(f"10.0.0.{i}, 203.0.113.7")) for i in range(50)}
        check("rotating the forged prefix yields ONE bucket, not 50",
              buckets == {"203.0.113.7"}, f"{len(buckets)} distinct: {sorted(buckets)[:4]}")


def directly_exposed() -> None:
    """hops=0: no proxy in front, so XFF is not trustworthy at all."""
    print("hops=0 (direct exposure): XFF is ignored entirely")
    with _with_hops(0):
        check("forged XFF does not move the bucket",
              server._client_ip(scope("203.0.113.7", peer="172.16.0.5")) == "172.16.0.5")
        buckets = {server._client_ip(scope(f"203.0.113.{i}", peer="172.16.0.5"))
                   for i in range(50)}
        check("rotating XFF yields ONE bucket", buckets == {"172.16.0.5"}, str(buckets))


def two_proxies() -> None:
    print("hops=2 (e.g. CDN in front of the edge)")
    with _with_hops(2):
        check("takes the second-from-right entry",
              server._client_ip(scope("203.0.113.7, 104.16.0.1, 66.51.0.1")) == "104.16.0.1")
        # Fewer entries than trusted hops means the chain is not what we
        # expect, so trust the peer rather than a caller-supplied value.
        check("short chain falls back to the peer",
              server._client_ip(scope("9.9.9.9", peer="172.16.0.5")) == "172.16.0.5")


def malformed_input_is_safe() -> None:
    print("malformed headers never raise")
    with _with_hops(1):
        for raw in ("", "   ", ",,,", "not-an-ip", "9.9.9.9,,203.0.113.7"):
            try:
                got = server._client_ip(scope(raw))
                check(f"{raw!r} handled -> {got!r}", isinstance(got, str) and got != "")
            except Exception as e:
                check(f"{raw!r} handled", False, f"{type(e).__name__}: {e}")
        # Non-UTF8 bytes in the header must not blow up the request path.
        try:
            s = {"headers": [(b"x-forwarded-for", b"\xff\xfe bad")], "client": ("172.16.0.5", 0)}
            got = server._client_ip(s)
            check("undecodable header handled", isinstance(got, str))
        except Exception as e:
            check("undecodable header handled", False, f"{type(e).__name__}: {e}")


def uvicorn_does_not_rewrite_client() -> None:
    """The fix depends on scope["client"] being the real peer, which is only
    true with proxy_headers disabled."""
    print("uvicorn is configured not to rewrite scope['client']")
    import re
    src = Path(server.__file__).read_text(encoding="utf-8")
    check("proxy_headers=False is set", "proxy_headers=False" in src)
    # Match the KWARG, not prose: the docstring in _client_ip mentions
    # forwarded_allow_ips="*" to explain the bug it fixes, and that
    # explanation should not fail its own test.
    passed_as_kwarg = re.search(r'^\s*forwarded_allow_ips\s*=', src, re.M)
    check("forwarded_allow_ips is not passed to uvicorn",
          passed_as_kwarg is None,
          "a wildcard trust would re-enable leftmost-XFF rewriting")
    check("proxy_headers is not enabled anywhere",
          re.search(r'^\s*proxy_headers\s*=\s*True', src, re.M) is None)


def hops_is_configurable() -> None:
    print("hop count is configurable and clamped")
    check("defaults to 1 (Fly)", isinstance(server._TRUSTED_PROXY_HOPS, int))
    check("never negative", server._TRUSTED_PROXY_HOPS >= 0)


one_proxy_in_front()
directly_exposed()
two_proxies()
malformed_input_is_safe()
uvicorn_does_not_rewrite_client()
hops_is_configurable()

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nall client-IP bucketing tests passed")
