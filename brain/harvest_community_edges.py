#!/usr/bin/env python3
"""Graduate live community brain edges from D1 into the committed static base.

docs/BRAIN-EDITS-ROADMAP.md phase 4. Reads the LIVE (non-deleted) rows of the D1
`brain_edges` table, validates each edge's endpoints against the node universe,
runs AI-submitted edges through the existence oracle (decl / QID), and writes the
survivors to `brain/data/community_edges.jsonl` — the durable, git-versioned
graduation record that `build_shards.py` folds into the base (the xref reverse
index) and that `query.py` / the AI surface can read.

Trust model (docs/BRAIN-EDITS-ROADMAP.md): human edges are endpoint-validated and
trusted; AI edges (`actor_type='ai'`) must ADDITIONALLY pass the oracle — mirroring
`fold_proposals.py`, so AI data never becomes a permanent graph fact without a
machine check. Deleted (gravestoned) edges are excluded, so a rebuild never
resurrects one and never drops a currently-live edge. D1 stays canonical for the
LIVE tail (the /brain overlay renders from D1); this snapshot is the base layer.

    python3 brain/harvest_community_edges.py             # read remote D1 → jsonl
    python3 brain/harvest_community_edges.py --dry-run   # report only
    python3 brain/harvest_community_edges.py --from-json rows.json   # offline/test
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NODES = ROOT / "brain" / "data" / "nodes.jsonl"
OUT = ROOT / "brain" / "data" / "community_edges.jsonl"

# must mirror wiki/src/brain-edits.ts COMMUNITY_KINDS + XREF_DBS
COMMUNITY_KINDS = {"relates", "xref", "formalizes", "mentions", "matches", "cites"}
XREF_DBS = {"mathworld", "nlab", "proofwiki", "eom", "planetmath", "metamath",
            "lmfdb_knowl", "oeis", "dlmf", "msc", "stacks", "kerodon", "kgmid"}


def load_node_ids() -> set[str]:
    ids: set[str] = set()
    for line in NODES.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("id"):
            ids.add(r["id"])
    return ids


def read_d1_live() -> list[dict]:
    """One read-only remote SELECT of every live brain_edges row."""
    proc = subprocess.run(
        ["npx", "wrangler", "d1", "execute", "wikilean", "--remote", "--json",
         "--command", "SELECT id,src,dst,kind,evidence,added_by,actor_type,created_at "
                      "FROM brain_edges WHERE status='live'"],
        cwd=str(ROOT / "wiki"), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"wrangler d1 execute failed:\n{proc.stderr[-2000:]}")
    out = proc.stdout
    start = min((i for i in (out.find("["), out.find("{")) if i != -1), default=-1)
    if start == -1:
        raise RuntimeError("no JSON in wrangler output")
    parsed = json.loads(out[start:])
    return (parsed[0] if isinstance(parsed, list) else parsed)["results"]


# ---- AI oracle (reused from fold_proposals; lazily built, fail-closed) --------
_oracle: set[str] | None = None
_qids: dict | None = None


def _ai_endpoint_ok(node_id: str) -> bool:
    """AI edges: a decl endpoint must exist in the oracle/checkout; a QID must be
    a known Wikidata item. Fail closed (drop the AI edge) if the oracle is
    unavailable — never let unverified AI data through."""
    global _oracle, _qids
    try:
        from fold_proposals import oracle_names, checkout_has, known_qids
    except Exception:
        return False
    if node_id.startswith("decl:"):
        if _oracle is None:
            _oracle = oracle_names()
        if not _oracle:
            return False
        fq = node_id.split(":", 2)[2] if node_id.count(":") >= 2 else ""
        return bool(fq) and (fq in _oracle or checkout_has(fq))
    if len(node_id) > 1 and node_id[0] == "Q" and node_id[1:].isdigit():
        if _qids is None:
            _qids = known_qids()
        return node_id in _qids
    # container/literature endpoints: universe membership (checked already) suffices
    return True


def validate_edge(row: dict, node_ids: set[str], pin: str) -> tuple[dict | None, str]:
    """(edge, "") if the row graduates into the base, else (None, drop-reason)."""
    kind = row.get("kind")
    if kind not in COMMUNITY_KINDS:
        return None, f"bad kind: {kind}"
    src, dst = row.get("src"), row.get("dst")
    ai = row.get("actor_type") == "ai"
    if src not in node_ids:
        return None, "src not a known node"
    if ai and not _ai_endpoint_ok(src):
        return None, "src fails AI oracle"
    if kind == "xref":
        if not (isinstance(dst, str) and dst.startswith("xref:")):
            return None, "xref dst malformed"
        parts = dst.split(":")
        if len(parts) < 3 or parts[1] not in XREF_DBS or not parts[2]:
            return None, "unknown/empty xref db"
    else:
        if dst not in node_ids:
            return None, "dst not a known node"
        if ai and not _ai_endpoint_ok(dst):
            return None, "dst fails AI oracle"
    try:
        ev = json.loads(row.get("evidence") or "{}")
        if not isinstance(ev, dict):
            ev = {"note": str(ev)}
    except (TypeError, json.JSONDecodeError):
        ev = {"note": row.get("evidence")}
    actor = row.get("actor_type", "human")
    edge = {
        "src": src, "dst": dst, "kind": kind,
        "provenance": {"source": "community",
                       "method": f"community-{actor} (brain_edges)", "pin": pin},
        "confidence": "high" if actor == "human" else "medium",
        "evidence": {**ev, "added_by": row.get("added_by"),
                     "actor_type": actor, "edge_id": row.get("id")},
    }
    return edge, ""


def harvest(rows: list[dict], node_ids: set[str], pin: str) -> tuple[list[dict], dict]:
    kept: list[dict] = []
    dropped: dict[str, int] = {}
    for row in rows:
        edge, reason = validate_edge(row, node_ids, pin)
        if edge is not None:
            kept.append(edge)
        else:
            dropped[reason] = dropped.get(reason, 0) + 1
    return kept, dropped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report, don't write")
    ap.add_argument("--from-json", help="read rows from a JSON file instead of D1 (offline/test)")
    args = ap.parse_args()

    node_ids = load_node_ids()
    if not node_ids:
        sys.exit(f"FATAL: no nodes at {NODES} — run the brain build first")

    if args.from_json:
        rows = json.loads(Path(args.from_json).read_text())
    else:
        rows = read_d1_live()

    pin = datetime.date.today().isoformat()
    kept, dropped = harvest(rows, node_ids, pin)

    n_human = sum(1 for e in kept if e["evidence"].get("actor_type") == "human")
    n_ai = len(kept) - n_human
    print(f"community edges: {len(rows)} live → {len(kept)} graduate "
          f"({n_human} human, {n_ai} AI-verified)")
    for reason, n in sorted(dropped.items(), key=lambda kv: -kv[1]):
        print(f"  dropped {n}: {reason}")

    if args.dry_run:
        print("(dry run — not written)")
        return 0
    OUT.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in kept))
    print(f"wrote {OUT} ({len(kept)} edges)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
