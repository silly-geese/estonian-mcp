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
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, TypedDict

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

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

# How many reverse proxies sit in front of this server. Used to pick the
# trustworthy entry out of X-Forwarded-For for the public-mode per-IP rate
# limiter — see _client_ip. Fly.io puts exactly one proxy in front, which
# is the default. Set to 0 if the server is directly internet-exposed, so
# that a caller-supplied XFF is never trusted.
_TRUSTED_PROXY_HOPS = max(0, int(os.environ.get("ESTNLTK_MCP_TRUSTED_PROXY_HOPS", "1")))

# Bumped manually in lockstep with pyproject.toml's [project].version.
SERVER_VERSION = "0.5.5"

# Favicons served alongside the MCP endpoint so Google's favicon service
# (used by the Anthropic Connectors Directory + tool-call UI in Claude)
# can fetch our icon when probing estonian-mcp.fly.dev.
#
# Google's pipeline only accepts raster (PNG/ICO/JPG) — it rejects SVG,
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
# fall back to serving the SVG at the .ico path — which Google still
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
<meta name="description" content="Estonian NLP MCP server \xe2\x80\x94 spell-check, morphology, synonyms, NER for AI agents writing Estonian.">
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
<p>Estonian NLP MCP server \xe2\x80\x94 spell-check, morphology, synonyms, NER, and more, exposed as MCP tools so AI agents stop hallucinating Estonian.</p>
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
    "Estonian NLP tools for AI agents that write, edit, or proofread "
    "Estonian. Provides spell-check, morphological analysis, lemmatization, "
    "POS tagging, tokenization, syllabification, named-entity recognition, "
    "WordNet synonyms, fastText related words, full inflection paradigms, and "
    "EKI-Reeglid orthography checks (capitalization, compound writing, "
    "commas, number formatting, abbreviation hyphenation), plus object-case, "
    "register, style, redundancy and calque-risk checks. For legal Estonian, "
    "check_legalese aids plain-language simplification while listing the "
    "terms of art that must be preserved, check_defined_terms maps "
    "defined terms and cross-references in long documents, and "
    "common_legal_usage returns canonical legal collocations for a term so "
    "you use real legalese instead of inventing phrasings. "
    "Use these tools as ground truth rather than guessing Estonian spelling, "
    "case forms, or inflections — language models routinely hallucinate "
    "plausible-but-wrong Estonian morphology. Note that spell_check passing "
    "does NOT prove a word is real Estonian: Vabamorf accepts any "
    "morphologically valid compound, including ones you just coined, so "
    "verify coined or unusual compounds with check_compound_familiarity "
    "before using them. "
    "REACH FOR THESE TOOLS ON EDITORIAL QUESTIONS TOO, not just mechanical "
    "ones. 'Is this the right word here?' → synonyms, and read each "
    "definition's domain constraint against the context rather than "
    "trusting the word's ML-jargon familiarity. 'Make this read more "
    "human' / 'this is too bureaucratic' → check_officialese for "
    "kantseliit in reports, academic and business prose (check_legalese "
    "is for statutes and stays silent on those), plus check_style for "
    "umbisikuline tegumood and rhythm. 'Keep the terminology "
    "consistent across this document' → check_term_consistency. A word "
    "can be correctly spelled, morphologically valid and still the wrong "
    "register or the wrong term for its domain — the mechanical checks "
    "will not tell you that. All tools are read-only and "
    "operate on Estonian text; results are returned in UTF-8 (preserve "
    "õ/ä/ö/ü/š/ž)."
)

mcp = FastMCP("estonian-mcp", instructions=SERVER_INSTRUCTIONS)
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
    "(the server never downloads resources by itself — see PRIVACY.md)."
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
       missing" rather than "lookup failed" — a false negative on top of a
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


def _wordnet_available() -> bool:
    """True if Estonian WordNet is unpacked on disk, WITHOUT any network.

    Checks the resources directory directly rather than calling
    `get_resource_paths()`, which consults EstNLTK's resource *index* and
    re-fetches that index over HTTPS when it is more than two hours old —
    an outbound call from inside a tool, and a false "missing" verdict
    whenever it fails. See _forbid_resource_downloads.

    Deliberately NOT cached, unlike `_wordnet()` below. Caching the probe
    would reintroduce the very confusion issue #38 was about: an operator
    sees `degraded: true`, runs `scripts/fetch_resources.py` as the message
    tells them to, calls the tool again — and a cached `False` still says
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
# tool — the script cannot export a variable into the server process, and
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
    """Lazy-load the compressed fastText model used by find_related_words
    and check_compound_familiarity."""
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
# culture/language noun. Hand-curated; not exhaustive — covers the
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

# Bigram lexicon for check_compounds. Each (word_a, word_b) — keys are
# lowercased surface tokens — represents a common AI mis-split that
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
# small on purpose — phase-1 coverage.

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
# speech, no special flag).
_POS_USAGE_NOTES_ET: dict[str, tuple[str, str]] = {
    "X": ("foreign", "võõrsõna või tundmatu sõna"),
    "Y": ("abbreviation", "lühend"),
    "I": ("interjection", "interjektsioon"),
    "H": ("proper-noun", "pärisnimi"),
}

# Paradigm form lists for the new `paradigm` tool. Forms passed to
# Vabamorf.synthesize(lemma, form, pos). Phase-1 scope: the most
# commonly-needed forms per word class, not every possible form.

_NOMINAL_FORMS: tuple[str, ...] = (
    "sg n", "sg g", "sg p", "sg ill", "sg in", "sg el", "sg all",
    "sg ad", "sg abl", "sg tr", "sg ter", "sg es", "sg ab", "sg kom",
    "pl n", "pl g", "pl p", "pl ill", "pl in", "pl el", "pl all",
    "pl ad", "pl abl", "pl tr", "pl ter", "pl es", "pl ab", "pl kom",
)

# Human-readable Estonian labels for the case forms.
_CASE_LABELS_ET: dict[str, str] = {
    "sg n": "ainsuse nimetav", "sg g": "ainsuse omastav",
    "sg p": "ainsuse osastav", "sg ill": "ainsuse sisseütlev",
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
    "ma", "da", "vat", "mas", "mast", "mata",
    # present indicative (1sg, 2sg, 3sg, 1pl, 2pl, 3pl)
    "n", "d", "b", "me", "te", "vad",
    # past indicative
    "sin", "sid", "s", "sime", "site",
    # conditional
    "ksin", "ksid", "ks", "ksime", "ksite", "ksid",
    # participles
    "nud", "tud", "v", "tav", "tava",
    # imperative (mostly 2nd / 3rd person)
    "gu", "gem", "ge",
)

# Passive-voice form codes from Vabamorf. When analyze_morphology
# returns one of these as the `form` for a V-pos word, the verb is in
# passive voice — Estonian's -takse / -ti / -tud / -tav family.
_PASSIVE_FORMS_ET: frozenset[str] = frozenset({
    "takse", "dakse",   # present passive
    "ti", "di",         # past passive (e.g. tehti, kasutati)
    "tud", "dud",       # past passive participle (tehtud, kasutatud)
    "tav", "dav",       # present passive participle (tehtav, kasutatav)
    "tava", "dava",     # umbisikuline kesksõna
    "taks", "daks",     # passive conditional
})

# Hedging / wishy-washy markers — counted to gauge how confident the
# prose reads. Higher density = more uncertain / less assertive copy.
# Hand-curated; single-word entries only (multi-word hedging phrases
# left for a later round).
# Lexicons for check_object_case — Estonian's object-case-government
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


_HEDGING_WORDS_ET: frozenset[str] = frozenset({
    "võib-olla", "võibolla", "umbes", "vist", "pigem", "äkki",
    "ehk", "ilmselt", "arvatavasti", "tõenäoliselt", "mõnevõrra",
    "veidi", "üpris", "tundub", "näiliselt", "ligilähedaselt",
})

# POS tags whose lemmas we IGNORE when counting repetition — function
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
    "vat": "vat-vorm", "mas": "mas-vorm",
    "mast": "mast-vorm", "mata": "mata-vorm",
    "n": "olevik 1.p ainsus", "d": "olevik 2.p ainsus",
    "b": "olevik 3.p ainsus", "me": "olevik 1.p mitmus",
    "te": "olevik 2.p mitmus", "vad": "olevik 3.p mitmus",
    "sin": "lihtminevik 1.p ainsus", "sid": "lihtminevik 2.p ainsus / 3.p mitmus",
    "s": "lihtminevik 3.p ainsus", "sime": "lihtminevik 1.p mitmus",
    "site": "lihtminevik 2.p mitmus",
    "ksin": "tingiv 1.p ainsus", "ksid": "tingiv 2.p ainsus / 3.p mitmus",
    "ks": "tingiv 3.p ainsus", "ksime": "tingiv 1.p mitmus",
    "ksite": "tingiv 2.p mitmus",
    "nud": "mineviku kesksõna", "tud": "tegumoeline kesksõna",
    "v": "olevikuline kesksõna", "tav": "tegumoeline olevikuline kesksõna",
    "tava": "umbisikuline olevikuline kesksõna",
    "gu": "käskiv 3.p ainsus", "gem": "käskiv 1.p mitmus",
    "ge": "käskiv 2.p mitmus",
}


# Subordinating / coordinating conjunctions where Estonian comma rules
# require a comma immediately before. `kui`, `mis`, `kes` deliberately
# excluded — they're highly context-dependent (kui = "when/if" needs
# comma but kui = "than/as" doesn't; mis can be relative or
# interrogative; kes similar) and the false-positive cost outweighs
# the catch rate for v1.
_COMMA_BEFORE: frozenset[str] = frozenset({
    "et", "kuna", "sest", "kuigi", "kuid", "vaid", "nagu",
    "mistõttu", "millepärast", "kuhu",
})


# Lexicons for check_redundancy — semantic doubling that's
# grammatically fine but reads redundant to a native speaker. Kept
# deliberately high-precision: better to miss than to flag legitimate
# phrasing, since each flag costs the user attention.

# Sets of adverbs/particles that all mean roughly the same thing
# ("also / too / likewise"). Two DIFFERENT members appearing adjacent
# is the classic pleonasm — e.g. "samuti ka", "ka samuti".
_ALSO_PARTICLES_ET: frozenset[str] = frozenset({"samuti", "ka", "ühtlasi"})

# Adjectives that are already absolute / non-gradable: putting "kõige"
# (most) in front is a double superlative — "kõige optimaalsem" is
# wrong the way "most optimal" is. Matched by STEM PREFIX rather than
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
# invited to expand these (see CONTRIBUTING) — they are precision-first,
# not exhaustive.

# Archaic "kantseliit" filler whose plain equivalent does NOT change legal
# meaning. Matched by surface-prefix so inflected forms are caught. Terms
# of ART (below) are deliberately NOT here — those must survive simplification.
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
# Officialese (kantseliit) lexicons — the NON-legal sibling of the legalese
# set above. Aimed at reports, academic prose, grant/R&D paperwork and
# business writing, where check_legalese finds nothing because its lexicon
# is legal-specific and its length gate is tuned for statutes.
#
# Matched on LEMMA (exact), not surface prefix: prefix matching would fire
# on 'oma'/'omadus' for the 'omama' entry. Precision-first, per repo
# convention — a missed flag costs less than a wrong one.
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
_OFFICIALESE_PHRASES_ET: dict[tuple[str, str], tuple[str, str]] = {
    ("kujutab", "endast"): ("on", "kantseliit; piisab tegusõnast 'on'"),
    ("kujutavad", "endast"): ("on", "kantseliit; piisab tegusõnast 'on'"),
    ("viidi", "läbi"): ("tehti / korraldati", "tugiverb 'läbi viima'; kasuta konkreetset tegusõna"),
    ("viiakse", "läbi"): ("tehakse / korraldatakse", "tugiverb 'läbi viima'; kasuta konkreetset tegusõna"),
    ("läbi", "viia"): ("teha / korraldada", "tugiverb 'läbi viima'; kasuta konkreetset tegusõna"),
    ("läbi", "viidud"): ("tehtud / korraldatud", "tugiverb 'läbi viima'; kasuta konkreetset tegusõna"),
    ("selles", "osas"): ("selle kohta", "kantseliitlik 'osas'; 'kohta' on lihtsam"),
    ("selle", "osas"): ("selle kohta", "kantseliitlik 'osas'; 'kohta' on lihtsam"),
    ("mille", "osas"): ("mille kohta", "kantseliitlik 'osas'; 'kohta' on lihtsam"),
    ("seoses", "sellega"): ("seetõttu", "kantseliit; 'seetõttu' on lühem"),
    ("arvestades", "asjaolu"): ("kuna", "kantseliit; alusta kõrvallauset sõnaga 'kuna'"),
}

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
# R&D-report officialese as 'neutraalne', score 0.0, zero markers — these
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

# Below this word count classify_register scores on the lexicon alone —
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
    """True if a token is a specialised Estonian legal term of art — matched
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
# da-infinitive code — these only count when a negation precedes.
_IMPERSONAL_NEG_FORMS_ET: frozenset[str] = frozenset({"ta", "da"})

# VERBAL negation only — the subset of _NEGATION_LEMMAS_ET that actually
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
    2. `ta`/`da` (impersonal present negative — `ei esitata`) is a form
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

        # (2) impersonal present negative — only after a negation.
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
# actually runs — NOT on initialize / tools/list / SSE stream opens, so
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
            # caller to run nltk.download() — advice this server
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
# Output-schema types. total=False so every field is optional — this
# advertises a structured output schema to clients (Smithery quality
# score) without FastMCP rejecting a return that conditionally omits a
# key. Inner shapes stay loose (list[dict]/dict) on purpose; the
# top-level schema is what matters.
# ---------------------------------------------------------------------------

class _TokenizeResult(TypedDict, total=False):
    sentences: list[str]
    words: list[str]


class _ParadigmResult(TypedDict, total=False):
    input: str
    lemma: str
    partofspeech: str
    word_class: str
    forms: list[dict]
    summary_estonian: str
    note: str


class _RelatedWordsResult(TypedDict, total=False):
    word: str
    matches: list[dict]


class _SynonymsResult(TypedDict, total=False):
    word: str
    synsets: list[dict]
    synset_count: int


class _HyphenationResult(TypedDict, total=False):
    word: str
    breaks: list[int]
    preferred: str
    syllable_count: int
    summary_estonian: str
    note: str


class _CompoundFamiliarityResult(TypedDict, total=False):
    text: str
    compounds_analysed: int
    suspect_compounds: list[dict]
    all_compounds: list[dict]
    summary_estonian: str
    note: str


class _StyleResult(TypedDict, total=False):
    text: str
    repetition: dict
    passive_voice: dict
    sentence_length: dict
    hedging: dict
    note: str


class _RegisterResult(TypedDict, total=False):
    tier: str
    tier_estonian: str
    score: float
    formal_markers: list[str]
    colloquial_markers: list[str]
    consistency: dict
    structure: dict
    word_count: int
    note: str


class _CheckResult(TypedDict, total=False):
    """Shared output shape for the issue-list orthography/grammar checks."""
    text: str
    issues: list[dict]
    summary_estonian: str
    note: str


class _LegaleseResult(TypedDict, total=False):
    """Output of check_legalese: simplification hints + terms to preserve."""
    text: str
    issues: list[dict]
    terms_of_art: list[dict]
    summary_estonian: str
    note: str


class _DefinedTermsResult(TypedDict, total=False):
    """Output of check_defined_terms: defined-term map + cross-references."""
    text: str
    defined_terms: list[dict]
    cross_references: list[dict]
    issues: list[dict]
    summary_estonian: str
    note: str


class _LegalUsageResult(TypedDict, total=False):
    """Output of common_legal_usage: canonical collocations for a legal term."""
    word: str
    lemma: str
    found: bool
    frequency: int
    common_before: list[dict]
    common_after: list[dict]
    summary_estonian: str
    note: str


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
def tokenize(text: Annotated[str, Field(description="Estonian text to split into sentences and words.")]) -> _TokenizeResult:
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


def _usage_note(lemma: str | None, pos: str | None) -> tuple[str | None, str | None]:
    """Return (code, estonian_note) for a word, or (None, None) if neutral.

    Priority: POS-tag markers (X, Y, I, H) before lemma-lexicon markers.
    Word is matched lowercased against the lemma lexicon.
    """
    if pos and pos in _POS_USAGE_NOTES_ET:
        return _POS_USAGE_NOTES_ET[pos]
    if lemma:
        key = lemma.lower()
        if key in _MARKED_LEMMAS_ET:
            return _MARKED_LEMMAS_ET[key]
    return None, None


# A small set of lexically indeclinable (muutumatu) Estonian adjectives —
# they keep one form regardless of the noun's case/number. Conservative
# and high-confidence; extend as real cases turn up.
_INDECLINABLE_ADJ_ET: frozenset[str] = frozenset({
    "täis", "eri", "väärt", "katki", "lahti", "valmis", "puru", "segi",
})


# Endings that mark an attributive as invariant when Vabamorf gives us no
# usable analysis. -mata is the tud-participle's negative form (issue #42);
# EKI: "mata-vorm jaab alati kaandumatuks".
_INDECLINABLE_ATTR_ENDINGS: tuple[str, ...] = ("tud", "dud", "nud", "mata")


@lru_cache(maxsize=4096)
def _attr_analyses(word: str) -> tuple[tuple[str, str], ...]:
    """Every (partofspeech, form) pair Vabamorf offers for a word in
    isolation.

    ALL analyses, not just the first: a participle like `tuntud` comes back
    as A/'', V/tud, A/pl n and A/sg n, and picking index 0 would make the
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
        poss, forms = list(span.partofspeech), list(span.form)
        # strict=False on purpose: if Vabamorf ever returns mismatched
        # list lengths, truncating is far better than raising inside a
        # tool call over a morphology detail.
        return tuple((p or "", f or "") for p, f in zip(poss, forms, strict=False))
    except Exception:
        return ()


def _is_indeclinable_attr(
    word: str, analyses: tuple[tuple[str, str], ...] | None = None
) -> bool:
    """True if a word does NOT inflect when used attributively (before a
    noun), so adjective-noun agreement should leave it in base form.

    Three invariant classes:
    - lexical indeclinables (tais, eri, vaart, ...)
    - past participles in -tud / -dud / -nud (`tuntud laulja` stays
      `tuntud` in the genitive, not *tuntu laulja)
    - the -mata form, the tud-participle's negative counterpart, which EKI
      states "jaab alati kaandumatuks": `taitmata lepingute reserv`, not
      *taitmatute. Issue #42.

    WHY THIS IS NOT AN ENDING TEST. Two traps an ending test walks into:

    1. -tu caritive adjectives DO agree, and their nominative plural also
       ends in -tud: `onnetu` -> `onnetud`, `lugematu` -> `lugematud`.
       Freezing those yields *`onnetud laste` for `onnetute laste`.
    2. A noun whose stem ends in -ma forms its abessive in -mata:
       `teema` -> `teemata`, `kliima` -> `kliimata`. Those are inflected
       nouns, not the mata-form.

    So: if any analysis is an adjective, trust morphology -- an invariant
    attributive has an adjective reading with NO case/number form, while a
    declining one only ever carries `sg n` / `pl n`. Otherwise, an
    abessive reading means trap 2 and we decline. Only with no usable
    analysis do we fall back to the ending, which still matters because
    Vabamorf sometimes misanalyses a participle as a noun
    (`hajutatud` -> S/pl n/`hajutatu`) and the ending is right there.

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

    adjective_readings = [f for p, f in analyses if p == "A"]
    if adjective_readings:
        return any(not (f or "").strip() for f in adjective_readings)
    if any((f or "").endswith("ab") for _p, f in analyses):
        return False
    return w.endswith(_INDECLINABLE_ATTR_ENDINGS)


@mcp.tool(annotations=ToolAnnotations(
    title="Estonian morphological analysis",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def analyze_morphology(text: Annotated[str, Field(description="Estonian text to analyse morphologically.")], all_analyses: Annotated[bool, Field(description="Return every ambiguous analysis per word instead of only the most likely one.")] = False) -> list[dict]:
    """Run full morphological analysis on Estonian text.

    For each word returns lemma(s), part-of-speech, grammatical form, root,
    ending, clitic, compound parts, ambiguity info, and a usage note
    flagging archaic / foreign / abbreviation / interjection / proper-noun
    cases. By default returns the first (most likely) analysis per word;
    set `all_analyses=True` to return every ambiguous analysis.

    Each word's response includes:
      - lemma, partofspeech, form, root, ending, clitic, root_tokens
      - analyses_count: how many alternative analyses Vabamorf produced
        for this surface form (>1 means the word is morphologically
        ambiguous)
      - is_ambiguous: shorthand for analyses_count > 1
      - usage_note: machine code (None if neutral)
        — "archaic" / "foreign" / "abbreviation" / "interjection" /
          "proper-noun"
      - usage_note_estonian: human-readable Estonian rendering of the
        same flag (quote this verbatim in Estonian replies; do NOT
        translate the English usage_note yourself)
      - indeclinable: True for words that stay in base form when used
        attributively (lexical indeclinables like `täis`, -tud/-nud past
        participles like `tuntud`, and the -mata form like `täitmata`)
        — i.e. they do NOT take the noun's case ending in agreement. Use this before inflecting a
        noun phrase so you don't wrongly decline an invariant adjective.

    Input is capped at 100,000 characters.
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
        analyses_count = len(lemmas)
        is_ambiguous = analyses_count > 1
        code, et = _usage_note(_first(lemmas), _first(pos))
        indeclinable = _is_indeclinable_attr(
            word, tuple((p or "", f or "") for p, f in zip(pos, forms, strict=False)))
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
            out.append({
                "word": word,
                "analyses": analyses,
                "analyses_count": analyses_count,
                "is_ambiguous": is_ambiguous,
                "usage_note": code,
                "usage_note_estonian": et,
                "indeclinable": indeclinable,
            })
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
                "analyses_count": analyses_count,
                "is_ambiguous": is_ambiguous,
                "usage_note": code,
                "usage_note_estonian": et,
                "indeclinable": indeclinable,
            })
    return out


def _paradigm(word: str) -> dict:
    """Generate a full inflection paradigm for a word.

    Resolves the input through analyze() to find its lemma + POS, then
    calls Vabamorf.synthesize() for each form in the appropriate paradigm
    table.
    """
    _check_text(word, limit=MAX_WORD_CHARS, name="word")
    if any(ch.isspace() for ch in word):
        raise ValueError("paradigm expects a single word, no whitespace")

    vm = _vabamorf()
    # Find the dominant lemma + POS for this word.
    analyses = vm.analyze([word], disambiguate=True)
    if not analyses or not analyses[0].get("analysis"):
        return {
            "input": word,
            "lemma": None,
            "partofspeech": None,
            "forms": [],
            "summary_estonian": f"Sõnale '{word}' paradigmat ei leitud.",
            "note": "Vabamorf couldn't analyse this word.",
        }
    primary = analyses[0]["analysis"][0]
    lemma = primary["lemma"]
    pos = primary["partofspeech"]

    if pos in {"S", "A", "P", "N"}:
        form_list = _NOMINAL_FORMS
        labels = _CASE_LABELS_ET
        class_name = "nominal"
    elif pos == "V":
        form_list = _VERB_FORMS
        labels = _VERB_LABELS_ET
        class_name = "verb"
    else:
        return {
            "input": word,
            "lemma": lemma,
            "partofspeech": pos,
            "forms": [],
            "summary_estonian": (
                f"Sõnaliik '{pos}' ei käändu ega pöördu — paradigmat pole."
            ),
            "note": (
                "This part of speech does not inflect (e.g. adverbs, "
                "conjunctions, particles). No paradigm to generate."
            ),
        }

    forms: list[dict] = []
    for f in form_list:
        try:
            generated = vm.synthesize(lemma, f, pos)
        except Exception:
            generated = []
        if not generated:
            continue
        forms.append({
            "form": f,
            "form_estonian": labels.get(f, f),
            "surface": generated[0] if len(generated) == 1 else generated,
        })

    return {
        "input": word,
        "lemma": lemma,
        "partofspeech": pos,
        "word_class": class_name,
        "forms": forms,
        "summary_estonian": (
            f"Sõna '{lemma}' ({pos}) paradigma: {len(forms)} vormi."
        ),
        "note": (
            "Generated via Vabamorf.synthesize. Some forms may be marked, "
            "rare, or stylistically odd — Vabamorf produces what's "
            "morphologically possible, not what a native speaker would "
            "necessarily use. For ambiguous lemmas pass the bare lemma "
            "(e.g. 'kasutama') rather than an inflected form for the "
            "cleanest result."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Generate Estonian inflection paradigm",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def paradigm(word: Annotated[str, Field(description="A single Estonian word (lemma or inflected form) to generate the full paradigm for.")]) -> _ParadigmResult:
    """Generate the full inflection paradigm for an Estonian word.

    For nominals (nouns, adjectives, pronouns, numerals): produces all 14
    cases × 2 numbers = up to 28 forms. For verbs: produces infinitives,
    present/past/conditional indicative, imperative, and participles
    (~30 forms). Other parts of speech (adverbs, conjunctions,
    particles) don't inflect — `forms` is empty.

    Each form entry has the Vabamorf form code (e.g. `sg p`, `ksin`),
    its Estonian label (e.g. `ainsuse osastav`, `tingiv 1.p ainsus`),
    and the surface form Vabamorf generated. Use `form_estonian` verbatim
    in Estonian replies — don't translate the English `form` code.

    Phase-1 scope: covers the most commonly-needed forms per word class,
    not every theoretical form Vabamorf can produce. Single-word input,
    capped at 200 characters.
    """
    return _paradigm(word)


@mcp.tool(annotations=ToolAnnotations(
    title="Lemmatize Estonian words",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def lemmatize(text: Annotated[str, Field(description="Estonian text to reduce to dictionary-form lemmas.")]) -> list[dict]:
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
@_counted
def pos_tag(text: Annotated[str, Field(description="Estonian text to part-of-speech tag.")]) -> list[dict]:
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
@_counted
def spell_check(text: Annotated[str, Field(description="Estonian text to spell-check.")], suggestions: Annotated[bool, Field(description="Include correction suggestions for misspelled words.")] = True) -> list[dict]:
    """Check Estonian spelling for each word and optionally return suggestions.

    Returns one entry per word with `text`, `spelling` (bool), and
    `suggestions` (list of correction candidates) when `suggestions=True`.
    Input is capped at 100,000 characters.

    CAVEAT: Vabamorf accepts ANY morphologically well-formed word,
    including compounds you just invented (e.g. `toortõlkeoht`) — it
    splits them into valid roots and reports `spelling: true`. So passing
    spell_check does NOT mean a word is real, attested Estonian. For a
    coined or unusual compound, confirm it with `check_compound_familiarity`
    before trusting it.
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
@_counted
def syllabify(word: Annotated[str, Field(description="A single Estonian word (no whitespace) to split into syllables.")]) -> list[dict]:
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
@_counted
def named_entities(text: Annotated[str, Field(description="Estonian text to extract named entities (people, places, organisations) from.")]) -> list[dict]:
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
@_counted
def find_related_words(word: Annotated[str, Field(description="A single Estonian word to find semantically related words for.")], n: Annotated[int, Field(description="How many nearest-neighbour words to return (1-50).")] = 10) -> _RelatedWordsResult:
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
@_counted
def synonyms(word: Annotated[str, Field(description="A single Estonian word to look up WordNet synonyms for.")], max_synsets: Annotated[int, Field(description="Maximum number of word-sense synsets to return.")] = 5) -> _SynonymsResult:
    """Look up Estonian synonyms via WordNet.

    Returns synsets (groups of synonymous lemmas) for the input word, each
    with its definition and example usages. Useful when you want Claude to
    pick a different word with the same meaning, e.g. swap an over-used
    verb in marketing copy. Word-sense ambiguity is preserved: a polysemous
    word returns multiple synsets, one per meaning. Input capped at 200
    characters.

    WORD-FIT CHECK: when the question is "is this the right word here?"
    rather than "give me an alternative", READ EACH `definition` and test
    it against the user's actual context — do not just harvest `lemmas`.
    Estonian glosses routinely carry a domain constraint that decides the
    answer: `korpus` returns the sense "kirjaliku või suulise teksti
    elektrooniline kogu", so calling a set of IMAGES a `korpus` is wrong
    however natural it sounds in ML jargon; `andmestik` carries no such
    constraint. A gloss naming a medium, field, or material is a
    constraint on where the word may be used. Note also that a word can
    be well-formed, correctly spelled and still the wrong register — for
    that, check_officialese and classify_register, not this tool.
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
            "(the server never downloads resources by itself — see "
            "PRIVACY.md)."
        )
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

    # Structural signals. The tool's own note always conceded that real
    # register lives in syntax as much as vocabulary — and a dense R&D
    # report scored 'neutraalne', 0.0, with zero markers, while 87.5% of
    # its verbs were umbisikuline tegumood. Impersonal voice and noun
    # density now contribute, bounded and additive-only:
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
        "note": (
            "Heuristic classifier — lexicon-based and lemma-aware, plus "
            "two structural signals. The lexicon covers legal-"
            "administrative AND academic/report vocabulary. `structure` "
            "adds umbisikuline tegumood ratio and noun/verb density, "
            "bounded at +0.4 and applied only from 25 words up and only "
            "when the lexicon is not net-colloquial — without them a dense "
            "R&D report scored 'neutraalne' at 0.0 while 87.5% of its "
            "verbs were impersonal. The `consistency` field flags texts "
            "that carry BOTH formal AND colloquial markers — useful for "
            "catching jarring register-mixing even when the overall "
            "tier rounds to 'neutral'. Treat as a directional hint, not "
            "a verdict; for a full kantseliit breakdown use "
            "check_officialese. When composing an Estonian-language reply, "
            "USE THE tier_estonian AND consistency.summary_estonian "
            "FIELDS VERBATIM rather than translating yourself — common "
            "mistranslations include 'formalne' (wrong) vs 'formaalne' "
            "(correct)."
        ),
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
        explanation: str | None = None

        if lemma_lower in _WEEKDAYS_ET:
            rule = "weekday"
            rule_estonian = "nädalapäev"
            explanation = (
                "Estonian weekday names are written with a lowercase initial "
                "letter mid-sentence (Algustäheortograafia, EKI Reeglid). "
                "Capitalize only at the start of a sentence."
            )
        elif lemma_lower in _MONTHS_ET:
            rule = "month"
            rule_estonian = "kuu nimi"
            explanation = (
                "Estonian month names are written with a lowercase initial "
                "letter mid-sentence (Algustäheortograafia, EKI Reeglid). "
                "Capitalize only at the start of a sentence."
            )
        elif lemma_lower in _NATIONALITIES_ET:
            rule = "nationality"
            rule_estonian = "rahvuse nimetus"
            explanation = (
                "Estonian nationality names (eestlane, soomlane, sakslane, …) "
                "are lowercase mid-sentence (Algustäheortograafia, EKI Reeglid). "
                "Capitalize only at the start of a sentence."
            )
        elif word.lower() in _LANG_ADJECTIVES_ET:
            # Country/language adjectives are lowercase only when used
            # attributively before a culture/language noun. The
            # capitalized form is a valid proper-noun usage on its own
            # (Eesti, Eestit, Eestis = the country).
            #
            # NOTE: match the surface form, not the lemma — Vabamorf
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
                explanation = (
                    "Language and culture adjectives derived from country "
                    "names are lowercase when attributive (eesti keel, vene "
                    "kultuur, soome saun). Capitalize only as a country "
                    "proper noun on its own (Eesti, Eesti Vabariik, Eestis)."
                )

        if rule is None:
            continue

        issues.append({
            "word": word,
            "position": span.start,
            "rule": rule,
            "rule_estonian": rule_estonian,
            "explanation": explanation,
            "suggestion": word[0].lower() + word[1:],
        })

    return {
        "text": text,
        "issues": issues,
        "summary_estonian": (
            f"Leiti {len(issues)} algustäheortograafia viga." if issues
            else "Algustäheortograafia probleeme ei leitud."
        ),
        "note": (
            "Heuristic Algustäheortograafia checker — covers the four most "
            "common AI-generated mistakes (weekdays, months, nationalities, "
            "and language/culture adjectives before related nouns). Not a "
            "full EÕS substitute; edge cases like proper-noun brand names "
            "containing a culture word (e.g. a restaurant called 'Eesti Köök') "
            "may produce a false positive that the user can ignore. When "
            "surfacing rule labels in an Estonian reply, USE THE rule_estonian "
            "FIELD VERBATIM rather than translating `rule` yourself."
        ),
    }


def _check_compounds(text: str) -> dict:
    """Phase-1 liitsõnaõigekiri checker — scans for common AI splits."""
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
                "explanation": (
                    f"In Estonian, '{joined}' is a single compound word "
                    f"and should be written together (liitsõnaõigekiri, EKI "
                    f"Reeglid). The split form is a common AI mistake."
                ),
                "suggestion": joined,
            })

    return {
        "text": text,
        "issues": issues,
        "summary_estonian": (
            f"Leiti {len(issues)} liitsõnaõigekirja viga." if issues
            else "Liitsõnaõigekirja probleeme ei leitud."
        ),
        "note": (
            "Heuristic liitsõnaõigekiri checker — flags ~30 common "
            "AI-generated compound-splits per a hand-curated bigram "
            "lexicon. NOT exhaustive: Estonian compounding is productive "
            "and many valid compounds aren't in the lexicon. Treat hits "
            "as high-confidence (likely real errors); absence of hits is "
            "NOT proof of compound correctness. When surfacing rule "
            "labels in an Estonian reply, USE THE rule_estonian FIELD "
            "VERBATIM rather than translating `rule` yourself."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian compound writing (liitsõnaõigekiri)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_compounds(text: Annotated[str, Field(description="Estonian text to check for wrongly split compound words.")]) -> _CheckResult:
    """Heuristic Estonian compound-word check (liitsõnaõigekiri).

    Scans for common AI-generated splits of words that should be written
    as a single compound — `kooli maja` (wrong) → `koolimaja` (right),
    `nädala vahetus` (wrong) → `nädalavahetus` (right), etc. Uses a
    curated bigram lexicon (~30 entries covering the highest-frequency
    AI mistakes); not a full liitsõnaõigekiri solver.

    Phase-1 limitations: only catches the bigrams in the lexicon.
    Estonian compounding is highly productive and most valid compounds
    aren't enumerated here. Treat hits as high-confidence; absence of
    hits does not prove the compound writing is correct everywhere.
    Input capped at 100,000 characters.
    """
    return _check_compounds(text)


def _check_punctuation(text: str) -> dict:
    """Phase-1 punctuation checker — comma before subordinating words."""
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["morph_analysis"])
    spans = list(t.morph_analysis)

    issues: list[dict] = []
    skip_prev = {",", ";", ":", "(", "—", "–", "-", ".", "!", "?", "...", "…"}
    for i, span in enumerate(spans):
        word_lower = span.text.lower()
        if word_lower not in _COMMA_BEFORE:
            continue
        if i == 0:
            continue
        prev = spans[i - 1]
        if prev.text in skip_prev:
            continue
        issues.append({
            "word": span.text,
            "position": span.start,
            "rule": "comma-before-clause-conjunction",
            "rule_estonian": "koma alistava sidesõna ees",
            "explanation": (
                f"Estonian punctuation rules require a comma before "
                f"clause-introducing conjunctions like '{span.text}'. "
                f"Insert a comma between the previous word and "
                f"'{span.text}'."
            ),
            "suggestion": f", {span.text}",
        })

    return {
        "text": text,
        "issues": issues,
        "summary_estonian": (
            f"Leiti {len(issues)} kirjavahemärgiviga." if issues
            else "Kirjavahemärgivigu ei leitud."
        ),
        "note": (
            "Heuristic comma checker — catches missing commas before "
            "the most common subordinating conjunctions (et, kuna, sest, "
            "kuigi, kuid, vaid, nagu, mistõttu, millepärast, kuhu). "
            "Excludes `kui` / `mis` / `kes` because their function is "
            "context-dependent (kui = than/when, mis = which/what) and "
            "naive flagging produces too many false positives. NOT a full "
            "Estonian punctuation rule engine — listing comma, "
            "apposition comma, and dash/colon rules are out of scope for "
            "phase 1. Quote `rule_estonian` verbatim in Estonian replies."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian punctuation (kirjavahemärgid)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_punctuation(text: Annotated[str, Field(description="Estonian text to check for missing commas before subordinating conjunctions.")]) -> _CheckResult:
    """Heuristic Estonian punctuation check — comma-before-clause rule.

    Flags missing commas before subordinating conjunctions where Estonian
    rules require one: et (that/in order to), kuna (because), sest
    (because), kuigi (although), kuid (but), vaid (rather), nagu (like),
    mistõttu (because of which), millepärast, kuhu.

    Phase-1 limitations: only the comma-before-clause-conjunction rule
    is covered. `kui`, `mis`, `kes` are deliberately excluded because
    their function is contextual (kui = than/as in comparisons doesn't
    need a comma). Listing commas, apposition commas, dash and colon
    rules — all out of scope for phase 1. Input capped at 100,000
    characters.
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
        return {
            "word": word,
            "breaks": [],
            "preferred": word,
            "syllable_count": len(syls),
            "summary_estonian": "Sõna on liiga lühike poolitamiseks.",
            "note": (
                "Single-syllable Estonian words can't be hyphenated "
                "across lines."
            ),
        }
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
    return {
        "word": word,
        "breaks": breaks,
        "preferred": preferred,
        "syllable_count": len(syls),
        "summary_estonian": (
            f"Lubatud poolitamiskohad: {breaks}." if breaks
            else "Sõnal puuduvad turvalised poolitamiskohad."
        ),
        "note": (
            "Phase-1 hyphenation: syllable-boundary based, with the "
            "edge-character rule that you can't leave fewer than 2 "
            "characters before or after the break. Compound-boundary "
            "preference is NOT applied yet (Estonian poolitamine "
            "prefers compound seams over syllable seams); for compounds "
            "like 'koolimaja' the morphologically-preferred break is at "
            "the compound seam, which this tool may not surface. "
            "Treat the offsets as a safe-break list, not an authoritative "
            "preference."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian word hyphenation (poolitamine)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_hyphenation(word: Annotated[str, Field(description="A single Estonian word (no whitespace) to find safe line-break positions for.")]) -> _HyphenationResult:
    """Return safe line-break positions for an Estonian word (poolitamine).

    Different from `syllabify` (which is phonological): this returns
    character offsets where a typesetter can legally break the word
    across lines. Applies the no-orphan-edge rule (don't leave fewer
    than 2 characters before or after the break point).

    Phase-1 limitation: pure syllable-boundary based. Compound-boundary
    preference (Estonian poolitamine prefers `kooli-maja` over
    `koo-limaja`) is not yet applied. Input must be a single word with
    no whitespace, capped at 200 characters.
    """
    return _check_hyphenation(word)


def _check_numbers(text: str) -> dict:
    """Phase-1 number-writing checker — separator rules only."""
    _check_text(text)
    import re
    issues: list[dict] = []

    # Decimal with period instead of Estonian comma. Skip patterns that
    # look like dates (\d+\.\d+\.\d+) or version numbers / IPs by
    # excluding matches whose tail is followed by another period+digit.
    for m in re.finditer(r"(?<![\d.])(\d+)\.(\d+)(?![\d.])", text):
        # if followed by ".\d+" (date-like), skip
        rest = text[m.end():]
        if rest.startswith(".") and len(rest) > 1 and rest[1].isdigit():
            continue
        original = m.group(0)
        corrected = f"{m.group(1)},{m.group(2)}"
        issues.append({
            "text": original,
            "position": m.start(),
            "rule": "decimal-separator",
            "rule_estonian": "kümnenduskoma",
            "explanation": (
                "Estonian uses a comma as the decimal separator, not a "
                "period (e.g. 3,14 not 3.14). EKI Reeglid: numbrite "
                "õigekirjutus."
            ),
            "suggestion": corrected,
        })

    # Thousands separator using comma where Estonian uses a space.
    # Matches \d{1,3}(,\d{3})+ where the grouping is exactly 3-digit
    # blocks (real thousand separator), not a decimal like 3,14.
    for m in re.finditer(r"(?<!\d)\d{1,3}(?:,\d{3})+(?!\d)", text):
        original = m.group(0)
        corrected = original.replace(",", " ")
        issues.append({
            "text": original,
            "position": m.start(),
            "rule": "thousands-separator",
            "rule_estonian": "tuhandeliste eraldaja",
            "explanation": (
                "Estonian uses a non-breaking space (or thin space) as "
                "the thousands separator, not a comma. EKI Reeglid: "
                "numbrite õigekirjutus."
            ),
            "suggestion": corrected,
        })

    return {
        "text": text,
        "issues": issues,
        "summary_estonian": (
            f"Leiti {len(issues)} numbrite õigekirjutuse viga." if issues
            else "Numbrite õigekirjutuse vigu ei leitud."
        ),
        "note": (
            "Heuristic number-writing checker — covers decimal-separator "
            "(period vs comma) and thousands-separator (comma vs space) "
            "rules. Spell-out-vs-digits guidance (Estonian convention: "
            "spell out 1-10 in running text) is out of scope for phase 1 "
            "because it requires context awareness (years, dates, "
            "measurements stay as digits). Quote `rule_estonian` "
            "verbatim in Estonian replies."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian number writing (numbrite õigekirjutus)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_numbers(text: Annotated[str, Field(description="Estonian text to check for number-formatting (decimal comma, thousands space).")]) -> _CheckResult:
    """Heuristic Estonian number-writing check.

    Flags two clear-cut cases per EKI Reeglid:
    - Decimal separator: Estonian uses a comma (3,14), not a period (3.14).
    - Thousands separator: Estonian uses a space (1 000 000), not a
      comma (1,000,000).

    Phase-1 limitations: spell-out-vs-digits guidance (the
    one-to-ten-spelled-out convention) is intentionally not implemented
    — it requires distinguishing measurements, dates, years, and lists
    from running prose, and naive flagging produces too many false
    positives. Input capped at 100,000 characters.
    """
    return _check_numbers(text)


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian capitalization (Algustäheortograafia)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_capitalization(text: Annotated[str, Field(description="Estonian text to check for capitalization errors (Algustäheortograafia).")]) -> _CheckResult:
    """Heuristic Estonian capitalization checker (Algustäheortograafia).

    Scans Estonian text for the most common AI-generated capitalization
    errors per EKI's Reeglid:

    - Weekday names capitalized mid-sentence (Esmaspäeval → esmaspäeval)
    - Month names capitalized mid-sentence (Jaanuaris → jaanuaris)
    - Nationality names capitalized mid-sentence (Eestlane → eestlane)
    - Country/language adjectives capitalized before a culture or
      language noun (Eesti keel → eesti keel; Eesti köök → eesti köök).
      The bare capitalized form on its own (Eesti, Eestis) is left
      alone because it's a valid country proper-noun usage.

    Sentence-initial capitalization is always allowed. All-caps
    acronyms are ignored. Returns each issue with rule code, an
    Estonian rule label (`rule_estonian` — quote this verbatim in
    Estonian replies, don't translate the English `rule`), a
    user-facing explanation, and a suggested correction. Input capped
    at 100,000 characters.

    PHASE-1 LIMITATION: this is a lexicon-based heuristic, not a full
    EÕS implementation. Compound-word capitalization, punctuation
    rules, and hyphenation are NOT covered by this tool (separate
    check_compounds / check_punctuation / check_hyphenation tools may
    follow).
    """
    return _check_capitalization(text)


# Familiarity-verdict thresholds. The fastText nearest-neighbour score
# is a fuzzy proxy for "is this compound actually used in Estonian"; these
# gates turn it into a recall-favouring "worth a second look" flag.
# Tuned against real model output (see tests/test_familiarity.py):
# coinages toortõlkeoht (top 0.571) and mõtteliin (0.536) MUST flag,
# while real OOV compounds tervisekindlustus (0.71) and allalaadimisnupp
# (0.66) must NOT. The old single gate sat at 0.55 — toortõlkeoht slipped
# through at 0.571, which is exactly the miss that motivated this.
_FAMILIARITY_SUSPECT_SCORE = 0.60
_FAMILIARITY_JUNK_RATIO = 0.4


def _looks_like_scrape_junk(word: str) -> bool:
    """True for concatenated web-scrape tokens the compressed model's
    vocabulary carries (e.g. 'KoolKudumidPolosärgidTriiksärgid'). An
    uppercase letter anywhere but the first position never occurs in a
    normal Estonian word, so such a neighbour means fastText fell back to
    character n-grams because the input compound is unfamiliar."""
    return any(ch.isupper() for ch in word[1:])


def _familiarity_verdict(
    in_vocab: bool,
    top_score: float,
    neighbours: list,
    parts: list,
) -> tuple[bool, list[str], dict]:
    """Decide whether a compound is a suspect coinage. Pure (no model I/O)
    so the heuristic is unit-testable against captured fastText output
    without loading the 33 MB model.

    `neighbours` is a list of (word, score) pairs; `parts` is the input
    compound's morphemes (Vabamorf root_tokens). Returns
    (is_suspect, reasons, neighbour_quality).

    in-vocab → never suspect (the word is among the 100K most frequent,
    i.e. attested). Out-of-vocab → suspect when the top neighbour score is
    weak OR the neighbours are dominated by scrape-artifact tokens (the
    mõtteliin failure mode, 4/5 junk) AND that junky tail is decisive —
    see the comment on `tail_is_decisive`. Subword-echo overlap is COUNTED and
    surfaced but never triggers on its own: real sibling compounds share a
    head morpheme too (tervisekindlustus ↔ ravikindlustus), so echoes
    don't discriminate coinages from rare-but-real compounds."""
    names = [n for n, _ in neighbours]
    n = len(names)
    junk = sum(1 for w in names if _looks_like_scrape_junk(w))
    echoes = sum(
        1 for w in names
        if any(len(p) >= 4 and p in w.lower() for p in parts)
    )
    quality = {"neighbours": n, "scrape_junk": junk, "subword_echoes": echoes}

    if in_vocab:
        return False, [], quality

    reasons: list[str] = []
    if top_score < _FAMILIARITY_SUSPECT_SCORE:
        reasons.append(
            f"out-of-vocabulary with weak top similarity "
            f"({top_score:.3f} < {_FAMILIARITY_SUSPECT_SCORE})"
        )
    # The junk-tail gate is only DECISIVE when the compound is also weak at
    # the top, or its nearest neighbour is itself junk. A clean, real,
    # >= 0.60 top neighbour vouches for the compound whatever the tail looks
    # like: a junky tail then describes how sparse that corner of the
    # 100K-vocab model is, not whether the word is real. Without this guard
    # the gate inverted human judgement on ordinary compounds —
    # `pildiandmestik` (top neighbour `andmestik`, 0.71) was flagged while
    # `teadusandmestik` (0.705) passed. mõtteliin still flags: its top
    # neighbour scores 0.536, under the gate.
    top_is_junk = bool(names) and _looks_like_scrape_junk(names[0])
    tail_is_decisive = top_score < _FAMILIARITY_SUSPECT_SCORE or top_is_junk
    if n and (junk / n) >= _FAMILIARITY_JUNK_RATIO and tail_is_decisive:
        reasons.append(
            f"{junk}/{n} nearest neighbours are scrape-artifact tokens, "
            "not real Estonian words"
        )
    return bool(reasons), reasons, quality


def _check_compound_familiarity(text: str) -> dict:
    """Surface fastText neighborhood diagnostic for compound nouns.

    For each compound noun (Vabamorf root_tokens of length >= 2) in the
    text, look up its fastText nearest neighbours. Legitimate Estonian
    compounds are either in-vocab or have semantically coherent neighbours
    with a decent top similarity score (typically >= 0.60). Calques /
    coined compounds tend to be out-of-vocab with a weak top score and/or
    neighbours that are subword echoes of the input's own morphemes or
    web-scrape junk tokens. The suspect decision lives in
    `_familiarity_verdict` (a pure function, unit-tested without the
    model).

    Output is *diagnostic*, not authoritative — the underlying
    fastText-et-medium model has a 100K-word pruned vocabulary, so some
    legitimate but rare compounds also produce weak signal. Treat
    flagged entries as "worth a second look" not "wrong."
    """
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["morph_analysis"])
    kv = _embeddings()
    vocab = kv.key_to_index

    seen: set[str] = set()
    compounds: list[dict] = []

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
        if lemma in seen:
            continue
        seen.add(lemma)

        in_vocab = lemma in vocab
        try:
            neighbours = kv.most_similar(lemma, topn=8)
        except KeyError:
            neighbours = []
        top_score = float(neighbours[0][1]) if neighbours else 0.0

        # Suspect-coinage decision (pure, see _familiarity_verdict): an
        # in-vocab lemma is real; an OOV lemma is flagged when its top
        # similarity is weak OR its neighbours are scrape junk. This
        # catches both mõtteliin (weak score 0.536) and toortõlkeoht
        # (0.571 — over the old 0.55 gate, but OOV with subword-echo /
        # junk neighbours, so still a coinage).
        is_suspect, reasons, quality = _familiarity_verdict(
            in_vocab, top_score, neighbours, parts
        )
        # De-noise legal register: specialised legal compounds (õigussuhe,
        # solidaarvõlgnik, abieluvaraleping) are OOV in the general-web
        # fastText vocab and were false-flagged as coinages (~15% of legal
        # compounds). A known term of art is real by definition.
        #
        # The marker is stamped for EVERY term of art, not only ones whose
        # verdict had to be rescued. Since the junk-tail decisiveness guard
        # landed, a compound like `solidaarvõlgnik` (top neighbour
        # `võlgnik`, 0.625) is already cleared before this runs — but the
        # caller still wants to know it is attested legal vocabulary.
        if _is_legal_term(lemma):
            quality = {**quality, "legal_term": True}
            if is_suspect:
                is_suspect = False
                reasons = []

        compounds.append({
            "word": span.text,
            "lemma": lemma,
            "parts": parts,
            "position": span.start,
            "in_vocab": in_vocab,
            "top_score": round(top_score, 3),
            "top_neighbour": neighbours[0][0] if neighbours else None,
            "neighbours": [
                {"word": n, "score": round(float(s), 3)}
                for n, s in neighbours[:5]
            ],
            "neighbour_quality": quality,
            "is_suspect": is_suspect,
            "reasons": reasons,
        })

    suspects = [c for c in compounds if c["is_suspect"]]

    return {
        "text": text,
        "compounds_analysed": len(compounds),
        "suspect_compounds": suspects,
        "all_compounds": compounds,
        "summary_estonian": (
            f"Tuvastati {len(compounds)} liitsõnanimisõna; "
            f"{len(suspects)} märgiti kahtlaseks (tasub üle vaadata, "
            f"kas tegu on tegeliku eesti keele sõnaga)." if compounds
            else "Liitsõnanimisõnu analüüsiks ei leitud."
        ),
        "note": (
            "Heuristic compound-familiarity check via fastText nearest "
            "neighbours, using a 100K-vocab compressed model. in-vocab "
            "compounds are treated as real Estonian and never flagged. An "
            "out-of-vocab compound is flagged as suspect when EITHER its "
            "top neighbour similarity is below 0.60 OR at least 40% of its "
            "neighbours are scrape-artifact tokens AND that junky tail is "
            "decisive — i.e. the top score is also under 0.60, or the top "
            "neighbour is itself junk (per-entry `reasons` says which). The "
            "0.60 gate catches coinages like 'toortõlkeoht' (top 0.571) "
            "that the older 0.55 gate missed; the junk-neighbour gate "
            "catches calques like 'mõtteliin' whose neighbours are mostly "
            "web-scrape junk. The decisiveness guard stops ordinary "
            "compounds whose nearest neighbour is a real word (e.g. "
            "'pildiandmestik' → 'andmestik', 0.71) from being flagged just "
            "because that corner of the vocabulary is sparse. NOTE the "
            "converse limit: a well-formed compound that is merely STILTED "
            "rather than invented ('teadusandmestik', 0.705) will pass — "
            "similarity cannot judge register or idiom, so use "
            "check_officialese and classify_register for that. `neighbour_"
            "quality` reports the neighbour, scrape_junk and subword_echo "
            "counts. NOT authoritative — even at 100K vocab some legitimate "
            "but rare compounds are OOV; the rule favours recall (a flagged "
            "real compound just gets a second look, a missed coinage "
            "ships). Use the neighbours list to judge: semantically "
            "coherent neighbours (synonyms / related concepts, e.g. "
            "tervisekindlustus → ravikindlustus, elukindlustus) mean the "
            "compound is real; neighbours that just recycle the input's "
            "morphemes or are junk tokens mean a likely coinage. Designed "
            "for Claude inventing literal compounds like 'mõtteliin' "
            "(English 'train of thought'; real Estonian 'mõttekäik')."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian compound familiarity (calque-risk diagnostic)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_compound_familiarity(text: Annotated[str, Field(description="Estonian text whose compound nouns are checked for calque / translationese risk.")]) -> _CompoundFamiliarityResult:
    """fastText-based diagnostic for compound-noun familiarity in Estonian.

    For each compound noun (root_tokens length >= 2), returns its top
    fastText neighbours, a `top_score` similarity, a `neighbour_quality`
    breakdown, and `is_suspect: true` + human-readable `reasons` when the
    compound is out-of-vocab AND its top similarity is below 0.60 OR its
    neighbours are mostly scrape-artifact tokens. This catches both
    `toortõlkeoht` (OOV, top 0.571 — over the old 0.55 gate but a coinage)
    and `mõtteliin` (literal English "train of thought"; real Estonian is
    `mõttekäik`).

    Output is diagnostic, not authoritative. Even with the 100K-vocab
    medium model, some legitimate but rare compounds (e.g.
    `tervisekindlustus`) can still be OOV; the rule favours recall, so a
    flagged real compound just earns a second look. Judge by the included
    neighbours: semantically coherent neighbours (related real words) mean
    the compound is fine; neighbours that recycle the input's morphemes or
    are junk tokens mean a likely coinage.

    Input capped at 100,000 characters.
    """
    return _check_compound_familiarity(text)


def _check_abbreviation_hyphenation(text: str) -> dict:
    """Heuristic Estonian abbreviation-case-ending hyphenation checker.

    Per EKI Reeglid: case endings on Latin-letter / all-caps abbreviations
    are separated from the stem by a hyphen (`MCP-st` not `MCPst`,
    `API-ga` not `APIga`). Uses Vabamorf's POS + form analysis to
    identify tokens Vabamorf recognised as abbreviations carrying a
    case ending, then flags any that aren't already hyphenated.
    """
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["morph_analysis"])

    issues: list[dict] = []
    for span in t.morph_analysis:
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
        suggestion = f"{stem}-{ending}"
        issues.append({
            "word": word,
            "lemma": _first(list(span.lemma)) or "",
            "form": form,
            "position": span.start,
            "rule": "abbreviation-case-ending-hyphen",
            "rule_estonian": "lühendi käändelõpu sidekriips",
            "explanation": (
                f"In Estonian, case endings on Latin-letter abbreviations "
                f"are separated by a hyphen (EKI Reeglid: lühendi-"
                f"ortograafia). '{word}' should be written as "
                f"'{suggestion}'."
            ),
            "suggestion": suggestion,
        })

    return {
        "text": text,
        "issues": issues,
        "summary_estonian": (
            f"Leiti {len(issues)} lühendi käändelõpu sidekriipsu viga." if issues
            else "Lühendiortograafia probleeme ei leitud."
        ),
        "note": (
            "Heuristic checker for the EKI Reeglid rule that case "
            "endings on abbreviations are hyphen-separated (MCP-st, "
            "API-ga, OÜ-le). Uses Vabamorf's Y-pos tag + case form "
            "analysis, so we only flag tokens Vabamorf actually "
            "recognised as abbreviations carrying a case ending. "
            "Single-letter endings on short capital sequences are not "
            "specially filtered — relies on Vabamorf to know whether "
            "'APIs' is an abbreviation plus inessive ending or just "
            "an English plural. Quote rule_estonian verbatim in "
            "Estonian replies."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian abbreviation case-ending hyphenation",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_abbreviation_hyphenation(text: Annotated[str, Field(description="Estonian text to check for abbreviation case-ending hyphenation (MCPst → MCP-st).")]) -> _CheckResult:
    """Heuristic check for the EKI Reeglid rule that case endings on
    Latin-letter / all-caps abbreviations are separated by a hyphen.

    Catches the common AI mistake of writing `MCPst`, `APIga`, `OÜle`
    instead of `MCP-st`, `API-ga`, `OÜ-le`. Uses Vabamorf's POS+form
    analysis to identify tokens recognised as abbreviations carrying a
    case ending; only flags those that aren't already hyphenated.

    Phase-1 scope: matches what Vabamorf tags as `Y` (abbreviation).
    Custom acronyms Vabamorf doesn't know (your brand acronym, niche
    industry shorthand) won't be flagged because Vabamorf doesn't see
    them as abbreviations. Input capped at 100,000 characters.
    """
    return _check_abbreviation_hyphenation(text)


def _check_object_case(text: str) -> dict:
    """Heuristic Estonian object-case-government checker.

    Two rules:
    1. Negation triggers partitive — any noun in nominative or
       genitive in a sentence containing 'ei'/'pole'/'ära'/etc. is a
       likely error.
    2. Partitive-only verbs — a curated set of verbs always take
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

    for sentence in t.sentences:
        sent_spans = [
            s for s in spans
            if s.start >= sentence.start and s.end <= sentence.end
        ]
        if not sent_spans:
            continue

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
        # partitive ("see EI OLE raamat", not *raamatut) — so a negated
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
            if " p" in form:   # partitive ('sg p', 'pl p', 'adt')
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
                    "explanation": (
                        f"Estonian negation (ei/pole/ära/…) requires "
                        f"direct objects in the partitive case. "
                        f"'{word}' is in {form!r} (nominative/genitive); "
                        f"a partitive form of '{lemma}' is likely "
                        f"expected here."
                    ),
                    "suggestion_hint": f"consider partitive form of '{lemma}'",
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
                    "explanation": (
                        f"The verb '{partitive_verb}' takes its direct "
                        f"object in the partitive case. '{word}' is in "
                        f"{form!r} (nominative/genitive); a partitive "
                        f"form of '{lemma}' is likely expected."
                    ),
                    "suggestion_hint": f"consider partitive form of '{lemma}'",
                })

    return {
        "text": text,
        "issues": issues,
        "summary_estonian": (
            f"Leiti {len(issues)} käändevigade kahtlust." if issues
            else "Käändevigade kahtlust ei leitud."
        ),
        "note": (
            "Heuristic phase-1 object-case checker — no syntactic parser, "
            "so we can't distinguish subjects from objects. Flags nouns "
            "in nominative/genitive in sentences with negation or "
            "partitive-only verbs. Subjects of those sentences may be "
            "false-positive flags. Treat as 'worth a second look', not "
            "authoritative corrections. When surfacing rule labels in "
            "Estonian replies, quote rule_estonian verbatim."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian object case (käändeõpetus)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_object_case(text: Annotated[str, Field(description="Estonian text to check for direct-object case errors under negation and partitive-governing verbs.")]) -> _CheckResult:
    """Heuristic Estonian object-case-government check.

    Catches the single biggest class of confidently-wrong Estonian that
    AI agents produce: direct objects in the wrong case after negation
    or after partitive-governing verbs.

    Two rules in phase 1:
    - **Negation → partitive**: any sentence containing 'ei', 'pole',
      'ära', 'ärge', 'ärgu', 'ärgem', or 'mitte' must have direct
      objects in partitive. Flags nominative / genitive nouns.
    - **Partitive-only verbs**: the verbs `armastama`, `vihkama`,
      `vajama`, `soovima`, `ootama`, `austama`, `kartma`, `puudutama`,
      `tundma` always take partitive direct objects. Flags any noun
      in nominative/genitive in the same sentence.

    Phase-1 limitation: no syntactic parser, so we can't perfectly
    distinguish subject from object. Subjects in negation/partitive-verb
    sentences may be flagged as false positives. Treat hits as "worth a
    second look", not authoritative. Proper nouns are skipped. Input
    capped at 100,000 characters.
    """
    return _check_object_case(text)


def _check_redundancy(text: str) -> dict:
    """Heuristic pleonasm / semantic-doubling checker. Flags phrasing
    that is grammatically valid but redundant to a native speaker —
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
                        f"'samuti / ka' — koos on tautoloogia. Vali üks."
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
                        f"'{nxt.text}' on juba absoluutne omadus — 'kõige' "
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
        "text": text,
        "issues": issues,
        "summary_estonian": (
            f"Leiti {len(issues)} liiasuse (pleonasmi) kahtlust."
            if issues else "Liiasust ei tuvastatud."
        ),
        "note": (
            "Heuristic pleonasm checker — flags high-confidence semantic "
            "doubling: adjacent 'also' particles (samuti ka), double "
            "superlatives (kõige optimaalsem), and a small set of fixed "
            "redundant phrases. Deliberately conservative; it does NOT "
            "catch every redundancy a native speaker would hear, so "
            "absence of flags is not proof the text is tight. Quote "
            "rule_estonian verbatim in Estonian replies."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian redundancy / pleonasm",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_redundancy(text: Annotated[str, Field(description="Estonian text to check for pleonasm / redundant word pairs.")]) -> _CheckResult:
    """Heuristic Estonian pleonasm / semantic-doubling check.

    Flags phrasing that is grammatically valid but reads redundant to a
    native speaker — the class of error AI agents produce when they
    stack synonyms. Phase-1 rules, all high-precision:

    - **Doubled 'also' particles**: `samuti ka`, `ka samuti`,
      `ühtlasi ka` — both words mean "also/too", so together they're a
      tautology. (This is the exact `samuti ka suvesärgid` case.)
    - **Double superlative**: `kõige` before an already-absolute
      adjective (`optimaalne`, `ideaalne`, `maksimaalne`, `täiuslik`,
      `ainus`, …) — like English "most optimal". Lemma-matched, so all
      inflected forms count.
    - **Fixed pleonasm phrases**: a small curated set (`ajaline
      periood`, `väike nüanss`, `üldine konsensus`, …).

    Conservative by design — it catches the obvious, high-confidence
    cases, not every redundancy. Absence of flags is not proof the
    prose is tight. Input capped at 100,000 characters.
    """
    return _check_redundancy(text)


def _check_legalese(text: str) -> dict:
    """Heuristic Estonian legalese-simplification aid. Flags archaic
    'kantseliit' filler with plain equivalents, flags over-long/over-nested
    sentences, and — crucially — lists the legal TERMS OF ART that must be
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
                "explanation": (
                    f"Lause on {n_words} sõna ja {n_commas} koma — kaalu "
                    f"jagamist lühemateks lauseteks, termineid muutmata."
                ),
                "suggestion": "jaga lause lühemateks osadeks",
            })

    return {
        "text": text,
        "issues": issues,
        "terms_of_art": terms,
        "summary_estonian": (
            f"Leiti {len(issues)} lihtsustamiskohta; säilitada tuleb "
            f"{len(terms)} juriidilist terminit." if (issues or terms)
            else "Kantseliiti ega juriidilisi termineid ei tuvastatud."
        ),
        "note": (
            "Heuristic legalese-simplification aid. `issues` flags archaic "
            "'kantseliit' filler (käesolev → see, juhul kui → kui) and "
            "over-long / over-nested sentences that can be split — WITHOUT "
            "changing legal meaning. `terms_of_art` lists specialised legal "
            "terms detected that MUST be preserved verbatim when simplifying: "
            "replacing e.g. 'vastutus' or 'hagi' with a general synonym would "
            "change the legal meaning. STARTER lexicons, precision-first and "
            "not exhaustive — absence of flags is not proof the text is plain, "
            "and terms_of_art will miss terms not yet in the list. Quote "
            "rule_estonian verbatim in Estonian replies."
        ),
    }


# Definition markers: '(edaspidi «Müüja»)', '(edaspidi ühiselt "Pooled")',
# '(edaspidi nimetatud Leping)'.
_EDASPIDI_QUOTED_RE = re.compile(
    r"edaspidi(?:\s+ühiselt)?(?:\s+nimetatud)?\s*[«„\"“]\s*([^»\"”“]+?)\s*[»\"”]",
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


def _count_word_occurrences(term: str, text: str) -> int:
    """Case-sensitive whole-token occurrence count, Estonian-letter aware
    (Python \\b is ASCII-only, so use explicit non-letter boundaries)."""
    pat = r"(?<![\wäöüõšžÄÖÜÕŠŽ])" + re.escape(term) + r"(?![\wäöüõšžÄÖÜÕŠŽ])"
    return len(re.findall(pat, text))


def _check_defined_terms(text: str) -> dict:
    """Structural aid for LONG legal documents: extract the terms defined
    with '(edaspidi «X»)', map their usage, extract section cross-references,
    and flag defined-but-unused or doubly-defined terms. Pure regex, so it
    scales to whole contracts / statutes (cap raised to MAX_DOC_CHARS)."""
    _check_text(text, limit=MAX_DOC_CHARS)

    defs: dict[str, dict] = {}
    for rx in (_EDASPIDI_QUOTED_RE, _EDASPIDI_BARE_RE):
        for m in rx.finditer(text):
            term = m.group(1).strip().strip("«»„“”\"' ")
            if not term:
                continue
            d = defs.setdefault(term, {"position": m.start(), "definitions": 0})
            d["definitions"] += 1

    issues: list[dict] = []
    defined_terms: list[dict] = []
    for term, d in sorted(defs.items(), key=lambda kv: kv[1]["position"]):
        uses = _count_word_occurrences(term, text)
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
        "text": text if len(text) <= 2000 else text[:2000] + "…",
        "defined_terms": defined_terms,
        "cross_references": xrefs,
        "issues": issues,
        "summary_estonian": (
            f"Leiti {len(defined_terms)} defineeritud mõistet, {len(xrefs)} "
            f"viidet ja {len(issues)} probleemi."
        ),
        "note": (
            "Structural check for long legal documents. defined_terms maps "
            "each '(edaspidi «X»)' definition to how many times the term "
            "actually occurs; cross_references lists § / lõige / punkt / "
            "artikkel references (surfaced, not validated). issues flags "
            "defined-but-unused and doubly-defined terms. Regex-based and "
            "PII-free (nothing stored); input cap is raised to 500,000 chars "
            "so whole contracts fit. `text` is truncated to a 2,000-char "
            "preview in the response. Quote rule_estonian verbatim in "
            "Estonian replies."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Simplify Estonian legalese (keep terms of art)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_legalese(text: Annotated[str, Field(description="Estonian legal text to lint for plain-language simplification while protecting legal terms of art.")]) -> _LegaleseResult:
    """Aid for simplifying Estonian legal text WITHOUT losing legal precision.

    Returns two things:
    - `issues`: archaic 'kantseliit' filler with plain equivalents
      (`käesolev` → `see`, `juhul kui` → `kui`), plus over-long / heavily
      subordinated sentences worth splitting.
    - `terms_of_art`: specialised legal terms detected in the text that
      MUST be kept verbatim when rewriting — swapping `hagi` or `vastutus`
      for a general synonym changes the legal meaning. Use this as a
      do-not-touch list while you simplify.

    Heuristic, precision-first, backed by curated starter lexicons (not
    exhaustive). Input capped at 100,000 characters.
    """
    return _check_legalese(text)


@mcp.tool(annotations=ToolAnnotations(
    title="Track defined terms & cross-references in a legal document",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_defined_terms(text: Annotated[str, Field(description="A long Estonian legal document to map defined terms ('edaspidi «X»') and § / lõige / punkt cross-references.")]) -> _DefinedTermsResult:
    """Structural map of a long Estonian legal document.

    Extracts every term defined with `(edaspidi «X»)`, counts how often each
    is actually used, lists `§` / `lõige` / `punkt` / `artikkel`
    cross-references, and flags defined-but-unused or doubly-defined terms —
    the consistency errors that creep into long contracts and statutes.

    Regex-based and PII-free (nothing is stored). Input cap is raised to
    500,000 characters so a whole contract fits in one call; the echoed
    `text` is truncated to a 2,000-character preview.
    """
    return _check_defined_terms(text)


def _common_legal_usage(word: str) -> dict:
    """Look up the canonical legal collocations for a word from the offline
    legal-corpus index — the 'what's the standard legal phrasing' answer.
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
    entry = lemmas.get(lemma) or lemmas.get(word.lower())

    if not entry:
        return {
            "word": word,
            "lemma": lemma or word.lower(),
            "found": False,
            "frequency": 0,
            "common_before": [],
            "common_after": [],
            "summary_estonian": (
                f"Sõna '{word}' ei esine juriidilise korpuse indeksis "
                f"(võib olla haruldane või mitte juriidiline termin)."
            ),
            "note": _LEGAL_USAGE_NOTE,
        }

    return {
        "word": word,
        "lemma": lemma or word.lower(),
        "found": True,
        "frequency": entry.get("freq", 0),
        "common_before": [{"word": w, "count": c} for w, c in entry.get("left", [])],
        "common_after": [{"word": w, "count": c} for w, c in entry.get("right", [])],
        "summary_estonian": (
            f"'{word}' esineb juriidilises korpuses {entry.get('freq', 0)} korda; "
            f"sagedasimad naabersõnad on toodud common_before / common_after all."
        ),
        "note": _LEGAL_USAGE_NOTE,
    }


_LEGAL_USAGE_NOTE = (
    "Canonical legal-usage lookup from an offline collocation index distilled "
    "from Estonian legal corpora (see scripts/build_legal_collocations.py). "
    "common_before / common_after are the content words most often seen "
    "immediately before / after the term's lemma in real legislation, with "
    "raw counts — use them to pick idiomatic legal phrasing (e.g. 'esitama "
    "hagi', 'kohustuse täitmine') instead of inventing collocations. It is a "
    "frequency signal, NOT prescriptive: rare-but-correct phrasings exist, and "
    "coverage is bounded by the corpus. The bundled index is built from "
    "public-domain Riigi Teataja legislation — five core codes (obligations, "
    "general civil, civil procedure, property, penal), ~2,000 legal terms; "
    "broaden coverage by adding more acts and supplying the index via "
    "ESTNLTK_MCP_LEGAL_INDEX. Deterministic and offline; no text is stored."
)


@mcp.tool(annotations=ToolAnnotations(
    title="Canonical legal usage / collocations for an Estonian term",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def common_legal_usage(word: Annotated[str, Field(description="A single Estonian (legal) word to look up canonical collocations for, e.g. 'hagi', 'kohustus', 'taotlus'.")]) -> _LegalUsageResult:
    """Canonical legal collocations for a term, from an offline corpus index.

    Answers "what's the standard legal phrasing" — returns how often the
    term occurs in Estonian legal text and the words most frequently seen
    directly before/after it (`hagi` → `esitama` before it = 'esitama hagi';
    `kohustus` → `täitmine` after it = 'kohustuse täitmine'). Use it so the
    AI picks real, idiomatic legalese instead of inventing collocations.

    A frequency signal, not prescriptive; coverage is bounded by the corpus.
    The bundled index is a proof-of-concept sample; the full-corpus artifact
    is loaded via ESTNLTK_MCP_LEGAL_INDEX. Input is a single word.
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

    # 1. Repetition — lemma-aware, skip function-word POS classes.
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
        # Skip very short lemmas (1-2 char) — usually function-y.
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

    # 2. Umbisikuline tegumood — see _impersonal_voice for the four
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
        f"Kõige sagedamini korduvad lemmad: {[r['lemma'] for r in repeated]}."
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
        "text": text,
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
        "note": (
            "Heuristic phase-1 style checker. Repetition threshold "
            "scales with text length. `passive_voice` counts Estonian "
            "umbisikuline tegumood: the -takse/-ti/-tud/-tav family, PLUS "
            "negated impersonals ('ei esitata', 'ei avaldatud') that raw "
            "Vabamorf form codes miss, MINUS `ei`/`ära` (tagged pos=V but "
            "not real verbs) and MINUS attributive -tud participles "
            "('lukustatud osa'), which are listed under "
            "`attributive_excluded` instead. ~15% is a healthy ceiling for "
            "marketing copy and <5% may read too forceful; for reports and "
            "academic prose anything above ~40% is the single clearest "
            "kantseliit signal — pair this with check_officialese. "
            "Hedging density >5% reads wishy-washy. Sentence length "
            "stddev should typically be at least 30% of the mean for "
            "natural rhythm. Quote *_estonian fields verbatim in "
            "Estonian replies."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian style (repetition, passive, hedging, rhythm)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_style(text: Annotated[str, Field(description="Estonian text to compute style metrics for (repetition, passive voice, sentence length, hedging).")]) -> _StyleResult:
    """Heuristic Estonian style metrics for newsletter / ad / email copy.

    Returns four metrics that flag common writing issues, each with an
    Estonian-language summary line for quoting verbatim:

    - repetition: lemma-aware (so 'kasutab' and 'kasutamine' both count
      under 'kasutama'). Threshold scales with text length so short
      replies don't fire on natural repeats.
    - passive_voice: ratio of Estonian -takse/-ti/-tud/-tav forms over
      total verbs. Newsletter copy usually wants <15%.
    - sentence_length: mean, stddev, min, max in content words. Low
      stddev = monotonous rhythm.
    - hedging: density of hedging words (võib-olla, vist, pigem, ehk,
      ilmselt, …). >5% reads wishy-washy.

    Phase-1 limitation: heuristic only. No detection of cliché phrases,
    weasel-words beyond the curated 15 lemmas, or genre-specific style
    drift. Input capped at 100,000 characters.
    """
    return _check_style(text)


# Officialese thresholds, calibrated on a real Estonian R&D report
# against the plain-language rewrite a native speaker produced from it.
# Measured across that edit:
#
#   impersonal ratio   0.875 → 0.308   (gate 0.4  — separates cleanly)
#   -mine per 100 wds  7.89  → 2.33    (gate 4.0  — separates cleanly)
#   longest sentence   30    → 21 wds  (gate 25   — separates cleanly)
#   noun/verb ratio    2.50  → 2.23    (gate 2.5  — barely separates)
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


def _check_officialese(text: str) -> dict:
    """Kantseliit diagnostic for NON-legal Estonian: reports, academic
    prose, R&D/grant paperwork, business writing.

    check_legalese exists but is scoped to statutes — on a real Estonian
    R&D report paragraph it returned zero issues, because its filler
    lexicon is legal-specific and its length gate (34 words) sits above
    where Estonian prose actually becomes unreadable. This tool measures
    what makes such text heavy:

    - nominalisation (-mine verbal nouns) and overall noun/verb density,
      the classic 'nimisõnastiil'
    - umbisikuline tegumood, correctly counted (see _impersonal_voice)
    - clause stacking per sentence — the 'mille käigus … ning …' pile-up
      a raw word count misses
    - long sentences, on an Estonian-calibrated gate
    - administrative filler with plain equivalents

    Precision-first, like its legal sibling: absence of flags is not proof
    the text is plain.
    """
    _check_text(text)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["sentences", "morph_analysis"])
    spans = list(t.morph_analysis)

    issues: list[dict] = []

    # --- 1. Nominalisation: -mine verbal nouns, with the verb to swap in.
    nominalisations: list[dict] = []
    for span in spans:
        pos, _form, lemma = _span_bits(span)
        if pos != "S" or not lemma.endswith("mine") or len(lemma) < 7:
            continue
        verb = lemma[:-4] + "ma"
        nominalisations.append({
            "word": span.text,
            "lemma": lemma,
            "position": span.start,
            "verb": verb,
        })

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
                f"muudab lause raskeks — kasuta tegusõna."
            ),
            "suggestion": ", ".join(
                f"{n['lemma']} → {n['verb']}" for n in nominalisations[:5]
            ),
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
                f"kõige selgem kantseliidi tunnus — nimeta tegija."
            ),
            "suggestion": "kirjuta isikulises tegumoes, nt 'koguti' → 'kogusime'",
        })

    # --- 2. Per-sentence: clause stacking and length.
    for sent in t.sentences:
        s_spans = [s for s in spans if s.start >= sent.start and s.end <= sent.end]
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
                    f"Lauses on {n_words} sisusõna (piir "
                    f"{_OFFICIALESE_LONG_SENTENCE_WORDS}). Eesti keeles "
                    f"mahub käändevormide tõttu ühte lausesse rohkem infot "
                    f"kui inglise keeles, nii et pikk lause muutub kiiresti "
                    f"raskeks."
                ),
                "suggestion": "jaga lause lühemateks lauseteks",
            })

    # --- 3. Administrative filler (lemma-exact + fixed phrase pairs).
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
                    "phrase": f"{span.text} {spans[i + 1].text}",
                    "position": span.start,
                    "rule": "officialese-phrase",
                    "rule_estonian": "kantseliitlik väljend",
                    "explanation": why,
                    "suggestion": plain,
                })

        # 'X-i poolt tehtud' — the passive-agent calque. Only when a
        # genitive noun precedes, so 'hääletas poolt' (in favour) is safe.
        if low == "poolt" and i > 0:
            prev_pos, prev_form, _prev_lemma = _span_bits(spans[i - 1])
            if prev_pos == "S" and prev_form in ("sg g", "pl g"):
                issues.append({
                    "phrase": f"{spans[i - 1].text} poolt",
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
        "text": text,
        "issues": issues,
        "metrics": {
            "word_count": word_count,
            "noun_count": noun_count,
            "verb_count": verb_count,
            "noun_verb_ratio": round(noun_verb_ratio, 2),
            "nominalisations": nominalisations,
            "nominalisations_per_100_words": round(nom_per_100, 2),
            "impersonal_voice": imp,
        },
        "summary_estonian": (
            f"Leiti {len(issues)} kantseliidikohta. Nimisõnade ja tegusõnade "
            f"suhe on {noun_verb_ratio:.2f}, umbisikulises tegumoes "
            f"{round(imp['ratio'] * 100, 1)}% tegusõnadest."
            if issues else
            "Selget kantseliiti ei tuvastatud."
        ),
        "note": (
            "Heuristic kantseliit diagnostic for NON-legal Estonian "
            "(reports, academic prose, business writing) — the sibling of "
            "check_legalese, which is scoped to statutes and stays silent "
            "on report officialese. Thresholds are calibrated against a "
            "native speaker's plain-language rewrite of a real Estonian R&D "
            "report. Across that edit the impersonal ratio went 0.875 → "
            "0.308, -mine nouns per 100 words 7.89 → 2.33 and the longest "
            "sentence 30 → 21 content words, so those three gates (0.4, "
            "4.0, 25) separate bureaucratic from plain cleanly. Noun/verb "
            "moved only 2.50 → 2.23, so its gate (2.5) is deliberately "
            "loose and that signal is the weakest of the four — weigh it "
            "last. `metrics.nominalisations` gives each "
            "-mine noun with the verb to swap in. STARTER lexicons, "
            "precision-first and not exhaustive — absence of flags is not "
            "proof the text is plain. For legal text use check_legalese "
            "instead: it protects terms of art, which this tool does not. "
            "Quote rule_estonian and summary_estonian verbatim in Estonian "
            "replies."
        ),
    }


class _OfficialeseResult(TypedDict, total=False):
    """Output of check_officialese: kantseliit issues + density metrics."""
    text: str
    issues: list[dict]
    metrics: dict
    summary_estonian: str
    note: str


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian officialese (kantseliit in non-legal prose)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_officialese(text: Annotated[str, Field(description="Estonian non-legal text (report, academic, business) to check for kantseliit / bureaucratic density.")]) -> _OfficialeseResult:
    """Flag Estonian kantseliit in reports, academic and business prose.

    check_legalese is the legal-text sibling; use THIS one for anything
    that is not a statute or contract, where check_legalese finds nothing
    because its lexicon and length gate are tuned for legislation.

    Returns `issues` (each with an Estonian `rule_estonian` label, an
    explanation and a concrete suggestion) plus `metrics`:

    - nominalisation: -mine verbal nouns per 100 words, each paired with
      the verb to use instead (`hindamine` → `hindama`)
    - noun_verb_ratio: nimisõnastiil density; over ~2.0 reads heavy
    - impersonal_voice: umbisikuline tegumood, correctly counted
      (negated impersonals included, `ei`/`ära` and attributive -tud
      participles excluded)
    - clause-stacking: 3+ subordinate-clause openers in one sentence, the
      'mille käigus … ning …' pile-up that a word count alone misses
    - long-sentence: 25+ content words, calibrated for Estonian
    - officialese filler: `omama` → `olema`, `kujutab endast` → `on`,
      `viidi läbi` → `tehti`, `X-i poolt tehtud` → `X-i tehtud`

    Heuristic and precision-first — no flags does not prove the text is
    plain. Input capped at 100,000 characters.
    """
    return _check_officialese(text)


# How many distinct content nouns get a WordNet lookup in
# check_term_consistency. Bounds cost on long documents; the nouns are
# ranked by frequency first, so the cap drops only long-tail hapaxes.
_TERM_CONSISTENCY_WORDNET_CAP = 40


def _check_term_consistency(text: str) -> dict:
    """One referent, one term — flag a document that names the same thing
    several ways.

    This is the failure mode a human editor catches instantly and a model
    sliding through a long document does not: a dataset called `andmestik`
    in one paragraph, `teadusandmestik` in the next and `korpus` in a
    third. Two rules, both precision-first:

    A. Shared compound head. The text uses a bare noun X *and* a compound
       ending in X (`andmestik` + `teadusandmestik`), or three or more
       distinct lemmas share one head. Two compounds sharing a head is NOT
       enough on its own — `tegevusvaldkond` and `märgendusvaldkond` are
       usually genuinely different things.
    B. Shared WordNet synset. Two distinct lemmas in the text sit in the
       same synset, i.e. Estonian WordNet considers them synonyms
       (`andmestik` / `andmebaas`).

    Reports counts per variant so the caller can pick the dominant term,
    and never decides which variant is right — that needs context the tool
    does not have.

    Capped at MAX_TEXT_CHARS, NOT MAX_DOC_CHARS. check_defined_terms may
    take 500k because it is pure regex; this one runs full morphological
    analysis over the whole document, which is ~4s per 100k chars. At 500k
    a single call would burn ~22s of CPU, which would blow the
    defence-in-depth budget the public per-IP rate limit is sized against.
    """
    _check_text(text, limit=MAX_TEXT_CHARS)
    Text = _Text()
    t = Text(text)
    t.tag_layer(["morph_analysis"])

    from collections import Counter, defaultdict

    counts: Counter = Counter()
    first_pos: dict[str, int] = {}
    heads: dict[str, str] = {}

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
    # would attempt a download — breaching the no-outbound-HTTP promise in
    # PRIVACY.md — and print its prompt to stdout, which under stdio
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
                "head": None,
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

    groups.sort(key=lambda g: g["variants"][0]["position"])

    return {
        "text": text,
        "groups": groups,
        "terms_analysed": len(counts),
        "rules_run": {
            "shared-compound-head": True,
            "shared-wordnet-synset": wordnet_ok,
        },
        # Top-level so a caller cannot miss it. A partial run that reports
        # "nothing found" reads as a clean bill of health, which is exactly
        # how a half-strength checker misleads someone — so say it here AND
        # in summary_estonian, not only in rules_run.
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
        "note": (
            "Heuristic terminology-consistency check: 'one referent, one "
            "term'. Rule `shared-compound-head` fires when a bare noun and "
            "a compound built on it both appear (andmestik + "
            "teadusandmestik), or when 3+ lemmas share one head; two "
            "compounds sharing a head is deliberately NOT enough, since "
            "those are usually distinct things. Rule "
            "`shared-wordnet-synset` fires when two lemmas sit in the same "
            "Estonian WordNet synset — and does not run at all when that "
            "resource is missing, in which case `degraded` is true and an "
            "empty `groups` list means only that the compound-head rule "
            "found nothing. The tool does NOT decide which "
            "variant is correct — that needs domain context it cannot see; "
            "it reports counts so you can pick the dominant term, and some "
            "groups are legitimately distinct concepts. KNOWN GAP: "
            "synonym pairs that share neither a head nor a synset "
            "(korpus / andmestik) are not caught — for those, read each "
            "candidate's `synonyms` definition and check whether its "
            "domain fits the text. WordNet lookups are capped at the "
            f"{_TERM_CONSISTENCY_WORDNET_CAP} most frequent nouns, and "
            "`rules_run` reports whether the WordNet rule actually ran — "
            "if the resource is unavailable it degrades to the "
            "compound-head rule alone rather than failing. Input "
            "capped at 100,000 characters."
        ),
    }


class _TermConsistencyResult(TypedDict, total=False):
    """Output of check_term_consistency: groups of competing terms."""
    text: str
    groups: list[dict]
    terms_analysed: int
    rules_run: dict
    degraded: bool
    summary_estonian: str
    note: str


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian terminology consistency (one referent, one term)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_term_consistency(text: Annotated[str, Field(description="Estonian document to check for the same thing being named several different ways.")]) -> _TermConsistencyResult:
    """Flag a document that calls the same thing several different names.

    The classic long-document defect, and the one a model editing
    paragraph-by-paragraph reliably misses: a dataset that is `andmestik`
    on page 1, `teadusandmestik` on page 2 and `korpus` on page 3.

    Two precision-first rules:

    - `shared-compound-head`: a bare noun and a compound built on it both
      occur (`andmestik` + `pildiandmestik`), or 3+ lemmas share one head.
    - `shared-wordnet-synset`: two lemmas sit in one Estonian WordNet
      synset, i.e. WordNet calls them synonyms.

    Each group lists its variants with occurrence counts and the dominant
    one, so you can standardise on the most-used term. The tool does not
    decide which variant is right — some groups are genuinely distinct
    concepts, so read them before rewriting.

    CHECK `degraded` BEFORE TRUSTING AN EMPTY RESULT. When Estonian WordNet
    is not installed, the `shared-wordnet-synset` rule cannot run; the tool
    then returns `degraded: true`, says so in `summary_estonian`, and marks
    the rule false in `rules_run`. "No groups found" from a degraded run
    means "the compound-head rule found nothing", NOT "the terminology is
    consistent".

    Known gap: synonyms sharing neither a head nor a synset (korpus /
    andmestik) are not caught. Input capped at 100,000 characters.
    """
    return _check_term_consistency(text)


@mcp.tool(annotations=ToolAnnotations(
    title="Classify Estonian register (formal vs colloquial)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def classify_register(text: Annotated[str, Field(description="Estonian text to classify by register (formal vs colloquial).")]) -> _RegisterResult:
    """Heuristic register classifier for Estonian (formal vs colloquial).

    Returns a tier label (English in `tier`, correct Estonian in
    `tier_estonian` — quote that field verbatim when composing an
    Estonian-language reply rather than translating `tier` yourself, to
    avoid mistranslations like "formalne" instead of the correct
    "formaalne"), a normalised score in [-1, 1] (positive = formal,
    negative = colloquial), and the matched formal/colloquial markers
    found in the text. Useful for sanity-checking that marketing copy
    hasn't drifted into officialese, or that a contract draft hasn't
    slipped into chat tone.

    The lexicon covers legal-administrative AND academic/report
    vocabulary. `structure` adds two syntactic signals — umbisikuline
    tegumood ratio and noun/verb density — bounded at +0.4, applied only
    from 25 words up and only when the lexicon is not net-colloquial.

    LIMITATION: still a heuristic, not a trained model. Address forms and
    finer syntax go uncaught, and most newsletter prose scores 'neutral'.
    Use the result as a directional hint, not a verdict; for a full
    kantseliit breakdown with per-issue suggestions, call
    check_officialese. Input capped at 100,000 characters.
    """
    return _classify_register(text)


# ---------------------------------------------------------------------------
# HTTP transport: bearer auth + rate limit
# ---------------------------------------------------------------------------

# Aggregate request counters surfaced at /metrics. Optionally persisted
# to a Fly volume so machine restarts don't reset the cumulative total.
# Only counts — never request bodies or tokens — so the "no request
# logging" property in SECURITY.md stays intact.
_STATS_START_TS: float = time.time()
_STATS: dict[str, Any] = {
    "total": 0,
    "by_status": {},
    "by_path": {},
    # Count of MCP `initialize` calls — a privacy-safe proxy for client
    # connections / session-starts. NOT a user count: a client that
    # reconnects counts again, and automated probes count too. No identity,
    # no IP, no body is stored — only the fact that an initialize occurred.
    "sessions": 0,
    # JSON-RPC method mix on POST /mcp, bucketed to a FIXED allowlist (see
    # _MCP_METHODS). Stateless HTTP means an `initialize` cannot be tied to
    # the tool calls that follow it, so "sessions that made >=1 tool call"
    # is not computable without inventing a client identifier — which would
    # be a privacy step backwards. This breakdown answers the same question
    # from the other side: `initialize` vs `notifications/initialized` shows
    # how many handshakes were actually completed rather than abandoned by a
    # probe, and `tools/list` vs `tools/call` shows how many clients
    # enumerate the tools but never use one.
    "mcp_methods": {},
}

# Ring buffer of recent 5xx errors so they're inspectable at /metrics
# without depending on Fly's short-lived log tail. PII-free: only
# timestamp, path, status, and (when known) the exception type — never
# request bodies. Persisted with the counters so they survive restarts.
_recent_errors: collections.deque = collections.deque(maxlen=20)

# Persistence: if ESTNLTK_MCP_METRICS_PATH is set (default
# /data/metrics.json — matches the Fly volume mount), counters survive
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
            "tool_calls": _TOOL_CALLS,
            "recent_errors": list(_recent_errors),
            "saved_at_unix": int(time.time()),
        }))
        tmp.replace(_METRICS_PATH)
    except Exception as e:
        log.warning("metrics persistence: failed to save %s: %s", _METRICS_PATH, e)


# Fixed allowlist of JSON-RPC methods we bucket into `mcp_methods`.
# The method name comes from a request body, i.e. it is caller-controlled,
# so it is NEVER stored verbatim — anything outside this set is counted as
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
    is read — never params, arguments, or clientInfo — and nothing from the
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
    if isinstance(msg, list):  # JSON-RPC batch — classify by its first method
        msg = next((m for m in msg if isinstance(m, dict) and m.get("method")), None)
    if not isinstance(msg, dict):
        return None
    method = msg.get("method")
    if not isinstance(method, str):
        return None
    return method if method in _MCP_METHODS else "other"


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
# it via logger.exception(...) and returns its OWN HTTP 500 — the exception
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
    Type name only — PII-free, no message, no stack."""

    def emit(self, record: logging.LogRecord) -> None:
        exc_info = record.exc_info
        if exc_info and exc_info[0] is not None:
            _last_inner_exc["type"] = exc_info[0].__name__
            _last_inner_exc["ts"] = time.time()


def _install_inner_exc_capture() -> None:
    """Attach the capture handler to the `mcp` logger once (idempotent).
    Hooks the parent `mcp` logger so it keeps working if the SDK renames
    the streamable_http submodule. Additive — does not suppress the SDK's
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


async def _serve_static_icon(send, scope, body: bytes, content_type: bytes, etag: str) -> None:
    """Serve a static icon with a 1-year immutable cache + ETag. If the
    client already holds this version (If-None-Match), answer 304 with no
    body. Cheap defence against clients that re-fetch the favicon in a loop
    (e.g. connector-directory icon rendering) — anything honouring either
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
    LEFTMOST XFF entry — which is fully caller-controlled, because a proxy
    appends to whatever the client sent. Bucketing on that let any caller
    mint a fresh rate-limit bucket per request simply by varying the
    header, defeating the limiter entirely. Reproduced against a local
    server in public mode at a 5/min limit: a fixed spoofed value gets 429
    after five requests, while rotating the value stays 200 indefinitely.

    The Nth-from-right entry is the one written by the Nth proxy in front
    of us, and a caller cannot append after a proxy — so it is correct
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


def _build_http_app(token: str | None, rate_limit: int, public_mode: bool = False, inner=None):
    """Wrap an ASGI MCP app with auth (or none) + rate limit + /health bypass.

    public_mode=False (default): require bearer token, rate-limit per token.
    public_mode=True:           no auth, rate-limit per client IP.

    `inner` defaults to FastMCP's streamable-http app; tests inject a stub.
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
            # Public health endpoint — no auth, no rate limit. Used by Fly
            # probes, uptime monitoring, and quick "is the latest deploy
            # live?" eyeballing (version + tool count surfaced here).
            if path == "/health":
                await _send_status(send, 200, {
                    "ok": True,
                    "version": SERVER_VERSION,
                    "tools": _count_registered_tools(),
                })
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

            # Public metrics — aggregate request counters since process
            # start. Resets on Fly machine restart (idle auto-stop,
            # redeploy, crash). No body inspection, no token logging —
            # only counts.
            if path == "/metrics":
                payload = {
                    "total_requests": _STATS["total"],
                    "by_status": dict(_STATS["by_status"]),
                    "by_path": dict(_STATS["by_path"]),
                    "tool_calls_total": sum(_TOOL_CALLS.values()),
                    "tool_calls": dict(_TOOL_CALLS),
                    "sessions_total": _STATS["sessions"],
                    "mcp_methods": dict(_STATS["mcp_methods"]),
                    "recent_errors": list(_recent_errors),
                    "uptime_seconds": int(time.time() - _STATS_START_TS),
                    "started_at_unix": int(_STATS_START_TS),
                    "note": (
                        "tool_calls counts ONLY real tool executions (not "
                        "initialize / tools-list / SSE opens, which inflate "
                        "the /mcp path bucket) — use tool_calls_total as the "
                        "true usage number. sessions_total counts MCP "
                        "initialize calls — a privacy-safe proxy for client "
                        "connections, NOT a user count: a client that "
                        "reconnects counts again and automated probes count "
                        "too. No identity, IP, or request body is ever stored "
                        "— only the fact of an initialize. Daily connections "
                        "= the day-over-day delta in the metrics snapshot. "
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
                        "is the process start, NOT when tracking began — the "
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

            # Landing page at / — public, no auth. Tells humans what they
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

            # Favicons — public, no auth. Google's s2/favicons service
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
                await _send_status(send, 429, {"error": "rate_limited"})
                return

            # Bucket MCP traffic by JSON-RPC method. Only an authorized,
            # non-rate-limited POST /mcp gets here; for those we buffer the
            # JSON-RPC body to peek at the `method`, then replay it to the
            # inner app byte-for-byte. Nothing from the body is stored — we
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
            # Log a minimal, PII-free breadcrumb — exception type + path
            # only, never the request body or token — so a recurrence is
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
        # LEFTMOST X-Forwarded-For entry, which is fully caller-controlled —
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
