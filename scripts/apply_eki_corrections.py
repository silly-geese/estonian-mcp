"""Rebuild `inflection_et` with the 13 EKI-adjudicated rows corrected.

WHY A PATCH AND NOT A CORRECTED COPY
------------------------------------
`TalTechNLP/inflection_et` carries no licence: no dataset card, no
LICENSE file, no licence tag, only `.gitattributes` and the JSONL. No
licence means no permission to redistribute, so this repository does not
host a corrected copy of somebody else's data. It hosts the CORRECTIONS,
which are ours, and this script applies them to a copy fetched from the
original.

The result is the same for anyone who wants the corrected dataset, in
one command, and nothing here republishes data we were not given the
right to republish.

    uv run python scripts/apply_eki_corrections.py
    uv run python scripts/apply_eki_corrections.py --out /tmp/corrected.jsonl

WHAT IT CORRECTS
----------------
The 13 rows recorded in `data/inflection_et_eki_disputes.json`, where
the dataset's gold contradicts EKI on two participle rules:

  - a past participle in -tud/-dud/-nud is INVARIANT as a pre-modifier
    (`läbimõeldud plaani`, not `läbimõeldu plaani`);
  - a present participle in -v AGREES with its head noun
    (`rahuldava tulemuse`, not `rahuldav tulemuse`).

Each dispute carries its rule and the EKI citation in that file. The
same 13 are the subject of an open discussion on the dataset, which the
authors have not answered.

REFUSES RATHER THAN GUESSES
---------------------------
Every correction is checked against the row as it stands upstream before
it is applied. If a gold has changed since the dispute was recorded, the
row is left exactly as the authors have it and the change is reported as
stale, because a correction written against data that has since moved is
no longer a correction. That is the same rule `scripts/eval_inflection.py`
applies before awarding an adjudicated point.

Suggested by Pert Lomp (github.com/pertlomp/qwen38-et), who asked
whether the 13 were still live and proposed publishing a fixed version.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

DISPUTES_PATH = _ROOT / "data" / "inflection_et_eki_disputes.json"
SOURCE_URL = (
    "https://huggingface.co/datasets/TalTechNLP/inflection_et/resolve/{rev}/"
    "word_inflections_hf.jsonl"
)
DEFAULT_OUT = Path.home() / ".cache" / "estnltk-mcp" / "inflection_et_eki_corrected.jsonl"


def key(row: dict) -> tuple:
    return (row["noun_phrase"], row["plurality"], row["case"])


def fetch(revision: str) -> list[dict]:
    url = SOURCE_URL.format(rev=revision)
    print(f"fetching {url}")
    with urllib.request.urlopen(url, timeout=60) as r:
        body = r.read().decode("utf-8")
    return [json.loads(line) for line in body.splitlines() if line.strip()]


def apply_corrections(rows: list[dict], disputes: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """(corrected rows, applied disputes, stale disputes).

    A dispute is applied only when the row's gold is still exactly what
    was recorded against. Anything else is stale and the row is left as
    the authors wrote it.
    """
    by_key = {key(d): d for d in disputes}
    applied: list[dict] = []
    seen: set[tuple] = set()
    out: list[dict] = []

    for row in rows:
        k = key(row)
        d = by_key.get(k)
        if d is None:
            out.append(row)
            continue
        seen.add(k)
        if sorted(row["inflection"]) != sorted(d["dataset_gold"]):
            out.append(row)          # moved upstream: not ours to touch
            continue
        fixed = dict(row)
        fixed["inflection"] = list(d["eki_forms"])
        out.append(fixed)
        applied.append(d)

    stale = [d for k, d in by_key.items() if k not in seen
             or d not in applied]
    return out, applied, stale


def main() -> None:
    argv = sys.argv[1:]
    out_path = DEFAULT_OUT
    if "--out" in argv:
        i = argv.index("--out") + 1
        if i >= len(argv):
            sys.exit("--out needs a path")
        out_path = Path(argv[i])

    doc = json.loads(DISPUTES_PATH.read_text(encoding="utf-8"))
    rows = fetch(doc["dataset_revision"])
    if len(rows) != doc["dataset_rows"]:
        print(f"!! expected {doc['dataset_rows']} rows, fetched {len(rows)}")

    corrected, applied, stale = apply_corrections(rows, doc["disputes"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".part")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in corrected),
                   encoding="utf-8")
    tmp.replace(out_path)

    print(f"\nsource   : {doc['dataset']} at {doc['dataset_revision'][:12]}")
    print(f"rows     : {len(corrected)}")
    print(f"corrected: {len(applied)} of {len(doc['disputes'])} recorded disputes")
    print(f"written  : {out_path}")
    for d in applied:
        print(f"  {d['noun_phrase']:22} {d['plurality']} {d['case']:11} "
              f"{d['dataset_gold']} -> {d['eki_forms']}   [{d['rule']}]")
    if stale:
        print(f"\n!! {len(stale)} dispute(s) no longer match the dataset and were NOT applied:")
        for d in stale:
            print(f"  {d['noun_phrase']} {d['plurality']} {d['case']} "
                  f"(recorded gold {d['dataset_gold']})")
        print("   Re-audit data/inflection_et_eki_disputes.json before quoting this file.")

    print("\nThe output is a derivative of a dataset that carries no licence, so it is")
    print("written locally and is not redistributed by this repository. Cite the")
    print(f"original: {doc['dataset']}. The corrections and their EKI citations are in")
    print(f"{DISPUTES_PATH.relative_to(_ROOT)}.")


if __name__ == "__main__":
    main()
