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


async def run() -> None:
    app = server._build_http_app(TOKEN, rate_limit=5, inner=stub_inner)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        print("health (no auth)")
        r = await c.get("/health")
        check("200 with body", r.status_code == 200 and r.json() == {"ok": True}, str(r.status_code))

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


asyncio.run(run())

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nall HTTP smoke tests passed")
