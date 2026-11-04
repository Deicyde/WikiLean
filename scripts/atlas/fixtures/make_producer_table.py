#!/usr/bin/env python3
"""Write producer_derive_status.json: the producer's derived status, as a truth table.

New code (docs/ATLAS.md, section 3, check 6). The atlas builder recomputes each passage's
derived status with its own port of formal-companion's `cflib.vocab.derive_status`
(scripts/atlas/build_store.py, `derive_status`). This script runs the producer's function
itself, from a formal-companion checkout, over every one-location case and over a fixed
sample of two- and three-location cases, and writes the answers, so
test_build_store.py can check the port against them without the producer installed.

    python3 scripts/atlas/fixtures/make_producer_table.py --producer <formal-companion checkout>
    python3 scripts/atlas/fixtures/make_producer_table.py --producer <checkout> --status-rule 3

Rerun it when the producer's rules change, then fix build_store.derive_status until the
parity test passes. Each case is [kind, [[relation, proof_status, statement], ...], answer],
where answer is the status the producer stamps, null for no status (unresolved), or
"refused" when the producer raises (a statement flag on a proved declaration). A coverage
case (the maintainer's decision D1, for a producer whose derive_status takes a coverage
claim) has a fourth element, {"parts": [indexes into the locations], "challenge": <value>}:
the claim {complete: true, parts: those locations' keys, challenge}. Its parts are always
hasPart locations, so the producer never refuses one.

--status-rule 2 or 3 (a producer whose derive_status takes the keyword: formal-companion
d85385a and later) writes producer_derive_status-<rule>.json instead, every answer derived by
that rule: a coverage case's claim is then a bundle and carries its "relation" too (one of
BUNDLE_RELATIONS, every survived pair under each of them), and the table names the rule.

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
# A bundle's relations under status rules 2 and 3 (cflib.vocab BUNDLE_RELATIONS), sorted.
BUNDLE_RELATIONS = ["broadMatch", "closeMatch", "exactMatch"]


def answer(vocab, kind: str, locations: list[list], coverage: dict | None = None,
           status_rule: int = 1) -> str | None:
    rows = [{"relation": r, "proof_status": p, "statement": s, "location_key": f"key{i}"}
            for i, (r, p, s) in enumerate(locations)]
    # Status rule 1 is the producer's three- and four-argument call, as before the keyword existed.
    rule = {} if status_rule == 1 else {"status_rule": status_rule}
    try:
        if coverage is None:
            return vocab.derive_status(kind, rows, False, **rule)
        claim = {"complete": True, "parts": [f"key{i}" for i in coverage["parts"]],
                 "challenge": coverage["challenge"]}
        if "relation" in coverage:
            claim["relation"] = coverage["relation"]
        return vocab.derive_status(kind, rows, False, claim, **rule)
    except vocab.CfError:
        return "refused"


def coverage_cases(vocab, valid: list[list], status_rule: int = 1) -> list[list]:
    """Cases with a coverage claim over hasPart locations, each answered by the producer.

    Under status rules 2 and 3 each claim is a bundle with a relation: every survived pair under
    each of BUNDLE_RELATIONS, the other challenge values under each, and a sampled one for the rest.
    """
    relations = BUNDLE_RELATIONS if status_rule >= 2 else [None]

    def claim_of(parts: list[int], challenge: str, relation: str | None) -> dict:
        return {"parts": parts, "challenge": challenge, **({"relation": relation} if relation else {})}

    part_ends = [["hasPart", p, s] for _, p, s in valid if _ == "exactMatch"]  # one per (proof, flag)
    pairs = [[a, b] for a in part_ends for b in part_ends]
    out = []
    for kind in KINDS:
        for relation in relations:
            for pair in pairs:
                claim = claim_of([0, 1], "survived", relation)
                out.append([kind, [list(e) for e in pair], answer(vocab, kind, pair, claim, status_rule), claim])
    # Four representative ends (two finished, two unfinished), under every other challenge value.
    upgradable = [end for end in part_ends if end[1:] in (["proved", False], ["definition", False],
                                                           ["statement_only", False], ["definition", True])]
    for kind in ("theorem", "definition", "example"):
        for challenge in OTHER_CHALLENGES:
            for relation in relations:
                for pair in ([a, b] for a in upgradable for b in upgradable):
                    claim = claim_of([0, 1], challenge, relation)
                    out.append([kind, [list(e) for e in pair], answer(vocab, kind, pair, claim, status_rule), claim])
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
        if status_rule >= 2:
            claim["relation"] = rng.choice(BUNDLE_RELATIONS)
        out.append([kind, locations, answer(vocab, kind, locations, claim, status_rule), claim])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--producer", type=Path, required=True, help="formal-companion checkout")
    parser.add_argument("--status-rule", type=int, choices=(1, 2, 3), default=1,
                        help="the derivation to answer by (default 1; 2 and 3 write producer_derive_status-<rule>.json)")
    args = parser.parse_args()
    rule = args.status_rule
    out = OUT if rule == 1 else HERE / f"producer_derive_status-{rule}.json"
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
    single = [[kind, [end], answer(vocab, kind, [end], status_rule=rule)] for kind in KINDS for end in ends]
    # Multi-location cases: a fixed sample, without the refused flag (a single case covers it).
    valid = [end for end in ends if not (end[2] and end[1] == "proved")]
    rng = random.Random(SAMPLE_SEED)
    multi = []
    for _ in range(SAMPLE_SIZE):
        kind = rng.choice(KINDS)
        locations = [list(end) for end in rng.sample(valid, rng.choice((2, 3)))]
        multi.append([kind, locations, answer(vocab, kind, locations, status_rule=rule)])
    parameters = inspect.signature(vocab.derive_status).parameters
    if rule != 1 and "status_rule" not in parameters:
        print(f"formal-companion {commit[:12]}'s derive_status takes no status_rule", file=sys.stderr)
        return 2
    covered = coverage_cases(vocab, valid, rule) if "coverage" in parameters else []
    table = {
        "about": "cflib.vocab.derive_status(kind, locations, absent=False[, coverage]"
                 + ("" if rule == 1 else f", status_rule={rule}") + "), generated by "
                 "scripts/atlas/fixtures/make_producer_table.py",
        "producer": {"repository": "formal-companion", "commit": commit, "function": "scripts/cflib/vocab.py derive_status"},
        **({} if rule == 1 else {"status_rule": rule}),
        "cases": single + multi + covered,
    }
    lines = ",\n".join(json.dumps(case, separators=(",", ":")) for case in table["cases"])
    head = json.dumps({k: v for k, v in table.items() if k != "cases"}, indent=1, sort_keys=True)[:-2]
    out.write_text(f'{head},\n "cases": [\n{lines}\n ]\n}}\n', encoding="utf-8")
    json.loads(out.read_text(encoding="utf-8"))
    print(f"wrote {len(table['cases'])} cases from formal-companion {commit[:12]} to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
