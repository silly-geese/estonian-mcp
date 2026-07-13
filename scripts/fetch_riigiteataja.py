"""Fetch consolidated Estonian legislation text from Riigi Teataja into a
directory of .txt files, ready for build_legal_collocations.py --source dir.

Riigi Teataja legislation is PUBLIC DOMAIN (Estonian law is freely reusable),
so an index built from this is license-clean and shippable — unlike the
non-commercial HuggingFace corpus.

RT serves each act's consolidated text via a clean JSON/HTML API:
    https://www.riigiteataja.ee/api/v1/akt/{id}/blob-html
(the human site is a JS SPA; this is the underlying data endpoint). Pass the
numeric act ids — look them up by opening the act on riigiteataja.ee and
reading the /akt/{id} in the URL. A few core codes are included as defaults.

  uv run python scripts/fetch_riigiteataja.py --out rt_txt/
  uv run python scripts/fetch_riigiteataja.py --ids 961235 13114893 --out rt_txt/
"""
from __future__ import annotations

import argparse
import html
import re
import sys
import urllib.request
from pathlib import Path

# A few core codes (act id → short name). VÕS is confirmed; extend freely by
# looking up ids on riigiteataja.ee. Kept small on purpose — coverage scales
# by adding ids, not by changing code.
DEFAULT_ACTS = {
    961235: "vols",    # Võlaõigusseadus — Law of Obligations Act
    12806823: "tsys",  # Tsiviilseadustiku üldosa seadus — General Part of the Civil Code
    22407: "tsms",     # Tsiviilkohtumenetluse seadustik — Code of Civil Procedure
    12807782: "aos",   # Asjaõigusseadus — Property Law Act
    184411: "kars",    # Karistusseadustik — Penal Code
}

_API = "https://www.riigiteataja.ee/api/v1/akt/{id}/blob-html"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t ]+")
# Rough sentence split for legal text: end punctuation, or a new § / clause.
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\s*(?=§\s*\d)")


def fetch_text(act_id: int) -> str:
    req = urllib.request.Request(_API.format(id=act_id),
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode("utf-8", errors="replace")
    txt = html.unescape(_TAG_RE.sub(" ", raw))
    return _WS_RE.sub(" ", txt)


def to_sentences(text: str) -> list[str]:
    out = []
    for chunk in _SENT_SPLIT_RE.split(text):
        s = chunk.strip()
        # Keep sentence-like lines: enough words, not boilerplate headers.
        if s and len(s.split()) >= 3:
            out.append(s)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", type=int, nargs="*", default=list(DEFAULT_ACTS))
    ap.add_argument("--out", default="rt_txt")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    total = 0
    for act_id in args.ids:
        name = DEFAULT_ACTS.get(act_id, str(act_id))
        try:
            text = fetch_text(act_id)
        except Exception as e:
            print(f"[warn] act {act_id} failed: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        sents = to_sentences(text)
        (out / f"{name}.txt").write_text("\n".join(sents), encoding="utf-8")
        total += len(sents)
        print(f"  act {act_id} ({name}): {len(text)} chars → {len(sents)} sentences")
    print(f"wrote {out}/ — {total} sentences total")


if __name__ == "__main__":
    sys.exit(main())
