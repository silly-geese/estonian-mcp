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

## `eval_inflection.py`

Benchmarks estonian-mcp's morphology engine (Vabamorf, the synthesizer
behind the `paradigm` tool) against TalTechNLP's
[`inflection_et`](https://huggingface.co/datasets/TalTechNLP/inflection_et)
dataset — the noun-phrase inflection task from EKI's
[Keelemudelite mõõdupuu](https://moodupuu.eki.ee/).

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

## License

cc.et.300 is CC-BY-SA-3.0 (Grave et al., LREC 2018). The compressed
artifact this script produces inherits CC-BY-SA-3.0. See
[`NOTICE`](../NOTICE) for attribution.
