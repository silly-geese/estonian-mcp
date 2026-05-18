"""Build the 100K-vocab compressed Estonian fastText shipped with this MCP.

This is the recipe used to produce the `fasttext-et-medium` artifact
mirrored at the `v0.1.0-models` GitHub Release. You should NOT need to
run this in normal operation — the Dockerfile fetches the pre-built
artifact at image-build time. Re-run only when you want to:

  - regenerate the model against a newer cc.et.300 release
  - tune the vocab / ngram sizes
  - reproduce the build from source for audit / verification

Pipeline:
  1. Download cc.et.300.bin.gz from Facebook (~4.5 GB compressed,
     ~7 GB unpacked). Cached after first run.
  2. Load with gensim's load_facebook_model (~50 s, ~4 GB RAM).
  3. Compress with compress-fasttext, pruning to 100K vocab /
     200K ngrams. Output: ~33 MB.
  4. Save to .context/models/fasttext-et-medium.

Run from the repo root:

  uv run python scripts/build_fasttext.py

Disk requirement: ~12 GB free for the intermediate files (cc.et.300.bin
unpacks to ~7 GB). The two .bin / .bin.gz files are kept in
.context/models/ for incremental re-runs; delete them after a
successful build to reclaim disk.

License: cc.et.300 is CC-BY-SA-3.0 (Grave et al., LREC 2018,
https://fasttext.cc/docs/en/crawl-vectors.html). The compressed
artifact this script produces inherits CC-BY-SA-3.0. See NOTICE.
"""

from __future__ import annotations

import gzip
import shutil
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / ".context" / "models"
SRC_URL = "https://dl.fbaipublicfiles.com/fasttext/vectors-crawl/cc.et.300.bin.gz"
SRC_GZ = OUT_DIR / "cc.et.300.bin.gz"
SRC_BIN = OUT_DIR / "cc.et.300.bin"
OUT_MODEL = OUT_DIR / "fasttext-et-medium"

VOCAB_SIZE = 100_000
NGRAMS_SIZE = 200_000


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if not SRC_BIN.exists():
        if not SRC_GZ.exists():
            print(f"[1/4] downloading {SRC_URL} → {SRC_GZ}")
            t0 = time.time()
            with urllib.request.urlopen(SRC_URL) as r, open(SRC_GZ, "wb") as f:
                shutil.copyfileobj(r, f, length=1 << 20)
            print(f"      done in {time.time() - t0:.0f}s, "
                  f"{SRC_GZ.stat().st_size / 1e9:.2f} GB")
        else:
            print(f"[1/4] using cached {SRC_GZ}")

        print(f"[2/4] decompressing {SRC_GZ} → {SRC_BIN}")
        t0 = time.time()
        with gzip.open(SRC_GZ, "rb") as r, open(SRC_BIN, "wb") as w:
            shutil.copyfileobj(r, w, length=1 << 20)
        print(f"      done in {time.time() - t0:.0f}s, "
              f"{SRC_BIN.stat().st_size / 1e9:.2f} GB unpacked")
    else:
        print(f"[1-2/4] using cached {SRC_BIN}")

    print("[3/4] loading model with gensim "
          "(~3-4 GB RAM, takes ~50 s)…")
    t0 = time.time()
    import gensim.models.fasttext
    ft = gensim.models.fasttext.load_facebook_model(str(SRC_BIN))
    print(f"      loaded in {time.time() - t0:.0f}s")

    print(f"[4/4] compressing → vocab={VOCAB_SIZE}, ngrams={NGRAMS_SIZE}")
    t0 = time.time()
    import compress_fasttext
    # gensim 4.x stores ngram vectors under ft.wv (KeyedVectors);
    # compress-fasttext expects the KeyedVectors object directly.
    small = compress_fasttext.prune_ft_freq(
        ft.wv,
        new_vocab_size=VOCAB_SIZE,
        new_ngrams_size=NGRAMS_SIZE,
        pq=True,
    )
    print(f"      compressed in {time.time() - t0:.0f}s")

    small.save(str(OUT_MODEL))
    size_mb = OUT_MODEL.stat().st_size / 1e6
    print(f"\nDone. {OUT_MODEL}  →  {size_mb:.1f} MB")

    # Sanity probes: a known-good compound + a known calque.
    # `raamatukogu` should be in-vocab with high top-similarity;
    # `mõtteliin` (English "train of thought") should be OOV with weak
    # similarity — the AI-calque failure mode this build helps catch.
    print("\nSanity: most_similar('raamatukogu') — should be coherent")
    for w, s in small.most_similar("raamatukogu", topn=5):
        print(f"  {s:.3f}  {w}")
    print("\nSanity: most_similar('mõtteliin') — should be weak (subword-only)")
    for w, s in small.most_similar("mõtteliin", topn=5):
        print(f"  {s:.3f}  {w}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
