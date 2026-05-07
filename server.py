"""Local + remote MCP server wrapping EstNLTK for Estonian NLP.

Exposes morphological analysis, lemmatization, POS tagging, tokenization,
spell-check + suggestions, syllabification, and NER as MCP tools so any
LLM client can write better Estonian in real time.

Two transports:

* `stdio` (default) — subprocess wired by Claude Desktop / Claude Code /
  Cursor / Cowork local mode / etc. Pure local, no network.
* `streamable-http` — ASGI server on `$PORT` exposing `/mcp` for remote
  clients (claude.ai web Custom Connectors, Smithery hosting, Cowork
  remote, self-hosted Fly.io). Bearer-token auth required; per-token
  rate limit.

Security posture: no shell exec, no filesystem writes, no outbound
network. Inputs size-bounded. HTTP mode refuses to start without a
configured auth token. See SECURITY.md.
"""

from __future__ import annotations

import argparse
import base64
import collections
import json
import logging
import os
import secrets
import sys
import time
from functools import lru_cache
from typing import Any

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Input-size caps. Bound memory + analysis time so a hostile or runaway
# prompt can't OOM the host or freeze the client.
MAX_TEXT_CHARS = 100_000
MAX_WORD_CHARS = 200

# HTTP-mode rate limits.
# Private mode (bearer auth required): per-token, generous default.
# Public mode (no auth, anyone can call): per-IP, tighter default.
DEFAULT_RATE_LIMIT_PER_MINUTE = 60
DEFAULT_PUBLIC_RATE_LIMIT_PER_MINUTE = 120

# Bumped manually in lockstep with pyproject.toml's [project].version.
SERVER_VERSION = "0.1.0"

log = logging.getLogger("estonian-mcp")

mcp = FastMCP("estonian-mcp")
# FastMCP's constructor doesn't accept a server-version kwarg, so reach
# into the underlying MCPServer to override the SDK-default version that
# would otherwise show up in `initialize` responses (and Smithery's UI).
mcp._mcp_server.version = SERVER_VERSION


def _check_text(text: str, *, limit: int = MAX_TEXT_CHARS, name: str = "text") -> None:
    if not isinstance(text, str):
        raise TypeError(f"{name} must be a string")
    if len(text) > limit:
        raise ValueError(
            f"{name} length {len(text)} exceeds limit {limit}; "
            "split the input into smaller chunks"
        )


@lru_cache(maxsize=1)
def _Text():
    from estnltk import Text
    return Text


@lru_cache(maxsize=1)
def _vabamorf():
    from estnltk.vabamorf.morf import Vabamorf
    return Vabamorf.instance()


def _first(values: list[Any] | None) -> Any:
    if not values:
        return None
    return values[0]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def tokenize(text: str) -> dict:
    """Split Estonian text into sentences and words.

    Returns a dict with `sentences` (list of strings) and `words` (list of strings).
    Input is capped at 100,000 characters.
    """
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["sentences"])
    sentences = [s.enclosing_text for s in t.sentences]
    words = [w.text for w in t.words]
    return {"sentences": sentences, "words": words}


@mcp.tool()
def analyze_morphology(text: str, all_analyses: bool = False) -> list[dict]:
    """Run full morphological analysis on Estonian text.

    For each word returns lemma(s), part-of-speech, grammatical form, root,
    ending, clitic and compound parts. By default returns the first (most
    likely) analysis per word; set `all_analyses=True` to return every
    ambiguous analysis. Input is capped at 100,000 characters.
    """
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["morph_analysis"])
    out: list[dict] = []
    for span in t.morph_analysis:
        word = span.text
        lemmas = list(span.lemma)
        pos = list(span.partofspeech)
        forms = list(span.form)
        roots = list(span.root)
        endings = list(span.ending)
        clitics = list(span.clitic)
        root_tokens = [list(rt) for rt in span.root_tokens]
        if all_analyses:
            analyses = [
                {
                    "lemma": lemmas[i],
                    "partofspeech": pos[i],
                    "form": forms[i],
                    "root": roots[i],
                    "ending": endings[i],
                    "clitic": clitics[i],
                    "root_tokens": root_tokens[i] if i < len(root_tokens) else [],
                }
                for i in range(len(lemmas))
            ]
            out.append({"word": word, "analyses": analyses})
        else:
            out.append({
                "word": word,
                "lemma": _first(lemmas),
                "partofspeech": _first(pos),
                "form": _first(forms),
                "root": _first(roots),
                "ending": _first(endings),
                "clitic": _first(clitics),
                "root_tokens": _first(root_tokens) or [],
            })
    return out


@mcp.tool()
def lemmatize(text: str) -> list[dict]:
    """Return lemma (dictionary form) for each word in the text.

    Concise output: `[{"word": ..., "lemma": ...}, ...]`. Input is capped at
    100,000 characters.
    """
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["morph_analysis"])
    return [
        {"word": span.text, "lemma": _first(list(span.lemma))}
        for span in t.morph_analysis
    ]


@mcp.tool()
def pos_tag(text: str) -> list[dict]:
    """Return part-of-speech tag for each word.

    POS tag set: S=noun, V=verb, A=adj, P=pron, D=adv, K=adp, J=conj,
    N=numeral, I=interj, Y=abbrev, X=foreign, Z=punct, etc. Input is capped
    at 100,000 characters.
    """
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["morph_analysis"])
    return [
        {"word": span.text, "partofspeech": _first(list(span.partofspeech))}
        for span in t.morph_analysis
    ]


@mcp.tool()
def spell_check(text: str, suggestions: bool = True) -> list[dict]:
    """Check Estonian spelling for each word and optionally return suggestions.

    Returns one entry per word with `text`, `spelling` (bool), and
    `suggestions` (list of correction candidates) when `suggestions=True`.
    Input is capped at 100,000 characters.
    """
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["words"])
    words = [w.text for w in t.words]
    if not words:
        return []
    return _vabamorf().spellcheck(words, suggestions=suggestions)


@mcp.tool()
def syllabify(word: str) -> list[dict]:
    """Split a single Estonian word into syllables with quantity and accent.

    Each syllable entry: `{"syllable": str, "quantity": int, "accent": int}`.
    Input is capped at 200 characters and must contain no whitespace.
    """
    _check_text(word, limit=MAX_WORD_CHARS, name="word")
    if any(ch.isspace() for ch in word):
        raise ValueError("syllabify expects a single word, no whitespace")
    from estnltk.vabamorf.morf import syllabify_word
    return syllabify_word(word)


@mcp.tool()
def named_entities(text: str) -> list[dict]:
    """Extract named entities (PER/LOC/ORG) using EstNLTK's CRF model.

    Returns `[{"text": ..., "type": ..., "start": ..., "end": ...}, ...]`.
    Input is capped at 100,000 characters.
    """
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["ner"])
    return [
        {
            "text": ne.enclosing_text,
            "type": ne.nertag,
            "start": ne.start,
            "end": ne.end,
        }
        for ne in t.ner
    ]


# ---------------------------------------------------------------------------
# HTTP transport: bearer auth + rate limit
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Per-token leaky-bucket rate limiter (in-process, restart-resets).

    Sufficient for one-process containers. Behind a load balancer with
    multiple replicas, each replica enforces independently — combined
    quota is N*replicas, which is acceptable for a defence-in-depth
    measure.
    """

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.buckets: dict[str, collections.deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - 60.0
        bucket = self.buckets.setdefault(key, collections.deque())
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self.per_minute:
            return False
        bucket.append(now)
        return True


def _extract_token(scope: dict) -> str | None:
    """Pull a token from either Authorization header or Smithery ?config= param."""
    headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])}
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None

    # Smithery passes user config as base64(JSON) in ?config=
    query_string = scope.get("query_string", b"").decode("latin1")
    if not query_string:
        return None
    for part in query_string.split("&"):
        if not part.startswith("config="):
            continue
        encoded = part[len("config="):]
        # url-decode minimal: smithery sends raw base64 url-safe
        try:
            padded = encoded + "=" * (-len(encoded) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii"))
            cfg = json.loads(raw.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return None
        for field in ("apiKey", "bearerToken", "token"):
            v = cfg.get(field)
            if isinstance(v, str) and v:
                return v
        return None
    return None


async def _send_status(send, status: int, body: dict[str, Any]) -> None:
    payload = json.dumps(body).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": payload})


def _client_ip(scope: dict) -> str:
    """Best-effort originator IP. uvicorn(proxy_headers=True) populates
    scope["client"] from X-Forwarded-For when running behind Fly/Smithery."""
    client = scope.get("client") or ("unknown", 0)
    return client[0] if isinstance(client, (tuple, list)) and client else "unknown"


def _build_http_app(token: str | None, rate_limit: int, public_mode: bool = False, inner=None):
    """Wrap an ASGI MCP app with auth (or none) + rate limit + /health bypass.

    public_mode=False (default): require bearer token, rate-limit per token.
    public_mode=True:           no auth, rate-limit per client IP.

    `inner` defaults to FastMCP's streamable-http app; tests inject a stub.
    """
    if inner is None:
        from mcp.server.transport_security import TransportSecuritySettings

        # FastMCP auto-enables DNS-rebinding protection with a
        # localhost-only host allowlist when constructed with the
        # default host=127.0.0.1. That allowlist is baked in at
        # construction time and rejects any request from a real
        # domain (Fly, Smithery, custom). DNS-rebinding protection is
        # designed for browser attacks against localhost-bound dev
        # servers and doesn't apply behind HTTPS termination, so we
        # disable it here for HTTP-mode deployments.
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        )
        mcp.settings.stateless_http = True
        mcp.settings.json_response = True  # simpler for clients without SSE
        inner = mcp.streamable_http_app()
    limiter = _RateLimiter(rate_limit)

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            return await inner(scope, receive, send)
        if scope["type"] != "http":
            await _send_status(send, 400, {"error": "unsupported scope"})
            return

        path = scope.get("path", "")

        # Public health endpoint — no auth, no rate limit. Used by Fly probes
        # and uptime monitoring.
        if path == "/health":
            await _send_status(send, 200, {"ok": True})
            return

        # Smithery + similar registries probe this for auto-discovery.
        # Spec: https://smithery.ai/docs/build/publish#troubleshooting
        if path == "/.well-known/mcp/server-card.json":
            card: dict[str, Any] = {
                "serverInfo": {"name": "estonian-mcp", "version": SERVER_VERSION},
                "authentication": {"required": not public_mode},
                "endpoints": {"streamable_http": "/mcp"},
            }
            if not public_mode:
                card["authentication"]["schemes"] = ["bearer"]
            await _send_status(send, 200, card)
            return

        if public_mode:
            bucket_key = f"ip:{_client_ip(scope)}"
        else:
            provided = _extract_token(scope)
            if not provided or token is None or not secrets.compare_digest(provided, token):
                await _send_status(send, 401, {"error": "unauthorized"})
                return
            # Bucket on truncated token so we don't log full secrets.
            bucket_key = provided[:8]

        if not limiter.allow(bucket_key):
            await _send_status(send, 429, {"error": "rate_limited"})
            return

        await inner(scope, receive, send)

    return app


def _run_http(host: str, port: int, token: str | None, rate_limit: int, public_mode: bool) -> None:
    import uvicorn  # local import; only needed in HTTP mode

    log.info(
        "starting estonian-mcp HTTP transport on %s:%d (path=/mcp, mode=%s, rate_limit=%d/min)",
        host, port, "public" if public_mode else "bearer", rate_limit,
    )
    app = _build_http_app(token, rate_limit, public_mode=public_mode)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,  # keep tokens out of logs
        proxy_headers=True,
        forwarded_allow_ips="*",  # behind Fly/Smithery edge
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="estonian-mcp", description=__doc__.splitlines()[0])
    p.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.environ.get("ESTNLTK_MCP_TRANSPORT", "stdio"),
        help="stdio for local clients, http for remote (default: stdio)",
    )
    p.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="HTTP bind host (default: 0.0.0.0)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8081")),
        help="HTTP bind port (default: $PORT or 8081)",
    )
    p.add_argument(
        "--public",
        action="store_true",
        default=os.environ.get("ESTNLTK_MCP_PUBLIC_MODE", "").strip() in ("1", "true", "yes"),
        help="Public mode: no bearer auth required, per-IP rate limit",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)

    if args.transport == "stdio":
        mcp.run()
        return

    token: str | None = None
    if args.public:
        default_rate = DEFAULT_PUBLIC_RATE_LIMIT_PER_MINUTE
        log.warning("public mode: bearer auth disabled, per-IP rate limit only")
    else:
        token = os.environ.get("ESTNLTK_MCP_AUTH_TOKEN", "").strip()
        if not token:
            sys.stderr.write(
                "ERROR: ESTNLTK_MCP_AUTH_TOKEN env var is required in HTTP mode.\n"
                "Either set the token or pass --public / ESTNLTK_MCP_PUBLIC_MODE=1.\n"
                "Generate one: python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
            )
            sys.exit(2)
        if len(token) < 16:
            sys.stderr.write("ERROR: ESTNLTK_MCP_AUTH_TOKEN must be at least 16 characters.\n")
            sys.exit(2)
        default_rate = DEFAULT_RATE_LIMIT_PER_MINUTE

    rate_limit = int(os.environ.get(
        "ESTNLTK_MCP_RATE_LIMIT_PER_MINUTE", str(default_rate)
    ))
    _run_http(args.host, args.port, token, rate_limit, public_mode=args.public)


if __name__ == "__main__":
    main()
