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
# Public mode (no auth, anyone can call): per-IP, tighter default.
DEFAULT_RATE_LIMIT_PER_MINUTE = 60
DEFAULT_PUBLIC_RATE_LIMIT_PER_MINUTE = 120

# Bumped manually in lockstep with pyproject.toml's [project].version.
SERVER_VERSION = "0.1.0"

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


@mcp.tool(annotations=ToolAnnotations(
    title="Estonian morphological analysis",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
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

    return {
        "tier": tier,
        "tier_estonian": _TIER_ET[tier],
        "score": round(score, 3),
        "formal_markers": sorted(set(formal_hits)),
        "colloquial_markers": sorted(set(colloquial_hits)),
        "word_count": word_count,
        "note": (
            "Heuristic phase-1 classifier — lexicon-based, lemma-aware. "
            "Catches obvious officialese vs slang; most newsletter prose "
            "scores 'neutral'. Treat as a directional hint, not a verdict. "
            "When composing an Estonian-language reply, USE THE "
            "tier_estonian FIELD VERBATIM rather than translating `tier` "
            "yourself — common mistranslations include 'formalne' (wrong) "
            "vs 'formaalne' (correct)."
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


@mcp.tool(annotations=ToolAnnotations(
    title="Classify Estonian register (formal vs colloquial)",
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
))
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

        # Landing page at / — public, no auth. Tells humans what they hit
        # and gives Google's favicon scraper the <link rel="icon"> tags
        # it needs to find our PNG.
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

        # Favicons — public, no auth. Google's s2/favicons service rejects
        # SVG, so /favicon.ico and /favicon.png must serve PNG bytes for
        # the icon to appear in Anthropic's Directory + Claude tool-call
        # UI. /favicon.svg keeps SVG for modern browsers.
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
