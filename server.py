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
from pathlib import Path
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
# Public mode (no auth, anyone can call): per-IP. Bumped from earlier
# 60/120 defaults after real-world usage showed legitimate active
# sessions (parallel tool calls + multiple users behind shared NATs)
# brushing the ceiling. The defence-in-depth math still holds: at
# 300/min/IP, a sustained attacker burns ~30s CPU/min/IP on Fly's
# shared-cpu-1x, ~5% capacity. Cloudflare in front is the right answer
# for actual DDoS, not tighter per-IP limits.
DEFAULT_RATE_LIMIT_PER_MINUTE = 120
DEFAULT_PUBLIC_RATE_LIMIT_PER_MINUTE = 300

# Bumped manually in lockstep with pyproject.toml's [project].version.
SERVER_VERSION = "0.2.0"

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

mcp = FastMCP("estonian-mcp")
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


@lru_cache(maxsize=1)
def _wordnet():
    from estnltk.wordnet import Wordnet
    return Wordnet()


@lru_cache(maxsize=1)
def _embeddings():
    """Lazy-load the compressed fastText model used by find_related_words
    and check_compound_familiarity."""
    import compress_fasttext
    path = os.environ.get(
        "ESTNLTK_MCP_FASTTEXT_PATH",
        "/opt/models/fasttext-et-medium",
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
        return fn(*args, **kwargs)

    return wrapper


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


def _is_indeclinable_attr(word: str) -> bool:
    """True if a word does NOT inflect when used attributively (before a
    noun), so adjective-noun agreement should leave it in base form.

    Two cases, both verified against EKI's inflection_et benchmark:
    - lexical indeclinables (täis, eri, väärt, ...)
    - past participles in -tud / -dud / -nud, which are invariant in
      attributive position (`tuntud laulja` → `tuntud laulja` in the
      genitive, not *tuntu laulja). Detected by ending because Vabamorf
      often misanalyses them (e.g. hajutatud → noun 'hajutatu').

    NOT flagged: -v present participles (rahuldav → rahuldava), which do
    agree normally.
    """
    w = word.lower()
    if w in _INDECLINABLE_ADJ_ET:
        return True
    return w.endswith(("tud", "dud", "nud"))


@mcp.tool(annotations=ToolAnnotations(
    title="Estonian morphological analysis",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def analyze_morphology(text: str, all_analyses: bool = False) -> list[dict]:
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
        attributively (lexical indeclinables like `täis`, and -tud/-nud
        past participles like `tuntud`) — i.e. they do NOT take the
        noun's case ending in agreement. Use this before inflecting a
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
        indeclinable = _is_indeclinable_attr(word)
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
def paradigm(word: str) -> dict:
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
@_counted
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
@_counted
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
@_counted
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
@_counted
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
@_counted
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
@_counted
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
        "word_count": word_count,
        "note": (
            "Heuristic phase-1 classifier — lexicon-based, lemma-aware. "
            "Catches obvious officialese vs slang; most newsletter prose "
            "scores 'neutral'. The `consistency` field flags texts that "
            "carry BOTH formal AND colloquial markers — useful for "
            "catching jarring register-mixing even when the overall "
            "tier rounds to 'neutral'. Treat as a directional hint, not "
            "a verdict. When composing an Estonian-language reply, "
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

        lemma_lower = (list(span.lemma)[0] if span.lemma else "").lower()
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
                    list(spans[i + 1].lemma)[0] if spans[i + 1].lemma else ""
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
def check_compounds(text: str) -> dict:
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
def check_punctuation(text: str) -> dict:
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
    for i, s in enumerate(syls[:-1]):
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
def check_hyphenation(word: str) -> dict:
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
def check_numbers(text: str) -> dict:
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
def check_capitalization(text: str) -> dict:
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


def _check_compound_familiarity(text: str) -> dict:
    """Surface fastText neighborhood diagnostic for compound nouns.

    For each compound noun (Vabamorf root_tokens of length >= 2) in the
    text, look up its fastText nearest neighbours. Legitimate Estonian
    compounds tend to have semantically coherent neighbours with a
    decent top similarity score (typically > 0.55). Calques / coined
    compounds often have weak top similarity and/or subword-only
    neighbours that just share letters with the input's parts.

    Output is *diagnostic*, not authoritative — the underlying
    fastText-et-mini model has a 20K-word pruned vocabulary, so some
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

        # Two-tier signal with the medium (100K vocab) model:
        #   - in-vocab → real Estonian compound, never suspect
        #   - out-of-vocab AND weak top similarity → likely calque
        # In-vocab covers most legitimate compounds; the score gate
        # catches the calque case (mõtteliin — literal English
        # "train of thought" — score 0.536, OOV).
        is_suspect = (not in_vocab) and top_score < 0.55

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
            "is_suspect": is_suspect,
        })

    suspects = [c for c in compounds if c["is_suspect"]]

    return {
        "text": text,
        "compounds_analysed": len(compounds),
        "suspect_compounds": suspects,
        "all_compounds": compounds,
        "summary_estonian": (
            f"Tuvastati {len(compounds)} liitsõnanimisõna; "
            f"{len(suspects)} neist on madala fastText-skooriga "
            f"(tasub üle vaadata, kas neid eesti keeles "
            f"tegelikult kasutatakse)." if compounds
            else "Liitsõnanimisõnu analüüsiks ei leitud."
        ),
        "note": (
            "Heuristic compound-familiarity check via fastText nearest "
            "neighbours, using a 100K-vocab compressed model. Two-tier "
            "signal: in-vocab compounds are treated as real Estonian and "
            "never flagged; out-of-vocab compounds whose top similarity "
            "is below 0.55 are flagged as suspect (likely calque or "
            "coined term). NOT authoritative — even at 100K vocab a few "
            "legitimate but rare compounds will be OOV with weak signal. "
            "The neighbours list is included so callers can judge close "
            "calls: if top neighbours mostly share letters with the "
            "input's parts (subword-similar), suspect a calque; if they "
            "are semantically coherent (synonyms or related concepts), "
            "the compound is probably real. Designed for the case of "
            "Claude inventing literal English-to-Estonian compounds like "
            "'mõtteliin' (English: 'train of thought'; real Estonian: "
            "'mõttekäik') — the lemma is morphologically valid but not "
            "in real Estonian usage."
        ),
    }


@mcp.tool(annotations=ToolAnnotations(
    title="Check Estonian compound familiarity (calque-risk diagnostic)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def check_compound_familiarity(text: str) -> dict:
    """fastText-based diagnostic for compound-noun familiarity in Estonian.

    For each compound noun (root_tokens length >= 2), returns its top
    fastText neighbours and a `top_score` similarity, flagging
    compounds that are out-of-vocab AND have top similarity below 0.55
    as `is_suspect: true` — the failure mode for AI-invented calques
    like `mõtteliin` (literal English "train of thought"; real Estonian
    is `mõttekäik`).

    Output is diagnostic, not authoritative. Even with the 100K-vocab
    medium model, some legitimate but rare compounds (e.g.
    `tervisekindlustus`) can still be OOV with weak signal. Treat
    suspect flags as "worth a second look" and judge by the included
    neighbours list: if neighbours are semantically coherent the
    compound is fine; if they're subword-similar variations (mostly
    sharing letters with
    the input's parts) the compound is likely translationese.

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
def check_abbreviation_hyphenation(text: str) -> dict:
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
        partitive_verb: str | None = None
        trigger_index = -1
        for idx, span in enumerate(sent_spans):
            lemma = (_first(list(span.lemma)) or "").lower()
            if lemma in _NEGATION_LEMMAS_ET:
                has_negation = True
                if trigger_index == -1:
                    trigger_index = idx
            if lemma in _PARTITIVE_ONLY_VERBS_ET:
                partitive_verb = lemma
                if trigger_index == -1:
                    trigger_index = idx

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
def check_object_case(text: str) -> dict:
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
def check_redundancy(text: str) -> dict:
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

    # 2. Passive voice — count verbs whose form is in the passive set.
    passive_count = 0
    passive_examples: list[str] = []
    verb_count = 0
    for span in spans:
        pos = _first(list(span.partofspeech))
        if pos != "V":
            continue
        verb_count += 1
        form = _first(list(span.form))
        if form and form in _PASSIVE_FORMS_ET:
            passive_count += 1
            if len(passive_examples) < 5 and span.text not in passive_examples:
                passive_examples.append(span.text)
    passive_ratio = (passive_count / verb_count) if verb_count else 0.0

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
            "scales with text length. Passive-voice ratio uses Vabamorf "
            "form codes (-takse/-ti/-tud/-tav family); ~15% is a healthy "
            "ceiling for marketing copy, <5% may read too forceful. "
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
def check_style(text: str) -> dict:
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


@mcp.tool(annotations=ToolAnnotations(
    title="Classify Estonian register (formal vs colloquial)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
@_counted
def classify_register(text: str) -> dict:
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

# Aggregate request counters surfaced at /metrics. Optionally persisted
# to a Fly volume so machine restarts don't reset the cumulative total.
# Only counts — never request bodies or tokens — so the "no request
# logging" property in SECURITY.md stays intact.
_STATS_START_TS: float = time.time()
_STATS: dict[str, Any] = {
    "total": 0,
    "by_status": {},
    "by_path": {},
}

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
        _TOOL_CALLS.clear()
        _TOOL_CALLS.update({str(k): int(v) for k, v in (data.get("tool_calls") or {}).items()})
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
            "tool_calls": _TOOL_CALLS,
            "saved_at_unix": int(time.time()),
        }))
        tmp.replace(_METRICS_PATH)
    except Exception as e:
        log.warning("metrics persistence: failed to save %s: %s", _METRICS_PATH, e)


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

    async def app(scope, receive, send_raw):
        if scope["type"] == "lifespan":
            return await inner(scope, receive, send_raw)
        if scope["type"] != "http":
            await _send_status(send_raw, 400, {"error": "unsupported scope"})
            return

        path = scope.get("path", "")
        # Wrap send so we can capture the final response status for
        # /metrics without changing the inner app's contract.
        captured = {"status": 0}

        async def send(message):
            if message["type"] == "http.response.start":
                captured["status"] = message.get("status", 0)
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
                    "uptime_seconds": int(time.time() - _STATS_START_TS),
                    "started_at_unix": int(_STATS_START_TS),
                    "note": (
                        "tool_calls counts ONLY real tool executions (not "
                        "initialize / tools-list / SSE opens, which inflate "
                        "the /mcp path bucket) — use tool_calls_total as the "
                        "true usage number. Counters persist to "
                        "/data/metrics.json every 30 s when a Fly volume is "
                        "mounted, surviving restarts; without a volume "
                        "(local dev) they reset. Counts are per-Fly-machine, "
                        "so with >1 machine each tracks its own and /metrics "
                        "reflects whichever served the request. started_at_unix "
                        "is the process start, NOT when tracking began — the "
                        "counts span all persisted history. Records tool NAME "
                        "+ count only, never arguments; privacy posture in "
                        "SECURITY.md is unchanged."
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
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"image/png"),
                        (b"content-length", str(len(FAVICON_PNG)).encode("ascii")),
                        (b"cache-control", b"public, max-age=86400"),
                    ],
                })
                await send({"type": "http.response.body", "body": FAVICON_PNG})
                return
            if path == "/favicon.svg" or (
                path == "/favicon.ico" and FAVICON_PNG is None
            ):
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
            if captured["status"] == 0:
                await _send_status(send, 500, {"error": "internal_error"})
            else:
                # Response already in flight (e.g. mid-SSE-stream); we
                # can't cleanly send a 500, so let the server framework
                # close the half-sent connection.
                raise
        finally:
            _stats_record(captured["status"] or 0, path)

    return app


def _run_http(host: str, port: int, token: str | None, rate_limit: int, public_mode: bool) -> None:
    import uvicorn  # local import; only needed in HTTP mode
    import atexit

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
