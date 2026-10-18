#!/usr/bin/env python3
"""Write producer_derive_status.json: the producer's derived status, as a truth table.

New code (docs/ATLAS.md, section 3, check 6). The atlas builder recomputes each passage's
derived status with its own port of formal-companion's `cflib.vocab.derive_status`
(scripts/atlas/build_store.py, `derive_status`). This script runs the producer's function
itself, from a formal-companion checkout, over every one-location case and over a fixed
sample of two- and three-location cases, and writes the answers, so
test_build_store.py can check the port against them without the producer installed.

    python3 scripts/atlas/fixtures/make_producer_table.py --producer <formal-companion checkout>

Rerun it when the producer's rules change, then fix build_store.derive_status until the
parity test passes. Each case is [kind, [[relation, proof_status, statement], ...], answer],
where answer is the status the producer stamps, null for no status (unresolved), or
"refused" when the producer raises (a statement flag on a proved declaration). A coverage
case (the maintainer's decision D1, for a producer whose derive_status takes a coverage
claim) has a fourth element, {"parts": [indexes into the locations], "challenge": <value>}:
the claim {complete: true, parts: those locations' keys, challenge}. Its parts are always
hasPart locations, so the producer never refuses one.

The producer's scripts/cflib must have no uncommitted change: the table names the commit
its answers came from.
"""
from __future__ import annotations

import argparse
import inspect
import json
import random
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "producer_derive_status.json"
KINDS = ["definition", "theorem", "conjecture", "example", "construction", "notation", "other"]
RELATIONS = ["exactMatch", "closeMatch", "broadMatch", "narrowMatch", "hasPart", "relatedMatch"]
PROOF_STATUSES = ["proved", "statement_only", "axiom_dependent", "wanted", "definition"]
SAMPLE_SEED = 20261017
SAMPLE_SIZE = 300
# Coverage cases (decision D1): every kind x every pair of valid part ends under a survived
# claim, the pairs of four representative ends (proved, data, statement_only, a Prop-valued
# definition: finished and unfinished, as the round-three reading orders them) under each other
# challenge value for three kinds, and a fixed sample of claims with three parts or with other
# locations beside them.
COVERAGE_SEED = 20261018
COVERAGE_SAMPLE_SIZE = 200
OTHER_CHALLENGES = ["downgraded", "not_required", "sampled_out", "challenge_pending"]


def answer(vocab, kind: str, locations: list[list], coverage: dict | None = None) -> str | None:
    rows = [{"relation": r, "proof_status": p, "statement": s, "location_key": f"key{i}"}
            for i, (r, p, s) in enumerate(locations)]
    try:
        if coverage is None:
            return vocab.derive_status(kind, rows, False)
        claim = {"complete": True, "parts": [f"key{i}" for i in coverage["parts"]],
                 "challenge": coverage["challenge"]}
        return vocab.derive_status(kind, rows, False, claim)
    except vocab.CfError:
        return "refused"


def coverage_cases(vocab, valid: list[list]) -> list[list]:
    """Cases with a coverage claim over hasPart locations, each answered by the producer."""
    part_ends = [["hasPart", p, s] for _, p, s in valid if _ == "exactMatch"]  # one per (proof, flag)
    pairs = [[a, b] for a in part_ends for b in part_ends]
    out = []
    for kind in KINDS:
        for pair in pairs:
            claim = {"parts": [0, 1], "challenge": "survived"}
            out.append([kind, [list(e) for e in pair], answer(vocab, kind, pair, claim), claim])
    # Four representative ends (two finished, two unfinished), under every other challenge value.
    upgradable = [end for end in part_ends if end[1:] in (["proved", False], ["definition", False],
                                                           ["statement_only", False], ["definition", True])]
    for kind in ("theorem", "definition", "example"):
        for challenge in OTHER_CHALLENGES:
            for pair in ([a, b] for a in upgradable for b in upgradable):
                claim = {"parts": [0, 1], "challenge": challenge}
                out.append([kind, [list(e) for e in pair], answer(vocab, kind, pair, claim), claim])
    rng = random.Random(COVERAGE_SEED)
    for _ in range(COVERAGE_SAMPLE_SIZE):
        kind = rng.choice(KINDS)
        n_parts = rng.choice((2, 3))
        parts = [list(rng.choice(part_ends)) for _ in range(n_parts)]
        others = [list(end) for end in rng.sample(valid, rng.choice((0, 1, 2)))]
        locations = others + parts if rng.random() < 0.5 else parts + others
        start = 0 if locations[0] is parts[0] else len(others)
        claim = {"parts": list(range(start, start + n_parts)), "challenge": rng.choice(["survived", "survived",
                                                                                       *OTHER_CHALLENGES])}
        rng.shuffle(claim["parts"])
        out.append([kind, locations, answer(vocab, kind, locations, claim), claim])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--producer", type=Path, required=True, help="formal-companion checkout")
    args = parser.parse_args()
    sys.path.insert(0, str(args.producer / "scripts"))
    from cflib import vocab  # noqa: PLC0415 - the producer's module, from --producer

    commit = subprocess.run(["git", "-C", str(args.producer), "rev-parse", "HEAD"], check=True,
                            capture_output=True, text=True).stdout.strip()
    changed = subprocess.run(["git", "-C", str(args.producer), "status", "--porcelain", "--", "scripts/cflib"],
                             check=True, capture_output=True, text=True).stdout.strip()
    if changed:
        print(f"the producer's scripts/cflib has uncommitted changes, so its answers are not those of "
              f"{commit[:12]}:\n{changed}", file=sys.stderr)
        return 2
    ends = [[r, p, s] for r in RELATIONS for p in PROOF_STATUSES for s in (False, True)]
    single = [[kind, [end], answer(vocab, kind, [end])] for kind in KINDS for end in ends]
    # Multi-location cases: a fixed sample, without the refused flag (a single case covers it).
    valid = [end for end in ends if not (end[2] and end[1] == "proved")]
    rng = random.Random(SAMPLE_SEED)
    multi = []
    for _ in range(SAMPLE_SIZE):
        kind = rng.choice(KINDS)
        locations = [list(end) for end in rng.sample(valid, rng.choice((2, 3)))]
        multi.append([kind, locations, answer(vocab, kind, locations)])
    covered = (coverage_cases(vocab, valid)
               if "coverage" in inspect.signature(vocab.derive_status).parameters else [])
    table = {
        "about": "cflib.vocab.derive_status(kind, locations, absent=False[, coverage]), generated by "
                 "scripts/atlas/fixtures/make_producer_table.py",
        "producer": {"repository": "formal-companion", "commit": commit, "function": "scripts/cflib/vocab.py derive_status"},
        "cases": single + multi + covered,
    }
    lines = ",\n".join(json.dumps(case, separators=(",", ":")) for case in table["cases"])
    head = json.dumps({k: v for k, v in table.items() if k != "cases"}, indent=1, sort_keys=True)[:-2]
    OUT.write_text(f'{head},\n "cases": [\n{lines}\n ]\n}}\n', encoding="utf-8")
    json.loads(OUT.read_text(encoding="utf-8"))
    print(f"wrote {len(table['cases'])} cases from formal-companion {commit[:12]} to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
