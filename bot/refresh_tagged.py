#!/usr/bin/env python3
"""Refresh bot/data/tagged_in_master.txt from the LIVE upstream Mathlib master.

Greps every `@[wikidata Q…]` already on leanprover-community/mathlib4:master so
the pool selector never re-proposes a tag that's already merged. Deterministic.

Needs a local mathlib4 git checkout (any branch) to fetch into.
  refresh_tagged.py --mathlib ~/mathlib4 [--dry-run]
"""
import argparse, re, subprocess, sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "data" / "tagged_in_master.txt"
PAIRS_OUT = Path(__file__).resolve().parent / "data" / "tagged_pairs.txt"
UPSTREAM = "https://github.com/leanprover-community/mathlib4"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mathlib", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    g = ["git", "-C", str(args.mathlib)]

    print(f"fetching {UPSTREAM} master (shallow)…")
    f = subprocess.run(g + ["fetch", "--depth=1", UPSTREAM, "master"], text=True, capture_output=True)
    if f.returncode != 0:
        sys.exit(f"git fetch failed: {f.stderr[:300]}")
    # Exclude the attribute's own file: its docstring example (`@[wikidata Q12345
    # "Optional comment"]`) is not a tag, and counting it silently blocks that
    # QID from ever being proposed.
    grep = subprocess.run(g + ["grep", "-IE", r"wikidata[[:space:]]+Q[0-9]+", "FETCH_HEAD", "--",
                               "Mathlib/", ":(exclude)Mathlib/Tactic/CrossRefAttribute.lean"],
                          text=True, capture_output=True)
    # FAIL CLOSED: both outputs are exclusion lists the dedupe layers depend on
    # (pool fresh-fill, open_batch.assemble requeue dedupe, apply_corrections
    # recovery skip). git grep rc=1 means ZERO tags on master — master carries
    # 100+, so an empty result is an error, not a state; never overwrite the
    # lists with it.
    if grep.returncode != 0 or not grep.stdout.strip():
        sys.exit(f"git grep for @[wikidata] tags failed or found nothing "
                 f"(rc={grep.returncode}): {grep.stderr[:300]} — refusing to overwrite "
                 f"{OUT.name}/{PAIRS_OUT.name} with an empty exclusion list.")
    # Lines look like `FETCH_HEAD:Mathlib/Foo/Bar.lean:@[wikidata Q123]`. Keep both
    # granularities: the QID set (pool exclusion) and (qid, file) pairs (requeue
    # dedupe in open_batch.assemble — per-file so a same-QID second-decl requeue
    # in another file still goes through).
    qids, pairs = set(), set()
    for line in grep.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        f = parts[1]
        for q in re.findall(r"wikidata\s+(Q\d+)", parts[2]):
            qids.add(q)
            pairs.add((q, f))
    qids = sorted(qids, key=lambda q: int(q[1:]))
    pairs = sorted(pairs, key=lambda p: (int(p[0][1:]), p[1]))
    print(f"found {len(qids)} tagged QIDs on master, {len(pairs)} (qid, file) pairs "
          f"(was {len(OUT.read_text().split()) if OUT.exists() else 0})")
    if args.dry_run:
        print("[dry-run] not writing.", " ".join(qids[:12]), "…")
        return
    OUT.write_text("\n".join(qids) + "\n")
    PAIRS_OUT.write_text("\n".join(f"{q}\t{f}" for q, f in pairs) + "\n")
    print(f"wrote {OUT} and {PAIRS_OUT}")


if __name__ == "__main__":
    main()
