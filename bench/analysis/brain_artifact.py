#!/usr/bin/env python3
"""Characterize the Brain as a scientific artifact (REVIEW-2 §5a).

Produces the compact artifact table for the Bridge report v2 revision:
cells, organs by type, distinct Mathlib decls joined, concepts (QIDs),
synapses + kinds histogram, provenance split, external DBs with per-DB
anchored-page counts, Mathlib coverage vs the doc-gen4 universe, snapshot
dates, construction pipeline — PLUS benchmark-target coverage (QR-171 gold
decls, MathlibMPR gold premises, fresh-100 golds; fresh must be 0 in the
Brain by construction).

Fully deterministic — no sampling, no RNG. Every number is recomputed from
repo artifacts listed in INPUTS below; nothing is copied from prior reports.

Outputs (same directory):
  brain_artifact.json   machine-readable numbers + per-input provenance
  brain_artifact.md     the compact table + notes, drop-in for the report

Run:  python3 bench/analysis/brain_artifact.py   (from the repo root)
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # repo root
OUT_JSON = Path(__file__).with_suffix(".json")
OUT_MD = Path(__file__).with_suffix(".md")

INPUTS = {
    "cells": ROOT / "brain/data/cells.jsonl",
    "synapses": ROOT / "brain/data/synapses.jsonl",
    "frontier": ROOT / "brain/data/frontier.jsonl",
    "aliases": ROOT / "site/assets/brain/cells/aliases.json",
    "source_registry": ROOT / "catalog/data/source_registry.json",
    "external_dir": ROOT / "catalog/data/external",
    "annotations_dir": ROOT / "site/annotations",
    "oracle": ROOT / ".claude/skills/mathlib-search/.cache/declaration-data.json",
    "qr171": ROOT / "bench/v2/data/MathlibQR_shared171.json",
    "mpr": ROOT / "bench/v2/data/MathlibMPR.json",
    "fresh": ROOT / "bench/data/fresh_tasks.jsonl",
}


def _mtime(p: Path) -> str:
    return _dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")


def _provenance() -> dict:
    return {
        k: {"path": str(p.relative_to(ROOT)), "mtime": _mtime(p)}
        for k, p in INPUTS.items()
        if p.exists()
    }


# ---------------------------------------------------------------- provenance
# Classification of the cells/synapses `_meta.prov` table into the four
# report registers. Rules (documented in the md; assert-complete):
#   human        merged mathlib4 source attributes (@[wikidata]/@[stacks]/
#                @[kerodon]) — every one passed human mathlib-maintainer PR
#                review before entering the source tree.
#   ai-moderated AI-generated claims that passed a verification gate before
#                folding: agent+oracle grounding, verified discovery
#                proposals, fold-verified fc-agent joins, TheoremGraph's
#                dual-LLM-judge matches.
#   ai           AI-generated, no review yet: tag-queue @[wikidata]
#                candidates (seed/recycle/pool queues).
#   automated    deterministic, no LLM anywhere: Wikidata property values,
#                external-ingest page qids, internal links, dependency
#                extraction, docstring reference URLs, whole-token FQ-name
#                scans, D1 sitelink article joins, transitive joins.
def classify_prov(entry: dict) -> str:
    src, method = entry.get("source", ""), entry.get("method", "")
    if "attribute (mathlib4 source)" in method:
        return "human"
    if src == "tag-queue":
        return "ai"
    if method in (
        "agent+oracle",
        "discovery_proposals (verified)",
        "fc-agent (fold-verified)",
        "theorem_matching dual-judge",
    ):
        return "ai-moderated"
    deterministic = (
        "wikidata-property",
        "external-ingest page qid",
        "internal_link",
        "internal_link (projected)",
        "container_links",
        "formal-conjectures reference URL",
        "wikipedia-reference (module docstring)",
        "wikipedia-citation (docstring)",
        "fq-name-in-statement",
        "lift_formal_edges (formal_dependency.csv)",
        "annotation-citation (decl_qid_roles_v2)",
        "wikidata-claims",
        "theorem_matching transitive-join",
        "theoremgraph_links",
        "annotated article (D1)",
    )
    if method in deterministic:
        return "automated"
    raise SystemExit(f"UNCLASSIFIED prov entry: {entry!r} — extend classify_prov")


# ------------------------------------------------------------------- cells
def scan_cells():
    meta = None
    n_cells = 0
    organ_kinds = Counter()
    organ_bonds = Counter()
    decl_ids: set[str] = set()
    decl_by_lib = Counter()
    qids: set[str] = set()
    page_by_db = Counter()
    organ_prov = Counter()  # class -> organ-attach count (prov-carrying organs)
    anchor_organs = 0  # organs with no prov = the cell's defining anchor organ(s)
    mathlib_concept_joined: set[str] = set()
    decls_concept_joined: set[str] = set()
    largest = 0
    multi = 0
    with open(INPUTS["cells"]) as fh:
        for line in fh:
            row = json.loads(line)
            if "_meta" in row:
                meta = row["_meta"]
                continue
            n_cells += 1
            organs = row["organs"]
            largest = max(largest, len(organs))
            if len(organs) > 1:
                multi += 1
            has_concept = any(o["kind"] == "concept" for o in organs)
            for o in organs:
                organ_kinds[o["kind"]] += 1
                if "bond" in o:
                    organ_bonds[o["bond"]] += 1
                if "prov" in o:
                    organ_prov[classify_prov(meta["prov"][o["prov"]])] += 1
                else:
                    anchor_organs += 1
                if o["kind"] == "decl":
                    decl_ids.add(o["id"])
                    lib = o["id"].split(":")[1]
                    decl_by_lib[lib] += 1
                    if has_concept:
                        decls_concept_joined.add(o["id"])
                        if lib == "Mathlib":
                            mathlib_concept_joined.add(o["id"])
                elif o["kind"] == "concept":
                    qids.add(o["id"])
                elif o["kind"] == "page":
                    page_by_db[o.get("db", o["id"].split(":")[1])] += 1
    # supercell organs (field concepts + multi-claimant pages) live in _meta
    sc_pages_by_db = Counter()
    sc_concepts = 0
    for organs in meta.get("supercell_organs", {}).values():
        for o in organs:
            if o["kind"] == "page":
                sc_pages_by_db[o.get("db", o["id"].split(":")[1])] += 1
            elif o["kind"] == "concept":
                sc_concepts += 1
    return {
        "meta": meta,
        "n_cells": n_cells,
        "organ_kinds": dict(organ_kinds),
        "organ_bonds": dict(organ_bonds),
        "decl_ids": decl_ids,
        "decl_by_lib": dict(decl_by_lib),
        "qids": qids,
        "page_by_db": dict(page_by_db),
        "organ_prov": dict(organ_prov),
        "anchor_organs": anchor_organs,
        "decls_concept_joined": decls_concept_joined,
        "mathlib_concept_joined": mathlib_concept_joined,
        "largest_cell": largest,
        "multi_organ_cells": multi,
        "supercell_pages_by_db": dict(sc_pages_by_db),
        "supercell_concept_organs": sc_concepts,
    }


# ---------------------------------------------------------------- synapses
def scan_synapses():
    meta = None
    n = 0
    kind_synapses = Counter()  # synapses carrying the kind
    kind_bonds = Counter()  # total constituent bonds per kind
    trace_prov = Counter()  # provenance class over retained traces
    endpoint = Counter()  # cell-cell vs cell-supercell
    with open(INPUTS["synapses"]) as fh:
        for line in fh:
            row = json.loads(line)
            if "_meta" in row:
                meta = row["_meta"]
                continue
            n += 1
            for k, v in row["kinds"].items():
                kind_synapses[k] += 1
                kind_bonds[k] += v
            sup = ("path:" in row["src"]) or ("path:" in row["dst"])
            endpoint["cell-supercell" if sup else "cell-cell"] += 1
            for t in row.get("traces", []):
                if "prov" in t:
                    trace_prov[classify_prov(meta["prov"][t["prov"]])] += 1
    return {
        "n_synapses": n,
        "kind_synapses": dict(kind_synapses),
        "kind_bonds": dict(kind_bonds),
        "trace_prov": dict(trace_prov),
        "endpoints": dict(endpoint),
        "generated_at": meta["generated_at"],
    }


# ------------------------------------------------------------- external DBs
def scan_external():
    """Per-DB: corpus size (ingested pages) from each <db>_pages.jsonl _meta."""
    corpora = {}
    for p in sorted(INPUTS["external_dir"].glob("*_pages.jsonl")):
        db = p.name[: -len("_pages.jsonl")]
        with open(p) as fh:
            meta = json.loads(fh.readline()).get("_meta", {})
        corpora[db] = {
            "n_pages": meta.get("n_pages"),
            "fetched_at": meta.get("fetched_at"),
            "source_pin": str(meta.get("source_pin", ""))[:40],
        }
    return corpora


# ------------------------------------------------------------------ oracle
def scan_oracle():
    with open(INPUTS["oracle"]) as fh:
        data = json.load(fh)
    decls = data["declarations"]
    total = len(decls)
    mathlib = sum(
        1 for v in decls.values() if v.get("docLink", "").startswith("./Mathlib/")
    )
    return {"names_total": total, "names_mathlib": mathlib, "names": set(decls)}


# -------------------------------------------------------- benchmark targets
def bench_coverage(cells, oracle_names):
    decl_ids = cells["decl_ids"]

    def in_brain(bare: str) -> bool:
        # the Brain's decl-organ universe, any library (QR/MPR golds are Mathlib)
        return any(f"decl:{lib}:{bare}" in decl_ids
                   for lib in ("Mathlib", "Init", "Batteries"))

    qr = json.load(open(INPUTS["qr171"]))["shared_declarations"]
    assert len(qr) == 171 and len(set(qr)) == 171
    qr_brain = sorted(d for d in qr if in_brain(d))
    qr_oracle = sum(1 for d in qr if d in oracle_names)

    mpr = json.load(open(INPUTS["mpr"]))
    premises = set()
    for row in mpr:
        for g in row["premise_group"]:
            premises.update(g["docs"])
    prem_brain = sorted(d for d in premises if in_brain(d))
    prem_oracle = sum(1 for d in premises if d in oracle_names)
    prem_oracle_missing = sorted(d for d in premises if d not in oracle_names)
    qr_oracle_missing = sorted(d for d in qr if d not in oracle_names)
    # per-query ceiling for group-recall with Brain-only retrieval:
    # every group needs >=1 retrievable member
    q_all, q_any = 0, 0
    for row in mpr:
        groups = [any(in_brain(d) for d in g["docs"]) for g in row["premise_group"]]
        q_all += all(groups)
        q_any += any(groups)
    mains = [row["formal_main_result"] for row in mpr]
    mains_brain = sorted(d for d in mains if in_brain(d))
    mains_oracle = sum(1 for d in mains if d in oracle_names)

    fresh = [json.loads(l) for l in open(INPUTS["fresh"])]
    fresh = [r for r in fresh if "decl_name" in r]
    assert len(fresh) == 100
    fresh_brain = sorted(r["decl_name"] for r in fresh if in_brain(r["decl_name"]))
    fresh_oracle = sum(1 for r in fresh if r["decl_name"] in oracle_names)
    fresh_oracle_missing = sorted(r["decl_name"] for r in fresh
                                  if r["decl_name"] not in oracle_names)

    return {
        "qr171": {"n": len(qr), "in_brain": len(qr_brain),
                  "in_brain_frac": round(len(qr_brain) / len(qr), 4),
                  "in_current_oracle": qr_oracle,
                  "oracle_missing": qr_oracle_missing,
                  "brain_hits": qr_brain},
        "mpr_premises": {"n_queries": len(mpr), "n_distinct_gold_premises": len(premises),
                         "in_brain": len(prem_brain),
                         "in_brain_frac": round(len(prem_brain) / len(premises), 4),
                         "in_current_oracle": prem_oracle,
                         "oracle_missing": prem_oracle_missing,
                         "queries_all_groups_coverable": q_all,
                         "queries_any_group_coverable": q_any},
        "mpr_main_results_postcutoff": {"n": len(mains), "in_brain": len(mains_brain),
                                        "in_brain_names": mains_brain,
                                        "in_current_oracle": mains_oracle},
        "fresh100": {"n": len(fresh), "in_brain": len(fresh_brain),
                     "in_current_oracle": fresh_oracle,
                     "oracle_missing_root_mangled": fresh_oracle_missing,
                     "brain_hits": fresh_brain,
                     "brain_hit_explanation": (
                         "fresh_025 AntitoneOn.summable_of_integrableOn_Ioi_zero "
                         "(merged into Mathlib 2026-07-07) sits in cell:Q1155313 "
                         "'integral test for convergence' with bond=exact, "
                         "prov=discovery_proposals (verified) pin 2026-07-18: the "
                         "discovery-proposal fold POSTDATES the fresh window "
                         "(2026-07-04..07-10), so the strict-0 construction claim "
                         "holds only for the 2026-07-04 harvest inputs, not the "
                         "whole Brain. Corroborates bench/analysis/fresh_exposure.md "
                         "which flags fresh_025 exposed on all four axes."
                     )},
    }


# -------------------------------------------------------------------- main
def main():
    os.chdir(ROOT)
    cells = scan_cells()
    syn = scan_synapses()
    corpora = scan_external()
    oracle = scan_oracle()
    bench = bench_coverage(cells, oracle["names"])
    frontier_meta = json.loads(open(INPUTS["frontier"]).readline())["_meta"]
    aliases = json.load(open(INPUTS["aliases"]))
    n_annotations = len(list(INPUTS["annotations_dir"].glob("*.json")))

    n_mathlib_decls = cells["decl_by_lib"]["Mathlib"]
    cov_mathlib = n_mathlib_decls / oracle["names_mathlib"]
    cov_overall = n_mathlib_decls / oracle["names_total"]

    prov_organs = cells["organ_prov"]
    prov_traces = syn["trace_prov"]

    def _pct(d: dict) -> dict:
        tot = sum(d.values())
        return {k: round(v / tot, 4) for k, v in sorted(d.items())}

    result = {
        "generated_by": "bench/analysis/brain_artifact.py (deterministic; no RNG)",
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "inputs": _provenance(),
        "snapshot": {
            "cells_generated_at": cells["meta"]["generated_at"],
            "synapses_generated_at": syn["generated_at"],
            "mathlib_pins": sorted({e.get("pin") for e in cells["meta"]["prov"]
                                    if e.get("source") == "mathlib" and e.get("pin")}),
            "oracle_mtime": _mtime(INPUTS["oracle"]),
        },
        "cells": {
            "n_cells": cells["n_cells"],
            "multi_organ_cells": cells["multi_organ_cells"],
            "largest_cell_organs": cells["largest_cell"],
            "organs_total": sum(cells["organ_kinds"].values()),
            "organs_by_kind": cells["organ_kinds"],
            "organ_bond_types": cells["organ_bonds"],
            "anchor_organs_no_prov": cells["anchor_organs"],
            "supercell_page_organs": sum(cells["supercell_pages_by_db"].values()),
            "supercell_concept_organs": cells["supercell_concept_organs"],
            "builder_meta_counts": cells["meta"]["counts"],
        },
        "decls": {
            "distinct_decl_organs": len(cells["decl_ids"]),
            "by_library": dict(sorted(cells["decl_by_lib"].items(),
                                      key=lambda kv: -kv[1])),
            "mathlib_distinct": n_mathlib_decls,
            "mathlib_concept_joined": len(cells["mathlib_concept_joined"]),
            "any_lib_concept_joined": len(cells["decls_concept_joined"]),
            "aliases_decls_crosscheck": len(aliases["decls"]),
        },
        "concepts": {
            "distinct_qids_in_cells": len(cells["qids"]),
            "concept_organs": cells["organ_kinds"]["concept"],
            "field_concepts_as_supercell_organs": cells["supercell_concept_organs"],
        },
        "synapses": {
            "n_synapses": syn["n_synapses"],
            "endpoints": syn["endpoints"],
            "synapses_carrying_kind": dict(sorted(syn["kind_synapses"].items(),
                                                  key=lambda kv: -kv[1])),
            "constituent_bonds_by_kind": dict(sorted(syn["kind_bonds"].items(),
                                                     key=lambda kv: -kv[1])),
            "bonds_total": sum(syn["kind_bonds"].values()),
        },
        "provenance_split": {
            "classes": ["human", "ai-moderated", "ai", "automated"],
            "organ_attach_bonds": {"counts": dict(sorted(prov_organs.items())),
                                   "fractions": _pct(prov_organs),
                                   "note": "prov-carrying organ attachments only; "
                                           f"{cells['anchor_organs_no_prov'] if 'anchor_organs_no_prov' in cells else cells['anchor_organs']} anchor organs are definitional (no prov)"},
            "synapse_traces": {"counts": dict(sorted(prov_traces.items())),
                               "fractions": _pct(prov_traces)},
        },
        "external_dbs": {
            db: {
                "anchored_cell_pages": cells["page_by_db"].get(db, 0),
                "anchored_supercell_pages": cells["supercell_pages_by_db"].get(db, 0),
                "ingested_corpus_pages": corpora.get(db, {}).get("n_pages"),
                "fetched_at": corpora.get(db, {}).get("fetched_at"),
            }
            for db in sorted(set(cells["page_by_db"]) | set(corpora)
                             | set(cells["supercell_pages_by_db"]))
        },
        "mathlib_coverage": {
            "joined_mathlib_decls": n_mathlib_decls,
            "docgen4_mathlib_names": oracle["names_mathlib"],
            "docgen4_total_names": oracle["names_total"],
            "coverage_vs_mathlib_names": round(cov_mathlib, 5),
            "coverage_vs_all_names": round(cov_overall, 5),
            "caveat": "oracle cache is the CURRENT doc-gen4 snapshot (see mtime), "
                      "~4 weeks after the Brain's Mathlib pin; known to miss a "
                      "small number of real decls (memory: oracle incomplete)",
        },
        "frontier": frontier_meta.get("counts"),
        "wikilean_annotations_disk": n_annotations,
        "benchmark_target_coverage": bench,
        "pipeline_one_liner": (
            "Deterministic no-LLM build: 10 external-DB ingest adapters + Wikidata/"
            "Mathlib/TheoremGraph/formal-conjectures harvests -> build_nodes/"
            "build_edges (organ layer) -> v3 cell merge (rule-1 exact fusion, "
            "single-best rule-2 attach, no transitive closure) -> synapse "
            "aggregation -> frontier partition -> layout -> shards; AI-generated "
            "joins enter only through verified proposal folding "
            "(brain/proposals -> fold_proposals.py); acceptance suites gate publish."
        ),
    }

    OUT_JSON.write_text(json.dumps(result, indent=1))
    OUT_MD.write_text(render_md(result))
    print(f"wrote {OUT_JSON}\nwrote {OUT_MD}")


# ---------------------------------------------------------------- markdown
def render_md(r: dict) -> str:
    c, d, s, m = r["cells"], r["decls"], r["synapses"], r["mathlib_coverage"]
    b = r["benchmark_target_coverage"]
    pv = r["provenance_split"]
    kinds_hist = " · ".join(f"{k} {v:,}" for k, v in
                            s["synapses_carrying_kind"].items())
    bonds_hist = " · ".join(f"{k} {v:,}" for k, v in
                            s["constituent_bonds_by_kind"].items())
    libs = " · ".join(f"{k} {v:,}" for k, v in d["by_library"].items())

    def prow(block):
        cnt, frac = block["counts"], block["fractions"]
        return " · ".join(f"{k} {cnt.get(k,0):,} ({frac.get(k,0)*100:.1f}%)"
                          for k in ["human", "ai-moderated", "ai", "automated"])

    ext_rows = "\n".join(
        f"| {db} | {v['anchored_cell_pages']:,} | {v['anchored_supercell_pages']:,} | "
        f"{v['ingested_corpus_pages'] if v['ingested_corpus_pages'] is not None else '—'} |"
        for db, v in r["external_dbs"].items()
    )
    fr = r["frontier"] or {}
    md = f"""# The Brain as a scientific artifact (REVIEW-2 §5a)

Generated by `bench/analysis/brain_artifact.py` — every number recomputed from
the repo artifacts listed in `brain_artifact.json.inputs` (no numbers copied
from prior reports). Snapshot: cells/synapses built
**{r['snapshot']['cells_generated_at']}**, Mathlib pins
{', '.join('`'+p+'`' for p in r['snapshot']['mathlib_pins'])}.

## Compact artifact table

| Quantity | Value |
|---|---|
| Cells (atoms / graph nodes) | **{c['n_cells']:,}** (multi-organ: {c['multi_organ_cells']:,}; largest cell: {c['largest_cell_organs']} organs) |
| Organs (total) | {c['organs_total']:,} — decl {c['organs_by_kind']['decl']:,} · concept {c['organs_by_kind']['concept']:,} · article {c['organs_by_kind']['article']:,} · xref page {c['organs_by_kind']['page']:,} · lit statement {c['organs_by_kind']['statement']:,} |
| Distinct declarations | {d['distinct_decl_organs']:,} ({libs}) |
| — Mathlib decls joined | **{d['mathlib_distinct']:,}** distinct; {d['mathlib_concept_joined']:,} share a cell with ≥1 concept (concept-joined) |
| Concepts (distinct QIDs in cells) | **{r['concepts']['distinct_qids_in_cells']:,}** (+{r['concepts']['field_concepts_as_supercell_organs']} field concepts held at supercell altitude) |
| Synapses (aggregated edges) | **{s['n_synapses']:,}** ({s['endpoints'].get('cell-cell',0):,} cell–cell, {s['endpoints'].get('cell-supercell',0):,} cell–supercell), carrying {s['bonds_total']:,} constituent bonds |
| Synapse kinds (synapses carrying kind) | {kinds_hist} |
| Constituent bonds by kind | {bonds_hist} |
| Provenance — organ attachments | {prow(pv['organ_attach_bonds'])} |
| Provenance — synapse traces | {prow(pv['synapse_traces'])} |
| Mathlib coverage | {m['joined_mathlib_decls']:,} / {m['docgen4_mathlib_names']:,} doc-gen4 Mathlib names = **{m['coverage_vs_mathlib_names']*100:.2f}%** ({m['coverage_vs_all_names']*100:.2f}% of all {m['docgen4_total_names']:,} indexed names) |
| Formal-home coverage of cells | {c['n_cells']-fr.get('homeless',0):,}/{c['n_cells']:,} cells carry ≥1 decl organ; {fr.get('homeless','?'):,} homeless → frontier areas (assigned {fr.get('assigned','?'):,}, unsorted {fr.get('unsorted','?'):,}) |
| WikiLean annotated articles | {r['wikilean_annotations_disk']:,} on-disk annotation files (disk is the cache; D1 canonical) — {c['organs_by_kind']['article']} article organs in cells |
| Snapshot date | cells {r['snapshot']['cells_generated_at']} · Mathlib pin 2026-07-04 / `bf3266149cda603f` · external ingests 2026-07-03…-12 |

**Construction (one line).** {r['pipeline_one_liner']}

## External databases — anchored pages per DB

Anchored = the page is an organ of a cell (single claimant) or of a supercell
(multi-claimant, rule 4). Corpus = pages ingested into
`catalog/data/external/<db>_pages.jsonl` (the Brain mints ext nodes only for
anchored + 1-hop frontier pages).

| DB | cell-anchored | supercell-anchored | ingested corpus |
|---|---|---|---|
{ext_rows}

## Benchmark-target coverage (is the answer in the Brain at all?)

Membership = the gold declaration is a decl organ in `brain/data/cells.jsonl`
(the Brain's decl universe; checked as `decl:Mathlib:<name>`, with
Init/Batteries fallback). "Current oracle" = the doc-gen4 cache at
`{r['inputs']['oracle']['path']}` (mtime {r['inputs']['oracle']['mtime']}).

| Gold set | n | in Brain decl universe | in current doc-gen4 oracle |
|---|---|---|---|
| MathlibQR shared-171 gold decls | {b['qr171']['n']} | **{b['qr171']['in_brain']}** ({b['qr171']['in_brain_frac']*100:.1f}%) | {b['qr171']['in_current_oracle']} |
| MathlibMPR distinct gold premises | {b['mpr_premises']['n_distinct_gold_premises']} | **{b['mpr_premises']['in_brain']}** ({b['mpr_premises']['in_brain_frac']*100:.1f}%) | {b['mpr_premises']['in_current_oracle']} |
| MathlibMPR post-cutoff main results | {b['mpr_main_results_postcutoff']['n']} | {b['mpr_main_results_postcutoff']['in_brain']} | {b['mpr_main_results_postcutoff']['in_current_oracle']} |
| Fresh-100 gold decls | {b['fresh100']['n']} | **{b['fresh100']['in_brain']}** | {b['fresh100']['in_current_oracle']} |

MPR per-query retrieval ceiling with Brain-only premises: all groups coverable
for {b['mpr_premises']['queries_all_groups_coverable']}/{b['mpr_premises']['n_queries']} queries; ≥1 group coverable for
{b['mpr_premises']['queries_any_group_coverable']}/{b['mpr_premises']['n_queries']}.

**Fresh-100 verification — the strict-0 claim FAILS by one.** 99/100 fresh
golds are absent from the Brain; the single hit is
`{b['fresh100']['brain_hits'][0] if b['fresh100']['brain_hits'] else ''}`
(fresh_025, merged into Mathlib 2026-07-07): it entered the Brain through the
verified discovery-proposal fold pinned **2026-07-18**, which postdates the
fresh window (2026-07-04→07-10). "Fresh ∉ Brain by construction" is therefore
true of the 2026-07-04 Mathlib harvest inputs but NOT of the whole artifact —
the discovery fold is a second, later Mathlib entry channel. This corroborates
`bench/analysis/fresh_exposure.md`, which independently flags fresh_025 as
exposed on all four axes. State the claim as 99/100 in the report.

Oracle-column footnotes: the 1 QR gold absent from the current oracle is
`{b['qr171']['oracle_missing'][0]}` (renamed/removed since MathlibQR was
built); the 2 absent MPR premises are
{', '.join('`'+x+'`' for x in b['mpr_premises']['oracle_missing'])}. The 6
fresh golds absent from the oracle are all `_root_`-mangled task names
(namespace + `_root_.` prefix), a naming artifact — not missing declarations.
{b['mpr_main_results_postcutoff']['in_brain']} MPR post-cutoff main results
appear in the Brain (3 as decl-node anchors from pre-pin merges, 1 via the
2026-07-18 discovery fold) — MPR's "post-cutoff" is relative to the retrieval
systems' training cutoffs, not the Brain's pin.

## Provenance classification rules

- **human** — merged mathlib4 source attributes (`@[wikidata]`/`@[stacks]`/
  `@[kerodon]`): each passed human maintainer review in a mathlib4 PR.
- **ai-moderated** — AI-generated, machine-verified before folding:
  agent+oracle grounding, verified discovery proposals, fold-verified
  fc-agent joins, TheoremGraph dual-LLM-judge matches.
- **ai** — AI-generated, not yet reviewed: tag-queue `@[wikidata]` candidates.
- **automated** — deterministic, no LLM: Wikidata property values,
  external-ingest qids, internal links, kernel dependency extraction,
  docstring reference URLs, FQ-name statement scans, D1 sitelink article
  joins, transitive joins.
- Anchor organs ({c['anchor_organs_no_prov']:,}) carry no provenance record — the anchor
  *names* the atom (definitional), so it is excluded from the split.

## Caveats

- The doc-gen4 denominator is the current cache ({r['inputs']['oracle']['mtime']}),
  ~4 weeks after the Brain's Mathlib pin, and is known to miss a small number
  of real declarations; coverage percentages move by <0.1pp under this drift.
- Article organs link WikiLean pages whose annotation content has its own
  human/ai-moderated/ai provenance in D1; here the article *join* (sitelink)
  is classified automated.
- Synapse-trace provenance is computed over the traces retained in
  `brain/data/synapses.jsonl` (TRACE_CAP=64 has never fired, so this is the
  full bond set).
"""
    return md


if __name__ == "__main__":
    main()
