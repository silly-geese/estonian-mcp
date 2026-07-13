"""Build the legal-Estonian collocation / frequency index that powers the
`common_legal_usage` tool — the "what's the canonical legal phrasing" engine.

DATA, THE SMART WAY: we never store the corpus, only the statistics distilled
from it. The source is streamed sentence-by-sentence; each is lemmatised with
Vabamorf (the same engine the MCP already uses), its content-word collocations
are counted, and the text is discarded. Memory stays bounded no matter how big
the corpus is; only the pruned ~MB index is written out.

LICENSING — read before choosing a source. The distilled index is aggregate
frequency statistics (facts, not the source's copyrightable expression), but
the SOURCE still matters for a bundled/shippable artifact:

  --source dir --corpus-dir DIR   RECOMMENDED for the shippable full index.
      Point at .txt files of PUBLIC-DOMAIN legislation (Riigi Teataja — Estonian
      law is freely reusable). This is the license-clean production path.
  --source sample                 DEFAULT. A small license-clean authored
      sample of legal Estonian — used to build the committed POC index so the
      tool works out of the box and in tests.
  --source hf                     RESEARCH/EVAL ONLY. Streams
      `paulpall/legalese-sentences_estonian` (61.3k sentences). That corpus is
      "free for NON-COMMERCIAL use" (Estonian National Corpus), so do NOT ship
      an index derived from it — use it only for local experimentation.

The pipeline never stores the corpus: it streams sentence-by-sentence, counts
collocations with Vabamorf, and discards the text — only the pruned ~MB index
is written. The full artifact is meant to be hosted on the v0.1.0-models
GitHub Release (like fastText) and fetched at image-build time.

Run from repo root:

  uv run python scripts/build_legal_collocations.py                              # POC (sample)
  uv run python scripts/build_legal_collocations.py --source dir --corpus-dir rt_txt/  # full, clean
  uv run --with datasets python scripts/build_legal_collocations.py --source hf --limit 4000  # research only
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Content parts of speech worth indexing (Vabamorf codes): noun, verb,
# adjective, plus proper noun. Everything else (particles, conjunctions,
# pronouns, numerals, punctuation) is skipped as collocation noise.
_CONTENT_POS = {"S", "V", "A", "H"}

# Light/auxiliary verbs and generic lemmas that survive the POS filter but
# add only noise to collocation lists (they neighbour everything).
_STOP_LEMMAS = {
    "olema", "ei", "võima", "pidama", "saama", "tegema", "hakkama",
    "ning", "ja", "või", "see", "tema", "mis", "kes", "too",
}


def iter_corpus(source: str, limit: int | None, corpus_dir: str | None):
    """Yield raw legal sentences from the chosen source. See module docstring
    for the licensing implications of each."""
    if source == "sample":
        yield from _SAMPLE_CORPUS
        return
    if source == "dir":
        if not corpus_dir:
            raise SystemExit("--source dir requires --corpus-dir")
        n = 0
        for p in sorted(Path(corpus_dir).rglob("*.txt")):
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                if limit is not None and n >= limit:
                    return
                n += 1
                yield line
        return
    if source == "hf":
        print("[license] paulpall/legalese-sentences_estonian is "
              "NON-COMMERCIAL — for research/eval only, do NOT ship the "
              "resulting index.", file=sys.stderr)
        from datasets import load_dataset
        ds = load_dataset("paulpall/legalese-sentences_estonian",
                           split="train", streaming=True)
        for i, row in enumerate(ds):
            if limit is not None and i >= limit:
                break
            text = row.get("Corpus") or next(
                (v for v in row.values() if isinstance(v, str)), "")
            if text:
                yield text
        return
    raise SystemExit(f"unknown --source {source!r}")


# License-clean authored sample of standard legal Estonian (factual legal
# statements — not copied from any restricted corpus). Deliberately repeats
# canonical collocations so the pruned POC index carries real signal. Used
# to build the committed proof-of-concept index (--source sample).
_SAMPLE_CORPUS = [
    "Hageja esitas hagi kostja vastu.",
    "Hageja esitas hagi maakohtule.",
    "Kohus rahuldas hagi täies ulatuses.",
    "Kohus jättis hagi rahuldamata.",
    "Hagi esitamine katkestab nõude aegumise.",
    "Hagi tagamiseks võib kohus kohaldada abinõusid.",
    "Võlausaldaja nõuab võlgnikult kohustuse täitmist.",
    "Võlgnik peab täitma kohustuse õigel ajal.",
    "Kohustuse täitmine lõpetab võlasuhte.",
    "Kohustuse rikkumine annab õiguse nõuda kahju hüvitamist.",
    "Pool täidab kohustuse vastavalt lepingule.",
    "Pooled sõlmisid lepingu kirjalikus vormis.",
    "Lepingu sõlmimine eeldab poolte kokkulepet.",
    "Lepingu lõpetamine toimub seaduses sätestatud korras.",
    "Pool rikkus lepingut ja peab hüvitama kahju.",
    "Kohus rahuldas nõude osaliselt.",
    "Nõude aegumistähtaeg on kolm aastat.",
    "Võlausaldaja esitas nõude võlgniku vastu.",
    "Nõude rahuldamine eeldab tõendeid.",
    "Isik esitas taotluse tähtaja ennistamiseks.",
    "Taotluse esitamine on tasuta.",
    "Kohus vaatas taotluse läbi ühe kuu jooksul.",
    "Põhjendatud taotlus tuleb esitada kirjalikult.",
    "Kahju hüvitamine toimub rahas.",
    "Pool hüvitab kahju täies ulatuses.",
    "Kahju hüvitamise kohustus tekib õigusvastasest teost.",
    "Kostja kannab vastutust tekitatud kahju eest.",
    "Vastutuse alus on süü.",
    "Tehing on tühine, kui see on vastuolus seadusega.",
    "Kohus tuvastas, et tehing on tühine.",
    "Tühine tehing ei too kaasa õiguslikke tagajärgi.",
    "Hageja esitas apellatsioonkaebuse ringkonnakohtule.",
    "Apellatsioonkaebuse esitamise tähtaeg on kolmkümmend päeva.",
    "Ringkonnakohus jättis apellatsioonkaebuse rahuldamata.",
    "Menetlusosaline võib esitada kaebuse kohtumäärusele.",
    "Kohus tegi otsuse menetlusosaliste kohalolekul.",
    "Pärija võttis pärandi vastu.",
    "Testament tuleb koostada notariaalses vormis.",
    "Käendaja vastutab võlgniku kohustuse eest solidaarselt.",
    "Solidaarvõlgnik vastutab kogu kohustuse ulatuses.",
]


def build_index(source: str, corpus_dir: str | None, limit: int | None,
                min_freq: int, top_k: int) -> dict:
    from estnltk import Text
    from estnltk.vabamorf.morf import Vabamorf  # noqa: F401 (ensures resources)

    freq: Counter[str] = Counter()
    left: dict[str, Counter[str]] = defaultdict(Counter)
    right: dict[str, Counter[str]] = defaultdict(Counter)
    n_sent = 0

    for sentence in iter_corpus(source, limit, corpus_dir):
        n_sent += 1
        t = Text(sentence)
        t.tag_layer(["morph_analysis"])
        seq: list[str] = []
        for span in t.morph_analysis:
            pos = (list(span.partofspeech) or [""])[0]
            lemma = (list(span.lemma) or [""])[0]
            lemma_l = lemma.lower()
            if (pos in _CONTENT_POS and lemma and lemma.isalpha()
                    and len(lemma) > 1 and lemma_l not in _STOP_LEMMAS):
                seq.append(lemma_l)
        for i, lemma in enumerate(seq):
            freq[lemma] += 1
            if i > 0:
                left[lemma][seq[i - 1]] += 1
            if i + 1 < len(seq):
                right[lemma][seq[i + 1]] += 1
        if n_sent % 5000 == 0:
            print(f"  ...{n_sent} sentences, {len(freq)} lemmas", file=sys.stderr)

    lemmas: dict[str, dict] = {}
    for lemma, f in freq.items():
        if f < min_freq:
            continue
        lemmas[lemma] = {
            "freq": f,
            "left": left[lemma].most_common(top_k),
            "right": right[lemma].most_common(top_k),
        }

    return {
        "meta": {
            "source": source,
            "sentences": n_sent,
            "lemmas_kept": len(lemmas),
            "min_freq": min_freq,
            "top_k": top_k,
        },
        "lemmas": lemmas,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["sample", "dir", "hf"], default="sample")
    ap.add_argument("--corpus-dir", default=None, help="dir of .txt files for --source dir")
    ap.add_argument("--limit", type=int, default=None, help="max sentences")
    ap.add_argument("--min-freq", type=int, default=2)
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--out", default="data/legal_collocations.json.gz")
    args = ap.parse_args()

    index = build_index(args.source, args.corpus_dir, args.limit,
                        args.min_freq, args.top_k)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if out.suffix == ".gz":
        out.write_bytes(gzip.compress(payload, mtime=0))
    else:
        out.write_bytes(payload)
    m = index["meta"]
    print(f"wrote {out} — {m['sentences']} sentences, {m['lemmas_kept']} lemmas, "
          f"{out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    sys.exit(main())
