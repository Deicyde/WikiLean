#!/usr/bin/env python3
"""Assemble the rebuilt concept graph (v2) from the agent grounding.

Reads the parallel-agent grounding (verified Mathlib formalizations per concept),
applies curated point-fixes from grounding_overrides.jsonl (the grounding file is
the immutable audit trail — never edited), applies a DETERMINISTIC decl-existence
backstop (the agents proposed + a skeptic verified; here the oracle/checkout has
the final say — anti-slop), builds the upgraded node set (multi-library
formalizations; primary_decl = best; xrefs joined by QID from
wikidata_crossrefs.json — the agent-echoed xrefs are dead, per brain/SCHEMA.md),
lifts the FULL formal dependency graph to QID→QID edges via lift_formal_edges,
folds in the Wikidata relation edges, and writes concept_graph_v2.json +
decl_to_qid_v2.json + decl_qid_roles_v2.json (formalization vs citation — the two
roles must never be conflated) + a diff vs the live graph. Does NOT touch the
canonical graph — this is a reviewable artifact.

Usage: python3 catalog/build_graph_v2.py --grounding catalog/data/rebuild_grounding.json
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
LIVE = DATA / "concept_graph.json"
ORACLE = HERE.parent / ".claude" / "skills" / "mathlib-search" / ".cache" / "declaration-data.json"
CHECKOUT = Path(os.environ.get("BRAIN_MATHLIB_CHECKOUT",
                               "/Users/jack/Desktop/LEAN/mathlib4/Mathlib"))
WD_EDGES = HERE / "mathlib_deps" / "wikidata_edges.jsonl"
ANNOT = HERE.parent / "site" / "annotations"
OUT = DATA / "concept_graph_v2.json"
D2Q_OUT = DATA / "decl_to_qid_v2.json"
ROLES_OUT = DATA / "decl_qid_roles_v2.json"
XREFS = DATA / "wikidata_crossrefs.json"
OVERRIDES = DATA / "grounding_overrides.jsonl"

sys.path.insert(0, str(HERE))
import lift_formal_edges  # noqa: E402


def oracle_names() -> set[str]:
    try:
        d = json.loads(ORACLE.read_text())
        return set(d.get("declarations", {}))
    except (OSError, json.JSONDecodeError):
        return set()


def checkout_has(decls: list[str]) -> set[str]:
    """Backstop for oracle gaps: grep the live checkout for each decl's defining
    line (last-segment match on a decl keyword). Returns those found."""
    found: set[str] = set()
    if not CHECKOUT.exists():
        return found
    kw = r"(theorem|lemma|def|abbrev|structure|class|instance|inductive)"
    for d in decls:
        seg = d.split(".")[-1]
        # NB: a trailing \b never matches a decl ending in a prime (smulAux',
        # Stream') — ' is a non-word char, so require end-or-non-identifier instead.
        # Decls are often DECLARED under a dotted name (`theorem
        # TendstoInDistribution.prodMk_of_tendstoInMeasure_const`), so allow an
        # optional dotted namespace prefix between the keyword and the segment —
        # requiring the keyword immediately before the segment cost Q643826.
        pat = f"{kw} +([A-Za-z0-9_'.«»]+\\.)?{re.escape(seg)}($|[^A-Za-z0-9_'])"
        try:
            r = subprocess.run(["grep", "-rIlE", pat, str(CHECKOUT)],
                               capture_output=True, text=True, timeout=30)
            if r.stdout.strip():
                found.add(d)
        except (subprocess.SubprocessError, OSError):
            pass
    return found


def load_overrides() -> dict[str, dict]:
    """Curated point-fixes applied AFTER loading the grounding (which stays an
    immutable audit trail — never edit it). One JSONL row per fix:
    {"qid": ..., "set": {"status": <s> | "match_kind:<decl>": <k>}, "reason": ...};
    rows for the same QID merge, later rows win. Returns {qid: merged set-map}."""
    out: dict[str, dict] = {}
    if not OVERRIDES.exists():
        return out
    for line in OVERRIDES.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            out.setdefault(r["qid"], {}).update(r.get("set") or {})
    return out


def apply_match_kind_overrides(forms: list[dict], ov: dict) -> bool:
    """Apply "match_kind:<decl>" set-keys to the matching formalization entries
    (in place). Returns True if any entry was touched."""
    hit = False
    for key, val in ov.items():
        if key.startswith("match_kind:"):
            decl = key.split(":", 1)[1]
            for f in forms:
                if f.get("decl") == decl:
                    f["match_kind"] = val
                    hit = True
    return hit


def derive_status(forms: list[dict], agent_status: str | None) -> str:
    """Node status from the (possibly override-corrected) formalization evidence,
    preserving the original deference to the agent's own status claim."""
    if not forms:
        return "not_formalized"
    status = "formalized" if any(f["match_kind"] == "exact" and f.get("confidence") == "high" for f in forms) \
        else "partial"
    # keep the agent's status if it's more conservative and forms exist
    if agent_status in ("formalized", "partial", "not_formalized"):
        status = agent_status if not (status == "formalized" and agent_status == "partial") else status
    return status


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text)
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grounding", type=Path, default=DATA / "rebuild_grounding.json")
    args = ap.parse_args()

    concepts = json.loads(args.grounding.read_text())
    if isinstance(concepts, dict):
        concepts = concepts.get("concepts", [])
    overrides = load_overrides()
    xrefs_by_qid: dict[str, dict] = {}
    if XREFS.exists():
        xrefs_by_qid = json.loads(XREFS.read_text()).get("xrefs", {})
    else:
        print(f"WARNING: {XREFS.name} missing — every node gets empty xrefs", file=sys.stderr)
    live = json.loads(LIVE.read_text())
    live_by_qid = {n["qid"]: n for n in live["nodes"]}
    live_edge_n = len(live["edges"])
    # Authoritative labels — the agents left `label` null for expansion nodes, so
    # resolve from the target list / universe, never fall back to a bare QID.
    tgt_label: dict[str, str] = {}
    tf = HERE.parent / "manage" / "data" / "rebuild_targets.json"
    if tf.exists():
        for r in json.loads(tf.read_text()):
            if r.get("label"):
                tgt_label[r["qid"]] = r["label"]

    def humanize(slug: str | None) -> str:
        return (slug or "").replace("_", " ") if slug else ""

    # ---- deterministic decl-existence filter --------------------------------
    proposed = {f["decl"] for c in concepts for f in (c.get("formalizations") or [])}
    okset = oracle_names()
    # An empty oracle must FAIL, not degrade: on a machine without the
    # gitignored cache (and/or the checkout) every formalization would be
    # silently "DROPPED (nonexistent)" and the build would exit 0 with a
    # formalization-free graph (2026-07-03 self-review, reproduced on a fresh
    # clone). Fetch the cache via the mathlib-search skill.
    if not okset:
        sys.exit(f"FATAL: decl oracle empty/missing at {ORACLE} — fetch it "
                 "(mathlib-search skill) before building")
    if not CHECKOUT.exists():
        print(f"WARNING: mathlib checkout missing at {CHECKOUT} "
              f"(override with BRAIN_MATHLIB_CHECKOUT) — oracle misses cannot "
              f"be rescued and will be dropped", file=sys.stderr)
    misses = sorted(proposed - okset)
    print(f"proposed decls: {len(proposed)} | in oracle: {len(proposed) - len(misses)} | "
          f"oracle-misses to checkout-verify: {len(misses)}", file=sys.stderr)
    rescued = checkout_has(misses) if misses else set()
    valid = okset | rescued
    dropped = [d for d in misses if d not in rescued]
    print(f"  rescued from checkout: {len(rescued)} | DROPPED (nonexistent): {len(dropped)}",
          file=sys.stderr)

    # ---- build v2 nodes ------------------------------------------------------
    nodes = []
    decl_to_qid: dict[str, list[str]] = collections.defaultdict(list)
    decl_qid_roles: dict[str, dict[str, str]] = {}
    ov_applied: set[str] = set()
    n_new = n_gained_decl = n_changed_decl = 0
    for c in concepts:
        qid = c["qid"]
        forms = [f for f in (c.get("formalizations") or []) if f["decl"] in valid]
        # regression-guard baseline (the Slutsky Q643826 class): status as it
        # would be with neither checkout rescues nor overrides.
        base_status = derive_status([f for f in forms if f["decl"] not in rescued], c.get("status"))
        ov = overrides.get(qid) or {}
        if apply_match_kind_overrides(forms, ov):
            ov_applied.add(qid)
        prev = live_by_qid.get(qid)
        primary = forms[0]["decl"] if forms else None
        module = forms[0].get("module") if forms else (prev or {}).get("module")
        status = derive_status(forms, c.get("status"))
        if "status" in ov:
            ov_applied.add(qid)
            status = ov["status"]
        if not forms:
            status = "not_formalized"  # zero oracle-valid decls never claim one
        if status != base_status:
            cause = "+".join(s for s, hit in (("override", bool(ov)),
                             ("checkout-rescue", any(f["decl"] in rescued for f in forms))) if hit) or "?"
            print(f"  STATUS GUARD: {qid} ({c.get('slug') or ''}) "
                  f"{base_status} -> {status} via {cause}", file=sys.stderr)
        xr = xrefs_by_qid.get(qid) or {}
        node = {
            "qid": qid,
            "label": c.get("label") or tgt_label.get(qid) or (prev or {}).get("label")
                     or humanize(c.get("slug")) or qid,
            "slug": c.get("slug") or (prev or {}).get("slug"),
            "primary_decl": primary,
            "module": module,
            "status": status,
            "importance": (prev or {}).get("importance") or "Mid",
            "formalizations": [{k: f.get(k) for k in ("decl", "module", "library", "match_kind", "confidence")}
                               for f in forms],
            "xrefs": xr,
            "xrefs_keys": sorted(xr),
            "arxiv": c.get("arxiv") or [],
            "is_new": qid not in live_by_qid,
        }
        nodes.append(node)
        for f in forms:
            decl_to_qid[f["decl"]].append(qid)
            decl_qid_roles.setdefault(f["decl"], {})[qid] = "formalization"
        if node["is_new"]:
            n_new += 1
        elif not prev.get("primary_decl") and primary:
            n_gained_decl += 1
        elif prev.get("primary_decl") and primary and prev["primary_decl"] != primary:
            n_changed_decl += 1

    node_qids = {n["qid"] for n in nodes}

    # Densify the EDGE decl→QID map with every decl an article CITES (mapped to
    # that article's concept QID). These are real, curated decl-usages; folding
    # them in lets the full dep-graph lift many more formally-grounded edges than
    # the grounding's primary decls alone. (Node formalizations stay the curated
    # high-quality set; this only feeds edge lifting.) grounding ∪ annotations.
    slug2qid = {n["slug"]: n["qid"] for n in nodes if n.get("slug")}
    import glob
    n_ann_decls = 0
    for f in glob.glob(str(ANNOT / "*.json")):
        if f.endswith(".agent1.json"):
            continue
        try:
            d = json.loads(Path(f).read_text())
        except (OSError, json.JSONDecodeError):
            continue
        qid = slug2qid.get(d.get("slug"))
        if not qid:
            continue
        for a in (d.get("annotations") or []):
            dec = (a.get("mathlib") or {}).get("decl")
            if dec:
                decl_to_qid[dec].append(qid)
                # formalization wins when a decl carries both roles for a QID
                decl_qid_roles.setdefault(dec, {}).setdefault(qid, "citation")
                n_ann_decls += 1
    decl_to_qid = {k: sorted(set(v)) for k, v in decl_to_qid.items()}
    print(f"  decl→QID map for edges: {len(decl_to_qid)} decls "
          f"(grounding + {n_ann_decls} annotation citations)", file=sys.stderr)
    write_atomic(D2Q_OUT, json.dumps(decl_to_qid, ensure_ascii=False))
    write_atomic(ROLES_OUT, json.dumps(decl_qid_roles, ensure_ascii=False, sort_keys=True))

    # ---- edges: full formal dep-graph lift + wikidata relations -------------
    formal_edges = lift_formal_edges.lift(decl_to_qid)
    formal_edges = [e for e in formal_edges if e["from"] in node_qids and e["to"] in node_qids]
    wd_edges = []
    if WD_EDGES.exists():
        seen = set()
        for line in WD_EDGES.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            s, o = r.get("s"), r.get("o")
            if s in node_qids and o in node_qids and s != o and (s, o) not in seen:
                seen.add((s, o))
                wd_edges.append({"from": s, "to": o, "source": "wikidata",
                                 "props": [{"p": r.get("p"), "label": r.get("p_label", "")}]})
    edges = formal_edges + wd_edges

    write_atomic(OUT, json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False))

    # ---- diff / stats --------------------------------------------------------
    n_formalized = sum(1 for n in nodes if n["status"] == "formalized")
    n_with_decl = sum(1 for n in nodes if n["primary_decl"])
    n_with_arxiv = sum(1 for n in nodes if n["arxiv"])
    live_formalized = sum(1 for n in live["nodes"] if n.get("status") == "formalized")
    live_with_decl = sum(1 for n in live["nodes"] if n.get("primary_decl"))
    print("\n===== concept_graph_v2 =====")
    print(f"nodes: {len(nodes)}  (was {len(live['nodes'])}; +{n_new} new)")
    print(f"with a formalization: {n_with_decl}  (was {live_with_decl})")
    print(f"status=formalized: {n_formalized}  (was {live_formalized})")
    print(f"edges: {len(edges)}  (was {live_edge_n})  [formal {len(formal_edges)} + wikidata {len(wd_edges)}]")
    print(f"decls newly grounded on existing nodes: +{n_gained_decl} | primary_decl changed: {n_changed_decl}")
    print(f"nodes with arXiv literature: {n_with_arxiv}")
    print(f"decl-existence: dropped {len(dropped)} nonexistent decl(s) as a backstop")
    print(f"grounding overrides: applied on {len(ov_applied)}/{len(overrides)} QIDs")
    unapplied = sorted(set(overrides) - ov_applied)
    if unapplied:
        print(f"  WARNING: override QIDs with nothing applied (absent from grounding, "
              f"or decl dropped?): {unapplied}", file=sys.stderr)
    n_role_f = sum(1 for m in decl_qid_roles.values() if "formalization" in m.values())
    print(f"decl roles: {len(decl_qid_roles)} decls ({n_role_f} with a formalization role)")
    print(f"\nwrote {OUT.name} ({OUT.stat().st_size/1024:.0f} KB) + {D2Q_OUT.name} + {ROLES_OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
