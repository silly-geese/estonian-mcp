# Changelog

All notable user-facing changes to this MCP server.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/);
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

- **New tool: `check_redundancy`** — pleonasm / semantic-doubling
  check (`samuti ka` → "also also", `kõige optimaalsem` → "most
  optimal", fixed redundant phrases). Brings the tool count to **21**.
- fastText model upgraded from the 20K-vocab `mini` build to a
  100K-vocab `medium` build (~33 MB), cutting calque-detection false
  positives on legitimate-but-uncommon compounds.
- `scripts/build_fasttext.py` brought into the repo so the fastText
  artifact is reproducible from source by anyone with the repo
  checked out.
- `/health` enriched: now returns `version` and `tools` count
  alongside `ok` so a single curl confirms which build is live.

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
