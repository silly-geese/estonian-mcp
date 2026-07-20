"""HTTP-mode smoke tests: auth + rate limit + health bypass.

Exercises the auth wrapper directly with a stub inner app — no uvicorn,
no FastMCP lifespan, no subprocess. Fast and deterministic.

Tool-dispatch over HTTP is covered by the live-curl block in CI's
docker-build job; this file pins the auth surface.

Run via:

    uv run python tests/test_http.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # transitive dep of mcp
import server  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS {label}")
    else:
        failures.append(f"{label}: {detail}")
        print(f"  FAIL {label} {detail}")


TOKEN = "test-token-abcdef0123456789"


async def stub_inner(scope, receive, send):
    """Minimal ASGI app that returns {"ok": "stub"} for any HTTP request."""
    if scope["type"] == "lifespan":
        msg = await receive()
        while msg["type"] != "lifespan.shutdown":
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            msg = await receive()
        await send({"type": "lifespan.shutdown.complete"})
        return
    body = b'{"ok":"stub"}'
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii"))],
    })
    await send({"type": "http.response.body", "body": body})


async def boom_inner(scope, receive, send):
    """ASGI app that raises before sending anything — simulates an
    unhandled failure escaping the inner MCP app."""
    if scope["type"] == "lifespan":
        msg = await receive()
        while msg["type"] != "lifespan.shutdown":
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            msg = await receive()
        await send({"type": "lifespan.shutdown.complete"})
        return
    raise RuntimeError("simulated inner-app failure")


async def echo_inner(scope, receive, send):
    """ASGI app that drains the full request body and echoes it back. Used
    to prove the session-counter's body peek replays the stream byte-for-
    byte to the inner app."""
    if scope["type"] == "lifespan":
        msg = await receive()
        while msg["type"] != "lifespan.shutdown":
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            msg = await receive()
        await send({"type": "lifespan.shutdown.complete"})
        return
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body", False):
            break
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii"))],
    })
    await send({"type": "http.response.body", "body": body})


async def logging_boom_inner(scope, receive, send):
    """Mimics the MCP SDK's inner-500 path: log the exception via the `mcp.*`
    logger (carrying exc_info) and then return its OWN 500 — the exception
    does NOT propagate to our wrapper. Verifies we borrow the logged type."""
    if scope["type"] == "lifespan":
        msg = await receive()
        while msg["type"] != "lifespan.shutdown":
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            msg = await receive()
        await send({"type": "lifespan.shutdown.complete"})
        return
    try:
        raise KeyError("simulated inner protocol error")
    except Exception:
        logging.getLogger("mcp.server.streamable_http").exception("Error handling POST request")
    body = b'{"jsonrpc":"2.0","error":{"code":-32603}}'
    await send({
        "type": "http.response.start",
        "status": 500,
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii"))],
    })
    await send({"type": "http.response.body", "body": body})


async def run() -> None:
    app = server._build_http_app(TOKEN, rate_limit=5, inner=stub_inner)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        print("health (no auth)")
        r = await c.get("/health")
        body = r.json()
        check("200", r.status_code == 200, str(r.status_code))
        check("ok=true", body.get("ok") is True, str(body))
        check("version surfaced", isinstance(body.get("version"), str) and body["version"], str(body))
        check("tool count surfaced", isinstance(body.get("tools"), int) and body["tools"] > 0, str(body))

        print("well-known server card (no auth)")
        r = await c.get("/.well-known/mcp/server-card.json")
        check("server-card 200", r.status_code == 200, str(r.status_code))
        body = r.json()
        check("server-card name", body.get("serverInfo", {}).get("name") == "estonian-mcp")
        check("server-card auth required (bearer mode)", body.get("authentication", {}).get("required") is True)
        check("server-card advertises /mcp", body.get("endpoints", {}).get("streamable_http") == "/mcp")

        print("browser GET /mcp redirect")
        r = await c.get("/mcp", headers={"Accept": "text/html,application/xhtml+xml"})
        check("browser GET /mcp → 302", r.status_code == 302, str(r.status_code))
        check("redirect points to /", r.headers.get("location") == "/", r.headers.get("location"))
        # A real MCP client carrying text/event-stream must NOT be redirected;
        # it reaches the inner app (stub returns ok:stub here).
        r = await c.get("/mcp", headers={"Accept": "text/event-stream"})
        check("MCP GET (event-stream) not redirected", r.status_code != 302, str(r.status_code))

        print("/sse helpful 404")
        r = await c.get("/sse")
        check("/sse → 404", r.status_code == 404, str(r.status_code))
        check("/sse points to /mcp", r.json().get("endpoint") == "/mcp", str(r.json()))

        print("favicon caching (ETag / 304)")
        r = await c.get("/favicon.svg")
        check("favicon.svg → 200", r.status_code == 200, str(r.status_code))
        check("favicon.svg is svg", r.headers.get("content-type") == "image/svg+xml")
        etag = r.headers.get("etag")
        check("favicon.svg has ETag", bool(etag), str(r.headers))
        check("favicon.svg immutable cache",
              "immutable" in (r.headers.get("cache-control") or ""), r.headers.get("cache-control"))
        # Conditional re-fetch with the ETag → 304, no body.
        r2 = await c.get("/favicon.svg", headers={"If-None-Match": etag})
        check("favicon.svg conditional → 304", r2.status_code == 304, str(r2.status_code))
        check("304 has empty body", not r2.content, repr(r2.content[:20]))

        print("metrics")
        r = await c.get("/metrics")
        check("metrics 200", r.status_code == 200)
        body = r.json()
        check("metrics has total_requests", "total_requests" in body)
        check("metrics has by_status dict", isinstance(body.get("by_status"), dict))
        check("metrics has by_path dict", isinstance(body.get("by_path"), dict))
        check("metrics has uptime_seconds", "uptime_seconds" in body)
        check("metrics has recent_errors list", isinstance(body.get("recent_errors"), list))
        check("metrics has sessions_total int", isinstance(body.get("sessions_total"), int))

        print("auth")
        r = await c.post("/mcp", json={})
        check("missing token → 401", r.status_code == 401)

        r = await c.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"})
        check("wrong token → 401", r.status_code == 401)

        r = await c.post("/mcp", json={}, headers={"Authorization": f"Bearer {TOKEN}"})
        check("bearer header good → 200", r.status_code == 200)
        check("inner app reached", r.json() == {"ok": "stub"})

        print("smithery ?config=")
        encoded = base64.urlsafe_b64encode(
            json.dumps({"apiKey": TOKEN}).encode("utf-8")
        ).decode("ascii").rstrip("=")
        r = await c.post(f"/mcp?config={encoded}", json={})
        check("?config= apiKey → 200", r.status_code == 200)

        # bearerToken / token field aliases
        for field in ("bearerToken", "token"):
            enc = base64.urlsafe_b64encode(
                json.dumps({field: TOKEN}).encode("utf-8")
            ).decode("ascii").rstrip("=")
            r = await c.post(f"/mcp?config={enc}", json={})
            check(f"?config= {field} field → 200", r.status_code == 200)

        bad = base64.urlsafe_b64encode(
            json.dumps({"apiKey": "wrong"}).encode("utf-8")
        ).decode("ascii").rstrip("=")
        r = await c.post(f"/mcp?config={bad}", json={})
        check("?config= wrong token → 401", r.status_code == 401)

        # malformed base64 should not crash, just 401
        r = await c.post("/mcp?config=!!!notbase64", json={})
        check("?config= malformed → 401", r.status_code == 401)

        print("rate limit (limit=5)")
        # Fresh token bucket per test (different prefix); spam until 429.
        statuses = []
        for _ in range(15):
            r = await c.post("/mcp", json={}, headers={"Authorization": f"Bearer {TOKEN}"})
            statuses.append(r.status_code)
            if r.status_code == 429:
                break
        check("rate limit eventually triggers 429", 429 in statuses, str(statuses))

    print("public mode")
    pub_app = server._build_http_app(token=None, rate_limit=4, public_mode=True, inner=stub_inner)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pub_app), base_url="http://t") as c:
        r = await c.get("/health")
        check("public: /health → 200", r.status_code == 200)

        r = await c.get("/.well-known/mcp/server-card.json")
        check("public: server-card 200", r.status_code == 200)
        check(
            "public: server-card auth not required",
            r.json().get("authentication", {}).get("required") is False,
        )

        r = await c.post("/mcp", json={})
        check("public: /mcp no token → 200", r.status_code == 200)
        check("public: inner reached", r.json() == {"ok": "stub"})

        r = await c.post("/mcp", json={}, headers={"Authorization": "Bearer whatever"})
        check("public: ignores bearer header → 200", r.status_code == 200)

        # Per-IP rate limit: ASGITransport pins client to ("client", X), so all
        # requests share one bucket and we can drive it to 429.
        statuses = []
        for _ in range(15):
            r = await c.post("/mcp", json={})
            statuses.append(r.status_code)
            if r.status_code == 429:
                break
        check("public: per-IP rate limit triggers 429", 429 in statuses, str(statuses))

    print("unhandled-error guard")
    server._recent_errors.clear()
    boom_app = server._build_http_app(token=None, rate_limit=50, public_mode=True, inner=boom_inner)
    transport2 = httpx.ASGITransport(app=boom_app)
    async with httpx.AsyncClient(transport=transport2, base_url="http://t") as c:
        r = await c.post("/mcp", json={})
        check("inner exception → clean 500 (not a raw crash)", r.status_code == 500, str(r.status_code))
        check("500 body is structured", r.json().get("error") == "internal_error", str(r.json()))

        # The 500 should leave a PII-free breadcrumb in the ring buffer,
        # visible at /metrics, with the exception type captured.
        r = await c.get("/metrics")
        errs = r.json().get("recent_errors", [])
        check("recent_errors captured the 500", len(errs) >= 1, str(errs))
        if errs:
            last = errs[-1]
            check("recent_error path is /mcp", last.get("path") == "/mcp", str(last))
            check("recent_error status is 500", last.get("status") == 500, str(last))
            check("recent_error type is RuntimeError", last.get("error") == "RuntimeError", str(last))
            check("recent_error has ts", isinstance(last.get("ts"), int), str(last))


def metrics_persistence_test() -> None:
    """Round-trip test for _save_persistent_stats / _load_persistent_stats.
    Synchronous; doesn't need the async http client."""
    import tempfile
    from pathlib import Path
    print("metrics persistence (round-trip)")
    saved_path = server._METRICS_PATH
    saved_total = server._STATS["total"]
    saved_status = dict(server._STATS["by_status"])
    saved_pathd = dict(server._STATS["by_path"])
    saved_errors = list(server._recent_errors)
    saved_sessions = server._STATS.get("sessions", 0)
    try:
        with tempfile.TemporaryDirectory() as d:
            server._METRICS_PATH = Path(d) / "metrics.json"
            server._STATS["total"] = 12345
            server._STATS["by_status"] = {"200": 12000, "429": 345}
            server._STATS["by_path"] = {"/mcp": 12300, "/health": 45}
            server._STATS["sessions"] = 678
            server._recent_errors.clear()
            server._recent_errors.append({"ts": 1700000000, "path": "/mcp", "status": 500, "error": "RuntimeError"})
            server._save_persistent_stats()
            check("file written", server._METRICS_PATH.exists())
            # wipe + restore
            server._STATS["total"] = 0
            server._STATS["by_status"] = {}
            server._STATS["by_path"] = {}
            server._STATS["sessions"] = 0
            server._recent_errors.clear()
            server._load_persistent_stats()
            check("total restored", server._STATS["total"] == 12345)
            check("by_status restored", server._STATS["by_status"] == {"200": 12000, "429": 345})
            check("by_path restored", server._STATS["by_path"] == {"/mcp": 12300, "/health": 45})
            check("sessions restored", server._STATS["sessions"] == 678, str(server._STATS["sessions"]))
            check("recent_errors restored", list(server._recent_errors) == [
                {"ts": 1700000000, "path": "/mcp", "status": 500, "error": "RuntimeError"}], str(list(server._recent_errors)))
            # graceful no-op when parent dir is gone (local dev path)
            server._METRICS_PATH = Path("/this/path/does/not/exist/metrics.json")
            try:
                server._save_persistent_stats()
                check("graceful no-op when parent missing", True)
            except Exception as e:
                check("graceful no-op when parent missing", False, str(e))
    finally:
        server._METRICS_PATH = saved_path
        server._STATS["total"] = saved_total
        server._STATS["by_status"] = saved_status
        server._STATS["by_path"] = saved_pathd
        server._STATS["sessions"] = saved_sessions
        server._recent_errors.clear()
        server._recent_errors.extend(saved_errors)


def is_initialize_unit_test() -> None:
    """Pure checks for the initialize detector — the heart of the session
    counter. Critically, a tool call whose text merely contains the word
    'initialize' must NOT be counted."""
    print("_is_initialize_request (unit)")
    init = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"x"}}'
    check("initialize → True", server._is_initialize_request(init) is True)
    tool = b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"spell_check"}}'
    check("tools/call → False", server._is_initialize_request(tool) is False)
    sneaky = b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"spell_check","arguments":{"text":"please initialize the session"}}}'
    check("tool call mentioning 'initialize' in text → False",
          server._is_initialize_request(sneaky) is False)
    check("empty body → False", server._is_initialize_request(b"") is False)
    check("malformed JSON → False", server._is_initialize_request(b'{not json initialize') is False)
    batch = b'[{"jsonrpc":"2.0","id":1,"method":"initialize"},{"jsonrpc":"2.0","method":"ping"}]'
    check("batch with initialize → True", server._is_initialize_request(batch) is True)


async def inner_exc_capture_test() -> None:
    """An inner-returned 500 (SDK logs the exception, then returns 500 itself)
    should be labelled in the ring buffer with the logged exception type,
    not error=None."""
    print("inner-500 exception-type capture")
    server._recent_errors.clear()
    server._last_inner_exc["type"] = None  # reset the capture slot
    app = server._build_http_app(token=None, rate_limit=100, public_mode=True,
                                 inner=logging_boom_inner)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/mcp", json={})
        check("inner 500 status", r.status_code == 500, str(r.status_code))
        errs = list(server._recent_errors)
        check("inner 500 recorded in buffer", len(errs) >= 1, str(errs))
        if errs:
            last = errs[-1]
            check("inner 500 labelled with type (not None)",
                  last.get("error") == "KeyError", str(last))
            check("inner 500 path/status correct",
                  last.get("path") == "/mcp" and last.get("status") == 500, str(last))
    # Stale-guard: an old logged type must not leak onto a later 5xx.
    server._last_inner_exc["ts"] = 0.0
    check("stale exception type ignored", server._inner_exc_type() is None)


async def session_counter_test() -> None:
    """End-to-end: an initialize POST bumps sessions_total AND the body is
    replayed to the inner app byte-for-byte; a non-initialize POST does not
    bump it. Uses echo_inner to prove the replay."""
    print("session counter")
    app = server._build_http_app(token=None, rate_limit=100, public_mode=True, inner=echo_inner)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        base = server._STATS["sessions"]

        init_body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {}}}
        r = await c.post("/mcp", json=init_body)
        check("initialize → 200", r.status_code == 200, str(r.status_code))
        check("initialize body replayed to inner verbatim", r.json() == init_body, r.text)
        check("initialize bumped sessions by 1",
              server._STATS["sessions"] == base + 1, str(server._STATS["sessions"]))

        call_body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "spell_check", "arguments": {"text": "tere"}}}
        r = await c.post("/mcp", json=call_body)
        check("tools/call → 200", r.status_code == 200, str(r.status_code))
        check("tools/call body replayed verbatim", r.json() == call_body, r.text)
        check("tools/call did NOT bump sessions",
              server._STATS["sessions"] == base + 1, str(server._STATS["sessions"]))


asyncio.run(run())
metrics_persistence_test()
is_initialize_unit_test()
asyncio.run(session_counter_test())
asyncio.run(inner_exc_capture_test())

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nall HTTP smoke tests passed")
