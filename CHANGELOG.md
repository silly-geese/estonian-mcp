# Changelog

All notable user-facing changes to this MCP server.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/);
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.4.1] — 2026-07-06

### Changed

- **The bundled `common_legal_usage` index is now real, license-clean data.**
  Replaced the tiny authored proof-of-concept sample with an index built from
  **public-domain Riigi Teataja legislation** — the five core codes
  (Võlaõigusseadus, Tsiviilseadustiku üldosa seadus, Tsiviilkohtumenetluse
  seadustik, Asjaõigusseadus, Karistusseadustik) — **~2,000 legal terms**
  across obligations, general civil, civil procedure, property, and penal law,
  with true corpus frequencies: `hagi` → `esitama hagi` / `hagi tagamine`,
  `kohustus` → `kohustuse täitmine`, `kuritegu` → `kuriteo toimepanemine`,
  `omand` → `omandi üleandmine`. ~100 KB, offline, PII-free.
- **New `scripts/fetch_riigiteataja.py`** — fetches consolidated act text from
  Riigi Teataja's public `/api/v1/akt/{id}/blob-html` endpoint into `.txt`
  files for `build_legal_collocations.py --source dir`. Coverage broadens by
  adding act ids — no code change.

## [0.4.0] — 2026-07-06

### Added

- **`common_legal_usage` (tool count 23 → 24)** — canonical legal-usage
  collocations from an offline corpus index. Given a legal term it returns
  how often it occurs in legislation and the content words most often seen
  directly before / after it (`hagi` → `esitama hagi`, `kohustus` →
  `kohustuse täitmine`), so the model uses real legalese instead of inventing
  collocations. Deterministic and offline.
- **`scripts/build_legal_collocations.py`** — the index build pipeline. It
  streams a corpus sentence-by-sentence, distills collocation/frequency
  statistics with Vabamorf, and discards the text — the corpus is never
  stored, only the pruned index. Source-agnostic (`--source sample|dir|hf`).

### Notes

- The **bundled index is a proof-of-concept** built from a small,
  license-clean authored sample, so `common_legal_usage` currently covers
  only a few dozen core terms. The production full index should be built
  from **public-domain Riigi Teataja** legislation (`--source dir`) and
  supplied via `ESTNLTK_MCP_LEGAL_INDEX`. The `paulpall/legalese-sentences_estonian`
  HuggingFace corpus is **non-commercial** (Estonian National Corpus), so it
  is a `--source hf` research option only and is NOT shipped.

## [0.3.0] — 2026-07-06

### Added

- **Two legal-Estonian tools (tool count 21 → 23)** — for working with
  Estonian legal texts, offline and PII-free so confidential documents
  never leave the machine:
  - **`check_legalese`** — plain-language simplification aid. Flags archaic
    'kantseliit' filler (`käesolev` → `see`, `juhul kui` → `kui`) and
    over-long / over-nested sentences to split, while listing the legal
    **terms of art** in the text that must be preserved verbatim (a general
    synonym would change the legal meaning).
  - **`check_defined_terms`** — structural map for long documents: extracts
    `(edaspidi «X»)` definitions and their usage, `§` / `lõige` / `punkt`
    cross-references, and flags defined-but-unused or doubly-defined terms.
    Input cap raised to 500,000 chars so whole contracts fit.

### Changed

- **`check_compound_familiarity` no longer false-flags legal compounds.** A
  curated legal terms-of-art list suppresses the ~15% of legal compounds
  (`õigussuhe`, `solidaarvõlgnik`, `abieluvaraleping`) that the general-web
  fastText vocabulary mistook for coinages.

## [0.2.4] — 2026-07-04

### Security

- Dependency refresh resolving 15 Dependabot advisories, no API or
  behaviour change: **starlette** 1.0.1 → 1.3.1 (2 high), **pyjwt**
  2.12.1 → 2.13.0 (1 high), **python-multipart** 0.0.27 → 0.0.31 (1 high),
  **cryptography** 48.0.0 → 48.0.1 (1 high), **pydantic-settings** 2.14.0 →
  2.14.2 (1 medium). One advisory is knowingly left open: **nltk**
  (GHSA-p4gq-832x-fm9v) has no upstream patch, and its vulnerable
  `nltk.data.load()` path-traversal is not reachable from user input here
  (estnltk only ever calls it with a hardcoded resource path) — monitored
  pending a fix.

## [0.2.3] — 2026-06-29

### Changed

- **Inner-returned 500s now carry an exception type in `recent_errors`.**
  When the MCP SDK hits an unhandled error in request handling it logs the
  exception and returns its own 500, so it never reached our wrapper and the
  `/metrics` breadcrumb showed `error: null` (a blind spot — two such 500s on
  Jun 21 were unattributable). A small logging handler now captures the
  exception TYPE name the SDK logs (type only — never the message or
  traceback) and the ring buffer labels the 500 with it. Best-effort and
  bounded by a freshness window; PII-free; SECURITY.md posture unchanged.

## [0.2.2] — 2026-06-19

### Added

- **`sessions_total` at `/metrics`** — a count of MCP `initialize` calls,
  a privacy-safe proxy for client connections. It is **not** a user count:
  a client that reconnects counts again, and automated probes count too. No
  identity, IP, or request body is stored — the wrapper peeks the small
  JSON-RPC body only to read the `method`, then replays it to the inner app
  byte-for-byte. The daily snapshot records it, so day-over-day deltas give
  "connections/day". Privacy posture in SECURITY.md is unchanged.

## [0.2.1] — 2026-06-17

A small quality release: sharper AI-coinage detection and a persistent
error log at `/metrics`. No breaking changes — drop-in over 0.2.0.

### Added

- **Persistent recent-errors log at `/metrics`.** The last 20 5xx
  responses (timestamp, path, status, exception type) are kept in a ring
  buffer exposed at `/metrics` and persisted alongside the counters, so
  failures stay inspectable without relying on Fly's short-lived log tail.
  PII-free — no request bodies, no tokens.

### Changed

- **`check_compound_familiarity` now catches more AI coinages.** The
  suspect-flag logic was a single score gate at 0.55, which let coinages
  like `toortõlkeoht` (top similarity 0.571) slip through. It now flags an
  out-of-vocab compound when its top similarity is below **0.60** OR its
  fastText neighbours are mostly scrape-artifact tokens (the `mõtteliin`
  failure mode). Each compound gains a `neighbour_quality` breakdown and,
  when suspect, a human-readable `reasons` list. The decision is a pure
  function (`_familiarity_verdict`), unit-tested against real model output
  without loading the 33 MB model (`tests/test_familiarity.py`).
- **Guidance against trusting `spell_check` blindly.** Vabamorf accepts any
  morphologically valid compound — including coined ones — so `spell_check`
  returning `spelling: true` does not prove a word is real Estonian. The
  `spell_check` docstring and the server instructions now say so and point
  to `check_compound_familiarity` for coined or unusual compounds.

## [0.2.0] — 2026-06-03

21 tools (up from 20), a bigger embedding model, request-count
persistence, and a round of transport/robustness hardening. No
breaking changes — drop-in over 0.1.0.

### Added

- **New tool: `check_redundancy`** — pleonasm / semantic-doubling
  check (`samuti ka` → "also also", `kõige optimaalsem` → "most
  optimal", plus fixed redundant phrases). Brings the count to **21**.
- `scripts/build_fasttext.py` — the recipe for the compressed fastText
  artifact, in-repo so the model is reproducible from source.
- `CONTRIBUTING` section in the README, with a call for native-speaker
  corrections to the linguistic lexicons.
- `/health` now returns `version` and `tools` count alongside `ok`.

### Changed

- fastText model upgraded from the 20K-vocab `mini` build to a
  100K-vocab `medium` build (~33 MB) — far fewer calque-detection
  false positives on legitimate-but-uncommon compounds.
- Public-mode rate limit raised 30 → 300/min per IP, bearer-mode
  60 → 120/min per token (data showed zero throttling at the old caps).
- `/metrics` counters now persist to a Fly volume, surviving machine
  restarts.

### Fixed / hardened

- Browser `GET /mcp` now redirects to the landing page instead of
  returning a cryptic 406; `/sse` returns a helpful pointer to `/mcp`.
- Unhandled errors in the HTTP wrapper return a clean structured 500
  with a PII-free log breadcrumb, instead of a raw crash.
- Estonian Wordnet (CC-BY-SA-4.0) attribution added to NOTICE — it was
  bundled and re-hosted but previously undocumented.
- Security: `idna` 3.13 → 3.16 (CVE-2026-45409).

### Skill

- `estonian-writing-assistant` updated: don't editorialize about the
  MCP inside deliverable copy; reference native-speaker intuition
  neutrally (`emakeele kõneleja`, not gendered framing).

## [0.1.0] — 2026-05-18

Initial public release. 20 MCP tools for Estonian writing and
analysis, fully offline (no third-party API calls at runtime).
Hosted as a public service at `https://estonian-mcp.fly.dev/mcp`;
listed on [Smithery](https://smithery.ai/servers/silly-geese/estonian-mcp);
submitted to the Anthropic Connectors Directory.

### Core NLP tools (EstNLTK + Vabamorf)

- `tokenize` — sentence + word segmentation
- `analyze_morphology` — lemma, POS, case form, root, ending, clitic,
  compound parts, ambiguity count, usage flags
  (archaic/foreign/interjection/abbreviation/proper-noun)
- `lemmatize` — dictionary form per word
- `pos_tag` — part-of-speech tags
- `spell_check` — Vabamorf spell-check + suggestions
- `syllabify` — syllables with quantity + accent
- `named_entities` — PER/LOC/ORG via the bundled CRF model
- `paradigm` — full Vabamorf-synthesised inflection paradigm for any
  Estonian word (14 cases × 2 numbers for nominals, ~30 verb forms)

### Vocabulary tools

- `synonyms` — Estonian WordNet synsets with definitions
- `find_related_words` — fastText nearest neighbours (subword-aware,
  100K-vocab medium model)

### Style + register

- `classify_register` — formal / colloquial / neutral classifier with
  matched markers and a `consistency` flag for register-mixed text
- `check_style` — repetition, passive-voice ratio, sentence-length
  variance, hedging-word density (one tool, four metrics)
- `check_object_case` — flags wrong direct-object cases under negation
  and after partitive-only verbs (`armastama`, `vihkama`, …)
- `check_compound_familiarity` — fastText-based diagnostic flagging
  out-of-vocab compounds with weak similarity (catches calques like
  `mõtteliin` for "train of thought" → real Estonian `mõttekäik`)

### EKI Reeglid orthography

- `check_capitalization` — Algustäheortograafia: weekdays, months,
  nationalities, and language/culture adjectives wrongly capitalised
- `check_compounds` — Liitsõnaõigekiri: common compound splits
  (`kooli maja` → `koolimaja`)
- `check_punctuation` — Kirjavahemärgid: missing commas before
  subordinating conjunctions (`et`, `kuna`, `sest`, `kuigi`, …)
- `check_hyphenation` — Poolitamine: safe line-break positions
- `check_numbers` — Decimal (`3.14` → `3,14`) and thousands
  (`1,000,000` → `1 000 000`) separator rules
- `check_abbreviation_hyphenation` — `MCPst` → `MCP-st`,
  `OÜle` → `OÜ-le` per EKI's lühendiortograafia rule

### Transport + ops

- Stdio for local Claude Desktop / Cursor / Code clients
- Streamable HTTP for claude.ai, Cowork (remote mode), Smithery
  hosting, self-hosted Fly.io
- Public-mode authentication off, bearer-mode on (env-var-gated)
- Per-IP and per-token rate limiting (120/min bearer, 300/min public)
- `/health` endpoint (public, version + tool count)
- `/metrics` endpoint (public, aggregate request counts, persisted
  via Fly volume so totals survive machine restarts)
- `/.well-known/mcp/server-card.json` for registry auto-discovery
- Estonian-flag favicon served at `/favicon.svg`, `/favicon.png`,
  `/favicon.ico` so Anthropic + Smithery surface the right icon

### Skills

- `estonian-writing-assistant` — agent skill that guides Claude through
  proofreading, register-aware rewriting, breaking repetition, and
  morphology study workflows using all 20 tools

### Privacy posture

- No outbound network calls at runtime
- No request bodies, tokens, or per-tool counters logged
- Aggregate-only counters at `/metrics`, optionally persisted to a
  Fly volume
- `PRIVACY.md`, `SECURITY.md`, `TERMS.md` document the full posture
