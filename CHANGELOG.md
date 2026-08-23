# Changelog

All notable user-facing changes to this MCP server.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/);
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.5.6] — 2026-08-23

Everything here came out of auditing the 13 `inflection_et` gold rows that
contradict EKI. Those 13 are the dataset's, and they are reported upstream.
The four defects below are ours, they were sitting behind the same
benchmark, and none of them had a test.

### Fixed

- **`paradigm` reported that ordinals, comparatives and superlatives do not
  inflect.** The nominal check listed `S`/`A`/`P`/`N` only, so `esimene`,
  `teine`, `kolmas`, `parem`, `suurem`, `parim` and every other `O`, `C` or
  `U` word came back as *"Sõnaliik 'O' ei käändu ega pöördu, paradigmat
  pole."* with an empty `forms`. That is 1.5% of the 20k most frequent
  Estonian word forms, and Vabamorf synthesises all three classes correctly
  under their own POS code. The tool simply never asked.

  This is the failure mode the server exists to prevent: an agent told a
  word has no paradigm does not stop, it guesses.

- **`paradigm` denied a paradigm to inflecting words with an adverb
  reading.** `kaunis` on its own is disambiguated `D` (`kaunis hea` =
  "quite good"), so `kaunis : kauni : kaunist`, an entirely ordinary
  adjective, was reported as having no paradigm at all. When the
  disambiguated reading does not inflect, the tool now looks for an
  inflecting reading **of the word's own lemma** and uses it, disclosing
  the other reading in `reading_estonian`.

  The guard is deliberately narrow. `veel` also carries a noun reading, but
  its lemma is `vesi`: answering a question about "still" with the paradigm
  of "water" would be worse than the bug. So the reading must be the word's
  own base form and must be an adjective, comparative, superlative or
  ordinal. Noun readings are not promoted, because a function word's
  surface routinely collides with a rare noun (`koos` to `koosi`, `miks` to
  `miksi`).

- **`paradigm` merged two different words into one table.** `kott` inflects
  as either `koti` (bag) or `kota`, two words that share a nominative. The
  output was a single table with `sg g: ["kota", "koti"]` and
  `sg p: ["kotta", "kotti"]`, nothing saying those belong to different
  words, and Vabamorf's lexicon order putting the rarer one first. An agent
  taking the first candidate got `kota`.

  Each inflection type is now generated separately under its own Vabamorf
  `hint`, so a table can no longer take its genitive from one word and its
  partitive from the other. `paradigm_key` names the type by its singular
  genitive, `paradigm_count` says how many exist, the rest are in
  `other_paradigms`, and `ambiguity_estonian` explains it in Estonian.

  Ranking uses corpus attestation from the fastText vocabulary that
  `find_related_words` already loads: `koti` is in it, `kota` is not. This
  is a local read of an already-present file, no network, and it is
  consulted **only** when a lemma has more than one inflection type (about
  2.5% of words). With the model absent, the order is Vabamorf's,
  `ranked_by_corpus_frequency` is `false`, the Estonian text says so, and
  no paradigm is lost.

- **`paradigm` threw away the caller's own evidence.** `paradigm("koti")`
  returned exactly what `paradigm("kott")` returned, although the input
  said which of the two words was meant. An inflected input now selects its
  inflection type directly, which beats corpus frequency because it is
  exact. The tool `note` used to advise the opposite ("pass the bare lemma
  ... for the cleanest result"); it now says what is true.

- **Smaller, same area.** Duplicate lexicon entries no longer surface as
  `["halli", "halli"]`. A form is no longer dropped from the table when
  POS-constrained synthesis comes up empty; synthesis falls back instead.
  A word whose forms never change (`väärt`, `eri`) is labelled `invariant`
  rather than quietly returning 28 identical strings.

### Changed

- **`inflection_et` benchmark: 96.5% to 99.1% first-candidate**, with
  any-candidate unchanged at 99.1%. First-candidate now equals
  any-candidate, and **every one of the 13 residual misses is a gold row
  that contradicts EKI**, so the EKI-adjudicated score is 100% / 100%.
  Without the fastText model the raw first-candidate figure is 96.6% and
  any-candidate is still 99.1%, so nothing regresses when it is absent.

- **`scripts/eval_inflection.py` now imports the server's synthesis path**
  instead of re-implementing it, so the published number measures the
  product. Only the phrase splitting and rejoining is harness code, and the
  docstring says so. It reports the raw score and an EKI-adjudicated score
  side by side.

### Notes

- **The 13 disputed gold rows** are recorded in
  `data/inflection_et_eki_disputes.json` with the EKI form and the rule
  cited: 12 decline a `-tud` participle that is invariant as a pre-modifier
  (`läbimõeldu plaani` for `läbimõeldud plaani`), 1 leaves a `-v`
  participle undeclined where it must agree (`rahuldav tulemuse` for
  `rahuldava tulemuse`). The dataset looks generated word by word with a
  synthesizer, which is exactly the operation that cannot know either rule.

  The adjudication self-invalidates: each dispute is re-checked against the
  live dataset before it counts, and is reported as stale and awarded
  nothing once the data changes upstream.
  `uv run python scripts/eval_inflection.py --report-disputes` prints them
  as a markdown table for reporting upstream.

- `tests/test_paradigm.py` is new: 202 checks over all four defects, both
  EKI rules, the disputed rows, the degraded no-model path, and the guards
  that keep the reading rescue from being worse than the bug.
## [0.5.5] — 2026-08-23

### Fixed

- **`analyze_morphology` reported `-mata` attributes as declinable**
  ([#42](https://github.com/silly-geese/estonian-mcp/issues/42), reported
  by @tomkabel with the normative citations). The `-mata` form is the
  tud-participle's negative counterpart and EKI states it "jääb alati
  käändumatuks": `täitmata lepingute reserv`, not *täitmatute. The ending
  test listed only `-tud`/`-dud`/`-nud`, so every `-mata` attribute came
  back `indeclinable: false`, nudging a consumer toward the non-standard
  declined form. This bites hardest in the legal and administrative
  register the editorial tools target, where `-mata` is everywhere.

- **`-tu` caritive adjectives were frozen when they should agree.** Not
  reported, but the reporter's own EKI citation points at it: `-tu`
  caritives DO agree, and their nominative plural also ends in `-tud`
  (`õnnetu` → `õnnetud`, `lugematu` → `lugematud`). An ending test cannot
  tell those from participles, so it marked them invariant, which would
  produce *`õnnetud laste` where the correct form is `õnnetute laste`.

  Rather than add `-mata` to the ending list and leave that in place, the
  check now consults Vabamorf, which separates *most* of them: a frozen
  attributive has an adjective reading carrying no case/number form, a
  declining one only ever carries `sg n` / `pl n`. Not all — see Known
  limits below. The ending list stays as a fallback,
  because Vabamorf sometimes misanalyses these as nouns (`hajutatud` →
  `S/pl n/hajutatu`) and the ending is correct there.

  `analyze_morphology` passes the analyses it already has, so the hot path
  costs nothing extra; other callers get a cached lookup.

- **A second `-mata` trap, caught while self-reviewing this fix.** A noun
  whose stem ends in `-ma` forms its abessive in `-mata`: `teema` →
  `teemata`, `kliima` → `kliimata`, `draama` → `draamata`. Those are
  inflected nouns, not the `mata`-form, so simply adding `mata` to the
  ending list froze them. The check now declines anything with an
  abessive reading before the ending fallback runs. The issue author
  predicted this class ("a handful of non-participle words end in
  `-mata`") and they were right.

- **Ordinary plural nouns were being frozen too, and that was the worst of
  the three.** Any Estonian noun whose nominative plural ends `-tud`,
  `-dud` or `-nud` hit the ending test: `raamatud`, `linnud`, `laenud`,
  `kohtud`, `toidud`, `säästud`. An agent following the documented
  contract would write *`paksude raamatud` for `paksude raamatute`. This
  predates #42, but it sits in the same code path and the fix is the same
  shape: the lemma separates them, because Vabamorf's misanalysed
  participles lemmatise to a deverbal stem (`hajutatud` → `hajutatu`)
  while an ordinary plural does not (`raamatud` → `raamat`). The ending
  fallback now requires a deverbal lemma.

- **The verdict no longer depends on Vabamorf's analysis ordering.** A
  participle like `tuntud` comes back as `A/''`, `V/tud`, `A/pl n` and
  `A/sg n`; reading only the first analysis meant a reordering upstream
  could silently flip it. All adjective readings are considered, and one
  with no case/number form is enough to freeze the word. The probe is also
  looked up from the lowercased word, so capitalisation cannot change the
  answer (`Täitmata` analysed as `H/sg n` where `täitmata` is `V/mata`).
  Without this, `lugupeetud` — the standard salutation in Estonian
  official correspondence — reported as declinable.

### Known limits

Stated here because the morphology cannot resolve them, and the tests
assert them so the documentation and the behaviour stay in step:

- A caritive whose lemma ends `-tu` and which Vabamorf tags only as a noun
  (`korratud` → `korratu`, `maitsetud`) is still frozen. That lemma is
  indistinguishable from a deverbal `hajutatu`.
- Where the caritive plural and the participle are the same string
  (`nõutud`, `kaalutud`, `kohatud`), the participle reading wins.
  Separating them needs semantics, not morphology.

### Notes

- The `inflection_et` benchmark is **unchanged at 96.5% first-candidate /
  99.1% any-candidate**, and that is not a coincidence worth trusting: its
  200 noun phrases contain zero `-mata` attributes and no `-tu` caritive
  plurals, so it exercises neither defect and could not have caught either.
  `tests/test_indeclinable.py` is what guards this now.

## [0.5.4] — 2026-08-23

### Security

- **The public per-IP rate limit could be evaded entirely by varying
  `X-Forwarded-For`.** `_client_ip` returned `scope["client"][0]`, which
  uvicorn (`proxy_headers=True`, `forwarded_allow_ips="*"`) had rewritten
  from the **leftmost** XFF entry. A proxy *appends* to whatever the caller
  sent, so that entry is entirely caller-controlled: every request with a
  different header got its own fresh bucket, and the public deployment's
  only DoS protection did nothing.

  Reproduced against a local server in public mode at a 5/min limit — a
  fixed spoofed value gets 429 after five requests, rotating values stay
  200 indefinitely. `SECURITY.md` asserted the opposite and has been
  corrected.

  `_client_ip` now reads the **Nth-from-right** entry, where N is
  `ESTNLTK_MCP_TRUSTED_PROXY_HOPS` (default 1 for Fly's single edge proxy).
  A caller cannot append after a proxy, so this is correct whether the edge
  appends to a client-supplied header or replaces it. uvicorn's
  `proxy_headers` is now **off**, because the fallback needs
  `scope["client"]` to be the real peer — with it on, even
  `TRUSTED_PROXY_HOPS=0` stayed bypassable, which the tests caught.

  Set `ESTNLTK_MCP_TRUSTED_PROXY_HOPS=0` if you run the server directly
  exposed with no proxy in front.

  Surfaced while reviewing PR #36; credit to @laazik, whose nginx config
  prompted the question even though the bug is ours, not theirs.

## [0.5.3] — 2026-08-23

Fixes both issues reported by @Kivaste against 0.5.1. Neither affected the
hosted server or the one-click image — verified by calling both tools on
the live deployment before changing anything — so this is a source-install
release. #37 asked whether the image was affected too; it is not, and CI
now asserts that rather than leaving it to luck.

### Fixed

- **`check_term_consistency` reported a confident negative while running at
  half strength** ([#38](https://github.com/silly-geese/estonian-mcp/issues/38)).
  With Estonian WordNet missing it returned "Ebajärjekindlat terminikasutust
  ei tuvastatud" — reads as a clean bill of health — while the
  `shared-wordnet-synset` rule never ran. The only signal was a `rules_run`
  flag you had to know to read. Now the degradation is stated in
  `summary_estonian` itself, in Estonian, with the command that fixes it,
  and a top-level `degraded: true` field. As the reporter put it: a crash is
  honest, this was not.
- **The server no longer attempts to DOWNLOAD a missing resource.** The old
  code called `Wordnet()` and caught the fallout, so on a machine without
  the resource EstNLTK would try to fetch it — breaching the "no outbound
  HTTP calls" promise in PRIVACY.md — and print its confirmation prompt to
  stdout, which under stdio transport *is* the MCP protocol channel. A new
  `_wordnet_available()` checks the filesystem first via
  `get_resource_paths(download_missing=False)`, so neither can happen.
  `synonyms` now raises a clear, actionable error instead.

### Added

- **`scripts/fetch_resources.py`** — the missing setup step for source
  installs ([#37](https://github.com/silly-geese/estonian-mcp/issues/37)).
  Fetches NLTK `punkt_tab`, Estonian WordNet and the fastText model, none of
  which can come from `uv.lock` because they are data, not Python
  distributions. Sets `SSL_CERT_FILE` from `certifi` first — uv-provisioned
  interpreters ship without a CA trust store, so the documented
  `nltk.download()` route fails with `CERTIFICATE_VERIFY_FAILED`; credit to
  the reporter for diagnosing that. fastText is checksum-verified and moved
  into place only after verification, so an interrupted download cannot
  masquerade as a good model. Idempotent.

  It is a script, not lazy auto-download, on purpose: the network access is
  yours at setup time, never the server's at request time.

### Changed

- **The server now structurally refuses resource downloads.** Adversarial
  review caught that the first version of this fix made things *worse*, not
  better: `get_resource_paths()` consults EstNLTK's resource index and
  re-fetches it over HTTPS whenever the local copy is more than two hours
  old. So a healthy, long-running server made a periodic outbound call to
  `raw.githubusercontent.com` on the next `synonyms` or
  `check_term_consistency` — and when that call failed it reported an
  *installed* WordNet as missing. `_wordnet_available()` now reads the
  resources directory directly.

  A second path was open in the same way: EstNLTK's sentence tokenizer
  catches `LookupError` for NLTK's `punkt_tab` and calls
  `nltk.downloader.download()`. The `sentences` layer is built by
  `tag_layer(["morph_analysis"])`, so nearly every tool could reach it.
  `_forbid_resource_downloads()` now pins the index timeout and replaces
  NLTK's downloader with a refusal that names the setup script.
- **`find_related_words` and `check_compound_familiarity` now find the
  model a source install actually has.** `_embeddings()` defaulted to the
  container path only, while `fetch_resources.py` writes to
  `~/.cache/estnltk-mcp/`. Following the documented setup and then running
  the documented verify step failed, because the script cannot export a
  variable into the server process — and a JSON-configured MCP client
  cannot run a shell `export` at all. The lookup now tries both.
- **Dockerfile fetches `punkt_tab` explicitly and asserts it loads.** The
  image already worked, but by accident rather than by construction — a
  base-image change could have removed it silently and broken
  `check_compounds` for every one-click user.
- **CI covers both issues.** The container test now calls `check_compounds`
  and asserts `check_term_consistency` comes back `degraded: false`, so the
  image can never regress into serving confident-but-partial answers. The
  smoke matrix runs the new `tests/test_resources.py` and executes
  `fetch_resources.py` to prove it is idempotent.
- **README** leads the source-install path with the fetch step and explains
  why the server never downloads anything itself.
- **`tests/test_no_network.py` enforces the privacy promise** rather than
  documenting it: every tool runs with sockets and DNS blocked, and the
  blocker *records* each attempt instead of only raising — the first
  version raised, and the code under test caught the exception by design,
  so it passed against a live violation. Covers the stale-index case
  specifically, which is the production steady state.
- **A `source-install` CI job follows the README verbatim** from a clean
  state with every resource path isolated: it asserts the tools fail
  *actionably* before setup, runs the documented command, then proves they
  work with no environment override. The existing `smoke` job pre-fetches
  resources and exports `ESTNLTK_MCP_FASTTEXT_PATH`, which is precisely why
  it stayed green while a README-following user got a broken install.
- **`scripts/fetch_resources.py` validates by use, not by existence.** Each
  resource is checked by actually tokenising / querying / hashing it, so a
  truncated or partially-extracted artifact is replaced rather than
  accepted forever. punkt_tab extracts to a staging dir with zip-slip
  protection and is swapped in only after it tokenises, with rollback.

## [0.5.2] — 2026-08-23

### Added

- **`mcp_methods` at `/metrics`** — POST `/mcp` traffic bucketed by JSON-RPC
  method. This exists to answer a question the old counters could not:
  `tool_calls_total / sessions_total` was 0.53, i.e. more than half of
  `initialize` calls never led to a tool call, with no way to tell probes
  from real clients bouncing. Now `initialize` vs
  `notifications/initialized` shows how many handshakes actually completed
  rather than being abandoned mid-probe, and `tools/list` vs `tools/call`
  shows how many clients enumerate the tools but never call one.

  Method names are bucketed against a **fixed allowlist**; anything else
  counts as `other`. The method comes from a request body and is therefore
  caller-controlled, so it is never stored verbatim — that keeps the
  metrics dict bounded against a hostile client inventing method names and
  keeps arbitrary caller strings off `/metrics`. Still PII-free: only the
  `method` field is read, never params, arguments or clientInfo.

  Deliberately NOT "sessions that made >=1 tool call". The transport runs
  `stateless_http`, so an `initialize` cannot be tied to the calls that
  follow it, and introducing a client identifier to make that possible
  would be a privacy step backwards. The method mix answers the same
  question from the other side.

### Changed

- `_is_initialize_request` is replaced by `_classify_mcp_method`, which
  parses the method once and buckets it. The old helper could skip the
  JSON parse via a substring gate; the new one parses every POST `/mcp`
  body. The body is already fully buffered at that point and a `json.loads`
  on even a 100k-char tool call is well under a millisecond against tool
  executions that run 10ms-7s, so the visibility is worth the trade. Method
  names are still matched by parsing rather than substring, so a tool call
  whose Estonian text merely contains `initialize` is not miscounted —
  pinned by test.

### Security

- **cryptography 48.0.1 → 50.0.0** (CVE-2026-69247, high): a Bleichenbacher
  oracle in PKCS#7 EnvelopedData decryption via distinguishable errors and
  timing. Transitive only (`mcp` → `pyjwt` → `cryptography`) and not in the
  exploitable path — the server never touches JWT or PKCS#7; bearer auth is
  a `secrets.compare_digest` comparison, and public mode has no auth at
  all. Patched regardless.

## [0.5.1] — 2026-07-31

### Changed

- **`check_officialese`'s nominalisation label now carries both Estonian
  terms**: `nimisõnastumine ehk nominalisatsioon` (was `nimisõnastumine
  (mine-vormide rohkus)`). Both names are in use and a reader may know
  only one; the concrete `-mine` detail already appears in each issue's
  `explanation` field, so the parenthetical was redundant. Output-only
  change, no logic touched.

## [0.5.0] — 2026-07-31

Editorial release. Everything here came out of replaying a real Estonian
editing conversation through the server: a native speaker rewriting an
R&D report for readability, asking "is this the right word here?", and
being told `korpus` is wrong for image data. The MCP was never invoked
in that conversation, and when the text was replayed through the tools,
most of them stayed silent or gave the wrong verdict. 24 tools → 26.

### Added

- **`check_officialese(text)` — kantseliit check for NON-legal Estonian.**
  `check_legalese` exists but is scoped to statutes: on a real R&D report
  paragraph it returned zero issues, because its filler lexicon is
  legal-specific and its 34-word gate sits above where Estonian prose
  actually becomes unreadable. The new tool measures nominalisation
  density (each `-mine` noun paired with the verb to swap in,
  `hindamine` → `hindama`), correctly-counted umbisikuline tegumood,
  clause stacking (`mille käigus … ning …`, which a raw word count
  misses), an Estonian-calibrated 25-word sentence gate, and
  administrative filler (`omab` → `on`, `kujutab endast` → `on`,
  `viidi läbi` → `tehti`, `mudeli poolt loodud` → `mudeli loodud`).
  Thresholds are calibrated against a native speaker's own rewrite of
  the report: impersonal ratio 0.875 → 0.308, `-mine` per 100 words
  7.89 → 2.33, longest sentence 30 → 21 words. The bureaucratic
  original flags; the human rewrite comes back clean.
- **`check_term_consistency(text)` — one referent, one term.** The
  long-document defect a model editing paragraph-by-paragraph reliably
  misses: a dataset that is `andmestik` on page 1, `teadusandmestik` on
  page 2 and `pildiandmestik` on page 3. Two precision-first rules —
  shared compound head, and shared Estonian WordNet synset — with
  per-variant counts so you can standardise on the dominant term. Two
  compounds sharing a head is deliberately *not* enough to flag, since
  those are usually distinct things. Known gap, stated in the tool's own
  note: synonyms sharing neither a head nor a synset (`korpus` /
  `andmestik`) are not caught.

### Fixed

- **`check_style` mis-counted umbisikuline tegumood four ways**, all
  verified against Vabamorf tags on real text. `ei` / `ära` are tagged
  `pos=V form=neg`, so every negation inflated `total_verbs` and
  *deflated* the ratio. The `ta` / `da` impersonal-present-negative form
  (`ei esitata`) was absent from the form set and missed entirely.
  `ei avaldatud` is tagged `pos=A`, not `V`, and was skipped. And
  attributive `-tud` participles (`lukustatud hindamisosa` — a modifier,
  not a predicate) were counted as passive; they now appear under
  `attributive_excluded` instead. The counter is shared with the two new
  tools, so all three agree.
- **`check_compound_familiarity`'s junk-neighbour gate inverted human
  judgement.** `pildiandmestik` — whose top fastText neighbour is its own
  head `andmestik` at 0.71 — was flagged suspect purely because 5/8 of
  the neighbour *tail* was scrape junk, while `teadusandmestik` (0.705),
  the word a native speaker called artificial, passed. The junk gate is
  now decisive only when the compound is also weak at the top (< 0.60)
  or its top neighbour is itself junk; a clean, real, ≥ 0.60 top
  neighbour vouches for the compound whatever the tail looks like.
  `mõtteliin` and `toortõlkeoht` still flag. The tool note now states the
  converse limit explicitly: similarity cannot judge register, so a
  well-formed but *stilted* compound will pass. Relatedly, the
  `neighbour_quality.legal_term` marker is now stamped for every known
  term of art rather than only for ones whose verdict had to be rescued —
  with the guard in place, `solidaarvõlgnik` clears on its own merits but
  callers still want to know it is attested legal vocabulary.

### Changed

- **`classify_register` scored dense officialese as `neutraalne`, 0.0,
  with zero markers** — while 87.5% of that text's verbs were
  impersonal. Two fixes: the lexicon gains academic/report vocabulary
  (`aruandeperiood`, `ettevõttesiseselt`, `valideerima`, `metoodika`,
  …), and a new `structure` block folds in umbisikuline tegumood ratio
  and noun/verb density. The structural component is bounded at +0.4 and
  applies only from 25 words up and only when the lexicon is not
  net-colloquial, so short strings still score purely on the lexicon and
  chatty copy can never be nudged formal.
- **`synonyms` now documents the word-fit check.** The server already
  knew that `korpus` means "kirjaliku või suulise teksti elektrooniline
  kogu" — the exact fact that settles whether a set of images can be
  called one — but returned it as one of five unranked senses with
  nothing telling the model to test the gloss against context. The
  docstring now says to read each `definition` for its domain constraint
  when the question is "is this the right word here?".
- **Server instructions name the editorial use case.** Every tool was
  named for a *mechanical* check, so models didn't reach for the server
  on "is this the right word" or "make this read more human". The
  `initialize` instructions now route those questions explicitly.
- `estonian-writing-assistant` skill: two new workflows (de-bureaucratise
  Estonian prose; "is this the right word here?"), the new tools in the
  table, and a corrected `check_compound_familiarity` threshold (the
  skill still documented the old 0.55 gate).

## [0.4.4] — 2026-07-29

### Security

- Dependency refresh resolving 13 Dependabot advisories (11 high, 2 medium),
  no API or behaviour change: **pillow** 12.2.0 → 12.3.0. Pillow is a
  transitive dev/eval dependency (via matplotlib) that the running server
  never invokes, so the advisories were not in the exploitable path. This
  patches it regardless and makes `/health` reflect the refreshed build.

## [0.4.3] — 2026-07-20

### Changed

- **Static icons now serve a 1-year immutable cache + ETag with `304 Not
  Modified` handling.** A client ignoring the existing cache header had been
  re-fetching `/favicon.svg` in a loop (~65% of all requests — most likely
  connector-directory icon rendering); conditional and well-behaved clients
  now stop re-downloading. Cheap, no functional change.
- **README getting-started reflects the Connectors Directory.** Now that
  estonian-mcp is in Anthropic's official directory, the install flow leads
  with one-click from the directory; pasting the custom URL is the fallback.

## [0.4.2] — 2026-07-18

### Security

- Dependency refresh resolving 6 high-severity Dependabot advisories, no
  API or behaviour change: **mcp** 1.27.0 → 1.28.1 (3 high — the core MCP
  SDK), **nltk** 3.9.4 → 3.10.0 (1 high — the `nltk.data.load()`
  path-traversal previously left open for lack of an upstream patch, now
  fixed and no longer just monitored), **soupsieve** 2.8.3 → 2.8.4 (2 high).

### Changed

- Docs/descriptions now credit **Riigi Teataja** (the public-domain
  legislation behind `common_legal_usage`) alongside EstNLTK + EKI Reeglid,
  and note **one-click install from Anthropic's Connectors Directory**.
  Updated the README intro, `pyproject.toml`, and the GitHub About
  description (Smithery listing is a manual dashboard field).

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
