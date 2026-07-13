# scripts

One-off build / maintenance scripts. Nothing in here runs at request
time — the MCP server is `server.py` at the repo root.

## `build_fasttext.py`

Builds the 100K-vocab compressed Estonian fastText model that the
`find_related_words` and `check_compound_familiarity` tools depend on.

The pre-built artifact (~33 MB, MD5 `3690ee9983fc95740a61125fd58ed385`)
is mirrored at the project's
[`v0.1.0-models` GitHub Release](https://github.com/silly-geese/estonian-mcp/releases/tag/v0.1.0-models),
and the Dockerfile fetches it at image-build time. **You only need to
run this script if** you want to:

- regenerate the model against a newer cc.et.300 release
- tune the vocab / ngram sizes (defaults: 100K / 200K)
- reproduce the build from source for audit / verification

### Running

From the repo root:

```sh
uv run python scripts/build_fasttext.py
```

Total runtime: ~5 min if the upstream `cc.et.300.bin.gz` is already
cached locally, ~15–25 min on a fresh download.

Disk: ~12 GB free required (cc.et.300.bin unpacks to ~7 GB). The
intermediate files live in `.context/models/`; delete them after a
successful build to reclaim disk.

Memory: ~4 GB RAM peak (gensim loads the full model into memory before
compression).

Output: `.context/models/fasttext-et-medium`, ~33 MB.

### Recipe

```
cc.et.300.bin (Facebook, CC-BY-SA-3.0)
  └─ gensim.load_facebook_model
     └─ compress_fasttext.prune_ft_freq(
          new_vocab_size=100_000,
          new_ngrams_size=200_000,
          pq=True,
        )
        └─ fasttext-et-medium (~33 MB)
```

### Updating the mirror after a rebuild

If you produce a new artifact and want the Dockerfile + CI to use it:

```sh
gh release upload v0.1.0-models scripts/.context/models/fasttext-et-medium \
  --repo silly-geese/estonian-mcp --clobber
md5sum .context/models/fasttext-et-medium    # update Dockerfile + CI hash
```

Then bump the MD5 in `Dockerfile` and `.github/workflows/ci.yml`.

## `build_legal_collocations.py`

Builds the legal-Estonian collocation / frequency index behind the
`common_legal_usage` tool — the "what's the canonical legal phrasing"
engine (`hagi` → `esitama hagi`, `kohustus` → `kohustuse täitmine`).

**The data, the smart way:** the pipeline never stores the corpus. It
streams source text sentence-by-sentence, lemmatises with Vabamorf,
counts adjacent content-word collocations, and *discards the text* —
only the pruned ~KB/MB index is written. Memory stays bounded no matter
how big the corpus is.

Sources (mind the licence):

```sh
# POC (default) — small, license-clean authored sample; this is what
# ships in data/legal_collocations.json.gz and is used by the tests.
uv run python scripts/build_legal_collocations.py

# Production, license-clean — point at .txt files of PUBLIC-DOMAIN
# Riigi Teataja legislation (Estonian law is freely reusable).
uv run python scripts/build_legal_collocations.py --source dir --corpus-dir rt_txt/

# Research/eval ONLY — streams paulpall/legalese-sentences_estonian, which
# is NON-COMMERCIAL (Estonian National Corpus). Do NOT ship the result.
uv run --with datasets python scripts/build_legal_collocations.py --source hf --limit 8000
```

The **committed index** (`data/legal_collocations.json.gz`, ~60 KB) is built
from public-domain Riigi Teataja legislation, so it ships license-clean and
the tool works out of the box. Reproduce / extend it:

```sh
# 1. fetch consolidated act text (add act ids to broaden coverage)
uv run python scripts/fetch_riigiteataja.py --ids 961235 --out rt_txt/
# 2. build the index from the fetched text
uv run python scripts/build_legal_collocations.py --source dir --corpus-dir rt_txt/
```

A larger index can also be hosted on the `v0.1.0-models` GitHub Release (like
fastText) and pointed at via `ESTNLTK_MCP_LEGAL_INDEX`.

## `fetch_riigiteataja.py`

Fetches consolidated Estonian legislation text from Riigi Teataja's public
`/api/v1/akt/{id}/blob-html` endpoint into a directory of `.txt` files, ready
for `build_legal_collocations.py --source dir`. Estonian legislation is
public domain, so an index built from it is license-clean and shippable. Look
up act ids by opening an act on riigiteataja.ee and reading `/akt/{id}` in the
URL; a few core codes are wired as defaults and coverage scales by adding ids.

## `eval_inflection.py`

Benchmarks estonian-mcp's morphology engine (Vabamorf, the synthesizer
behind the `paradigm` tool) against TalTechNLP's
[`inflection_et`](https://huggingface.co/datasets/TalTechNLP/inflection_et)
dataset — a noun-phrase inflection benchmark (Lillepalu & Alumäe,
[arXiv:2510.21193](https://arxiv.org/abs/2510.21193v2)).

estonian-mcp is a tool server, not an LLM, so it can't be ranked on the
model leaderboard. This instead scores our synthesis directly against
the benchmark's gold data: given a base noun phrase + number + case,
can we produce the correct inflected form?

```sh
uv pip install datasets   # dev-only, not a server dependency
uv run python scripts/eval_inflection.py
```

Reports any-candidate and first-candidate accuracy, a per-(number, case)
breakdown, and sample misses. Latest run: **98.1% any-candidate,
95.6% first-candidate** over 1,400 items (4 cases × sg/pl). The
residual misses cluster on indeclinable adjectives (e.g. `täis`,
`tuntud`) that Vabamorf inflects but standard Estonian leaves in base
form.

## `eval_coverage.py`

Coverage probes for two TalTechNLP datasets that — unlike
`inflection_et` — don't map to a clean accuracy score, because they're
generation/comprehension tasks while our tools are detectors/lookups:

```sh
uv run python scripts/eval_coverage.py 500   # sample size
```

- **grammar_et** (sentence correction): we report *detection recall* —
  on what fraction of erroneous sentences does any `check_*`/`spell_check`
  tool flag something. Latest: **~26%** on a 500 sample. Low by design —
  our lexicons are small and precision-oriented, not a broad grammar
  checker; read it as a floor, not a ceiling.
- **word_meanings_et** (word→definition): we report *WordNet vocabulary
  coverage* — fraction of target words with an Estonian WordNet entry
  via `synonyms`. Latest: **~65%** on a 500 sample.

The honest takeaway: deterministic morphology (`inflection_et`,
96.5%/99.1%) is our home turf; full-sentence correction and free-text
definitions are only partially served by detector/lookup tools.

## License

cc.et.300 is CC-BY-SA-3.0 (Grave et al., LREC 2018). The compressed
artifact this script produces inherits CC-BY-SA-3.0. See
[`NOTICE`](../NOTICE) for attribution.
