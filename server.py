"""Local + remote MCP server wrapping EstNLTK for Estonian NLP.

Exposes morphological analysis, lemmatization, POS tagging, tokenization,
spell-check + suggestions, syllabification, and NER as MCP tools so any
LLM client can write better Estonian in real time.

Two transports:

* `stdio` (default): subprocess wired by Claude Desktop / Claude Code /
  Cursor / Cowork local mode / etc. Pure local, no network.
* `streamable-http`: ASGI server on `$PORT` exposing `/mcp` for remote
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
import bisect
import collections
import difflib
import hashlib
import heapq
import inspect
import json
import logging
import os
import re
import secrets
import sys
import time
import unicodedata
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Annotated, Any, TypedDict

import pydantic_core
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import TextContent, ToolAnnotations
from mcp.types import Tool as MCPTool
from pydantic import Field, StringConstraints, ValidationError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Input-size caps. Bound memory + analysis time so a hostile or runaway
# prompt can't OOM the host or freeze the client.
MAX_TEXT_CHARS = 100_000
MAX_WORD_CHARS = 200
# Structural tools (defined-term / cross-reference tracking) need the whole
# document at once, and legal texts run long. Parsing is cheap, so allow more.
MAX_DOC_CHARS = 500_000
# A per-call domain glossary (check_domain_terms, check_term_consistency) is
# bounded like text: by entry count and entry length. What bounds the cost of
# comparing compounds with it is the time budget on suggestions,
# _DOMAIN_SUGGEST_SECONDS; these keep the argument itself a sane size.
MAX_GLOSSARY_TERMS = 1_000
_GlossaryTerm = Annotated[str, StringConstraints(max_length=MAX_WORD_CHARS)]

# HTTP-mode rate limits.
# Private mode (bearer auth required): per-token, generous default.
# Public mode (no auth, anyone can call): per-IP. Bumped from earlier
# 60/120 defaults after real-world usage showed legitimate active
# sessions (parallel tool calls + multiple users behind shared NATs)
# brushing the ceiling. The defence-in-depth math still holds: at
# 300/min/IP, a sustained attacker burns ~30s CPU/min/IP on Fly's
# shared-cpu-1x, ~5% capacity. Cloudflare in front is the right answer
# for actual DDoS, not tighter per-IP limits.
DEFAULT_RATE_LIMIT_PER_MINUTE = 120
DEFAULT_PUBLIC_RATE_LIMIT_PER_MINUTE = 300
# Per-IP limit on the public paths answered without reaching /mcp (see
# _STATIC_PATHS), in either mode. A browser loading the landing page
# makes two or three of these requests, and the icons are cached for a
# year, so 60 a minute from one address is a client in a loop. Until
# 0.6.4 these paths had no limit at all, and something fetched
# /favicon.svg about 6M times between 2026-07-18 and 2026-08-14.
DEFAULT_STATIC_RATE_LIMIT_PER_MINUTE = 60

# How many reverse proxies sit in front of this server. Used to pick the
# trustworthy entry out of X-Forwarded-For for the public-mode per-IP rate
# limiter (see _client_ip). Fly.io puts exactly one proxy in front, which
# is the default. Set to 0 if the server is directly internet-exposed, so
# that a caller-supplied XFF is never trusted.
_TRUSTED_PROXY_HOPS = max(0, int(os.environ.get("ESTNLTK_MCP_TRUSTED_PROXY_HOPS", "1")))

# Bumped manually in lockstep with pyproject.toml's [project].version.
SERVER_VERSION = "1.0.1"

# Favicons served alongside the MCP endpoint so Google's favicon service
# (used by the Anthropic Connectors Directory + tool-call UI in Claude)
# can fetch our icon when probing estonian-mcp.fly.dev.
#
# Google's pipeline only accepts raster (PNG/ICO/JPG): it rejects SVG,
# so /favicon.ico must serve the PNG bytes to be picked up. We keep
# /favicon.svg for modern user agents that prefer scalable.
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

# Pre-rasterised PNG of logo.svg (64x64, transparent corners). Generated
# at build/dev time via `rsvg-convert -w 64 -h 64 logo.svg -o logo.png`
# and shipped in the Docker image. If it's missing for any reason, we
# fall back to serving the SVG at the .ico path, which Google still
# can't read, but at least browsers will get something.
_LOGO_PNG_PATH = Path(__file__).resolve().parent / "logo.png"
try:
    FAVICON_PNG: bytes | None = _LOGO_PNG_PATH.read_bytes()
except OSError:
    FAVICON_PNG = None

# Content-hash ETags for the static icons so conditional requests can be
# answered with a bodyless 304 instead of re-sending the bytes. The icons
# are fixed per build, so a hash of the bytes is a stable validator.
_FAVICON_SVG_ETAG = '"' + hashlib.md5(FAVICON_SVG).hexdigest()[:16] + '"'
_FAVICON_PNG_ETAG = (
    '"' + hashlib.md5(FAVICON_PNG).hexdigest()[:16] + '"' if FAVICON_PNG else None
)

# Minimal HTML landing page at /. Two purposes:
# 1. Google's favicon scraper fetches / first and parses <link rel="icon">
#    tags before trying /favicon.ico. With no HTML response at /, the
#    scraper gives up and serves a generic placeholder. The link tags
#    here make our PNG the canonical icon.
# 2. Humans who paste estonian-mcp.fly.dev into a browser see something
#    useful instead of a 404.
INDEX_HTML = b"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>estonian-mcp</title>
<meta name="description" content="Estonian NLP MCP server: spell-check, morphology, synonyms, NER for AI agents writing Estonian.">
<link rel="icon" type="image/png" sizes="64x64" href="/favicon.png">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="shortcut icon" href="/favicon.ico">
<style>
  body { font-family: system-ui, -apple-system, sans-serif; max-width: 640px; margin: 4rem auto; padding: 0 1rem; line-height: 1.5; color: #1a1a1a; }
  code { background: #f3f3f3; padding: 0.1em 0.4em; border-radius: 3px; }
  a { color: #0072CE; }
  img.flag { display: inline-block; width: 32px; height: 32px; vertical-align: middle; margin-right: 8px; }
</style>
</head>
<body>
<h1><img class="flag" src="/favicon.svg" alt="">estonian-mcp</h1>
<p>Estonian NLP MCP server: spell-check, morphology, synonyms, NER, and more, exposed as MCP tools so AI agents stop hallucinating Estonian.</p>
<p>MCP endpoint: <code>https://estonian-mcp.fly.dev/mcp</code></p>
<p>Source: <a href="https://github.com/silly-geese/estonian-mcp">silly-geese/estonian-mcp</a> &nbsp;\xc2\xb7&nbsp; Listing: <a href="https://smithery.ai/servers/silly-geese/estonian-mcp">Smithery</a></p>
</body>
</html>
"""

log = logging.getLogger("estonian-mcp")

# Server-level description / system prompt. Surfaced in the MCP
# `initialize` response (`instructions`), which registries like Smithery
# read as the server description, and which gives an LLM client context
# on what the server is for and how to use it.
SERVER_INSTRUCTIONS = (
    "Estonian NLP for agents that write, edit or proofread Estonian: "
    "spelling, morphology, inflection paradigms, synonyms, EKI-Reeglid "
    "orthography checks, and register, style, terminology and legal checks. "
    "Treat the results as ground truth rather than guessing spelling, case "
    "forms or inflections: models routinely produce plausible but wrong "
    "Estonian morphology. A spell_check pass does not prove a word is real "
    "Estonian (Vabamorf accepts any well-formed compound, including ones you "
    "coin), so check compounds you coined with check_compound_familiarity. "
    "For text about an organisation's own systems, check_domain_terms reports "
    "the compounds that neither the lexicons nor its glossary vouch for (pass "
    "its terms in glossary when the server has none). Legal Estonian: "
    "check_legalese is a plain-language aid that lists the terms of art to "
    "keep, check_defined_terms maps defined terms and cross-references in "
    "long documents, and common_legal_usage gives canonical collocations so "
    "you do not invent legal phrasing. "
    "REACH FOR THESE TOOLS ON EDITORIAL QUESTIONS TOO. 'Right word here?': "
    "synonyms. 'Too bureaucratic', 'make it read more human': "
    "check_officialese for kantseliit in reports, academic and business "
    "prose (check_legalese is for statutes and stays silent on those), plus "
    "check_style for umbisikuline tegumood and rhythm. 'Keep the terminology "
    "consistent': check_term_consistency. 'Sounds translated': "
    "check_translationese. A correctly spelled, well-formed "
    "word can still be the wrong register or the wrong term for its domain, "
    "and the mechanical checks cannot tell you that. "
    "WRITING ESTONIAN: compose from the meaning, never word for word from "
    "English (Estonian says the same in about a quarter fewer words). Outside "
    "documents (reports, contracts, policies, academic text) keep it short: "
    "10 to 15 words a sentence, one idea each, no filler or hype. When "
    "editing, change only what is wrong or unclear, keep the writer's words "
    "and never return a longer text unless asked. Reread every draft once as "
    "an Estonian editor would, then run check_translationese on it. "
    "The check_* issue lists are heuristics: a hit is a candidate to verify, "
    "not a verdict, and an empty list means nothing was found in that "
    "check's scope, not that the text is correct. In Estonian replies, quote "
    "the *_estonian labels, rules and summaries (form_estonian, "
    "rule_estonian, summary_estonian) verbatim instead of translating the "
    "English codes. A result field that does not apply is omitted. Text "
    f"inputs are capped at {MAX_TEXT_CHARS:,} characters (check_defined_terms: "
    f"{MAX_DOC_CHARS:,}) and single words at {MAX_WORD_CHARS}. All tools are "
    "read-only and make no outbound calls; results are UTF-8, so keep "
    "õ ä ö ü š ž intact."
)

# Keys whose value maps NAMES to subschemas. A property called "title" is
# data, not the annotation, so these are walked name by name.
_SCHEMA_NAME_MAPS = ("properties", "$defs", "definitions", "patternProperties")


def _lean_schema(node: Any, *, output: bool) -> Any:
    """Drop JSON Schema annotations that change no validation result: every
    `title` (pydantic's "Text", "tokenizeArguments"), and in output schemas
    `default: null` and `additionalProperties: true`, which restate the JSON
    Schema defaults. Input defaults stay: they tell the model what leaving
    an argument out means."""
    if isinstance(node, list):
        return [_lean_schema(x, output=output) for x in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "title" and isinstance(value, str):
            continue
        if output and ((key == "default" and value is None)
                       or (key == "additionalProperties" and value is True)):
            continue
        if key in _SCHEMA_NAME_MAPS and isinstance(value, dict):
            out[key] = {name: _lean_schema(sub, output=output) for name, sub in value.items()}
        else:
            out[key] = _lean_schema(value, output=output)
    return out


# Argument errors list at most this many fields. A glossary of 1,000
# over-long terms is 1,000 errors, and each would cost the caller tokens.
_MAX_ARG_ERRORS = 5

_LIST_ITEM = re.compile(r"^(?:[-*•]|\d+[.)])\s")


def _reflow(doc: str) -> str:
    """A docstring as the client should get it: dedented (Python 3.13 does
    that to __doc__, 3.11 and 3.12 do not), with each paragraph and list item
    on one line. Hard wrapping at 72 columns costs about 5% more tokens and
    tells the model nothing."""
    paragraphs = []
    for block in re.split(r"\n\s*\n", inspect.cleandoc(doc)):
        lines: list[str] = []
        for raw in block.splitlines():
            line = raw.strip()
            if lines and not _LIST_ITEM.match(line):
                lines[-1] += " " + line
            else:
                lines.append(line)
        paragraphs.append("\n".join(lines))
    return "\n\n".join(paragraphs)


class _EstonianMCP(FastMCP):
    """FastMCP with a leaner wire format. Tools and validation are unchanged.

    Every caller pays for what crosses the wire in tokens: the tool list on
    every turn, each result once it is read. Claude Code, Codex and VS Code
    show the model structuredContent; Cursor shows the text content; ChatGPT
    shows both. So both are kept lean:

    * tools/list: schemas without `title` noise (see _lean_schema), and
      descriptions without hard wrapping (see _reflow).
    * tools/call: ONE text block of compact JSON built from the tool's own
      return value. The SDK default is one indent=2 block per list item,
      so a 100-word spell_check came back as 97 blocks. structuredContent
      keeps exactly the keys the tool returned; the SDK default fills every
      declared-but-absent key with null. The lowlevel server still
      validates structuredContent against the advertised outputSchema.
    * argument errors: one clause per field (at most _MAX_ARG_ERRORS),
      without pydantic's docs URL or a repr of the caller's input.
    """

    async def list_tools(self) -> list[MCPTool]:
        tools = await super().list_tools()
        for t in tools:
            if t.description:
                t.description = _reflow(t.description)
            t.inputSchema = _lean_schema(t.inputSchema, output=False)
            if t.outputSchema is not None:
                t.outputSchema = _lean_schema(t.outputSchema, output=True)
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        tool = self._tool_manager.get_tool(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name}")
        meta = tool.fn_metadata
        try:
            raw = await tool.run(arguments, context=self.get_context(), convert_result=False)
        except ToolError as e:
            cause = e.__cause__
            if isinstance(cause, ValidationError) and cause.title == meta.arg_model.__name__:
                errors = cause.errors(include_url=False, include_input=False)
                shown = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}"
                                  for err in errors[:_MAX_ARG_ERRORS])
                more = len(errors) - _MAX_ARG_ERRORS
                if more > 0:
                    shown += f"; and {more} more"
                raise ToolError(f"Input validation error: {shown}") from cause
            raise
        if meta.output_model is None or not isinstance(raw, (dict, list)):
            return meta.convert_result(raw)  # not this server's shape: SDK default
        # Compact, keeps õäöü, and NaN/inf become null as in structuredContent
        # (a bare NaN is not JSON, and a strict client would reject the block).
        text = pydantic_core.to_json(raw, fallback=str, inf_nan_mode="null").decode()
        payload = {"result": raw} if meta.wrap_output else raw
        structured = meta.output_model.model_validate(payload).model_dump(
            mode="json", by_alias=True, exclude_unset=True)
        return [TextContent(type="text", text=text)], structured


mcp = _EstonianMCP("estonian-mcp", instructions=SERVER_INSTRUCTIONS)
# FastMCP's constructor doesn't accept a server-version kwarg, so reach
# into the underlying MCPServer to override the SDK-default version that
# would otherwise show up in `initialize` responses (and Smithery's UI).
mcp._mcp_server.version = SERVER_VERSION


def _count_registered_tools() -> int:
    """Count tools registered on the FastMCP instance. Computed once at
    import time so /health doesn't pay the cost per request."""
    try:
        return len(mcp._tool_manager.list_tools())
    except Exception:
        return 0


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


_RESOURCE_FETCH_HINT = (
    "Fetch it from a checkout with: uv run python scripts/fetch_resources.py "
    "(the server never downloads resources by itself; see PRIVACY.md)."
)


def _forbid_resource_downloads() -> None:
    """Make PRIVACY.md's "no outbound HTTP calls" structural, not aspirational.

    Two libraries below us will silently reach for the network mid-tool-call
    if a resource is missing, and neither is reachable from our own call
    sites, so guarding each tool individually cannot close them:

    1. `estnltk.resource_utils.get_resources_index()` re-fetches its index
       from RESOURCES_INDEX_URL whenever the local copy is missing OR older
       than INDEX_TIMEOUT (2 h). On a long-lived server that is a periodic
       outbound request, and when it fails the caller sees "resource
       missing" rather than "lookup failed": a false negative on top of a
       broken promise. Setting the timeout to effectively infinite pins us
       to the on-disk index.
    2. `estnltk`'s sentence tokenizer catches `LookupError` for NLTK's
       `punkt_tab` and calls `nltk.downloader.download()`. The `sentences`
       layer is built by `tag_layer(["morph_analysis"])`, so nearly every
       tool reaches it. Replacing the downloader with a refusal turns a
       silent fetch into an actionable error.

    Best-effort and non-fatal: if either library's internals move, we lose
    the guard but not the server. tests/test_no_network.py is what actually
    proves the promise holds.
    """
    try:
        import estnltk.resource_utils as _ru
        _ru.INDEX_TIMEOUT = sys.maxsize
    except Exception:
        pass

    def _refuse(*args, **kwargs):
        name = args[0] if args else kwargs.get("info_or_id", "a resource")
        raise RuntimeError(
            f"estonian-mcp does not download resources while serving "
            f"(tried to fetch {name!r}). {_RESOURCE_FETCH_HINT}"
        )

    try:
        import nltk
        import nltk.downloader as _nd
        _nd.download = _refuse
        nltk.download = _refuse
    except Exception:
        pass


_forbid_resource_downloads()


def _keep_logs_off_stdout() -> None:
    """Remove the logging handler `import estnltk` puts on the root logger.

    estnltk/helpers/logger.py adds a TqdmLoggingHandler to the ROOT logger
    and sets it to INFO; the handler prints through tqdm.write(), which
    writes to sys.stdout. Under stdio transport stdout is the JSON-RPC
    channel, so every INFO record from any library went into it: the SDK's
    "Processing request of type CallToolRequest" on each call, gensim's
    three lines while loading the fastText model. sys.stdout buffers apart
    from the wrapper the SDK writes responses through, so once 8 KB piled
    up a flush could put half a log line in front of a response. The
    client skipped that line as non-JSON and waited for an answer already
    sent: check_compound_familiarity hung until the client's 30-minute
    idle timeout. FastMCP's RichHandler already logs to stderr, so nothing
    is lost. tests/test_stdio_channel.py is what proves stdout stays clean.

    Runs after _forbid_resource_downloads(), whose import of
    estnltk.resource_utils is what installs the handler. Best-effort, like
    that guard: if EstNLTK renames the class, the test goes red.
    """
    try:
        from estnltk.helpers.logger import TqdmLoggingHandler
    except Exception:
        return
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, TqdmLoggingHandler):
            root.removeHandler(handler)


_keep_logs_off_stdout()


def _wordnet_available() -> bool:
    """True if Estonian WordNet is unpacked on disk, WITHOUT any network.

    Checks the resources directory directly rather than calling
    `get_resource_paths()`, which consults EstNLTK's resource *index* and
    re-fetches that index over HTTPS when it is more than two hours old:
    an outbound call from inside a tool, and a false "missing" verdict
    whenever it fails. See _forbid_resource_downloads.

    Deliberately NOT cached, unlike `_wordnet()` below. Caching the probe
    would reintroduce the very confusion issue #38 was about: an operator
    sees `degraded: true`, runs `scripts/fetch_resources.py` as the message
    tells them to, calls the tool again, and a cached `False` still says
    degraded until they restart the process. The probe is a directory
    listing; there is nothing to buy here. `_wordnet()` stays cached
    because it loads a heavy object, and is only reached once this is True.
    """
    try:
        from estnltk.resource_utils import get_resources_dir
        root = Path(get_resources_dir()) / "wordnet"
        if not root.is_dir():
            return False
        # A version subdirectory with the sqlite files unpacked inside it.
        return any(
            any(child.glob("*.db")) for child in root.iterdir() if child.is_dir()
        )
    except Exception:
        return False


@lru_cache(maxsize=1)
def _wordnet():
    from estnltk.wordnet import Wordnet
    return Wordnet()


# Where the fastText model may live, in priority order. The container path
# comes first for the image; the cache path is where
# scripts/fetch_resources.py puts it on a source install. Having only the
# container default meant a source install could follow the documented
# setup, be told "All resources present", and still fail every fastText
# tool: the script cannot export a variable into the server process, and
# JSON-configured MCP clients cannot run a shell `export` at all.
_FASTTEXT_CANDIDATES: tuple[str, ...] = (
    "/opt/models/fasttext-et-medium",
    str(Path.home() / ".cache" / "estnltk-mcp" / "fasttext-et-medium"),
)


def _fasttext_path() -> str:
    """Resolve the model path: explicit env override, else first candidate
    that exists. Returns the container default when none exist, so the
    error message names a concrete path."""
    env = os.environ.get("ESTNLTK_MCP_FASTTEXT_PATH")
    if env:
        return env
    for candidate in _FASTTEXT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return _FASTTEXT_CANDIDATES[0]


@lru_cache(maxsize=1)
def _embeddings():
    """Lazy-load the compressed fastText model used by find_related_words,
    check_compound_familiarity and check_domain_terms."""
    import compress_fasttext
    path = _fasttext_path()
    if not Path(path).exists():
        raise RuntimeError(
            f"The fastText model is not installed (looked in "
            f"{', '.join(_FASTTEXT_CANDIDATES)}). {_RESOURCE_FETCH_HINT}"
        )
    return compress_fasttext.models.CompressedFastTextKeyedVectors.load(path)


@lru_cache(maxsize=1)
def _legal_index() -> dict:
    """Lazy-load the legal collocation/frequency index (built offline by
    scripts/build_legal_collocations.py from streamed legal corpora). Defaults
    to the bundled POC index shipped with the package; override with
    ESTNLTK_MCP_LEGAL_INDEX to point at the full-corpus artifact."""
    import gzip
    path = os.environ.get("ESTNLTK_MCP_LEGAL_INDEX") or str(
        Path(__file__).resolve().parent / "data" / "legal_collocations.json.gz")
    raw = Path(path).read_bytes()
    if path.endswith(".gz"):
        raw = gzip.decompress(raw)
    return json.loads(raw)


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

# Algustäheortograafia (initial-letter orthography) lexicons. Used by
# check_capitalization. Names that should be lowercase mid-sentence in
# Estonian: weekday names, month names, nationalities, and adjectives
# derived from country/language names when used attributively before a
# culture/language noun. Hand-curated; not exhaustive, covers the
# most common AI-generated mistakes per EKI's Reeglid.

_WEEKDAYS_ET: frozenset[str] = frozenset({
    "esmaspäev", "teisipäev", "kolmapäev", "neljapäev",
    "reede", "laupäev", "pühapäev",
})

_MONTHS_ET: frozenset[str] = frozenset({
    "jaanuar", "veebruar", "märts", "aprill", "mai", "juuni",
    "juuli", "august", "september", "oktoober", "november", "detsember",
})

_NATIONALITIES_ET: frozenset[str] = frozenset({
    "eestlane", "venelane", "soomlane", "sakslane", "rootslane",
    "lätlane", "leedulane", "prantslane", "inglane", "ameeriklane",
    "hispaanlane", "itaallane", "poolakas", "ungarlane", "taanlane",
    "kreeklane", "türklane", "araablane", "hiinlane", "jaapanlane",
    "korealane", "vietnamlane", "tšehh", "slovakk", "horvaat",
    "sloveen", "ukrainlane", "valgevenelane", "rumeenlane",
    "bulgaarlane", "serblane", "albaanlane", "kasahh", "usbekk",
})

_LANG_ADJECTIVES_ET: frozenset[str] = frozenset({
    "eesti", "vene", "inglise", "soome", "saksa", "rootsi", "läti",
    "leedu", "prantsuse", "hispaania", "itaalia", "poola", "tšehhi",
    "slovaki", "ungari", "taani", "norra", "kreeka", "türgi",
    "araabia", "hiina", "jaapani", "korea", "vietnami", "pärsia",
    "heebrea", "ladina", "bulgaaria", "ukraina", "valgevene",
    "rumeenia", "serbia", "horvaadi", "sloveeni", "albaania",
    "makedoonia", "armeenia", "gruusia", "kasahhi", "usbeki",
    "mongoli",
})

_CULTURE_NOUNS_ET: frozenset[str] = frozenset({
    # Words that, when preceded by a language/country adjective,
    # signal it's the adjective rather than the country proper-noun.
    "keel", "kõne", "sõna", "sõnastik", "sõnaraamat", "grammatika",
    "kirjandus", "kultuur", "kunst", "köök", "muusika", "tants",
    "rahvas", "tava", "ajalugu", "etnograafia", "folkloor",
    "ortograafia", "õigekiri", "haridus", "kool",
})

# Bigram lexicon for check_compounds. Keys are lowercased surface
# tokens. Each (word_a, word_b) represents a common AI mis-split that
# should be a single compound word. The value is the joined form.
# Hand-curated; phase-1 coverage.
_COMPOUND_BIGRAMS: dict[tuple[str, str], str] = {
    ("kooli", "maja"): "koolimaja",
    ("laste", "aed"): "lasteaed",
    ("laste", "aias"): "lasteaias",
    ("raamatu", "kogu"): "raamatukogu",
    ("ema", "keel"): "emakeel",
    ("kõrg", "kool"): "kõrgkool",
    ("üli", "kool"): "ülikool",
    ("alg", "kool"): "algkool",
    ("kesk", "kool"): "keskkool",
    ("kesk", "öö"): "keskööd",
    ("ette", "panek"): "ettepanek",
    ("nädala", "vahetus"): "nädalavahetus",
    ("nädala", "vahetusel"): "nädalavahetusel",
    ("nädala", "vahetuseks"): "nädalavahetuseks",
    ("aasta", "aeg"): "aastaaeg",
    ("aasta", "ajal"): "aastaajal",
    ("päeva", "kava"): "päevakava",
    ("kohvi", "kann"): "kohvikann",
    ("kohvi", "tass"): "kohvitass",
    ("töö", "koht"): "töökoht",
    ("töö", "kohale"): "töökohale",
    ("raha", "kott"): "rahakott",
    ("tervise", "kindlustus"): "tervisekindlustus",
    ("öko", "süsteem"): "ökosüsteem",
    ("info", "tehnoloogia"): "infotehnoloogia",
    ("ühis", "kond"): "ühiskond",
    ("ühis", "konnas"): "ühiskonnas",
    ("välis", "minister"): "välisminister",
    ("pea", "minister"): "peaminister",
    ("siseministeerium",): "siseministeerium",  # placeholder, removed below
    ("vee", "mass"): "veemass",
    ("toidu", "aine"): "toiduaine",
    ("toidu", "ained"): "toiduained",
    ("õhu", "saaste"): "õhusaaste",
    ("õhu", "rõhk"): "õhurõhk",
    ("metsa", "raie"): "metsaraie",
    ("õpilas", "esindus"): "õpilasesindus",
    ("õpetajate", "tuba"): "õpetajatetuba",
}
# trim placeholder
_COMPOUND_BIGRAMS = {k: v for k, v in _COMPOUND_BIGRAMS.items() if len(k) == 2}

# Marked-usage lexicon for analyze_morphology's usage_note annotation.
# Each entry is a lemma that is technically correct but stylistically
# marked (archaic, foreign, or otherwise non-neutral). The flag tells
# Claude this lemma is unusual without it having to guess. Curated and
# small on purpose (phase-1 coverage).

_MARKED_LEMMAS_ET: dict[str, tuple[str, str]] = {
    # archaic-formal alternatives to neutral words
    "tarvitama":   ("archaic",  "vananenud (neutraalne: kasutama)"),
    "nõnda":       ("archaic",  "vananenud (neutraalne: nii)"),
    "ent":         ("archaic",  "vananenud (neutraalne: aga)"),
    "kuid":        ("archaic",  "kirjakeelne (kõnekeelne: aga)"),
    "vaid":        ("archaic",  "kirjakeelne (kõnekeelne: ainult)"),
    "ülla":        ("archaic",  "vananenud (neutraalne: õilis)"),
    "siiski":      ("archaic",  "kirjakeelne"),
    "ehkki":       ("archaic",  "kirjakeelne (kõnekeelne: kuigi)"),
    "ometi":       ("archaic",  "kirjakeelne"),
    "senini":      ("archaic",  "kirjakeelne (neutraalne: seni)"),
    "kohaselt":    ("archaic",  "ametlik (neutraalne: vastavalt)"),
    # anglicisms / foreign words with Estonian alternatives
    "okei":        ("foreign",  "anglitsism (eesti: olgu, hästi)"),
    "super":       ("foreign",  "anglitsism (eesti: vahva, äge)"),
    "cool":        ("foreign",  "anglitsism (eesti: lahe, äge)"),
    "meeting":     ("foreign",  "anglitsism (eesti: kohtumine, koosolek)"),
    "email":       ("foreign",  "anglitsism (eesti: e-kiri)"),
    "weekend":     ("foreign",  "anglitsism (eesti: nädalavahetus)"),
    "deadline":    ("foreign",  "anglitsism (eesti: tähtaeg)"),
    "feedback":    ("foreign",  "anglitsism (eesti: tagasiside)"),
    "team":        ("foreign",  "anglitsism (eesti: meeskond)"),
    "boss":        ("foreign",  "anglitsism (eesti: ülemus)"),
}

# POS-tag-based usage notes. Maps Vabamorf POS codes that signal
# non-routine usage. Skipped: S/V/A/P/D/K/J/N/Z (standard parts of
# speech, no special flag) and X, which is a verb-bound word (`plehku`
# in `pani plehku`), not a foreign one.
_POS_USAGE_NOTES_ET: dict[str, tuple[str, str]] = {
    "Y": ("abbreviation", "lühend"),
    "I": ("interjection", "interjektsioon"),
    "H": ("proper-noun", "pärisnimi"),
}

# Paradigm form lists for the new `paradigm` tool. Forms passed to
# Vabamorf.synthesize(lemma, form, pos). Phase-1 scope: the most
# commonly-needed forms per word class, not every possible form.

# `adt` is the SHORT ILLATIVE (lühike sisseütlev, aditiiv): majja beside
# majasse, kätte beside käesse, merre beside meresse. It is a Vabamorf
# form of its own, spelled without a number prefix ("adt", never
# "sg adt", which synthesises nothing), and it exists only in the
# singular. Words that have no short illative return nothing for it and
# the slot is simply absent, which is what every empty form does here.
#
# Leaving it out was not a missing nicety. For maja, tuba, käsi, meri and
# kivi the short form is the one Estonians write, so a table containing
# only majasse invites a caller to "correct" a correct majja.
_NOMINAL_FORMS: tuple[str, ...] = (
    "sg n", "sg g", "sg p", "sg ill", "adt", "sg in", "sg el", "sg all",
    "sg ad", "sg abl", "sg tr", "sg ter", "sg es", "sg ab", "sg kom",
    "pl n", "pl g", "pl p", "pl ill", "pl in", "pl el", "pl all",
    "pl ad", "pl abl", "pl tr", "pl ter", "pl es", "pl ab", "pl kom",
)

# Human-readable Estonian labels for the case forms.
_CASE_LABELS_ET: dict[str, str] = {
    "sg n": "ainsuse nimetav", "sg g": "ainsuse omastav",
    "sg p": "ainsuse osastav", "sg ill": "ainsuse sisseütlev",
    "adt": "ainsuse lühike sisseütlev",
    "sg in": "ainsuse seesütlev", "sg el": "ainsuse seestütlev",
    "sg all": "ainsuse alaleütlev", "sg ad": "ainsuse alalütlev",
    "sg abl": "ainsuse alaltütlev", "sg tr": "ainsuse saav",
    "sg ter": "ainsuse rajav", "sg es": "ainsuse olev",
    "sg ab": "ainsuse ilmaütlev", "sg kom": "ainsuse kaasaütlev",
    "pl n": "mitmuse nimetav", "pl g": "mitmuse omastav",
    "pl p": "mitmuse osastav", "pl ill": "mitmuse sisseütlev",
    "pl in": "mitmuse seesütlev", "pl el": "mitmuse seestütlev",
    "pl all": "mitmuse alaleütlev", "pl ad": "mitmuse alalütlev",
    "pl abl": "mitmuse alaltütlev", "pl tr": "mitmuse saav",
    "pl ter": "mitmuse rajav", "pl es": "mitmuse olev",
    "pl ab": "mitmuse ilmaütlev", "pl kom": "mitmuse kaasaütlev",
}

_VERB_FORMS: tuple[str, ...] = (
    # infinitives + supine
    "ma", "da", "des", "maks", "vat", "tavat", "mas", "mast", "mata",
    # present indicative (1sg, 2sg, 3sg, 1pl, 2pl, 3pl)
    "n", "d", "b", "me", "te", "vad",
    # past indicative
    "sin", "sid", "s", "sime", "site",
    # conditional
    # `ksid` covers the 2nd person singular and the 3rd person plural,
    # one surface with one label, so it is listed once. `sid` in the past
    # tense above is the same syncretism and is listed once too.
    "ksin", "ksid", "ks", "ksime", "ksite",
    # participles
    "nud", "tud", "v", "tav",
    # imperative
    "o", "gu", "gem", "ge",
    # umbisikuline tegumood. The table carried its participles (tud,
    # tav) and its quotative (tavat) but none of its indicative or
    # conditional forms, though this is the voice official Estonian is
    # largely written in: kasutatakse is rank 232 in the corpus
    # vocabulary, well ahead of kasutatavat at 24,225.
    "takse", "ti", "taks", "ta", "tagu",
    # synthetic past conditional
    "nuks",
)

# Passive-voice form codes from Vabamorf. When analyze_morphology
# returns one of these as the `form` for a V-pos word, the verb is in
# passive voice: Estonian's -takse / -ti / -tud / -tav family.
_PASSIVE_FORMS_ET: frozenset[str] = frozenset({
    "takse", "dakse",   # present passive
    "ti", "di",         # past passive (e.g. tehti, kasutati)
    "tud", "dud",       # past passive participle (tehtud, kasutatud)
    "tav", "dav",       # present passive participle (tehtav, kasutatav)
    "tava", "dava",     # umbisikuline kesksõna
    "taks", "daks",     # passive conditional
})

# Hedging / wishy-washy markers, counted to gauge how confident the
# prose reads. Higher density = more uncertain / less assertive copy.
# Hand-curated; single-word entries only (multi-word hedging phrases
# left for a later round).
# Lexicons for check_object_case, Estonian's object-case-government
# checker. Negation markers (lemma forms) and a small curated set of
# verbs whose direct objects are always partitive. Conservative scope:
# better to miss real errors than to flag a lot of false positives,
# since each flag costs the user attention.

_NEGATION_LEMMAS_ET: frozenset[str] = frozenset({
    "ei",       # main negation auxiliary
    "pole",     # "is not" / "are not"
    "polnud",   # "wasn't" / "weren't"
    "ära",      # imperative negation 2sg
    "ärge",     # imperative negation 2pl
    "ärgu",     # imperative negation 3rd
    "ärgem",    # imperative negation 1pl
    "mitte",    # negation particle
})

_PARTITIVE_ONLY_VERBS_ET: frozenset[str] = frozenset({
    "armastama", "vihkama", "vajama", "soovima", "ootama",
    "austama", "kartma", "puudutama", "tundma",
})

# Case-form codes whose objects we'd flag (nominative + genitive).
# These are the only realistic direct-object cases other than
# partitive, so a noun here in a negation/partitive-verb context is a
# candidate error.
_DIRECT_OBJECT_CASES: frozenset[str] = frozenset({"sg n", "sg g", "pl n", "pl g"})

# Case forms that are CLEARLY not direct objects (locative, temporal,
# adverbial). Used to skip false positives.
_NON_OBJECT_CASES: frozenset[str] = frozenset({
    "sg ill", "sg in", "sg el", "sg all", "sg ad", "sg abl",
    "sg tr", "sg ter", "sg es", "sg ab", "sg kom",
    "pl ill", "pl in", "pl el", "pl all", "pl ad", "pl abl",
    "pl tr", "pl ter", "pl es", "pl ab", "pl kom",
})

# check_object_case, teavitama vs teatama. teavitama and informeerima take
# the person in the partitive (keda + millest: teavitas politseid
# õnnetusest); teatama takes the recipient in the allative (kellele:
# teatas politseile). Sõnaveeb, ÕS 2025, Valik rektsioone and the AMSS give
# teavitama no allative, and RahaPTS § 49 lg 3^1 uses both correctly in one
# sentence. With the guards below the detector made 0 false flags on all
# 388 current statutes in Riigi Teataja (a naive version made 6) and on the
# edited Korp corpora; precision on unedited web and newspaper text was
# about 97 to 100%, at the price of about half the recall.
_TEAV_HINT = re.compile(r"teavit|informeeri|teata", re.IGNORECASE)
_TEAV_VERBS_ET: frozenset[str] = frozenset({"teavitama", "informeerima"})
# Only the action noun in the nominative. teavitus (a notice), teavitaja,
# the genitive teavitamise and the compound teavitamiskohustus have other
# lemmas or forms and never trigger.
_TEAV_NOUNS_ET: frozenset[str] = frozenset({"teavitamine", "informeerimine"})
# Vabamorf lemmatises teile/Teile as sina and meile as mina.
_TEAV_RECIPIENT_PRONOUNS_ET: frozenset[str] = frozenset({
    "mina", "sina", "tema", "meie", "teie", "nemad", "kes", "kõik",
    "igaüks", "keegi", "mõlemad", "teine", "kumbki", "iga",
})
# Matched against the last compound part of span.root (andme_büroo ->
# büroo), since the lemma carries no compound marks. An address, a phone
# number, an account or a deadline is not a recipient and is skipped.
_TEAV_RECIPIENT_HEADS_ET: frozenset[str] = frozenset({
    "amet", "ametkond", "asutus", "büroo", "inspektsioon", "keskus", "komisjon", "politsei",
    "pank", "kool", "omavalitsus", "ministeerium", "valitsus", "juhatus", "nõukogu", "kohus",
    "prokuratuur", "kantselei", "teenistus", "agentuur", "fond", "ettevõte", "firma", "ühing",
    "ühistu", "organisatsioon", "selts", "liit", "koda", "vald", "linnavalitsus", "kogu",
    "klient", "patsient", "juht", "ülemus", "kolleeg", "naaber", "sõber", "omanik", "liige",
    "osanik", "aktsionär", "investor", "partner", "isik", "inimene", "elanik", "kodanik",
    "õpilane", "tudeng", "üliõpilane", "ajakirjanik", "avalikkus", "üldsus", "meedia",
    "politseinik", "arst", "õde", "notar", "advokaat", "audiitor", "maakler", "operaator",
    "administraator", "abonent", "vanem", "lapsevanem", "laps", "pere", "perekond", "sugulane",
    "lähedane", "kaaslane", "ametnik", "spetsialist", "kasutaja", "töötaja", "tööandja",
    "kiirabi", "päästeamet", "häirekeskus", "tuletõrje", "kindlustusandja", "kandidaat",
    "esindaja", "volinik", "kantsler", "minister", "riik", "liikmesriik", "pool",
})
# A root ending in -ja is an agent noun (töö_andja, osuta=ja) unless it is
# one of these.
_TEAV_NOT_AGENT_JA_ET: frozenset[str] = frozenset({
    "maja", "raja", "kaja", "vaja", "tuja", "pooja",
})
# A word right before the allative phrase that governs it (vastavalt
# seadusele, lisaks klientidele, viitega USA ametlikele ...). Matched on the
# word form as well as the lemma: viitega is lemma viide.
_TEAV_ALLATIVE_GOVERNORS_ET: frozenset[str] = frozenset({
    "vastavalt", "tänu", "vaatamata", "lisaks", "sarnaselt", "erinevalt", "lähedal",
    "lähedale", "omakorda", "viitega", "viidates", "osutades", "tuginedes", "toetudes",
    "vastuseks", "võrreldes", "kohaselt", "vastukaaluks", "suunatud", "mõeldud",
})
# Nouns that take an allative attribute, so an elative one right before the
# allative owns it (teavitame muudatuse mõjust klientidele).
_TEAV_ALLATIVE_ATTR_NOUNS_ET: frozenset[str] = frozenset({
    "mõju", "juurdepääs", "ligipääs", "üleminek", "vastus", "teade", "teavitus", "sõnum",
    "kiri", "kõne", "pakkumine", "hüvitis", "toetus", "soodustus", "abi", "nõue", "kohustus",
    "õigus", "luba", "tegevusluba", "kaebus", "pöördumine", "avaldus", "ettepanek", "soovitus",
    "juhis", "korraldus", "ettekirjutus", "viide", "oht", "kahju", "kasu", "tasu", "makse",
    "väljamakse", "laen", "kingitus", "preemia", "auhind", "stipendium", "edasilükkamine",
})
# Verbs with an allative recipient of their own. Their -mine noun owns a
# nearby allative (ettevõtjale tegevusloa andmise otsusest).
_TEAV_DITRANSITIVE_VERBS_ET: frozenset[str] = frozenset({
    "andma", "saatma", "edastama", "esitama", "väljastama", "üle_andma", "määrama", "maksma",
    "tagastama", "müüma", "osutama", "pakkuma", "eraldama", "võimaldama", "tagama",
    "selgitama", "tutvustama", "näitama", "soovitama", "lubama", "delegeerima", "suunama",
    "teatama", "kätte_toimetama", "loovutama", "laenama", "üürima", "rentima", "omistama",
    "kinkima", "hüvitama", "kompenseerima", "vastama", "helistama", "kirjutama",
    "adresseerima", "esitlema",
})
_TEAV_DITRANSITIVE_ENDINGS = tuple(sorted(v.replace("_", "") for v in _TEAV_DITRANSITIVE_VERBS_ET))
# Verbs whose allative is a post, not a recipient. Their -mine noun right
# after a bare amet owns it: kantsleri ametile kandideerimise tingimustest.
# kolima stays out (teavitas tööandjale kolimisest is an error).
_TEAV_POST_GOAL_VERBS_ET: frozenset[str] = frozenset({"kandideerima", "asuma"})
# Adjectives that govern an allative of their own (kõigile osalejatele
# mõistetavas keeles, meile oluline).
_TEAV_ALLATIVE_ADJECTIVES_ET: frozenset[str] = frozenset({
    "oluline", "tähtis", "vajalik", "kasulik", "kahjulik", "ohtlik", "sobilik", "sobimatu",
    "tundmatu", "võõras", "omane", "iseloomulik", "kohane", "kohustuslik", "ebameeldiv",
    "ootamatu", "lihtne", "raske", "keeruline", "mugav", "kallis", "tänulik", "truu",
    "sarnane", "lähedane", "ühine", "selge", "kättesaamatu", "soodne", "ebasoodne",
    "jõukohane", "valus", "piinlik", "olulisem", "tähtsam", "vajalikum", "kasulikum",
    "arusaamatu", "teadmata", "teadaolev", "ühesugune", "erinev", "võrdne", "kohustuslikum",
    "mürgine", "ligipääsmatu", "ühtne", "kahjutu", "ohutu", "harjumatu", "tasuline",
    "vabatahtlik", "eakohane", "eluohtlik", "terviseohtlik",
})
# A compound counts as its last part in the two lists above (lisatoetus as
# toetus, ülioluline as oluline), except these heads, whose first part
# names a norm or a hazard rather than a person: ajakohane, nõuetekohane,
# tuleohtlik.
_TEAV_NON_INHERITING_HEADS_ET: frozenset[str] = frozenset({"kohane", "ohtlik", "ohutu"})
# The experiencer of an adverb or a translative noun right after the
# allative, not a recipient: meile teadaolevalt, kõigile üllatuseks, Teile
# teadmiseks. The nouns count only in the translative singular, so
# meile reedeks is still flagged.
_TEAV_EXPERIENCER_ADVERBS_ET: frozenset[str] = frozenset({
    "teadaolevalt", "arusaadavalt", "arusaamatult", "nähtavalt", "kuuldavalt",
    "märkamatult", "ootamatult", "mõistetavalt", "omaselt", "iseloomulikult",
})
_TEAV_EXPERIENCER_TRANSLATIVES_ET: frozenset[str] = frozenset({
    "üllatus", "rõõm", "heameel", "hoiatus", "teadmine", "tutvumine", "info",
    "informatsioon", "eeskuju", "meelehea", "pahameel", "meelehärm", "kurbus",
    "kurvastus", "pettumus", "kergendus", "hämmastus", "nördimus", "õnn",
})
# Clause boundaries: a conjunction, a relative pronoun not in the allative
# (kellele is a recipient) or a relative/interrogative adverb.
_TEAV_CLAUSE_CONJUNCTIONS_ET: frozenset[str] = frozenset({
    "ja", "ning", "või", "ega", "kui", "et", "sest", "aga", "kuid", "vaid",
    "nagu", "kuna", "kuigi", "ehk", "ent", "nii",
})
_TEAV_RELATIVE_PRONOUNS_ET: frozenset[str] = frozenset({"mis", "kes", "milline", "missugune"})
_TEAV_RELATIVE_ADVERBS_ET: frozenset[str] = frozenset({
    "kuhu", "kus", "kust", "kuidas", "millal", "miks", "kas", "kuni",
})
# The personal pronouns of the teatama rule, and the pronouns that end an
# allative phrase as its head.
_TEAV_PERSONAL_PRONOUNS_ET: frozenset[str] = frozenset({"mina", "sina", "tema", "meie", "teie"})
_TEAV_HEAD_PRONOUNS_ET: frozenset[str] = _TEAV_PERSONAL_PRONOUNS_ET | {
    "nemad", "kes", "keegi", "igaüks",
}
# A partitive of these is a time measure, not an object (kuu aega ette).
_TEAV_TIME_MEASURES_ET: frozenset[str] = frozenset({
    "aeg", "päev", "nädal", "kuu", "aasta", "tund", "minut", "sekund", "kord", "öö",
    "tööpäev", "kalendripäev",
})
# Finite forms: one after the allative phrase starts an unpunctuated clause
# of its own. The shorter list is the one the token right after the phrase
# is held against, where any other verb form is a participle or infinitive.
_TEAV_FINITE_FORMS: frozenset[str] = frozenset({
    "b", "vad", "s", "sid", "n", "d", "me", "te", "sin", "sime", "site", "takse", "ti",
    "neg", "o", "ge", "gem", "ks", "ksid", "nuks", "taks", "vat", "gu", "tagu", "ksin",
    "ksime", "ksite", "neg o", "neg nud", "neg tud", "neg ks", "neg vat", "neg gu",
    "neg ge", "neg me",
})
_TEAV_NEXT_FINITE_FORMS: frozenset[str] = frozenset({
    "b", "vad", "s", "sid", "n", "d", "me", "te", "sin", "sime", "site", "takse", "ti",
    "neg", "o", "ge", "gem", "ks", "ksid", "nuks", "taks", "vat",
})
_TEAV_NONFINITE_FORMS: frozenset[str] = frozenset({
    "tud", "nud", "v", "tav", "mata", "des", "da", "ma", "mas", "mast", "maks",
})
# Suggestions: the pronoun forms Vabamorf would otherwise have to guess.
_TEAV_PARTITIVE_OF_ALLATIVE: dict[str, str] = {
    "mulle": "mind", "minule": "mind", "sulle": "sind", "sinule": "sind",
    "talle": "teda", "temale": "teda", "meile": "meid", "teile": "teid",
    "neile": "neid", "kõigile": "kõiki", "kellele": "keda", "teistele": "teisi",
}
_TEAV_GENITIVE_OF_ALLATIVE: dict[str, str] = {
    "mulle": "minu", "minule": "minu", "sulle": "sinu", "sinule": "sinu",
    "talle": "tema", "temale": "tema", "meile": "meie", "teile": "teie",
    "neile": "nende", "kõigile": "kõigi", "kellele": "kelle", "teistele": "teiste",
}
_TEAV_ALLATIVE_OF_PARTITIVE: dict[str, str] = {
    "mind": "mulle", "sind": "sulle", "teda": "talle", "meid": "meile",
    "teid": "teile", "neid": "neile",
}


_HEDGING_WORDS_ET: frozenset[str] = frozenset({
    "võib-olla", "võibolla", "umbes", "vist", "pigem", "äkki",
    "ehk", "ilmselt", "arvatavasti", "tõenäoliselt", "mõnevõrra",
    "veidi", "üpris", "tundub", "näiliselt", "ligilähedaselt",
})

# POS tags whose lemmas we IGNORE when counting repetition: function
# words and connectives that naturally repeat in any prose and would
# drown out real content-word repetition signal.
_REPETITION_SKIP_POS: frozenset[str] = frozenset({
    "K",  # postposition / preposition
    "J",  # conjunction
    "P",  # pronoun
    "D",  # adverb (most are function-y; trade-off accepted)
    "Z",  # punctuation
    "Y",  # abbreviation
})


_VERB_LABELS_ET: dict[str, str] = {
    "ma": "ma-tegevusnimi", "da": "da-tegevusnimi",
    "des": "des-vorm", "maks": "maks-vorm",
    "vat": "vat-vorm", "tavat": "tavat-vorm", "mas": "mas-vorm",
    "mast": "mast-vorm", "mata": "mata-vorm",
    "n": "olevik 1.p ainsus", "d": "olevik 2.p ainsus",
    "b": "olevik 3.p ainsus", "me": "olevik 1.p mitmus",
    "te": "olevik 2.p mitmus", "vad": "olevik 3.p mitmus",
    "sin": "lihtminevik 1.p ainsus", "sid": "lihtminevik 2.p ainsus / 3.p mitmus",
    "s": "lihtminevik 3.p ainsus", "sime": "lihtminevik 1.p mitmus",
    "site": "lihtminevik 2.p mitmus",
    "ksin": "tingiv 1.p ainsus", "ksid": "tingiv 2.p ainsus / 3.p mitmus",
    "ks": "tingiv 3.p ainsus / pöördelõputa vorm",
    "ksime": "tingiv 1.p mitmus",
    "ksite": "tingiv 2.p mitmus",
    "nud": "isikuline mineviku kesksõna",
    "tud": "umbisikuline mineviku kesksõna",
    "v": "isikuline oleviku kesksõna",
    "tav": "umbisikuline oleviku kesksõna",
    # `o` is the imperative AND the form after `ei` (ei tea, ei kasuta),
    # and `gu` is 3rd person of either number (ta tulgu, nad tulgu).
    "o": "käskiv 2.p ainsus / isikuline eitav vorm",
    "gu": "käskiv 3.p ainsus / mitmus",
    "gem": "käskiv 1.p mitmus", "ge": "käskiv 2.p mitmus",
    "takse": "umbisikuline olevik", "ti": "umbisikuline lihtminevik",
    "taks": "umbisikuline tingiv", "ta": "umbisikuline eitav vorm",
    "tagu": "umbisikuline käskiv",
    "nuks": "tingiv minevik",
}


# Forms Vabamorf returns that no paradigm slot lists: the personal past
# conditional's persons, the past quotatives, and the umbisikuline
# supine and past conditional. Rare, but a caller who meets one deserves
# its name rather than a bare code.
_ANALYSIS_FORM_LABELS_ET: dict[str, str] = {
    "nuksin": "tingiv minevik 1.p ainsus",
    "nuksid": "tingiv minevik 2.p ainsus / 3.p mitmus",
    "nuksime": "tingiv minevik 1.p mitmus",
    "nuksite": "tingiv minevik 2.p mitmus",
    "nuvat": "nuvat-vorm", "tuvat": "tuvat-vorm",
    "tuks": "umbisikuline tingiv minevik",
    "tama": "umbisikuline ma-tegevusnimi",
}


# A `neg` code belongs to one of three words, and the same code means
# different things in each: `neg o` is `ära`, the imperative negator,
# but it is also `pole` and `lähe`, neither of which is an imperative.
# The lemma separates them (every other `neg` code belongs to one word),
# so the label is looked up by lemma rather than composed by prefixing
# "eitav" to the suffix's ordinary label, which called `pole` a command,
# `polnud` a participle and `ärme` an indicative.
_NEG_LEMMA_FAMILY: dict[str, str] = {
    "ära": "ära", "olema": "olema", "ole": "olema",
    "minema": "minema", "mine": "minema",
}
_NEG_LABELS_ET: dict[str, dict[str, str]] = {
    # ära, ärge, ärgem, ärme, ärgu: the negator's own imperative paradigm.
    "ära": {
        "o": "eitav käskiv 2.p ainsus",
        "ge": "eitav käskiv 2.p mitmus",
        "gem": "eitav käskiv 1.p mitmus",
        "me": "eitav käskiv 1.p mitmus",
        # Vabamorf gives `neg gu` no person: `ärgu` covers `ärgu ta
        # tulgu`, `ärgu nad tulgu` and the umbisikuline `ärgu tehtagu`.
        "gu": "eitav käskiv",
    },
    # pole, polda, polnud, poldud, poleks, polnuks, polevat: olema's
    # contracted negatives, which carry no person at all.
    "olema": {
        "o": "eitav olevik",
        "da": "umbisikuline eitav vorm",
        "nud": "eitav lihtminevik",
        "tud": "umbisikuline eitav lihtminevik",
        "ks": "eitav tingiv",
        "nuks": "eitav tingiv minevik",
        "vat": "eitav vat-vorm",
    },
    # lähe: minema's suppletive negative stem, as in `ei lähe`. The same
    # slot other verbs fill with the plain `o` code (ei tule, ei anna).
    "minema": {"o": "isikuline eitav vorm"},
}


# Subordinating / coordinating conjunctions where Estonian comma rules
# require a comma immediately before. `kui`, `mis`, `kes` deliberately
# excluded: they're highly context-dependent (kui = "when/if" needs
# comma but kui = "than/as" doesn't; mis can be relative or
# interrogative; kes similar) and the false-positive cost outweighs
# the catch rate for v1.
_COMMA_BEFORE: frozenset[str] = frozenset({
    "et", "kuna", "sest", "kuigi", "kuid", "vaid", "nagu",
    "mistõttu", "millepärast", "kuhu",
})

# Two-part connectives (EKI teatmik, Koma: ühendsidendid): the first parts
# that can stand right before the subordinator. A comma before the whole
# connective sets it off (Sadas vihma, nii et jäime koju).
_PAIR_FIRST: dict[str, frozenset[tuple[str, ...]]] = {
    "et": frozenset({
        ("nii",), ("ilma",), ("olgugi",), ("selleks",), ("sellepärast",),
        ("seepärast",), ("seetõttu",), ("vaevalt",), ("peaasi",), ("ainult",),
        ("mitte",), ("selle", "asemel"), ("hoolimata", "sellest"),
        ("olenemata", "sellest"), ("vaatamata", "sellele"),
    }),
    "nagu": frozenset({("nii",)}),
}

# First parts a comma never separates from et (Ta lahkus, ilma et oleks
# hüvasti jätnud), so the comma they lack goes before them.
_PAIR_UNSPLIT: frozenset[str] = frozenset({"ilma", "olgugi"})

# Quotation marks. One not glued to the word before it (sõna“) opens a
# quotation, and the word after it starts a sentence.
_QUOTE_MARKS: frozenset[str] = frozenset({"„", "“", "”", '"', "«", "»"})

# Where the scan after a nagu stops. A sentence end, or a subordinator or
# adversative opening the next clause, with no verb seen: a verbless
# comparison (tugev nagu karu.). Other clause punctuation: undecided, so
# the comma is asked for, as 0.8.0 always did (Tegin nagu ema, kes ...,
# soovitas). ja / ning / ega / või do not stop it: they join words as
# often as clauses (nagu ema ja isa olid õpetanud).
_SENTENCE_END_MARKS: frozenset[str] = frozenset({".", "!", "?", "…", "..."})
_CLAUSE_MARKS: frozenset[str] = frozenset(",;:()[]-\u2013\u2014")
_NAGU_CLAUSE_END: frozenset[str] = _COMMA_BEFORE | {"kuid", "aga"}
_COORDINATORS: frozenset[str] = frozenset({"ja", "ning", "ega", "või"})

# Vabamorf verb form codes that make no clause after nagu: the
# infinitives and supines, the des form, the present participles. Every
# other verb code is a personal or impersonal form in some mood, or a
# negation (ei, pole). The past participles nud / tud are left out on
# purpose: nagu kokku lepitud, nagu öeldud are clauses that drop olema
# and take the comma (EKI).
_NONFINITE_FORMS: frozenset[str] = frozenset({
    "da", "des", "ma", "mas", "mast", "maks", "mata", "tama", "v", "tav",
})

# et right after a coordinator opens a coordinated clause and takes no
# comma (..., et see on õige ja et me peaksime jätkama): EKI õigekirja
# põhireeglid 2025, reegel 45 märkus, sets no comma at ja, ning, ega, ehk
# or või. või is also a noun (Sulata või et see seguneks), so it counts
# only after an earlier et in the sentence.
_ET_COORDINATORS: frozenset[str] = frozenset({"ja", "ning", "ega", "ehk"})

# The closing comma of an embedded clause (EKI õigekirja põhireeglid
# 2025, reegel 44: a clause inside the main clause is set off by commas
# on both sides, Mees, kes seal seisis, oli minu isa). A clause is its
# predicate (reegel 44, sõnaseletus 2), so the scan keys on finite verbs:
# Vabamorf form codes of the persons and the negations ei, pole, polnud,
# poleks. Imperatives (o, ge, gem, gu, tagu) and the bare negative stem
# (ta) are left out: too is read as a pronoun, ära as an adverb, and an o
# form mostly follows ei.
_FINITE_FORMS: frozenset[str] = frozenset({
    "b", "d", "me", "n", "te", "vad", "s", "sid", "sime", "sin", "site",
    "ks", "ksid", "ksime", "ksin", "ksite",
    "nuks", "nuksid", "nuksime", "nuksin", "nuksite",
    "vat", "nuvat", "takse", "ti", "taks", "tuks", "tavat", "tuvat",
    "neg", "neg o", "neg nud", "neg ks", "neg vat",
})

# Verb forms that cannot be the main clause's predicate on their own: the
# nonfinite forms and the past participles (tehtud, kulunud).
_NONPREDICATE_FORMS: frozenset[str] = _NONFINITE_FORMS | {"nud", "tud"}

# Parts of speech a word must have no lexicon reading in to count as a
# sure verb. P is not among them: Vabamorf gives tegi a pronoun reading.
_NONVERB_POS: frozenset[str] = frozenset("SAKDNOCUHYGIJX")

# What opens an embedded clause after a comma: a relative pronoun in any
# case (kes, keda, kellega; mis, mida, millest; milline, missugune), kus,
# kuhu or kust as written (kust is also a form of the noun kusi), or a
# subordinator.
_RELATIVE_LEMMAS: frozenset[str] = frozenset({"kes", "mis", "milline", "missugune"})
_RELATIVE_ADVERBS: frozenset[str] = frozenset({"kus", "kuhu", "kust"})
_CLAUSE_OPENERS: frozenset[str] = frozenset({
    "et", "kui", "kuna", "kuigi", "kuni", "ehkki", "mil", "millal", "kuidas", "miks", "kas",
})

# Words that end the scan through a clause: past a conjunction (or any
# word read as one, or a relative word) a verb may belong to another
# clause.
_CLAUSE_SCAN_STOP: frozenset[str] = frozenset({
    "ja", "ning", "ega", "või", "ehk", "aga", "kuid", "vaid", "ent", "sest", "et", "kui",
    "nagu", "kuna", "kuigi", "kuni", "ehkki", "justkui", "otsekui", "nii", "kas", "kus",
    "kuhu", "kust", "millal", "kuidas", "miks", "mistõttu", "millepärast", "mispärast",
    "seega", "siis",
})

# Coordinators that end the backward scan for the main clause's verb: the
# verb before one belongs to the conjunct before it (Ta ostis selle, mis
# oli odav, ja see, mis oli kallis, jäi poodi).
_CLAUSE_COORDINATORS: frozenset[str] = frozenset({
    "ja", "ning", "ega", "või", "ehk", "aga", "kuid", "vaid", "ent",
})

# Punctuation that ends a clause scan. Also single quotation marks (a
# gloss: tähendab ‘on olemas’), a table cell edge (|), arrows (→, ⇒;
# => comes as = and >) and HTML tags (_clause_mark).
_CLAUSE_BOUNDARY_MARKS: frozenset[str] = frozenset({
    ",", ";", ":", "(", ")", "[", "]", "-", "\u2013", "\u2014", "/",
    "„", "“", "”", '"', "«", "»", "'", ".", "!", "?", "…", "...",
    "\u2018", "\u2019", "\u201a", "\u2039", "\u203a", "|", "\u2192", "\u21d2", "<", ">",
})

# Question words that make ei tea or jumal teab one indefinite phrase
# (käinud ei tea kus, rääkis ei tea kellest), besides the relative words.
_INDEFINITE_WH: frozenset[str] = frozenset({"millal", "kunas", "kuidas", "miks"})

# Verbs whose nominative after them is a predicative (kui olin laps), so
# it cannot be the main clause's fronted subject.
_COPULA_LEMMAS: frozenset[str] = frozenset({
    "olema", "saama", "jääma", "tunduma", "paistma", "näima", "muutuma", "osutuma",
})

# Lexicon lookups one check_punctuation text may make for the words next
# to on (Wake on LAN) and for a fronted subject; past that, the scan
# reads on as a verb and keeps the comma before the main verb.
_PUNCT_LOOKUPS = 128

# The persons a finite form code can be in. sid, ksid and nuksid are 2nd
# person singular or 3rd plural (olid). Codes not here (ks, neg, vat,
# takse) say nothing about the person.
_FORM_PERSONS: dict[str, frozenset[str]] = {
    **dict.fromkeys(("n", "sin", "ksin", "nuksin"), frozenset({"1sg"})),
    "d": frozenset({"2sg"}),
    **dict.fromkeys(("sid", "ksid", "nuksid"), frozenset({"2sg", "3pl"})),
    **dict.fromkeys(("b", "s"), frozenset({"3sg"})),
    **dict.fromkeys(("me", "sime", "ksime", "nuksime"), frozenset({"1pl"})),
    **dict.fromkeys(("te", "site", "ksite", "nuksite"), frozenset({"2pl"})),
    "vad": frozenset({"3pl"}),
}
_THIRD_PERSONS: frozenset[str] = frozenset({"3sg", "3pl"})


# Lexicons for check_redundancy: semantic doubling that's
# grammatically fine but reads redundant to a native speaker. Kept
# deliberately high-precision: better to miss than to flag legitimate
# phrasing, since each flag costs the user attention.

# Sets of adverbs/particles that all mean roughly the same thing
# ("also / too / likewise"). Two DIFFERENT members appearing adjacent
# is the classic pleonasm, e.g. "samuti ka", "ka samuti".
_ALSO_PARTICLES_ET: frozenset[str] = frozenset({"samuti", "ka", "ühtlasi"})

# Adjectives that are already absolute / non-gradable: putting "kõige"
# (most) in front is a double superlative ("kõige optimaalsem" is
# wrong the way "most optimal" is). Matched by STEM PREFIX rather than
# lemma, because Vabamorf lemmatizes the comparative form to itself
# (optimaalsem → lemma 'optimaalsem', POS C) instead of to the base
# adjective, so the comparative/superlative forms that actually follow
# "kõige" wouldn't match a base-lemma set. Stems are distinctive and
# only checked immediately after "kõige", so false positives are
# negligible. Deliberately excludes gradable-in-practice words like
# "parim" (kõige parim is idiomatic Estonian).
_NON_GRADABLE_STEMS_ET: tuple[str, ...] = (
    "optimaal", "ideaal", "maksimaal", "minimaal", "täiusli",
    "identse", "identne", "universaal", "lõpli", "absoluut",
    "totaal", "ammendav",
)

# Fixed pleonasm phrases (lowercased, surface-adjacent). Each maps to a
# short Estonian note on why it's redundant. High-confidence only.
_PLEONASM_PHRASES_ET: dict[tuple[str, ...], str] = {
    ("ajaline", "periood"): "periood on juba ajaline mõiste",
    ("väike", "nüanss"): "nüanss on juba väike erinevus",
    ("üldine", "konsensus"): "konsensus tähendab juba üldist nõusolekut",
    ("esmakordne", "debüüt"): "debüüt on juba esmakordne",
    ("praegune", "status"): "tarbetu võõrsõna; piisab 'praegune olukord'",
    ("tagasi", "taanduma"): "taanduma sisaldab juba 'tagasi' tähendust",
}


# --- Legal / legalese support --------------------------------------------
# Curated STARTER lexicons for Estonian legal text. Native speakers are
# invited to expand these (see CONTRIBUTING). They are precision-first,
# not exhaustive.

# Archaic "kantseliit" filler whose plain equivalent does NOT change legal
# meaning. Matched by surface-prefix so inflected forms are caught. Terms
# of ART (below) are deliberately NOT here: those must survive simplification.
_LEGALESE_STEMS_ET: dict[str, tuple[str, str]] = {
    "käesolev": ("see", "ametlik täitesõna; igapäevakeeles piisab 'see'"),
    "alljärgnev": ("järgnev", "kantseliit; piisab 'järgnev'"),
    "eelnimetatud": ("eespool nimetatud", "kantseliit"),
    "ülalnimetatud": ("eespool nimetatud", "kantseliit"),
    "eelpoolnimetatud": ("eespool nimetatud", "kantseliit"),
    "eeltoodu": ("eelnev", "kantseliit"),
    "tulenevalt": ("tõttu / kuna", "kantseliitlik side; 'sellest tulenevalt' → 'seetõttu'"),
    "seonduvalt": ("seoses / kohta", "kantseliit"),
}

# Fixed legalese phrases (lowercased surface pairs) → (plain, why).
_LEGALESE_PHRASES_ET: dict[tuple[str, str], tuple[str, str]] = {
    ("juhul", "kui"): ("kui", "'juhul kui' → lihtsalt 'kui'"),
    ("antud", "juhul"): ("sel juhul", "'antud' on toortõlge (given); 'sel juhul'"),
    ("sellest", "tulenevalt"): ("seetõttu", "kantseliit; 'seetõttu'"),
}

# ---------------------------------------------------------------------------
# Officialese (kantseliit) lexicons, the NON-legal sibling of the legalese
# set above. Aimed at reports, academic prose, grant/R&D paperwork and
# business writing, where check_legalese finds nothing because its lexicon
# is legal-specific and its length gate is tuned for statutes.
#
# Matched on LEMMA (exact), not surface prefix: prefix matching would fire
# on 'oma'/'omadus' for the 'omama' entry. Precision-first, per repo
# convention: a missed flag costs less than a wrong one.
# ---------------------------------------------------------------------------

_OFFICIALESE_LEMMAS_ET: dict[str, tuple[str, str]] = {
    "omama": ("olema", "'omab tähtsust' → 'on tähtis'; 'omama' on kantseliitlik tugiverb"),
    "teostama": ("tegema (või konkreetne tegusõna)", "kantseliitlik tugiverb; 'teostada analüüs' → 'analüüsida'"),
    "lähtuvalt": ("järgi / põhjal", "kantseliitlik side"),
    "tingituna": ("tõttu", "kantseliit; 'tõttu' on lihtsam"),
    "johtuvalt": ("tõttu", "kantseliit; 'tõttu' on lihtsam"),
    "olemasolu": ("on olemas", "nimisõnastatud olemine; kasuta tegusõna"),
}

# Officialese matched on SURFACE form, not lemma. For these the specific
# inflected form is the marker while the lemma is an ordinary word:
# `eesmärgil` lemmatises to `eesmärk` and `vahendusel` to `vahendus`, so
# keying on the lemma would fire on every innocent "meie eesmärk on …".
_OFFICIALESE_SURFACE_ET: dict[str, tuple[str, str]] = {
    "eesmärgil": ("et + tegusõna", "kantseliit; 'analüüsi eesmärgil' → 'et analüüsida'"),
    "vahendusel": ("kaudu / abil", "kantseliit; 'süsteemi vahendusel' → 'süsteemi kaudu'"),
}

# Fixed officialese phrases (lowercased surface pairs) → (plain, why).
# 'läbi viima' is not here: a surface pair caught four of its forms and
# also 'Lapsed viidi läbi metsa koju'. _labi_viima_issues covers it.
_OFFICIALESE_PHRASES_ET: dict[tuple[str, str], tuple[str, str]] = {
    ("kujutab", "endast"): ("on", "kantseliit; piisab tegusõnast 'on'"),
    ("kujutavad", "endast"): ("on", "kantseliit; piisab tegusõnast 'on'"),
    ("selles", "osas"): ("selle kohta", "kantseliitlik 'osas'; 'kohta' on lihtsam"),
    ("selle", "osas"): ("selle kohta", "kantseliitlik 'osas'; 'kohta' on lihtsam"),
    ("mille", "osas"): ("mille kohta", "kantseliitlik 'osas'; 'kohta' on lihtsam"),
    ("seoses", "sellega"): ("seetõttu", "kantseliit; 'seetõttu' on lühem"),
    ("arvestades", "asjaolu"): ("kuna", "kantseliit; alusta kõrvallauset sõnaga 'kuna'"),
    # 'hetkel' alone is fine (Sõnaveeb: 'just nüüd, praegusel ajal'); the
    # pair is the filler (AMSS: "Tänasel hetkel on sageli ka ülearune
    # täiteväljend").
    ("tänasel", "hetkel"): ("praegu", "ülearune täiteväljend; piisab sõnast 'praegu'"),
    ("praegusel", "hetkel"): ("praegu", "stampväljend; piisab sõnast 'praegu'"),
}

# --- Translated stock phrases in letters and business prose -------------
# (_calque_issues, rule calque-phrase). Each is anchored on more than one
# word, because each word alone is ordinary Estonian. Sources: Mäekivi,
# Emakeelne eurokeel (EKI 2013) and the Ametniku soovitussõnastik (AMSS)
# for 'sealhulgas, kuid mitte ainult'; ÕS 2025 info notes for 'antud'
# (vene данный, inglise given) and 'adresseerima' (inglise address);
# Sõnaveeb's senses of 'kõhklema' and 'edasi vaatama'; EKI keelenõuanne
# "Kuidas tarvitada arvutisõna manus?" for the idiomatic 'Manuses on ...'.
# 'Leiate manusest' is EKI style and is not matched; 'läbi e-posti' is
# not matched either, as ÕS 2025 accepts 'läbi' for a means.
# 'eelkõige / eeskätt' (primarily) are not list openers: the tail is only
# redundant there, so the explanation and the suggestion follow the head.
_CALQUE_NOT_LIMITED_RE = re.compile(
    r"\b(?P<head>sealhulgas|sh|näiteks|nt|eelkõige|eeskätt)\b[\s,(]*"
    r"(?P<tail>(?:kuid|aga)\s+mitte\s+(?:ainult|üksnes))\b",
    re.IGNORECASE)
# 'antud hetkel' is also 'at any given moment' in a general rule (Igal
# mängijal võib antud hetkel olla seitse kaarti), where 'praegu' would
# change the rule, so that reading gets its own rewrite.
_CALQUE_ANTUD_NOUNS: dict[str, str] = {
    "juhul": "sel juhul", "hetkel": "praegu (üldreeglis: korraga)",
    "olukorras": "selles olukorras", "kontekstis": "selles kontekstis",
}
# Before 'antud' these make it the participle 'given' (talle antud juhul,
# eelnevalt antud olukorras, kohtu poolt antud olukorras), so the pair is
# left alone.
_CALQUE_ANTUD_PARTICIPLE_BEFORE = frozenset({
    "ette", "eelnevalt", "varem", "juba", "üle", "välja", "kätte", "kaasa", "tagasi",
    "poolt",
})
# 'on antud olukorras, kus ...', 'suuliselt antud hetkel, mil ...': after
# a form of olema or a manner adverb, with a clause on the noun, 'antud'
# is the passive perfect of andma and the noun says when it was given.
_CALQUE_ANTUD_CLAUSE_AFTER = frozenset({"kus", "mil", "millal", "kui"})
_CALQUE_PROBLEM_NOUNS = frozenset({
    "probleem", "mure", "väljakutse", "puudus", "risk", "kitsaskoht", "vajadus",
})
# 'adresseerima' in its first Sõnaveeb sense, addressing or redirecting a
# mail item (saadetised ümber adresseerima): a postal noun (or its compound
# head: postisaadetis) or the particle 'ümber' near the verb means it is
# not about a problem.
_CALQUE_MAIL_NOUNS = frozenset({
    "kiri", "e-kiri", "saadetis", "pakk", "ümbrik", "postkaart", "aadress",
})
_CALQUE_LETTER_NOUNS = frozenset({"kiri", "e-kiri", "sõnum", "meil", "e-mail", "läkitus"})
# 'vaatan edasi X-le' is the calque only when X is an event one waits for;
# 'vaatas edasi merele / emale otsa' is 'kept looking'. A compound counts
# by its end (projektikoostöö), and any -mine noun is an event (kohtumine,
# pakkumine, kuulmine).
_CALQUE_LOOK_FORWARD_NOUNS = (
    "koostöö", "vastus", "tagasiside", "üritus", "jätk", "kommentaar",
    "ettepanek", "külaskäik", "vestlus", "arutelu", "mine",
)
# The word right after 'Palun leidke': an attachment as the place the
# reader is pointed to. 'manus' counts in the inessive, elative or
# essive only (manuses, manusest, manusena), not in 'manuse avamiseks';
# 'kaasa lisatud' is matched as two words.
_CALQUE_ATTACHED = frozenset({"lisatud", "lisast", "lisas"})
_CALQUE_MANUS_FORMS = frozenset({"sg in", "sg el", "sg es", "pl in", "pl el", "pl es"})
# Cases that put a word after 'teie / sinu' in a place or manner phrase
# (sinu kodus, teie emakeeles), so the possessive is not the one that
# 'tänan teid teie ... eest' repeats.
_CALQUE_LOCAL_CASES = (" in", " el", " ill", " ad", " abl", " all", " tr", " ter",
                       " es", " ab", " kom", "adt")

# --- 'läbi viima' in the conduct sense (_labi_viima_issues) ---------------
# A style hint, not a norm. EKI ühendsõnastik 2026 gives 'läbi viima'
# ('korraldama, juhtima, teoks tegema') no register label and does not
# normalise general-language meanings. The kantseliit judgement is EKI's
# Ametniku soovitussõnastik (AMSS: "Teeb sõnastuse enamasti
# kantseliitlikumaks ... asendatav nt sõnaga korraldama, tegema"), shared
# by Kapanen (Õiguskeel 2019/2) and Militerm ('läbiviimine' väldi). AMSS
# also lists the physical sense ('Rajatav kergtee viidi metsast läbi'),
# common in descriptive prose ('Rada viib läbi metsa'); most guards below
# are there to keep it quiet.

# Nouns one conducts: Sõnaveeb's collocates of 'läbi viima' plus common
# report and ceremony objects. Matched on the lemma or the compound head
# (rahuloluküsitlus); any -mine noun of 7+ letters counts as well. Never
# add what one is led THROUGH (kriis, protsess, etapp, periood, aeg,
# raskus, torm, keerdkäik): 'Juht viis ettevõtte läbi kriisi' is a route.
_LABI_VIIMA_EVENTS_ET: frozenset[str] = frozenset("""
    uuring koolitus küsitlus intervjuu koosolek kampaania reform seminar
    kursus uurimus eksperiment analüüs ekspertiis katse test konkurss
    konverents õppus võistlus üritus viktoriin hange audit uurimistöö
    valimised projekt istung hääletus eksam laager protseduur
    arenguvestlus vestlus kontroll vaatlus inventuur revisjon läbivaatus
    ülevaatus arutelu töötuba loeng praktikum menetlus operatsioon
    referendum rahvahääletus tasemetöö kontrolltöö seire monitooring
    sõeluuring katsetus tehing toiming muudatus ümberkorraldus
    koolitusprogramm veebinar treening haarang reid rahvaloendus loendus
    talgud simulatsioon pilootprojekt piloot esitlus nõupidamine
    teenistus jumalateenistus talitus tseremoonia missa laulatus matus
    rituaal riitus riigipööre reformatsioon puhastus retk enampakkumine
    oksjon mobilisatsioon rünnak juurdlus ekspeditsioon õppetöö väljaõpe
    turniir dessant palvus festival programm
""".split())

# Events one holds rather than organises: the suggestion leads with
# pidama (AMSS: 'Arenguvestlust viiakse läbi' → 'Arenguvestlust peetakse').
_LABI_VIIMA_PIDAMA_ET: frozenset[str] = frozenset("""
    koosolek istung seminar konverents vestlus arenguvestlus loeng kursus
    tund missa jumalateenistus teenistus talitus
""".split())

# Heads of a route subject or an antecedent: 'Rada viib läbi metsa',
# 'Kraav, mis viib läbi surnuaia', or of a threaded object: 'Niit viiakse
# läbi ja tõmmatakse pingule'.
_LABI_VIIMA_ROUTES_ET: frozenset[str] = frozenset("""
    tee rada maantee raudtee raudteeliin liin trass marsruut teerada
    matkarada õpperada terviserada kõnnitee jalgtee sihtee allee tänav
    kraav kanal jõgi oja toru kaabel juhe tunnel sild trepp käik koridor
    uks värav niit nöör pael lõng traat voolik köis polt varras nõel rihm
""".split())

# Heads of the genitive object of 'läbiviimine' in its building sense:
# 'kaabli läbiviimine', 'toru läbiviimine läbi tulemüüri'.
_LABI_VIIMA_THREADED_ET: frozenset[str] = _LABI_VIIMA_ROUTES_ET | {"torustik"}

# Events that are also places: 'Tee viib läbi laagri' is a route.
_LABI_VIIMA_PLACE_EVENTS_ET: frozenset[str] = frozenset({"laager", "festival"})

# 'Läbiviimine' is also the music-theory term for a fugue theme passing
# through the voices. 'fuuga' marks that context alone; the others need
# a second distinct term in the sentence ('teemal' and 'kaanoni' sit in
# ordinary reports too).
_LABI_VIIMA_MUSIC_ET: frozenset[str] = frozenset("""
    teema hääl imitatsioon kaanon fuuga priim oktav kontrapunkt stretto
    faktuur polüfoonia augmentatsioon diminutsioon dux comes helistik
    toonika dominant sopran tenor bass vastak vahemäng
""".split())

# Postpositions that end a genitive chain after 'viidi läbi' without
# making it a route: 'Uuringud viidi läbi küsitluse abil'.
_LABI_VIIMA_NONSPATIAL_POSTP_ET: frozenset[str] = frozenset("""
    abil käigus ajal raames jooksul põhjal alusel järgi toel vahendusel
    eest tõttu pärast eel järel osalusel juhtimisel juhendamisel
    korraldusel eestvedamisel
""".split())

# Words after (or before) 'läbi' that make it a route: 'Vii nõel siit
# läbi', 'Kaabel viidi läbi seinast'.
_LABI_VIIMA_LOCAL_ADV_ET: frozenset[str] = frozenset("""
    siit sealt kust kustkaudu sealtkaudu tagasi sisse välja kaudu ümber
    üle mööda
""".split())

# An elative of these is a time, not a source: 'aastast läbi viidud'.
_LABI_VIIMA_TIME_NOUNS_ET: frozenset[str] = frozenset(
    {"aasta", "kuu", "päev", "nädal", "aeg", "sajand", "algus"})

# A time span in the nominative owns 'läbi' (EKSS läbi I.2: 'Eile sadas
# päev läbi', 'suvi läbi'), and so does a measure phrase (mitu tundi läbi,
# kuu aega läbi): 'Terve päev läbi viisime kaste autosse' is carrying.
_LABI_VIIMA_SPAN_NOUNS_ET: frozenset[str] = frozenset("""
    päev nädal kuu aasta aeg öö ööpäev tund minut sajand aastakümme
    suvi talv kevad sügis hommik õhtu nädalavahetus
""".split())
_LABI_VIIMA_SPAN_QUANTIFIERS_ET: frozenset[str] = frozenset("""
    mitu mõni paar tükk hulk kuu nädal aasta päev tund
""".split())

# viima's impersonal finite forms (viidi, viiakse), which take no
# genitive object, and its own particles, which need no goal case.
_VIIMA_IMPERSONAL_FORMS_ET: frozenset[str] = frozenset(
    {"ti", "takse", "taks", "tagu", "tavat"})
_VIIMA_GOAL_PARTICLES_ET: frozenset[str] = frozenset(
    {"ellu", "sisse", "välja", "üle", "kurssi", "täide", "lõpule", "kokku"})

# viima's personal active forms: a singular nominative before them is the
# subject, not the thing conducted ('Retk viis läbi Lõuna-Eesti metsade').
_VIIMA_ACTIVE_FORMS_ET: frozenset[str] = frozenset("""
    n d b me te vad sin sid s sime site ksin ks ksid ksime ksite nud
""".split())

# Non-finite verb forms. After a finite verb, 'läbi' before a numeral
# surface is that verb's particle: 'Kontrollisime läbi viis stsenaariumi'.
_NONFINITE_VERB_FORMS_ET: frozenset[str] = frozenset("""
    ma mas mast mata maks da des nud tud v tav vat tuvat nuvat tama
""".split())

# A genitive personal pronoun before 'läbi viinud' is the subject ('on
# ta läbi viinud'), not a postposition's complement ('metsa läbi viiv').
_PERSONAL_PRONOUNS_ET: frozenset[str] = frozenset(
    {"mina", "sina", "tema", "meie", "teie", "nemad"})

# 'viis' and its forms are also the numeral five: 'Uuring viidi läbi
# viies riigis', where Vabamorf reads 'viies' as the des form.
_VIIS_NUMERAL_SURFACES_ET: frozenset[str] = frozenset(
    {"viis", "viies", "viisid", "viie", "viit"})

# Participles Vabamorf returns as adjectives with the surface as lemma,
# mapped to the viima form code they stand for.
_VIIMA_PARTICIPLES_ET: dict[str, str] = {
    "viidud": "tud", "viinud": "nud", "viiv": "v", "viidav": "tav",
}

# Joined nouns and participles of 'läbi viima' (EKI õigekirja põhireeglid
# 2025, reegel 31 and 32 Märkus: läbiviimine ~ läbi viimine, läbiviija ~
# läbi viija are both correct, so the split spellings count too), mapped
# to the replacements, synthesised in the original's case.
_LABI_VIIMA_NOUNS_ET: dict[str, tuple[tuple[str, ...], str, str]] = {
    "läbiviimine": (("korraldamine", "tegemine"), "S",
                    "'Läbiviimine' on nimisõnastatud tugiverb. Kasuta "
                    "tegusõna või jäta sõna ära: 'tööde läbiviimise "
                    "kvaliteet' → 'tööde kvaliteet', 'konverentsi "
                    "läbiviimiseks' → 'konverentsi korraldamiseks'. See on "
                    "stiilisoovitus, mitte viga."),
    "läbiviija": (("korraldaja",), "S",
                  "'Läbiviija' asemel sobib korraldaja või konkreetne "
                  "tegija: koolitaja, küsitleja, uurija. See on "
                  "stiilisoovitus, mitte viga."),
    "läbiviidud": (("korraldatud", "tehtud"), "A",
                   "'Läbiviidud' kuulub tugiverbi 'läbi viima' perre. "
                   "Sageli sobib 'korraldatud' või 'tehtud', või jäta sõna "
                   "ära. See on stiilisoovitus, mitte viga."),
    "läbiviidav": (("korraldatav", "tehtav"), "A",
                   "'Läbiviidav' kuulub tugiverbi 'läbi viima' perre. "
                   "Sageli sobib 'korraldatav' või 'tehtav', või jäta sõna "
                   "ära. See on stiilisoovitus, mitte viga."),
}
_LABI_VIIMA_SPLIT_NOUNS_ET: dict[str, str] = {
    "viimine": "läbiviimine", "viija": "läbiviija",
}

_LABI_VIIMA_EXPLANATION_ET = (
    "Tugiverb 'läbi viima' (saksa 'durchführen' tõlkelaen) muudab lause "
    "kantseliitlikuks. Sageli sobib konkreetne tegusõna: korraldama, tegema "
    "või pidama (koosolek, seminar). Näiteks 'viidi läbi küsitlus' → "
    "'korraldati küsitlus'. Kui mõeldakse vastuseisust hoolimata "
    "saavutamist ('hange viidi protestide kiuste läbi'), sobib 'suruti "
    "läbi'. See on stiilisoovitus, mitte viga."
)

# --- 'Küsin täpsemalt, kas …' (_kusin_tapsemalt_issue) -------------------
# Sentence-initial present 1sg / 1pl 'Küsin/Küsime täpsemalt,' before an
# indirect question. Bare 'Küsin, kas …' is natural (Sõnaveeb: 'Uudishimust
# küsin, kui vana te olete?'; 397 sentence-initial hits in EKI Korp
# SONAVEEB_2023), and so are the other moods and persons (Küsi täpsemalt,
# küsis täpsemalt), but 'Küsin täpsemalt, kas' has no native hit in about
# 1.3 billion EKI Korp tokens. Requests in letters use palun + imperative
# or a wish (Reinsalu, Õiguskeel 2020/1; Haavel, Õiguskeel 2022/2), and a
# lead-in is softened with the conditional (Kaska 2015), so 'Küsiksin
# täpsemalt' is left alone. A style hint for letters, not an error.

# Words that open the indirect question after the comma (first lemma).
_KUSIN_INTERROG_LEMMAS_ET: frozenset[str] = frozenset({
    "kas", "mis", "milline", "missugune", "kes", "kumb", "kuidas", "millal",
    "kus", "kuhu", "kust", "miks", "mispärast", "mitu", "palju", "kuivõrd",
})

# 'kui' opens an indirect question only as a degree word ('kui palju',
# 'kui kaua'); bare 'kui' is 'if' ('küsin täpsemalt, kui vaja'), and vaja
# is an adverb too, so the list is closed.
_KUSIN_KUI_DEGREE_ET: frozenset[str] = frozenset({
    "palju", "kaua", "kauaks", "suur", "suure", "suured", "suurt", "pikk",
    "pika", "pikad", "pikka", "kallis", "kalliks", "kiiresti", "kiire",
    "tihti", "sageli", "kaugel", "kaugele", "vana", "kõrge", "kõrged",
    "väike", "vähe", "hästi", "suurel", "suures", "mitu",
})

# A later 1sg / 1pl verb of reporting back ('…, ja annan teada', '…, kui
# temaga räägin') marks a plan to ask someone else, which is fine. võtma
# counts only with 'ühendust' (…, ja võtan teiega ühendust), not in
# '…, ja võtan dokumendid kaasa'.
_KUSIN_PLAN_VERBS_ET: frozenset[str] = frozenset({
    "andma", "teatama", "tulema", "vastama", "kirjutama", "helistama",
    "saatma", "ütlema", "edastama", "informeerima", "rääkima", "teavitama",
})

# Direct speech opened by a dialogue dash (EKI: a reply may start on its
# own line with a mõttekriips instead of quotation marks) and a quoted
# e-mail line ('>'). A hyphen stays flagged: in a letter it is a bullet.
_KUSIN_SPEECH_LEAD_ET: frozenset[str] = frozenset({"\u2013", "\u2014", ">"})

# 'Küsin täpsemalt, mis juhtus, aga ta vaikib': the person asked reacts
# in the same sentence, which is narration, not a letter.
_KUSIN_NARRATION_BUT_ET: frozenset[str] = frozenset({"aga", "kuid", "ent"})

_KUSIN_TAPSEMALT_EXPLANATION_ET = (
    "'Küsin täpsemalt, kas …' kõlab kirjas võõralt. 'Täpsemalt' eeldab "
    "varasemat, üldisemat küsimust, ja olevikuline 'küsin' kirjeldab palve "
    "asemel küsimise tegevust. Kirjas esitatakse täpsustav küsimus palvena "
    "('Palun täpsustage, kas …') või soovina ('Sooviksin täpsemalt teada, "
    "kas …'). Kui lause tähendab kavatsust küsida kelleltki teiselt, jäta "
    "see alles. See on stiilisoovitus, mitte viga."
)

# Particles that form ühendtegusõnad, which are written apart (EKI
# õigekirja põhireeglid 2025, reegel 31 Sõnaseletus 2): the verb behind
# 'läbivaatamine' is 'läbi vaatama', as EKI's own 'Nominaalstiili
# näiteid' rewrites it.
_VERB_PARTICLES_ET: frozenset[str] = frozenset("""
    alla edasi eemale ette järele juurde kaasa kätte kinni kokku kõrvale
    külge lahku lahti laiali läbi maha mööda peale pähe pihta põhja rikki
    ringi sassi segi sisse taha tagasi täis üle üles ümber vahele valmis
    vastu välja ära ühte otsa katki minema tühjaks heaks hukka ellu esile
    omaks osa aru arvesse kasutusele tähele teoks korda selgeks kindlaks
    puhtaks teatavaks taga alt eest täide lõpule alal ülal üleval silmas
    käiku vette kohale kohal korras juures eemal pooleli kaasas tööle
    üürile aluseks teadmiseks koos
""".split())

# -mine nouns whose own verb is the kantseliit: offer the plain verb.
_MINE_VERB_OVERRIDES_ET: dict[str, str] = {
    "läbiviimine": "korraldama / tegema",
    "teostamine": "tegema",
}

# -mine nouns lexicalised as a thing, not an action (EKSS: pakkumine
# 'ettepanek', teadmised 'omandatud teadmised', kohtumine a meeting).
# Matched by the lemma or the compound head, so hinnapakkumine and
# tippkohtumine go too. No verb can replace them, so they stay out of the
# nominalisation count.
_MINE_LEXICALISED_ET: frozenset[str] = frozenset(
    {"teadmine", "pakkumine", "kohtumine", "ettevõtmine"})

# 'vaikimisi' (by default) is an adverb (ÕS, Sõnaveeb) that Vabamorf reads
# only as the plural partitive of 'vaikimine'. Other -misi adverbs
# (kiirustamisi, kõhklemisi) have a D reading out of context.
_MISI_ADVERBS_ET: frozenset[str] = frozenset({"vaikimisi"})

# Subordinating conjunctions + relative pronouns whose pile-up inside ONE
# sentence is what makes Estonian officialese unreadable. Counted per
# sentence by check_officialese; this is the 'mille käigus … ning …'
# failure mode that a raw word count misses.
_SUBORDINATORS_ET: frozenset[str] = frozenset({
    "et", "kuna", "sest", "kuigi", "mistõttu", "millepärast",
    "mis", "mida", "mille", "millest", "millega", "millele", "milles",
    "millist", "milliseid", "kes", "kelle", "keda", "kellele",
    "kus", "kuhu", "kust", "millal", "kuidas", "nagu",
})

# Academic / report-register markers. classify_register's original
# _FORMAL_MARKERS covers legal-administrative vocabulary but scored dense
# R&D-report officialese as 'neutraalne', score 0.0, zero markers. These
# fill that hole.
_ACADEMIC_MARKERS_ET: frozenset[str] = frozenset({
    "aruandeperiood", "aruandlus", "aruandeperioodil", "ettevõttesiseselt",
    "valideerima", "verifitseerima", "annoteerima", "kvantifitseerima",
    "metoodika", "taksonoomia", "hüpotees", "valim", "andmestik",
    "näitaja", "kriteerium", "indikaator", "parameeter", "protseduur",
    "analüüsima", "hindama", "mõõtma", "fikseerima", "dokumenteerima",
    "vastavus", "kooskõla", "lahknevus", "järeldus", "tulemus",
    "eesmärgipärane", "süstemaatiline", "märkimisväärne", "vastavalt",
    "arvestades", "tulenevalt", "olemasolev", "asjaomane",
})

# Below this word count classify_register scores on the lexicon alone:
# impersonal-voice and noun-density ratios are too noisy on a sentence or
# two to move a register verdict.
_REGISTER_STRUCTURE_MIN_WORDS = 25

# Specialised Estonian legal terms of art. Used to (1) protect them from
# over-eager simplification, (2) suppress compound-familiarity false
# 'coinage' flags on legal compounds, (3) mark legal register. STARTER SET.
_LEGAL_TERMS_OF_ART_ET: frozenset[str] = frozenset({
    "hagi", "hageja", "kostja", "võlgnik", "võlausaldaja", "võlasuhe",
    "õigussuhe", "solidaarvõlgnik", "käendus", "käendaja", "menetlus",
    "kohtumenetlus", "tsiviilkohtumenetlus", "kriminaalmenetlus",
    "haldusmenetlus", "hagimenetlus", "menetlusosaline", "tõendamiskoormis",
    "aegumistähtaeg", "sundtäitmine", "kahjuhüvitis", "hüvitamiskohustus",
    "pandiõigus", "hüpoteek", "servituut", "pärandvara", "pärand", "pärija",
    "abieluvaraleping", "esindusõigus", "õigusvastane", "seadusjärgne",
    "tühistatav", "tühine", "riigilõiv", "käsundusleping", "töövõtuleping",
    "üürileping", "kohtuotsus", "kohtumäärus", "määruskaebus", "tsiviilhagi",
    "apellatsioonkaebus", "kassatsioonkaebus", "tagaseljaotsus",
    "tehing", "leping", "kohustus", "nõue", "vastutus", "hüvitis", "tagatis",
    "testament", "kaashagi", "hagiavaldus", "kohtuistung",
})


def _is_legal_term(word: str) -> bool:
    """True if a token is a specialised Estonian legal term of art, matched
    by exact membership or as the head of a legal compound (e.g. hagiavaldus,
    kohtuistung). Used to protect terms of art and de-noise other tools."""
    w = word.lower().strip(".,;:()«»„“”\"'")
    if w in _LEGAL_TERMS_OF_ART_ET:
        return True
    # Compound whose first element is a longer legal stem.
    return any(w.startswith(t) and len(t) >= 6 for t in _LEGAL_TERMS_OF_ART_ET)


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


def _is_punct(span) -> bool:
    """True when every Vabamorf reading of a morph_analysis span is Z and
    the span has no letter or digit: punctuation, operators (+ = /) and
    emoticons such as `:)`. Vabamorf also tags any word of 40 or more
    characters Z without analysing it (andmekaitseinspektsiooni...), and
    that is a word, so it is kept. Symbols (§, €) are Y and are kept.
    lemmatize, pos_tag and analyze_morphology leave these out; tokenize
    keeps them."""
    pos = list(span.partofspeech)
    return (bool(pos) and all(p == "Z" for p in pos)
            and not any(ch.isalnum() for ch in span.text))


# Olema-forms (plus the negative 'pole' family) that turn a following -tud
# participle into an impersonal predicate rather than a modifier:
# 'on kasutatud andmeid' (impersonal) vs 'lukustatud hindamisosa'
# (attributive). Used by _impersonal_voice.
_OLEMA_FORMS_ET: frozenset[str] = frozenset({
    "on", "oli", "olid", "olen", "oled", "oleme", "olete", "olnud",
    "ole", "olema", "oleks", "olevat", "pole", "polnud", "polegi",
})

# Impersonal present NEGATIVE form codes ('ei esitata', 'ei kasutata').
# Kept separate from _PASSIVE_FORMS_ET because bare 'da' is also the
# da-infinitive code. These only count when a negation precedes.
_IMPERSONAL_NEG_FORMS_ET: frozenset[str] = frozenset({"ta", "da"})

# VERBAL negation only: the subset of _NEGATION_LEMMAS_ET that actually
# turns a following participle into a predicate. `mitte` negates a noun
# phrase, not a verb ('mitte inimeste antud märgenditega'), and `ära` /
# `ärge` negate imperatives, which are personal forms; including either
# here would over-count impersonals.
_VERBAL_NEGATION_ET: frozenset[str] = frozenset({"ei", "pole", "polnud"})


def _span_bits(span) -> tuple[str, str, str]:
    """(pos, form, lemma) for a morph_analysis span, lower-cased, never None."""
    return (
        (_first(list(span.partofspeech)) or ""),
        (_first(list(span.form)) or ""),
        (_first(list(span.lemma)) or "").lower(),
    )


def _impersonal_voice(spans: list) -> dict:
    """Count Estonian umbisikuline tegumood ('passive') over a span list.

    Vabamorf's raw form codes alone produce a wrong ratio in four ways,
    every one of which showed up on real Estonian report prose:

    1. `ei` / `ära` are tagged `pos=V form=neg`, so each negation inflated
       `total_verbs` and DEFLATED the impersonal ratio. Now excluded.
    2. `ta`/`da` (impersonal present negative: `ei esitata`) is a form
       code the -takse/-ti/-tud/-tav set never covered, so negated
       impersonals were missed outright. Now counted, but only after a
       negation, because bare `da` is also the da-infinitive.
    3. `ei avaldatud` is tagged `pos=A` (adjective), not V, so it was
       skipped entirely. Now counted when a negation precedes.
    4. `Lukustatud hindamisosas` is a participle used as a MODIFIER, not
       an impersonal predicate, and was counted as passive. A -tud
       participle now counts only when an olema-form or a negation sits
       within the two preceding tokens; otherwise it lands in
       `attributive_excluded` instead of inflating the ratio.

    Pure function over spans (no model I/O) so it is unit-testable and can
    be shared by check_style, check_officialese and classify_register.
    """
    impersonal = 0
    verbs = 0
    examples: list[str] = []
    attributive_excluded: list[str] = []

    def _preceded_by(i: int, lemmas: frozenset[str], window: int = 2) -> bool:
        for j in range(max(0, i - window), i):
            if _span_bits(spans[j])[2] in lemmas:
                return True
        return False

    for i, span in enumerate(spans):
        pos, form, lemma = _span_bits(span)

        # (3) negated impersonal participle mis-tagged as an adjective.
        if pos == "A" and (lemma.endswith("tud") or lemma.endswith("dud")):
            if _preceded_by(i, _VERBAL_NEGATION_ET):
                verbs += 1
                impersonal += 1
                if len(examples) < 5 and span.text not in examples:
                    examples.append(span.text)
            continue

        if pos != "V":
            continue

        # (1) `ei` / `ära` are auxiliaries, not verbs worth counting.
        if lemma in _NEGATION_LEMMAS_ET or form == "neg":
            continue

        verbs += 1

        # (4) -tud participle: impersonal predicate only with olema/negation.
        if form in ("tud", "dud"):
            if _preceded_by(i, _OLEMA_FORMS_ET) or _preceded_by(i, _VERBAL_NEGATION_ET):
                impersonal += 1
                if len(examples) < 5 and span.text not in examples:
                    examples.append(span.text)
            elif len(attributive_excluded) < 5 and span.text not in attributive_excluded:
                attributive_excluded.append(span.text)
            continue

        # (2) impersonal present negative, only after a negation.
        if form in _IMPERSONAL_NEG_FORMS_ET:
            if _preceded_by(i, _VERBAL_NEGATION_ET):
                impersonal += 1
                if len(examples) < 5 and span.text not in examples:
                    examples.append(span.text)
            continue

        if form in _PASSIVE_FORMS_ET:
            impersonal += 1
            if len(examples) < 5 and span.text not in examples:
                examples.append(span.text)

    return {
        "passive_count": impersonal,
        "total_verbs": verbs,
        "ratio": round((impersonal / verbs) if verbs else 0.0, 3),
        "examples": examples,
        "attributive_excluded": attributive_excluded,
    }


# Per-tool invocation counters. Incremented only when a tool function
# actually runs, NOT on initialize / tools/list / SSE stream opens, so
# this counts real tool calls rather than all /mcp protocol traffic.
# Records tool NAME + count only; never arguments (the Estonian text),
# so the no-request-content privacy posture is preserved. Surfaced at
# /metrics and persisted to the Fly volume alongside the HTTP counters.
_TOOL_CALLS: dict[str, int] = {}


def _counted(fn):
    """Decorator: bump _TOOL_CALLS[fn.__name__] each time the tool runs.
    functools.wraps preserves the signature + annotations + docstring so
    FastMCP's schema generation is unaffected."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        _TOOL_CALLS[fn.__name__] = _TOOL_CALLS.get(fn.__name__, 0) + 1
        try:
            return fn(*args, **kwargs)
        except LookupError as e:
            # NLTK raises a LookupError with a wall of text telling the
            # caller to run nltk.download(), advice this server
            # deliberately does not follow (see _forbid_resource_downloads).
            # Translate it once, here, into the instruction that actually
            # applies. Costs nothing on the success path, and catching at
            # the tool boundary covers every tool that builds a layer
            # rather than needing a guard at ~19 call sites.
            missing = "an NLTK resource"
            for name in ("punkt_tab", "punkt"):
                if name in str(e):
                    missing = f"NLTK {name}"
                    break
            raise RuntimeError(
                f"{missing} is not installed, so {fn.__name__} cannot run. "
                f"{_RESOURCE_FETCH_HINT}"
            ) from e

    return wrapper


# ---------------------------------------------------------------------------
# Output-schema types. total=False so every field is optional: this
# advertises a structured output schema to clients (Smithery quality
# score) without FastMCP rejecting a return that conditionally omits a
# key. Inner shapes stay loose (list[dict]/dict) on purpose; the
# top-level schema is what matters.
# ---------------------------------------------------------------------------

class _TokenizeResult(TypedDict, total=False):
    sentences: list[str]
    words: list[str]


class _ParadigmResult(TypedDict, total=False):
    # lemma and partofspeech are None when Vabamorf cannot analyse the
    # word. Every other optional field is absent when it does not apply.
    lemma: str | None
    partofspeech: str | None
    partofspeech_estonian: str
    word_class: str
    word_class_estonian: str
    forms: list[dict]
    paradigm_count: int
    paradigm_key: str
    other_paradigms: list[dict]
    ranked_by_corpus_frequency: bool
    ambiguity_estonian: str
    reading_estonian: str
    invariant: bool
    invariant_estonian: str
    summary_estonian: str


class _RelatedWordsResult(TypedDict, total=False):
    matches: list[dict]


class _SynonymsResult(TypedDict, total=False):
    synsets: list[dict]
    synset_count: int


class _HyphenationResult(TypedDict, total=False):
    breaks: list[int]
    preferred: str
    syllable_count: int


class _CompoundFamiliarityResult(TypedDict, total=False):
    compounds_analysed: int
    suspect_compounds: list[str]
    unattested_compounds: list[str]
    wordnet_checked: bool
    all_compounds: list[dict]
    summary_estonian: str
    note: str


class _DomainTermsResult(TypedDict, total=False):
    glossary_size: int
    identifiers_size: int
    suggest_cutoff: float
    compounds_analysed: int
    wordnet_checked: bool
    unlisted_compounds: list[dict]
    suggestions_capped: bool
    summary_estonian: str
    note: str


class _StyleResult(TypedDict, total=False):
    repetition: dict
    passive_voice: dict
    sentence_length: dict
    hedging: dict


class _RegisterResult(TypedDict, total=False):
    tier: str
    tier_estonian: str
    score: float
    formal_markers: list[str]
    colloquial_markers: list[str]
    consistency: dict
    structure: dict
    word_count: int


class _CheckResult(TypedDict, total=False):
    """Shared output shape for the issue-list orthography/grammar checks."""
    issues: list[dict]
    summary_estonian: str


class _LegaleseResult(TypedDict, total=False):
    """Output of check_legalese: simplification hints + terms to preserve."""
    issues: list[dict]
    terms_of_art: list[dict]
    summary_estonian: str


class _DefinedTermsResult(TypedDict, total=False):
    """Output of check_defined_terms: defined-term map + cross-references."""
    defined_terms: list[dict]
    cross_references: list[dict]
    issues: list[dict]
    summary_estonian: str


class _LegalUsageResult(TypedDict, total=False):
    """Output of common_legal_usage: canonical collocations for a legal term."""
    lemma: str
    found: bool
    frequency: int
    common_before: list[dict]
    common_after: list[dict]
    summary_estonian: str


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(
    title="Tokenize Estonian text",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def tokenize(text: Annotated[str, Field(description="Estonian text.")]) -> _TokenizeResult:
    """Split Estonian text into `sentences` and `words` (every token in
    order, punctuation included).
    """
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["sentences"])
    sentences = [s.enclosing_text for s in t.sentences]
    words = [w.text for w in t.words]
    return {"sentences": sentences, "words": words}


def _usage_note(lemma: str | None, pos: str | None) -> tuple[str | None, str | None]:
    """Return (code, estonian_note) for a word, or (None, None) if neutral.

    Priority: POS-tag markers (Y, I, H) before lemma-lexicon markers.
    Word is matched lowercased against the lemma lexicon.
    """
    if pos and pos in _POS_USAGE_NOTES_ET:
        return _POS_USAGE_NOTES_ET[pos]
    if lemma:
        key = lemma.lower()
        if key in _MARKED_LEMMAS_ET:
            return _MARKED_LEMMAS_ET[key]
    return None, None


# A small set of lexically indeclinable (muutumatu) Estonian adjectives:
# they keep one form regardless of the noun's case/number. Conservative
# and high-confidence; extend as real cases turn up.
_INDECLINABLE_ADJ_ET: frozenset[str] = frozenset({
    "täis", "eri", "väärt", "katki", "lahti", "valmis", "puru", "segi",
})


# Lemma endings that mark a -tud/-dud/-nud surface form as genuinely
# deverbal. Needed because Vabamorf misanalyses some participles as nouns
# (hajutatud -> S/pl n, lemma `hajutatu`), and the ending alone cannot tell
# those from an ordinary plural noun that happens to end the same way
# (raamatud -> lemma `raamat`, linnud -> `lind`).
_DEVERBAL_LEMMA_ENDINGS: tuple[str, ...] = ("tu", "nu", "du", "tud", "nud", "dud")


@lru_cache(maxsize=4096)
def _attr_analyses(word: str) -> tuple[tuple[str, str, str], ...]:
    """Every (partofspeech, form, lemma) triple Vabamorf offers for a word
    in isolation.

    ALL analyses, not just the first: a participle like `tuntud` comes back
    as A/'', V/tud, A/pl n and A/sg n, so picking index 0 would make the
    verdict depend on Vabamorf's ordering. Cached, because callers hit the
    same attributes repeatedly; only consulted when the caller has no
    analysis of its own to pass in.

    Returns () if the word cannot be analysed, which sends the caller to
    the ending heuristic.
    """
    try:
        t = _Text()(word)
        t.tag_layer(["morph_analysis"])
        spans = list(t.morph_analysis)
        if not spans:
            return ()
        span = spans[0]
        # strict=False on purpose: if Vabamorf ever returns mismatched list
        # lengths, truncating beats raising inside a tool call over a
        # morphology detail.
        return tuple(
            (p or "", f or "", (lm or "").lower())
            for p, f, lm in zip(span.partofspeech, span.form, span.lemma, strict=False)
        )
    except Exception:
        return ()


def _is_indeclinable_attr(
    word: str, analyses: tuple[tuple[str, str, str], ...] | None = None
) -> bool:
    """True if a word does NOT inflect when used attributively (before a
    noun), so adjective-noun agreement should leave it in base form.

    Three invariant classes:
    - lexical indeclinables (täis, eri, väärt, ...)
    - past participles in -tud / -dud / -nud (`tuntud laulja` stays
      `tuntud` in the genitive, not *tuntu laulja)
    - the -mata form, the tud-participle's negative counterpart, which EKI
      states "jääb alati käändumatuks": `täitmata lepingute reserv`, not
      *täitmatute. Issue #42.

    WHY THIS IS NOT AN ENDING TEST. Three traps an ending test walks into:

    1. -tu caritive adjectives DO agree, and their nominative plural also
       ends in -tud: `õnnetu` → `õnnetud`. Freezing those yields
       *`õnnetud laste` for `õnnetute laste`.
    2. Ordinary plural nouns end the same way: `raamatud` (raamat),
       `linnud` (lind), `kohtud` (kohus). Freezing those is worse still,
       since they are common words.
    3. A noun whose stem ends in -ma forms its abessive in -mata:
       `teema` → `teemata`. That is an inflected noun, not the mata-form.

    So the order is: an adjective reading decides it (an invariant
    attributive has one with NO case/number form, a declining one only
    ever carries `sg n` / `pl n`); then -mata is invariant unless it is a
    NOUN abessive; then -tud/-dud/-nud is invariant only when some lemma
    looks deverbal, which separates `hajutatu` from `raamat`.

    KNOWN LIMITS, stated because the morphology cannot resolve them:

    - A caritive whose lemma ends -tu and which Vabamorf tags ONLY as a
      noun (`töötud` → `töötu`, `korratud`, `maitsetud`) is still frozen.
      That lemma is shaped exactly like the deverbal `hajutatu`, and no
      suffix test separates them. Checking whether a matching verb exists
      is worse, not better: it breaks `lugupeetud` and `mahajäetud` while
      falsely firing on `kasutud` and `raamatud`.
    - Homographs where the caritive plural and the participle are the same
      string cannot be resolved at all, and they fail in BOTH directions
      depending on which readings Vabamorf offers: `nõutud` and `kaalutud`
      have an A/'' reading and freeze, while a word Vabamorf knows only as
      a caritive declines even where the participle sense was meant.
      Disambiguating needs semantics, not morphology.

    CONTEXT BEATS ISOLATION, and callers differ. `analyze_morphology`
    supplies analyses disambiguated from the SENTENCE, which resolves most
    of the first class correctly: `töötud inimesed` gives A/pl n and
    declines, where the same word alone gives S/pl n and freezes. The
    isolated lookup is a best-effort fallback for callers that have no
    sentence, so the two can disagree on exactly those ambiguous words.
    Prefer passing analyses when you have them.

    `analyses` may be supplied by a caller that already has them, to avoid
    a second pass; when omitted they are looked up from the LOWERCASED
    word, so the verdict does not depend on incidental capitalisation.

    NOT flagged: -v present participles (rahuldav -> rahuldava), which
    agree normally.
    """
    w = word.lower()
    if w in _INDECLINABLE_ADJ_ET:
        return True
    if analyses is None:
        analyses = _attr_analyses(w)

    adjective_readings = [f for p, f, _lm in analyses if p == "A"]
    if adjective_readings:
        return any(not (f or "").strip() for f in adjective_readings)

    if w.endswith("mata"):
        # Trap 3. Only a NOUN abessive is an inflected form here; the
        # guesser also emits spurious abessive readings under other tags
        # for real mata-forms (`võltsimata` → C/sg ab).
        return not any(
            p == "S" and (f or "").endswith("ab") for p, f, _lm in analyses
        )

    if w.endswith(("tud", "dud", "nud")):
        # Trap 2. Vabamorf misanalyses some participles as nouns, so the
        # ending still has work to do -- but only when a lemma looks
        # deverbal. `hajutatu` yes, `raamat` no.
        return any(
            lm.endswith(_DEVERBAL_LEMMA_ENDINGS) for _p, _f, lm in analyses
        )
    return False


@mcp.tool(annotations=ToolAnnotations(
    title="Estonian morphological analysis",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def analyze_morphology(text: Annotated[str, Field(description="Estonian text.")], all_analyses: Annotated[bool, Field(description="Return every reading left after in-context disambiguation, in an `analyses` list, instead of only the most likely one.")] = False) -> list[dict]:
    """Morphological analysis of each word (punctuation omitted): `word`,
    `lemma`, `partofspeech` (pos_tag's tags), `form` (Vabamorf code, ""
    if uninflected), `root` (`_` joins compound parts), `ending` ("0" =
    none).

    Present only when they apply: `form_estonian` (the form's Estonian
    name), `clitic`, `root_tokens` (compound parts), `analyses_count` (>1:
    still ambiguous in context), `usage_note` (archaic, foreign,
    abbreviation, interjection, proper-noun) and the more precise
    `usage_note_estonian` (often names the neutral word), `indeclinable:
    true` (keeps its base form before a noun: täis, tuntud, täitmata;
    check it before declining a noun phrase).
    """
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["morph_analysis"])
    out: list[dict] = []
    for span in t.morph_analysis:
        if _is_punct(span):
            continue
        word = span.text
        lemmas = list(span.lemma)
        pos = list(span.partofspeech)
        forms = list(span.form)
        roots = list(span.root)
        endings = list(span.ending)
        clitics = list(span.clitic)
        root_tokens = list(span.root_tokens)
        readings = [
            _morph_reading(lemmas[i], pos[i], forms[i], roots[i], endings[i], clitics[i],
                           root_tokens[i] if i < len(root_tokens) else None)
            for i in range(len(lemmas))
        ]
        if all_analyses:
            # No analyses_count here: it would only repeat len(analyses).
            entry: dict = {"word": word, "analyses": readings}
        else:
            first = readings[0] if readings else _morph_reading(
                None, None, None, None, None, None, None)
            entry = {"word": word, **first}
            if len(readings) > 1:
                entry["analyses_count"] = len(readings)
        code, et = _usage_note(_first(lemmas), _first(pos))
        if code:
            entry["usage_note"] = code
            entry["usage_note_estonian"] = et
        if _is_indeclinable_attr(
                word,
                tuple((p or "", f or "", (lm or "").lower())
                      for p, f, lm in zip(pos, forms, lemmas, strict=False))):
            entry["indeclinable"] = True
        out.append(entry)
    return out


def _morph_reading(lemma: str | None, pos: str | None, form: str | None,
                   root: str | None, ending: str | None, clitic: str | None,
                   parts: Any) -> dict:
    """One Vabamorf reading as analyze_morphology reports it, in either
    mode. A key that would say nothing is left out: `form_estonian` when
    the form has no Estonian name, an empty `clitic`, and a one-part
    `root_tokens`, which is only `root` without its markers."""
    reading: dict = {"lemma": lemma, "partofspeech": pos, "form": form}
    label = _form_et(form, lemma)
    if label:
        reading["form_estonian"] = label
    reading["root"] = root
    reading["ending"] = ending
    if clitic:
        reading["clitic"] = clitic
    if parts and len(parts) > 1:
        reading["root_tokens"] = list(parts)
    return reading


def _form_et(code: str, lemma: str | None = None) -> str | None:
    """The Estonian name of a Vabamorf form code, or None.

    `paradigm` has labelled its forms since it existed, while
    `analyze_morphology` returned the raw code: a caller was told a word
    is `adt` or `takse` and left to guess, in a server whose stated rule
    is that every English label carries a correct Estonian rendering.

    `lemma` only matters for the `neg` codes, where one code covers
    three unrelated words (see _NEG_LABELS_ET). Without it they go
    unnamed rather than guessed at.

    None rather than the code itself when there is no label, so a caller
    can tell "this is the Estonian name" from "we do not have one", and
    so a missing label cannot masquerade as terminology.
    """
    if not code:
        return None
    if code == "neg":
        return "eitussõna"
    if code.startswith("neg "):
        family = _NEG_LEMMA_FAMILY.get((lemma or "").lower())
        return _NEG_LABELS_ET.get(family or "", {}).get(code[4:])
    return (_CASE_LABELS_ET.get(code) or _VERB_LABELS_ET.get(code)
            or _ANALYSIS_FORM_LABELS_ET.get(code))


# Vabamorf part-of-speech codes whose words inflect as nominals. O, C and
# U were missing, so paradigm() answered "this word class does not inflect,
# there is no paradigm" for every ordinal (esimene, kolmas), every
# comparative (parem, suurem) and every superlative (parim, suurim) in
# Estonian, 1.5% of the 20k most frequent word forms. Vabamorf synthesises
# all three correctly under their own POS code; the tool just never asked.
# H (proper noun) was missing the same way, so Tallinn, Tartu and Jaan were
# all said not to inflect. Its synthesis is lexicon-only: see
# _LEXICON_ONLY_POS.
_NOMINAL_POS: frozenset[str] = frozenset({"S", "H", "A", "P", "N", "O", "C", "U"})

# POS codes we will promote a non-inflecting reading TO. Deliberately
# excludes S: a function word's surface routinely collides with some rare
# noun lemma (koos -> 'koosi', miks -> 'miksi', siin -> 'siini'), and
# promoting those would answer a paradigm question with a word nobody
# meant. Adjective / comparative / superlative / ordinal readings of the
# same lemma are safe: at worst they are invariant (mööda, alasti), which
# is a true statement about the word rather than an invented paradigm.
_PROMOTABLE_POS: frozenset[str] = frozenset({"A", "C", "U", "O"})

# The form that identifies WHICH paradigm a word belongs to. Estonian
# grammar reads the muuttüüp off the singular genitive, and Vabamorf's
# synthesizer accepts exactly that as its `hint`.
#
# Nominals only. Verbs are deliberately NOT split this way: a verb with two
# da-infinitives (öelda / ütelda, mõelda / mõtelda) has rööpvormid, free
# variants of one lexeme, not two muuttüüpi. Splitting them produced two
# byte-identical tables and an Estonian sentence claiming a distinction
# that does not exist.
_NOMINAL_KEY_FORM = "sg g"

# Estonian names for the Vabamorf POS codes this module puts in front of a
# reader. A bare tagset letter in an Estonian sentence is not an answer.
_POS_LABELS_ET: dict[str, str] = {
    "S": "nimisõna",
    "H": "pärisnimi",
    "A": "omadussõna",
    "C": "omadussõna keskvõrdes",
    "U": "omadussõna ülivõrdes",
    "P": "asesõna",
    "N": "põhiarvsõna",
    "O": "järgarvsõna",
    "V": "tegusõna",
    "D": "määrsõna",
    "K": "kaassõna",
    "J": "sidesõna",
    "I": "hüüdsõna",
    "Y": "lühend",
    "G": "genitiivatribuut",
    "Z": "kirjavahemärk",
}

_WORD_CLASS_ET: dict[str, str] = {"nominal": "käändsõna", "verb": "tegusõna"}


def _pos_et(pos: str) -> str:
    """'D (määrsõna)' when the code is known, otherwise just the code."""
    label = _POS_LABELS_ET.get(pos or "")
    return f"{pos} ({label})" if label else f"{pos}"


# POS codes synthesised from Vabamorf's lexicon alone, its guesser off. Given
# a capitalised word it does not know, the guesser makes a name of whatever
# it was handed: a sentence-initial `Teatris` (inessive of teater) comes back
# as a name `Teatris : Teatrise`, a compound with its internal marker left
# in (`Kultuurik_atlase`). For the 2,699 names in the corpus vocabulary
# that the lexicon knows, the guesser changes no surface in any slot, so
# switching it off costs nothing real. Google and Lindgren get no forms
# instead of invented ones; Teatris gets the answer for teatris.
_LEXICON_ONLY_POS: frozenset[str] = frozenset({"H"})


def _synthesize(lemma: str, form: str, pos: str = "", hint: str = "") -> list[str]:
    """Vabamorf synthesis for one form: deduplicated, order preserved.

    The constraints are honoured STRICTLY. An empty result means "this
    lexeme has no such form", and that is a true and useful answer:
    `iga` (pronoun) has no plural, `kogu` (invariant adjective) has only
    a nominative, `ei` has no verb forms at all.

    Relaxing the POS when a form comes up empty looks helpful and is not.
    It hands back another lexeme's forms, so the table's singular would be
    the pronoun `iga` and its plural the noun `iga` "age" (`pl n: ead`).
    That is the same splice this module separates paradigms to avoid, and
    for a server whose job is to stop LLMs inventing Estonian morphology it
    is the worst possible output. A caller that knows the POS is a
    misanalysis should pass a corrected one, not be silently overruled.

    DEDUPLICATION is the one thing added. Vabamorf lists one candidate per
    matching lexicon entry, so homonyms with the same surface come back
    twice (`hall` sg g -> halli, halli). Callers saw a two-element list and
    had to guess why.
    """
    try:
        got = _vabamorf().synthesize(lemma, form, pos, hint,
                                     guess=pos not in _LEXICON_ONLY_POS) or []
    except Exception:
        return []
    return list(dict.fromkeys(got))


def _lexicon_readings(word: str) -> list[dict]:
    """Vabamorf's readings of `word` from its lexicon alone: no guesser, and
    none of the proper-name heuristics that make a name of any capitalised
    word. Empty when the lexicon does not know the word."""
    try:
        return _vabamorf().analyze([word], disambiguate=False, guess=False,
                                   propername=False)[0].get("analysis") or []
    except Exception:
        return []


def _lexicon_names(word: str) -> list[str]:
    """The proper-noun lemmas the lexicon itself gives `word`, leaving out
    the entries it lists only as an indeclinable name part (form `?`: New,
    San, Franz)."""
    return list(dict.fromkeys(
        a["lemma"] for a in _lexicon_readings(word)
        if a.get("partofspeech") == "H" and a.get("form") != "?"))


def _name_inflects(lemma: str) -> bool:
    """Whether the lexicon can inflect the proper noun `lemma` at all."""
    return bool(_synthesize(lemma, "sg n", "H") or _synthesize(lemma, "pl n", "H"))


def _corpus_ranks() -> dict | None:
    """fastText vocabulary as {surface: frequency rank}, or None when the
    model is not installed.

    The vocabulary is ordered by corpus frequency, which is the only
    evidence available offline for which of two morphologically valid
    paradigms Estonian actually uses. Read-only, local file, no network:
    the same model `find_related_words` already loads.
    """
    try:
        return _embeddings().key_to_index
    except Exception:
        return None


def _rank_paradigm_keys(keys: list[str], pos: str = "",
                        lemma: str = "") -> tuple[list[str], bool]:
    """Order homonymous paradigms by corpus attestation, best first.

    Vabamorf's candidate order is lexicon order, not a preference ranking:
    for `kott` it offers `kota` before `koti`, and a caller taking the first
    candidate gets a paradigm no Estonian speaker meant. Corpus frequency
    settles it: `koti` is in the vocabulary, `kota` is not.

    Returns (ordered keys, whether corpus evidence was actually used). With
    no model installed the order is Vabamorf's, unchanged, and the caller
    is told so rather than being handed a silent guess.

    A name's keys (`pos` H) are looked up as spelled. The vocabulary keeps
    case, so the lowercased `yorgi` and `yorki` find nothing where `Yorgi`
    and `Yorki` are both attested. The name's own nominative is not
    counted: `Jane` is also the key of the `Jane : Jane` type, and its
    count says nothing about that type over `Jane’i`. Every other key is
    looked up lowercased, as it always was.
    """
    # The length check comes FIRST. _corpus_ranks() deserialises a 34 MB
    # model, and there is nothing to rank below two candidates, so putting
    # it second would load it on every paradigm call for every ordinary
    # word, and load it once per concurrent caller during a cold burst.
    if len(keys) < 2:
        return (keys, False)
    ranks = _corpus_ranks()
    if not ranks:
        return (keys, False)
    if pos == "H":
        found = [ranks.get(k) if k != lemma else None for k in keys]
    else:
        found = [ranks.get(k.lower()) for k in keys]
    unattested = len(ranks) + 1
    order = sorted(
        range(len(keys)),
        key=lambda i: (unattested if found[i] is None else found[i], i),
    )
    ordered = [keys[i] for i in order]
    used = any(r is not None for r in found)
    return (ordered, used)


def _paradigm_hints(lemma: str, pos: str, key_form: str) -> tuple[list[str], bool]:
    """The distinct paradigms this lemma has, best first, as Vabamorf hints.

    Returns `([""], False)` for the ordinary case of a single paradigm. An
    empty hint means "no constraint", so nothing changes for the ~97% of
    words that are unambiguous. Otherwise one hint per paradigm, ranked, and
    a flag saying whether corpus evidence decided the order.

    Shared with scripts/eval_inflection.py so the published benchmark
    number measures this code and not a re-implementation of it.
    """
    keys = _synthesize(lemma, key_form, pos)
    ranked, used = _rank_paradigm_keys(keys, pos, lemma)
    if len(ranked) < 2:
        return ([""], False)
    return (ranked, used)


def _inflecting_reading(word: str, analyses: list[dict]) -> dict | None:
    """An inflecting reading of the word's OWN lemma, or None.

    `kaunis` is analysed as a D (adverb, `kaunis hea` = "quite good") when
    it stands alone, so paradigm() used to reply that it has no paradigm at
    all, while `kaunis : kauni : kaunist` is one of the most ordinary
    adjectives in the language, and Vabamorf generates it happily.

    Two guards, because the naive version is worse than the bug.

    OWN LEMMA. `veel` also carries a noun reading, but its lemma is `vesi`:
    rescuing that would answer a question about "still" with the paradigm
    of "water". So the reading must be the word's own base form, and must
    be one of _PROMOTABLE_POS.

    CORPUS ATTESTATION. That is still not enough. `kohe` ("immediately",
    one of the commonest adverbs) carries an adjective reading of its own
    lemma, `kohe : koheda`, which is a real word almost nobody means. The
    promoted reading must therefore be one Estonian actually writes: its
    singular genitive has to appear in the corpus vocabulary. `kauni` does
    (rank 10765), `koheda` does not.

    With no corpus model installed there is no evidence either way, so
    nothing is promoted and the answer is what it was before this check
    existed. Being unhelpful is recoverable; promoting the wrong word is
    not.
    """
    w = word.lower()
    for a in analyses:
        if a.get("partofspeech") not in _PROMOTABLE_POS:
            continue
        if (a.get("lemma") or "").lower() != w:
            continue
        if (a.get("form") or "") not in ("", "sg n"):
            continue
        # Only now is the model worth loading: this branch is reached by
        # roughly 0.25% of words, and never by ja / ning / väga / et.
        ranks = _corpus_ranks()
        if not ranks:
            return None
        keys = _synthesize(a["lemma"], _NOMINAL_KEY_FORM, a["partofspeech"])
        if any(k.lower() in ranks for k in keys):
            return a
    return None


def _rank_surfaces(surfaces: list[str], stem: str) -> list[str]:
    """Order the variants of ONE slot by the paradigm's own stem.

    Estonian builds its oblique plural cases on the genitive plural, and
    Vabamorf returns a slot's variants in lexicon order rather than that
    one: `raamat` came back as `pl all: raamatuile, raamatutele`, and a
    caller takes the first entry. `raamatute` is the genitive plural, so
    `raamatutele` is the form built on this paradigm's own stem and
    `raamatuile` is the i-plural, a real but literary formation built on
    a different one.

    Grammar, not frequency. Ranking these by corpus attestation instead
    scored five points lower and promoted homographs, because a surface can
    be frequent as some OTHER word: `käsi pl ad` became `käsil` (a
    lexicalised adverb) over `kätel`, and `soov pl p` became `soovisid`
    (the past tense of `soovima`) over `soove`. A stem the paradigm
    itself generated cannot belong to another lexeme.

    It also needs no model, so the cheapest tool in the server stays
    cheap: this used to consult the 34 MB corpus vocabulary for 92.5% of
    words instead of 2%.

    Variants that do not start with the stem keep their order behind the
    ones that do, and a slot the stem cannot decide is returned exactly
    as Vabamorf gave it.
    """
    if len(surfaces) < 2 or not stem:
        return surfaces
    return sorted(surfaces, key=lambda w: not w.startswith(stem))


def _table_surfaces(forms: list[dict]) -> set[str]:
    """Every surface in one paradigm table, a slot's variants included."""
    return {s for e in forms
            for s in ([e["surface"]] if isinstance(e["surface"], str) else e["surface"])}


# The ASCII apostrophe and the modifier letter, as Vabamorf's U+2019.
_APOSTROPHES = str.maketrans("'\u02bc", "\u2019\u2019")


def _build_forms(lemma: str, pos: str, form_list, labels: dict, hint: str) -> list[dict]:
    """One paradigm table, generated under a single paradigm hint."""
    # The stems this table's own oblique forms are built on, generated
    # under the same hint so they can only belong to this paradigm. A
    # word class that has neither (a verb) gets "", and _rank_surfaces
    # then leaves every slot in Vabamorf's order.
    stems = {
        "pl": (_synthesize(lemma, "pl g", pos, hint) or [""])[0],
        "sg": (_synthesize(lemma, "sg g", pos, hint) or [""])[0],
    }
    forms: list[dict] = []
    for f in form_list:
        generated = _rank_surfaces(_synthesize(lemma, f, pos, hint),
                                   stems.get(f[:2], ""))
        if not generated:
            continue
        forms.append({
            "form": f,
            "form_estonian": labels.get(f, f),
            "surface": generated[0] if len(generated) == 1 else generated,
        })
    return forms


def _paradigm(word: str) -> dict:
    """Generate a full inflection paradigm for a word.

    Resolves the input through analyze() to find its lemma + POS, then
    calls Vabamorf.synthesize() for each form in the appropriate paradigm
    table.

    A lemma can belong to more than one paradigm (`kott` inflects as either
    `koti` or `kota`). Those are generated separately and ranked, instead of
    being merged into a single table where `sg g` and `sg p` could come from
    different words.
    """
    _check_text(word, limit=MAX_WORD_CHARS, name="word")
    if any(ch.isspace() for ch in word):
        raise ValueError("paradigm expects a single word, no whitespace")

    vm = _vabamorf()
    # Find the dominant lemma + POS for this word.
    analyses = vm.analyze([word], disambiguate=True)
    if not analyses or not analyses[0].get("analysis"):
        return {
            "lemma": None,
            "partofspeech": None,
            "forms": [],
            "paradigm_count": 0,
            "summary_estonian": f"Sõnale '{word}' paradigmat ei leitud.",
        }
    primary = analyses[0]["analysis"][0]
    lemma = primary["lemma"]
    pos = primary["partofspeech"]
    reading_et = ""

    if pos == "H" and not _name_inflects(lemma):
        # The disambiguator can put a guessed lemma ahead of the lexicon's
        # own: Lõuna-Eesti as `Lõuna-Eest`, Ida-Euroopa as `Ida-Euroobas`,
        # Kesk-Euroopat as a nominative `Kesk-Euroopat`. The lexicon's own
        # name for the word wins, one the disambiguator also kept first.
        names = _lexicon_names(word)
        if names:
            kept = {a["lemma"] for a in analyses[0]["analysis"]}
            lemma = next((n for n in names if n in kept), names[0])

    if pos not in _NOMINAL_POS and pos != "V":
        # The isolated-word reading does not inflect. Before claiming the
        # word has no paradigm, check whether the word's own lemma has an
        # inflecting reading (the `kaunis` case).
        try:
            allr = vm.analyze([word], disambiguate=False)[0].get("analysis") or []
        except Exception:
            allr = []
        alt = _inflecting_reading(word, allr)
        if alt is not None:
            reading_et = (
                f"Üksiku sõnana loeb Vabamorf selle sõnaliigiks {_pos_et(pos)}, "
                f"mille vormid ei muutu. Sõnal on ka "
                f"{_POS_LABELS_ET.get(alt['partofspeech'], alt['partofspeech'])} "
                f"({alt['partofspeech']}) tähendus, mis käändub, ja selle paradigma "
                f"on väljal 'forms'."
            )
            lemma = alt["lemma"]
            pos = alt["partofspeech"]

    if pos in _NOMINAL_POS:
        form_list = _NOMINAL_FORMS
        labels = _CASE_LABELS_ET
        class_name = "nominal"
    elif pos == "V":
        form_list = _VERB_FORMS
        labels = _VERB_LABELS_ET
        class_name = "verb"
    else:
        return {
            "lemma": lemma,
            "partofspeech": pos,
            "partofspeech_estonian": _POS_LABELS_ET.get(pos, pos),
            "forms": [],
            "paradigm_count": 0,
            "summary_estonian": (
                f"Sõnaliik {_pos_et(pos)} ei käändu ega pöördu, paradigmat pole."
            ),
        }

    # Which inflection types does this lemma have? Estonian reads the
    # muuttüüp off the singular genitive; Vabamorf takes exactly that back
    # as a `hint`. Verbs are not split (see _NOMINAL_KEY_FORM).
    if class_name == "verb":
        ranked, ranked_by_corpus = ([""], False)
    else:
        ranked, ranked_by_corpus = _paradigm_hints(lemma, pos, _NOMINAL_KEY_FORM)
    chosen_by_input = False

    if len(ranked) < 2:
        # One paradigm (the overwhelmingly common case): no hint needed, so
        # nothing about the existing output changes.
        tables = [("", _build_forms(lemma, pos, form_list, labels, ""))]
    else:
        tables = [(k, _build_forms(lemma, pos, form_list, labels, k)) for k in ranked]
        # An inflected input already says which type the caller means:
        # `paradigm("koti")` must not lead with the `kota` table. This is
        # exact evidence, so it outranks corpus frequency.
        #
        # Only when it IS exact, though. Types share surfaces: `kotti` is
        # the singular partitive of `koti` AND the plural partitive of
        # `kota`, so it identifies nothing. Taking the first table that
        # contains it would have answered `paradigm("kotti")` with `kota`
        # whenever corpus ranking was unavailable, while claiming the input
        # had decided. A form in more than one table is left to the ranking.
        #
        # Vabamorf writes a name's apostrophe as U+2019 (Margot’d) and reads
        # the ASCII one as the same, so both are compared as one character.
        w = word.lower().translate(_APOSTROPHES)
        if w != lemma.lower().translate(_APOSTROPHES):
            matched = [
                i for i, (_key, forms) in enumerate(tables)
                if w in {s.lower().translate(_APOSTROPHES) for s in _table_surfaces(forms)}
            ]
            if len(matched) == 1:
                tables.insert(0, tables.pop(matched[0]))
                chosen_by_input = True

    # The lexicon lists some names only as an indeclinable part of a longer
    # one (form `?`: New, San, Rio, also Franz and Charlotte), and gives the
    # lemma for every slot. Names inflect, so that table is no answer:
    # `Charlottega` would come back as "Charlotte ei muutu".
    name_part = (pos == "H" and len(tables) == 1 and bool(tables[0][1])
                 and _table_surfaces(tables[0][1]) == {lemma})
    if name_part:
        tables = [("", [])]

    key, forms = tables[0]
    # Every table is built from the same `labels`, so a code the first
    # table already names gets no second `form_estonian` further down.
    labelled = {e["form"] for e in forms}
    others = [
        {"paradigm_key": k,
         "forms": [{"form": e["form"], "surface": e["surface"]}
                   if e["form"] in labelled else e for e in f]}
        for k, f in tables[1:]
    ]

    result = {
        "lemma": lemma,
        "partofspeech": pos,
        "partofspeech_estonian": _POS_LABELS_ET.get(pos, pos),
        "word_class": class_name,
        "word_class_estonian": _WORD_CLASS_ET[class_name],
        "forms": forms,
        "paradigm_count": len(tables),
    }
    if not forms and pos == "H":
        lower = word.lower()
        if lower != word and _lexicon_readings(lower):
            # A capitalised common word Vabamorf took for a name it has no
            # table for (a sentence-initial Pikk, Raamat, Neljas): the
            # answer is the lowercase word's.
            common = _paradigm(lower)
            note = (f"Vabamorf ei oska sõna '{word}' pärisnimena käänata, siin on "
                    f"vastus sõnale '{lower}'.")
            common["reading_estonian"] = " ".join(
                filter(None, (note, common.get("reading_estonian"))))
            return common
        # A name the lexicon lacks (Google, Lindgren). Names inflect, so
        # "0 vormi" alone would read as "does not inflect". The input is
        # named, not the lemma: an unknown name's lemma is a guess.
        if name_part:
            result["summary_estonian"] = (
                f"Vabamorf tunneb sõna '{lemma}' ainult mitmesõnalise nime "
                f"muutumatu osana (nagu New nimes New York) ega oska seda eraldi käänata.")
        else:
            result["summary_estonian"] = (
                f"Vabamorf ei tunne sõna '{word}' pärisnimena ega oska seda käänata.")
    elif not forms:
        # `ei` reaches this path as a verb with no forms; a table that is
        # not empty explains itself.
        result["summary_estonian"] = (
            f"Sõna '{lemma}' ({_pos_et(pos)}) paradigma: 0 vormi."
        )
    if key:
        result["paradigm_key"] = key
    if reading_et:
        result["reading_estonian"] = reading_et
    if others:
        result["other_paradigms"] = others
        result["ranked_by_corpus_frequency"] = ranked_by_corpus and not chosen_by_input
        all_keys = ", ".join(k for k, _ in tables)
        if chosen_by_input:
            why = f"Esimesena on tüüp, kuhu sinu antud vorm '{word}' kuulub."
        elif ranked_by_corpus:
            why = "Esimesena on korpuses sagedasem tüüp."
        else:
            why = "Järjestus on Vabamorfi oma, sagedusandmeid ei olnud."
        # "käändevorm", not "käändeline vorm": the latter is the term for a
        # verb's nominal forms (infinitives, participles), so it would ask
        # the caller for a participle.
        hint_et = (
            "" if chosen_by_input
            else " Kui tead, millist sõna silmas pead, anna sisendiks käändevorm."
        )
        result["ambiguity_estonian"] = (
            f"Sõnal '{lemma}' on Vabamorfi sõnastikus {len(tables)} eri muuttüüpi "
            f"({all_keys}). {why} Ülejäänud tüübid on väljal 'other_paradigms'.{hint_et}"
        )
    if forms and _table_surfaces(forms) == {lemma}:
        result["invariant"] = True
        result["invariant_estonian"] = f"Sõna '{lemma}' ei muutu, kõik vormid on ühesugused."
    return result


@mcp.tool(annotations=ToolAnnotations(
    title="Generate Estonian inflection paradigm",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def paradigm(word: Annotated[str, Field(description="One Estonian word, lemma or inflected form.")]) -> _ParadigmResult:
    """Inflection paradigm of one Estonian word from Vabamorf, which
    generates what is possible, not necessarily idiomatic.

    Nominals: up to 28 forms, 14 cases in singular and plural (iga has no
    plural), plus the short illative `adt` (majja beside majasse) where it
    exists. Verbs: about 39 common forms, impersonal included, not all
    Vabamorf can make. A word that does not inflect gets empty `forms` and
    a `summary_estonian` saying why.

    `forms` entries: `form` (Vabamorf code), `form_estonian` (its Estonian
    name), `surface`. A list `surface` holds correct variants, any built on
    this paradigm's own stem first (raamatutele before the literary
    raamatuile).

    Both illatives are correct and the short illative is often the
    commoner: never "correct" one into the other. `adt` is usually spelled
    like the singular partitive `sg p` (vend: venda), so a surface in this
    table does not confirm its case in a sentence.

    `paradigm_count` > 1 means several inflection types (kott: koti or
    kota): `forms` is the first-ranked, named by `paradigm_key` (its
    singular genitive), and the rest are in `other_paradigms`, which give
    `form_estonian` only for a code `forms` lacks. Read
    `ambiguity_estonian` before quoting a form. Pass an inflected form
    (koti) to select the type you mean.
    """
    return _paradigm(word)


@mcp.tool(annotations=ToolAnnotations(
    title="Lemmatize Estonian words",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def lemmatize(text: Annotated[str, Field(description="Estonian text.")]) -> list[dict]:
    """Lemma (dictionary form) of each word, from its most likely
    analysis. Punctuation is omitted.
    """
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["morph_analysis"])
    return [
        {"word": span.text, "lemma": _first(list(span.lemma))}
        for span in t.morph_analysis
        if not _is_punct(span)
    ]


@mcp.tool(annotations=ToolAnnotations(
    title="Part-of-speech tagging",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def pos_tag(text: Annotated[str, Field(description="Estonian text.")]) -> list[dict]:
    """Part-of-speech tag of each word (most likely reading); punctuation
    omitted.

    S noun, H proper noun, V verb, A adjective (C comparative, U
    superlative, G indeclinable), P pronoun, D adverb, K adposition, J
    conjunction, N cardinal, O ordinal, I interjection, Y abbreviation or
    symbol (§, €), X verb-bound word (`plehku`), Z a word of 40+
    characters, too long for Vabamorf to analyse.
    """
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["morph_analysis"])
    return [
        {"word": span.text, "partofspeech": _first(list(span.partofspeech))}
        for span in t.morph_analysis
        if not _is_punct(span)
    ]


@mcp.tool(annotations=ToolAnnotations(
    title="Estonian spell check",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def spell_check(text: Annotated[str, Field(description="Estonian text.")], suggestions: Annotated[bool, Field(description="Include correction suggestions for misspelled words.")] = True) -> list[dict]:
    """Spell-check Estonian text. Lists only the misspelled tokens, each
    occurrence in text order: `text`, `spelling: false`, and
    `suggestions` when Vabamorf has any. An empty list means every word
    passed.

    Passing proves only that a word is well-formed: Vabamorf accepts any
    valid compound, even an invented one (`toortõlkeoht`). Check a
    compound you coined with check_compound_familiarity.
    """
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["words"])
    words = [w.text for w in t.words]
    if not words:
        return []
    out: list[dict] = []
    for row in _vabamorf().spellcheck(words, suggestions=suggestions):
        if row["spelling"]:
            continue
        if not row.get("suggestions"):
            row.pop("suggestions", None)
        out.append(row)
    return out


@mcp.tool(annotations=ToolAnnotations(
    title="Syllabify Estonian word",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def syllabify(word: Annotated[str, Field(description="One Estonian word, no whitespace.")]) -> list[dict]:
    """Lowercased syllables of one Estonian word; `quantity` is the välde
    (1-3), `accent` 1 marks stress. Vabamorf's guesser syllabifier: it can
    err even on common words.
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
@_counted
def named_entities(text: Annotated[str, Field(description="Estonian text.")]) -> list[dict]:
    """People, places and organisations (`type` PER, LOC, ORG) with
    character offsets `start` and `end`. `text` is the span as written,
    often inflected (`Tallinnas`).
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
@_counted
def find_related_words(word: Annotated[str, Field(description="One Estonian word, any form.")], n: Annotated[int, Field(description="How many nearest-neighbour words to return (1-50).")] = 10) -> _RelatedWordsResult:
    """Nearest neighbours of an Estonian word in a fastText model (Common
    Crawl and Wikipedia), ranked by cosine `score`. Unlike `synonyms` (same
    meaning, WordNet), these are words used in similar contexts:
    near-synonyms, related concepts, inflections of the input itself
    (lemmatize to dedupe) and sometimes antonyms (tark: loll). Senses are
    not separated: `lahe` gets whichever of "bay" and "cool" dominates the
    corpus. Any string, even a typo, gets neighbours.
    """
    _check_text(word, limit=MAX_WORD_CHARS, name="word")
    if any(ch.isspace() for ch in word):
        raise ValueError("find_related_words expects a single word, no whitespace")
    n = max(1, min(int(n), 50))
    kv = _embeddings()
    matches = kv.most_similar(word, topn=n)
    return {
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
@_counted
def synonyms(word: Annotated[str, Field(description="One Estonian word in dictionary form, spelled as the dictionary spells it (kohv, not kohvi or Kohv).")], max_synsets: Annotated[int, Field(description="Maximum number of word-sense synsets to return.")] = 5) -> _SynonymsResult:
    """Estonian WordNet synsets for a word, one per sense, each with
    `lemmas` (the synonyms), any `definition` and any `examples`.
    `synset_count` is the total before `max_synsets` cuts the list.

    For "is this the right word here?", read each `definition` against the
    context, not just `lemmas`. A gloss naming a medium, field or material
    limits where the word fits: `korpus` is "kirjaliku või suulise teksti
    elektrooniline kogu", so a set of images is not a korpus, while
    `andmestik` has no such limit. Register is a different question:
    check_officialese, classify_register.
    """
    _check_text(word, limit=MAX_WORD_CHARS, name="word")
    if any(ch.isspace() for ch in word):
        raise ValueError("synonyms expects a single word, no whitespace")
    # Fail with an actionable message rather than letting EstNLTK try to
    # download the resource (outbound HTTP, which PRIVACY.md rules out) and
    # print its prompt to stdout (the MCP protocol channel under stdio).
    if not _wordnet_available():
        raise RuntimeError(
            "Estonian WordNet is not installed, so synonyms cannot run. "
            "Fetch it with: uv run python scripts/fetch_resources.py "
            "(the server never downloads resources by itself; see "
            "PRIVACY.md)."
        )
    wn = _wordnet()
    synsets = wn[word] or []
    out: list[dict] = []
    for s in synsets[:max_synsets]:
        # The POS is in `name` (korpus.n.01), so it is not repeated.
        entry: dict = {"name": s.name}
        if s.definition:
            entry["definition"] = s.definition
        if s.examples:
            entry["examples"] = list(s.examples)
        entry["lemmas"] = list(s.lemmas)
        out.append(entry)
    return {"synsets": out, "synset_count": len(synsets)}


def _classify_register(text: str) -> dict:
    """Pure helper, also used by tests."""
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["morph_analysis"])

    spans = list(t.morph_analysis)
    formal_hits: list[str] = []
    colloquial_hits: list[str] = []
    word_count = 0
    noun_count = 0

    for span in spans:
        word = span.text
        # Skip punctuation
        if not any(ch.isalpha() for ch in word):
            continue
        word_count += 1
        if _first(list(span.partofspeech)) == "S":
            noun_count += 1
        # Test against both surface form and best lemma; lower-cased.
        lemma = (_first(list(span.lemma)) or "").lower()
        surface = word.lower()
        for candidate in {surface, lemma}:
            if not candidate:
                continue
            if candidate in _FORMAL_MARKERS or candidate in _ACADEMIC_MARKERS_ET:
                formal_hits.append(candidate)
                break
            if candidate in _COLLOQUIAL_MARKERS:
                colloquial_hits.append(candidate)
                break

    # Score: positive = formal, negative = colloquial, 0 = neutral.
    # Normalise by word count so longer text doesn't dominate.
    if word_count == 0:
        score = 0.0
        raw = 0
    else:
        raw = len(formal_hits) - len(colloquial_hits)
        score = max(-1.0, min(1.0, raw * 4.0 / word_count))

    # Structural signals. Real register lives in syntax as much as
    # vocabulary: on the lexicon alone a dense R&D report scored
    # 'neutraalne', 0.0, with zero markers, while 87.5% of its verbs were
    # umbisikuline tegumood. Impersonal voice and noun density now
    # contribute, bounded and additive-only:
    #   * they apply only from 25 words up, so short strings still score
    #     purely on the lexicon;
    #   * they apply only when the lexicon is not net-colloquial, so
    #     chatty copy can never be nudged formal.
    imp = _impersonal_voice(spans)
    noun_verb_ratio = (
        noun_count / imp["total_verbs"] if imp["total_verbs"] else 0.0
    )
    structural_signals: list[str] = []
    structural = 0.0
    if word_count >= _REGISTER_STRUCTURE_MIN_WORDS and raw >= 0:
        if imp["ratio"] >= 0.4:
            structural += 0.15
            structural_signals.append("umbisikuline tegumood")
        if imp["ratio"] >= 0.7:
            structural += 0.10
        if noun_verb_ratio >= _OFFICIALESE_NOUN_VERB_RATIO:
            structural += 0.15
            structural_signals.append("nimisõnade kuhjumine")
        structural = min(structural, 0.4)
        score = max(-1.0, min(1.0, score + structural))

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

    # Estonian translations for the tier label. Without these, models
    # composing an Estonian-language reply will invent plausible-looking
    # but wrong inflections (e.g. *formalne instead of formaalne when
    # rendering "formal"). Hard-coding the right word is the only way
    # to keep the hallucination off our surface.
    _TIER_ET = {
        "formal": "formaalne",
        "neutral-formal": "pigem formaalne",
        "neutral": "neutraalne",
        "neutral-colloquial": "pigem kõnekeelne",
        "colloquial": "kõnekeelne",
    }

    # Consistency: text contains BOTH formal and colloquial markers.
    # Real register-mixed copy reads jarring; flag it explicitly so
    # callers don't need to compute it themselves from the two marker
    # lists.
    formal_unique = sorted(set(formal_hits))
    colloquial_unique = sorted(set(colloquial_hits))
    is_mixed = bool(formal_unique) and bool(colloquial_unique)
    if is_mixed:
        consistency_et = (
            f"Registriline ebakõla: tekst sisaldab nii ametlikke "
            f"({', '.join(formal_unique[:3])}) kui ka kõnekeelseid "
            f"({', '.join(colloquial_unique[:3])}) markereid."
        )
    elif formal_unique and not colloquial_unique:
        consistency_et = "Register on järjekindlalt formaalne."
    elif colloquial_unique and not formal_unique:
        consistency_et = "Register on järjekindlalt kõnekeelne."
    else:
        consistency_et = "Registri markereid ei tuvastatud."

    return {
        "tier": tier,
        "tier_estonian": _TIER_ET[tier],
        "score": round(score, 3),
        "formal_markers": formal_unique,
        "colloquial_markers": colloquial_unique,
        "consistency": {
            "is_mixed": is_mixed,
            "summary_estonian": consistency_et,
        },
        "structure": {
            "impersonal_ratio": imp["ratio"],
            "noun_verb_ratio": round(noun_verb_ratio, 2),
            "signals": structural_signals,
            "applied": round(structural, 3),
            "summary_estonian": (
                f"Lauseehitus viitab ametlikule stiilile: "
                f"{', '.join(structural_signals)}."
                if structural_signals
                else "Lauseehitus ametlikule stiilile ei viita."
            ),
        },
        "word_count": word_count,
    }


def _check_capitalization(text: str) -> dict:
    """Pure helper; the @mcp.tool wrapper below delegates here so tests
    can call it without going through the MCP wire layer."""
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["sentences", "morph_analysis"])

    # EstNLTK sets sentence.start to the offset of the first word in
    # that sentence (modulo leading whitespace, which is rare in
    # well-formed text). Words starting at any of these offsets are
    # legitimately capitalized; everything else is suspect.
    sentence_starts = {s.start for s in t.sentences}
    spans = list(t.morph_analysis)

    issues: list[dict] = []
    for i, span in enumerate(spans):
        word = span.text
        if not word or not word[0].isupper():
            continue
        if span.start in sentence_starts:
            continue
        # All-caps acronyms (NATO, EÜ, …) are deliberate; skip.
        if word.isupper() and len(word) > 1:
            continue

        lemma_lower = (_first(list(span.lemma)) or "").lower()
        if not lemma_lower:
            continue

        rule: str | None = None
        rule_estonian: str | None = None

        if lemma_lower in _WEEKDAYS_ET:
            rule = "weekday"
            rule_estonian = "nädalapäev"
        elif lemma_lower in _MONTHS_ET:
            rule = "month"
            rule_estonian = "kuu nimi"
        elif lemma_lower in _NATIONALITIES_ET:
            rule = "nationality"
            rule_estonian = "rahvuse nimetus"
        elif word.lower() in _LANG_ADJECTIVES_ET:
            # Country/language adjectives are lowercase only when used
            # attributively before a culture/language noun. The
            # capitalized form is a valid proper-noun usage on its own
            # (Eesti, Eestit, Eestis = the country).
            #
            # NOTE: match the surface form, not the lemma. Vabamorf
            # lemmatizes some adjectives to a stem (e.g. Inglise -> Inglis),
            # which would miss the rule. Language adjectives don't inflect
            # in attributive position, so the surface form is reliable.
            next_lemma = ""
            if i + 1 < len(spans):
                next_lemma = (
                    _first(list(spans[i + 1].lemma)) or ""
                ).lower()
            if next_lemma in _CULTURE_NOUNS_ET:
                rule = "language-adjective"
                rule_estonian = "keele- või kultuuriadjektiiv"

        if rule is None:
            continue

        issues.append({
            "word": word,
            "position": span.start,
            "rule": rule,
            "rule_estonian": rule_estonian,
            "suggestion": word[0].lower() + word[1:],
        })

    return {
        "issues": issues,
        "summary_estonian": (
            f"Leiti {len(issues)} algustäheortograafia viga." if issues
            else "Algustäheortograafia probleeme ei leitud."
        ),
    }


def _check_compounds(text: str) -> dict:
    """Phase-1 liitsõnaõigekiri checker: scans for common AI splits."""
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["morph_analysis"])
    spans = list(t.morph_analysis)

    issues: list[dict] = []
    for i in range(len(spans) - 1):
        a, b = spans[i], spans[i + 1]
        if not a.text.isalpha() or not b.text.isalpha():
            continue
        # Verify there's actual whitespace between them in the source.
        if a.end >= b.start:
            continue
        key = (a.text.lower(), b.text.lower())
        if key in _COMPOUND_BIGRAMS:
            joined = _COMPOUND_BIGRAMS[key]
            issues.append({
                "split": f"{a.text} {b.text}",
                "position": a.start,
                "rule": "compound-split",
                "rule_estonian": "liitsõna kokkukirjutamine",
                "suggestion": joined,
            })

    return {
        "issues": issues,
        "summary_estonian": (
            f"Leiti {len(issues)} liitsõnaõigekirja viga." if issues
            else "Liitsõnaõigekirja probleeme ei leitud."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian compound writing (liitsõnaõigekiri)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_compounds(text: Annotated[str, Field(description="Estonian text.")]) -> _CheckResult:
    """Flags compounds wrongly written as two words (liitsõnaõigekiri):
    `kooli maja` → `koolimaja`.

    Matches a curated list of about 30 common splits, so a hit is reliable
    but most compounds are not covered.
    """
    return _check_compounds(text)


def _pair_start(spans: list, i: int) -> int | None:
    """Where the two-part connective ending in the subordinator at spans[i]
    (nii et, selle asemel et, nii nagu) starts, if it is one."""
    firsts = _PAIR_FIRST.get(spans[i].text.lower(), frozenset())
    for n in (2, 1):
        if i >= n and tuple(s.text.lower() for s in spans[i - n:i]) in firsts:
            return i - n
    return None


def _opening_quote(spans: list, k: int) -> bool:
    """spans[k] opens a quotation, so what follows it starts a sentence:
    „ and « always open; the other marks only when nothing is glued to
    them from the left (a mark glued to a word or to ! ? closes one:
    „Mine ära!“ nii et)."""
    if spans[k].text in {"„", "«"}:
        return True
    return spans[k].text in _QUOTE_MARKS and (k == 0 or spans[k - 1].end < spans[k].start)


@lru_cache(maxsize=4096)
def _may_be_finite(word: str) -> bool:
    """Some lexicon reading of `word` is a clause verb. Read out of context,
    as the sentence analysis keeps one reading of a homograph: sõnas,
    lisas, selgus and tuli are nouns there as often as verbs."""
    found = _vabamorf().analyze([word], disambiguate=False, guess=False, propername=False)
    return any(r["partofspeech"] == "V" and r["form"] not in _NONFINITE_FORMS
               for r in (found[0].get("analysis") or []))


def _verb_after(spans: list, i: int) -> bool:
    """False only for a clear verbless comparison (tugev nagu karu.), which
    takes no comma: no word after the nagu at spans[i] can be a clause verb
    before the sentence ends or the next subordinator opens a clause.
    Otherwise nagu opens a clause (Vaatas mind, nagu oleksin võõras) or the
    scan hit clause punctuation and cannot tell, and the comma is asked
    for, as 0.8.0 did."""
    def verb(span) -> bool:
        return (any(a.partofspeech == "V" and a.form not in _NONFINITE_FORMS
                    for a in span.annotations) or _may_be_finite(span.text))

    for k in range(i + 1, len(spans)):
        span = spans[k]
        nxt = spans[k + 1] if k + 1 < len(spans) else None
        if span.text in _SENTENCE_END_MARKS or span.text.lower() in _NAGU_CLAUSE_END:
            return False
        if span.text.lower() in _COORDINATORS:
            # ja + verb: a second predicate of the main clause (jooksis
            # nagu tuul ja hüppas); ja + anything else stays in the scan.
            if nxt is not None and verb(nxt):
                return False
            continue
        if not any(ch.isalnum() for ch in span.text) and not _CLAUSE_MARKS.isdisjoint(span.text):
            # A comma before a coordinator ends a verbless comparison
            # (tugev nagu karu, aga ...), and so does one before a finite
            # verb, the main clause's after an embedded clause (Laps, kes
            # jooksis nagu tuul, võitis); any other mark cannot tell.
            return not (span.text == "," and nxt is not None
                        and (nxt.text.lower() in _COORDINATORS | {"aga", "kuid", "vaid"}
                             or _finite_here(nxt)))
        if verb(span):
            return True
    return False


def _finite_here(span) -> bool:
    """The sentence analysis reads `span` as a finite verb."""
    return any(a.partofspeech == "V" and a.form in _FINITE_FORMS for a in span.annotations)


def _lexicon_readings(word: str) -> list[dict]:
    found = _vabamorf().analyze([word], disambiguate=False, guess=False, propername=False)
    return found[0].get("analysis") or []


@lru_cache(maxsize=4096)
def _only_verb(word: str) -> bool:
    """No lexicon reading of `word` is anything but a verb. The sentence
    analysis can read a noun as a verb (in kelle majas tuli lahti pääses,
    the noun tuli), and a participle as the verb it comes from. ei counts
    despite its adverb reading."""
    if word.lower() == "ei":
        return True
    readings = _lexicon_readings(word)
    return bool(readings) and all(r["partofspeech"] not in _NONVERB_POS for r in readings)


@lru_cache(maxsize=4096)
def _vad_participle_only(word: str) -> bool:
    """`word` is a 3rd person plural verb in -vad whose only other readings
    are the -v participle in the plural nominative (soovivad: sooviv)."""
    if not word.lower().endswith("vad"):
        return False
    readings = _lexicon_readings(word)
    others = [r for r in readings if r["partofspeech"] in _NONVERB_POS]
    return len(others) < len(readings) and all(
        r["partofspeech"] == "A" and r["form"] == "pl n" and r["lemma"].endswith("v")
        for r in others)


def _sure_finite(span, nxt) -> bool:
    """`span` is a finite verb in the sentence and can be nothing else. A
    -vad verb that is also a participle counts unless a plural nominative
    follows it, which the participle would qualify (sobivad kingad)."""
    if not _finite_here(span):
        return False
    if _only_verb(span.text):
        return True
    return (_vad_participle_only(span.text)
            and any(a.form == "vad" for a in span.annotations)
            and not (nxt is not None
                     and any(a.partofspeech in {"S", "A", "H", "P"} and a.form == "pl n"
                             for a in nxt.annotations)))


def _clause_mark(spans: list, k: int) -> bool:
    """spans[k] ends a clause: clause punctuation, an HTML tag (<li>,
    </p>), or a number or abbreviation with its full stop glued on (2013.,
    6.) that ends the sentence, before a capital or at the end of the
    text."""
    t = spans[k].text
    if t in _CLAUSE_BOUNDARY_MARKS or (t[:1] == "<" and t[-1:] == ">"):
        return True
    if len(t) > 1 and t.endswith("."):
        return k + 1 == len(spans) or spans[k + 1].text[:1].isupper()
    return False


def _line_start(spans: list, k: int, text: str) -> bool:
    """spans[k] starts a line, so a list item ends before it, or a table
    cell after a tab."""
    if k == 0:
        return False
    gap = text[spans[k - 1].end:spans[k].start]
    return "\n" in gap or "\t" in gap


def _relative_word(span) -> bool:
    if span.text.lower() in _RELATIVE_ADVERBS:
        return True
    return (any(a.partofspeech == "P" for a in span.annotations)
            and any(a.lemma in _RELATIVE_LEMMAS for a in span.annotations))


def _clause_stop(span) -> bool:
    return (span.text.lower() in _CLAUSE_SCAN_STOP
            or any(a.partofspeech == "J" for a in span.annotations)
            or _relative_word(span))


def _persons(span) -> frozenset[str] | None:
    """The persons the finite verb `span` can be in; None when a reading is
    not a verb or says nothing about the person (ks, neg, vat, takse)."""
    out: set[str] = set()
    for a in span.annotations:
        persons = _FORM_PERSONS.get(a.form) if a.partofspeech == "V" else None
        if persons is None:
            return None
        out |= persons
    return frozenset(out) or None


def _plain_adverb(span) -> bool:
    return {a.partofspeech for a in span.annotations} == {"D"}


def _olema_complement(spans: list, k: int, last: bool) -> bool:
    """spans[k] is a -vad verb that is also a participle (sobivad: sobiv),
    and stands as the participle, the predicative after a form of olema:
    right after it, unless a verb it would govern follows (kes on siin
    soovivad osaleda is the verb; kes on sobivad kutsutakse is not), or
    with one or two plain adverbs between, when it ends the clause (`last`:
    kes olid väga sobivad,) or a sure verb follows. Before anything else
    the adverb may be olema's own predicative (kes on rahul soovitavad
    meid: the main verb)."""
    if _only_verb(spans[k].text):
        return False
    m = k - 1
    while m >= 0 and k - m < 3 and _plain_adverb(spans[m]):
        m -= 1
    if m < 0 or not any(a.partofspeech == "V" and a.lemma == "olema"
                        for a in spans[m].annotations):
        return False
    after = spans[k + 1] if k + 1 < len(spans) else None
    finite_after = after is not None and _sure_finite(
        after, spans[k + 2] if k + 2 < len(spans) else None)
    if m < k - 1:
        return last or finite_after
    return finite_after or not (
        after is not None and any(a.partofspeech == "V" for a in after.annotations))


@lru_cache(maxsize=4096)
def _lexicon_pos(word: str) -> frozenset[str]:
    """The parts of speech of `word`'s lexicon readings; none when Vabamorf
    does not know it without guessing (demand, Always)."""
    return frozenset(r["partofspeech"] for r in _lexicon_readings(word))


# The person of a nominative personal pronoun, by lemma and form.
_PRONOUN_PERSONS: dict[tuple[str, str], str] = {
    ("mina", "sg n"): "1sg", ("sina", "sg n"): "2sg", ("tema", "sg n"): "3sg",
    ("mina", "pl n"): "1pl", ("sina", "pl n"): "2pl", ("tema", "pl n"): "3pl",
}


@lru_cache(maxsize=4096)
def _nominative_person(word: str) -> str | None:
    """The person `word` stands for as a subject when no lexicon reading
    of it is anything but a nominative: 3sg for a singular noun (päike,
    poiss), the pronoun's own for ma, sa, nad, nemad. None for a word that
    can be in another case (auto, ta, me: an object or an attribute) and
    for a plural noun, which can be an object (ostsin kingad)."""
    readings = _lexicon_readings(word)
    if not readings:
        return None
    if all(r["partofspeech"] == "P" for r in readings):
        persons = {_PRONOUN_PERSONS.get((r["lemma"], r["form"])) for r in readings}
        return persons.pop() if len(persons) == 1 else None
    if (all(r["partofspeech"] in {"S", "A"} and r["form"] == "sg n" for r in readings)
            and any(r["partofspeech"] == "S" for r in readings)):
        return "3sg"
    return None


def _budgeted(fn, word: str, known: dict):
    """fn(word), counted against this text's _PUNCT_LOOKUPS; None past them."""
    key = (fn.__name__, word)
    if key not in known:
        if len(known) >= _PUNCT_LOOKUPS:
            return None
        known[key] = fn(word)
    return known[key]


def _english_on(spans: list, k: int, known: dict) -> bool:
    """The on at spans[k] is the English word inside a name (video on
    demand, add on, Wake on LAN): a word next to it is a lower-case word in
    plain Latin letters that the lexicon does not know, or a name stands
    before it and an abbreviation after it. A capitalised unknown word is
    mostly a name the sentence is about (mille kirjutas Brennick on
    klassika), so Always on top is still read as olema."""
    if k == 0 or k + 1 >= len(spans):
        return False
    before, after = spans[k - 1].text, spans[k + 1].text

    def pos(word: str) -> frozenset[str] | None:
        return _budgeted(_lexicon_pos, word, known) if word.isascii() and word.isalpha() else None

    if before.islower() and pos(before) == frozenset():
        return True
    if after.islower() and pos(after) == frozenset():
        return True
    names = pos(before)
    return names is not None and names <= {"H", "Y"} and pos(after) == {"Y"}


def _fronted_subject(spans: list, pred: int, verb: int, known: dict) -> int | None:
    """The main clause's subject, when it stands right before its verb at
    spans[verb] (plain adverbs aside) and right after the clause's last
    predicate at spans[pred]: Hommikul, kui ärkasin päike juba paistis
    (päike). Only a word that is a nominative in every lexicon reading, in
    the main verb's person and in none of the clause verb's, after a verb
    that takes no predicative (kui olin laps)."""
    j = verb - 1
    while j > pred and _plain_adverb(spans[j]):
        j -= 1
    m = j - 1
    while m > pred and _plain_adverb(spans[m]):
        m -= 1
    if j <= pred or m != pred:
        return None
    clause, main = _persons(spans[pred]), _persons(spans[verb])
    if not clause or not main or any(a.lemma in _COPULA_LEMMAS for a in spans[pred].annotations):
        return None
    person = _budgeted(_nominative_person, spans[j].text, known)
    return j if person is not None and person in main and person not in clause else None


def _closing_comma_issue(spans: list, i: int, text: str, known: dict) -> dict | None:
    """The clause opened by the word at spans[i], after a comma, runs into
    the main clause's verb with no comma closing it (Mees, kes seal seisis
    oli minu isa: `, oli`), or into the main clause's subject right before
    it (_fronted_subject). Only when the main clause still owes its
    predicate: no verb back to the previous clause mark or coordinator.
    The verbs are sure verbs (_sure_finite); the clause may have two joined
    by ja (seisis ja naeratas), and ei with the verb after it (ei oleks, ei
    tehtaks, ja ei suudaks) is one. Not a second verb: ei in ei ... ega (ei
    liha ega kala), ei tea or jumal teab before a question word (käinud ei
    tea kus), a -vad participle after olema (kes olid sobivad); nor any
    verb: on in an English name (Wake on LAN). Both scans stop at clause
    punctuation (the backward one also at a coordinator, the forward one at
    a line break, a tab, a conjunction or a relative word), so they stay
    between two commas. `known` holds this text's lexicon lookups."""
    n = len(spans)
    k = i - 2
    while k >= 0 and not _clause_mark(spans, k):
        if spans[k].text.lower() in _CLAUSE_COORDINATORS:
            break
        if any(a.partofspeech == "V" and a.form not in _NONPREDICATE_FORMS
               for a in spans[k].annotations):
            return None
        if _line_start(spans, k, text):
            break
        k -= 1

    def end(m: int) -> bool:
        return _clause_mark(spans, m) or _line_start(spans, m, text)

    def past_ei(m: int) -> int:
        """Past the word with a verb reading at or after spans[m], the verb
        an ei before spans[m] negates: the two are one predicate."""
        while (m < n and not end(m) and not _clause_stop(spans[m])
               and not any(a.partofspeech == "V" for a in spans[m].annotations)):
            m += 1
        return m + 1 if m < n and not end(m) and not _clause_stop(spans[m]) else m

    def ega_pair(k: int) -> int | None:
        """Where ega stands when the ei at spans[k] pairs two words with it
        (ei liha ega kala, ei rohkem ega vähem): before any word that can be
        a verb."""
        m = k + 1
        while (m < n and not end(m) and spans[m].text.lower() not in {"ega", "ei"}
               and not _clause_stop(spans[m])
               and not any(a.partofspeech == "V" for a in spans[m].annotations)):
            m += 1
        return m if m < n and not end(m) and spans[m].text.lower() == "ega" else None

    def question_after(k: int) -> int | None:
        """Where the question word stands after ei tea or jumal teab at
        spans[k]."""
        word = spans[k].text.lower()
        wh = (k + 2 if word == "ei" and k + 1 < n and spans[k + 1].text.lower() == "tea"
              else k + 1 if word == "teab" and spans[k - 1].text.lower() == "jumal"
              else None)
        if (wh is not None and wh < n and not end(wh)
                and (_relative_word(spans[wh]) or spans[wh].text.lower() in _INDEFINITE_WH)):
            return wh
        return None

    pred = None     # the clause's last predicate
    k = i + 1
    while k < n and not end(k):
        span = spans[k]
        word = span.text.lower()
        nxt = spans[k + 1] if k + 1 < n else None
        if (pred is not None and word in _COORDINATORS and nxt is not None and not end(k + 1)
                and _sure_finite(nxt, spans[k + 2] if k + 2 < n else None)):
            # ja + verb: the clause's second predicate, ja ei with its verb too.
            pred = k + 1
            k = past_ei(k + 2) if nxt.text.lower() == "ei" else k + 2
            continue
        if _clause_stop(span):
            return None
        if word == "ei" and pred is None:
            pred = k
            k = past_ei(k + 1)
            continue
        if _sure_finite(span, nxt) and not (word == "on" and _english_on(spans, k, known)):
            if pred is None:
                pred = k
            elif span.text[:1].isupper():
                return None     # an unquoted title, or text in capitals
            elif _olema_complement(spans, k, k + 1 >= n or end(k + 1)):
                pass            # kes olid sobivad: the participle
            elif word == "ei" and (m := ega_pair(k)) is not None:
                k = m           # ei liha ega kala: no predicate
                continue
            elif (wh := question_after(k)) is not None:
                # ei tea kus, jumal teab mis: one indefinite phrase (käinud
                # ei tea kus). A sure verb after it may be the main clause's
                # (... ei tea kus ta elab) or the clause's: undecided.
                m = wh + 1
                while m < n and not end(m) and not _clause_stop(spans[m]):
                    if _sure_finite(spans[m], spans[m + 1] if m + 1 < n else None):
                        return None
                    m += 1
                k = wh + 1
                continue
            else:
                j = _fronted_subject(spans, pred, k, known)
                at = spans[j] if j is not None else span
                return {
                    "word": at.text,
                    "position": at.start,
                    "rule": "comma-after-subordinate-clause",
                    "rule_estonian": "koma kõrvallause lõpus",
                    "suggestion": f", {at.text}",
                }
        k += 1
    return None


def _ja_comma_issue(spans: list, i: int, text: str) -> dict | None:
    """The main clause goes on after the clause opened by kes or mis at
    spans[i] with ja or ning and no comma before it (EKI õigekirja
    põhireeglid 2025, reegel 44 märkus 2: the clause's comma stays before
    ja). Told by person: the clause's verb is 3rd person (kes or mis is its
    subject), the verb right after ja (plain adverbs may stand between) is
    in none of its persons and in the main verb's (Juhtisin projekti, mis
    kestis kaks aastat ja koolitasin: `, ja`)."""
    n = len(spans)
    main = None
    k = i - 2
    while k >= 0 and not _clause_mark(spans, k):
        word = spans[k].text.lower()
        if word in _CLAUSE_COORDINATORS or word in _CLAUSE_OPENERS or _relative_word(spans[k]):
            break
        if _sure_finite(spans[k], spans[k + 1]):
            main = k
        if _line_start(spans, k, text):
            break
        k -= 1
    main_persons = _persons(spans[main]) if main is not None else None
    if not main_persons:
        return None

    def end(m: int) -> bool:
        return _clause_mark(spans, m) or _line_start(spans, m, text)

    clause_verb = None
    k = i + 1
    while k < n and not end(k):
        span = spans[k]
        if span.text.lower() in {"ja", "ning"} and clause_verb is not None:
            m = k + 1
            while (m < n and not end(m) and spans[m].text.lower() not in _CLAUSE_SCAN_STOP
                   and {a.partofspeech for a in spans[m].annotations} == {"D"}):
                m += 1
            if m < n and not end(m) and _sure_finite(spans[m], spans[m + 1] if m + 1 < n else None):
                clause_persons = _persons(spans[clause_verb])
                after = _persons(spans[m])
                if (clause_persons and after and clause_persons <= _THIRD_PERSONS
                        and not clause_persons & after and after <= main_persons):
                    return {
                        "word": span.text,
                        "position": span.start,
                        "rule": "comma-before-ja-after-subordinate-clause",
                        "rule_estonian": "kõrvallause koma ja ees",
                        "suggestion": f", {span.text}",
                    }
            return None
        if _clause_stop(span):
            return None
        if clause_verb is None and _sure_finite(span, spans[k + 1] if k + 1 < n else None):
            clause_verb = k
        k += 1
    return None


def _check_punctuation(text: str) -> dict:
    """Comma before subordinating words, and the comma closing a clause
    that opens after a comma (before the main verb, or before ja)."""
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["morph_analysis"])
    spans = list(t.morph_analysis)

    issues: list[dict] = []
    skip_prev = {",", ";", ":", "(", "[", "\u2014", "\u2013", "-", ".", "!", "?", "...", "…"}
    et_in_sentence = False
    known: dict = {}
    for i, span in enumerate(spans):
        word_lower = span.text.lower()
        if (i and spans[i - 1].text == ","
                and (word_lower in _CLAUSE_OPENERS or _relative_word(span))
                and not (i + 1 < len(spans) and spans[i + 1].text.lower() == "tahes")):
            found = _closing_comma_issue(spans, i, text, known)
            if found is None and word_lower in {"kes", "mis"}:
                found = _ja_comma_issue(spans, i, text)
            if found is not None:
                issues.append(found)
        if span.text in _SENTENCE_END_MARKS:
            et_in_sentence = False
        coordinated = False
        if word_lower == "et":
            after = spans[i - 1].text.lower() if i else ""
            coordinated = after in _ET_COORDINATORS or (after == "või" and et_in_sentence)
            et_in_sentence = True
        if word_lower not in _COMMA_BEFORE or i == 0 or coordinated:
            continue
        prev = spans[i - 1]
        if prev.text in skip_prev:
            continue
        word, position = span.text, span.start
        first = _pair_start(spans, i)
        if first is not None:
            # A comma, the sentence start or an opening quotation mark
            # before the whole connective sets it off. A full stop counts
            # only before a capital (vms. nii et is mid-sentence).
            before = spans[first - 1].text if first else ""
            capital = spans[first].text[:1].isupper()
            # A closing quote counts as what stands before it: „jah,“ nii
            # et has its comma, „Jah.“ Nii et starts a sentence.
            if before in _QUOTE_MARKS and first >= 2 and not _opening_quote(spans, first - 1):
                before = spans[first - 2].text if spans[first - 2].text in skip_prev else (
                    "." if capital else before)
            if (first == 0 or _opening_quote(spans, first - 1)
                    or (before in skip_prev and (before not in _SENTENCE_END_MARKS or capital))):
                continue
            if spans[first].text.lower() in _PAIR_UNSPLIT:
                position = spans[first].start
                word = text[position:span.end]
        if word_lower == "nagu" and not _verb_after(spans, i):
            continue
        issues.append({
            "word": word,
            "position": position,
            "rule": "comma-before-clause-conjunction",
            "rule_estonian": "koma alistava sidesõna ees",
            "suggestion": f", {word}",
        })
    issues.sort(key=lambda issue: issue["position"])

    return {
        "issues": issues,
        "summary_estonian": (
            f"Leiti {len(issues)} kirjavahemärgiviga." if issues
            else "Kirjavahemärgivigu ei leitud."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian punctuation (kirjavahemärgid)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_punctuation(text: Annotated[str, Field(description="Estonian text.")]) -> _CheckResult:
    """Flags a missing comma:
    - `comma-before-clause-conjunction`: before et, kuna, sest, kuigi,
      kuid, vaid, nagu, mistõttu, millepärast or kuhu. A comma before a
      pair (nii et, ilma et) passes, as does et after ja, ning, ega or ehk;
      nagu is flagged only before a clause with a verb.
    - `comma-after-subordinate-clause`: closing a clause opened after a
      comma by a relative word (kes, mis, kus) or a subordinator (et, kui,
      kas), before the main clause's verb: Mees, kes seal seisis oli minu
      isa → `, oli`; before its subject instead when that stands right
      before the verb and cannot be the clause's own (Hommikul, kui
      ärkasin päike juba paistis → `, päike`). Otherwise that is where the
      comma goes at the latest; it may belong earlier, before a longer
      fronted subject or inside a split phrase.
    - `comma-before-ja-after-subordinate-clause`: before ja or ning when
      the main clause goes on after a kes or mis clause, told by the
      verbs' person: Juhtisin projekti, mis kestis kaks aastat ja
      koolitasin → `, ja`.

    The last two read only words that cannot be anything but a verb, so a
    verb that is also a noun (tuli, sai, lõppes) is missed; ei in ei ...
    ega and in ei tea kus, a -vad participle after olema (kes olid
    sobivad) and on in an English name (video on demand) are not counted.
    A line break, a table cell edge (| or a tab), an HTML tag, an arrow or
    a quotation mark ends the clause. Not checked: the comma before kui,
    mis or kes (it depends on meaning), apposition commas, dashes, colons.
    A comma before ja in a list and a letter's greeting and closing commas
    are check_translationese's.
    """
    return _check_punctuation(text)


def _check_hyphenation(word: str) -> dict:
    """Return valid line-break positions for an Estonian word."""
    _check_text(word, limit=MAX_WORD_CHARS, name="word")
    if any(ch.isspace() for ch in word):
        raise ValueError("check_hyphenation expects a single word, no whitespace")
    from estnltk.vabamorf.morf import syllabify_word
    syls = syllabify_word(word)
    if len(syls) < 2:
        return {"breaks": [], "preferred": word, "syllable_count": len(syls)}
    breaks: list[int] = []
    offset = 0
    for _i, s in enumerate(syls[:-1]):
        offset += len(s["syllable"])
        # poolitamine rule: don't leave <2 characters at either edge of
        # the broken word.
        if offset >= 2 and len(word) - offset >= 2:
            breaks.append(offset)
    # Build a human-readable form with break markers (interpunct U+00B7)
    pieces: list[str] = []
    last = 0
    for b in breaks:
        pieces.append(word[last:b])
        last = b
    pieces.append(word[last:])
    preferred = "·".join(pieces) if breaks else word
    return {"breaks": breaks, "preferred": preferred, "syllable_count": len(syls)}


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian word hyphenation (poolitamine)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_hyphenation(word: Annotated[str, Field(description="One Estonian word, no whitespace.")]) -> _HyphenationResult:
    """Line-break positions in one Estonian word (poolitamine); use only
    when line breaks matter. `breaks`: character offsets at syllable
    boundaries, at least 2 characters from either end; `preferred`: the
    word with each break marked ·. Compound seams, which Estonian prefers,
    are among the breaks but not singled out: choose the seam yourself
    (kooli·maja). A break can also split one vowel off a compound part
    (maao-manik in maa·o·ma·nik); skip those.
    """
    return _check_hyphenation(word)


# check_numbers. Thousands: a comma group is flagged only where it cannot be
# the Estonian decimal comma, two or more groups (1,500,000) or one before a
# period decimal (1,500.50: one issue, the decimal converted too). One group
# (1,250) is a decimal.
# Decimals: a period between digits is flagged, before a sentence end too
# (Pi on 3.1416.), unless a rule below skips it. Each rule reads a bounded
# window: the word before the number on its line, the next few characters,
# the candidate next to it across a dash.
# Always skipped:
# - a comma part after the number (race time 1.47,58; 1.500,50);
# - a version glued to v or to letters and a hyphen (v1.2, GPT-3.5);
# - a clause number opening a line or list item, with a period after it
#   (4.13. Tellija), or after a heading mark and before a capital (## 3.7
#   Tulemused). Not a bare 3.7 Tulemused: data rows (9.58 Usain Bolt) look
#   the same.
# Skipped unless a unit, currency, per cent or multiplier follows (1.10
# punkti, 2.5 korda, 1.25-1.50 eurot) or a currency precedes (€14.05):
# - a time after a form of kell (kell, kella, kellaajal; not kelle, of kes)
#   or kl (kell 10.20);
# - a time range, both ends, the right one later (10.30-12.00; a confidence
#   interval 1.05-1.20 too);
# - a section or version number after its word: §, p, lg, ptk, nr, v, ver,
#   also inflected (p-s, lg-s), or punkt, lõige, peatükk, jaotis, tabel,
#   joonis, versioon, paragrahv, artikkel in any form, and lisa as written
#   (Lisa is mostly the imperative, lisas mostly the past tense);
# - a date, day 1-31 and a two-digit month (14.05, 1.05; 2.05 ja too);
# - before a sentence end, where numbers were never flagged before, the
#   shape of a time (väljub 6.55., ajaga 32.15.) or a word with a capital
#   before the number (iOS 17.2., Android 4.4.); so Pi on 3.14. and Narvas
#   5.1. are missed;
# - a version after a product name: a capitalised word mid-sentence, not
#   all capitals (BMI 31.2 is a measure), that Vabamorf's lexicon does not
#   know (Python 3.12, not Tartus 8.7). A lookup can take 2 ms, so a text
#   gets _NUM_LOOKUPS of them; past that a name is read as known.
# A bare time mid-sentence (väljub 8.47 ja) is flagged: only kell tells it from a decimal.
_NUM_DECIMAL_RE = re.compile(
    r"(?<![\d.])(\d+)\.(\d+)(?!\d)(?!\.(?!\s*\Z|\s+[^\s\da-zõäöüšž]))")
_NUM_THOUSANDS_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+)(?:\.(\d+))?(?!\d)")
_NUM_WORD_RE = re.compile(r"\S+")
_NUM_OPENERS = "([{«\"„“'"
_NUM_CASE_END_RE = re.compile(r"(?<=.)-[a-zõäöüšž]{1,5}\Z")
_NUM_UNIT_RE = re.compile(
    r"[^\S\n]?(?:[%€]|(?:EUR|USD|CHF|SEK|GBP|kg|g|mg|t|km|m|cm|mm|l|ml|dl|s|ms"
    r"|min|h|V|A|W|kW|Hz|kHz|MHz|GHz|bar|kPa|tl|sl|kl|korda)(?![^\W\d_²³])"
    r"|protsent|eur|miljon|miljard|tuhat|meetr|kilo|liitr|tonn|sekund|minut"
    r"|tund|punkt)")
_NUM_CURRENCY_RE = re.compile(r"(?:€|(?<![^\W\d_])(?:EUR|USD|CHF|SEK|GBP))[^\S\n]?\Z")
_NUM_RANGE_GAP_RE = re.compile(r"[^\S\n]*[-\u2013][^\S\n]*")
_NUM_NEXT_LETTER_RE = re.compile(r"[^\S\n]+([^\W\d_])")
_NUM_SENTENCE_START_RE = re.compile(r"(?:\A|\n|[.!?…][\"'“”»)\]]*\s)[\s\"'„“«(\[#>*•-]*\Z")
_NUM_KELL_RE = re.compile(r"kell(?:a\w*)?|kl\.?")
_NUM_SECTION_RE = re.compile(
    r"(?:§|p|lg|ptk|nr|v|ver)\.?|(?:punkt|lõige|lõike|peatük"
    r"|jaotis|tabel|joonis|versioon|paragrahv|artikl|artikkel)\w*")


_NUM_LOOKUPS = 128

# A tilde used as "about" before a number (~50 inimest). EKI (Arvukirjutus,
# p 11) writes a rounded number with u (no period) or ligikaudu, and keeps
# the tilde for parallel variants in linguistic text (mina ~ ma), always
# spaced. So a glued ~50 is flagged anywhere it is not code; a spaced ~ 50
# only where no word stands before it (after a colon, comma or bracket, or
# opening a list item), because "aastal ~ 2005. a" is EKI's own variant
# notation. Skipped: anything glued on the left (HEAD~1, 5~10, ~~, /~50),
# a digit or operator before it (5 ~10, = ~10), a period or comma part
# after the number (~3.11, ~1.5 and ~1,250 are left to the version and
# separator rules), a closing tilde later on the line (~2~ subscript,
# ~50 €~ strikethrough), and code (see _NumCode). A * or + opening a line
# is a list bullet, not an operator, so "* ~10 minutit" is flagged like
# "- ~10 minutit". Not flagged: ≈, a mathematical sign (π ≈ 3,14).
_NUM_TILDE_RE = re.compile(r"~([ \u00a0]?)(\d+(?:[ \u00a0]\d{3}(?!\d))*(?:,\d{1,2}(?!\d))?)")
_NUM_FENCE_RE = re.compile(r"^ {0,3}(?:```|~~~)", re.M)
_NUM_TILDE_CLOSE_RE = re.compile(r"[^\s(\[~]~")
_NUM_TILDE_AFTER_NUMBER_RE = re.compile(r"[.,]\d")
_NUM_TILDE_OPERATORS = "=<>+*/&^%"
_NUM_TILDE_SPACED_AFTER = ":,([|"
_NUM_TILDE_LINE_MARKS = " \t-*+•>#|"
_NUM_TILDE_BULLET_RE = re.compile(r"[ \t]*[*+][ \t]+\Z")

# Code is not prose. The tilde, month.year and range rules skip inline code
# spans and fenced blocks. A span opens on a run of backticks and closes
# on the next run of the same length on its line (CommonMark), so `` ~5 ``
# is code too; a fenced block runs from one fence line to the next. Only
# the tilde also skips indented code blocks: lines indented four spaces or
# a tab after a blank line or the text start, outside a list (CommonMark).
# A tilde there is shell or git (ls ~1, HEAD~2) far more often than prose,
# while an indented 2015 - 2019 is often a dated list line. The cost: a
# plain-text paragraph indented that way gets no tilde hint.
_NUM_TICK_RUN_RE = re.compile(r"`+")
_NUM_LIST_ITEM_RE = re.compile(r"[ \t]*(?:[-*+•]|\d+[.)])[ \t]")


class _NumCode:
    """Where a text has code, indexed on first use: a text with no
    candidate pays nothing, and each lookup is a bisect."""

    def __init__(self, text: str) -> None:
        self._text = text

    @cached_property
    def newlines(self) -> list[int]:
        return [m.start() for m in re.finditer("\n", self._text)]

    @cached_property
    def _spans(self) -> tuple[list[int], list[int], list[int]]:
        """Inline code spans as parallel lists of content starts and ends,
        and the fence line starts."""
        text, newlines = self._text, self.newlines
        runs = [(r.start(), r.end()) for r in _NUM_TICK_RUN_RE.finditer(text)]
        by_len: dict[int, list[int]] = {}
        for j, (s, e) in enumerate(runs):
            by_len.setdefault(e - s, []).append(j)
        starts: list[int] = []
        ends: list[int] = []
        j = 0
        while j < len(runs):
            s, e = runs[j]
            same = by_len[e - s]
            n = bisect.bisect_right(same, j)
            if n < len(same):
                close = runs[same[n]][0]
                if bisect.bisect_left(newlines, e) == bisect.bisect_left(newlines, close):
                    starts.append(e)
                    ends.append(close)
                    j = same[n] + 1
                    continue
            j += 1
        return starts, ends, [f.start() for f in _NUM_FENCE_RE.finditer(text)]

    def __contains__(self, i: int) -> bool:
        """Whether position `i` is in an inline code span or fenced block."""
        starts, ends, fences = self._spans
        k = bisect.bisect_right(starts, i) - 1
        return (k >= 0 and i < ends[k]) or bisect.bisect_left(fences, i) % 2 == 1

    @cached_property
    def indented(self) -> set[int]:
        """Line numbers (from 0) of the lines in indented code blocks."""
        code: set[int] = set()
        blank, block, listed = True, False, False
        for n, line in enumerate(self._text.split("\n")):
            if not line.strip():
                blank = True
                continue
            if line[:4].expandtabs(4).startswith("    "):
                if block or (blank and not listed):
                    code.add(n)
                    block = True
            else:
                block = False
                if _NUM_LIST_ITEM_RE.match(line):
                    listed = True
                elif blank:
                    listed = False
            blank = False
        return code


# A month and year in digits (09.2016) is a date, not a decimal. EKI
# (Arvukirjutus p 33) finds the month in words clearer: "tööde lõpp: sept
# 2016 (mitte: tööde lõpp: 09.2016)". A two-digit month gets that hint,
# with Sõnaveeb's month abbreviation so no case ending is needed (alates
# sept 2016); march to july have none. A one-digit month (1.2050) could be
# an exchange rate, so it is only kept out of the decimal rule. A unit,
# currency or math sign next to the number still makes it a decimal.
# 10, 11 and 12 have no leading zero to mark a month, and a SEK or NOK
# rate (11.2050), a coefficient, a mean or a coordinate has their shape. A
# rate word or a currency pair (EUR/SEK) earlier in the clause makes one a
# decimal again, unless a date word stands next to it (keskmine palk
# seisuga 10.2023). A date cue is not required instead: most months stand
# with none (kursus algab 10.2027), and reading those as decimals would
# turn a date into 10,2027, while a rate nearly always names what it is.
# A comma does not end the clause here, so a list of rates shares its word
# (koefitsiendid olid 0.2019, 10.2019 ja 11.1998).
_NUM_MONTH_ABBR = ("jaan", "veebr", "märts", "apr", "mai", "juuni", "juuli",
                   "aug", "sept", "okt", "nov", "dets")
_NUM_MONTH_RATE_RE = re.compile(
    r"\w*(?:kurs(?:s|si|i|il|ile|ilt|iga|ist)|koefitsien\w*|kordaja\w*|keskmi\w*"
    r"|mediaan\w*|kraad\w*)", re.I)
_NUM_MONTH_DATE_RE = re.compile(r"seisuga|alates|kuni|enne|pärast|tähtaeg\w*|kuupäev\w*", re.I)
_NUM_CURRENCY_PAIR_RE = re.compile(r"\b[A-Z]{3}/[A-Z]{3}\b")
_NUM_CLAUSE_END_RE = re.compile(r"[.!?;][\"'”»)\]]*\s|\n")
_NUM_LETTERS_RE = re.compile(r"[^\W\d_]+")
_NUM_MATH_BEFORE_RE = re.compile(r"[=<>≈±][^\S\n]*\Z")

# A spaced hyphen between two numbers that make a range (2020 - 2024, 5 - 10
# päeva, 10.00 - 12.00). EKI writes a range unspaced when neither end
# contains a space (teatmik "Tühikud", Arvukirjutus pp 35-38), with a
# mõttekriips; a hyphen stands for "ja" between consecutive numbers. The
# suggestion only removes the spaces and keeps a plain hyphen. Only the
# keyboard hyphen is matched. Not ranges, and skipped: subtraction and
# chains (10 - 5 = 5), an expression after = (vanus = 1990 - 2024), code,
# scores (3 - 1: EKI writes 3 : 1; a sports word such as geim or poolaeg
# anywhere in the clause, other score words among the last three),
# phone numbers, digit groups (9000 - 10 000 may stay spaced), ordinal and
# season ranges (2015 - 2019. aastal, õppeaasta 2025 - 2026), descending
# pairs, month names after the range (5 - 10 mai), and month.year ends (EKI
# wants the months in words there, spaced: aprill 2008 - august 2009).
# Bare integers need a cue: a unit, a page or paragraph word, kell, a glued
# compound (18 - 25-aastased) or a partitive noun after them (5 - 10
# päeva). A singular label noun before the left number with a unit or noun
# after the right one is a label and its value, the hyphen typed for EKI's
# spaced mõttekriips (ülesanne 1 - 10 punkti, pakett 2 - 40 eurot), not a
# range; plural labels (peatükid 3 - 5) stay ranges. Only the last three
# words are read for most score words, as tulemus, mäng or lõpp a clause
# back is as often prose (uuringu tulemuste põhjal kestab ravi 5 - 10 päeva).
_NUM_RANGE_EP = r"\d{1,4}(?:[.,]\d{1,4}){0,2}"
_NUM_RANGE_SPACED_RE = re.compile(
    r"(?<![\w.,/:+\-\u2013\u2212±€$£#~])"
    rf"({_NUM_RANGE_EP})"
    r"([^\S\n]+-[^\S\n]+|-[^\S\n]+)"
    rf"({_NUM_RANGE_EP})"
    r"(?![\d.,]?\d)")
_NUM_RANGE_CHAIN_BEFORE_RE = re.compile(
    r"\d[^\S\n]*(?:[-\u2013\u2212+×*/=]|[^\S\n]:)[^\S\n]*\Z|\d[^\S\n]+\Z|=[^\S\n]*\Z")
_NUM_RANGE_AFTER_BAD_RE = re.compile(
    r"[^\S\n]+\d"
    r"|[^\S\n]*(?:=|[+×*/<>≤≥]|[-\u2013\u2212][^\S\n]*\d|[^\S\n]:[^\S\n]*\d)"
    r"|\.[^\S\n]+[a-zõäöüšž]"
    r"|[^\S\n]*%[^\S\n]*[-\u2013]"
    r"|\.[^\W\d_]"
    r"|\.[^\S\n]+[IVX]+\b")
_NUM_RANGE_PHONE_RE = re.compile(
    r"(?:tel|telefon|mob|mobiil|gsm|faks|fax|helista|kontakt|kood|isikukood"
    r"|registrikood|isbn|issn|konto|arveldusarve)\w*\.?:?", re.I)
_NUM_RANGE_CUE_RE = re.compile(
    r"(?:lk|lehek\w*|pp|p|punkt\w*|§|lg|lõi[gk]\w*|ptk|peatük\w*|kell|kella\w*|kl"
    r"|aastatel|aastail|vanuses|vahemikus|ajavahemikus|perioodil)\.?", re.I)
_NUM_RANGE_SCORE_RE = re.compile(
    r"(?:seis|skoor|tulemus|võit|kaot|viik|viigi|mäng|kohtumi|matš|poolaj|alist"
    r"|lõpp|lõppe|finaal|geim|sett)\w*", re.I)
_NUM_RANGE_SCORE_CLAUSE_RE = re.compile(
    r"(?:skoor|viik|viigi|matš|poolaj|alist|finaal|geim|sett)\w*", re.I)
_NUM_RANGE_CLAUSE_CUT_RE = re.compile(r"[.!?;,][\"'”»)\]]*[^\S\n]")
_NUM_RANGE_LABEL_RE = re.compile(
    r"ülesanne|küsimus|pakett|variant|rühm|klass|tuba|ruum|korrus|osa|episood"
    r"|joonis|tabel|samm|etapp|voor|peatükk", re.I)
_NUM_RANGE_YEAR_WORD_RE = re.compile(
    r"(?:õppe|majandus|eelarve|hooaja|kalendri)?aasta\w*|hooa[ej]\w*|õa|a", re.I)
_NUM_RANGE_SEASON_BEFORE_RE = re.compile(
    r"\w*hooa[ej]\w*|\w*õppeaasta\w*|\w*majandusaasta\w*", re.I)
_NUM_RANGE_MONTHS = ("jaan", "veebr", "märts", "apr", "mai", "juun", "juul",
                     "aug", "sept", "okt", "nov", "dets")
_NUM_RANGE_NEXT_WORD_RE = re.compile(r"[^\S\n]*([^\W\d_]+)")

# A derivational suffix on the per cent sign. EKI (Arvukirjutus p 30):
# "protsendimärgiga, millele tuletusliiteid ei lisata": 20% hinnaalandus or
# 20-protsendiline hinnaalandus, never 20%-line. Case endings stay
# (10%-st, 5%-ni, 3%-le); none of them starts with li, ne or se. No form
# of -line starts with lisa, so a noun (5%-lisa, 10%-lisatasu) is no
# suffix. The number starts after a non-digit: a match tried inside a long
# digit run would rescan the rest of it, quadratic in the run's length.
_NUM_PERCENT_SUFFIX_RE = re.compile(
    r"(?<!\d)(\d+(?:,\d+)?)[^\S\n]?%-?(li(?:ne|s(?!a)[a-zõäöü]*|st)|ne|se[a-zõäöü]*)"
    r"(?![a-zõäöüšž])")


def _number_word_known(word: str, known: dict[str, bool]) -> bool:
    """Whether Vabamorf's lexicon knows `word`, without guessing: Tartus
    yes, Python no. `known` holds this text's lookups. True when Vabamorf
    fails or the text has used its lookups, so the number is flagged."""
    if word not in known:
        if len(known) >= _NUM_LOOKUPS:
            return True
        try:
            found = _vabamorf().analyze(
                words=[word], guess=False, propername=False, disambiguate=False)
            known[word] = any(w["analysis"] for w in found)
        except Exception:
            known[word] = True
    return known[word]


def _number_before(text: str, start: int) -> tuple[str, str, int]:
    """What is glued to the number at `start` ('' after a space), and the
    word before it on its line with its start: no opening brackets or
    quotes, trailing colon or hyphenated case ending (p-s, iOS-i)."""
    lo = max(0, start - 40)
    nl = text.rfind("\n", lo, start)
    toks = list(_NUM_WORD_RE.finditer(text, nl + 1 if nl >= 0 else lo, start))[-2:]
    glued = toks[-1].group() if toks and toks[-1].end() == start else ""
    for tok in reversed(toks):
        word = tok.group().lstrip(_NUM_OPENERS).rstrip(":")
        if word:
            return glued, _NUM_CASE_END_RE.sub("", word), tok.start()
    return glued, "", start


def _number_line_marks(text: str, start: int) -> str | None:
    """The spaces, bullets and Markdown marks (#, >) between the line start
    and the number at `start`, or None when the number does not open it."""
    lo = max(0, start - 20)
    nl = text.rfind("\n", lo, start)
    if nl < 0 and lo:
        return None
    marks = text[nl + 1:start]
    return None if marks.strip(" \t#>-*+•") else marks


def _clock_minutes(m: re.Match) -> int | None:
    """Minutes since midnight when a candidate reads as h.mm, else None."""
    whole, frac = m.groups()
    if len(whole) <= 2 and len(frac) == 2 and int(whole) <= 24 and int(frac) <= 59:
        return int(whole) * 60 + int(frac)
    return None


def _decimal_skipped(text: str, cands: list[re.Match], i: int, known: dict[str, bool]) -> bool:
    """True when candidate `i` is no English decimal, by the rules above."""
    m = cands[i]
    whole, frac = m.groups()
    start, end = m.span()
    after = text[end:end + 2]
    if after[:1] == "," and after[1:].isdigit():
        return True
    glued, word, word_start = _number_before(text, start)
    if glued.lstrip(_NUM_OPENERS) in ("v", "V") or (glued[-1:] == "-" and glued[-2:-1].isalpha()):
        return True
    final = after[:1] == "."
    marks = _number_line_marks(text, start)
    if marks is not None:
        nxt = _NUM_NEXT_LETTER_RE.match(text, end)
        if final or ("#" in marks and nxt and nxt.group(1).isupper()):
            return True
    # A number joined by a dash shares the unit and may close a time range.
    left = cands[i - 1] if i and _NUM_RANGE_GAP_RE.fullmatch(text, cands[i - 1].end(), start) else None
    right = (cands[i + 1] if i + 1 < len(cands)
             and _NUM_RANGE_GAP_RE.fullmatch(text, end, cands[i + 1].start()) else None)
    if any(_NUM_UNIT_RE.match(text, c.end())
           or _NUM_CURRENCY_RE.search(text, max(0, c.start() - 5), c.start())
           for c in (left, m, right) if c):
        return False
    low = word.lower()
    here = _clock_minutes(m)
    before = _clock_minutes(left) if left and here is not None else None
    later = _clock_minutes(right) if right and here is not None else None
    short = len(whole) <= 2 and len(frac) == 2
    return (
        _NUM_KELL_RE.fullmatch(low) is not None
        or (before is not None and before < here) or (later is not None and here < later)
        or _NUM_SECTION_RE.fullmatch(low) is not None or word == "lisa"
        or (short and 1 <= int(whole) <= 31 and 1 <= int(frac) <= 12)  # a date
        or (final and (word != low or (short and int(whole) <= 59 and int(frac) <= 59)))
        or (word[:1].isupper() and not word.isupper() and word[-1].isalnum()  # a product
            and not _NUM_SENTENCE_START_RE.search(text, max(0, word_start - 20), word_start)
            and not _number_word_known(word, known)))


def _tilde_issues(text: str, code: _NumCode) -> list[dict]:
    """approximate-tilde issues: a tilde meaning "about" before a number.
    Line ends, code and closing tildes are indexed once, so a long single
    line costs no more than many short ones."""
    issues: list[dict] = []
    matches = list(_NUM_TILDE_RE.finditer(text))
    if not matches:
        return issues
    newlines = code.newlines
    closers = [c.end() - 1 for c in _NUM_TILDE_CLOSE_RE.finditer(text)]
    for m in matches:
        i, end = m.span()
        line = bisect.bisect_left(newlines, i)
        ls = newlines[line - 1] + 1 if line else 0
        le = newlines[line] if line < len(newlines) else len(text)
        window = text[max(ls, i - 40):i]
        prev = window[-1:]
        prev_ns = window.rstrip()[-1:]
        if prev and not prev.isspace() and prev not in "([":
            continue
        bullet = i - ls <= 40 and _NUM_TILDE_BULLET_RE.match(text, ls, i) is not None
        if prev_ns.isdigit() or (prev_ns and prev_ns in _NUM_TILDE_OPERATORS and not bullet):
            continue
        if m.group(1):
            opens_line = i - ls <= 40 and window.strip(_NUM_TILDE_LINE_MARKS) == ""
            prev_ls = newlines[line - 2] + 1 if line >= 2 else 0
            prev_blank = ls == 0 or text[prev_ls:ls - 1].strip() == ""
            marked = any(ch in window for ch in "-*+•>#|")
            if not ((prev_ns and prev_ns in _NUM_TILDE_SPACED_AFTER)
                    or (opens_line and (prev_blank or marked))):
                continue
        if _NUM_TILDE_AFTER_NUMBER_RE.match(text, end):
            continue
        if text.startswith("~", end):
            continue
        k = bisect.bisect_right(closers, end)
        if k < len(closers) and closers[k] < le:
            continue
        if i in code or line in code.indented:
            continue
        start = bullet or _NUM_SENTENCE_START_RE.search(text, max(0, i - 20), i)
        issues.append({
            "text": m.group(0),
            "position": i,
            "rule": "approximate-tilde",
            "rule_estonian": "umbkaudne arv",
            "suggestion": ("Umbes " if start else "u ") + m.group(2),
        })
    return issues


def _month_year(text: str, cands: list[re.Match], i: int) -> str | None:
    """'' when candidate `i` is a month and year with a one-digit month, the
    hint ('sept 2016') when the month has two digits, None when it is no
    month and year: a unit, currency or math sign makes it a number, and so
    does a rate word before a 10 to 12 (kurss 11.2050)."""
    m = cands[i]
    whole, frac = m.groups()
    if not (len(whole) <= 2 and 1 <= int(whole) <= 12 and len(frac) == 4
            and 1900 <= int(frac) <= 2099):
        return None
    start, end = m.span()
    if _NUM_MATH_BEFORE_RE.search(text, max(0, start - 4), start):
        return None
    left = cands[i - 1] if i and _NUM_RANGE_GAP_RE.fullmatch(text, cands[i - 1].end(), start) else None
    right = (cands[i + 1] if i + 1 < len(cands)
             and _NUM_RANGE_GAP_RE.fullmatch(text, end, cands[i + 1].start()) else None)
    if any(_NUM_UNIT_RE.match(text, c.end())
           or _NUM_CURRENCY_RE.search(text, max(0, c.start() - 5), c.start())
           for c in (left, m, right) if c):
        return None
    if len(whole) == 1:
        return ""
    if whole[0] == "1" and _month_is_rate(text, start, end):
        return None
    return f"{_NUM_MONTH_ABBR[int(whole) - 1]} {frac}"


def _month_is_rate(text: str, start: int, end: int) -> bool:
    """Whether a 10.yyyy to 12.yyyy at start:end is a rate or a measure: a
    rate word or a currency pair earlier in its clause and no date word
    next to it. Reads at most 60 characters back and 40 ahead."""
    window = text[max(0, start - 60):start]
    cuts = [c.end() for c in _NUM_CLAUSE_END_RE.finditer(window)]
    if cuts:
        window = window[cuts[-1]:]
    words = _NUM_LETTERS_RE.findall(window)[-6:]
    nxt = _NUM_RANGE_NEXT_WORD_RE.match(text, end, min(len(text), end + 40))
    if ((words and _NUM_MONTH_DATE_RE.fullmatch(words[-1]))
            or (nxt and _NUM_MONTH_DATE_RE.fullmatch(nxt.group(1)))):
        return False
    return (_NUM_CURRENCY_PAIR_RE.search(window) is not None
            or any(_NUM_MONTH_RATE_RE.fullmatch(w) for w in words))


def _range_kinds(ep: str) -> dict:
    """What a range end can be (year, date, time, ...), each with a key
    that orders it, so a pair is a range only when it ascends."""
    kinds: dict = {}
    if "," in ep:
        if ep.count(",") == 1 and "." not in ep:
            a, b = ep.split(",")
            kinds["comma"] = float(f"{a}.{b}")
        return kinds
    parts = ep.split(".")
    if len(parts) == 1:
        kinds["int"] = int(ep)
        if len(ep) == 4 and 1000 <= int(ep) <= 2099:
            kinds["year"] = int(ep)
    elif len(parts) == 2:
        a, b = parts
        if len(a) <= 2 and len(b) == 2 and int(a) <= 24 and int(b) <= 59:
            kinds["time"] = int(a) * 60 + int(b)
        if len(a) <= 2 and len(b) <= 2 and 1 <= int(a) <= 31 and 1 <= int(b) <= 12:
            kinds["daymonth"] = (int(b), int(a))
        if len(a) <= 2 and len(b) == 4 and 1 <= int(a) <= 12 and 1900 <= int(b) <= 2099:
            kinds["monthyear"] = (int(b), int(a))
    elif len(parts) == 3:
        d, mo, y = parts
        if len(d) <= 2 and len(mo) <= 2 and len(y) == 4 and 1 <= int(d) <= 31 and 1 <= int(mo) <= 12:
            kinds["date"] = (int(y), int(mo), int(d))
    return kinds


def _range_partitive(word: str, known: dict[str, bool]) -> bool:
    """Whether some lexicon reading of `word` is a partitive noun or
    adjective (päeva, inimest), the noun a bare range counts. `known` holds
    this text's lookups; past _NUM_LOOKUPS the answer is no, so nothing
    more is flagged."""
    if word not in known:
        if len(known) >= _NUM_LOOKUPS:
            return False
        try:
            found = _vabamorf().analyze(
                words=[word], guess=False, propername=False, disambiguate=False)
            known[word] = any(a["partofspeech"] in ("S", "A") and a["form"] == "sg p"
                              for w in found for a in w["analysis"])
        except Exception:
            known[word] = False
    return known[word]


def _range_spacing_issues(text: str, taken: set[int], code: _NumCode) -> list[dict]:
    """range-spacing issues; `taken` holds every character position an
    earlier rule has already flagged."""
    issues: list[dict] = []
    known: dict[str, bool] = {}
    for m in _NUM_RANGE_SPACED_RE.finditer(text):
        left, _gap, right = m.groups()
        ls, rs, end = m.start(1), m.start(3), m.end()
        if ls in taken or rs in taken:
            continue
        if _NUM_RANGE_CHAIN_BEFORE_RE.search(text, max(0, ls - 40), ls):
            continue
        if _NUM_RANGE_AFTER_BAD_RE.match(text, end):
            continue
        if ls in code:
            continue
        nl = text.rfind("\n", max(0, ls - 60), ls)
        window = text[max(nl + 1, ls - 60):ls]
        before = [_NUM_CASE_END_RE.sub("", w.strip(_NUM_OPENERS).rstrip(":"))
                  for w in window.split()][-3:]
        before = [w for w in before if w]
        if any(_NUM_RANGE_PHONE_RE.fullmatch(w) for w in before):
            continue
        lk, rk = _range_kinds(left), _range_kinds(right)
        rising = [k for k in ("date", "monthyear", "time", "daymonth", "year", "comma", "int")
                  if k in lk and k in rk and lk[k] < rk[k]]
        if not rising or "monthyear" in rising:
            continue
        nxt = _NUM_RANGE_NEXT_WORD_RE.match(text, end)
        next_word = nxt.group(1) if nxt else ""
        if set(rising) & {"date", "time", "daymonth"}:
            ok = True
        elif "year" in rising:
            ok = not ((next_word and _NUM_RANGE_YEAR_WORD_RE.fullmatch(next_word))
                      or (before and _NUM_RANGE_SEASON_BEFORE_RE.fullmatch(before[-1])))
        else:
            if any(_NUM_RANGE_SCORE_RE.fullmatch(w) for w in before):
                continue
            # The clause: back to a sentence end, or a comma or semicolon
            # and a space (1,5 has none).
            cut = max((c.end() for c in _NUM_RANGE_CLAUSE_CUT_RE.finditer(window)), default=0)
            if any(_NUM_RANGE_SCORE_CLAUSE_RE.fullmatch(w)
                   for w in _NUM_LETTERS_RE.findall(window, cut)):
                continue
            cue = bool(before) and _NUM_RANGE_CUE_RE.fullmatch(before[-1]) is not None
            unit = _NUM_UNIT_RE.match(text, end) is not None
            compound = re.match(r"-[^\W\d_]", text[end:end + 2]) is not None
            noun = (bool(next_word) and text[end:end + 1].isspace()
                    and not next_word.lower().startswith(_NUM_RANGE_MONTHS)
                    and _range_partitive(next_word, known))
            if (left.isdigit() and (unit or noun) and before
                    and _NUM_RANGE_LABEL_RE.fullmatch(before[-1])):
                continue
            lone_year = "year" in rk and "year" not in lk
            ok = unit or compound or noun or (cue and not lone_year)
        if ok:
            issues.append({
                "text": m.group(0),
                "position": m.start(),
                "rule": "range-spacing",
                "rule_estonian": "tühikud vahemiku kriipsu ümber",
                "suggestion": f"{left}-{right}",
            })
    return issues


def _check_numbers(text: str) -> dict:
    """Number-writing checker: separators, month and year, ranges, the per
    cent sign and the approximation tilde."""
    _check_text(text)
    issues: list[dict] = []

    # Thousands: comma-grouped digits are the English thousands separator.
    # One group (1,250) could also be an Estonian decimal with three places;
    # the writer knows which, and the description says to leave a decimal
    # alone. 0,125 can only be a decimal. The suggestion follows EKI: four
    # digits are written together (1250), five and more in groups of three
    # (12 500, 1 500 000).
    with_decimals: set[int] = set()
    for m in _NUM_THOUSANDS_RE.finditer(text):
        groups, decimals = m.groups()
        if groups.startswith("0,"):
            continue
        if decimals is not None:
            with_decimals.add(m.end())
        digits = groups.replace(",", "")
        whole = digits if len(digits) <= 4 else f"{int(digits):,}".replace(",", " ")
        issues.append({
            "text": m.group(0),
            "position": m.start(),
            "rule": "thousands-separator",
            "rule_estonian": "tuhandeliste eraldaja",
            "suggestion": whole + (f",{decimals}" if decimals else ""),
        })

    cands = list(_NUM_DECIMAL_RE.finditer(text))
    known: dict[str, bool] = {}
    code = _NumCode(text)
    for i, m in enumerate(cands):
        # 000.50 of 1,500,000.50 is part of the thousands issue.
        if m.end() in with_decimals or _decimal_skipped(text, cands, i, known):
            continue
        month = _month_year(text, cands, i)
        if month is not None:
            # A month and year in code (`04.2017`) is program output.
            if month and m.start() not in code:
                issues.append({
                    "text": m.group(0),
                    "position": m.start(),
                    "rule": "month-year-digits",
                    "rule_estonian": "kuu sõnaga",
                    "suggestion": month,
                })
            continue
        issues.append({
            "text": m.group(0),
            "position": m.start(),
            "rule": "decimal-separator",
            "rule_estonian": "kümnenduskoma",
            "suggestion": f"{m.group(1)},{m.group(2)}",
        })

    for m in _NUM_PERCENT_SUFFIX_RE.finditer(text):
        number, suffix = m.groups()
        issues.append({
            "text": m.group(0),
            "position": m.start(),
            "rule": "percent-sign-suffix",
            "rule_estonian": "protsendimärk ilma liiteta",
            "suggestion": f"{number}% / {number}-protsendi{suffix}",
        })

    issues.extend(_tilde_issues(text, code))
    taken = {k for d in issues for k in range(d["position"], d["position"] + len(d["text"]))}
    issues.extend(_range_spacing_issues(text, taken, code))
    issues.sort(key=lambda d: d["position"])
    return {
        "issues": issues,
        "summary_estonian": (
            f"Leiti {len(issues)} numbrite õigekirjutuse viga." if issues
            else "Numbrite õigekirjutuse vigu ei leitud."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian number writing (numbrite õigekirjutus)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_numbers(text: Annotated[str, Field(description="Estonian text.")]) -> _CheckResult:
    """Flags number-writing slips (numbrite õigekirjutus):
    - `decimal-separator`: 3.14 → 3,14; skips kell 10.30, 14.05, punkt
      3.2, Python 3.12, but flags a bare 10.30.
    - `thousands-separator`: 1,250 → 1250, 1,500,000 → 1 500 000 (EKI
      groups from five digits; ideally non-breaking spaces, `suggestion`
      has plain ones). If 1,250 was meant as the decimal 1,25, keep it.
    - `approximate-tilde`: ~50 → u 50 (Umbes 50 at a sentence start; umbes
      and ligikaudu also fit). A style hint for general prose: EKI keeps
      the tilde for variants (mina ~ ma), so a spaced tilde after a word,
      ≈, code (inline, fenced or indented), paths, versions and ranges
      (5~10) are not flagged.
    - `month-year-digits`: 09.2016 → sept 2016, a date rather than a
      decimal; EKI finds the month in words clearer. A one-digit month
      (1.2019) is left alone, as it may be a rate, and 10 to 12 after a
      rate word or currency pair in the clause (kurss 11.2050, EUR/SEK,
      keskmine, koefitsient) is a decimal. Inflect a full month name to
      fit the sentence (alates märtsist 2019); abbreviations need no ending.
    - `range-spacing`: 2020 - 2024 → 2020-2024, 5 - 10 päeva → 5-10. Only
      a spaced keyboard hyphen between plain numbers; typeset EKI text uses
      the mõttekriips, also unspaced. Skips sums and expressions after =,
      scores, code, phone numbers, a label and its value (ülesanne 1 - 10
      punkti), ends containing a space (9000 - 10 000), ordinal and season
      ranges (2015.-2019. aastal, 2019/2020), and month.year ends.
    - `percent-sign-suffix`: 20%-line → 20% or 20-protsendiline; case
      endings (10%-st, 5%-ni) are fine.

    Spelling out small numbers is not checked.
    """
    return _check_numbers(text)


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian capitalization (Algustäheortograafia)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_capitalization(text: Annotated[str, Field(description="Estonian text.")]) -> _CheckResult:
    """Flags words wrongly capitalized mid-sentence (algustäheortograafia):
    - `weekday`, `month`, `nationality`: lowercase except at sentence
      start (esmaspäeval, jaanuaris, eestlane).
    - `language-adjective`: a country name before a language or culture
      noun is lowercase (eesti keel, vene kultuur). On its own it is the
      country (Eesti, Eestis) and is not flagged.

    Sentence-initial words and all-caps acronyms are skipped. A hit that
    opens a line, list item or quotation, or sits in a proper name (Eesti
    Keele Instituut), is usually false. English title case in headings is
    check_translationese's `title-case`.
    """
    return _check_capitalization(text)


# The one gate left. The fastText top-neighbour score is a proxy for "how
# deep in the corpus does this compound sit", and it separates coinages
# from real vocabulary only at the very bottom of its range. Calibrated
# by running 64 attested compounds and 13 coinages through the production
# model; the ones that pin the boundary are fixtures in
# tests/test_familiarity.py. The lowest-scoring attested compound whose
# top neighbour is a real word is `hinnakujundus` at 0.563, and the gate
# sits just under it.
#
# It used to sit at 0.60, raised there to catch one coinage,
# `toortõlkeoht` (0.571). That raise put the gate INSIDE the band where
# ordinary Estonian lives (`hinnakujundus` 0.563, `kliendibaas` 0.566,
# `katusekorter` 0.577, `klikkimismäär` 0.586, `koolitoit` 0.590,
# `rehvivahetus` 0.596, `tervisetõend` 0.598, `müügilehter` 0.599), and
# every one of them was reported to the caller as a suspected coinage.
# Back under the floor it goes, and `toortõlkeoht` is a documented miss:
# no threshold can hold it and release `hinnakujundus`, eight
# thousandths away.
_FAMILIARITY_SUSPECT_SCORE = 0.55

# WordNet is a sqlite lookup per distinct compound lemma, and a 100,000
# character document can hold hundreds. Cap it the way
# check_term_consistency caps its own: compounds past the cap are still
# analysed, just on corpus evidence alone (`in_wordnet: null`).
_FAMILIARITY_WORDNET_CAP = 200


def _looks_like_scrape_junk(word: str) -> bool:
    """True for concatenated web-scrape tokens the compressed model's
    vocabulary carries (e.g. 'KoolKudumidPolosärgidTriiksärgid'). An
    uppercase letter anywhere but the first position never occurs in a
    normal Estonian word."""
    return any(ch.isupper() for ch in word[1:])


def _familiarity_verdict(
    in_vocab: bool,
    top_score: float,
    neighbours: list,
    parts: list,
    attested_elsewhere: bool = False,
) -> tuple[bool, list[str], dict]:
    """Decide whether a compound is a suspect coinage. Pure (no model I/O)
    so the heuristic is unit-testable against captured fastText output
    without loading the 33 MB model.

    `neighbours` is a list of (word, score) pairs; `parts` is the input
    compound's morphemes (Vabamorf root_tokens); `attested_elsewhere` is
    true when a lexicon outside the embedding vocabulary (Estonian
    WordNet, the legal terms-of-art list) carries the lemma. Returns
    (is_suspect, reasons, neighbour_quality).

    Three states, in order:

    ATTESTED: in the 100K-word corpus vocabulary, or in WordNet, or a
    known term of art. Real by definition, never suspect.

    NO SIGNAL: out-of-vocabulary and the nearest neighbour is not a word:
    fastText fell back to character n-grams and landed in the hub of
    scrape-artifact tokens the compressed vocabulary carries. Nothing can
    be concluded, in either direction, so nothing is claimed. This is the
    branch that used to flag: a junk top neighbour was read as evidence of
    invention, and it is not. Attested `lisakäive` (top neighbour
    'kihtFliisidPluusidPüksid', 0.670) and coined `klõpsusild` (top
    'pluusidPolosärgidPüksid', 0.656) are indistinguishable here: the
    scores are a flat plateau, which is what a hub looks like from the
    inside.

    WEAK: out-of-vocabulary, the top neighbour IS a real word, and even
    so the similarity is under the gate. The only configuration where the
    model has something to say and what it says is bad.

    Subword-echo overlap is COUNTED in the returned quality (the tool
    shows the caller only its `signal`) but never triggers: real
    sibling compounds share a head morpheme too (tervisekindlustus ↔
    ravikindlustus). Nor does the junk RATIO over the neighbour tail: a
    junky tail behind a real top neighbour describes how sparse that
    corner of a 100K vocabulary is, not whether the word is real.
    """
    names = [n for n, _ in neighbours]
    n = len(names)
    junk = sum(1 for w in names if _looks_like_scrape_junk(w))
    echoes = sum(
        1 for w in names
        if any(len(p) >= 4 and p in w.lower() for p in parts)
    )
    top_is_junk = bool(names) and _looks_like_scrape_junk(names[0])
    quality = {
        "neighbours": n,
        "scrape_junk": junk,
        "subword_echoes": echoes,
        "signal": "none" if (not names or top_is_junk) else "usable",
    }

    if in_vocab or attested_elsewhere:
        return False, [], quality
    if quality["signal"] == "none":
        return False, [], quality
    if top_score < _FAMILIARITY_SUSPECT_SCORE:
        return True, [
            f"unattested (not in the corpus vocabulary, not in WordNet) and "
            f"its nearest real neighbour is only {top_score:.3f} "
            f"(< {_FAMILIARITY_SUSPECT_SCORE})"
        ], quality
    return False, [], quality


def _check_compound_familiarity(text: str, *, neighbours: bool = True) -> dict:
    """Attestation check for compound nouns, with the evidence attached.

    For each compound noun (Vabamorf root_tokens of length >= 2) in the
    text, ask two lexicons whether the lemma is attested (the fastText
    model's 100K-word corpus vocabulary and Estonian WordNet). An attested
    compound is reported as attested, and its neighbours are not searched:
    they cannot change the verdict. An unattested one carries its fastText
    nearest neighbours, and is flagged only in the single
    configuration where the model has something to say and what it says
    is bad: its nearest neighbour is a real word and even that similarity
    is under the gate. The decision lives in `_familiarity_verdict` (a
    pure function, unit-tested without the model).

    Output is *diagnostic*, not authoritative. Unattested is the normal
    state for correct specialist vocabulary in a 100K-word vocabulary.
    `lisakäive`, `klikkimismäär`, `koolitoit` and `rehvivahetus` are all
    unattested here and all ordinary Estonian, so an unattested verdict
    is never on its own a reason to rewrite anything.

    `neighbours=False` is check_domain_terms' path. That tool reads only
    attestation, so the fastText neighbour search, nearly all of the cost
    (~0.15 s a compound), is skipped and no verdict is reached. Entries
    carry no neighbours, and neighbour_quality only for a legal term
    ({"legal_term": true}). Each compound carries `forms` instead: every
    lemma reading and surface form the text has for it (see _word_forms).
    """
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["morph_analysis"])
    kv = _embeddings()
    vocab = kv.key_to_index

    # WordNet is the second attestation source, and it carries compounds
    # the pruned corpus vocabulary drops: `pilveteenus` (top neighbour
    # 0.536, under the gate) and `katusekorter` are both in it, and both
    # are ordinary Estonian. Checked on disk first, never downloaded;
    # see _wordnet_available. Absent, the tool still runs on corpus
    # evidence alone and says so in `wordnet_checked`.
    wn = None
    if _wordnet_available():
        try:
            wn = _wordnet()
        except Exception:
            wn = None
    wordnet_lookups = 0

    seen: set[str] = set()
    compounds: list[dict] = []
    forms_by_lemma: dict[str, set[str]] = {}

    for span in t.morph_analysis:
        pos = _first(list(span.partofspeech))
        if pos != "S":
            continue
        rt_lists = [list(rt) for rt in span.root_tokens]
        parts = rt_lists[0] if rt_lists else []
        if len(parts) < 2:
            continue
        lemma_raw = _first(list(span.lemma)) or ""
        if not lemma_raw:
            continue
        # Skip proper-noun-like words (capitalized lemma).
        if lemma_raw[0].isupper():
            continue
        lemma = lemma_raw.lower()
        forms_by_lemma.setdefault(lemma, set()).update(_word_forms(span))
        if lemma in seen:
            continue
        seen.add(lemma)

        in_vocab = lemma in vocab

        # Second attestation source. Specialised legal compounds
        # (õigussuhe, solidaarvõlgnik, abieluvaraleping) are OOV in the
        # general-web fastText vocab and used to be false-flagged as
        # coinages (~15% of legal compounds); a known term of art is real
        # by definition, and so is a WordNet lemma. Both are marked for
        # every hit, because the caller wants to know which lexicon
        # vouched for the word: WordNet in `in_wordnet`, the legal list in
        # `neighbour_quality.legal_term`.
        legal_term = _is_legal_term(lemma)
        in_wordnet: bool | None = None
        if wn is not None and wordnet_lookups < _FAMILIARITY_WORDNET_CAP:
            wordnet_lookups += 1
            try:
                in_wordnet = bool(wn[lemma])
            except Exception:
                in_wordnet = None
        attested = bool(in_vocab or in_wordnet or legal_term)

        # Attested ends the verdict, so the neighbours of an attested
        # lemma could only be shown, never weighed: the search runs for
        # unattested lemmas only.
        search = neighbours and not attested
        try:
            nearest = kv.most_similar(lemma, topn=8) if search else []
        except KeyError:
            nearest = []
        top_score = float(nearest[0][1]) if nearest else 0.0

        # Verdict (pure, see _familiarity_verdict): attested lemmas pass;
        # an unattested one is flagged only when its nearest neighbour is
        # a real word and that similarity is still under the gate.
        is_suspect, _, quality = _familiarity_verdict(
            in_vocab, top_score, nearest, parts,
            attested_elsewhere=bool(in_wordnet) or legal_term,
        )

        entry = {
            "word": span.text,
            "lemma": lemma,
            "parts": parts,
            "position": span.start,
            "in_vocab": in_vocab,
            "in_wordnet": in_wordnet,
            "attested": attested,
        }
        if search:
            entry["neighbours"] = [
                {"word": n, "score": round(float(s), 3)}
                for n, s in nearest[:5]
            ]
            entry["neighbour_quality"] = {"signal": quality["signal"]}
        if legal_term:
            entry.setdefault("neighbour_quality", {})["legal_term"] = True
        entry["is_suspect"] = is_suspect
        compounds.append(entry)

    glossary = _domain_glossary_or_empty()
    for c in compounds:
        forms = forms_by_lemma[c["lemma"]]
        _domain_mark(c, forms, glossary)
        if not neighbours:
            c["forms"] = sorted(forms)

    suspects = [c["lemma"] for c in compounds if c["is_suspect"]]
    unattested = [c for c in compounds if not c["attested"]]
    listed = sum(1 for c in unattested if c.get("in_domain_glossary"))

    result = {
        "compounds_analysed": len(compounds),
        "suspect_compounds": suspects,
        "unattested_compounds": [c["lemma"] for c in unattested],
        "wordnet_checked": wn is not None,
        "all_compounds": compounds,
        "summary_estonian": (
            (f"Tuvastati {len(compounds)} liitsõnanimisõna; "
             f"{len(unattested)} neist ei leidu ei sõnavaras ega "
             f"tesauruses (oskus- ja uudissõnade puhul on see tavaline)."
             + (f" {len(suspects)} juhul on ka lähinaaber nõrk ja tasub "
                f"üle vaadata." if suspects else "")
             + (f" Sõnavarast ja tesaurusest puuduvatest on {listed} "
                f"valdkonna sõnastikus." if listed else ""))
            if compounds else "Liitsõnanimisõnu analüüsiks ei leitud."
        ),
    }
    # WordNet gave no answer for an unattested lemma, so it might still be
    # there: unavailable, past the lookup cap, or a lookup that failed.
    if any(c["in_wordnet"] is None and not c["attested"] for c in compounds):
        result["note"] = (
            "WordNet was unavailable (`wordnet_checked: false`), so "
            "`attested` rests on the corpus vocabulary and the legal terms "
            "list alone." if wn is None else
            "Where `in_wordnet` is null, WordNet was not asked (it is asked "
            f"about the first {_FAMILIARITY_WORDNET_CAP} compounds only) or "
            "the lookup failed, so `attested` there rests on the corpus "
            "vocabulary and the legal terms list alone.")
    return result


@mcp.tool(annotations=ToolAnnotations(
    title="Check whether an Estonian compound is attested",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_compound_familiarity(text: Annotated[str, Field(description="Estonian text to check.")]) -> _CompoundFamiliarityResult:
    """Attestation check for Estonian compound nouns; use it on compounds you
    coined.

    A compound is `attested` when its lemma is in the fastText 100K-word
    corpus vocabulary (`in_vocab`), Estonian WordNet (`in_wordnet`) or the
    legal terms list (`neighbour_quality.legal_term`). Read `attested` first:
    false is normal for correct specialist words, not evidence of invention,
    and never on its own a reason to rewrite.

    `is_suspect` is narrow: unattested, and the nearest neighbour is a real
    word scoring under 0.55 (`mõtteliin`). Judge a flag from `neighbours`
    (unattested entries only): coherent related words mean the word is
    real. `neighbour_quality.signal: "none"`: the nearest neighbour is
    scrape junk, so there is no verdict. Recall is low on purpose: some
    coinages pass, and so does any stilted compound (check register with
    check_officialese).

    `in_domain_glossary: true` (only with a server glossary): the
    organisation's own word, so treat it as real. For your own glossary use
    check_domain_terms. Proper nouns are skipped.
    """
    return _check_compound_familiarity(text)


# Operator-supplied domain vocabulary. An organisation writing about its own
# systems has words the general corpus never saw, and an agent writing for it
# tends to coin compounds to name things instead of using them. Attestation
# can't tell the two apart, by design (see _check_compound_familiarity); a
# glossary can. Both files are optional: unset, nothing below changes any
# output. One lemma per line, UTF-8 (with or without a BOM), `#` starts a
# comment, matched case-insensitively after NFC normalisation. IDENTIFIERS
# holds names check_domain_terms never reports and never suggests (table or
# column names written as words); only that tool reads them.
#
# main() reads both files before serving (_load_domain_vocab), so a bad path
# stops the server at startup instead of failing tool calls.

_DOMAIN_GLOSSARY_ENV = "ESTNLTK_MCP_DOMAIN_GLOSSARY"
_DOMAIN_IDENTIFIERS_ENV = "ESTNLTK_MCP_DOMAIN_IDENTIFIERS"
_DOMAIN_CUTOFF_ENV = "ESTNLTK_MCP_DOMAIN_SUGGEST_CUTOFF"

# The difflib similarity a glossary term needs to be offered as a
# suggestion. The operator moves it with ESTNLTK_MCP_DOMAIN_SUGGEST_CUTOFF,
# a caller with `suggest_cutoff`. Lower finds more look-alikes, and more of
# them name a different thing. Under 0.4 a "look-alike" shares little more
# than its letters, and difflib's cheap filters stop filtering, so 0.4 is
# the floor.
_DOMAIN_SUGGEST_CUTOFF = 0.6
_DOMAIN_SUGGEST_CUTOFF_MIN = 0.4

# Suggestions compare a compound with every glossary term, so they cost
# (reported compounds) x (glossary size) x (term length), and a low cutoff
# multiplies that again. The tools run on the event loop, so the work is
# bounded twice: the first 200 reported compounds only, as with
# _FAMILIARITY_WORDNET_CAP, and 2 seconds a call. Past either, compounds are
# still reported, only without suggestions (`suggestions: null`), and
# `suggestions_capped` says so.
_DOMAIN_SUGGEST_CAP = 200
_DOMAIN_SUGGEST_SECONDS = 2.0


class _DomainVocabUnreadable(RuntimeError):
    """A configured domain file could not be read. The message names the
    env var and not the path, because it can reach anonymous callers; the
    server log has both."""


def _norm_term(term: str) -> str:
    return unicodedata.normalize("NFC", term).strip().lower()


def _read_term_file(path: str) -> frozenset[str]:
    terms: set[str] = set()
    text = Path(path).expanduser().read_text(encoding="utf-8-sig")
    for line in text.splitlines():
        term = _norm_term(line.split("#", 1)[0])
        if term:
            terms.add(term)
    return frozenset(terms)


def _read_domain_env(env: str) -> frozenset[str]:
    path = os.environ.get(env, "").strip()
    if not path:
        return frozenset()
    try:
        return _read_term_file(path)
    except (OSError, UnicodeError, RuntimeError, ValueError) as e:
        # RuntimeError: expanduser on an unknown ~user. ValueError: a NUL
        # in the path.
        log.error("%s=%s could not be read: %s", env, path, e)
        raise _DomainVocabUnreadable(
            f"{env} is set but its file could not be read; see the server log."
        ) from None


@lru_cache(maxsize=1)
def _domain_glossary() -> frozenset[str]:
    return _read_domain_env(_DOMAIN_GLOSSARY_ENV)


@lru_cache(maxsize=1)
def _domain_identifiers() -> frozenset[str]:
    return _read_domain_env(_DOMAIN_IDENTIFIERS_ENV)


def _domain_glossary_or_empty() -> frozenset[str]:
    """The glossary for the tools that existed before it. An unreadable
    file leaves them exactly as they were instead of failing them. main()
    refuses to start on one, so this matters only when server.py is
    imported directly."""
    try:
        return _domain_glossary()
    except _DomainVocabUnreadable:
        return frozenset()


def _domain_suggest_cutoff() -> float:
    raw = os.environ.get(_DOMAIN_CUTOFF_ENV, "").strip()
    if not raw:
        return _DOMAIN_SUGGEST_CUTOFF
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not _DOMAIN_SUGGEST_CUTOFF_MIN <= value <= 1.0:
        raise ValueError(
            f"{_DOMAIN_CUTOFF_ENV} must be a number from "
            f"{_DOMAIN_SUGGEST_CUTOFF_MIN} to 1, not {raw!r}")
    return value


def _load_domain_vocab(public: bool) -> None:
    """Read the domain files and the cutoff once, before serving, so a
    mistake in them stops the server instead of failing tool calls."""
    try:
        glossary, identifiers = _domain_glossary(), _domain_identifiers()
        _domain_suggest_cutoff()
    except (_DomainVocabUnreadable, ValueError) as e:
        sys.stderr.write(f"ERROR: {e}\n")
        sys.exit(2)
    for env, terms in ((_DOMAIN_GLOSSARY_ENV, glossary),
                       (_DOMAIN_IDENTIFIERS_ENV, identifiers)):
        if os.environ.get(env, "").strip():
            (log.info if terms else log.warning)("%s: %d terms", env, len(terms))
    if public and (glossary or identifiers):
        log.warning(
            "public mode with a domain glossary: anyone who can call the "
            "server can read its terms through check_domain_terms")


def _call_glossary(glossary: list[str] | None) -> frozenset[str]:
    """A per-call `glossary`, bounded and normalised like the file. The
    tool schema bounds it as well; this covers direct callers."""
    if not glossary:
        return frozenset()
    if len(glossary) > MAX_GLOSSARY_TERMS:
        raise ValueError(
            f"glossary has {len(glossary)} terms, over the limit of "
            f"{MAX_GLOSSARY_TERMS}; pass the terms this text needs"
        )
    for term in glossary:
        _check_text(term, limit=MAX_WORD_CHARS, name="glossary term")
    # Checked again after normalising: lower() can lengthen a string
    # ('İ' becomes 'i' plus a combining dot), and the normalised form is
    # what gets compared.
    terms = [_norm_term(term) for term in glossary]
    for term in terms:
        _check_text(term, limit=MAX_WORD_CHARS, name="normalised glossary term")
    return frozenset(t for t in terms if t)


def _word_forms(span) -> set[str]:
    """Every lemma Vabamorf offers for a word, plus the word as written.
    The glossary is matched against all of them: the first lemma alone
    misses ambiguous plurals (`ladustamiskohad` gives `ladustamiskoha`
    first and `ladustamiskoht` second), and an identifier is a name, so it
    matches as written (`tellimusread`, lemma `tellimusrida`)."""
    forms = {_norm_term(lemma) for lemma in span.lemma if lemma}
    forms.add(_norm_term(span.text))
    return forms


def _domain_mark(compound: dict, forms: set[str], glossary: frozenset[str]) -> dict:
    """Stamp `in_domain_glossary` on a compound, only when a glossary is
    loaded, so output without one stays exactly as it was. A listed
    compound is the organisation's own word, so it is never `is_suspect`."""
    if glossary:
        listed = not forms.isdisjoint(glossary)
        compound["in_domain_glossary"] = listed
        if listed and compound.get("is_suspect"):
            compound["is_suspect"] = False
    return compound


def _lemmas_of(word: str) -> set[str]:
    try:
        analysed = _vabamorf().analyze(words=[word], guess=True, propername=False)
    except Exception:
        return set()
    return {
        _norm_term(a["lemma"])
        for w in analysed for a in w["analysis"] if a.get("lemma")
    }


def _domain_parts_found(lemma: str, parts: list[str], terms: frozenset[str]) -> set[str]:
    """The glossary terms a compound is built on: `vagunireisitabel` is
    `vagunireis` + `tabel`. That is ordinary Estonian on the organisation's
    word, and offering `vagunireis` as its replacement would change the
    meaning. Cuts fall at Vabamorf's part boundaries, matched without case
    because a part keeps its capitals (`eID` in `eid-kaardilugeja`). A
    modifier stands in its compounding form (`vagunireisi`), so it is
    lemmatised before the lookup."""
    cuts: list[int] = []
    at = 0
    for part in parts[:-1]:
        part = part.lower()
        at = lemma.find(part, at)
        if at < 0:
            return set()
        at += len(part)
        cuts.append(at)
    found: set[str] = set()
    for cut in cuts:
        head, modifier = lemma[cut:].lstrip("-"), lemma[:cut].rstrip("-")
        if head in terms:
            found.add(head)
        if modifier in terms:
            found.add(modifier)
        else:
            found |= _lemmas_of(modifier) & terms
    return found


def _look_alikes(lemma: str, ranked: list[str], cutoff: float,
                 deadline: float) -> list[tuple[float, str]] | None:
    """Every (similarity, term) at or above `cutoff`: difflib's
    get_close_matches, with its three filters in its order, but stopping
    with None once time.monotonic() passes `deadline`."""
    matcher = difflib.SequenceMatcher()
    matcher.set_seq2(lemma)
    hits = []
    for i, term in enumerate(ranked):
        if i % 64 == 0 and time.monotonic() > deadline:
            return None
        matcher.set_seq1(term)
        if matcher.real_quick_ratio() >= cutoff and matcher.quick_ratio() >= cutoff:
            score = matcher.ratio()
            if score >= cutoff:
                hits.append((score, term))
    return hits


def _domain_terms_from(compounds: list[dict], terms: frozenset[str],
                       allowed: frozenset[str],
                       cutoff: float = _DOMAIN_SUGGEST_CUTOFF,
                       cap: int = _DOMAIN_SUGGEST_CAP,
                       seconds: float = _DOMAIN_SUGGEST_SECONDS) -> list[dict]:
    """The decision behind check_domain_terms: a compound is reported when
    it is unattested AND none of its forms (see _word_forms) is listed.
    Suggestions are the closest glossary lemmas by spelling similarity
    (difflib), not fastText, because a glossary term is usually outside
    the model vocabulary too. They are look-alikes, not synonyms, and never
    a term the compound is built on, which `contains_glossary_term` names
    when there is one. Only the first `cap` reported compounds, within
    `seconds`, are compared: the rest carry `suggestions` None and no
    `contains_glossary_term`. With no terms there is nothing to compare,
    and neither key is set."""
    ranked = sorted(terms)
    deadline = time.monotonic() + seconds
    unlisted = []
    for c in compounds:
        lemma = c["lemma"]
        forms = set(c.get("forms") or ()) | {lemma}
        if c["attested"] or not forms.isdisjoint(allowed):
            continue
        entry = {
            "word": c["word"],
            "lemma": lemma,
            "position": c["position"],
            "in_wordnet": c.get("in_wordnet"),
        }
        if terms:
            hits = _look_alikes(lemma, ranked, cutoff, deadline) if len(unlisted) < cap else None
            suggestions = None
            if hits is not None:
                found = _domain_parts_found(lemma, c.get("parts") or [], terms)
                if found:
                    entry["contains_glossary_term"] = max(sorted(found), key=len)
                suggestions = [
                    {"term": t, "similarity": round(score, 3)}
                    for score, t in heapq.nlargest(3, [h for h in hits if h[1] not in found])
                ]
            entry["suggestions"] = suggestions
        unlisted.append(entry)
    return unlisted


def _domain_terms_summary(analysed: int, unlisted: list[dict], lists_given: bool) -> str:
    if not analysed:
        return "Liitsõnanimisõnu analüüsiks ei leitud."
    k = len(unlisted)
    if not lists_given:
        return (
            (f"Tuvastati {analysed} liitsõnanimisõna; {k} neist ei leidu ei "
             f"sõnavaras, tesauruses ega õigusterminite loendis (oskus- ja "
             f"uudissõnade puhul on see tavaline)." if k else
             "Kõik leitud liitsõnanimisõnad on sõnavaras, tesauruses või "
             "õigusterminite loendis.")
            + " Valdkonna sõnastikku pole antud."
        )
    if not k:
        return ("Kõik leitud liitsõnanimisõnad on sõnavaras, tesauruses, "
                "õigusterminite loendis või valdkonna loendites.")
    s = sum(1 for u in unlisted if u.get("suggestions"))
    return (
        f"Tuvastati {analysed} liitsõnanimisõna; {k} neist ei leidu ei "
        f"sõnavaras, tesauruses, õigusterminite loendis ega valdkonna "
        f"loendites."
        + (f" Neist {s} jaoks leidub sõnastikus sarnase kirjapildiga termin."
           if s else "")
    )


def _check_domain_terms(text: str, glossary: list[str] | None = None,
                        suggest_cutoff: float | None = None) -> dict:
    _check_text(text)
    terms = _domain_glossary() | _call_glossary(glossary)
    identifiers = _domain_identifiers()
    allowed = terms | identifiers
    if suggest_cutoff is None:
        cutoff = _domain_suggest_cutoff()
    elif _DOMAIN_SUGGEST_CUTOFF_MIN <= suggest_cutoff <= 1.0:
        cutoff = suggest_cutoff
    else:
        raise ValueError(
            f"suggest_cutoff must be from {_DOMAIN_SUGGEST_CUTOFF_MIN} to 1, "
            f"not {suggest_cutoff}")
    fam = _check_compound_familiarity(text, neighbours=False)
    unlisted = _domain_terms_from(fam["all_compounds"], terms, allowed, cutoff)
    capped = any("suggestions" in u and u["suggestions"] is None for u in unlisted)
    result = {
        "glossary_size": len(terms),
        "identifiers_size": len(identifiers),
        "suggest_cutoff": cutoff,
        "compounds_analysed": fam["compounds_analysed"],
        "wordnet_checked": fam["wordnet_checked"],
        "unlisted_compounds": unlisted,
        "suggestions_capped": capped,
        "summary_estonian": _domain_terms_summary(
            fam["compounds_analysed"], unlisted, bool(allowed)),
    }
    notes = []
    if capped:
        notes.append(
            f"Suggestions stop after {_DOMAIN_SUGGEST_CAP} reported compounds "
            f"or {_DOMAIN_SUGGEST_SECONDS:g} seconds, whichever comes first; "
            "later entries have `suggestions: null` and were not checked "
            "for `contains_glossary_term`.")
    if any(u["in_wordnet"] is None for u in unlisted):
        notes.append(
            "WordNet was unavailable (`wordnet_checked: false`), so it may "
            "still list a reported compound." if not fam["wordnet_checked"] else
            "Where `in_wordnet` is null, WordNet was not asked (it is asked "
            f"about the first {_FAMILIARITY_WORDNET_CAP} compounds only) or "
            "the lookup failed, so it may still list that word.")
    if notes:
        result["note"] = " ".join(notes)
    return result


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian compounds against a domain glossary",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_domain_terms(
    text: Annotated[str, Field(description="Estonian text to check.")],
    glossary: Annotated[list[_GlossaryTerm] | None, Field(max_length=MAX_GLOSSARY_TERMS, description="Domain lemmas (base forms), added to the server's glossary for this call only. Not stored.")] = None,
    suggest_cutoff: Annotated[float | None, Field(ge=_DOMAIN_SUGGEST_CUTOFF_MIN, le=1.0, description="Minimum spelling similarity for a glossary term to be suggested. Default: the server's setting, else 0.6.")] = None,
) -> _DomainTermsResult:
    """Reports compounds no lexicon or domain glossary vouches for, in Estonian
    you wrote about an organisation's own systems.

    A compound is reported when it is unattested (see
    check_compound_familiarity) and absent from the server's glossary and
    identifier list and your `glossary`. A reported compound is not
    necessarily coined: it is only not the organisation's word, and may be
    ordinary Estonian (`vagunisõit` is). Where it names something the
    organisation has a term or identifier for, use theirs; otherwise leave
    it.

    `suggestions` are glossary terms SPELLED alike, not synonyms: use one only
    if it names the same thing. `contains_glossary_term` is the term the
    compound is built on (`vagunireisitabel` on `vagunireis`): ordinary
    Estonian, not to be shortened to the term; ask only whether the thing it
    names has its own term.

    The hosted server has no glossary: pass yours in `glossary`. Without one
    this is the unattested list, never on its own a reason to rewrite.
    Proper nouns are skipped.
    """
    return _check_domain_terms(text, glossary, suggest_cutoff)


# HCl, KCl, NH4Cl, CH3Cl: Vabamorf reads a chloride formula as a capital
# stem and the adessive l (HC + l), but the l is the l of Cl. Every capital
# in the stem is a one-letter element symbol and the last is C. With a
# digit in the stem that is a formula; without one only the common
# formulas are, so BBCl and PCl (BBC-l, PC-l) are still flagged.
_CHLORIDE_STEM = re.compile(r"(?:[BCFHIKNOPSUVWY]\d*)*C")
_CHLORIDE_PLAIN_STEMS = frozenset({"HC", "KC", "IC", "OC", "HOC", "NOC"})
# I to XXXIX: the Roman ordinals of centuries, classes, floors and stages.
# CD, MM, DC and the like are kept out: they are far more often abbreviations.
_ROMAN_IVX = re.compile(r"(?=[IVX])X{0,3}(?:IX|IV|V?I{0,3})")
# Letters whose Estonian name ends in a consonant: eff, ell, emm, enn,
# err, ess, iks, zett. J and Y are left out: DJ is read dii-džei (EKI) and
# a capital word ending in Y is mostly English (SONY, SKY).
_CONSONANT_NAMED = frozenset("FLMNRSXZ")
# Out-of-context lookups one check_abbreviation_hyphenation call may make.
# Only the word after a Roman ordinal with an l-ending is looked up, and
# only when disambiguation kept no noun reading in its case (XIIl korral:
# korral comes out as the postposition), so real text needs a handful;
# past the budget the numeral keeps its hyphenated ending.
_ABBR_LOOKUPS = 64


@lru_cache(maxsize=4096)
def _abbr_noun_forms(word: str) -> frozenset[str]:
    """The forms of every out-of-context noun or adjective reading of `word`."""
    try:
        found = _vabamorf().analyze(words=[word], disambiguate=False)
    except Exception:
        return frozenset()
    return frozenset(a["form"] for a in (found[0].get("analysis") or [])
                     if a["partofspeech"] in ("S", "A"))


def _check_abbreviation_hyphenation(text: str) -> dict:
    """Heuristic Estonian abbreviation-case-ending hyphenation checker.

    EKI (teatmik, "Lühendite ja tähiste käänamine") lets an all-capital
    abbreviation take its case ending with or without a hyphen: EL-i and
    ELi, OÜ-sid and OÜsid, AS-idel and ASidel are all correct. The one
    place it prefers the hyphen is an ending that starts with l, which
    reads into the capitals: THI-l (mitte THIl), and its MP3 table gives
    only MP3-le, MP3-l, MP3-lt. Only that case is flagged. Vabamorf
    puts a written stem vowel into the ending (ELile: "ile"), so
    ASile ~ AS-ile, which EKI accepts both ways, never starts with l.

    The suggestion follows EKI (teatmik, "Lühendamine"; "Arvukirjutus"):
    a stem read with a final consonant takes the stem vowel i (ASle ->
    AS-ile, as in EL-i), a digit keeps the bare ending (MP3-le), and a
    Roman ordinal before a noun or adjective in the same case takes no
    ending at all (XIIl sajandil -> XII sajandil); with no such word after
    it, it keeps one (XII-le). Chloride formulas (HCl, NH4Cl) are not
    abbreviations and are skipped.
    """
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["morph_analysis"])

    spans = list(t.morph_analysis)
    looked_up: dict[str, frozenset[str]] = {}

    def shows_case(k: int, form: str) -> bool:
        """spans[k] reads as a noun or adjective in `form`."""
        if k >= len(spans):
            return False
        nxt = spans[k]
        if any(a.partofspeech in ("S", "A") and a.form == form
               for a in nxt.annotations):
            return True
        if nxt.text not in looked_up:
            if len(looked_up) >= _ABBR_LOOKUPS:
                return False
            looked_up[nxt.text] = _abbr_noun_forms(nxt.text)
        return form in looked_up[nxt.text]

    issues: list[dict] = []
    linked = roman = False
    for k, span in enumerate(spans):
        word = span.text
        if not word or "-" in word:
            continue  # already hyphenated or empty
        pos = _first(list(span.partofspeech))
        if pos != "Y":
            continue  # not an abbreviation per Vabamorf
        form = _first(list(span.form)) or ""
        if form in ("", "?", "sg n", "pl n"):
            continue  # no case ending to hyphenate
        ending = _first(list(span.ending)) or ""
        if not ending or ending == "0":
            continue
        if not word.endswith(ending):
            continue
        stem = word[: -len(ending)]
        # Stem must look like an abbreviation (all-uppercase). This
        # filters Estonian noun lemmas like "tuba" / "mati" that
        # might also have a case ending but aren't abbreviations.
        if not stem or not stem.isupper():
            continue
        if not ending.startswith("l"):
            continue  # ELi, APIga, MCPst: EKI accepts these without a hyphen
        if ending == "l" and _CHLORIDE_STEM.fullmatch(stem) and (
                stem in _CHLORIDE_PLAIN_STEMS or any(c.isdigit() for c in stem)):
            continue  # HCl, KCl, NH4Cl, CH3Cl: the l belongs to Cl
        if _ROMAN_IVX.fullmatch(stem):
            if shows_case(k + 1, form):
                suggestion = stem  # XII sajandil: the noun shows the case
                roman = True
            else:
                suggestion = f"{stem}-{ending}"  # XII-le: no word shows the case
        elif stem[-1] in _CONSONANT_NAMED:
            suggestion = f"{stem}-i{ending}"  # AS-ile, EL-ilt, SMS-il
            linked = True
        else:
            suggestion = f"{stem}-{ending}"  # OÜ-le, THI-l, MP3-le
        issues.append({
            "word": word,
            "lemma": _first(list(span.lemma)) or "",
            "form": form,
            "position": span.start,
            "rule": "abbreviation-case-ending-hyphen",
            "rule_estonian": "lühendi käändelõpu sidekriips",
            "suggestion": suggestion,
        })

    if not issues:
        return {"issues": issues,
                "summary_estonian": "Lühendiortograafia probleeme ei leitud."}
    # One takes the nominative (1 käändelõpp), two or more the partitive.
    summary = ("Leiti 1 l-alguline käändelõpp" if len(issues) == 1
               else f"Leiti {len(issues)} l-algulist käändelõppu")
    summary += ", mille EKI soovitab lühendist sidekriipsuga eraldada."
    if linked:
        summary += (" Kui lühend lõpeb hääldatuna kaashäälikuga, tuleb lõpu "
                    "ette ka tüvevokaal i (AS-ile, EL-ilt).")
    if roman:
        summary += (" Kui rooma numbriga järgarvu kääne selgub järgnevast "
                    "sõnast, jääb number hoopis käändelõputa (XII sajandil).")
    return {"issues": issues, "summary_estonian": summary}


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian abbreviation case-ending hyphenation",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_abbreviation_hyphenation(text: Annotated[str, Field(description="Estonian text.")]) -> _CheckResult:
    """Flags an ending that starts with l written straight onto an
    all-capital abbreviation: OÜle → OÜ-le, ÜROlt → ÜRO-lt, THIl → THI-l.
    EKI finds the hyphen clearer there. Every other hyphenless form (ELi,
    ELis, APIga, MCPst, ASile) is as correct as the hyphenated one and is
    not flagged: keep the text's own choice.

    A stem read with a final consonant also needs the stem vowel i
    (ASle → AS-ile, ELlt → EL-ilt); a Roman ordinal up to XXXIX before a
    noun or adjective in the same case drops the ending (XIIl sajandil →
    XII).

    Any all-capital stem counts, known or not; mixed-case ones (SaaS, PhD,
    iOS) and chloride formulas (HCl, NH4Cl) are not checked.
    """
    return _check_abbreviation_hyphenation(text)


# Out-of-context lookups one check_object_case call may make. A lookup can
# take 1 to 2 ms, and a word between teavitama and an allative is looked up
# only when every other guard has passed, so real text needs a handful;
# past the budget a word counts as a possible object and is not flagged.
_TEAV_LOOKUPS = 512


@lru_cache(maxsize=4096)
def _teav_may_be_partitive(word: str) -> bool:
    """Some out-of-context reading of `word` is a partitive noun, name,
    pronoun or abbreviation. Disambiguation keeps one reading of a
    genitive/partitive homograph and often the genitive (Teavitage oma
    arsti: arsti comes out sg g; also kooli, Komisjoni), so a word that may
    be the object of teavitama is looked up here instead."""
    try:
        found = _vabamorf().analyze(words=[word], disambiguate=False)
    except Exception:
        return False
    return any(a["form"] in ("sg p", "pl p") and a["partofspeech"] in ("S", "H", "P", "Y")
               and a["lemma"].lower() not in _TEAV_TIME_MEASURES_ET
               for a in (found[0].get("analysis") or []))


@lru_cache(maxsize=4096)
def _teav_synth(lemma: str, form: str, pos: str) -> str:
    """The first form Vabamorf synthesises, or "" when it has none."""
    return (_synthesize(lemma, form, pos) or [""])[0]


@lru_cache(maxsize=4096)
def _teav_synth_like(lemma: str, form: str, pos: str, word: str, target: str,
                     head: str = "") -> str:
    """The first `target` form of the paradigm whose `form` is `word`. A
    lemma with two paradigms (seade: seadme 'device' and seade 'setting';
    kool: kooli and koola) is synthesised under the sg g hint that gives
    back the allative in the text, the most specific one when several do
    (the hint seade also yields seadmetele). Vabamorf reads a compound's
    hint as its last part, `head` (lepingupool under poole, not
    lepingupoole). A target with one form, or a lemma with one paradigm,
    gets the first form, as _teav_synth gives it."""
    forms = _synthesize(lemma, target, pos)
    keys = _synthesize(lemma, "sg g", pos) if len(forms) > 1 else []
    if len(keys) > 1:
        cut = len(lemma) - len(head) if head and lemma.lower().endswith(head) else 0
        own = [(len(got), key[cut:]) for key in keys
               if word in (got := [g.lower() for g in _synthesize(lemma, form, pos, key[cut:])])]
        if own:
            return (_synthesize(lemma, target, pos, min(own)[1]) or [""])[0]
    return (forms or [""])[0]


def _teav_governor_head(span) -> str:
    """The last compound part of `span` (lisa_toetus -> toetus), or "" for a
    simplex word, a head in _TEAV_NON_INHERITING_HEADS_ET or a compound that
    names a body (kiirabi is no kind of abi here)."""
    roots = list(span.root)
    parts = (roots[0] if roots else span.text).lower().replace("=", "").split("_")
    if (len(parts) < 2 or parts[-1] in _TEAV_NON_INHERITING_HEADS_ET
            or "".join(parts) in _TEAV_RECIPIENT_HEADS_ET):
        return ""
    return parts[-1]


def _teav_is_allative(reading: tuple) -> bool:
    return reading[2] in ("sg all", "pl all")


def _teav_is_genitive(reading: tuple) -> bool:
    return reading[2] in ("sg g", "pl g") and reading[1] in ("S", "P", "H", "A", "N", "O", "Y")


def _teav_has_partitive(readings: list) -> bool:
    if readings and readings[0][1] == "S" and readings[0][0].lower() in _TEAV_TIME_MEASURES_ET:
        return False
    return any(f in ("sg p", "pl p") and p in ("S", "P", "H", "Y") for _, p, f in readings)


def _teav_participle_like(reading: tuple) -> bool:
    lemma, pos, form = reading
    return ((pos == "A" and lemma.lower().endswith(("tud", "dud", "nud", "v")))
            or (pos == "V" and form in _TEAV_NONFINITE_FORMS))


def _teav_ditransitive_action(reading: tuple) -> bool:
    """A -mine noun of a verb with an allative recipient (andmine,
    edastamine). The lemma is enough: a root ending in =mine always gives a
    lemma ending in mine."""
    lemma, pos, _ = reading
    lemma = lemma.lower()
    if pos != "S" or not lemma.endswith("mine"):
        return False
    stem = lemma[:-4] + "ma"
    return (stem in _TEAV_DITRANSITIVE_VERBS_ET
            or stem.split("_")[-1] in _TEAV_DITRANSITIVE_VERBS_ET
            or stem.endswith(_TEAV_DITRANSITIVE_ENDINGS))


def _teav_match_case(word: str, got: str) -> str:
    """`got` in the letter case of `word`: Teile -> Teid, POLITSEILE ->
    POLITSEID."""
    if len(word) > 1 and word.isupper():
        return got.upper()
    if word[:1].isupper():
        return got[:1].upper() + got[1:]
    return got


def _teav_inflect(span, reading: tuple, case: str) -> str:
    """`span`, an allative, in the partitive (case "p") or genitive ("g") of
    the same number; "" when Vabamorf has no such form."""
    word = span.text
    table = _TEAV_PARTITIVE_OF_ALLATIVE if case == "p" else _TEAV_GENITIVE_OF_ALLATIVE
    got = table.get(word.lower())
    if got is None:
        lemma, pos, form = reading
        roots = list(span.root)
        head = roots[0].lower().replace("=", "").split("_")[-1] if roots else ""
        got = _teav_synth_like(lemma, form, pos, word.lower(), f"{form.split()[0]} {case}", head)
    return _teav_match_case(word, got)


def _teavitama_recipient_issues(sent_spans: list, text: str,
                                lookups: dict[str, bool] | None = None) -> list[dict]:
    """The two government checks of check_object_case for one sentence.
    `lookups` holds the call's out-of-context lookups (_TEAV_LOOKUPS), so
    pass the same dict for every sentence of one text.

    `teavitama-allative-recipient`: teavitama or informeerima (finite, or a
    participle after olema/ei/pole), or the noun teavitamine/informeerimine
    in the nominative, followed in its clause by an allative person or body
    (teavitas politseile, teavitamine Rahapesu Andmebüroole). The guards, in
    the order the research measured them:
    1. a clause ends at punctuation, a clause conjunction, a relative
       pronoun not in the allative, or a relative adverb (but not at the ja
       of Maksu- ja Tolliamet);
    2. a partitive anywhere else in the clause is the object already;
    3. the allative comes within 6 tokens of a verb (3 of a noun, with only
       genitives between) and before any other verb;
    4. a word between the trigger and the allative that may be a partitive
       out of context is the object (Teavitab Euroopa Komisjoni ...), except
       a capitalised genitive before a capitalised head (a name chain);
    5. a governor right before the allative phrase owns it: vastavalt,
       lisaks, a postposition, an elative noun with an allative attribute
       (muudatuse mõjust klientidele, lisatoetusest peredele) or the -mine
       noun of a verb with a recipient of its own;
    6. a participle, an adjective or a postposition right after the phrase
       owns it (klientidele mõeldud, kõigile olulisest, kõigile
       ühtsetest), and so does a participle, a ditransitive -mine noun or
       (after a verb) a finite verb later in the clause (Rahapesu
       Andmebüroole esitatud teatest); an adverb or translative noun right
       after it makes the allative its experiencer (meile teadaolevalt,
       kõigile üllatuseks), kandideerimine or asumine after a bare amet
       makes amet a post, and a line break after the noun ends a heading
       (Teavitamine alone on its line, or before a sentence whose own verb
       has the recipient: Klientidele saadetakse kiri);
    7. the head is a person or a body: a pronoun, a word whose last
       compound part is in _TEAV_RECIPIENT_HEADS_ET, or an agent noun in -ja.
       A noun head decides alone, so kõigile, teisele and igale count only
       with no noun after them (Teavitame kolimisest teisele korrusele is
       clean).

    `teatama-partitive-person`: teatama right before a partitive personal
    pronoun that ends the clause or comes before et or an elative noun
    (Teatame Teid, et ...). In Teatame Teid puudutavatest muudatustest the
    pronoun is not teatama's object but the participle's, so it is not
    flagged, though Teatame Teile ... reads better. A participle within 8
    tokens, past commas, adverbial elatives and a coordinated object, that
    is elative itself or comes before the elative noun it describes, counts
    (Teatame meid südamest rõõmustanud uudisest; Teatame Teid, Teie
    perekonda ja lähedasi puudutavast otsusest).

    Each check reads a window of a few tokens or a table built once per
    sentence, and a sentence without teavit-, informeeri- or teata- is not
    read at all, so the pass is linear in the text length.
    """
    toks = sent_spans
    n = len(toks)
    if not n or not _TEAV_HINT.search(text, toks[0].start, toks[-1].end):
        return []
    readings = [list(zip(s.lemma, s.partofspeech, s.form, strict=True)) for s in toks]
    first = [r[0] if r else ("", "", "") for r in readings]
    punct = [all(p == "Z" for _, p, _ in r) and not any(c.isalnum() for c in s.text)
             for s, r in zip(toks, readings, strict=True)]

    def boundary(k: int) -> bool:
        if punct[k]:
            return True
        lemma, pos, form = first[k]
        lemma = lemma.lower()
        if not ((pos == "P" and lemma in _TEAV_RELATIVE_PRONOUNS_ET
                 and form not in ("sg all", "pl all"))
                or (pos == "D" and lemma in _TEAV_RELATIVE_ADVERBS_ET)
                or (pos == "J" and lemma in _TEAV_CLAUSE_CONJUNCTIONS_ET)):
            return False
        return not (lemma in ("ja", "ning", "või") and k > 0 and toks[k - 1].text.endswith("-"))

    # Per-token tables, each one pass over the sentence: the clause a token
    # is in ([start, end)), a running count of partitives, and the next
    # token at or after k that owns a later allative or starts a clause.
    is_boundary = [boundary(k) for k in range(n)]
    clause_start, clause_end = [0] * n, [n] * n
    s = 0
    for k in range(n):
        clause_start[k] = s
        if is_boundary[k]:
            s = k + 1
    e = n
    for k in range(n - 1, -1, -1):
        if is_boundary[k]:
            e = k
        clause_end[k] = e
    partitive = [_teav_has_partitive(r) for r in readings]
    partitives_before = [0] * (n + 1)
    for k in range(n):
        partitives_before[k + 1] = partitives_before[k] + partitive[k]
    next_owner, next_finite = [n] * (n + 1), [n] * (n + 1)
    next_ditransitive = [n] * (n + 1)
    for k in range(n - 1, -1, -1):
        lemma, pos, form = first[k]
        next_owner[k] = (k if _teav_participle_like(first[k]) or _teav_ditransitive_action(first[k])
                         else next_owner[k + 1])
        finite = pos == "V" and (form in _TEAV_FINITE_FORMS or lemma in ("ei", "ära"))
        next_finite[k] = k if finite else next_finite[k + 1]
        next_ditransitive[k] = (k if finite and (
            lemma.lower() in _TEAV_DITRANSITIVE_VERBS_ET
            or lemma.lower().split("_")[-1] in _TEAV_DITRANSITIVE_VERBS_ET)
            else next_ditransitive[k + 1])

    def owns_allative_after(k: int) -> bool:
        lemma, pos, form = first[k]
        lemma = lemma.lower()
        if pos == "K":
            return True
        if pos == "V":
            return form not in _TEAV_NEXT_FINITE_FORMS
        if pos in ("C", "U"):
            return True
        if pos == "A":
            # An elative adjective outside the lists does not block:
            # kõigile uutest tingimustest is still flagged.
            return (lemma in _TEAV_ALLATIVE_ADJECTIVES_ET
                    or _teav_governor_head(toks[k]) in _TEAV_ALLATIVE_ADJECTIVES_ET
                    or lemma.endswith(("tud", "dud", "nud", "v"))
                    or form not in ("sg el", "pl el", "sg all", "pl all"))
        return any(p == "V" and f in ("tud", "nud", "v", "tav", "mata") for _, p, f in readings[k])

    def experiencer_after(k: int, end: int) -> bool:
        # meile teadaolevalt, kõigile üllatuseks, meile kõigile rõõmuks:
        # past up to two agreeing allatives, an adverb or a translative
        # noun whose experiencer the allative is.
        m = k + 1
        while m < min(end, k + 3) and _teav_is_allative(first[m]):
            m += 1
        if m >= end:
            return False
        lemma, pos, form = first[m]
        lemma = lemma.lower()
        return ((pos == "D" and lemma in _TEAV_EXPERIENCER_ADVERBS_ET)
                or (pos == "S" and form == "sg tr" and lemma in _TEAV_EXPERIENCER_TRANSLATIVES_ET))

    def heading(k: int) -> bool:
        # Token k alone on its line but for markup (## Teavitamine,
        # 1. Teavitamine), and the next line opens with a capital: a
        # heading, not a hard-wrapped sentence.
        if (k + 1 >= n or "\n" not in text[toks[k].end:toks[k + 1].start]
                or not toks[k + 1].text[:1].isupper()):
            return False
        low = max(0, toks[k].start - 12)
        cut = text.rfind("\n", low, toks[k].start)
        if cut < 0 and low > 0:
            return False
        return not any(c.isalpha() for c in text[cut + 1:toks[k].start])

    def post_goal(k: int, end: int) -> bool:
        # A bare amet before the -mine noun of kandideerima or asuma is a
        # post: kantsleri ametile kandideerimise tingimustest.
        if k + 1 >= end or [r.lower() for r in toks[k].root][:1] != ["amet"]:
            return False
        lemma, pos, _ = first[k + 1]
        lemma = lemma.lower()
        return (pos == "S" and lemma.endswith("mine")
                and lemma[:-4] + "ma" in _TEAV_POST_GOAL_VERBS_ET)

    def active_participle(k: int) -> bool:
        # Teid puudutav, meid rõõmustanud, teid kontorisse viiva: an active
        # participle takes the pronoun as its object (a passive -tud or
        # -tav one cannot). Vabamorf tags some as nouns (ümbritsev, S);
        # direktiiv, motiiv and arhiiv are nouns.
        for lemma, pos, form in readings[k]:
            lemma = lemma.lower()
            if pos == "V" and form in ("v", "nud"):
                return True
            if pos == "A" and lemma.endswith(("nud", "v")):
                return True
            if (pos == "S" and lemma.endswith("v") and len(lemma) >= 6
                    and not lemma.endswith(("tiiv", "siiv", "hiiv"))
                    and not any("_" in r for r in toks[k].root)):
                return True
        return False

    def elative(k: int) -> bool:
        return any(f in ("sg el", "pl el") for _, _, f in readings[k])

    def governed_by_participle(k: int) -> bool:
        # Teatame Teid ümbritsevatest ohtudest, meid südamest rõõmustanud
        # uudisest: an active participle within 8 tokens, past commas,
        # adverbial elatives and a coordinated object (Teid, Teie perekonda
        # ja lähedasi puudutavast), that is elative itself or comes before
        # the elative noun it describes. Other punctuation, a finite verb,
        # or a conjunction or relative word that opens a clause (Teatame
        # Teid, et ...) ends the search; so does a participle that
        # describes something else (teid muudatustest kehtivas hinnakirjas).
        for x in range(k, min(n, k + 8)):
            if punct[x]:
                if toks[x].text == ",":
                    continue
                return False
            lemma, pos, form = first[x]
            if is_boundary[x] and lemma.lower() not in ("ja", "ning", "või"):
                return False
            if pos == "V" and (form in _TEAV_FINITE_FORMS or lemma in ("ei", "ära")):
                return False
            if active_participle(x):
                y = x + 1
                while y < min(n, x + 3) and _teav_is_genitive(first[y]):
                    y += 1
                return elative(x) or (y < n and first[y][1] == "S" and elative(y))
        return False

    def recipient(k: int) -> bool:
        lemma, pos, _ = first[k]
        lemma = lemma.lower()
        if pos == "P":
            return lemma in _TEAV_RECIPIENT_PRONOUNS_ET
        if pos not in ("S", "H", "Y"):
            return False
        roots = list(toks[k].root)
        head = (roots[0] if roots else toks[k].text).lower().split("_")[-1]
        plain = head.replace("=", "")
        return (plain in _TEAV_RECIPIENT_HEADS_ET or lemma in _TEAV_RECIPIENT_HEADS_ET
                or head.endswith("=ja")
                or (plain.endswith("ja") and len(plain) >= 5 and plain not in _TEAV_NOT_AGENT_JA_ET))

    def phrase_head(reading: tuple) -> bool:
        # An allative noun, or an allative pronoun that stands for one.
        return _teav_is_allative(reading) and (
            reading[1] in ("S", "H", "Y")
            or (reading[1] == "P" and reading[0].lower() in _TEAV_HEAD_PRONOUNS_ET))

    def genitive_or_name(k: int) -> bool:
        return _teav_is_genitive(first[k]) or first[k][1] in ("Y", "H")

    looked_up = {} if lookups is None else lookups

    def may_be_partitive(word: str) -> bool:
        if word not in looked_up:
            if len(looked_up) >= _TEAV_LOOKUPS:
                return True
            looked_up[word] = _teav_may_be_partitive(word)
        return looked_up[word]

    issues: list[dict] = []
    flagged: set[int] = set()
    for i in range(n):
        l0, p0, f0 = first[i]
        low0 = l0.lower()
        if (p0 == "V" and low0 == "teatama" and i + 1 < n
                and f0 not in ("tud", "nud", "mata", "v", "tav", "des")):
            lx, px, fx = first[i + 1]
            if px == "P" and lx.lower() in _TEAV_PERSONAL_PRONOUNS_ET and fx in ("sg p", "pl p"):
                after = first[i + 2] if i + 2 < n else None
                if ((after is None or punct[i + 2] or after[0].lower() == "et"
                        or (after[1] == "S" and after[2] in ("sg el", "pl el")))
                        and not governed_by_participle(i + 2)):
                    word = toks[i + 1].text
                    allative = _teav_match_case(word, _TEAV_ALLATIVE_OF_PARTITIVE.get(
                        word.lower()) or _teav_synth(lx, f"{fx.split()[0]} all", px))
                    issues.append({
                        "word": word,
                        "lemma": lx,
                        "position": toks[i + 1].start,
                        "form": fx,
                        "verb": "teatama",
                        "rule": "teatama-partitive-person",
                        "rule_estonian": "teatama: kellele, mitte keda",
                        "suggestion": (f"{allative} (teatama) / teavitama + {word}" if allative
                                       else f"teavitama + {word}"),
                    })
            continue

        verb = next((lemma.lower() for lemma, pos, _ in readings[i]
                     if pos == "V" and lemma.lower() in _TEAV_VERBS_ET), None)
        is_verb = p0 in ("V", "A") and verb is not None
        is_noun = p0 == "S" and low0 in _TEAV_NOUNS_ET and f0 == "sg n"
        if not (is_verb or is_noun):
            continue
        start, end = clause_start[i], clause_end[i]
        if is_verb:
            if f0 == "mata" or (p0 == "V" and f0 in ("v", "tav")):
                continue
            # A participle counts only in a verb phrase (on teavitanud, ei
            # teavitanud), not as an attribute (teavitatud kliendile).
            if (p0 == "A" or f0 in ("tud", "nud")) and not any(
                    first[x][0] in ("olema", "ei", "pole") for x in range(max(start, i - 4), i)):
                continue
        elif i > 0 and _teav_is_genitive(first[i - 1]):
            continue   # kliendi teavitamine: the object slot is filled
        if partitives_before[end] - partitives_before[start] - partitive[i] > 0:
            continue

        found = None
        for j in range(i + 1, min(end, i + (6 if is_verb else 3) + 1)):
            if first[j][1] == "V":
                break
            if _teav_is_allative(first[j]):
                found = j
                break
        if found is None:
            continue
        if is_noun and not all(_teav_is_genitive(first[x]) for x in range(i + 1, found)):
            continue
        # The phrase runs through agreeing allatives and genitive modifiers
        # to its head: kõigile meie klientidele.
        k = found
        while k + 1 < end and not phrase_head(first[k]):
            if _teav_is_allative(first[k + 1]) or (
                    _teav_is_genitive(first[k + 1]) and k + 2 < end
                    and (_teav_is_allative(first[k + 2])
                         or (_teav_is_genitive(first[k + 2]) and k + 3 < end
                             and _teav_is_allative(first[k + 3])))):
                k += 1
                continue
            break

        phrase_start = found
        while phrase_start - 1 > i and genitive_or_name(phrase_start - 1):
            phrase_start -= 1
        lp, pp, fp = first[phrase_start - 1]
        if (lp.lower() in _TEAV_ALLATIVE_GOVERNORS_ET or pp == "K"
                or toks[phrase_start - 1].text.lower() in _TEAV_ALLATIVE_GOVERNORS_ET):
            continue
        if pp == "S" and fp in ("sg el", "pl el") and (
                lp.lower() in _TEAV_ALLATIVE_ATTR_NOUNS_ET
                or _teav_governor_head(toks[phrase_start - 1]) in _TEAV_ALLATIVE_ATTR_NOUNS_ET
                or _teav_ditransitive_action(first[phrase_start - 1])):
            continue
        if k + 1 < n and not punct[k + 1] and owns_allative_after(k + 1):
            continue
        if experiencer_after(k, end):
            continue
        if next_owner[k + 1] < end or (is_verb and next_finite[k + 1] < end):
            continue
        # A line break after the noun: a heading (Teavitamine alone on its
        # line), or a sentence whose own verb has the recipient follows
        # (Teavitamine\nKlientidele saadetakse kiri).
        if is_noun and "\n" in text[toks[i].end:toks[found].start] and (
                heading(i) or next_ditransitive[k + 1] < end):
            continue
        if post_goal(k, end):
            continue
        # The noun head decides; a determiner (kõigile, teisele, igale)
        # counts only when no noun follows it: Teavitame kontori kolimisest
        # teisele korrusele is no recipient.
        if not (recipient(k) or (first[k][1] not in ("S", "H", "Y") and recipient(found))):
            continue
        name_chain = toks[k].text[:1].isupper()
        if any(not (name_chain and toks[x].text[:1].isupper() and _teav_is_genitive(first[x]))
               and not toks[x].text.endswith("-") and first[x][1] in ("S", "H", "Y")
               and may_be_partitive(toks[x].text)
               for x in range(i + 1, found)):
            continue

        # Report the whole phrase, genitive modifiers and a Maksu- ja
        # coordination included.
        shown = phrase_start
        while (shown - 2 > i and first[shown - 1][0].lower() in ("ja", "ning", "või")
               and toks[shown - 2].text.endswith("-")):
            shown -= 2
            while shown - 1 > i and genitive_or_name(shown - 1):
                shown -= 1
        if toks[shown].start in flagged:
            continue
        flagged.add(toks[shown].start)
        word = text[toks[shown].start:toks[k].end]
        pieces, cursor, complete = [], toks[shown].start, True
        for x in range(shown, k + 1):
            pieces.append(text[cursor:toks[x].start])
            if x >= found and _teav_is_allative(first[x]):
                form = _teav_inflect(toks[x], first[x], "p" if is_verb else "g")
                complete = complete and bool(form)
                pieces.append(form)
            else:
                pieces.append(toks[x].text)
            cursor = toks[x].end
        rewritten = "".join(pieces)
        if is_verb:
            suggestion = (f"{rewritten} ({verb}) / teatama + {word}" if complete
                          else f"teatama + {word}")
        else:
            suggestion = (f"{rewritten} {low0} / teatamine + {word}" if complete
                          else f"teatamine + {word}")
        issues.append({
            "word": word,
            "lemma": first[k][0],
            "position": toks[shown].start,
            "form": first[k][2],
            "verb": verb if is_verb else low0,
            "rule": "teavitama-allative-recipient",
            "rule_estonian": "teavitama: keda, mitte kellele",
            "suggestion": suggestion,
        })
    return issues


def _check_object_case(text: str) -> dict:
    """Heuristic Estonian object-case-government checker.

    Two government rules run on every sentence first (see
    _teavitama_recipient_issues): teavitama with an allative recipient, and
    teatama with a partitive personal pronoun. Then two object-case rules:
    1. Negation triggers partitive: any noun in nominative or
       genitive in a sentence containing 'ei'/'pole'/'ära'/etc. is a
       likely error.
    2. Partitive-only verbs: a curated set of verbs always take
       partitive direct objects; any noun in nominative or genitive
       in the same sentence is suspicious.

    Without a parser we can't tell subjects from objects, so we flag
    on syntactic candidates and let the caller decide.
    """
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["sentences", "morph_analysis"])

    spans = list(t.morph_analysis)
    issues: list[dict] = []
    # Words do not overlap, so starts and ends both ascend and the words
    # inside a sentence are one slice. Filtering every word per sentence
    # took 20 s on 100,000 characters.
    starts = [s.start for s in spans]
    ends = [s.end for s in spans]
    lookups: dict[str, bool] = {}

    for sentence in t.sentences:
        sent_spans = spans[bisect.bisect_left(starts, sentence.start):
                           bisect.bisect_right(ends, sentence.end)]
        if not sent_spans:
            continue

        issues.extend(_teavitama_recipient_issues(sent_spans, text, lookups))

        # Detect negation + partitive-only verb governance, and remember
        # the position of whichever fires first so we only flag nouns
        # AFTER it. Estonian SVO/SOV word order puts subjects before the
        # verb/negation, so this cheaply skips the subject-noun FPs we'd
        # otherwise generate.
        has_negation = False
        has_olema = False
        partitive_verb: str | None = None
        trigger_index = -1
        for idx, span in enumerate(sent_spans):
            lemma = (_first(list(span.lemma)) or "").lower()
            if lemma in _NEGATION_LEMMAS_ET:
                has_negation = True
                if trigger_index == -1:
                    trigger_index = idx
            if lemma == "olema":
                has_olema = True
            if lemma in _PARTITIVE_ONLY_VERBS_ET:
                partitive_verb = lemma
                if trigger_index == -1:
                    trigger_index = idx

        # Predicative after a negated copula stays NOMINATIVE, not
        # partitive ("see EI OLE raamat", not *raamatut), so a negated
        # `olema` clause is not an object-case context. We can't reliably
        # tell the copula use ("X ei ole Y") from the existential/
        # possessive use ("mul ei ole raamatut", which IS partitive)
        # without parsing, so we suppress the negation rule whenever
        # olema is the negated verb. Trade-off: avoids the common
        # false positive on predicatives at the cost of missing the
        # narrower existential-object case. The partitive-only-verb rule
        # is unaffected.
        if has_negation and has_olema:
            has_negation = False

        if not (has_negation or partitive_verb):
            continue

        for idx, span in enumerate(sent_spans):
            if idx <= trigger_index:
                continue
            word = span.text
            if not word or not word[0].isalpha():
                continue
            pos = _first(list(span.partofspeech))
            if pos != "S":   # phase 1: nouns only; adjectives generate too much FP
                continue
            form = _first(list(span.form)) or ""
            lemma = _first(list(span.lemma)) or ""

            # Skip proper nouns (likely place/person names, not common-noun
            # direct objects).
            if lemma and lemma[0].isupper():
                continue
            # Skip if already partitive (correct) or clearly non-object case.
            if form in _NON_OBJECT_CASES:
                continue
            if " p" in form:   # partitive ('sg p', 'pl p')
                continue
            if form not in _DIRECT_OBJECT_CASES:
                continue

            if has_negation:
                issues.append({
                    "word": word,
                    "lemma": lemma,
                    "position": span.start,
                    "form": form,
                    "rule": "negation-requires-partitive",
                    "rule_estonian": "eitus nõuab osastavat",
                })
            elif partitive_verb:
                issues.append({
                    "word": word,
                    "lemma": lemma,
                    "position": span.start,
                    "form": form,
                    "verb": partitive_verb,
                    "rule": "partitive-only-verb",
                    "rule_estonian": "osastavat nõudev tegusõna",
                })

    return {
        "issues": issues,
        "summary_estonian": (
            f"Leiti {len(issues)} käändevigade kahtlust." if issues
            else "Käändevigade kahtlust ei leitud."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian object case (käändeõpetus)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_object_case(text: Annotated[str, Field(description="Estonian text.")]) -> _CheckResult:
    """Flags a direct object in the wrong case (käändeõpetus): a common noun
    in nominative or genitive (`form`) after
    - `negation-requires-partitive`: negation (ei söönud leib → leiba), or
    - `partitive-only-verb`: `verb`, one of nine verbs such as armastama,
      vajama and ootama that always take a partitive object.

    The negation rule skips any sentence containing olema (pole, ei ole,
    pole näinud). The fix is the partitive of `lemma` in the number `form`
    shows; take it from paradigm. With no parser, a hit may be a subject or
    an adverbial, so treat it as worth a second look.

    Two more rules check the government of teavitama (keda + millest) and
    teatama (kellele + millest), and add a `suggestion`:
    - `teavitama-allative-recipient`: teavitama or informeerima, or their
      noun teavitamine or informeerimine (`verb`), with the person or body
      in the allative (teavitas politseile; `word` is the allative phrase).
    - `teatama-partitive-person`: teatama with a partitive personal pronoun
      (Teatame Teid, et ...).

    The fix is either teavitama + partitive (teavitas politseid) or
    teatama + allative (teatas politseile); for the noun, Rahapesu
    Andmebüroo teavitamine or teatamine Rahapesu Andmebüroole. Recall is low by
    design: a clause that already has a partitive object (teavitab
    õpilastele hindeid), an allative before the verb, an allative that a
    participle, adjective or action noun governs (teavitada isikut talle
    seatud piirangust), one whose head noun is not a person or body
    (kolimisest teisele korrusele), the experiencer of an adverb or a
    translative (meile teadaolevalt, kõigile üllatuseks) and an allative
    after a heading line are not flagged, and neither is a partitive
    pronoun that a following participle governs (Teatame Teid
    puudutavatest muudatustest: grammatical, but Teatame Teile ... reads
    better).
    """
    return _check_object_case(text)


def _check_redundancy(text: str) -> dict:
    """Heuristic pleonasm / semantic-doubling checker. Flags phrasing
    that is grammatically valid but redundant to a native speaker,
    e.g. 'samuti ka' (also also), 'kõige optimaalsem' (most optimal)."""
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["morph_analysis"])
    spans = list(t.morph_analysis)

    issues: list[dict] = []

    for i, span in enumerate(spans):
        word = span.text
        lower = word.lower()
        lemma = (_first(list(span.lemma)) or "").lower()

        # 1. Adjacent "also" particles: samuti ka / ka samuti / ...
        if i + 1 < len(spans):
            nxt = spans[i + 1]
            nxt_lower = nxt.text.lower()
            if (
                lower in _ALSO_PARTICLES_ET
                and nxt_lower in _ALSO_PARTICLES_ET
                and lower != nxt_lower
            ):
                issues.append({
                    "phrase": f"{word} {nxt.text}",
                    "position": span.start,
                    "rule": "doubled-also",
                    "rule_estonian": "topeldatud rõhumäärsõna",
                    "explanation": (
                        f"'{lower}' ja '{nxt_lower}' tähendavad mõlemad "
                        f"'samuti / ka'. Koos on tautoloogia. Vali üks."
                    ),
                    "suggestion": f"jäta alles kas '{lower}' VÕI '{nxt_lower}', mitte mõlemad",
                })

        # 2. Double superlative: kõige + already-absolute adjective.
        # Stem-prefix match on the surface form so comparative
        # (optimaalsem) and superlative (optimaalseim) forms are caught,
        # not just the base lemma.
        if lower == "kõige" and i + 1 < len(spans):
            nxt = spans[i + 1]
            nxt_lower = nxt.text.lower()
            if any(nxt_lower.startswith(stem) for stem in _NON_GRADABLE_STEMS_ET):
                issues.append({
                    "phrase": f"{word} {nxt.text}",
                    "position": span.start,
                    "rule": "double-superlative",
                    "rule_estonian": "topeltülivõrre",
                    "explanation": (
                        f"'{nxt.text}' on juba absoluutne omadus, 'kõige' "
                        f"ette ei sobi (nagu inglise 'most optimal'). "
                        f"Piisab sõnast '{nxt.text}'."
                    ),
                    "suggestion": nxt.text,
                })

        # 3. Fixed pleonasm phrases (lemma-adjacent).
        if i + 1 < len(spans):
            nxt = spans[i + 1]
            nxt_lemma = (_first(list(nxt.lemma)) or "").lower()
            key = (lemma, nxt_lemma)
            if key in _PLEONASM_PHRASES_ET:
                issues.append({
                    "phrase": f"{word} {nxt.text}",
                    "position": span.start,
                    "rule": "fixed-pleonasm",
                    "rule_estonian": "liiasus (pleonasm)",
                    "explanation": _PLEONASM_PHRASES_ET[key],
                    "suggestion": f"sõnasta ümber: {_PLEONASM_PHRASES_ET[key]}",
                })

    return {
        "issues": issues,
        "summary_estonian": (
            f"Leiti {len(issues)} liiasuse (pleonasmi) kahtlust."
            if issues else "Liiasust ei tuvastatud."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian redundancy / pleonasm",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_redundancy(text: Annotated[str, Field(description="Estonian text.")]) -> _CheckResult:
    """Flags redundant doubling (liiasus, pleonasm):
    - `doubled-also`: two "also" particles together (samuti ka, ka samuti,
      ühtlasi ka); keep one.
    - `double-superlative`: kõige before an absolute adjective (kõige
      optimaalsem, "most optimal"); drop kõige.
    - `fixed-pleonasm`: a short curated list (ajaline periood, väike
      nüanss, üldine konsensus).

    Each issue's `explanation` gives the reason in Estonian. Conservative:
    it misses much redundancy a native speaker would hear.
    """
    return _check_redundancy(text)


def _check_legalese(text: str) -> dict:
    """Heuristic Estonian legalese-simplification aid. Flags archaic
    'kantseliit' filler with plain equivalents, flags over-long/over-nested
    sentences, and, crucially, lists the legal TERMS OF ART that must be
    preserved verbatim when simplifying (a general synonym would change the
    legal meaning)."""
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["sentences", "morph_analysis"])
    spans = list(t.morph_analysis)

    issues: list[dict] = []
    terms: list[dict] = []
    seen: set[str] = set()

    for i, span in enumerate(spans):
        surface = span.text
        low = surface.lower()
        lemma = (_first(list(span.lemma)) or "").lower()

        # Terms of art: preserve, never treat as filler.
        if _is_legal_term(surface):
            key = lemma or low
            if key not in seen:
                seen.add(key)
                terms.append({"word": surface, "lemma": lemma or low, "position": span.start})
            continue

        # Archaic filler (surface-prefix match so inflected forms are caught).
        for stem, (plain, why) in _LEGALESE_STEMS_ET.items():
            if low.startswith(stem):
                issues.append({
                    "word": surface,
                    "position": span.start,
                    "rule": "archaic-filler",
                    "rule_estonian": "kantseliitlik täitesõna",
                    "explanation": why,
                    "suggestion": plain,
                })
                break

        # Fixed legalese phrases (adjacent surface pair).
        if i + 1 < len(spans):
            pair = (low, spans[i + 1].text.lower())
            if pair in _LEGALESE_PHRASES_ET:
                plain, why = _LEGALESE_PHRASES_ET[pair]
                issues.append({
                    "phrase": f"{surface} {spans[i + 1].text}",
                    "position": span.start,
                    "rule": "legalese-phrase",
                    "rule_estonian": "kantseliitlik väljend",
                    "explanation": why,
                    "suggestion": plain,
                })

    # Sentence-level complexity: long or heavily-subordinated sentences that
    # can be split for readability without touching any legal term.
    for sent in t.sentences:
        s_text = sent.enclosing_text
        n_words = len(s_text.split())
        n_commas = s_text.count(",")
        if n_words >= 34 or n_commas >= 4:
            issues.append({
                "phrase": (s_text[:60] + "…") if len(s_text) > 60 else s_text,
                "position": sent.start,
                "rule": "complex-sentence",
                "rule_estonian": "liiga pikk/keeruline lause",
                "explanation": f"Lause on {n_words} sõna ja {n_commas} koma.",
                "suggestion": "jaga lause lühemateks osadeks",
            })

    return {
        "issues": issues,
        "terms_of_art": terms,
        "summary_estonian": (
            f"Leiti {len(issues)} lihtsustamiskohta; säilitada tuleb "
            f"{len(terms)} juriidilist terminit." if (issues or terms)
            else "Kantseliiti ega juriidilisi termineid ei tuvastatud."
        ),
    }


# Definition markers: '(edaspidi „Müüja“)', '(edaspidi ühiselt «Pooled»)',
# '(edaspidi "Ostja")', '(edaspidi nimetatud Leping)'. Quotes pair loosely:
# «…», „…“, „…”, “…”, ”…”, "…" and mixes of them; a doubled opener
# ('««Leping»') is a typo and is skipped. The term holds no double quote
# except a whole inner «…» ('„Programm «Kevad»“', the Estonian nesting),
# so it ends at the first closing quote and never swallows one.
# An unclosed opener ('(edaspidi «Müüja), ...') must not swallow the
# definitions after it, so the term also stops at an unbalanced
# parenthesis or the word 'edaspidi' and runs at most _DEF_TERM_MAX
# characters (an inner «…» or (…) counting as one); past those it is no
# definition at all.
# “, ” and " also close a quotation, so as openers they need a space
# before them or the term right after them: '„Teen seda edaspidi“, ütles
# ta ja lisas “aitäh”' defines nothing.
# Drafting puts 'edaspidi' right after '(' or ',' (see _introduces_term):
# the adverb in 'lubas edaspidi „kõik võlad tasuda“' defines nothing.
_DEF_QUOTES = "«»„“”\""
_DEF_TERM_MAX = 150
_DEF_CHAR = rf"[^{_DEF_QUOTES}()]"
_EDASPIDI_QUOTED_RE = re.compile(
    r"edaspidi(?:\s+ühiselt)?(?:\s+nimetatud)?"
    r"(?:\s*[«„]+?|\s+[“”\"]|[“”\"](?=\w))"
    # No \s* around the term: the caller strips it, and a \s* next to a
    # lazy term makes an unclosed quote before a long blank run quadratic.
    rf"((?:(?!edaspidi(?!\w)){_DEF_CHAR}|«{_DEF_CHAR}+»|\({_DEF_CHAR}*\))"
    rf"{{1,{_DEF_TERM_MAX}}}?)[»“”\"]",
    re.IGNORECASE,
)
_EDASPIDI_BARE_RE = re.compile(
    r"edaspidi(?:\s+ühiselt)?(?:\s+nimetatud)?\s+([A-ZÄÖÜÕŠŽ][\wäöüõšžÄÖÜÕŠŽ-]{1,40})",
)
_XREF_RE = re.compile(
    r"(§\s*\d+(?:\s*lg\s*\d+)?|"
    r"(?:lõige|lõiget|lõikes|lõike|punkt|punkti|punktis|artikkel|artikli)\s+\d+)",
    re.IGNORECASE,
)


# Inner quotes around the whole term are double quoting, not part of the
# name: „«Leping»“ and "'Leping'" define Leping. An apostrophe that ends
# the name (Jones') or quotes a part of it (Kohvik 'Kodu') stays.
_DEF_INNER_PAIRS = (("«", "»"), ("'", "'"), ("‘", "’"))


def _unwrap_defined_term(term: str) -> str:
    term = term.strip()
    for o, c in _DEF_INNER_PAIRS:
        if len(term) > 2 and term[0] == o and term[-1] == c and o not in term[1:-1]:
            return term[1:-1].strip()
    return term


def _introduces_term(text: str, at: int) -> bool:
    """Whether the 'edaspidi' at `at` follows '(' or ',', blanks allowed:
    '(edaspidi „Leping“)', ', edaspidi „Leping“,'."""
    return text[max(0, at - 20):at].rstrip().endswith(("(", ","))


# Where a term can start: a whole word, or one character that is neither a
# word character nor a blank (a term can begin with «).
_TERM_START_RE = re.compile(r"(?<!\w)(?:\w+|[^\w\s])")
# Where a term can end: no word character follows.
_TERM_END_RE = re.compile(r"(?!\w)")


def _count_terms(terms: list[str], text: str) -> dict[str, int]:
    """Case-sensitive whole-token occurrences of each term, in one pass.

    Each place a term can start tries only the terms that start the same
    way, longest first, and the shorter terms that match begins with occur
    there too ('Teenuse' inside 'Teenuse osutaja'). Scanning the text once
    per term took seconds for a document with thousands of terms.
    """
    by_start: dict[str, list[str]] = {}
    for t in terms:
        by_start.setdefault(_TERM_START_RE.match(t).group(), []).append(t)
    # Terms that start alike are tried in one regex; a regex for a term
    # that starts alone would cost more to compile than it saves.
    alike = {s: re.compile("(?:" + "|".join(map(re.escape, sorted(ts, key=len, reverse=True)))
                           + r")(?!\w)")
             for s, ts in by_start.items() if len(ts) > 1}
    longest = dict.fromkeys(terms, 0)
    for m in _TERM_START_RE.finditer(text):
        start, at = m.group(), m.start()
        if start in alike:
            hit = alike[start].match(text, at)
            if hit:
                longest[hit.group()] += 1
        elif start in by_start:
            t = by_start[start][0]
            if text.startswith(t, at) and _TERM_END_RE.match(text, at + len(t)):
                longest[t] += 1
    counts = dict.fromkeys(terms, 0)
    for t, n in longest.items():
        if n:
            counts[t] += n
            for k in range(1, len(t)):
                if _TERM_END_RE.match(t, k) and t[:k] in counts:
                    counts[t[:k]] += n
    return counts


def _check_defined_terms(text: str) -> dict:
    """Structural aid for LONG legal documents: extract the terms defined
    with '(edaspidi «X»)', map their usage, extract section cross-references,
    and flag defined-but-unused or doubly-defined terms. Pure regex, so it
    scales to whole contracts / statutes (cap raised to MAX_DOC_CHARS)."""
    _check_text(text, limit=MAX_DOC_CHARS)

    defs: dict[str, dict] = {}
    for rx in (_EDASPIDI_QUOTED_RE, _EDASPIDI_BARE_RE):
        for m in rx.finditer(text):
            term = _unwrap_defined_term(m.group(1))
            if not term or (rx is _EDASPIDI_QUOTED_RE and not _introduces_term(text, m.start())):
                continue
            d = defs.setdefault(term, {"position": m.start(), "definitions": 0})
            d["definitions"] += 1

    issues: list[dict] = []
    defined_terms: list[dict] = []
    counts = _count_terms(list(defs), text)
    for term, d in sorted(defs.items(), key=lambda kv: kv[1]["position"]):
        uses = counts[term]
        defined_terms.append({
            "term": term, "position": d["position"],
            "definitions": d["definitions"], "total_occurrences": uses,
        })
        if d["definitions"] > 1:
            issues.append({
                "term": term, "position": d["position"],
                "rule": "duplicate-definition",
                "rule_estonian": "korduv mõistemääratlus",
                "explanation": f"Mõiste «{term}» on defineeritud {d['definitions']} korda.",
            })
        if uses <= d["definitions"]:
            issues.append({
                "term": term, "position": d["position"],
                "rule": "defined-but-unused",
                "rule_estonian": "kasutamata mõiste",
                "explanation": f"Mõiste «{term}» on defineeritud, kuid edaspidi tekstis ei kasutata.",
            })

    xrefs = [{"reference": m.group(0).strip(), "position": m.start()}
             for m in _XREF_RE.finditer(text)]

    return {
        "defined_terms": defined_terms,
        "cross_references": xrefs,
        "issues": issues,
        "summary_estonian": (
            f"Leiti {len(defined_terms)} defineeritud mõistet, {len(xrefs)} "
            f"viidet ja {len(issues)} probleemi."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Simplify Estonian legalese (keep terms of art)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_legalese(text: Annotated[str, Field(description="Estonian statute or contract text.")]) -> _LegaleseResult:
    """Plain-language aid for Estonian statutes and contracts; rewrites
    must keep the legal meaning.

    issues: archaic filler and stock phrases with a plainer suggestion,
    and over-long or comma-heavy sentences to split. terms_of_art: legal
    terms (first use per lemma) to keep verbatim, never swapped for a
    general synonym.
    Starter lexicons: an empty or short issues list does not mean the
    text is plain, and an unlisted word can still be a term of art. For
    reports and business prose use check_officialese.
    """
    return _check_legalese(text)


@mcp.tool(annotations=ToolAnnotations(
    title="Track defined terms & cross-references in a legal document",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_defined_terms(text: Annotated[str, Field(description="Whole Estonian legal document.")]) -> _DefinedTermsResult:
    """Maps defined terms and cross-references in a long Estonian legal
    document.

    defined_terms: each (edaspidi «X») term. total_occurrences counts
    exact-form, case-sensitive matches, the definition included, so it
    misses inflected uses: verify a defined-but-unused issue before
    acting. cross_references: §, lõige, punkt and artikkel mentions, not
    validated. issues: terms defined twice or unused.
    """
    return _check_defined_terms(text)


def _common_legal_usage(word: str) -> dict:
    """Look up the canonical legal collocations for a word from the offline
    legal-corpus index: the 'what's the standard legal phrasing' answer.
    common_before / common_after are the words most frequently seen directly
    before / after the term in real legislation (e.g. `hagi` → before
    `esitama`, i.e. 'esitama hagi'; `kohustus` → after `täitmine`)."""
    _check_text(word, limit=MAX_WORD_CHARS, name="word")
    if any(ch.isspace() for ch in word.strip()):
        raise ValueError("common_legal_usage expects a single word, no whitespace")

    idx = _legal_index()
    Text = _Text()
    t = Text(word)
    t.tag_layer(["morph_analysis"])
    lemma = ""
    for span in t.morph_analysis:
        lemma = (_first(list(span.lemma)) or "").lower()
        break
    lemmas = idx.get("lemmas", {})
    # The entry actually matched, which the summary names: the lemma, or
    # the lowercased input when the lemma is not indexed (võime analyses
    # as võima, a light verb the index drops, but võime is indexed).
    key = lemma if lemmas.get(lemma) else word.lower()
    entry = lemmas.get(key)

    if not entry:
        return {
            "lemma": lemma or word.lower(),
            "found": False,
            "frequency": 0,
            "common_before": [],
            "common_after": [],
            "summary_estonian": (
                f"Sõna '{word}' ei esine juriidilise korpuse indeksis "
                f"(võib olla haruldane või mitte juriidiline termin)."
            ),
        }

    return {
        "lemma": lemma or word.lower(),
        "found": True,
        "frequency": entry.get("freq", 0),
        "common_before": [{"word": w, "count": c} for w, c in entry.get("left", [])],
        "common_after": [{"word": w, "count": c} for w, c in entry.get("right", [])],
        "summary_estonian": (
            f"'{key}' esineb juriidilises korpuses {entry.get('freq', 0)} korda."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Canonical legal usage / collocations for an Estonian term",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def common_legal_usage(word: Annotated[str, Field(description="One Estonian word, any inflected form.")]) -> _LegalUsageResult:
    """Standard legal collocations for an Estonian word, from an index of
    Riigi Teataja legislation (the bundled one: five core codes). Use it
    instead of inventing legal phrasing.

    common_before / common_after: the commonest lemmas of the nearest
    content word on each side, with counts; inflect them to fit. A
    frequency signal, not a rule. found: false says only that the index
    lacks the word, not that it is wrong.
    """
    return _common_legal_usage(word)


def _check_style(text: str) -> dict:
    """Heuristic style metrics for Estonian text. Returns repetition,
    passive-voice ratio, sentence-length variance, and hedging density."""
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["sentences", "morph_analysis"])

    spans = list(t.morph_analysis)
    sentences = list(t.sentences)

    # 1. Repetition: lemma-aware, skip function-word POS classes.
    from collections import Counter, defaultdict
    lemma_counts: Counter = Counter()
    lemma_positions: dict[str, list[int]] = defaultdict(list)
    content_word_count = 0
    for span in spans:
        if not span.text or not span.text[0].isalpha():
            continue
        lemma = _first(list(span.lemma))
        pos = _first(list(span.partofspeech))
        if not lemma or pos in _REPETITION_SKIP_POS:
            continue
        # Skip very short lemmas (1-2 char): usually function-y.
        if len(lemma) <= 2:
            continue
        key = lemma.lower()
        lemma_counts[key] += 1
        lemma_positions[key].append(span.start)
        content_word_count += 1

    # Threshold scales with text length so short replies don't trigger
    # on natural repeats and long copy doesn't drown in non-issues.
    if content_word_count < 50:
        threshold = 3
    elif content_word_count < 200:
        threshold = 4
    else:
        threshold = max(5, content_word_count // 60)

    repeated = []
    for lemma, count in lemma_counts.most_common():
        if count < threshold:
            break
        repeated.append({
            "lemma": lemma,
            "count": count,
            "positions": lemma_positions[lemma],
        })

    # 2. Umbisikuline tegumood: see _impersonal_voice for the four
    # counting corrections over raw Vabamorf form codes.
    imp = _impersonal_voice(spans)
    passive_count = imp["passive_count"]
    verb_count = imp["total_verbs"]
    passive_ratio = imp["ratio"]
    passive_examples = imp["examples"]
    attributive_excluded = imp["attributive_excluded"]

    # 3. Sentence-length variance (in content words per sentence).
    sentence_lengths: list[int] = []
    for sent in sentences:
        # Count word-shaped spans within this sentence's text range.
        wc = sum(
            1 for s in spans
            if s.start >= sent.start and s.end <= sent.end
            and s.text and s.text[0].isalpha()
        )
        if wc > 0:
            sentence_lengths.append(wc)
    if sentence_lengths:
        mean_len = sum(sentence_lengths) / len(sentence_lengths)
        var = sum((x - mean_len) ** 2 for x in sentence_lengths) / len(sentence_lengths)
        stddev = var ** 0.5
        min_len = min(sentence_lengths)
        max_len = max(sentence_lengths)
    else:
        mean_len = stddev = 0.0
        min_len = max_len = 0

    # 4. Hedging density.
    hedge_matches: list[str] = []
    for span in spans:
        if span.text.lower() in _HEDGING_WORDS_ET:
            hedge_matches.append(span.text)
    total_words = sum(1 for s in spans if s.text and s.text[0].isalpha())
    hedge_density = (len(hedge_matches) / total_words) if total_words else 0.0

    # Summary lines in Estonian (so Claude can quote directly).
    rep_et = (
        f"Kõige sagedamini korduvad lemmad: "
        f"{', '.join(r['lemma'] for r in repeated)}."
        if repeated else "Sõnade kordumist ei tuvastatud."
    )
    passive_et = (
        f"Umbisikuline tegumood: {passive_count}/{verb_count} verbi "
        f"({round(passive_ratio*100, 1)}%)." if verb_count
        else "Verbe ei leitud."
    )
    if sentence_lengths and len(sentence_lengths) > 1:
        sl_et = (
            f"Lausepikkus: keskmiselt {mean_len:.1f} sõna "
            f"(min {min_len}, max {max_len}, hajuvus {stddev:.1f})."
        )
    elif sentence_lengths:
        sl_et = f"Üksainus lause, {sentence_lengths[0]} sõna."
    else:
        sl_et = "Lauseid ei leitud."
    hedge_et = (
        f"Kõhklussõnu: {len(hedge_matches)}/{total_words} sõna "
        f"({round(hedge_density*100, 1)}%)." if total_words
        else "Sõnu ei leitud."
    )

    return {
        "repetition": {
            "threshold": threshold,
            "repeated_lemmas": repeated,
            "summary_estonian": rep_et,
        },
        "passive_voice": {
            "passive_count": passive_count,
            "total_verbs": verb_count,
            "ratio": round(passive_ratio, 3),
            "examples": passive_examples,
            "attributive_excluded": attributive_excluded,
            "summary_estonian": passive_et,
        },
        "sentence_length": {
            "mean": round(mean_len, 2),
            "stddev": round(stddev, 2),
            "min": min_len,
            "max": max_len,
            "count": len(sentence_lengths),
            "summary_estonian": sl_et,
        },
        "hedging": {
            "hedge_count": len(hedge_matches),
            "total_words": total_words,
            "density": round(hedge_density, 3),
            "matches": hedge_matches,
            "summary_estonian": hedge_et,
        },
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian style (repetition, passive, hedging, rhythm)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_style(text: Annotated[str, Field(description="Estonian text to measure.")]) -> _StyleResult:
    """Heuristic Estonian style metrics; no cliche detection.

    - `repetition`: content lemmas used `threshold`+ times (scales with
      length), with character offsets.
    - `passive_voice`: umbisikuline tegumood share of verbs (attributive
      -tud participles go to `attributive_excluded`). Marketing copy: aim
      under 15%; under 5% may read forceful. Reports: over 40% is the
      clearest kantseliit sign (see check_officialese).
    - `sentence_length`: words per sentence; a stddev under 30% of the
      mean reads monotonous. News and web text: at most 10 to 15 words a
      sentence (the state's web-writing guide). Measured averages: e-mail
      about 9, news about 14, statutes about 25.
    - `hedging`: share of words on a short fixed list (vist, pigem,
      ehk); over 5% reads wishy-washy, 0% does not rule hedging out.
    """
    return _check_style(text)


# Officialese thresholds, calibrated on a real Estonian R&D report
# against the plain-language rewrite a native speaker produced from it.
# Measured across that edit:
#
#   impersonal ratio   0.875 → 0.308   (gate 0.4,  separates cleanly)
#   -mine per 100 wds  7.89  → 2.33    (gate 4.0,  separates cleanly)
#   longest sentence   30    → 21 wds  (gate 25,   separates cleanly)
#   noun/verb ratio    2.50  → 2.23    (gate 2.5,  barely separates)
#
# Noun/verb is deliberately the loosest gate: one document pair is thin
# calibration and the two texts sit close together on it, so it is
# reported as a metric always and raised as an issue only when clearly
# heavy. The other three carry the signal.
_OFFICIALESE_LONG_SENTENCE_WORDS = 25
_OFFICIALESE_CLAUSE_STACK = 3
_OFFICIALESE_NOUN_VERB_RATIO = 2.5
_OFFICIALESE_NOMINALISATION_PER_100 = 4.0
_OFFICIALESE_IMPERSONAL_RATIO = 0.4


def _sentence_ranges(t, spans: list) -> list[tuple[int, int]]:
    """(first, end) indices into `spans` for each sentence of `t`, in one
    pass: a span belongs to the sentence whose character range holds it
    whole. Filtering every span per sentence made a 100,000-character
    text quadratic."""
    ranges: list[tuple[int, int]] = []
    k = 0
    for sent in t.sentences:
        while k < len(spans) and spans[k].start < sent.start:
            k += 1
        first = k
        while k < len(spans) and spans[k].end <= sent.end:
            k += 1
        ranges.append((first, k))
    return ranges


def _root_parts(span) -> list[str]:
    """Compound parts of the first analysis, lower-cased, markers dropped
    (läbivaatamine → ['läbi', 'vaatamine'])."""
    parts = _first(list(span.root_tokens)) or ()
    return [p.lower().replace("_", "").replace("=", "") for p in parts]


# Compounds Vabamorf left as one root and whose joined verb it rejects
# (andmekaitsemõjuhindamine) that get the suffix search, per text. Each
# search looks up about 30 long unknown words at under a millisecond
# apiece; past the cap the verb is left out, never guessed.
_MINE_SUFFIX_SEARCHES = 25

_VOWELS_ET = "aeiouõäöü"


def _batch_readings(words: set[str]) -> dict[str, set[tuple[str, str]]]:
    """(partofspeech, form) of each word's lexicon readings, in one
    Vabamorf call, with no guesser and no proper names. A spellcheck
    accepts any well-formed form, so 'sisulooma' passed as a verb when it
    is only the noun sisu_loom (content + animal)."""
    if not words:
        return {}
    ordered = sorted(words)
    rows = _vabamorf().analyze(ordered, disambiguate=False, guess=False,
                               propername=False)
    return {w: {(a["partofspeech"], a["form"]) for a in (row.get("analysis") or [])}
            for w, row in zip(ordered, rows, strict=True)}


def _genitive_shift(prefix: str, verb: str) -> list[tuple[str, str]]:
    """Vabamorf sometimes cuts a genitive compound one letter late: risk +
    ihaldamine for riski + haldamine, pilv + emigreerimine for pilve +
    migreerimine. When the first part ends in a consonant and the verb
    starts with a vowel, the shorter verb is a candidate on condition that
    the first part plus that vowel is a genitive: [(verb, genitive)]."""
    if (prefix and prefix[-1] not in _VOWELS_ET and verb[:1] in _VOWELS_ET
            and len(verb) > 5):
        return [(verb[1:], prefix + verb[0])]
    return []


def _mine_verbs(nouns: dict[str, list[str]]) -> dict[str, str | None]:
    """The verb behind each -mine noun, given as lemma → _root_parts.

    In order: an override (läbiviimine → korraldama / tegema); a particle
    verb, written apart (läbivaatamine → läbi vaatama, EKI reegel 31),
    when its head is a verb, tested BEFORE the joined verb because
    Vabamorf knows the noun 'juurdepääsema'; the joined verb when Vabamorf
    reads it as a verb (taaskasutamine → taaskasutama, but not the noun
    'sisulooma'); the head verb of a noun + verb compound, which has no
    compound verb (riskihindamine → hindama); and for a compound Vabamorf
    left as one root, the longest verb of 5+ letters it ends in
    (andmekaitsemõjuhindamine → hindama). Where Vabamorf cut a genitive
    one letter late, the boundary moves back (riskihaldamine → haldama,
    not ihaldama). None when nothing passes: a missing verb beats an
    invented one. The lookups are batched: one call per text, and a
    second only for the suffix searches."""
    verbs: dict[str, str | None] = {}
    pending: dict[str, list[tuple[str, str | None]]] = {}
    particle: dict[str, str] = {}
    for lemma, parts in nouns.items():
        if lemma in _MINE_VERB_OVERRIDES_ET:
            verbs[lemma] = _MINE_VERB_OVERRIDES_ET[lemma]
            continue
        joined = lemma[:-4] + "ma"
        if len(parts) >= 2 and parts[-1].endswith("mine"):
            head = parts[-1][:-4] + "ma"
            if parts[-2] in _VERB_PARTICLES_ET:
                particle[lemma] = f"{parts[-2]} {head}"
            pending[lemma] = [(joined, None), *_genitive_shift(parts[-2], head),
                              (head, None)]
        else:
            pending[lemma] = [(joined, None)]

    def words(lemmas) -> set[str]:
        return {w for lemma in lemmas for pair in pending[lemma] for w in pair if w}

    readings = _batch_readings(words(pending))
    unsplit = [lemma for lemma, candidates in pending.items()
               if len(candidates) == 1
               and ("V", "ma") not in readings.get(candidates[0][0], ())]
    for lemma in unsplit[:_MINE_SUFFIX_SEARCHES]:
        for i in range(1, len(lemma) - 6):
            verb = lemma[i:-4] + "ma"
            pending[lemma] += [*_genitive_shift(lemma[:i], verb), (verb, None)]
    readings |= _batch_readings(words(unsplit[:_MINE_SUFFIX_SEARCHES]) - set(readings))

    def accepted(verb: str, genitive: str | None) -> bool:
        return (("V", "ma") in readings.get(verb, ())
                and (genitive is None or ("S", "sg g") in readings.get(genitive, ())))

    for lemma, candidates in pending.items():
        if lemma in particle and accepted(candidates[-1][0], None):
            verbs[lemma] = particle[lemma]
        else:
            verbs[lemma] = next((v for v, g in candidates if accepted(v, g)), None)
    return verbs


@lru_cache(maxsize=4096)
def _guessed_readings(word: str) -> tuple[tuple[str, str, str], ...]:
    """Every (lemma, partofspeech, form) Vabamorf gives `word` out of
    context, guesser and proper names included. The sentence analysis
    keeps one reading of a case homograph (seina comes back sg p only,
    küla and metsa sg n or sg p), and the genitive among them is what
    makes 'läbi' a postposition."""
    try:
        found = _vabamorf().analyze([word], disambiguate=False, guess=True,
                                    propername=True)
    except Exception:
        return ()
    return tuple((a["lemma"], a["partofspeech"], a["form"])
                 for a in (found[0].get("analysis") or []))


def _all_readings(span) -> list[tuple[str, str, str, tuple]]:
    """(lemma, partofspeech, form, root_tokens) of every analysis the
    sentence analysis kept for a span."""
    return [(a.lemma or "", a.partofspeech or "", a.form or "",
             tuple(a.root_tokens or ())) for a in span.annotations]


def _any_readings(span) -> list[tuple[str, str, str]]:
    """The span's readings in context plus those of its surface out of
    context."""
    return ([r[:3] for r in _all_readings(span)]
            + list(_guessed_readings(span.text)))


def _pos_any(span) -> set[str]:
    return {pos for _lemma, pos, _form in _any_readings(span)}


def _is_genitive(span) -> bool:
    """Any reading, in context or out of it, is a nominal genitive."""
    return any(pos in ("S", "A", "P", "N", "O", "H", "Y") and form.endswith(" g")
               for _lemma, pos, form in _any_readings(span))


def _is_elative(span) -> bool:
    """Any reading, in context or out of it, is an elative. The guesser
    gives capitalised words fake elatives (Pärast), so this is used only
    where a missed hit is the cost."""
    return any(form.endswith(" el") for _lemma, _pos, form in _any_readings(span))


def _head_lemma(span) -> str:
    """The lemma of a compound's last part (rahuloluküsitlus → küsitlus),
    or the whole lemma."""
    lemma, _pos, _form, parts = _all_readings(span)[0]
    lemma = lemma.lower()
    if len(parts) >= 2:
        prefix = "".join(parts[:-1]).lower().replace("_", "")
        if lemma.startswith(prefix):
            return lemma[len(prefix):]
    return lemma


def _is_event_noun(span) -> bool:
    """A noun one conducts: an _LABI_VIIMA_EVENTS_ET lemma or compound
    head, or any -mine noun of 7+ letters."""
    if "S" not in _pos_any(span):
        return False
    lemma = _span_bits(span)[2]
    return (lemma in _LABI_VIIMA_EVENTS_ET
            or _head_lemma(span) in _LABI_VIIMA_EVENTS_ET
            or (lemma.endswith("mine") and len(lemma) >= 7))


def _viima_form(span) -> str | None:
    """The viima form code of a span (viidi → ti, viinud → nud), from any
    of its readings, or None. Vabamorf returns viidud, viiv and viidav as
    adjectives with the surface as lemma, so those map by surface."""
    readings = _all_readings(span)
    for lemma, pos, form, _parts in readings:
        if lemma == "viima" and pos == "V":
            return form
    low = span.text.lower()
    if low in _VIIMA_PARTICIPLES_ET and any(r[0] == low for r in readings):
        return _VIIMA_PARTICIPLES_ET[low]
    return None


def _clause_before(spans: list, j: int, n: int) -> list:
    """Up to `n` spans before spans[j], nearest first, stopping at
    punctuation or a coordinator."""
    out = []
    for m in range(j - 1, max(-1, j - 1 - n), -1):
        if (_is_punct(spans[m])
                or spans[m].text.lower() in ("ja", "ning", "ega", "või", "kuid", "aga")):
            break
        out.append(spans[m])
    return out


def _genitive_chain(spans: list, j: int) -> tuple[bool, list]:
    """Walk the genitive chain that starts at spans[j], at most 6 spans
    ('kõigi harjutuste', 'Monte Grigio küla'). A postposition after it
    takes the last noun as its complement and drops it. Returns (True, [])
    when that leaves nothing and the postposition is non-spatial
    ('küsitluse abil'), else (False, [first noun, last noun]) or
    (False, [])."""
    chain = []
    k = j
    while k < len(spans) and k < j + 6:
        span = spans[k]
        low = span.text.lower()
        if _is_punct(span):
            break
        if _is_genitive(span) or low == "kogu":
            chain.append(span)
        elif not ((low in ("väidetavalt", "eriti", "väga", "ka") and "D" in _pos_any(span))
                  or (low in ("ja", "ning") and chain)):
            break
        k += 1
    # 'pärast' before a partitive noun is a preposition ('läbi metsa
    # pärast lõunat'), so the chain stays the complement of 'läbi'.
    prep = (k + 1 < len(spans) and spans[k].text.lower() == "pärast"
            and any(pos == "S" and form.endswith(" p")
                    for _l, pos, form, _r in _all_readings(spans[k + 1])))
    if chain and k < len(spans) and "K" in _pos_any(spans[k]) and not prep:
        chain = chain[:-1]
        if not chain and spans[k].text.lower() in _LABI_VIIMA_NONSPATIAL_POSTP_ET:
            return True, []
    nouns = [s for s in chain if _pos_any(s) & {"S", "H"}]
    return False, ([nouns[0], nouns[-1]] if nouns else [])


def _adverbial_event(spans: list, k: int) -> bool:
    """spans[k] is a -mine noun used as a time adverbial, not the thing
    conducted: an adessive (Kaabel viiakse paigaldamisel läbi) or a
    genitive before a non-spatial postposition (ehitamise käigus). A
    listed event keeps its weight there: 'Tee ehitus viiakse läbi laagri
    ajal'."""
    if not (_span_bits(spans[k])[2].endswith("mine") and _is_event_noun(spans[k])):
        return False
    forms = [f for _l, p, f, _r in _all_readings(spans[k]) if p == "S"]
    if forms and all(f.endswith(" ad") for f in forms):
        return True
    return (k + 1 < len(spans) and any(f.endswith(" g") for f in forms)
            and spans[k + 1].text.lower() in _LABI_VIIMA_NONSPATIAL_POSTP_ET)


def _route_subject(spans: list, k: int) -> bool:
    """spans[k] is a nominative route noun, not part of a name: 'Rada viib',
    'Toru viiakse', not 'Eesti Raudtee'. In-context readings only: the
    guesser gives every capitalised word an H reading."""
    if k < 0:
        return False
    for m in (k, k - 1):
        if m >= 0 and any(p == "H" for _l, p, _f, _r in _all_readings(spans[m])):
            return False
    return (_head_lemma(spans[k]) in _LABI_VIIMA_ROUTES_ET
            and any(p == "S" and f.endswith(" n") for _l, p, f, _r in _all_readings(spans[k])))


def _place_after(spans: list, j: int) -> bool:
    """'läbi' follows the viima form at spans[j] and its genitive
    complement names a place: 'läbi laagri', 'läbi festivali
    territooriumi', not 'läbi küsitluse'."""
    if j + 2 >= len(spans) or spans[j + 1].text.lower() != "läbi":
        return False
    _postp, ends = _genitive_chain(spans, j + 2)
    return bool(ends) and (not _is_event_noun(ends[-1])
                           or _head_lemma(ends[-1]) in _LABI_VIIMA_PLACE_EVENTS_ET)


def _labi_source(span) -> bool:
    """A noun in the elative or ablative that is not a time (Külast,
    seinast; not aastast), in context only: the guesser's fake elatives
    (Pärast) would hide real hits."""
    readings = _all_readings(span)
    return ("S" in _pos_any(span)
            and any(form.endswith((" el", " abl")) for _l, _p, form, _r in readings)
            and not any(lemma in _LABI_VIIMA_TIME_NOUNS_ET
                        for lemma, _p, _f, _r in readings))


def _physical_before(spans: list, j: int, head_only: bool = False) -> bool:
    """The viima form at spans[j] reads as leading through a place: no
    event noun within 6 spans before it or 7 after, and its clause holds
    a source in the elative or ablative (Külast viib läbi), kust / siit /
    sealt, or a route noun (Rada viib läbi metsa) that is not an event's
    modifier (Tee ehitamise käigus). With `head_only`, a route noun before
    any other noun is a modifier too (Tee remont tuleb läbi viia). In a
    relative clause the antecedent counts too (Kraav, mis viib läbi
    surnuaia). An event noun used as a time adverbial does not count
    (Kaabel viiakse paigaldamisel läbi), and nor does one after the verb
    when a route subject leads through a place (Tee viib läbi laagri).
    Readings in context only: the guesser's fake elatives (Pärast) would
    hide real hits here."""
    lo = j - len(_clause_before(spans, j, 6))
    before = list(range(j - 1, j - 1 - len(_clause_before(spans, j, 4)), -1))
    antecedent = j - len(before) - 2
    relative = (bool(before) and spans[before[-1]].text.lower() in ("mis", "mida", "mille", "kus")
                and antecedent >= 0)
    after = range(j + 1, min(len(spans), j + 8))
    subject = antecedent if relative and len(before) == 1 else j - 1
    if _route_subject(spans, subject) and _place_after(spans, j):
        after = range(0)
    if any(_is_event_noun(spans[k]) and not _adverbial_event(spans, k)
           for k in (*range(lo, j), *after)):
        return False
    if relative:
        before.append(antecedent)
    for k in before:
        span = spans[k]
        if _labi_source(span):
            return True
        if span.text.lower() in ("kust", "siit", "sealt", "kustkaudu"):
            return True
        if ("S" in _pos_any(span) and _head_lemma(span) in _LABI_VIIMA_ROUTES_ET
                and not (k + 1 < len(spans) and _is_event_noun(spans[k + 1]))
                and not (head_only and k + 1 < j and any(
                    p == "S" for _l, p, _f, _r in _all_readings(spans[k + 1])))):
            return True
    return False


def _viima_goal_after(spans: list, k: int) -> bool:
    """A viima particle or a goal case (illative, short illative, allative,
    terminative) within 6 spans from spans[k], before punctuation, a
    coordinator or another verb: 'viidi ellu', 'viidi projekt ummikusse',
    'viiakse kaup lõpptarbijani'."""
    for s in spans[k:k + 6]:
        low = s.text.lower()
        if _is_punct(s) or low in ("ja", "ning", "ega", "või", "kuid", "aga"):
            return False
        if low in _VIIMA_GOAL_PARTICLES_ET:
            return True
        if any(pos in ("S", "A", "P", "N", "O", "H")
               and form.endswith((" ill", "adt", " all", " ter"))
               for _l, pos, form in _any_readings(s)):
            return True
        if _pos_any(s) == {"V"}:
            return False
    return False


def _finite_verb(span) -> bool:
    """Every in-context reading is a finite verb form (kontrollisime,
    tuli, murdis)."""
    readings = _all_readings(span)
    return bool(readings) and all(
        pos == "V" and form and form not in _NONFINITE_VERB_FORMS_ET
        and not form.startswith("neg")
        for _l, pos, form, _r in readings)


def _labi_local(span) -> bool:
    """An elative or a local adverb next to 'läbi': a route."""
    return _is_elative(span) or span.text.lower() in _LABI_VIIMA_LOCAL_ADV_ET


def _labi_possessor(span) -> bool:
    """A genitive that makes 'läbi' its postposition. A personal pronoun
    is the subject instead: 'on ta läbi viinud'."""
    return (_is_genitive(span) and not _is_punct(span) and not _is_event_noun(span)
            and _span_bits(span)[2] not in _PERSONAL_PRONOUNS_ET)


def _labi_duration(spans: list, k: int) -> bool:
    """spans[k] is a time span that can own 'läbi': a nominative (päev,
    aastad), a plural partitive (päevi), or a singular partitive after a
    numeral or quantifier (mitu tundi, kuu aega). A bare singular
    partitive is an object: 'Tundi läbi viies'."""
    if k < 0:
        return False
    before = spans[k - 1] if k > 0 else None
    for lemma, pos, form, _r in _all_readings(spans[k]):
        if pos != "S" or lemma not in _LABI_VIIMA_SPAN_NOUNS_ET:
            continue
        if form in ("sg n", "pl n", "pl p"):
            return True
        if form == "sg p" and before is not None and (
                "N" in _pos_any(before)
                or before.text.lower() in _LABI_VIIMA_SPAN_QUANTIFIERS_ET):
            return True
    return False


def _bare_name(span) -> bool:
    """A name Vabamorf cannot inflect: every non-Y reading is sg n, ? or
    empty (Monte, Munt)."""
    forms = {form for _l, pos, form, _r in _all_readings(span) if pos != "Y"}
    return bool(forms) and forms <= {"sg n", "?", ""}


def _labi_viima_issues(text: str, spans: list,
                       sentences: list[tuple[int, int]]) -> list[dict]:
    """'läbi viima' in the conduct sense, every form: viidi läbi, on
    läbi viinud, viis uuringu läbi (officialese-phrase), and the nouns
    läbiviimine, läbiviija, läbiviidud, läbiviidav, läbi viimine
    (officialese-filler), with the korraldama / tegema form to swap in.

    'läbi' is a postposition or adverb in both senses and Vabamorf's D / K
    tag does not separate them, so the guards read the neighbours: an
    elative or local adverb after 'läbi' (viidi metsast läbi, siit läbi),
    a genitive before it (Metsa läbi viiv tee, Selle läbi), a genitive
    after it whose chain holds no event noun (viis lapse läbi metsa, läbi
    kõigi harjutuste), a route, a threaded object or a source in the
    clause (Rada viib läbi küla, Kaabel tuleb läbi viia seinast, kaabli
    läbiviimine), a time span that owns 'läbi' (Terve päev läbi viisime
    kaste autosse), a genitive before an impersonal viima with a goal of
    its own (Projekti läbi viidi ellu), the numeral (läbi viies riigis,
    Kontrollisime läbi viis) and music theory (teema läbiviimine). Each
    'läbi' gives at most one issue."""
    sent_of = [-1] * len(spans)
    for si, (lo, hi) in enumerate(sentences):
        for k in range(lo, hi):
            sent_of[k] = si
    music: dict[int, bool] = {}

    def music_context(i: int) -> bool:
        si = sent_of[i]
        if si < 0:
            return False
        if si not in music:
            lo, hi = sentences[si]
            terms = {w for s in spans[lo:hi] for w in (_span_bits(s)[2], _head_lemma(s))
                     if w in _LABI_VIIMA_MUSIC_ET}
            music[si] = len(terms) >= 2 or "fuuga" in terms
        return music[si]

    local, possessor = _labi_local, _labi_possessor

    def phrase(first: int, last: int, form: str) -> dict:
        # One holds a meeting rather than organising it: pidama leads when
        # the clause names such an event (koosolek viidi läbi → peeti). A
        # time span that owns 'läbi' is not one: 'kaks tundi läbi viidud'.
        near = _clause_before(spans, first, 5)
        if near and spans[first].text.lower() == "läbi" and _labi_duration(spans, first - 1):
            near = near[1:]
        near += spans[first:last + 1]
        for s in spans[last + 1:last + 5]:
            if _is_punct(s):
                break
            near.append(s)
        held = any(any(p == "S" for _l, p, _f, _r in _all_readings(s))
                   and (_span_bits(s)[2] in _LABI_VIIMA_PIDAMA_ET
                        or _head_lemma(s) in _LABI_VIIMA_PIDAMA_ET)
                   for s in near)
        verbs = ("pidama", "korraldama") if held else ("korraldama", "tegema")
        forms = [max(_synthesize(v, form, "V") or [""], key=len) for v in verbs]
        forms = forms if all(forms) else list(verbs)
        # 'viis uuringu läbi': the words between stay, so the suggestion
        # replaces the whole phrase ('korraldas uuringu').
        if last - first >= 2:
            middle = text[spans[first + 1].start:spans[last - 1].end]
            forms = [f"{f} {middle}" for f in forms]
        return {
            "phrase": text[spans[first].start:spans[last].end],
            "position": spans[first].start,
            "rule": "officialese-phrase",
            "rule_estonian": "kantseliitlik väljend",
            "explanation": _LABI_VIIMA_EXPLANATION_ET,
            "suggestion": " / ".join(forms),
        }

    def filler(first: int, last: int, lemma: str) -> dict:
        replacements, pos, why = _LABI_VIIMA_NOUNS_ET[lemma]
        form = _span_bits(spans[last])[1]
        words = [(_synthesize(r, form, pos) or [""])[0] if form else ""
                 for r in replacements]
        return {
            "word": text[spans[first].start:spans[last].end],
            "position": spans[first].start,
            "rule": "officialese-filler",
            "rule_estonian": "kantseliitlik täitesõna",
            "explanation": why,
            "suggestion": " / ".join(words if all(words) else replacements),
        }

    issues: list[dict] = []
    n = len(spans)
    for i, span in enumerate(spans):
        lemma = _span_bits(span)[2]
        if lemma in _LABI_VIIMA_NOUNS_ET:
            # Not 'kaablite läbiviimisel seinast', not 'toru läbiviimine
            # läbi tulemüüri' (the nearest genitive noun before it is a
            # threaded object), not a fugue.
            window = spans[max(0, i - 3):i] + spans[i + 1:i + 4]
            obj = next((s for s in _clause_before(spans, i, 3)
                        if _is_genitive(s) and "S" in _pos_any(s)), None)
            threaded = obj is not None and _head_lemma(obj) in _LABI_VIIMA_THREADED_ET
            if (not threaded and not any(_is_elative(w) for w in window)
                    and not music_context(i)):
                issues.append(filler(i, i, lemma))
            continue
        if span.text.lower() != "läbi" or music_context(i):
            continue
        prev = spans[i - 1] if i > 0 else None
        nxt = spans[i + 1] if i + 1 < n else None

        # 1. viima form + läbi: viidi läbi, viis läbi põhjaliku kontrolli.
        form = _viima_form(prev) if prev is not None else None
        if form is not None:
            if _physical_before(spans, i - 1):
                continue
            if nxt is None or _is_punct(nxt):
                hit = True
            elif local(nxt):
                hit = False
            elif (("H" in _pos_any(nxt) or nxt.text[:1].isupper())
                  and (_is_genitive(nxt) or _bare_name(nxt))):
                # An agent or a place name after 'läbi' (viib läbi
                # ülikool, viib läbi Monte Grigio küla): only with an
                # event noun before the verb or in the name's chain. A
                # singular nominative before an active verb is its
                # subject, which goes along the route: 'Retk viis läbi
                # Lõuna-Eesti metsade'.
                _postp, ends = _genitive_chain(spans, i + 1)
                before = [s for s in _clause_before(spans, i - 1, 5)
                          if not (form in _VIIMA_ACTIVE_FORMS_ET
                                  and {f for _l, p, f, _r in _all_readings(s)
                                       if p == "S"} == {"sg n"})]
                hit = any(_is_event_noun(s) for s in before + ends)
            elif _is_genitive(nxt):
                postp, ends = _genitive_chain(spans, i + 1)
                if postp:
                    # 'küsitluse abil' marks the conduct sense only beside
                    # something one conducts: not 'Niit viiakse läbi nõela
                    # abil'.
                    after = []
                    for s in spans[i + 1:i + 8]:
                        if _is_punct(s):
                            break
                        after.append(s)
                    postp = any(_is_event_noun(s)
                                for s in _clause_before(spans, i - 1, 5) + after)
                hit = postp or any(_is_event_noun(s) for s in ends)
            else:
                hit = True
            if hit:
                issues.append(phrase(i - 1, i, form))
            continue

        # 2. Split nouns: läbi viimine, läbi viija.
        split = _span_bits(nxt)[2] if nxt is not None else ""
        if split in _LABI_VIIMA_SPLIT_NOUNS_ET:
            if prev is None or not (local(prev) or possessor(prev)):
                issues.append(filler(i, i + 1, _LABI_VIIMA_SPLIT_NOUNS_ET[split]))
            continue

        # 3. läbi + viima form: tuleb läbi viia, läbi viidud uuring.
        form = _viima_form(nxt) if nxt is not None else None
        if form is not None:
            after = spans[i + 2] if i + 2 < n else None
            # The numeral before a noun (läbi viies riigis), or after a
            # finite verb whose particle 'läbi' is (Kontrollisime läbi viis
            # väga erinevat stsenaariumi, Sõelast tuli läbi viies ja kuues).
            if (nxt.text.lower() in _VIIS_NUMERAL_SURFACES_ET
                    and ((after is not None and "S" in _pos_any(after))
                         or (prev is not None and _finite_verb(prev)))):
                continue
            if prev is not None and local(prev) and not any(
                    lemma in _LABI_VIIMA_TIME_NOUNS_ET
                    for lemma, _p, _f in _guessed_readings(prev.text)):
                continue
            if prev is not None and possessor(prev):
                continue
            # An impersonal verb takes no genitive object, so a genitive
            # before 'läbi' is its postposition's complement, even for an
            # event noun or a pronoun, when viima has its own goal:
            # 'Projekti läbi viidi ellu mitu algatust', 'Tema läbi viidi
            # raha välismaale'.
            if (form in _VIIMA_IMPERSONAL_FORMS_ET and prev is not None
                    and _is_genitive(prev) and _viima_goal_after(spans, i + 2)):
                continue
            rest = []
            for s in spans[i + 2:i + 8]:
                if _is_punct(s) or s.text.lower() in ("ja", "ning", "ega", "või", "kuid", "aga"):
                    break
                rest.append(s)
            clause = _clause_before(spans, i, 6) + rest
            # A time span owns 'läbi' (Terve päev läbi viisime kaste
            # autosse) unless an event noun is near (Kaks nädalat läbi
            # viidud küsitlus).
            if _labi_duration(spans, i - 1) and not any(_is_event_noun(s) for s in clause):
                continue
            # A route subject or source before (Kaabel tuleb läbi viia), or
            # a source or 'kaudu' after (tuli läbi viia rahvamassist, on
            # läbi viidud seina kaudu), with no event noun near. The route
            # noun must head its phrase: 'Tee remont tuleb läbi viia' is
            # conduct.
            if _physical_before(spans, i + 1, head_only=True) or (
                    any(_labi_source(s) or s.text.lower() in ("kaudu", "siit", "sealt")
                        for s in rest)
                    and not any(_is_event_noun(s) for s in clause)):
                continue
            issues.append(phrase(i, i + 1, form))
            continue

        # 4. viima form, 1 to 3 words, läbi: viis uuringu läbi.
        for back in (2, 3, 4):
            j = i - back
            if j < 0:
                break
            form = _viima_form(spans[j])
            if form is None:
                continue
            between = spans[j + 1:i]
            # Not past the verb's own 'läbi': 'Talgud viidi läbi päev läbi'.
            if (any(_is_punct(s) or s.text.lower() == "läbi" for s in between)
                    or (prev is not None and local(prev))
                    or (nxt is not None and not _is_punct(nxt)
                        and (_is_genitive(nxt) or local(nxt)))
                    or _physical_before(spans, j)):
                break
            # An event noun as a time adverbial is not what is conducted:
            # 'Kaabel viiakse paigaldamisel läbi'.
            if any(_is_event_noun(spans[k]) and not _adverbial_event(spans, k)
                   for k in range(j - len(_clause_before(spans, j, 4)), i) if k != j):
                issues.append(phrase(j, i, form))
            break
    return issues


def _kusin_tapsemalt_issue(text: str, s: list, prev: list) -> dict | None:
    """'Küsin täpsemalt, kas …' at the start of the sentence `s` (spans),
    with `prev` the spans of the sentence before it. See
    _KUSIN_INTERROG_LEMMAS_ET for the rule; the guards skip quoted speech
    (quotation marks, a leading dialogue dash or a quoted e-mail line '>'),
    a question, a question noun (uurimisküsimus) or a stated aim (eesmärk)
    just before (then 'täpsemalt' has something to refine), the stressed
    Mina / Meie, any word between küsin and täpsemalt (küsin
    raamatupidajalt täpsemalt, küsin siis täpsemalt), bare 'kui' (if), a
    plan to report back (…, ja annan teada; …, ja võtan teiega ühendust)
    and narration where the person asked reacts in the same sentence
    (…, aga ta vaikib)."""
    if prev and (prev[-1].text == "?"
                 or any((lemma := _span_bits(x)[2]) == "küsima"
                        or lemma.endswith(("küsimus", "eesmärk")) for x in prev)):
        return None
    if any(x.text in _QUOTE_MARKS for x in s):
        return None
    k = 0
    while k < len(s) and not s[k].text[:1].isalpha():
        k += 1
    if any(x.text[:1] in _KUSIN_SPEECH_LEAD_ET for x in s[:k]):
        return None
    if k + 1 < len(s) and s[k].text.lower() in ("tere", "tervist") and s[k + 1].text == ",":
        k += 2
    start = k
    if k < len(s) and s[k].text.lower() in ("ma", "me"):
        k += 1
    if k >= len(s):
        return None
    forms = {a.form for a in s[k].annotations
             if a.lemma == "küsima" and a.partofspeech == "V"}
    if not forms & {"n", "me"}:
        return None
    j = k + 1
    to_you = j < len(s) and s[j].text.lower() == "sinult"
    if j < len(s) and s[j].text.lower() in ("teilt", "sinult"):
        j += 1
    if j >= len(s) or s[j].text.lower() != "täpsemalt":
        return None
    end = s[j].end
    j += 1
    if j >= len(s) or s[j].text not in (",", ":"):
        return None
    j += 1
    if j < len(s) and s[j].text.lower() == "et":
        j += 1
    if j >= len(s):
        return None
    opener = s[j]
    if not (_span_bits(opener)[2] in _KUSIN_INTERROG_LEMMAS_ET
            or (opener.text.lower() == "kui" and j + 1 < len(s)
                and s[j + 1].text.lower() in _KUSIN_KUI_DEGREE_ET)):
        return None
    tail = s[j + 1:]
    contact = any(x.text.lower() == "ühendust" for x in tail)
    if any(a.partofspeech == "V" and a.form in ("n", "me")
           and (a.lemma in _KUSIN_PLAN_VERBS_ET or (contact and a.lemma == "võtma"))
           for x in tail for a in x.annotations):
        return None
    if any(tail[m].text == "," and tail[m + 1].text.lower() in _KUSIN_NARRATION_BUT_ET
           and _span_bits(tail[m + 2])[2] in ("tema", "nemad")
           for m in range(len(tail) - 2)):
        return None
    plural = "me" in forms
    if to_you:
        suggestion = ("Palun täpsusta, … / Tahaksime täpsemalt teada, …" if plural
                      else "Palun täpsusta, … / Tahaksin täpsemalt teada, …")
    elif plural:
        suggestion = "Palume täpsustada, … / Sooviksime täpsemalt teada, …"
    else:
        suggestion = "Palun täpsustage, … / Sooviksin täpsemalt teada, …"
    return {
        "phrase": text[s[start].start:end],
        "position": s[start].start,
        "rule": "kusin-tapsemalt",
        "rule_estonian": "'küsin täpsemalt'-palve",
        "explanation": _KUSIN_TAPSEMALT_EXPLANATION_ET,
        "suggestion": suggestion,
    }


# How far _calque_find_is_search looks past 'Palun leidke' for 'üles'.
# Bounded, so a text of 'Palun leidke manuses' without punctuation stays
# linear.
_CALQUE_SEARCH_WINDOW = 12


def _calque_find_is_search(spans: list, i: int, k: int) -> bool:
    """True when 'Palun leidke' at spans[i] with the attachment word at
    spans[k] is a real search instruction, not 'please find attached':
    'leia lisast üles', 'leidke manusest oma nimi', 'leia lisatud failist
    viga', 'leidke lisas 3 toodud tabelist', 'leidke manuses toodud
    juhendi järgi'."""
    n = len(spans)
    end = min(n, i + _CALQUE_SEARCH_WINDOW)
    end = next((j for j in range(i + 2, end) if _is_punct(spans[j])), end)
    if any(s.text.lower() == "üles" for s in spans[i + 2:end]):
        return True
    rest = spans[k + 1:end]
    if rest and rest[0].text.lower() == "oma":
        return True
    # The head after the attachment word, past numbers, adjectives and
    # participles: a place to search (elative, ablative, or genitive +
    # postposition) rather than the thing sent.
    for j, s in enumerate(rest):
        pos, form, _lemma = _span_bits(s)
        if pos in ("N", "A") or (pos == "V" and form in ("tud", "nud", "v")):
            continue
        if form.endswith((" el", " abl")):
            return True
        return (form.endswith(" g") and j + 1 < len(rest)
                and _span_bits(rest[j + 1])[0] == "K")
    return False


def _calque_thanks_for_your(spans: list, j: int) -> bool:
    """True when spans[j:] is the 'X eest' that 'tänan teid teie' thanks
    for: an 'eest' within six words, with no punctuation or adposition on
    the way, and not 'teie ja ...' (two possessors), 'sinu enda ...' or a
    place or manner phrase (sinu kodus, teie emakeeles)."""
    first = spans[j]
    readings = [(a.lemma or "", a.partofspeech or "", a.form or "")
                for a in first.annotations]
    if (any(pos == "J" or lemma in ("ise", "oma") for lemma, pos, _f in readings)
            or (readings and all(form.endswith(_CALQUE_LOCAL_CASES)
                                 for _l, _p, form in readings))):
        return False
    for k in range(j, min(len(spans), j + 6)):
        w = spans[k]
        if w.text.lower() == "eest":
            return k > j
        if _is_punct(w) or _span_bits(w)[0] == "K":
            return False
    return False


@lru_cache(maxsize=4096)
def _calque_imperative(lemma: str, plural: bool) -> str:
    """The 2sg or 2pl imperative of a verb (küsima: küsi, küsige), or ''
    when Vabamorf has none."""
    return _first(_synthesize(lemma, "ge" if plural else "o", "V")) or ""


def _calque_issues(text: str, spans: list) -> list[dict]:
    """calque-phrase issues: translated stock phrases of letters and
    business prose (see _CALQUE_NOT_LIMITED_RE and the sets after it)."""
    issues: list[dict] = []

    def add(start: int, end: int, why: str, plain: str) -> None:
        issues.append({
            "phrase": text[start:end],
            "position": start,
            "rule": "calque-phrase",
            "rule_estonian": "tõlkelaenuline väljend",
            "explanation": why,
            "suggestion": plain,
        })

    for m in _CALQUE_NOT_LIMITED_RE.finditer(text):
        head, tail = m.group("head"), " ".join(m.group("tail").split())
        if head.lower() in ("eelkõige", "eeskätt"):
            why = (f"'{tail}' on üleliigne lisand (inglise primarily, but not "
                   f"exclusively); '{head}' ütleb juba, et peale nimetatu on teisigi.")
        else:
            why = (f"'{tail}' on tõlkelaen (including, but not limited to); "
                   f"'{head}' ütleb juba, et loetelu pole lõplik.")
        add(m.start(), m.end(), why, f"{head} (ilma lisandita '{tail}')")

    # Imperatives synthesised for 'ära kõhkle ...-mast' in this text; past
    # _NUM_LOOKUPS distinct verbs the suggestion falls back to a fixed one.
    imperatives: dict[tuple[str, bool], str] = {}

    def readings(span) -> list[tuple[str, str, str]]:
        return [(a.lemma or "", a.partofspeech or "", a.form or "") for a in span.annotations]

    def lemmas(span) -> set[str]:
        return {lemma for lemma, _pos, _form in readings(span)}

    n = len(spans)
    for i, span in enumerate(spans):
        low = span.text.lower()
        nxt = spans[i + 1] if i + 1 < n else None

        # antud juhul / hetkel / olukorras / kontekstis, not the participle.
        if low == "antud" and nxt is not None and nxt.text.lower() in _CALQUE_ANTUD_NOUNS:
            prev = spans[i - 1] if i else None
            prev_low = prev.text.lower() if prev is not None else ""
            clause_after = (i + 3 < n and spans[i + 2].text == ","
                            and spans[i + 3].text.lower() in _CALQUE_ANTUD_CLAUSE_AFTER)
            participle = prev is not None and (
                _span_bits(prev)[0] in ("S", "P", "H")
                or prev_low in _CALQUE_ANTUD_PARTICIPLE_BEFORE
                or (clause_after and (prev_low in _OLEMA_FORMS_ET or prev_low.endswith("lt"))))
            if not participle:
                add(span.start, nxt.end,
                    "'antud' on toortõlge (vene данный, inglise given); eesti keeles "
                    "'see, kõnealune, nimetatud'. ÕS lubab seda, kuid omasõna on "
                    "selgem. Stiilisoovitus.",
                    _CALQUE_ANTUD_NOUNS[nxt.text.lower()])

        # adresseerima a problem, not a letter or question to someone.
        if "adresseerima" in lemmas(span):
            window = spans[max(0, i - 4):i + 5]
            if (any(_span_bits(w)[2] in _CALQUE_PROBLEM_NOUNS for w in window)
                    and not any(_span_bits(w)[1].endswith(" all") for w in window)
                    and not any(w.text.lower() == "ümber"
                                or (_span_bits(w)[0] == "S" and _head_lemma(w) in _CALQUE_MAIL_NOUNS)
                                for w in window)):
                add(span.start, span.end,
                    "'adresseerima' tähenduses 'probleemiga tegelema' on inglise "
                    "keele mõju (address); selgem on 'tegelema, lahendama, "
                    "käsitlema'. Stiilisoovitus.",
                    "tegelema / lahendama / käsitlema")

        # ära / ärge kõhelge ... võtmast (don't hesitate to).
        if (lemmas(span) & {"kõhklema", "kõhelema"}
                and any(form in ("o", "ge") for _l, _p, form in readings(span))
                and i and spans[i - 1].text.lower() in ("ära", "ärge")):
            for k in range(i + 1, min(n, i + 5)):
                if _is_punct(spans[k]):
                    break
                verb = next((lemma for lemma, pos, form in readings(spans[k])
                             if pos == "V" and form == "mast"), None)
                if verb is not None:
                    # The suggestion keeps the verb and the number of
                    # 'ära' / 'ärge': Ära kõhkle küsimast → Küsi julgelt.
                    plural = spans[i - 1].text.lower() == "ärge"
                    key = (verb, plural)
                    if key not in imperatives and len(imperatives) < _NUM_LOOKUPS:
                        imperatives[key] = _calque_imperative(verb, plural)
                    imperative = imperatives.get(key, "")
                    if imperative:
                        if spans[i - 1].text[:1].isupper():
                            imperative = imperative[:1].upper() + imperative[1:]
                        between = " ".join(s.text for s in spans[i + 1:k])
                        plain = " ".join(w for w in (imperative, "julgelt", between) if w)
                    else:
                        plain = "Võtke julgelt ühendust" if plural else "Võta julgelt ühendust"
                    add(spans[i - 1].start, spans[k].end,
                        "'ärge kõhelge ühendust võtmast' on tõlkelaen (don't hesitate "
                        "to contact); eesti keeles 'võtke julgelt ühendust'.",
                        plain)
                    break

        # (kiri) leiab teid hästi (this email finds you well).
        if ("leidma" in lemmas(span) and i + 2 < n
                and spans[i + 1].text.lower() in ("teid", "sind")
                and spans[i + 2].text.lower() in ("hästi", "heas", "tervena")
                and not (i + 3 < n and spans[i + 3].text.lower() == "üles")):
            # The letter noun is the subject of leidma: nominative, in the
            # same clause (no punctuation or conjunction between), at most
            # three words back (see kiri ka seekord leiab).
            for k in range(i - 1, max(-1, i - 4), -1):
                if _is_punct(spans[k]) or any(pos == "J" for _l, pos, _f in readings(spans[k])):
                    break
                if any(lemma in _CALQUE_LETTER_NOUNS and form in ("sg n", "pl n")
                       for lemma, _p, form in readings(spans[k])):
                    add(span.start, spans[i + 2].end,
                        "'kiri leiab teid hästi' on tõlkelaen (I hope this email "
                        "finds you well); alusta otse asjast või kirjuta 'Loodan, et "
                        "teil läheb hästi'.",
                        "jäta lause ära / Loodan, et teil läheb hästi.")
                    break

        # tänan teid teie ... eest (thank you for your).
        if ("tänama" in lemmas(span) and i + 3 < n
                and (spans[i + 1].text.lower(), spans[i + 2].text.lower())
                in (("teid", "teie"), ("sind", "sinu"))
                and _calque_thanks_for_your(spans, i + 3)):
            add(span.start, spans[i + 2].end,
                "'tänan teid teie ... eest' kordab omanikku inglise 'thank you for "
                "your' eeskujul; eesti keeles piisab 'tänan teid ... eest'.",
                f"{span.text} {spans[i + 1].text} (ilma sõnata '{spans[i + 2].text}')")

        # vaatan edasi koostööle (look forward to).
        if "vaatama" in lemmas(span) and nxt is not None and nxt.text.lower() == "edasi":
            for k in range(i + 2, min(n, i + 4)):
                if _is_punct(spans[k]):
                    break
                if any(pos == "S" and form in ("sg all", "pl all")
                       and lemma.lower().endswith(_CALQUE_LOOK_FORWARD_NOUNS)
                       for lemma, pos, form in readings(spans[k])):
                    add(span.start, spans[k].end,
                        "'vaatan edasi millelegi' on tõlkelaen (look forward to); "
                        "eesti keeles 'ootan huviga'.",
                        "ootan huviga + osastav (nt 'ootan huviga meie koostööd')")
                    break

        # Palun leidke manuses (please find attached).
        if (low == "palun" and nxt is not None
                and any(lemma == "leidma" and form in ("ge", "o") for lemma, _p, form in readings(nxt))):
            # The attachment word right after the verb: 'Palun leidke
            # aega lisatud küsimustikule vastamiseks' is not the calque.
            k = i + 2
            if (k + 1 < n and spans[k].text.lower() == "kaasa"
                    and spans[k + 1].text.lower() == "lisatud"):
                k += 1
            att = spans[k] if k < n else None
            if (att is not None
                    and (att.text.lower() in _CALQUE_ATTACHED
                         or any(lemma == "manus" and form in _CALQUE_MANUS_FORMS
                                for lemma, _p, form in readings(att)))
                    and not _calque_find_is_search(spans, i, k)):
                add(span.start, att.end,
                    "'Palun leidke manuses' on tõlkelaen (please find attached); "
                    "eesti keeles 'Manuses on ...', 'Saadan manuses ...' või "
                    "'Kirjale on lisatud ...'.",
                    "Manuses on ... / Saadan manuses ...")
    return issues


def _check_officialese(text: str) -> dict:
    """Kantseliit diagnostic for NON-legal Estonian: reports, academic
    prose, R&D/grant paperwork, business writing.

    check_legalese exists but is scoped to statutes: on a real Estonian
    R&D report paragraph it returned zero issues, because its filler
    lexicon is legal-specific and its length gate (34 words) sits above
    where Estonian prose actually becomes unreadable. This tool measures
    what makes such text heavy:

    - nominalisation (-mine verbal nouns) and overall noun/verb density,
      the classic 'nimisõnastiil'
    - umbisikuline tegumood, correctly counted (see _impersonal_voice)
    - clause stacking per sentence, the 'mille käigus … ning …' pile-up
      a raw word count misses
    - long sentences, on an Estonian-calibrated gate
    - administrative filler with plain equivalents, 'läbi viima' in the
      conduct sense (_labi_viima_issues) among it
    - 'Küsin täpsemalt, kas …' in letters (_kusin_tapsemalt_issue)

    Precision-first, like its legal sibling: absence of flags is not proof
    the text is plain.
    """
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["sentences", "morph_analysis"])
    spans = list(t.morph_analysis)

    issues: list[dict] = []

    # --- 1. Nominalisation: -mine verbal nouns, with the verb to swap in
    # (see _mine_verbs; omitted when no real verb is found).
    nominalisations: list[dict] = []
    mine_parts: dict[str, list[str]] = {}
    for k, span in enumerate(spans):
        pos, _form, lemma = _span_bits(span)
        if pos != "S" or not lemma.endswith("mine") or len(lemma) < 7:
            continue
        low = span.text.lower()
        # -misi adverbs: vaikimisi (by default), kiirustamisi.
        if low in _MISI_ADVERBS_ET or (low.endswith("misi") and any(
                p == "D" for _l, p, _f in _guessed_readings(span.text))):
            continue
        parts = _root_parts(span)
        if lemma in _MINE_LEXICALISED_ET or (parts and parts[-1] in _MINE_LEXICALISED_ET):
            continue
        first = span
        # 'läbi viimine' is the split spelling of 'läbiviimine' (EKI
        # reegel 32 Märkus), so it takes the same verb, not bare 'viima';
        # not after a genitive or a source ('metsa läbi viimine').
        if (lemma == "viimine" and k and spans[k - 1].text.lower() == "läbi"
                and not (k >= 2 and (_labi_local(spans[k - 2])
                                     or _labi_possessor(spans[k - 2])))):
            first, lemma, parts = spans[k - 1], "läbiviimine", ["läbi", "viimine"]
        mine_parts.setdefault(lemma, parts)
        nominalisations.append({
            "word": text[first.start:span.end],
            "lemma": lemma,
            "position": first.start,
        })
    mine_verbs = _mine_verbs(mine_parts)
    for nom in nominalisations:
        if mine_verbs.get(nom["lemma"]):
            nom["verb"] = mine_verbs[nom["lemma"]]

    word_count = sum(1 for s in spans if s.text and s.text[0].isalpha())
    noun_count = sum(1 for s in spans if _span_bits(s)[0] == "S")
    imp = _impersonal_voice(spans)
    verb_count = imp["total_verbs"]
    noun_verb_ratio = (noun_count / verb_count) if verb_count else 0.0
    nom_per_100 = (len(nominalisations) * 100 / word_count) if word_count else 0.0

    if nom_per_100 >= _OFFICIALESE_NOMINALISATION_PER_100:
        issues.append({
            "position": nominalisations[0]["position"] if nominalisations else 0,
            "rule": "nominalisation",
            "rule_estonian": "nimisõnastumine ehk nominalisatsioon",
            "explanation": (
                f"Tekstis on {len(nominalisations)} mine-tuletist "
                f"{word_count} sõna kohta. Tegevust väljendav nimisõna "
                f"muudab lause raskeks. Kasuta tegusõna."
            ),
            "suggestion": ", ".join(list(dict.fromkeys(
                f"{n['lemma']} → {n['verb']}" for n in nominalisations if "verb" in n
            ))[:5]) or "kasuta tegusõna",
        })

    if noun_verb_ratio >= _OFFICIALESE_NOUN_VERB_RATIO:
        issues.append({
            "position": 0,
            "rule": "noun-density",
            "rule_estonian": "nimisõnade kuhjumine",
            "explanation": (
                f"Nimisõnu on {noun_count}, tegusõnu {verb_count} "
                f"(suhe {noun_verb_ratio:.2f}). Kui suhe ületab "
                f"{_OFFICIALESE_NOUN_VERB_RATIO}, loeb tekst end raskelt."
            ),
            "suggestion": "muuda osa nimisõnu tegusõnadeks",
        })

    if imp["ratio"] >= _OFFICIALESE_IMPERSONAL_RATIO and verb_count >= 4:
        issues.append({
            "position": 0,
            "rule": "impersonal-voice",
            "rule_estonian": "umbisikuline tegumood",
            "explanation": (
                f"Umbisikulises tegumoes on {imp['passive_count']}/"
                f"{verb_count} tegusõna "
                f"({round(imp['ratio'] * 100, 1)}%). Aruandekeeles on see "
                f"kõige selgem kantseliidi tunnus. Nimeta tegija."
            ),
            "suggestion": "kirjuta isikulises tegumoes, nt 'koguti' → 'kogusime'",
        })

    # --- 2. Per-sentence: clause stacking, length, 'Küsin täpsemalt, kas'.
    sentences = _sentence_ranges(t, spans)
    prev_spans: list = []
    for sent, (lo, hi) in zip(t.sentences, sentences, strict=True):
        s_spans = spans[lo:hi]
        n_words = sum(1 for s in s_spans if s.text and s.text[0].isalpha())
        subs = [s.text for s in s_spans if s.text.lower() in _SUBORDINATORS_ET]
        preview = sent.enclosing_text
        preview = (preview[:60] + "…") if len(preview) > 60 else preview

        if len(subs) >= _OFFICIALESE_CLAUSE_STACK:
            issues.append({
                "phrase": preview,
                "position": sent.start,
                "rule": "clause-stacking",
                "rule_estonian": "kõrvallausete kuhjumine",
                "explanation": (
                    f"Lauses on {len(subs)} kõrvallause algust "
                    f"({', '.join(subs)}). Jaga lause mitmeks."
                ),
                "suggestion": "jaga lause lühemateks lauseteks",
            })
        elif n_words >= _OFFICIALESE_LONG_SENTENCE_WORDS:
            issues.append({
                "phrase": preview,
                "position": sent.start,
                "rule": "long-sentence",
                "rule_estonian": "liiga pikk lause",
                "explanation": (
                    f"Lauses on {n_words} sõna (piir "
                    f"{_OFFICIALESE_LONG_SENTENCE_WORDS})."
                ),
                "suggestion": "jaga lause lühemateks lauseteks",
            })

        found = _kusin_tapsemalt_issue(text, s_spans, prev_spans)
        if found:
            issues.append(found)
        prev_spans = s_spans

    # --- 3. Administrative filler (lemma-exact + fixed phrase pairs), and
    # 'läbi viima' in all its forms.
    issues.extend(_labi_viima_issues(text, spans, sentences))
    issues.extend(_calque_issues(text, spans))
    for i, span in enumerate(spans):
        pos, _form, lemma = _span_bits(span)
        low = span.text.lower()

        filler = _OFFICIALESE_LEMMAS_ET.get(lemma) or _OFFICIALESE_SURFACE_ET.get(low)
        if filler:
            plain, why = filler
            issues.append({
                "word": span.text,
                "position": span.start,
                "rule": "officialese-filler",
                "rule_estonian": "kantseliitlik täitesõna",
                "explanation": why,
                "suggestion": plain,
            })

        if i + 1 < len(spans):
            pair = (low, spans[i + 1].text.lower())
            if pair in _OFFICIALESE_PHRASES_ET:
                plain, why = _OFFICIALESE_PHRASES_ET[pair]
                issues.append({
                    "phrase": text[span.start:spans[i + 1].end],
                    "position": span.start,
                    "rule": "officialese-phrase",
                    "rule_estonian": "kantseliitlik väljend",
                    "explanation": why,
                    "suggestion": plain,
                })

        # 'X-i poolt tehtud': the passive-agent calque. Only when a
        # genitive noun precedes, so 'hääletas poolt' (in favour) is safe.
        if low == "poolt" and i > 0:
            prev_pos, prev_form, _prev_lemma = _span_bits(spans[i - 1])
            if prev_pos == "S" and prev_form in ("sg g", "pl g"):
                issues.append({
                    "phrase": text[spans[i - 1].start:span.end],
                    "position": spans[i - 1].start,
                    "rule": "poolt-calque",
                    "rule_estonian": "'poolt'-tarind",
                    "explanation": (
                        "'X-i poolt tehtud' on tõlkelaen. Eesti keeles "
                        "piisab omastavast: 'mudeli loodud', mitte "
                        "'mudeli poolt loodud'."
                    ),
                    "suggestion": f"{spans[i - 1].text} (ilma sõnata 'poolt')",
                })

    issues.sort(key=lambda d: d.get("position", 0))

    return {
        "issues": issues,
        "metrics": {
            "word_count": word_count,
            "noun_count": noun_count,
            "verb_count": verb_count,
            "noun_verb_ratio": round(noun_verb_ratio, 2),
            "nominalisations": nominalisations,
            "nominalisations_per_100_words": round(nom_per_100, 2),
            # verb_count above already carries imp["total_verbs"].
            "impersonal_voice": {
                k: v for k, v in imp.items() if k != "total_verbs"
            },
        },
        "summary_estonian": (
            f"Leiti {len(issues)} kantseliidikohta. Nimisõnade ja tegusõnade "
            f"suhe on {noun_verb_ratio:.2f}, umbisikulises tegumoes "
            f"{round(imp['ratio'] * 100, 1)}% tegusõnadest."
            if issues else
            "Selget kantseliiti ei tuvastatud."
        ),
    }


class _OfficialeseResult(TypedDict, total=False):
    """Output of check_officialese: kantseliit issues + density metrics."""
    issues: list[dict]
    metrics: dict
    summary_estonian: str


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian officialese (kantseliit in non-legal prose)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_officialese(text: Annotated[str, Field(description="Estonian non-legal prose (report, academic, business).")]) -> _OfficialeseResult:
    """Flag Estonian kantseliit in reports, academic and business prose;
    statutes and contracts go to check_legalese, which protects terms of
    art.

    Issue rules: `impersonal-voice` (ratio 0.4+, 4+ verbs),
    `nominalisation` (4+ -mine nouns per 100 words, positioned at the
    first), `noun-density` (noun/verb 2.5+, weakest: weigh it last),
    `clause-stacking` (3+ subordinate-clause openers in a sentence),
    `long-sentence` (25+ words), `officialese-filler`,
    `officialese-phrase`, `poolt-calque`, `kusin-tapsemalt`,
    `calque-phrase`.
    `impersonal-voice` and `noun-density` are document-wide (position 0).
    'läbi viima' in the conduct sense is an `officialese-phrase` in any
    form (viidi läbi, on läbi viinud, viis uuringu läbi), and
    läbiviimine, läbiviija, läbiviidud, läbiviidav, läbi viimine an
    `officialese-filler`, suggesting korraldama / tegema (pidama for a
    meeting) in the same form, with the words between kept (viis uuringu
    läbi → korraldas uuringu); routes and threaded objects (Rada viib
    läbi metsa, Kaabel tuleb läbi viia seinast, kaabli läbiviimine), time
    spans (päev läbi), postpositions, the numeral viis and the fugue term
    are skipped, and the 'läbi suruma' sense is not told apart. A thing
    carried through with no route word (Kohver viidi läbi) is still
    flagged. `kusin-tapsemalt` is a
    sentence-initial 'Küsin/Küsime täpsemalt, kas …' in a letter; a plan
    to ask someone else matches too unless a reporting-back verb follows
    (…, ja annan teada; …, ja võtan teiega ühendust), and quoted speech,
    a dialogue-dash line and a quoted e-mail line ('>') are skipped.
    Both are style hints, not errors.
    `calque-phrase` is a translated stock phrase of letters: sealhulgas,
    kuid mitte ainult (and eelkõige, kuid mitte ainult, where only the
    tail goes); antud juhul / hetkel / olukorras, not the participle (on
    antud olukorras, kus ...); adresseerima a problem, not a mail item;
    ärge kõhelge ... võtmast, rewritten with its own verb; kiri leiab
    teid hästi; tänan teid teie ... eest; vaatan edasi koostööle, with an
    awaited event (not vaatas edasi merele); Palun leidke manuses, not a
    search instruction (Palun leidke manusest oma nimi). Leiate manusest
    and läbi e-posti are accepted Estonian and not flagged.
    `metrics.nominalisations` gives each -mine noun's verb, a particle
    verb written apart (läbivaatamine → läbi vaatama, tagaajamine → taga
    ajama), a noun + verb compound by its head (riskihindamine → hindama,
    sisuloomine → looma), and no `verb` when none is known. The split
    'läbi viimine' counts as läbiviimine. Nouns lexicalised as things
    (teadmised, pakkumine, kohtumine, ettevõtmine and their compounds)
    and -misi adverbs (vaikimisi) are not counted.

    Precision-first starter lexicons: no issues is not proof the text is
    plain.
    """
    return _check_officialese(text)


# ---------------------------------------------------------------------------
# Translationese (check_translationese): English sentence structure and
# English habits of form carried into Estonian. Every rule is one that EKI
# or an author EKI publishes rules on, and each keeps quiet where the same
# surface is correct Estonian. Sources, by rule:
#
#   oma-possessive        EKK 1997, M 8 Asesõnad: a possessor that is the
#                         subject takes oma; the personal genitive is for
#                         a possessor that is not (Ma andsin Jürile tema
#                         artikli tagasi). ÕS 2025 'oma'.
#   pronoun-after-verb    EKI teatmik, Tekstisidusus (2026): -me me, -te te.
#   subject-pronoun       EKI teatmik, Kirjutaja mina (2026): käsitlen,
#                         mitte mina käsitlen.
#   verb-second           EKK 1997, SÜ 66 Lause sõnajärg, condition 3: after
#                         a fronted subordinate clause the verb comes next
#                         (Kui saabub kevad, muutub ka meeleolu paremaks).
#   saama-future          EKK 1997, SÜ 27: discouraged, unsuitable with
#                         active verbs.
#   maks-*                EKK 1997, SÜ 100 ("maksitamine ... mõjub keelelise
#                         eputamisena"); Dreving in "Kas eurotekstides on
#                         kohta ilusale ja selgele keelele?" (EKI 2009): a
#                         noun's maks form for a da-infinitive is "lausa
#                         vale".
#   mitte-des             EKK 1997, SÜ 100: the negation of des is mata.
#   tanu-negative         Mäekivi, Emakeelne eurokeel (EKI 2013), on the
#                         English 'thanks to'; AMSS 'tänu'.
#   english-phrase        AMSS 'minu', 'oma'; Kask, Oma Keel 1/2022.
#   greeting-punctuation, closing-comma
#                         EKI keelenõuvakk "Vahemärgid kirja alguses ja
#                         lõpus" (2026), EKI teatmik, Koma (2026).
#   serial-comma          EKI teatmik, Koma (2026).
#   title-case            EKI teatmik, Pealkiri (2022); Microsoft Estonian
#                         Style Guide (2024).
#
# Left alone because ÕS 2025 or the EKI 2026 dictionary accepts them:
# aset leidma, rolli mängima, mis puudutab, X aastat vana, rohkem kui,
# päeva lõpuks, kursis hoidma, hea meel + da, teiste sõnadega, läbi for a
# means; also a pronoun subject after an adverb (Eile ma läksin: EKK calls
# it "üsna tavaline") and the comma after a fronted des phrase (Kasutades
# seda tööriista, ...), which EKI requires.
# ---------------------------------------------------------------------------

# The persons a verb form marks, with the imperatives that _FORM_PERSONS
# leaves out: o (2sg), ge (2pl), gem (1pl).
_TR_FORM_PERSONS: dict[str, frozenset[str]] = {
    **_FORM_PERSONS, "o": frozenset({"2sg"}), "ge": frozenset({"2pl"}),
    "gem": frozenset({"1pl"}),
}

# Genitive personal pronouns that stand for oma when they name the
# subject's own thing. me and te are left out: they are also nominatives.
_TR_POSSESSIVES: dict[str, str] = {
    "minu": "1sg", "mu": "1sg", "sinu": "2sg", "su": "2sg", "meie": "1pl", "teie": "2pl",
}
_TR_SUBJECT_PRONOUNS: dict[str, str] = {
    "ma": "1sg", "mina": "1sg", "sa": "2sg", "sina": "2sg", "ta": "3sg", "tema": "3sg",
    "me": "1pl", "meie": "1pl", "te": "2pl", "teie": "2pl", "nad": "3pl", "nemad": "3pl",
}

# Sentence adverbials that keep minu (AMSS 'minu': minu arvates / meelest /
# arust; EKI teatmik, Üldlaiend: minu teada).
_TR_POSSESSIVE_ADVERBIALS: frozenset[str] = frozenset(
    {"arvates", "meelest", "arust", "teada", "poolest"})

# Participles whose genitive is their agent, not a possessor: meie
# hüvitatav osa is the part we reimburse.
_TR_PARTICIPLE_ENDINGS: tuple[str, ...] = ("tav", "dav", "tud", "dud", "nud", "tu", "matu")

# A short subject pronoun only: a long one may be a stressed, contrastive
# topic (Kui sa tuled, mina ootan sind kodus).
_TR_SHORT_PRONOUNS: frozenset[str] = frozenset({"ma", "sa", "ta", "me", "te", "nad"})

# Verbs after which a pronoun that repeats the ending is redundant (EKI:
# Pöördelõppu kordava asesõna saab tuua öeldise ette või lausest välja
# jätta).
_TR_PLURAL_FORMS: dict[str, frozenset[str]] = {
    "me": frozenset({"me", "sime", "ksime", "nuksime"}),
    "te": frozenset({"te", "site", "ksite", "nuksite"}),
}

# Subordinators that can open a sentence, and the second word the
# two-word ones need (Enne kui, Juhul kui, Olgugi et).
_TR_FRONT_OPENERS: dict[str, frozenset[str]] = {
    "kui": frozenset(), "kuna": frozenset(), "kuigi": frozenset(), "ehkki": frozenset(),
    "kuni": frozenset(), "et": frozenset(), "kuivõrd": frozenset(),
    "enne": frozenset({"kui"}), "pärast": frozenset({"kui"}), "niipea": frozenset({"kui"}),
    "juhul": frozenset({"kui"}), "olgugi": frozenset({"et"}),
}

# After a comma inside the fronted part these open another clause of it;
# any other word starts the main clause.
_TR_EMBED_OPENERS: frozenset[str] = frozenset(
    {"et", "kui", "kuna", "kuigi", "ehkki", "kuni", "kas", "ja", "ning", "või", "ega", "nagu", "sest"})

# Determiners and quantifiers that open a noun phrase but are not its head.
_TR_DETERMINERS: frozenset[str] = frozenset({
    "see", "need", "too", "nood", "iga", "kogu", "mõni", "mitu", "üks", "kõik", "selline",
    "niisugune", "seesama",
})

# Present indicative forms of saama: the future is saab olema, saavad
# toimuma. sai õppima (got to study) is past and is left alone.
_TR_SAAMA_PRESENT: frozenset[str] = frozenset({"n", "d", "b", "me", "te", "vad"})

# hakkama saama (to cope) and minema saama (to get away) are idioms.
_TR_SAAMA_IDIOMS: frozenset[str] = frozenset({"hakkama", "minema"})

# Nouns that take a da-infinitive (ettepanek võtta, võimalus osaleda);
# matched on the lemma or the compound head (muudatusettepanek).
_TR_DA_NOUNS: frozenset[str] = frozenset("""
    ettepanek võimalus soov otsus plaan õigus kohustus vajadus luba nõue kavatsus
    katse valmidus valmisolek oskus julgus aeg põhjus tahtmine tahe huvi ülesanne
    palve käsk korraldus kutse idee püüd püüe soovitus kokkulepe lubadus volitus
    pädevus kohus komme harjumus vabadus tung
""".split())

# Verbs of motion and starting that take a ma-infinitive (sõitis Pärnu
# lepingut sõlmima), where a maks form is "iseäranis mõttetu" (EKK).
_TR_MA_VERBS: frozenset[str] = frozenset("""
    minema tulema sõitma jooksma kiirustama ruttama asuma hakkama jääma saatma
    kutsuma lähetama kogunema istuma lendama suunduma pöörduma
""".split())

# Causes nobody is grateful for (Mäekivi: "Tänu ressursside piiratusele"
# → "Ressursside piiratuse tõttu"); the lemma or the compound head.
_TR_NEGATIVE_CAUSES: frozenset[str] = frozenset("""
    õnnetus kahju kriis haigus viga häire ebaõnnestumine maavärin torm kaotus rike
    tõrge probleem puudus puudujääk hilinemine streik pankrot sõda tulekahju üleujutus
    põud katkestus ummik avarii inflatsioon kärbe piiratus paanika vigastus surm
    halvenemine langus vähenemine rünnak pettus vargus korruptsioon hooletus lohakus
    eksitus eksimus pandeemia epideemia viirus katastroof häving nappus defitsiit
    võlg võlgnevus trahv konflikt segadus viivitus seisak koondamine tuisk libedus
    ebaõnn õnnetusjuhtum purunemine leke kahjustus
""".split())

# Phrases translated word for word, each with its Estonian and the reason.
_TR_PHRASES: tuple[tuple[re.Pattern, str, str], ...] = (
    (re.compile(r"\bminu\s+arvamusel\b", re.IGNORECASE), "minu arvates / minu meelest",
     "'Minu arvamusel' on tõlkelaen (in my opinion); eesti keeles 'minu arvates'."),
    (re.compile(r"\bminu\s+vabandused\b", re.IGNORECASE), "vabandan",
     "'Minu vabandused' (my apologies) on otsetõlge; eesti keeles 'vabandan'."),
    (re.compile(r"\btunne\s+oma\s+klienti\b", re.IGNORECASE), "klienditundmise põhimõte",
     "'Tunne oma klienti' (know your customer) on otsetõlge; põhimõtte nimi on "
     "'klienditundmise põhimõte'."),
)

# Letter formulas. A closing takes no comma before the name, on one line
# or two (Lugupidamisega / Mari Tamm); a salutation ends in ! or in
# nothing, and the letter goes on with a capital (EKI 2026).
_TR_CLOSINGS: tuple[str, ...] = (
    "lugupidamisega", "austusega", "tervitades", "tervitustega", "parimate soovidega",
    "heade soovidega", "parimat", "ette tänades", "tänades", "tänuga", "kõike head",
    "kõike paremat", "soojade soovidega", "sõbralikult", "meeldivat koostööd soovides",
    "edu soovides",
)
_TR_CLOSING_RE = re.compile(
    r"^[ \t]*(?P<f>" + "|".join(map(re.escape, _TR_CLOSINGS)) + r")[ \t]*,[ \t]*"
    r"(?P<rest>[^\n,.!?]{0,60})$", re.IGNORECASE | re.MULTILINE)
_TR_GREETING_RE = re.compile(
    r"^[ \t]*(?P<g>(?:tere|hei|tervist|tšau|hommikust|hea|head|lugupeetud|austatud|kallis|"
    r"kallid|armas|armsad)\b(?:,[ \t]+)?[^\n,.!?:;]{0,60}?)[ \t]*,[ \t]*$",
    re.IGNORECASE | re.MULTILINE)
# Tere and a name with no comma between (Tere, Mari! is an address, üte);
# Hea kolleeg Tiina! is not one and takes none.
_TR_NONSPACE_RE = re.compile(r"\S")
_TR_TERE_NAME_RE = re.compile(
    r"^[ \t]*(?P<g>tere hommikust|tere päevast|tere õhtust|tere|hei|tervist|tšau)[ \t]+"
    r"(?P<n>(?-i:[A-ZÕÄÖÜŠŽ])[\w\-]+)", re.IGNORECASE | re.MULTILINE)
# What Hea / Kallis must be followed by to open a letter (Hea klient), so
# that Hea meel, ... is left alone. A capitalised word (a name) counts too.
_TR_ADDRESS_NOUNS: frozenset[str] = frozenset("""
    kolleeg kolleegid klient kliendid sõber sõbrad partner partnerid koostööpartner
    koostööpartnerid lugeja lugejad kasutaja kasutajad liige liikmed külaline külalised
    härra proua preili meeskond pere perekond õpetaja lapsevanem lapsevanemad tudeng
    tudengid õpilane õpilased osaleja osalejad kandidaat kandidaadid kodanik kodanikud
""".split())

# A list item is a short phrase with none of these in it; nor does it open
# with an inserted remark (Korjasin seeni, nagu riisikaid ja pilvikuid, ning ...).
_TR_LIST_CONJUNCTIONS: frozenset[str] = frozenset({"ja", "ning", "või", "ega"})
_TR_KIIL_OPENERS: frozenset[str] = frozenset({
    "nagu", "näiteks", "nt", "sealhulgas", "sh", "eriti", "eelkõige", "peamiselt", "ehk",
    "st", "eeskätt", "muuhulgas",
})
# After ja these add an emphasised afterthought, which may take a comma.
_TR_AFTERTHOUGHT: frozenset[str] = frozenset({"ka", "isegi", "eriti", "veel", "samuti", "siis"})
_TR_NOMINAL_POS: frozenset[str] = frozenset("SHAPNOCU")
# What a possessive pronoun stands before: a noun, adjective or numeral.
_TR_HEAD_POS: frozenset[str] = frozenset("SANOCU")

# Title case: a heading of 2 to 10 words where most words after the first
# are capitalised. Names, institutions (Maksu- ja Tolliamet), the polite
# Teie and a word after a colon or a quotation mark keep their capital.
_TR_HEADING_WORD_RE = re.compile(r"[^\W\d_][\w'’\-]*")
_TR_INSTITUTION_HEADS: tuple[str, ...] = tuple("""
    amet ministeerium instituut ülikool kool keskus inspektsioon kantselei kohus liit
    selts fond pank kogu muuseum teater haigla büroo agentuur nõukogu komisjon ühing
    koda teenistus akadeemia gümnaasium kolledž klubi kirik vald linn maakond vabariik
    sihtasutus kontsern grupp
""".split())
_TR_POLITE: frozenset[str] = frozenset({
    "teie", "teid", "teile", "teil", "teilt", "sina", "sinu", "sind", "sulle", "sul", "sult",
})
_TR_SENTENCE_OPENERS: str = ':"„“«(!?.'
_TR_FUNCTION_POS: frozenset[str] = frozenset("VPJKD")
# Heading lines a text gets checked in; each costs a few lexicon lookups.
_TR_HEADING_LINES = 200


def _tr_readings(span) -> list[tuple[str, str, str]]:
    return [(a.lemma or "", a.partofspeech or "", a.form or "") for a in span.annotations]


def _tr_persons(spans: list, k: int) -> frozenset[str] | None:
    """The persons spans[k] marks as a finite verb or an imperative; None
    when it marks none. A verb after ei or pole is the negative stem (ei
    tee), not an imperative, and palun is the politeness word."""
    if spans[k].text.lower() == "palun":
        return None
    out: set[str] = set()
    for _lemma, pos, form in _tr_readings(spans[k]):
        persons = _TR_FORM_PERSONS.get(form) if pos == "V" else None
        if persons is None:
            return None
        out |= persons
    if k and spans[k - 1].text.lower() in ("ei", "pole"):
        return None
    return frozenset(out) or None


def _tr_clauses(spans: list, lo: int, hi: int) -> list[tuple[int, int]]:
    """One sentence cut into clause-sized pieces: at punctuation, at
    conjunctions and subordinators, and at relative words."""
    out, start = [], lo
    for k in range(lo, hi):
        if (spans[k].text in _CLAUSE_BOUNDARY_MARKS or spans[k].text.lower() in _CLAUSE_SCAN_STOP
                or _relative_word(spans[k])):
            if start < k:
                out.append((start, k))
            start = k + 1
    if start < hi:
        out.append((start, hi))
    return out


def _tr_clause_person(spans: list, a: int, b: int) -> str | None:
    """The one person the clause spans[a:b] is in: every person-marked verb
    agrees on it, and so does any subject pronoun. None when that is not
    certain (no such verb, verbs in different persons, a sid form with no
    pronoun to choose between 2sg and 3pl)."""
    verbs = [p for k in range(a, b) if (p := _tr_persons(spans, k))]
    if not verbs:
        return None
    subjects = {_TR_SUBJECT_PRONOUNS[spans[k].text.lower()] for k in range(a, b)
                if spans[k].text.lower() in _TR_SUBJECT_PRONOUNS
                and any(pos == "P" and form in ("sg n", "pl n")
                        for _l, pos, form in _tr_readings(spans[k]))}
    if all(len(v) == 1 for v in verbs) and len(set(verbs)) == 1:
        person = next(iter(verbs[0]))
        return None if subjects - {person} else person
    if len(subjects) == 1:
        person = next(iter(subjects))
        return person if all(person in v for v in verbs) else None
    return None


def _tr_oma_issues(text: str, spans: list, lo: int, hi: int) -> list[dict]:
    """oma-possessive: Palun saatke meile teie andmed → oma andmed."""
    issues: list[dict] = []
    for a, b in _tr_clauses(spans, lo, hi):
        person = _tr_clause_person(spans, a, b)
        if person is None:
            continue
        for k in range(a, b - 1):
            low = spans[k].text.lower()
            if _TR_POSSESSIVES.get(low) != person or not any(
                    pos == "P" and form in ("sg g", "pl g") for _l, pos, form in _tr_readings(spans[k])):
                continue
            nxt = spans[k + 1]
            readings = _tr_readings(nxt)
            if (nxt.text.lower() in _TR_POSSESSIVE_ADVERBIALS or nxt.text.lower() == "oma"
                    or not any(pos in _TR_HEAD_POS for _l, pos, _f in readings)
                    or any(pos in ("K", "V", "D") for _l, pos, _f in readings)
                    or any(pos == "A" and lemma.endswith(_TR_PARTICIPLE_ENDINGS)
                           for lemma, pos, _f in readings)):
                continue
            # A quoted or capitalised Minu / Meie inside a sentence is a
            # title or a name (raamat „Minu Eesti“); Teie and Sinu are a
            # letter's polite capital.
            if k > lo and spans[k - 1].text in _QUOTE_MARKS:
                continue
            if spans[k].text[:1].isupper() and k > lo and low not in ("teie", "sinu"):
                continue
            issues.append({
                "phrase": text[spans[k].start:nxt.end],
                "position": spans[k].start,
                "rule": "oma-possessive",
                "rule_estonian": "omastav asesõna oma",
                "explanation": (f"Kui omanik on lause alus, on eesti keeles 'oma' või ei "
                                f"midagi, mitte '{spans[k].text}' (inglise my, your)."),
                "suggestion": f"oma {nxt.text} / {nxt.text}",
            })
    return issues


def _tr_pronoun_after_verb_issues(text: str, spans: list, lo: int, hi: int) -> list[dict]:
    """pronoun-after-verb: lähtume me → lähtume. Not in a question (Olete te
    kindel?), and not before a comma."""
    issues: list[dict] = []
    if spans[hi - 1].text == "?":
        return issues
    for k in range(lo, hi - 2):
        pronoun = spans[k + 1].text.lower()
        forms = _TR_PLURAL_FORMS.get(pronoun)
        readings = _tr_readings(spans[k])
        if (forms is None or not readings or spans[k + 2].text in _CLAUSE_BOUNDARY_MARKS
                or not all(pos == "V" and form in forms for _l, pos, form in readings)):
            continue
        verb = spans[k].text
        issues.append({
            "phrase": text[spans[k].start:spans[k + 1].end],
            "position": spans[k].start,
            "rule": "pronoun-after-verb",
            "rule_estonian": "pöördelõppu kordav asesõna",
            "explanation": (f"Pöördelõpp näitab tegijat juba; '{spans[k + 1].text}' tegusõna "
                            f"järel kordab seda ('lähtume me' → 'lähtume')."),
            "suggestion": f"{verb} / {spans[k + 1].text} {verb[:1].lower() + verb[1:]}",
        })
    return issues


def _tr_subject_np(spans: list, j: int, hi: int) -> tuple[int, str] | None:
    """A nominative noun phrase of at most four words from spans[j]: the
    index after its noun, and the noun's number ('sg' or 'pl'). Modifiers
    are nominatives and genitives (meie uus süsteem, ettevõtte juht)."""
    k = j
    while k < hi and k - j < 4:
        readings = _tr_readings(spans[k])
        if not readings or _is_punct(spans[k]) or any(pos == "V" for _l, pos, _f in readings):
            return None
        if any(pos == "S" and form in ("sg n", "pl n") for _l, pos, form in readings):
            nxt = _tr_readings(spans[k + 1]) if k + 1 < hi else []
            if (nxt and any(pos in ("S", "H") and form in ("sg n", "pl n") for _l, pos, form in nxt)
                    and not any(pos == "V" for _l, pos, _f in nxt)):
                k += 1  # a noun before another nominal qualifies it
                continue
            plural = any(pos == "S" and form == "pl n" for _l, pos, form in readings)
            return k + 1, "pl" if plural else "sg"
        if not any(pos in _TR_NOMINAL_POS and form.endswith((" n", " g"))
                   for _l, pos, form in readings):
            return None
        k += 1
    return None


def _tr_verb_second_issue(text: str, spans: list, lo: int, hi: int) -> dict | None:
    """verb-second: Kui tellimus on kinnitatud, me saadame arve → saadame.
    The sentence opens with a subordinate clause; its comma is followed by
    a short subject pronoun or a common-noun subject and then the finite
    verb. Not in a question, not after siis (Kui ..., siis me ...), not
    with a negated verb, and not for a parenthetical (..., ma arvan, ...)."""
    k = lo
    while k < hi and (spans[k].text in _QUOTE_MARKS or spans[k].text in ("-", "*", "•")):
        k += 1
    opener = spans[k].text.lower() if k < hi else ""
    if opener not in _TR_FRONT_OPENERS or spans[hi - 1].text == "?":
        return None
    second = _TR_FRONT_OPENERS[opener]
    if second and not any(s.text.lower() in second for s in spans[k + 1:k + 3]):
        return None
    comma, predicate = None, False
    for m in range(k + 1, hi):
        word = spans[m].text
        if word == ",":
            if m + 1 < hi and (spans[m + 1].text.lower() in _TR_EMBED_OPENERS
                               or _relative_word(spans[m + 1])):
                continue
            comma = m
            break
        if word in _CLAUSE_BOUNDARY_MARKS:
            return None
        predicate = predicate or any(
            pos == "V" and (form in _FINITE_FORMS or (opener == "et" and form == "da"))
            for _l, pos, form in _tr_readings(spans[m]))
    if comma is None or not predicate or comma + 3 > hi:
        return None
    j = comma + 1
    low = spans[j].text.lower()
    explanation = ("Lause algul olev kõrvallause on pealause esimene liige, nii et kohe "
                   "järgneb öeldis: 'Kui saabub kevad, muutub ka meeleolu paremaks.'")
    if low in _TR_SHORT_PRONOUNS:
        if not any(pos == "P" and form in ("sg n", "pl n") for _l, pos, form in _tr_readings(spans[j])):
            return None
        # The pronoun agreeing with the person settles a verb that is also
        # a noun (saame: saam), so the sentence's reading is enough.
        persons = _tr_persons(spans, j + 1)
        if (not persons or _TR_SUBJECT_PRONOUNS[low] not in persons
                or (j + 2 < hi and spans[j + 2].text in (",", ")", "-", "\u2013", "\u2014"))):
            return None
        verb = spans[j + 1].text
        suggestion = (verb if _TR_SUBJECT_PRONOUNS[low][0] in "12"
                      else f"{verb} {spans[j].text}")
        return {
            "phrase": text[spans[j].start:spans[j + 1].end],
            "position": spans[j].start,
            "rule": "verb-second",
            "rule_estonian": "öeldis teisel kohal",
            "explanation": explanation,
            "suggestion": suggestion,
        }
    if low in _TR_DETERMINERS and not any(pos == "S" for _l, pos, _f in _tr_readings(spans[j + 1])):
        return None
    found = _tr_subject_np(spans, j, hi)
    if found is None or found[0] >= hi:
        return None
    end, number = found
    persons = _tr_persons(spans, end)
    if (not persons or ("3pl" if number == "pl" else "3sg") not in persons
            or not _sure_finite(spans[end], spans[end + 1] if end + 1 < hi else None)):
        return None
    return {
        "phrase": text[spans[j].start:spans[end].end],
        "position": spans[j].start,
        "rule": "verb-second",
        "rule_estonian": "öeldis teisel kohal",
        "explanation": explanation,
        "suggestion": f"{spans[end].text} {text[spans[j].start:spans[end - 1].end]}",
    }


def _tr_saama_future_issues(text: str, spans: list, lo: int, hi: int) -> list[dict]:
    """saama-future: saab olema → on, saab toimuma → toimub. saama with a
    da-infinitive (seda saab teha) is a possibility and is not matched."""
    issues: list[dict] = []
    for k in range(lo, hi):
        readings = _tr_readings(spans[k])
        if not readings or not all(lemma == "saama" and pos == "V" and form in _TR_SAAMA_PRESENT
                                   for lemma, pos, form in readings):
            continue
        form = readings[0][2]
        for m in range(k + 1, min(hi, k + 3)):
            after = _tr_readings(spans[m])
            verb = next((lemma for lemma, pos, f in after if pos == "V" and f == "ma"), None)
            if verb is None:
                if any(pos == "D" for _l, pos, _f in after):
                    continue  # saab kindlasti olema
                break
            if verb in _TR_SAAMA_IDIOMS or not all(pos == "V" and f == "ma" for _l, pos, f in after):
                break
            if verb == "olema":
                plain = _first(_synthesize("olema", form, "V")) or "on"
                suggestion = f"{plain} / tuleb"
            else:
                suggestion = _first(_synthesize(verb, form, "V")) or "olevik"
            issues.append({
                "phrase": text[spans[k].start:spans[m].end],
                "position": spans[k].start,
                "rule": "saama-future",
                "rule_estonian": "saama-tulevik",
                "explanation": ("Tulevikku väljendab eesti keeles olevik; 'saab tegema' on "
                                "laenkonstruktsioon ja ebasoovitav."),
                "suggestion": suggestion,
            })
            break
    return issues


def _tr_maks_issues(text: str, spans: list, lo: int, hi: int) -> list[dict]:
    """maks-infinitive (ettepanek võtmaks → võtta) and maks-purpose
    (Tagamaks → et tagada; sõitis sõlmimaks → sõlmima). A purpose maks
    after an et in the same sentence is left alone: EKI accepts it where
    it keeps two et from meeting."""
    issues: list[dict] = []
    et_before = False
    for k in range(lo, hi):
        if spans[k].text.lower() == "et":
            et_before = True
        readings = _tr_readings(spans[k])
        if not readings or not all(pos == "V" and form == "maks" for _l, pos, form in readings):
            continue
        verb = readings[0][0]
        da = _first(_synthesize(verb, "da", "V"))
        prev = spans[k - 1] if k > lo else None
        if prev is not None and _span_bits(prev)[0] == "S" and (
                _span_bits(prev)[2] in _TR_DA_NOUNS or (_root_parts(prev) or [""])[-1] in _TR_DA_NOUNS):
            issues.append({
                "phrase": text[prev.start:spans[k].end],
                "position": prev.start,
                "rule": "maks-infinitive",
                "rule_estonian": "maks-vorm da-tegevusnime asemel",
                "explanation": ("Nimisõna laiendab da-tegevusnimi: 'ettepanek võtta', mitte "
                                "'ettepanek võtmaks'."),
                "suggestion": f"{prev.text} {da}" if da else "da-tegevusnimi",
            })
            continue
        motion = False
        for m in range(k - 1, max(lo, k - 5) - 1, -1):
            if spans[m].text in _CLAUSE_BOUNDARY_MARKS:
                break
            if any(pos == "V" and lemma in _TR_MA_VERBS for lemma, pos, _f in _tr_readings(spans[m])):
                motion = True
                break
        if motion:
            ma = _first(_synthesize(verb, "ma", "V"))
            issues.append({
                "word": spans[k].text,
                "position": spans[k].start,
                "rule": "maks-purpose",
                "rule_estonian": "maks-lühend",
                "explanation": ("Liikumist väljendava tegusõna järel sobib ma-tegevusnimi: "
                                "'sõitis Pärnu lepingut sõlmima'."),
                "suggestion": ma or "ma-tegevusnimi",
            })
        elif not et_before:
            issues.append({
                "word": spans[k].text,
                "position": spans[k].start,
                "rule": "maks-purpose",
                "rule_estonian": "maks-lühend",
                "explanation": ("Eesmärki väljendab loomulikumalt et-lause; sage maks-vorm mõjub "
                                "eputavalt. Stiilisoovitus."),
                "suggestion": f"et {da}" if da else "et + da-tegevusnimi",
            })
    return issues


def _tr_mitte_des_issues(text: str, spans: list, lo: int, hi: int) -> list[dict]:
    """mitte-des: Mitte leides → Leidmata. A contrast (õpid mitte lugedes,
    vaid tehes) keeps mitte."""
    issues: list[dict] = []
    if any(s.text.lower() in ("vaid", "kuid", "aga", "ent") for s in spans[lo:hi]):
        return issues
    for k in range(lo, hi - 1):
        if spans[k].text.lower() != "mitte":
            continue
        verb = next((lemma for lemma, pos, form in _tr_readings(spans[k + 1])
                     if pos == "V" and form == "des"), None)
        mata = _first(_synthesize(verb, "mata", "V")) if verb else None
        if not mata:
            continue
        if spans[k].text[:1].isupper():
            mata = mata[:1].upper() + mata[1:]
        issues.append({
            "phrase": text[spans[k].start:spans[k + 1].end],
            "position": spans[k].start,
            "rule": "mitte-des",
            "rule_estonian": "mitte + des-vorm",
            "explanation": "des-vormi eitus on mata-vorm: 'Leidmata', mitte 'Mitte leides'.",
            "suggestion": mata,
        })
    return issues


def _tr_tanu_issues(text: str, spans: list, lo: int, hi: int) -> list[dict]:
    """tanu-negative: tänu ressursside piiratusele → ressursside piiratuse
    tõttu. Only the preposition tänu, before a cause on the list."""
    issues: list[dict] = []
    for k in range(lo, hi - 1):
        if spans[k].text.lower() != "tänu" or not any(
                pos == "K" for _l, pos, _f in _tr_readings(spans[k])):
            continue
        for m in range(k + 1, min(hi, k + 5)):
            if spans[m].text in _CLAUSE_BOUNDARY_MARKS:
                break
            noun = next((lemma for lemma, pos, form in _tr_readings(spans[m])
                         if pos == "S" and form.endswith(" all")), None)
            if noun is None:
                continue
            if noun in _TR_NEGATIVE_CAUSES or (_root_parts(spans[m]) or [""])[-1] in _TR_NEGATIVE_CAUSES:
                words = []
                for w in spans[k + 1:m + 1]:
                    allative = next(((lemma, pos, form) for lemma, pos, form in _tr_readings(w)
                                     if form.endswith(" all")), None)
                    genitive = allative and _first(_synthesize(
                        allative[0], allative[2][:-len("all")] + "g", allative[1]))
                    words.append(genitive or w.text)
                issues.append({
                    "phrase": text[spans[k].start:spans[m].end],
                    "position": spans[k].start,
                    "rule": "tanu-negative",
                    "rule_estonian": "tänu halva põhjuse korral",
                    "explanation": ("'Tänu' sobib, kui on põhjust tänulik olla; halva põhjuse "
                                    "korral 'tõttu' või 'tagajärjel'."),
                    "suggestion": " ".join(words) + " tõttu",
                })
            break
    return issues


def _tr_item(spans: list, a: int, b: int) -> bool:
    """spans[a:b] is a short list item: one to four words, no verb,
    conjunction, relative word or inserted-remark opener, ending in a
    word with a case."""
    if not 1 <= b - a <= 4:
        return False
    for s in spans[a:b]:
        low = s.text.lower()
        if (_is_punct(s) or low in _CLAUSE_SCAN_STOP or low in _TR_KIIL_OPENERS
                or _relative_word(s) or any(pos in ("V", "J") for _l, pos, _f in _tr_readings(s))):
            return False
    return bool(_tr_cases(spans[b - 1]))


def _tr_cases(span) -> set[str]:
    return {form.split()[-1] for _l, pos, form in _tr_readings(span)
            if pos in _TR_NOMINAL_POS and " " in form}


def _tr_serial_comma_issues(text: str, spans: list, lo: int, hi: int) -> list[dict]:
    """serial-comma: kohvi, teed, ja küpsiseid → teed ja. Three short items
    in a shared case, the last two joined by ', ja'. A comma that closes a
    clause, an apposition (Costa, Portugali peaminister, ja Juncker), an
    inserted remark or an address stays."""
    issues: list[dict] = []
    for i in range(lo + 2, hi - 1):
        if spans[i].text != "," or spans[i + 1].text.lower() not in _TR_LIST_CONJUNCTIONS:
            continue
        j = i + 2
        if j >= hi or spans[j].text.lower() in _TR_AFTERTHOUGHT:
            continue
        end3 = j
        while (end3 < hi and end3 - j < 4 and not _is_punct(spans[end3])
               and not any(pos == "V" for _l, pos, _f in _tr_readings(spans[end3]))):
            end3 += 1
        if not _tr_item(spans, j, end3):
            continue
        p = i - 1
        while p > lo and not _is_punct(spans[p]):
            p -= 1
        if spans[p].text != "," or not _tr_item(spans, p + 1, i):
            continue
        q = p - 1
        while (q >= lo and p - q <= 4 and not _is_punct(spans[q]) and not _relative_word(spans[q])
               and not any(pos in ("V", "J", "K") for _l, pos, _f in _tr_readings(spans[q]))):
            q -= 1
        if q + 1 >= p or not _tr_item(spans, q + 1, p):
            continue
        if not (_tr_cases(spans[p - 1]) & _tr_cases(spans[i - 1]) & _tr_cases(spans[end3 - 1])):
            continue

        def named(a: int, b: int) -> bool:
            return any(pos == "H" for s in spans[a:b] for _l, pos, _f in _tr_readings(s))
        if named(q + 1, p) and not named(p + 1, i) and named(j, end3):
            continue
        issues.append({
            "phrase": text[spans[i - 1].start:spans[i + 1].end],
            "position": spans[i - 1].start,
            "rule": "serial-comma",
            "rule_estonian": "koma ja ees loetelus",
            "explanation": ("Loetelus ei panda koma sõnade ja, ning, või, ega ette; see on "
                            "inglise tava."),
            "suggestion": f"{spans[i - 1].text} {spans[i + 1].text}",
        })
    return issues


@lru_cache(maxsize=4096)
def _tr_capital_kind(word: str) -> str:
    """How a capitalised heading word reads: 'name' (a known name, an
    abbreviation or an institution: Eesti, ERR, Tolliameti), 'function'
    (only a verb, pronoun, conjunction, adposition or adverb: Me, Saame,
    Ja), 'common' (another known word: Teenused) or '' (unknown)."""
    if word.isupper() or _lexicon_names(word):
        return "name"
    readings = _lexicon_readings(word.lower())
    if not readings:
        return ""
    if any(r["partofspeech"] == "S" and r["lemma"].lower().endswith(_TR_INSTITUTION_HEADS)
           for r in readings):
        return "name"
    pos = {r["partofspeech"] for r in readings}
    return "function" if pos <= _TR_FUNCTION_POS else "common"


def _tr_title_case_issues(text: str) -> list[dict]:
    """title-case: Meie Teenused Ja Hinnad → Meie teenused ja hinnad. A
    markdown heading, or a line of 2 to 10 words with no closing
    punctuation after a blank line and before more text."""
    issues: list[dict] = []
    lines = text.split("\n")
    offset, checked = 0, 0
    for idx, line in enumerate(lines):
        start, offset = offset, offset + len(line) + 1
        raw = line.strip()
        body = raw.lstrip("#").strip()
        if not body or body[-1] in ".,;!?":
            continue
        if not raw.startswith("#") and not (
                (idx == 0 or not lines[idx - 1].strip()) and idx + 1 < len(lines)):
            continue
        words = [(m.group(0), m.start()) for m in _TR_HEADING_WORD_RE.finditer(body)]
        if not 2 <= len(words) <= 10:
            continue
        checked += 1
        if checked > _TR_HEADING_LINES:
            break
        counted, capitals, common, function, named = 0, 0, [], 0, False
        for w, at in words:
            kind = _tr_capital_kind(w) if w[:1].isupper() else ""
            named = named or kind == "name"
            if at == 0 or body[:at].rstrip()[-1:] in _TR_SENTENCE_OPENERS:
                continue
            if w.lower() in _TR_LIST_CONJUNCTIONS:
                if w[:1].isupper():
                    common.append(w)  # lowered in the suggestion, not counted
                    function += 1
                continue
            counted += 1
            if not w[:1].isupper() or w.endswith("-") or w.lower() in _TR_POLITE:
                continue
            capitals += 1
            if kind in ("common", "function"):
                common.append(w)
                function += kind == "function"
        # Most words capitalised, at least two of them ordinary words, and
        # with a name in the line one of those must be a word no name
        # capitalises (Eesti Keele Instituut is a name, Kuidas Me Saame
        # Aidata is not).
        ordinary = [w for w in common if w.lower() not in _TR_LIST_CONJUNCTIONS]
        if len(ordinary) < 2 or capitals * 3 < counted * 2 or (named and not function):
            continue
        fixed = body
        for w in common:
            fixed = re.sub(rf"(?<!\w){re.escape(w)}(?!\w)", w[:1].lower() + w[1:], fixed, count=1)
        issues.append({
            "phrase": body,
            "position": start + line.index(body),
            "rule": "title-case",
            "rule_estonian": "pealkirja suurtähed",
            "explanation": ("Eesti pealkirjas on suure tähega esimene sõna ja nimed; iga sõna "
                            "suure tähega on inglise tava."),
            "suggestion": fixed,
        })
    return issues


@lru_cache(maxsize=4096)
def _tr_addressee(word: str) -> bool:
    """A capitalised word after Tere that names who is greeted: a known
    name, an address noun (Kolleeg) or a word the lexicon lacks (a name it
    does not know). Tere Tulemast is a phrase in title case, not an
    address."""
    return bool(_lexicon_names(word) or word.lower() in _TR_ADDRESS_NOUNS
                or not _lexicon_readings(word.lower()))


def _tr_letter_issues(text: str) -> list[dict]:
    """closing-comma (Lugupidamisega, → Lugupidamisega) and
    greeting-punctuation (Hea Mari, → Hea Mari!; Tere Mari → Tere, Mari)."""
    issues: list[dict] = []
    for m in _TR_CLOSING_RE.finditer(text):
        rest = m.group("rest").strip()
        if rest and not rest[:1].isupper():
            continue
        issues.append({
            "phrase": text[m.start("f"):text.index(",", m.end("f")) + 1],
            "position": m.start("f"),
            "rule": "closing-comma",
            "rule_estonian": "koma lõputervituse järel",
            "explanation": "Lõputervituse ja nime vahele koma ei panda: 'Lugupidamisega / Mari Tamm'.",
            "suggestion": m.group("f"),
        })
    greeted = set()
    for m in _TR_GREETING_RE.finditer(text):
        greeting = m.group("g").strip()
        words = greeting.split()
        if words[0].lower() in ("hea", "head", "kallis", "kallid", "armas", "armsad") and (
                len(words) < 2 or not (words[1][:1].isupper()
                                       or words[1].lower() in _TR_ADDRESS_NOUNS)):
            continue
        if len(words) > 6 or not _TR_NONSPACE_RE.search(text, m.end()):
            continue  # a letter goes on after its salutation
        if words[0].lower() in ("tere", "hei", "tervist", "tšau") and len(words) > 1:
            greeting = f"{words[0]}, {' '.join(words[1:])}"
        greeted.add(m.start("g"))
        issues.append({
            "phrase": text[m.start("g"):m.end()].rstrip(),
            "position": m.start("g"),
            "rule": "greeting-punctuation",
            "rule_estonian": "pöördumise kirjavahemärgid",
            "explanation": ("Pöördumine lõpeb hüüumärgiga või märgita, mitte komaga, ja kiri "
                            "jätkub suure tähega."),
            "suggestion": f"{greeting}!",
        })
    for m in _TR_TERE_NAME_RE.finditer(text):
        name = m.group("n")
        if m.start("g") in greeted or not _tr_addressee(name):
            continue
        issues.append({
            "phrase": text[m.start("g"):m.end("n")],
            "position": m.start("g"),
            "rule": "greeting-punctuation",
            "rule_estonian": "pöördumise kirjavahemärgid",
            "explanation": "Tervituse ja pöördumise vahele käib koma: 'Tere, Mari!'",
            "suggestion": f"{m.group('g')}, {name}",
        })
    return issues


def _tr_subject_pronoun_issue(spans: list, sentences: list[tuple[int, int]]) -> dict | None:
    """subject-pronoun: three or more sentences, and a third of them or
    more, open with ma, mina, me or meie before a verb whose ending shows
    the same person. Reported once, at the first."""
    hits = []
    for lo, hi in sentences:
        k = lo
        while k < hi and spans[k].text in _QUOTE_MARKS:
            k += 1
        if k + 1 >= hi or spans[k].text.lower() not in ("ma", "mina", "me", "meie"):
            continue
        persons = _tr_persons(spans, k + 1)
        if persons == {_TR_SUBJECT_PRONOUNS[spans[k].text.lower()]}:
            hits.append(spans[k].start)
    if len(hits) < 3 or len(hits) * 3 < len(sentences):
        return None
    return {
        "position": hits[0],
        "rule": "subject-pronoun",
        "rule_estonian": "liigne isikuline asesõna",
        "explanation": (f"{len(hits)} lauset {len(sentences)}-st algab sõnaga 'ma' või 'me', "
                        "kuigi pöördelõpp näitab tegijat juba: 'Loodan', mitte 'Ma loodan'."),
        "suggestion": "jäta asesõna ära, kus see vastandust ei rõhuta",
    }


def _check_translationese(text: str) -> dict:
    """English sentence structure and English habits of form in Estonian
    text: the rules listed above _TR_FORM_PERSONS."""
    _check_text(text)
    t = _Text()(text)
    t.tag_layer(["sentences", "morph_analysis"])
    spans = list(t.morph_analysis)
    sentences = [(lo, hi) for lo, hi in _sentence_ranges(t, spans) if hi > lo]

    issues: list[dict] = []
    for lo, hi in sentences:
        issues.extend(_tr_oma_issues(text, spans, lo, hi))
        issues.extend(_tr_pronoun_after_verb_issues(text, spans, lo, hi))
        found = _tr_verb_second_issue(text, spans, lo, hi)
        if found:
            issues.append(found)
        issues.extend(_tr_saama_future_issues(text, spans, lo, hi))
        issues.extend(_tr_maks_issues(text, spans, lo, hi))
        issues.extend(_tr_mitte_des_issues(text, spans, lo, hi))
        issues.extend(_tr_tanu_issues(text, spans, lo, hi))
        issues.extend(_tr_serial_comma_issues(text, spans, lo, hi))
    for rx, plain, why in _TR_PHRASES:
        issues.extend({
            "phrase": m.group(0),
            "position": m.start(),
            "rule": "english-phrase",
            "rule_estonian": "inglise keele mõjuga väljend",
            "explanation": why,
            "suggestion": plain,
        } for m in rx.finditer(text))
    issues.extend(_tr_letter_issues(text))
    issues.extend(_tr_title_case_issues(text))
    found = _tr_subject_pronoun_issue(spans, sentences)
    if found:
        issues.append(found)
    issues.sort(key=lambda d: d["position"])
    return {
        "issues": issues,
        "summary_estonian": (
            f"Leiti {len(issues)} kohta, mis kõlavad tõlkena." if issues
            else "Tõlkekeele tunnuseid ei leitud."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian for English structure (translationese)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_translationese(text: Annotated[str, Field(description="Estonian text you wrote, translated or are editing.")]) -> _CheckResult:
    """Flag English structure that makes Estonian read as translated. Run
    it on Estonian you wrote or translated.

    Rules: `oma-possessive` (saatke meile teie andmed → oma andmed),
    `pronoun-after-verb` (lähtume me → lähtume), `subject-pronoun` (a third
    of the sentences open with Ma or Me; once), `verb-second` (Kui ...,
    me saadame → saadame), `saama-future` (saab olema → on),
    `maks-infinitive` (ettepanek võtmaks → võtta), `maks-purpose`
    (Tagamaks → et tagada), `mitte-des` (Mitte leides → Leidmata),
    `tanu-negative` (tänu + a bad cause → tõttu), `english-phrase` (minu
    arvamusel), `greeting-punctuation` (Hea Mari, → Hea Mari!; Tere Mari →
    Tere, Mari), `closing-comma` (Lugupidamisega,), `serial-comma` (teed,
    ja küpsiseid), `title-case` (Meie Teenused Ja Hinnad).

    `maks-purpose` and `subject-pronoun` are style hints. Third-person
    possessives, negated verbs and questions are not checked. Accepted
    Estonian is never flagged (aset leidma, rolli mängima, Eile ma läksin,
    a capital Teie to one reader). Letter formulas such as Palun leidke
    manuses are check_officialese's.
    """
    return _check_translationese(text)


# How many distinct content nouns get a WordNet lookup in
# check_term_consistency. Bounds cost on long documents; the nouns are
# ranked by frequency first, so the cap drops only long-tail hapaxes.
# The tool's docstring states this number; tests/test_officialese.py pins
# the two together.
_TERM_CONSISTENCY_WORDNET_CAP = 40


def _domain_prefer(group: dict, readings: dict[str, set[str]],
                   terms: frozenset[str]) -> dict:
    """Mark a check_term_consistency group against the domain glossary.
    Every variant gets `in_domain_glossary`. When exactly one variant is
    listed, it becomes the group's `preferred` form, beside the frequency
    answer in `dominant`. When several are, the organisation uses each,
    which suggests distinct concepts, so none is preferred."""
    listed: dict[str, str] = {}
    for v in group["variants"]:
        lemma = v["lemma"]
        hits = readings.get(lemma, {lemma}) & terms
        v["in_domain_glossary"] = bool(hits)
        if hits:
            listed[lemma] = lemma if lemma in hits else min(hits)
    if len(listed) == 1:
        term = next(iter(listed.values()))
        group["preferred"] = term
        group["explanation"] += (
            f" Kui valid ühe, eelista valdkonna sõnastiku terminit '{term}'.")
    return group


def _check_term_consistency(text: str, glossary: list[str] | None = None) -> dict:
    """One referent, one term: flag a document that names the same thing
    several ways.

    This is the failure mode a human editor catches instantly and a model
    sliding through a long document does not: a dataset called `andmestik`
    in one paragraph, `teadusandmestik` in the next and `korpus` in a
    third. Two rules, both precision-first:

    A. Shared compound head. The text uses a bare noun X *and* a compound
       ending in X (`andmestik` + `teadusandmestik`), or three or more
       distinct lemmas share one head. Two compounds sharing a head is NOT
       enough on its own: `tegevusvaldkond` and `märgendusvaldkond` are
       usually genuinely different things.
    B. Shared WordNet synset. Two distinct lemmas in the text sit in the
       same synset, i.e. Estonian WordNet considers them synonyms
       (`andmestik` / `andmebaas`).

    Reports counts per variant so the caller can pick the dominant term,
    and never decides which variant is right: that needs context the tool
    does not have.

    Capped at MAX_TEXT_CHARS, NOT MAX_DOC_CHARS. check_defined_terms may
    take 500k because it is pure regex; this one runs full morphological
    analysis over the whole document, which is ~4s per 100k chars. At 500k
    a single call would burn ~22s of CPU, which would blow the
    defence-in-depth budget the public per-IP rate limit is sized against.

    With a domain glossary (the server's file, or `glossary` for this
    call), a variant that is the only one listed in its group becomes the
    group's `preferred` form; see _domain_prefer. Without one, the output is as it always was.
    """
    _check_text(text, limit=MAX_TEXT_CHARS)
    terms = _domain_glossary_or_empty() | _call_glossary(glossary)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["morph_analysis"])

    from collections import Counter, defaultdict

    counts: Counter = Counter()
    first_pos: dict[str, int] = {}
    heads: dict[str, str] = {}
    readings: dict[str, set[str]] = {}

    for span in t.morph_analysis:
        pos = _first(list(span.partofspeech))
        if pos != "S":
            continue
        lemma_raw = _first(list(span.lemma)) or ""
        if not lemma_raw or lemma_raw[0].isupper() or len(lemma_raw) < 4:
            continue
        lemma = lemma_raw.lower()
        rt_lists = [list(rt) for rt in span.root_tokens]
        parts = rt_lists[0] if rt_lists else []
        counts[lemma] += 1
        first_pos.setdefault(lemma, span.start)
        heads[lemma] = (parts[-1].lower() if parts else lemma)
        if terms:
            readings.setdefault(lemma, set()).update(_word_forms(span))

    groups: list[dict] = []
    grouped_lemmas: set[str] = set()

    # --- Rule A: shared compound head.
    by_head: dict[str, set[str]] = defaultdict(set)
    for lemma, head in heads.items():
        by_head[head].add(lemma)

    for head, lemmas in sorted(by_head.items()):
        if len(lemmas) < 2:
            continue
        bare_present = head in lemmas
        if not (bare_present or len(lemmas) >= 3):
            continue
        variants = sorted(lemmas, key=lambda w: (-counts[w], w))
        groups.append({
            "head": head,
            "rule": "shared-compound-head",
            "rule_estonian": "sama põhisõna",
            "variants": [
                {"lemma": w, "count": counts[w], "position": first_pos[w]}
                for w in variants
            ],
            "dominant": variants[0],
            "explanation": (
                f"Tekstis kasutatakse sama põhisõnaga termineid: "
                f"{', '.join(variants)}. Kui need tähistavad üht ja sama "
                f"asja, vali üks ja kasuta seda läbivalt."
            ),
        })
        grouped_lemmas.update(lemmas)

    # --- Rule B: shared WordNet synset.
    #
    # Check the resource is on disk FIRST (_wordnet_available is a pure
    # filesystem lookup). The old code called Wordnet() and caught the
    # fallout, which meant that on a machine without the resource EstNLTK
    # would attempt a download (breaching the no-outbound-HTTP promise in
    # PRIVACY.md) and print its prompt to stdout, which under stdio
    # transport is the MCP protocol channel. Checking first means the
    # running server never attempts either, and Rule B just reports itself
    # as not run.
    ranked = [w for w, _ in counts.most_common(_TERM_CONSISTENCY_WORDNET_CAP)]
    synsets_by_lemma: dict[str, set[str]] = {}
    wordnet_ok = _wordnet_available()
    if wordnet_ok:
        try:
            wn = _wordnet()
        except Exception:
            wn = None
            wordnet_ok = False
        if wn is not None:
            for lemma in ranked:
                try:
                    synsets_by_lemma[lemma] = {s.name for s in (wn[lemma] or [])}
                except Exception:
                    synsets_by_lemma[lemma] = set()
                    wordnet_ok = False

    seen_pairs: set[tuple[str, str]] = set()
    for i, a in enumerate(ranked):
        for b in ranked[i + 1:]:
            if a == b or (a, b) in seen_pairs:
                continue
            shared = synsets_by_lemma.get(a, set()) & synsets_by_lemma.get(b, set())
            if not shared:
                continue
            # Skip pairs Rule A already reported together.
            if heads.get(a) == heads.get(b) and {a, b} <= grouped_lemmas:
                continue
            seen_pairs.add((a, b))
            variants = sorted([a, b], key=lambda w: (-counts[w], w))
            groups.append({
                "rule": "shared-wordnet-synset",
                "rule_estonian": "sama tähendusrühm",
                "synsets": sorted(shared),
                "variants": [
                    {"lemma": w, "count": counts[w], "position": first_pos[w]}
                    for w in variants
                ],
                "dominant": variants[0],
                "explanation": (
                    f"'{a}' ja '{b}' kuuluvad eesti WordNetis samasse "
                    f"tähendusrühma ({', '.join(sorted(shared))}). Kui need "
                    f"tähistavad üht ja sama asja, vali üks."
                ),
            })

    if terms:
        for g in groups:
            _domain_prefer(g, readings, terms)
    groups.sort(key=lambda g: g["variants"][0]["position"])

    return {
        "groups": groups,
        "terms_analysed": len(counts),
        # Top-level so a caller cannot miss it. A partial run that reports
        # "nothing found" reads as a clean bill of health, which is exactly
        # how a half-strength checker misleads someone, so say it here AND
        # in summary_estonian. The compound-head rule always runs; the
        # WordNet rule ran exactly when this is false.
        "degraded": not wordnet_ok,
        "summary_estonian": (
            (
                f"Leiti {len(groups)} rühma, kus üht asja võidakse nimetada "
                f"mitmel viisil. Vali igas rühmas üks termin ja kasuta seda "
                f"läbivalt."
                if groups else
                "Ebajärjekindlat terminikasutust ei tuvastatud."
            )
            + (
                "" if wordnet_ok else
                " TÄHELEPANU: tulemus on osaline. Eesti WordNet ei ole "
                "paigaldatud, mistõttu jäi tähendusrühmade reegel "
                "käivitamata ja osa kattuvaid termineid võib jääda "
                "leidmata. Paigalda ressurss käsuga "
                "`uv run python scripts/fetch_resources.py`."
            )
        ),
    }


class _TermConsistencyResult(TypedDict, total=False):
    """Output of check_term_consistency: groups of competing terms."""
    groups: list[dict]
    terms_analysed: int
    degraded: bool
    summary_estonian: str


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian terminology consistency (one referent, one term)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_term_consistency(
    text: Annotated[str, Field(description="The whole Estonian document, not one paragraph at a time.")],
    glossary: Annotated[list[_GlossaryTerm] | None, Field(max_length=MAX_GLOSSARY_TERMS, description="Optional domain lemmas (base forms) for this call only, added to any server glossary. Not stored.")] = None,
) -> _TermConsistencyResult:
    """Flag a document that names one thing several ways (andmestik,
    teadusandmestik, pildiandmestik).

    Rules: `shared-compound-head` (a bare noun plus a compound on it, or
    3+ lemmas sharing a head) and `shared-wordnet-synset` (two of the 40
    most frequent nouns share a WordNet synset). Each group gives
    `variants` with counts and `dominant`, the most used. Some groups are
    distinct concepts and the tool does not pick the right term: read
    each first.

    `degraded: true` means WordNet is missing and the synset rule did not
    run: no groups is then no proof of consistency.

    With a domain glossary (the server's or `glossary`), variants carry
    `in_domain_glossary`, and a group's only listed variant is its
    `preferred` term, whatever `dominant` says (several listed suggest
    distinct concepts).

    Misses synonyms sharing neither head nor synset (korpus /
    andmestik): compare their `synonyms` definitions.
    """
    return _check_term_consistency(text, glossary)


@mcp.tool(annotations=ToolAnnotations(
    title="Classify Estonian register (formal vs colloquial)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def classify_register(text: Annotated[str, Field(description="Estonian text to classify by register (formal vs colloquial).")]) -> _RegisterResult:
    """Heuristic Estonian register score: a directional hint, not a verdict.
    Most everyday prose scores neutral, and address forms (sina/teie)
    are not scored.

    `score` runs from -1 (colloquial) to 1 (formal); `tier_estonian` is
    the correct Estonian label to quote (formaalne, never *formalne).
    `consistency.is_mixed`: formal and colloquial markers both occur,
    which jars even when the tier is neutral. `structure` adds up to 0.4
    (`applied`) for umbisikuline tegumood and noun density, from 25 words
    up and never on net-colloquial text. For a per-issue kantseliit
    breakdown use check_officialese.
    """
    return _classify_register(text)


# ---------------------------------------------------------------------------
# HTTP transport: bearer auth + rate limit
# ---------------------------------------------------------------------------

# Aggregate request counters surfaced at /metrics. Optionally persisted
# to a Fly volume so machine restarts don't reset the cumulative total.
# Only counts (never request bodies or tokens), so the "no request
# logging" property in SECURITY.md stays intact.
_STATS_START_TS: float = time.time()
_STATS: dict[str, Any] = {
    "total": 0,
    "by_status": {},
    "by_path": {},
    # Count of MCP `initialize` calls, a privacy-safe proxy for client
    # connections / session-starts. NOT a user count: a client that
    # reconnects counts again, and automated probes count too. No identity,
    # no IP, no body is stored: only the fact that an initialize occurred.
    "sessions": 0,
    # JSON-RPC method mix on POST /mcp, bucketed to a FIXED allowlist (see
    # _MCP_METHODS). Stateless HTTP means an `initialize` cannot be tied to
    # the tool calls that follow it, so "sessions that made >=1 tool call"
    # is not computable without inventing a client identifier, which would
    # be a privacy step backwards. This breakdown answers the same question
    # from the other side: `initialize` vs `notifications/initialized` shows
    # how many handshakes were actually completed rather than abandoned by a
    # probe, and `tools/list` vs `tools/call` shows how many clients
    # enumerate the tools but never use one.
    "mcp_methods": {},
    # Runs of 429s to one client with no gap over a minute (see
    # _RateLimiter.note_denial). by_status["429"] says how many requests
    # were throttled; this says whether that was many clients briefly or
    # a few clients at length. Only the count is kept: which IP or token
    # was throttled never leaves the limiter's in-memory state.
    "rate_limit_episodes": 0,
    # The share of the above that came from the static-path limiter.
    "static_rate_limited": 0,
    "static_rate_limit_episodes": 0,
    # Icon requests by User-Agent FAMILY (see _UA_FAMILIES), so the next
    # client that fetches the icon in a loop can be named. The header is
    # caller-controlled and is never stored: it is matched against a fixed
    # list and only the family's count is kept.
    "icon_clients": {},
}

# Ring buffer of recent 5xx errors so they're inspectable at /metrics
# without depending on Fly's short-lived log tail. PII-free: only
# timestamp, path, status, and (when known) the exception type, never
# request bodies. Persisted with the counters so they survive restarts.
_recent_errors: collections.deque = collections.deque(maxlen=20)

# Persistence: if ESTNLTK_MCP_METRICS_PATH is set (default
# /data/metrics.json, matches the Fly volume mount), counters survive
# machine restarts. Locally, the path's parent dir doesn't exist and
# we silently stay in-memory.
_METRICS_PATH = Path(
    os.environ.get("ESTNLTK_MCP_METRICS_PATH", "/data/metrics.json")
)
_METRICS_FLUSH_INTERVAL_SEC: float = 30.0
_metrics_last_flush_ts: float = 0.0


def _load_persistent_stats() -> None:
    """Restore counters from disk on process start, if available."""
    try:
        if not _METRICS_PATH.exists():
            return
        import json as _json
        data = _json.loads(_METRICS_PATH.read_text())
        _STATS["total"] = int(data.get("total", 0))
        _STATS["by_status"] = {str(k): int(v) for k, v in (data.get("by_status") or {}).items()}
        _STATS["by_path"] = {str(k): int(v) for k, v in (data.get("by_path") or {}).items()}
        _STATS["sessions"] = int(data.get("sessions", 0))
        _STATS["rate_limit_episodes"] = int(data.get("rate_limit_episodes", 0))
        _STATS["static_rate_limited"] = int(data.get("static_rate_limited", 0))
        _STATS["static_rate_limit_episodes"] = int(data.get("static_rate_limit_episodes", 0))
        _STATS["icon_clients"] = {
            str(k): int(v) for k, v in (data.get("icon_clients") or {}).items()
            if str(k) in _UA_FAMILY_NAMES
        }
        _STATS["mcp_methods"] = {
            str(k): int(v) for k, v in (data.get("mcp_methods") or {}).items()
            if str(k) in _MCP_METHODS or str(k) == "other"
        }
        _TOOL_CALLS.clear()
        _TOOL_CALLS.update({str(k): int(v) for k, v in (data.get("tool_calls") or {}).items()})
        _recent_errors.clear()
        for e in (data.get("recent_errors") or []):
            _recent_errors.append(e)
        log.info(
            "metrics persistence: restored total=%d, tool_calls=%d from %s",
            _STATS["total"], sum(_TOOL_CALLS.values()), _METRICS_PATH,
        )
    except Exception as e:
        log.warning("metrics persistence: failed to load %s: %s", _METRICS_PATH, e)


def _save_persistent_stats() -> None:
    """Atomic flush of current counters to disk. No-op if the parent
    directory doesn't exist (local dev without a mounted volume)."""
    if not _METRICS_PATH.parent.exists():
        return
    try:
        import json as _json
        tmp = _METRICS_PATH.with_suffix(_METRICS_PATH.suffix + ".tmp")
        tmp.write_text(_json.dumps({
            "total": _STATS["total"],
            "by_status": _STATS["by_status"],
            "by_path": _STATS["by_path"],
            "sessions": _STATS["sessions"],
            "mcp_methods": _STATS["mcp_methods"],
            "rate_limit_episodes": _STATS["rate_limit_episodes"],
            "static_rate_limited": _STATS["static_rate_limited"],
            "static_rate_limit_episodes": _STATS["static_rate_limit_episodes"],
            "icon_clients": _STATS["icon_clients"],
            "tool_calls": _TOOL_CALLS,
            "recent_errors": list(_recent_errors),
            "saved_at_unix": int(time.time()),
        }))
        tmp.replace(_METRICS_PATH)
    except Exception as e:
        log.warning("metrics persistence: failed to save %s: %s", _METRICS_PATH, e)


# Fixed allowlist of JSON-RPC methods we bucket into `mcp_methods`.
# The method name comes from a request body, i.e. it is caller-controlled,
# so it is NEVER stored verbatim: anything outside this set is counted as
# "other". That keeps the metrics dict bounded (no unbounded key growth
# from a hostile client) and keeps arbitrary caller strings off /metrics.
_MCP_METHODS: frozenset[str] = frozenset({
    "initialize",
    "notifications/initialized",
    "tools/list",
    "tools/call",
    "prompts/list",
    "resources/list",
    "resources/templates/list",
    "ping",
})


def _classify_mcp_method(body: bytes) -> str | None:
    """Bucket an MCP request body by its JSON-RPC method name.

    Returns an allowlisted method name, "other" for anything unrecognised,
    or None if the body is not parseable JSON-RPC. Only the `method` field
    is read (never params, arguments, or clientInfo), and nothing from the
    body is stored.

    Cost note: this parses every POST /mcp body, where the old
    initialize-only check could skip the parse via a substring gate. The
    body is already fully buffered by _drain_body at this point, and a
    json.loads on even a 100k-char tool call is well under a millisecond
    against tool executions that run 10ms-7s, so the trade is worth the
    visibility. Method names are matched by parsing rather than substring
    so a tool call whose Estonian text merely contains "tools/call" is not
    miscounted.
    """
    try:
        msg = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(msg, list):  # JSON-RPC batch: classify by its first method
        msg = next((m for m in msg if isinstance(m, dict) and m.get("method")), None)
    if not isinstance(msg, dict):
        return None
    method = msg.get("method")
    if not isinstance(method, str):
        return None
    return method if method in _MCP_METHODS else "other"


# Public paths the wrapper answers itself, before the /mcp limiter. They
# share one per-IP limiter of their own (DEFAULT_STATIC_RATE_LIMIT_PER_MINUTE)
# so a looping icon fetcher can neither run unbounded nor spend the /mcp
# budget of real clients behind the same address. /health stays exempt:
# Fly's probes hit it every 30 s.
_STATIC_PATHS: frozenset[str] = frozenset({
    "/",
    "/metrics",
    "/favicon.ico",
    "/favicon.png",
    "/favicon.svg",
    "/.well-known/mcp/server-card.json",
    "/sse",
    "/sse/",
})
_ICON_PATHS: frozenset[str] = frozenset({
    "/favicon.ico", "/favicon.png", "/favicon.svg",
})

# User-Agent families for icon_clients. Checked in order, first match
# wins: crawlers announce themselves as "Mozilla/5.0 (compatible; ...bot)",
# so every named family comes before "browser". Matching is substring on
# the lowercased header; the header itself is never stored.
_UA_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("google", ("google",)),
    ("anthropic", ("claude", "anthropic")),
    ("openai", ("openai", "chatgpt", "gptbot")),
    ("link-preview", (
        "slackbot", "discordbot", "telegrambot", "twitterbot",
        "facebookexternalhit", "linkedinbot", "whatsapp",
    )),
    ("script", (
        "curl", "wget", "python", "go-http-client", "node", "axios",
        "okhttp", "java/", "ruby", "httpx", "aiohttp", "libwww",
    )),
    ("other-bot", ("bot", "crawler", "spider", "preview")),
    ("browser", ("mozilla/",)),
)
_UA_FAMILY_NAMES: frozenset[str] = frozenset(
    {name for name, _ in _UA_FAMILIES} | {"none", "other"}
)


def _classify_user_agent(scope: dict) -> str:
    """Bucket a request's User-Agent into one of _UA_FAMILY_NAMES."""
    ua = ""
    for raw_key, raw_val in (scope.get("headers") or []):
        if raw_key.lower() == b"user-agent":
            ua = raw_val.decode("latin-1").strip().lower()
            break
    if not ua:
        return "none"
    for name, needles in _UA_FAMILIES:
        if any(n in ua for n in needles):
            return name
    return "other"


async def _drain_body(receive):
    """Consume an ASGI HTTP request stream, returning (messages, body).

    `messages` is the exact list of events consumed, so they can be replayed
    to the inner app unchanged; `body` is the concatenated request body for
    a one-time peek. Bodies on /mcp are small JSON-RPC payloads."""
    messages = []
    body = b""
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] == "http.request":
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        elif message["type"] == "http.disconnect":
            break
    return messages, body


def _replay_receive(messages, receive):
    """Return a receive() that re-emits already-consumed messages in order,
    then defers to the original receive (e.g. for a later disconnect), so
    the inner app sees a byte-for-byte-identical request stream."""
    queue = list(messages)

    async def _receive():
        if queue:
            return queue.pop(0)
        return await receive()

    return _receive


# When the MCP SDK hits an unhandled error in POST-request handling it logs
# it via logger.exception(...) and returns its OWN HTTP 500: the exception
# never propagates to our wrapper's except block, so those 500s landed in
# the ring buffer with error=None (a blind spot). We attach a handler to the
# SDK's logger that stashes ONLY the exception type name (never the message
# or traceback) the instant it's logged; the send wrapper then borrows it to
# label an inner-returned 500. The logger.exception() runs synchronously just
# before the 500 is sent, in the same task, so the slot is fresh and correct
# for that request. A short freshness window guards against a later 5xx
# reusing a stale type.
_last_inner_exc: dict[str, Any] = {"type": None, "ts": 0.0}
_INNER_EXC_FRESH_SECONDS = 5.0


class _InnerExcCapture(logging.Handler):
    """Records the exception type name from any log record carrying exc_info.
    Type name only: PII-free, no message, no stack."""

    def emit(self, record: logging.LogRecord) -> None:
        exc_info = record.exc_info
        if exc_info and exc_info[0] is not None:
            _last_inner_exc["type"] = exc_info[0].__name__
            _last_inner_exc["ts"] = time.time()


def _install_inner_exc_capture() -> None:
    """Attach the capture handler to the `mcp` logger once (idempotent).
    Hooks the parent `mcp` logger so it keeps working if the SDK renames
    the streamable_http submodule. Additive: does not suppress the SDK's
    own logging."""
    lg = logging.getLogger("mcp")
    if not any(isinstance(h, _InnerExcCapture) for h in lg.handlers):
        handler = _InnerExcCapture()
        handler.setLevel(logging.ERROR)
        lg.addHandler(handler)


def _inner_exc_type() -> str | None:
    """Return the most-recently-logged exception type if fresh, else None."""
    if _last_inner_exc["type"] and (time.time() - _last_inner_exc["ts"]) < _INNER_EXC_FRESH_SECONDS:
        return _last_inner_exc["type"]
    return None


def _record_error(path: str, status: int, error: str | None) -> None:
    """Append a PII-free breadcrumb for a 5xx response to the ring buffer."""
    _recent_errors.append({
        "ts": int(time.time()),
        "path": path,
        "status": status,
        "error": error,
    })


def _stats_record(status: int, path: str) -> None:
    global _metrics_last_flush_ts
    _STATS["total"] += 1
    sk = str(status)
    _STATS["by_status"][sk] = _STATS["by_status"].get(sk, 0) + 1
    # Collapse /mcp to a single bucket; everything else (well-known
    # paths) keeps its literal value. Keeps the bucket count bounded.
    bucket = path if path in {
        "/health", "/metrics", "/favicon.ico", "/favicon.svg",
        "/favicon.png", "/.well-known/mcp/server-card.json", "/",
    } else "/mcp" if path == "/mcp" else "other"
    _STATS["by_path"][bucket] = _STATS["by_path"].get(bucket, 0) + 1
    # Periodic flush. Synchronous JSON write of ~few hundred bytes is
    # sub-millisecond; acceptable in the request path at our scale.
    now = time.time()
    if now - _metrics_last_flush_ts > _METRICS_FLUSH_INTERVAL_SEC:
        _metrics_last_flush_ts = now
        _save_persistent_stats()


class _RateLimiter:
    """Per-token leaky-bucket rate limiter (in-process, restart-resets).

    Sufficient for one-process containers. Behind a load balancer with
    multiple replicas, each replica enforces independently: combined
    quota is N*replicas, which is acceptable for a defence-in-depth
    measure.
    """

    # A client whose denials are further apart than this has stopped
    # hammering, so its next denial opens a new throttle episode.
    EPISODE_GAP_SECONDS = 60.0

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.buckets: dict[str, collections.deque[float]] = {}
        # Time of each bucket's latest denial, pruned once it is older
        # than EPISODE_GAP_SECONDS. Holds the same keys as `buckets` (IP
        # or token prefix), in memory only, and never leaves the process:
        # only the episode COUNT reaches /metrics.
        self.last_denied: dict[str, float] = {}

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

    def note_denial(self, key: str) -> bool:
        """Record a 429 for `key`. True if it opens a new throttle episode,
        i.e. `key` had no denial in the previous EPISODE_GAP_SECONDS.

        A client hammering past the limit gets one allowed request each
        time a slot frees up, so counting every allowed-to-denied flip
        would count a single sustained flood hundreds of times. Measuring
        the gap between denials counts it once."""
        now = time.monotonic()
        last = self.last_denied.get(key)
        self.last_denied[key] = now
        if last is not None and now - last <= self.EPISODE_GAP_SECONDS:
            return False
        # A new episode is rare, so this is the cheap moment to drop
        # entries whose episode has ended. Keeps the dict bounded by the
        # number of clients throttled in the last minute.
        cutoff = now - self.EPISODE_GAP_SECONDS
        self.last_denied = {
            k: t for k, t in self.last_denied.items() if t >= cutoff
        }
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


async def _serve_static_icon(send, scope, body: bytes, content_type: bytes, etag: str) -> None:
    """Serve a static icon with a 1-year immutable cache + ETag. If the
    client already holds this version (If-None-Match), answer 304 with no
    body. Cheap defence against clients that re-fetch the favicon in a loop
    (e.g. connector-directory icon rendering): anything honouring either
    caching or conditional requests stops re-downloading."""
    if_none_match = ""
    for k, v in scope.get("headers", []):
        if k == b"if-none-match":
            if_none_match = v.decode("latin1")
            break
    cache_headers = [
        (b"cache-control", b"public, max-age=31536000, immutable"),
        (b"etag", etag.encode("ascii")),
    ]
    if etag in if_none_match or if_none_match.strip() == "*":
        await send({"type": "http.response.start", "status": 304, "headers": cache_headers})
        await send({"type": "http.response.body", "body": b""})
        return
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type", content_type),
            (b"content-length", str(len(body)).encode("ascii")),
            *cache_headers,
        ],
    })
    await send({"type": "http.response.body", "body": body})


async def _send_redirect(send, location: str) -> None:
    await send({
        "type": "http.response.start",
        "status": 302,
        "headers": [
            (b"location", location.encode("latin1")),
            (b"content-length", b"0"),
        ],
    })
    await send({"type": "http.response.body", "body": b""})


def _accept_header(scope: dict) -> str:
    for k, v in scope.get("headers", []):
        if k.decode("latin1").lower() == "accept":
            return v.decode("latin1").lower()
    return ""


def _client_ip(scope: dict) -> str:
    """Originator IP for the public-mode per-IP rate limiter.

    Reads X-Forwarded-For from the RIGHT, not the left. uvicorn runs with
    `forwarded_allow_ips="*"` and rewrites `scope["client"]` from the
    LEFTMOST XFF entry, which is fully caller-controlled, because a proxy
    appends to whatever the client sent. Bucketing on that let any caller
    mint a fresh rate-limit bucket per request simply by varying the
    header, defeating the limiter entirely. Reproduced against a local
    server in public mode at a 5/min limit: a fixed spoofed value gets 429
    after five requests, while rotating the value stays 200 indefinitely.

    The Nth-from-right entry is the one written by the Nth proxy in front
    of us, and a caller cannot append after a proxy, so it is correct
    whether the edge APPENDS to a client-supplied header or REPLACES it.
    `_TRUSTED_PROXY_HOPS` is how many proxies we sit behind (Fly = 1).
    Set it to 0 when the server is directly exposed, which makes XFF
    untrusted entirely and buckets on the real peer.

    Falls back to the peer address whenever XFF has fewer entries than
    there are trusted hops, i.e. when the header is absent or too short to
    have come through the expected chain.
    """
    hops = _TRUSTED_PROXY_HOPS
    if hops > 0:
        for raw_key, raw_val in (scope.get("headers") or []):
            try:
                if raw_key.decode("latin-1").lower() != "x-forwarded-for":
                    continue
                parts = [p.strip() for p in raw_val.decode("latin-1").split(",") if p.strip()]
            except Exception:
                continue
            if len(parts) >= hops:
                return parts[-hops]
            break
    # `scope["client"]` is uvicorn's parse of XFF and therefore spoofable;
    # it is only reached when the header is missing or malformed, in which
    # case it degrades to the true peer.
    client = scope.get("client") or ("unknown", 0)
    return client[0] if isinstance(client, (tuple, list)) and client else "unknown"


def _build_http_app(
    token: str | None,
    rate_limit: int,
    public_mode: bool = False,
    inner=None,
    static_rate_limit: int | None = None,
):
    """Wrap an ASGI MCP app with auth (or none) + rate limit + /health bypass.

    public_mode=False (default): require bearer token, rate-limit per token.
    public_mode=True:           no auth, rate-limit per client IP.

    `inner` defaults to FastMCP's streamable-http app; tests inject a stub.
    `static_rate_limit` is the per-IP limit on _STATIC_PATHS; it defaults
    to $ESTNLTK_MCP_STATIC_RATE_LIMIT_PER_MINUTE, then
    DEFAULT_STATIC_RATE_LIMIT_PER_MINUTE.
    """
    # Start capturing the exception type the MCP SDK logs-then-swallows on
    # an inner 500, so the recent-errors buffer can label it.
    _install_inner_exc_capture()
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
    if static_rate_limit is None:
        static_rate_limit = int(os.environ.get(
            "ESTNLTK_MCP_STATIC_RATE_LIMIT_PER_MINUTE",
            str(DEFAULT_STATIC_RATE_LIMIT_PER_MINUTE),
        ))
    static_limiter = _RateLimiter(static_rate_limit)

    async def app(scope, receive, send_raw):
        if scope["type"] == "lifespan":
            return await inner(scope, receive, send_raw)
        if scope["type"] != "http":
            await _send_status(send_raw, 400, {"error": "unsupported scope"})
            return

        path = scope.get("path", "")
        # Wrap send so we can capture the final response status for
        # /metrics without changing the inner app's contract.
        captured: dict[str, Any] = {"status": 0, "error": None}

        async def send(message):
            if message["type"] == "http.response.start":
                status = message.get("status", 0)
                captured["status"] = status
                # Record any 5xx. Exceptions our wrapper catches set
                # captured["error"] below; for a 500 the inner MCP app
                # returns on its own, borrow the exception type the SDK
                # logged-then-swallowed (best effort) instead of None.
                if status >= 500:
                    _record_error(path, status, captured.get("error") or _inner_exc_type())
            await send_raw(message)

        try:
            # Public health endpoint: no auth, no rate limit. Used by Fly
            # probes, uptime monitoring, and quick "is the latest deploy
            # live?" eyeballing (version + tool count surfaced here).
            if path == "/health":
                await _send_status(send, 200, {
                    "ok": True,
                    "version": SERVER_VERSION,
                    "tools": _count_registered_tools(),
                })
                return

            if path in _STATIC_PATHS:
                # Counted before the limit, so a throttled loop still shows
                # up under its family.
                if path in _ICON_PATHS:
                    family = _classify_user_agent(scope)
                    _STATS["icon_clients"][family] = (
                        _STATS["icon_clients"].get(family, 0) + 1
                    )
                static_key = f"ip:{_client_ip(scope)}"
                if not static_limiter.allow(static_key):
                    _STATS["static_rate_limited"] += 1
                    if static_limiter.note_denial(static_key):
                        _STATS["rate_limit_episodes"] += 1
                        _STATS["static_rate_limit_episodes"] += 1
                    await _send_status(send, 429, {"error": "rate_limited"})
                    return

            # A human pasting the /mcp URL into a browser otherwise gets a
            # cryptic JSON-RPC 406 ("Client must accept text/event-stream").
            # If this looks like a browser (GET, wants HTML, not the SSE
            # stream a real MCP client opens), send them to the landing
            # page instead. Real MCP GETs carry Accept: text/event-stream
            # and pass straight through.
            if path == "/mcp" and scope.get("method") == "GET":
                accept = _accept_header(scope)
                if "text/event-stream" not in accept and "text/html" in accept:
                    await _send_redirect(send, "/")
                    return

            # Old SSE-transport clients hit /sse. We only speak Streamable
            # HTTP now; return a clear pointer instead of a bare 404.
            if path in ("/sse", "/sse/"):
                await _send_status(send, 404, {
                    "error": "not_found",
                    "message": (
                        "This server uses MCP Streamable HTTP, not the "
                        "deprecated SSE transport. Connect to /mcp instead."
                    ),
                    "endpoint": "/mcp",
                })
                return

            # Public metrics: aggregate request counters, persisted across
            # restarts when a volume is mounted (see _METRICS_PATH). No body
            # inspection, no token logging, no IPs: only counts.
            if path == "/metrics":
                payload = {
                    "total_requests": _STATS["total"],
                    "by_status": dict(_STATS["by_status"]),
                    "by_path": dict(_STATS["by_path"]),
                    "tool_calls_total": sum(_TOOL_CALLS.values()),
                    "tool_calls": dict(_TOOL_CALLS),
                    "sessions_total": _STATS["sessions"],
                    "mcp_methods": dict(_STATS["mcp_methods"]),
                    "rate_limited": {
                        "requests": _STATS["by_status"].get("429", 0),
                        "episodes": _STATS["rate_limit_episodes"],
                        "static_paths": {
                            "requests": _STATS["static_rate_limited"],
                            "episodes": _STATS["static_rate_limit_episodes"],
                        },
                    },
                    "icon_clients": dict(_STATS["icon_clients"]),
                    "recent_errors": list(_recent_errors),
                    "uptime_seconds": int(time.time() - _STATS_START_TS),
                    "started_at_unix": int(_STATS_START_TS),
                    "note": (
                        "tool_calls counts ONLY real tool executions (not "
                        "initialize / tools-list / SSE opens, which inflate "
                        "the /mcp path bucket); use tool_calls_total as the "
                        "true usage number. sessions_total counts MCP "
                        "initialize calls, a privacy-safe proxy for client "
                        "connections, NOT a user count: a client that "
                        "reconnects counts again and automated probes count "
                        "too. No identity, IP, or request body is ever stored, "
                        "only the fact of an initialize. Daily connections "
                        "= the day-over-day delta in the metrics snapshot. "
                        "rate_limited.requests is every 429 the rate limiter "
                        "sent (the same number as by_status['429']). "
                        "rate_limited.episodes counts runs of 429s to one "
                        "client (one IP in public mode, one token in bearer "
                        "mode) with no gap over 60 s between them, so "
                        "requests / episodes is how long a throttled client "
                        "kept going. An episode is NOT a distinct client: one "
                        "that hits the limit twice an hour apart counts twice. "
                        "Only the two counts are stored, never which IP or "
                        "token was throttled. rate_limited.static_paths is the "
                        "part of both that came from the separate per-IP "
                        "limit on the landing page, icons, server card, "
                        "/metrics and /sse (not /health). icon_clients counts "
                        "icon requests by User-Agent family against a FIXED "
                        "list (google, anthropic, openai, link-preview, "
                        "script, other-bot, browser, none, other); the header "
                        "itself is never stored. "
                        "mcp_methods buckets POST /mcp by JSON-RPC method "
                        "against a FIXED allowlist (anything else counts as "
                        "'other', so a caller-supplied method name can never "
                        "become a metrics key). Read it to tell probes from "
                        "real clients: initialize vs notifications/initialized "
                        "shows how many handshakes completed rather than being "
                        "abandoned, and tools/list vs tools/call shows how many "
                        "clients enumerate the tools but never call one. NOTE "
                        "this is NOT 'sessions that made >=1 tool call': the "
                        "transport is stateless_http, so an initialize cannot "
                        "be tied to the calls that follow it, and adding a "
                        "client identifier to make that possible would be a "
                        "privacy step backwards. "
                        "Counters persist to "
                        "/data/metrics.json every 30 s when a Fly volume is "
                        "mounted, surviving restarts; without a volume "
                        "(local dev) they reset. Counts are per-Fly-machine, "
                        "so with >1 machine each tracks its own and /metrics "
                        "reflects whichever served the request. started_at_unix "
                        "is the process start, NOT when tracking began: the "
                        "counts span all persisted history. recent_errors is a "
                        "ring buffer of the last 20 5xx responses (ts, path, "
                        "status, exception type) so failures are inspectable "
                        "here without Fly's short-lived log tail; it persists "
                        "with the counters. The exception type is captured both "
                        "for errors this wrapper catches and for 500s the MCP "
                        "SDK returns on its own (borrowed from the type it logs "
                        "internally); it may still be null if the SDK didn't log "
                        "a typed exception. Records tool NAME + count and "
                        "exception TYPE only, never arguments, messages, or "
                        "tracebacks; PII-free; privacy posture in SECURITY.md is "
                        "unchanged."
                    ),
                }
                await _send_status(send, 200, payload)
                return

            # Landing page at / (public, no auth). Tells humans what they
            # hit and gives Google's favicon scraper the <link rel="icon">
            # tags it needs to find our PNG.
            if path == "/":
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/html; charset=utf-8"),
                        (b"content-length", str(len(INDEX_HTML)).encode("ascii")),
                        (b"cache-control", b"public, max-age=300"),
                    ],
                })
                await send({"type": "http.response.body", "body": INDEX_HTML})
                return

            # Favicons: public, no auth. Google's s2/favicons service
            # rejects SVG, so /favicon.ico and /favicon.png must serve
            # PNG bytes for the icon to appear in Anthropic's Directory
            # + Claude tool-call UI. /favicon.svg keeps SVG for modern
            # browsers.
            if path in ("/favicon.ico", "/favicon.png") and FAVICON_PNG is not None:
                await _serve_static_icon(
                    send, scope, FAVICON_PNG, b"image/png", _FAVICON_PNG_ETAG)
                return
            if path == "/favicon.svg" or (
                path == "/favicon.ico" and FAVICON_PNG is None
            ):
                await _serve_static_icon(
                    send, scope, FAVICON_SVG, b"image/svg+xml", _FAVICON_SVG_ETAG)
                return

            # Smithery + similar registries probe this for auto-discovery.
            # Spec: https://smithery.ai/docs/build/publish#troubleshooting
            if path == "/.well-known/mcp/server-card.json":
                card: dict[str, Any] = {
                    "serverInfo": {
                        "name": "estonian-mcp",
                        "version": SERVER_VERSION,
                        "description": SERVER_INSTRUCTIONS,
                    },
                    "description": SERVER_INSTRUCTIONS,
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
                if limiter.note_denial(bucket_key):
                    _STATS["rate_limit_episodes"] += 1
                await _send_status(send, 429, {"error": "rate_limited"})
                return

            # Bucket MCP traffic by JSON-RPC method. Only an authorized,
            # non-rate-limited POST /mcp gets here; for those we buffer the
            # JSON-RPC body to peek at the `method`, then replay it to the
            # inner app byte-for-byte. Nothing from the body is stored: we
            # bump _STATS["sessions"] on the fact of an initialize, and
            # bucket the method into a FIXED allowlist so a caller-supplied
            # string can never become a metrics key. All other traffic (GET
            # SSE streams, other paths) passes straight through with the
            # original receive.
            receive_for_inner = receive
            if scope.get("method") == "POST" and path in ("/mcp", "/mcp/"):
                consumed, body = await _drain_body(receive)
                method = _classify_mcp_method(body)
                if method is not None:
                    _STATS["mcp_methods"][method] = (
                        _STATS["mcp_methods"].get(method, 0) + 1
                    )
                    if method == "initialize":
                        _STATS["sessions"] += 1
                receive_for_inner = _replay_receive(consumed, receive)

            await inner(scope, receive_for_inner, send)
        except Exception as exc:
            # Defence-in-depth. The MCP SDK already converts tool
            # exceptions into JSON-RPC error responses, so reaching here
            # means something failed OUTSIDE normal dispatch (transport,
            # a pre-dispatch parse failure, or this wrapper itself).
            #
            # Log a minimal, PII-free breadcrumb (exception type + path
            # only, never the request body or token) so a recurrence is
            # greppable in `fly logs` without weakening the privacy
            # posture. Then, if the response hasn't started yet, return a
            # clean 500 instead of letting it surface as a raw crash.
            #
            # We catch Exception, NOT BaseException: asyncio.CancelledError
            # (the client disconnecting from a long-lived SSE GET) is a
            # BaseException, so it passes through here and normal stream
            # teardown proceeds untouched.
            log.error("unhandled error on %s: %s", path, type(exc).__name__)
            # Stash the exception type so the send wrapper records it on the
            # ring buffer when the 500 below goes out.
            captured["error"] = type(exc).__name__
            if captured["status"] == 0:
                await _send_status(send, 500, {"error": "internal_error"})
            else:
                # Response already in flight (e.g. mid-SSE-stream); we can't
                # cleanly send a 500, so let the server framework close the
                # half-sent connection. No new http.response.start will fire,
                # so record the breadcrumb directly here.
                _record_error(path, captured["status"], type(exc).__name__)
                raise
        finally:
            _stats_record(captured["status"] or 0, path)

    return app


def _run_http(host: str, port: int, token: str | None, rate_limit: int, public_mode: bool) -> None:
    import atexit

    import uvicorn  # local import; only needed in HTTP mode

    # Restore metrics from disk if a Fly volume (or local override) has them.
    _load_persistent_stats()
    # Best-effort final flush on shutdown so we capture the last interval.
    atexit.register(_save_persistent_stats)

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
        # proxy_headers is OFF on purpose. With it on (and
        # forwarded_allow_ips="*") uvicorn rewrites scope["client"] from the
        # LEFTMOST X-Forwarded-For entry, which is fully caller-controlled:
        # that is what let a caller defeat the per-IP rate limiter by
        # varying the header. We interpret XFF ourselves in _client_ip,
        # counting from the right, and we need scope["client"] to stay the
        # true peer so it is a trustworthy fallback.
        proxy_headers=False,
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
    _load_domain_vocab(public=args.transport != "stdio" and args.public)

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
