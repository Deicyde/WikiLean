#!/usr/bin/env python3
"""Build bench/data/tasks.jsonl — gold tasks for the Wikibrain benchmark.

Gold sources (docs/BRAIN-V2.md "Benchmark (axis 5's referee)"):
  (a) catalog/data/mathlib_tag_xrefs.jsonl rows db=="wikidata" — decl<->QID pairs
      human-merged into mathlib4 as @[wikidata] attributes (the strongest gold).
  (b) catalog/data/rebuild_grounding.json formalizations with match_kind=="exact"
      and confidence=="high" (agent+oracle grounding, verified pass).
  (c) site/annotations/*.json statements with status=="formalized" and a cited
      decl (statement-level tasks; canonical files only, .agent1.json excluded).

Task types (bench/tasklib.py documents the row shapes):
  T1 informal->formal: concept (+statement) -> fully-qualified Mathlib decl.
  T2 formal->informal: decl -> Wikidata QID + enwiki slug.
  T3 formalized-or-not: YES + witness decl / NO; the NO side is drawn from
     grounding rows with status=="not_formalized" (balanced with the YES side).

Deterministic: seeded sampling over sorted pools, rows sorted by id, atomic
write, no timestamps in the output. Every gold/witness decl is verified against
the local decl oracle (declaration-data cache) so no task carries a stale name.

Run: python3 bench/generate_tasks.py
"""
from __future__ import annotations

import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tasklib import DATA_DIR, REPO, TASKS_PATH, write_jsonl  # noqa: E402

TAG_XREFS = REPO / "catalog" / "data" / "mathlib_tag_xrefs.jsonl"
GROUNDING = REPO / "catalog" / "data" / "rebuild_grounding.json"
UNIVERSE = REPO / "catalog" / "data" / "wikidata_universe.jsonl"
BRAIN_NODES = REPO / "brain" / "data" / "nodes.jsonl"
ANNOT_DIR = REPO / "site" / "annotations"
CACHE_ORACLE = REPO / ".claude" / "skills" / "mathlib-search" / ".cache" / "declaration-data.json"

SEED = 20260710
DEV_FRACTION = 1 / 6  # ~150 eval / 30 dev at the 180-task target
QUOTAS = {
    "T1": {"mathlib_tag_xrefs": 30, "rebuild_grounding": 20, "annotations": 10},
    "T2": {"mathlib_tag_xrefs": 30, "rebuild_grounding": 30},
    "T3": {"annotations": 20, "rebuild_grounding_yes": 10, "rebuild_grounding_no": 30},
}
MIN_STATEMENT_LEN = 15
MAX_ANNOT_PER_ARTICLE = 2


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------

def load_oracle() -> set[str]:
    if not CACHE_ORACLE.exists():
        raise RuntimeError(
            f"decl oracle absent: {CACHE_ORACLE}\n"
            "  Prime it once: python3 .claude/skills/mathlib-search/mathlib_search.py "
            "decl Group --live")
    d = json.loads(CACHE_ORACLE.read_text())
    decls = d.get("declarations", {})
    return set(decls.keys()) if isinstance(decls, dict) else set(decls)


def load_tag_pairs() -> list[dict]:
    rows = []
    with open(TAG_XREFS) as f:
        for line in f:
            r = json.loads(line)
            if "_meta" in r or r.get("db") != "wikidata" or r.get("unverified"):
                continue
            rows.append(r)
    return rows


def load_labels() -> tuple[dict[str, str], dict[str, list[str]], dict[str, str]]:
    """qid -> label; qid -> [slugs] (true enwiki slug first when known);
    wikilean_slug -> qid (for joining annotation files)."""
    label: dict[str, str] = {}
    slugs: dict[str, list[str]] = defaultdict(list)
    slug_to_qid: dict[str, str] = {}

    def add_slug(qid: str, slug: str | None, front: bool = False) -> None:
        if not slug:
            return
        cur = slugs[qid]
        if slug in cur:
            return
        cur.insert(0, slug) if front else cur.append(slug)

    # wikidata_universe: authoritative labels + TRUE enwiki sitelink slugs.
    with open(UNIVERSE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if "_meta" in r:
                continue
            q = r.get("qid")
            if not q:
                continue
            if r.get("label"):
                label.setdefault(q, r["label"])
            add_slug(q, r.get("enwiki_slug"), front=True)
    # brain nodes: broader coverage; slugs here are WikiLean-sanitized.
    if BRAIN_NODES.exists():
        with open(BRAIN_NODES) as f:
            for line in f:
                r = json.loads(line)
                if r.get("type") != "concept":
                    continue
                q = r["id"]
                if r.get("label"):
                    label.setdefault(q, r["label"])
                add_slug(q, r.get("slug"))
    # grounding: WikiLean slugs (these are the annotation-file slugs).
    for r in json.load(open(GROUNDING)):
        q = r["qid"]
        if r.get("label"):
            label.setdefault(q, r["label"])
        add_slug(q, r.get("slug"))
        slug_to_qid.setdefault(r["slug"], q)
    return label, dict(slugs), slug_to_qid


def load_annotation_statements(oracle: set[str], slug_to_qid: dict[str, str]) -> list[dict]:
    """Formalized statements with a cited, oracle-verified decl and real anchor
    text, from canonical annotation files joinable to a QID."""
    out = []
    for fp in sorted(ANNOT_DIR.glob("*.json")):
        if fp.name.endswith(".agent1.json"):
            continue
        try:
            d = json.loads(fp.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        slug = d.get("slug") or fp.stem
        qid = slug_to_qid.get(slug)
        if not qid:
            continue
        kept = 0
        seen_decls: set[str] = set()
        for i, a in enumerate(d.get("annotations") or []):
            if not isinstance(a, dict) or a.get("status") != "formalized":
                continue
            ml = a.get("mathlib") or {}
            decl = ml.get("decl")
            snippet = ((a.get("anchor") or {}).get("snippet") or "").strip()
            if not decl or decl not in oracle or len(snippet) < MIN_STATEMENT_LEN:
                continue
            if decl in seen_decls or kept >= MAX_ANNOT_PER_ARTICLE:
                continue
            seen_decls.add(decl)
            kept += 1
            stmt = snippet if not a.get("label") else f"{a['label']} — {snippet}"
            out.append({
                "slug": slug, "qid": qid,
                "display_title": d.get("display_title") or slug.replace("_", " "),
                "statement": stmt, "decl": decl,
                "module": ml.get("module"), "match_kind": ml.get("match_kind"),
                "provenance": a.get("provenance"),
                "annot_key": a.get("id") or f"i{i}",
            })
    return out


# ---------------------------------------------------------------------------
# Pool -> task assembly
# ---------------------------------------------------------------------------

def file_to_module(path: str) -> str | None:
    if not path or not path.endswith(".lean"):
        return None
    return path[: -len(".lean")].replace("/", ".")


def sample(rng: random.Random, pool: list, k: int) -> list:
    return pool if len(pool) <= k else rng.sample(pool, k)


def main() -> int:
    oracle = load_oracle()
    tag_rows = load_tag_pairs()
    grounding = json.load(open(GROUNDING))
    label, slugs, slug_to_qid = load_labels()
    annots = load_annotation_statements(oracle, slug_to_qid)

    # Index the gold structure.
    tag_qid_decls: dict[str, set[str]] = defaultdict(set)   # qid -> tagged decls
    tag_decl_rows: dict[str, list[dict]] = defaultdict(list)  # decl -> tag rows
    for r in tag_rows:
        if r["decl"] not in oracle:
            continue  # tag rows are pre-verified; belt-and-suspenders
        tag_qid_decls[r["tag"]].add(r["decl"])
        tag_decl_rows[r["decl"]].append(r)

    exact_by_qid: dict[str, list[dict]] = defaultdict(list)      # exact (any conf)
    exact_high_by_qid: dict[str, list[dict]] = defaultdict(list)  # exact + high
    decl_to_qids: dict[str, set[str]] = defaultdict(set)
    not_formalized: list[dict] = []
    for row in grounding:
        if row.get("status") == "not_formalized" and not row.get("formalizations"):
            not_formalized.append(row)
        for f in row.get("formalizations") or []:
            if f.get("match_kind") != "exact" or f.get("decl") not in oracle:
                continue
            exact_by_qid[row["qid"]].append(f)
            decl_to_qids[f["decl"]].add(row["qid"])
            if f.get("confidence") == "high":
                exact_high_by_qid[row["qid"]].append(f)

    def accept_decls(qid: str) -> list[str]:
        """Every decl any gold source calls an exact formalization of qid."""
        s = set(tag_qid_decls.get(qid, ())) | {f["decl"] for f in exact_by_qid.get(qid, ())}
        return sorted(s)

    def gold_pairs(decl: str) -> list[dict]:
        qids = {r["tag"] for r in tag_decl_rows.get(decl, ())} | decl_to_qids.get(decl, set())
        pairs = []
        for q in sorted(qids):
            if q in label and slugs.get(q):
                pairs.append({"qid": q, "slug": slugs[q][0], "slugs": slugs[q],
                              "label": label[q]})
        return pairs

    def has_identity(qid: str) -> bool:
        return qid in label and bool(slugs.get(qid))

    rng = random.Random(SEED)
    tasks: list[dict] = []
    skipped = {"no_identity": 0}

    # ---- T1: informal -> formal --------------------------------------------
    t1_qids: set[str] = set()

    def add_t1(qid: str, gold_decl: str, statement: str | None,
               provenance: dict, label_override: str | None = None,
               slug_override: str | None = None) -> None:
        if qid in t1_qids:
            return
        if not has_identity(qid) and not (label_override and slug_override):
            skipped["no_identity"] += 1
            return
        t1_qids.add(qid)
        decls = sorted(set(accept_decls(qid)) | {gold_decl})
        tasks.append({
            "id": f"T1-{qid}", "type": "T1", "split": None,
            "prompt_context": {
                "label": label_override or label[qid],
                "slug": slug_override or slugs[qid][0],
                "statement": statement,
            },
            "gold": {"decl": gold_decl, "decls": decls, "qid": qid},
            "provenance": provenance,
        })

    pool = sorted(q for q in tag_qid_decls if has_identity(q))
    for qid in sample(rng, pool, QUOTAS["T1"]["mathlib_tag_xrefs"]):
        row = min(tag_decl_rows[sorted(tag_qid_decls[qid])[0]],
                  key=lambda r: (r["file"], r["line"]))
        add_t1(qid, sorted(tag_qid_decls[qid])[0], None,
               {"source": "mathlib_tag_xrefs", "file": row["file"], "line": row["line"]})

    pool = sorted(q for q in exact_high_by_qid
                  if q not in tag_qid_decls and has_identity(q))
    for qid in sample(rng, pool, QUOTAS["T1"]["rebuild_grounding"]):
        f = exact_high_by_qid[qid][0]  # row order in the committed file is stable
        add_t1(qid, f["decl"], None,
               {"source": "rebuild_grounding", "match_kind": "exact",
                "confidence": "high", "module": f.get("module")})

    pool = sorted((a for a in annots if a["qid"] not in t1_qids),
                  key=lambda a: (a["slug"], a["annot_key"]))
    t1_annots = sample(rng, pool, QUOTAS["T1"]["annotations"])
    used_annot_keys = {(a["slug"], a["annot_key"]) for a in t1_annots}
    for a in t1_annots:
        add_t1(a["qid"], a["decl"], a["statement"],
               {"source": "annotations", "slug": a["slug"], "annotation": a["annot_key"],
                "annotation_provenance": a["provenance"], "match_kind": a["match_kind"]},
               label_override=a["display_title"], slug_override=a["slug"])

    # ---- T2: formal -> informal --------------------------------------------
    t2_decls: set[str] = set()

    def add_t2(decl: str, module: str | None, provenance: dict) -> None:
        if decl in t2_decls:
            return
        pairs = gold_pairs(decl)
        if not pairs:
            skipped["no_identity"] += 1
            return
        t2_decls.add(decl)
        tasks.append({
            "id": f"T2-{decl}", "type": "T2", "split": None,
            "prompt_context": {"decl": decl, "module": module},
            "gold": {"pairs": pairs},
            "provenance": provenance,
        })

    pool = sorted(d for d in tag_decl_rows if gold_pairs(d))
    for decl in sample(rng, pool, QUOTAS["T2"]["mathlib_tag_xrefs"]):
        row = min(tag_decl_rows[decl], key=lambda r: (r["file"], r["line"]))
        add_t2(decl, file_to_module(row["file"]),
               {"source": "mathlib_tag_xrefs", "file": row["file"], "line": row["line"]})

    grounding_decls: dict[str, dict] = {}
    for qid, fs in sorted(exact_high_by_qid.items()):
        for f in fs:
            if f["decl"] not in tag_decl_rows:
                grounding_decls.setdefault(f["decl"], f)
    pool = sorted(d for d in grounding_decls if gold_pairs(d))
    for decl in sample(rng, pool, QUOTAS["T2"]["rebuild_grounding"]):
        add_t2(decl, grounding_decls[decl].get("module"),
               {"source": "rebuild_grounding", "match_kind": "exact", "confidence": "high"})

    # ---- T3: formalized-or-not ---------------------------------------------
    # YES side mixes statement-level (annotations) and concept-level (grounding)
    # items; the NO side is concept-level only — see README contamination note.
    pool = sorted((a for a in annots if (a["slug"], a["annot_key"]) not in used_annot_keys),
                  key=lambda a: (a["slug"], a["annot_key"]))
    for a in sample(rng, pool, QUOTAS["T3"]["annotations"]):
        witnesses = sorted(set(accept_decls(a["qid"])) | {a["decl"]})
        tasks.append({
            "id": f"T3-y-{a['slug']}-{a['annot_key']}", "type": "T3", "split": None,
            "prompt_context": {"label": a["display_title"], "slug": a["slug"],
                               "statement": a["statement"]},
            "gold": {"formalized": True, "witness_decl": a["decl"],
                     "witness_decls": witnesses},
            "provenance": {"source": "annotations", "slug": a["slug"],
                           "annotation": a["annot_key"],
                           "annotation_provenance": a["provenance"]},
        })

    t3_qids: set[str] = set()
    pool = sorted(q for q in exact_high_by_qid if has_identity(q))
    for qid in sample(rng, pool, QUOTAS["T3"]["rebuild_grounding_yes"]):
        f = exact_high_by_qid[qid][0]
        t3_qids.add(qid)
        tasks.append({
            "id": f"T3-y-{qid}", "type": "T3", "split": None,
            "prompt_context": {"label": label[qid], "slug": slugs[qid][0],
                               "statement": None},
            "gold": {"formalized": True, "witness_decl": f["decl"],
                     "witness_decls": accept_decls(qid)},
            "provenance": {"source": "rebuild_grounding", "match_kind": "exact",
                           "confidence": "high"},
        })

    pool = sorted((r["qid"] for r in not_formalized
                   if has_identity(r["qid"]) and r["qid"] not in t3_qids))
    for qid in sample(rng, pool, QUOTAS["T3"]["rebuild_grounding_no"]):
        tasks.append({
            "id": f"T3-n-{qid}", "type": "T3", "split": None,
            "prompt_context": {"label": label[qid], "slug": slugs[qid][0],
                               "statement": None},
            "gold": {"formalized": False, "witness_decl": None, "witness_decls": []},
            "provenance": {"source": "rebuild_grounding", "status": "not_formalized"},
        })

    # ---- splits (stratified per type, seeded) ------------------------------
    by_type: dict[str, list[dict]] = defaultdict(list)
    for t in tasks:
        by_type[t["type"]].append(t)
    for typ in sorted(by_type):
        group = sorted(by_type[typ], key=lambda t: t["id"])
        rng.shuffle(group)
        n_dev = round(len(group) * DEV_FRACTION)
        for i, t in enumerate(group):
            t["split"] = "dev" if i < n_dev else "eval"

    tasks.sort(key=lambda t: t["id"])
    ids = [t["id"] for t in tasks]
    assert len(ids) == len(set(ids)), "duplicate task ids"

    counts = {
        typ: {
            "total": len(by_type[typ]),
            "eval": sum(1 for t in by_type[typ] if t["split"] == "eval"),
            "dev": sum(1 for t in by_type[typ] if t["split"] == "dev"),
            "by_source": dict(sorted(
                (src, sum(1 for t in by_type[typ]
                          if t["provenance"]["source"] == src))
                for src in {t["provenance"]["source"] for t in by_type[typ]})),
        }
        for typ in sorted(by_type)
    }
    meta = {
        "spec": "docs/BRAIN-V2.md 'Benchmark (axis 5's referee)'",
        "generator": "bench/generate_tasks.py",
        "seed": SEED,
        "quotas": QUOTAS,
        "oracle": {"path": str(CACHE_ORACLE.relative_to(REPO)), "names": len(oracle)},
        "inputs": {
            "mathlib_tag_xrefs": len(tag_rows),
            "rebuild_grounding": len(grounding),
            "annotation_statements": len(annots),
        },
        "counts": counts,
        "n_tasks": len(tasks),
    }
    write_jsonl(TASKS_PATH, meta, tasks)
    print(f"wrote {len(tasks)} tasks -> {TASKS_PATH.relative_to(REPO)}")
    for typ in sorted(counts):
        c = counts[typ]
        print(f"  {typ}: {c['total']} (eval {c['eval']} / dev {c['dev']})  "
              f"{c['by_source']}")
    if skipped["no_identity"]:
        print(f"  skipped (no label/slug for QID): {skipped['no_identity']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
