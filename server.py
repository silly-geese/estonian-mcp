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
from mcp.types import ToolAnnotations

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

# Favicon served at /favicon.svg and /favicon.ico so Google's favicon
# service (used by the Anthropic Connectors Directory + tool-call UI in
# Claude) can fetch our icon when probing estonian-mcp.fly.dev. The
# same SVG lives at logo.svg in the repo for direct upload to the
# Directory submission form. Estonian flag, rounded square.
FAVICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" '
    b'role="img" aria-label="estonian-mcp"><title>estonian-mcp</title>'
    b'<defs><clipPath id="r"><rect width="64" height="64" rx="10" ry="10"/>'
    b'</clipPath></defs><g clip-path="url(#r)">'
    b'<rect width="64" height="21.33" fill="#0072CE"/>'
    b'<rect y="21.33" width="64" height="21.34" fill="#000000"/>'
    b'<rect y="42.67" width="64" height="21.33" fill="#FFFFFF"/></g>'
    b'<rect x="0.5" y="0.5" width="63" height="63" rx="9.5" ry="9.5" '
    b'fill="none" stroke="#cfd4d9" stroke-width="1"/></svg>'
)

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


@lru_cache(maxsize=1)
def _wordnet():
    from estnltk.wordnet import Wordnet
    return Wordnet()


@lru_cache(maxsize=1)
def _embeddings():
    """Lazy-load the compressed fastText model used by find_related_words."""
    import compress_fasttext
    path = os.environ.get(
        "ESTNLTK_MCP_FASTTEXT_PATH",
        "/opt/models/fasttext-et-mini",
    )
    return compress_fasttext.models.CompressedFastTextKeyedVectors.load(path)


# Phase-1 register lexicons. Hand-curated; coarse by design. Real register
# lives in syntax (sentence structure, address forms, passive voice) which
# this approach misses, so treat the score as a directional hint, not a
# verdict. Phase 2 (corpus-trained classifier) is the upgrade path.

_FORMAL_MARKERS: frozenset[str] = frozenset({
    # Officialese / legal-administrative markers
    "käesolev", "käesolevalt", "vastavalt", "tulenevalt", "lähtuvalt",
    "alusel", "raames", "kohaselt", "antud", "nimetatud", "kohaldatav",
    "sätestatud", "määratletud", "ettenähtud", "ette nähtud",
    "järgnevalt", "eelnevalt", "punkt", "lõige", "alapunkt",
    # Formal verbs (lemmas)
    "sätestama", "kohaldama", "tagama", "teostama", "korraldama",
    "viitama", "esitama", "rakendama", "võimaldama", "tähistama",
    "määrama", "otsustama", "kinnitama", "kehtestama", "väljendama",
    # Formal-register conjunctions / connectives
    "seetõttu", "seega", "muuhulgas", "sealhulgas", "millest tulenevalt",
    "millele viidates", "eeltoodust",
})

_COLLOQUIAL_MARKERS: frozenset[str] = frozenset({
    # Discourse particles / interjections of casual speech
    "noh", "nojah", "nojaa", "vot", "ahsoo", "mhm",
    "kuule", "kuulge", "njah", "nuhh", "ahaa",
    # Anglicisms / youth slang
    "okei", "cool", "lahe", "vinge", "mõnus", "vahva", "äge",
    "krõbe", "jurakas", "kihvt",
    # NOTE: deliberately excluding pronouns ("see", "no"), neutral
    # adverbs ("ikka", "vist", "natuke"), and the bare interjection
    # "ah" because they appear in formal text too. Adding them caused
    # false positives that swung neutral prose to "colloquial".
})

# Punctuation we'll skip when matching markers
_PUNCT_RE = None  # populated lazily; see _classify_register


def _first(values: list[Any] | None) -> Any:
    if not values:
        return None
    return values[0]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(
    title="Tokenize Estonian text",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
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


@mcp.tool(annotations=ToolAnnotations(
    title="Estonian morphological analysis",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
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


@mcp.tool(annotations=ToolAnnotations(
    title="Lemmatize Estonian words",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
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


@mcp.tool(annotations=ToolAnnotations(
    title="Part-of-speech tagging",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
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


@mcp.tool(annotations=ToolAnnotations(
    title="Estonian spell check",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
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


@mcp.tool(annotations=ToolAnnotations(
    title="Syllabify Estonian word",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
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


@mcp.tool(annotations=ToolAnnotations(
    title="Estonian named entity recognition",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
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


@mcp.tool(annotations=ToolAnnotations(
    title="Find semantically related Estonian words",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
def find_related_words(word: str, n: int = 10) -> dict:
    """Find Estonian words semantically similar to the input via fastText.

    Returns the top-n nearest neighbours by cosine similarity over a
    pre-trained Estonian fastText model (Common Crawl + Wikipedia, 2018).
    Useful for breaking repetition, finding alternative phrasings, or
    expanding vocabulary when WordNet's exact-meaning synonyms aren't
    enough.

    Distinct from `synonyms`: that one returns WordNet synsets — words
    with the same meaning. This one returns words that *pattern* with
    the input in real Estonian text, which can include near-synonyms,
    related concepts, and (sometimes) antonyms.

    Known quirks of the embedding model:
    - **Inflections crowd the top results** for some words. fastText
      sees `kasutama` and `kasutada` as related because the surface
      forms share subword n-grams; you may want to lemmatize matches
      yourself to dedupe.
    - **Antonyms can appear** because antonyms occur in similar
      contexts (`tark` may surface `loll`). Treat the list as
      "semantically nearby" rather than "synonymous."
    - **Polysemy is not disambiguated.** `lahe` (which means both
      "bay" and the colloquial "cool") will return whichever sense
      dominates the training data.

    Single-word input only, capped at 200 characters.
    """
    _check_text(word, limit=MAX_WORD_CHARS, name="word")
    if any(ch.isspace() for ch in word):
        raise ValueError("find_related_words expects a single word, no whitespace")
    n = max(1, min(int(n), 50))
    kv = _embeddings()
    matches = kv.most_similar(word, topn=n)
    return {
        "word": word,
        "matches": [
            {"word": w, "score": round(float(s), 4)} for w, s in matches
        ],
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Estonian WordNet synonyms",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
def synonyms(word: str, max_synsets: int = 5) -> dict:
    """Look up Estonian synonyms via WordNet.

    Returns synsets (groups of synonymous lemmas) for the input word, each
    with its definition and example usages. Useful when you want Claude to
    pick a different word with the same meaning, e.g. swap an over-used
    verb in marketing copy. Word-sense ambiguity is preserved: a polysemous
    word returns multiple synsets, one per meaning. Input capped at 200
    characters.
    """
    _check_text(word, limit=MAX_WORD_CHARS, name="word")
    if any(ch.isspace() for ch in word):
        raise ValueError("synonyms expects a single word, no whitespace")
    wn = _wordnet()
    synsets = wn[word] or []
    out: list[dict] = []
    for s in synsets[:max_synsets]:
        out.append({
            "name": s.name,
            "pos": s.pos,
            "definition": s.definition,
            "examples": list(s.examples) if s.examples else [],
            "lemmas": list(s.lemmas),
        })
    return {"word": word, "synsets": out, "synset_count": len(synsets)}


def _classify_register(text: str) -> dict:
    """Pure helper, also used by tests."""
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["morph_analysis"])

    formal_hits: list[str] = []
    colloquial_hits: list[str] = []
    word_count = 0

    for span in t.morph_analysis:
        word = span.text
        # Skip punctuation
        if not any(ch.isalpha() for ch in word):
            continue
        word_count += 1
        # Test against both surface form and best lemma; lower-cased.
        lemma = (list(span.lemma)[0] if span.lemma else "").lower()
        surface = word.lower()
        for candidate in {surface, lemma}:
            if not candidate:
                continue
            if candidate in _FORMAL_MARKERS:
                formal_hits.append(candidate)
                break
            if candidate in _COLLOQUIAL_MARKERS:
                colloquial_hits.append(candidate)
                break

    # Score: positive = formal, negative = colloquial, 0 = neutral.
    # Normalise by word count so longer text doesn't dominate.
    if word_count == 0:
        score = 0.0
    else:
        raw = len(formal_hits) - len(colloquial_hits)
        score = max(-1.0, min(1.0, raw * 4.0 / word_count))

    if score >= 0.25:
        tier = "formal"
    elif score >= 0.05:
        tier = "neutral-formal"
    elif score <= -0.25:
        tier = "colloquial"
    elif score <= -0.05:
        tier = "neutral-colloquial"
    else:
        tier = "neutral"

    return {
        "tier": tier,
        "score": round(score, 3),
        "formal_markers": sorted(set(formal_hits)),
        "colloquial_markers": sorted(set(colloquial_hits)),
        "word_count": word_count,
        "note": (
            "Heuristic phase-1 classifier — lexicon-based, lemma-aware. "
            "Catches obvious officialese vs slang; most newsletter prose "
            "scores 'neutral'. Treat as a directional hint, not a verdict."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Classify Estonian register (formal vs colloquial)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
def classify_register(text: str) -> dict:
    """Heuristic register classifier for Estonian (formal vs colloquial).

    Returns a tier label, a normalised score in [-1, 1] (positive = formal,
    negative = colloquial), and the matched formal/colloquial markers found
    in the text. Useful for sanity-checking that marketing copy hasn't
    drifted into officialese, or that a contract draft hasn't slipped into
    chat tone.

    PHASE-1 LIMITATION: this is a coarse lexicon-based heuristic, not a
    trained model. Real register also lives in sentence structure,
    address forms, and passive voice — none of which this catches. Most
    newsletter prose scores 'neutral'. Use the result as a directional
    hint, not a verdict. Input capped at 100,000 characters.
    """
    return _classify_register(text)


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

        # Favicon — public, no auth. Google's favicon service probes
        # /favicon.ico; modern browsers also accept /favicon.svg.
        if path in ("/favicon.svg", "/favicon.ico"):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"image/svg+xml"),
                    (b"content-length", str(len(FAVICON_SVG)).encode("ascii")),
                    (b"cache-control", b"public, max-age=86400"),
                ],
            })
            await send({"type": "http.response.body", "body": FAVICON_SVG})
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
