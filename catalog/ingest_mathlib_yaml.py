#!/usr/bin/env python3
"""Ingest mathlib4's docs/1000.yaml (the 1000+ theorems project tracker) as an
authoritative QID→Mathlib-decl source for the WikiLean catalog. Deterministic —
no LLM, no network; reads the bot's read-only mathlib4 checkout and the local
decl-index shards.

1000.yaml is keyed by Wikidata QID and its decl fields are maintainer-reviewed
(merged through mathlib PR review), i.e. the highest-trust mapping supply the
catalog can get. 100.yaml / undergrad.yaml are NOT QID-keyed — out of scope.

For each QID entry carrying `decl:` (or `decls:`):
  - resolve every decl's module via wiki/public/assets/decl-index/ (doc-gen4's
    411k-decl index); a decl that doesn't resolve is flagged, never emitted
    (it rotted since the docs build, or predates Mathlib4 — review by hand);
  - QID absent from the catalog       → emit (provenance "mathlib-maintainer");
  - QID present, same primary decl    → skip (already covered);
  - QID present, DIFFERENT decl       → flag to the conflicts file for human
    review — maintainer data never silently overrides existing mappings.

Outputs (both gitable):
  catalog/data/mathlib_yaml_tagged.jsonl     ← wired into bot/pool.py CATALOG
  catalog/data/mathlib_yaml_conflicts.jsonl  ← review queue (not in CATALOG)

  catalog/.venv/bin/python3 catalog/ingest_mathlib_yaml.py [--dry-run]
      [--mathlib /Users/jack/Desktop/LEAN/mathlib4]
"""
import argparse
import json
import re
import sys
from pathlib import Path

import yaml

# Strict QID only. The 1000+ project uses X-suffixed provisional ids (e.g.
# Q180345X) for theorems WITHOUT a Wikidata item — those must never enter the
# catalog (they'd break tagging and Wikidata ops downstream).
QID_RE = re.compile(r"Q\d+\Z")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "catalog" / "data"
DECL_INDEX = ROOT / "wiki" / "public" / "assets" / "decl-index"
OUT = DATA / "mathlib_yaml_tagged.jsonl"
CONFLICTS = DATA / "mathlib_yaml_conflicts.jsonl"
# The files pool.py already loads (keep in sync with bot/pool.py CATALOG).
EXISTING = [DATA / "pilot_tagged.jsonl", DATA / "tier2_tagged.jsonl",
            DATA / "generated_candidates.jsonl", DATA / "refresh_tagged.jsonl"]


def load_decl_modules() -> dict[str, str]:
    """decl → module from every decl-index shard (skip the manifest)."""
    if not DECL_INDEX.exists():
        sys.exit(f"decl-index not found at {DECL_INDEX} — run `npm run build:decl-index` in wiki/ first")
    modules: dict[str, str] = {}
    for shard in DECL_INDEX.glob("*.json"):
        if shard.name == "manifest.json":
            continue
        for decl, module in json.loads(shard.read_text()):
            modules[decl] = module
    return modules


def load_existing() -> dict[str, str]:
    """wikidata_qid → primary_decl across the current catalog files.
    LAST file wins on a QID collision — the catalog's real semantics
    (bot/pool.py CATALOG: refresh_tagged.jsonl overrides the originals), so
    conflict/skip decisions compare against the decl the pool actually uses,
    not a stale overridden row."""
    seen: dict[str, str] = {}
    for f in EXISTING:
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            q, pd = r.get("wikidata_qid"), r.get("primary_decl")
            if isinstance(q, str) and isinstance(pd, str) and pd:
                seen[q] = pd
    return seen


def entry_decls(rec: dict) -> list[str]:
    """The decl names an entry carries: `decl:` scalar first, then `decls:` list."""
    out: list[str] = []
    if isinstance(rec.get("decl"), str) and rec["decl"]:
        out.append(rec["decl"])
    if isinstance(rec.get("decls"), list):
        out += [d for d in rec["decls"] if isinstance(d, str) and d and d not in out]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mathlib", type=Path,
                    default=Path("/Users/jack/Desktop/LEAN/mathlib4"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = args.mathlib / "docs" / "1000.yaml"
    if not src.exists():
        sys.exit(f"not found: {src}")
    doc = yaml.safe_load(src.read_text())
    modules = load_decl_modules()
    existing = load_existing()

    rows, conflicts, skipped_same, unresolved, provisional = [], [], 0, [], 0
    for qid, rec in doc.items():
        if not (isinstance(qid, str) and isinstance(rec, dict)):
            continue
        if not QID_RE.fullmatch(qid):
            provisional += 1  # X-suffixed 1000+ provisional id — no Wikidata item
            continue
        decls = entry_decls(rec)
        if not decls:
            continue  # tracked but not formalized — no mapping to ingest
        primary = decls[0]
        title = rec.get("title") or ""
        if primary not in modules:
            unresolved.append({"qid": qid, "decl": primary, "title": title})
            continue
        prior = existing.get(qid)
        if prior == primary:
            skipped_same += 1
            continue
        if prior is not None:
            conflicts.append({
                "wikidata_qid": qid, "title": title,
                "yaml_decl": primary, "catalog_decl": prior,
                "source": "mathlib4:docs/1000.yaml",
                "note": "maintainer YAML disagrees with catalog — review by hand",
            })
            continue
        rows.append({
            "wikidata_qid": qid,
            "primary_decl": primary,
            "primary_qid": qid,          # 1000.yaml QIDs are theorem-level (tight)
            "primary_qid_label": title,
            "title": title,
            "mathlib_decls": [
                {"decl": d, "module": modules[d], "kind": "theorem",
                 "confidence": "high",
                 "evidence": "mathlib4 docs/1000.yaml (maintainer-reviewed)"}
                for d in decls if d in modules
            ],
            "authors": rec.get("authors"),
            "provenance": "mathlib-maintainer",
            "source": "mathlib4:docs/1000.yaml",
        })

    print(f"1000.yaml: {len(doc)} entries, {sum(1 for r in doc.values() if isinstance(r, dict) and entry_decls(r))} with decls")
    print(f"  new mappings          : {len(rows)}")
    print(f"  provisional (QxxxX)   : {provisional} (no Wikidata item — skipped)")
    print(f"  already in catalog    : {skipped_same} (same decl — skipped)")
    print(f"  conflicts for review  : {len(conflicts)}")
    print(f"  unresolved decls      : {len(unresolved)} (not in decl-index — rotted/renamed?)")
    for u in unresolved[:8]:
        print(f"    ? {u['qid']:12} {u['decl']}  ({u['title']})")
    if args.dry_run:
        print("[dry-run] nothing written.")
        return 0
    OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    CONFLICTS.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in conflicts))
    print(f"wrote {OUT.name} ({len(rows)}) + {CONFLICTS.name} ({len(conflicts)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
