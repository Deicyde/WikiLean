#!/usr/bin/env python3
"""BRAIN acceptance gate — the 5 datapoints of brain/SCHEMA.md §Acceptance + invariants.

Runs against brain/data/{nodes,edges}.jsonl and exits 0 only if every check
passes (plain python3, no pytest — usable as a CI gate). Beyond the 5
datapoints it enforces the schema laws: every edge carries
kind/provenance/confidence; every provenance.source is a key in
catalog/data/source_registry.json (the single source of truth for provenance);
every `formalizes` dst resolves to a node in nodes.jsonl — build_edges only
emits decls that passed the existence oracle, so at test time nodes.jsonl
membership IS the oracle proxy (re-running the oracle here would just re-test
the builder); every `contains` edge joins two existing nodes; node ids are
unique.

P3 (insphere multi-QID) and P4 (Q217413 container link) depend on the agent
discovery workflow's verified fold-in; until that lands they fail with a
distinct PENDING DISCOVERY tag. Pending is still not passing — exit stays 1.

    python3 brain/test_acceptance.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = HERE / "data"
NODES = DATA / "nodes.jsonl"
EDGES = DATA / "edges.jsonl"
REGISTRY = ROOT / "catalog" / "data" / "source_registry.json"

MAX_EXAMPLES = 5  # cap per-failure example spam

# Datapoint targets (SCHEMA.md §Acceptance)
ABELIAN = "Q181296"
COMM_GROUP = "decl:Mathlib:CommGroup"
# Accept the canonical encodings of "the LMFDB knowl group.abelian" — the
# registry key is lmfdb_knowl, the SCHEMA prose says lmfdb:group.abelian.
LMFDB_ABELIAN = re.compile(r"^(?:xref:|obj:)?lmfdb(?:_knowl)?:group\.abelian$")
MODULE_DECL = "decl:Mathlib:Module"
MODULE_QIDS = {"Q18848", "Q125977"}
INSPHERE_DECL = "decl:Mathlib:Affine.Simplex.insphere"
INSPHERE_QIDS = {"Q354337", "Q683362"}
CAT_THEORY = "Q217413"
CAT_CONTAINER = "path:Mathlib/CategoryTheory"
FIELD_P31 = "Q1936384"  # "area of mathematics"

PENDING_DISCOVERY = {"P3", "P4"}


def registry_source_keys() -> set[str]:
    """All valid provenance.source values: the spine key plus every entry key
    of every *_sources-style section (nested dict-of-dicts)."""
    reg = json.loads(REGISTRY.read_text())
    keys: set[str] = set()
    for section, val in reg.items():
        if not isinstance(val, dict):
            continue
        if section == "spine":
            if isinstance(val.get("key"), str):
                keys.add(val["key"])
            continue
        for k, v in val.items():
            if isinstance(v, dict) and ("name" in v or "kind" in v):
                keys.add(k)
    return keys


def iter_jsonl(path: Path, require_key: str):
    """Stream records, skipping blank lines and _meta/attribution rows
    (rows lacking the identity key)."""
    with path.open() as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                raise SystemExit(f"FATAL: {path.name}:{lineno} is not valid JSON")
            if isinstance(rec, dict) and require_key in rec:
                yield rec


def main() -> int:
    missing = [p for p in (NODES, EDGES, REGISTRY) if not p.exists()]
    if missing:
        print("BRAIN acceptance: FAIL — required artifacts missing:")
        for p in missing:
            print(f"  - {p.relative_to(ROOT)}")
        print("(run brain/build_nodes.py + brain/build_edges.py first)")
        return 1

    valid_sources = registry_source_keys()

    # ---- nodes pass: id set, duplicates, targeted payloads -------------------
    node_ids: set[str] = set()
    dup_ids: Counter[str] = Counter()
    n_nodes = 0
    cat_node = None
    for rec in iter_jsonl(NODES, "id"):
        n_nodes += 1
        nid = rec["id"]
        if nid in node_ids:
            dup_ids[nid] += 1
        node_ids.add(nid)
        if nid == CAT_THEORY:
            cat_node = rec

    # ---- edges pass: invariants + targeted captures, one streaming pass ------
    n_edges = 0
    bad_shape: list[str] = []          # missing kind/provenance/confidence
    n_bad_shape = 0
    unknown_sources: Counter[str] = Counter()
    fz_bad_dst: list[str] = []         # formalizes dst not a known node / bad prefix
    n_fz_bad_dst = 0
    contains_bad: list[str] = []
    n_contains_bad = 0

    abelian_fz: set[str] = set()       # formalizes dsts of Q181296
    abelian_xref: set[str] = set()     # xref dsts of Q181296
    module_in: set[str] = set()        # formalizes srcs into Module
    insphere_in: set[str] = set()      # formalizes srcs into insphere
    cat_fz: set[str] = set()           # formalizes dsts of Q217413

    for rec in iter_jsonl(EDGES, "src"):
        n_edges += 1
        src, dst, kind = rec["src"], rec.get("dst", ""), rec.get("kind")
        prov = rec.get("provenance")
        if not kind or not isinstance(prov, dict) or not prov.get("source") \
                or not rec.get("confidence"):
            n_bad_shape += 1
            if len(bad_shape) < MAX_EXAMPLES:
                bad_shape.append(f"{src} -{kind}-> {dst}")
        elif prov["source"] not in valid_sources:
            unknown_sources[prov["source"]] += 1

        if kind == "formalizes":
            if not (dst.startswith("decl:") or dst.startswith("path:")) \
                    or dst not in node_ids:
                n_fz_bad_dst += 1
                if len(fz_bad_dst) < MAX_EXAMPLES:
                    fz_bad_dst.append(f"{src} -> {dst}")
            if src == ABELIAN:
                abelian_fz.add(dst)
            elif src == CAT_THEORY:
                cat_fz.add(dst)
            if dst == MODULE_DECL:
                module_in.add(src)
            elif dst == INSPHERE_DECL:
                insphere_in.add(src)
        elif kind == "contains":
            if src not in node_ids or dst not in node_ids:
                n_contains_bad += 1
                if len(contains_bad) < MAX_EXAMPLES:
                    contains_bad.append(f"{src} -> {dst}")
        elif kind == "xref" and src == ABELIAN:
            abelian_xref.add(dst)

    # ---- checks ---------------------------------------------------------------
    # (id, description, ok, details)
    checks: list[tuple[str, str, bool, list[str]]] = []

    has_lmfdb = any(LMFDB_ABELIAN.match(d) for d in abelian_xref)
    has_cg = COMM_GROUP in abelian_fz
    checks.append((
        "P1", f"{ABELIAN} abelian group: xref->lmfdb:group.abelian + formalizes->{COMM_GROUP}",
        has_lmfdb and has_cg,
        ([] if has_lmfdb else [f"no lmfdb group.abelian xref (xrefs seen: {sorted(abelian_xref)[:MAX_EXAMPLES]})"])
        + ([] if has_cg else [f"no formalizes->{COMM_GROUP} (has: {sorted(abelian_fz)[:MAX_EXAMPLES]})"]),
    ))

    mod_missing = MODULE_QIDS - module_in
    checks.append((
        "P2", f"{MODULE_DECL} has >=2 inbound formalizes incl. Q18848+Q125977",
        len(module_in) >= 2 and not mod_missing,
        [f"inbound={sorted(module_in)}, missing={sorted(mod_missing)}"] if (len(module_in) < 2 or mod_missing) else [],
    ))

    ins_missing = INSPHERE_QIDS - insphere_in
    checks.append((
        "P3", f"{INSPHERE_DECL} has >=2 inbound formalizes incl. Q354337+Q683362",
        len(insphere_in) >= 2 and not ins_missing,
        [f"inbound={sorted(insphere_in)}, missing={sorted(ins_missing)}"] if (len(insphere_in) < 2 or ins_missing) else [],
    ))

    has_container = CAT_CONTAINER in cat_fz
    p31 = (cat_node or {}).get("altitude_evidence", {}).get("p31", [])
    has_p31 = FIELD_P31 in p31
    checks.append((
        "P4", f"{CAT_THEORY} category theory: formalizes->{CAT_CONTAINER} + P31 {FIELD_P31}",
        has_container and has_p31,
        ([] if has_container else [f"no formalizes->{CAT_CONTAINER} (has: {sorted(cat_fz)[:MAX_EXAMPLES]})"])
        + ([] if has_p31 else [f"altitude_evidence.p31={p31}"
                               + ("" if cat_node else f" (node {CAT_THEORY} missing)")]),
    ))

    checks.append((
        "P5a", "every edge has kind + provenance.source + confidence",
        n_bad_shape == 0,
        [f"{n_bad_shape} malformed edges, e.g. {bad_shape}"] if n_bad_shape else [],
    ))
    checks.append((
        "P5b", "every provenance.source is a source_registry.json key",
        not unknown_sources,
        [f"unknown source '{s}' on {c} edges" for s, c in unknown_sources.most_common(MAX_EXAMPLES)],
    ))
    checks.append((
        "P5c", "every formalizes dst is an existing decl:/path: node (oracle proxy)",
        n_fz_bad_dst == 0,
        [f"{n_fz_bad_dst} bad formalizes dsts, e.g. {fz_bad_dst}"] if n_fz_bad_dst else [],
    ))
    checks.append((
        "P5d", "every contains edge joins two existing nodes",
        n_contains_bad == 0,
        [f"{n_contains_bad} dangling contains edges, e.g. {contains_bad}"] if n_contains_bad else [],
    ))
    checks.append((
        "P5e", "no duplicate node ids",
        not dup_ids,
        [f"'{i}' appears {c + 1}x" for i, c in dup_ids.most_common(MAX_EXAMPLES)],
    ))

    # Rollups carry provenance at artifact level (_meta first line) by design —
    # per-row provenance on 287MB of weights would say the same thing 3M times.
    rollup_bad: list[str] = []
    rollups = sorted(DATA.glob("rollup_edges.*.jsonl"))
    for rp in rollups:
        with rp.open() as f:
            first = json.loads(f.readline() or "{}")
        meta = first.get("_meta") if isinstance(first, dict) else None
        if not isinstance(meta, dict) or meta.get("provenance_source") not in valid_sources:
            rollup_bad.append(f"{rp.name}: _meta.provenance_source="
                              f"{(meta or {}).get('provenance_source')!r}")
    checks.append((
        "P5f", f"rollup artifacts ({len(rollups)}) carry a registry-keyed _meta provenance",
        not rollup_bad,
        rollup_bad[:MAX_EXAMPLES],
    ))

    # ---- report ---------------------------------------------------------------
    print(f"BRAIN acceptance — {n_nodes:,} nodes, {n_edges:,} edges "
          f"({len(valid_sources)} registry source keys)\n")
    n_pass = n_hard = n_pending = 0
    for cid, desc, ok, details in checks:
        if ok:
            tag, n_pass = "PASS", n_pass + 1
        elif cid in PENDING_DISCOVERY:
            tag, n_pending = "FAIL PENDING DISCOVERY", n_pending + 1
        else:
            tag, n_hard = "FAIL", n_hard + 1
        print(f"[{tag}] {cid}  {desc}")
        for d in details:
            print(f"        - {d}")

    print(f"\n{n_pass}/{len(checks)} passed"
          f" ({n_hard} hard failures, {n_pending} pending-discovery)")
    if n_hard == 0 and n_pending:
        print("pending-discovery checks land with the discovery fold-in; "
              "exit stays 1 until they pass.")
    return 0 if n_pass == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
