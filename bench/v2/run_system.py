#!/usr/bin/env python3
"""Bridge v2 — SYSTEM mode: score the Wikibrain retrieval API directly.

No LLM anywhere: each benchmark query goes to the brain's transfer endpoint
(informal -> ranked formal decls) exactly once; the ranked decl list is written
for score_retrieval.py. This measures the API's raw retrieval quality — the
number comparable to published *retriever* rows (not agent rows).

Endpoint: GET <base>/api/brain/transfer?q=<query>&limit=<k>
Ranked decls are extracted from the response in result order: any hit whose id
is decl:<Lib>:<Name> (or that carries a decls list) contributes names in order,
deduplicated, truncated to --k.

Usage:
  python3 bench/v2/run_system.py --bench qr810           # 810 queries
  python3 bench/v2/run_system.py --bench mpr             # 69 NL statements
  python3 bench/v2/run_system.py --bench mpr --query-field formal
Options: --base http://localhost:8790 (default; the local v3 worker),
         --system wikibrain (output label), --k 10, --resume (default on).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from score_retrieval import qr_rows, mpr_rows, norm  # noqa: E402


def fetch_ranked(base: str, query: str, k: int, retries: int = 4) -> tuple[list[str], dict]:
    """One brain_bridge MCP call (the same tool the agents use). Ranking =
    hits[].decl in result order, then decl-anchored atoms as the tail."""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "brain_bridge",
                                     "arguments": {"q": query}}}).encode()
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                f"{base}/mcp", data=payload,
                headers={"Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"})
            with urllib.request.urlopen(req, timeout=60) as r:
                rpc = json.loads(r.read().decode())
            body = json.loads(rpc["result"]["content"][0]["text"])
            names: list[str] = []
            def add(n: str) -> None:
                n = norm(n)
                if n and n not in names:
                    names.append(n)
            for hit in body.get("hits") or []:
                add(hit.get("decl") or "")
            for atom in body.get("atoms") or []:
                aid = atom.get("id") or ""
                if aid.startswith("cell:decl:"):
                    add(aid.removeprefix("cell:"))
            return names[:k], {"match": body.get("match"),
                               "n_hits": len(body.get("hits") or []),
                               "n_atoms": len(body.get("atoms") or [])}
        except Exception as e:  # noqa: BLE001 — record and retry
            last = str(e)
            time.sleep(2 * (attempt + 1))
    return [], {"error": last}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bench", choices=["qr810", "mpr"], required=True)
    ap.add_argument("--base", default="http://localhost:8790")
    ap.add_argument("--system", default="wikibrain")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--query-field", choices=["nl", "formal"], default="nl",
                    help="mpr only: which side of the statement to query with")
    args = ap.parse_args()

    if args.bench == "qr810":
        items = [(r["qid"], r["query"]) for r in qr_rows()]
    else:
        items = [(r["qid"], r["nl"] if args.query_field == "nl" else r["formal"])
                 for r in mpr_rows()]
        if args.query_field == "formal":
            args.system += "-formalq"

    out_dir = HERE / "runs" / "system" / args.bench / args.system
    out_dir.mkdir(parents=True, exist_ok=True)
    done = skipped = 0
    for qid, query in items:
        out = out_dir / f"{qid}.json"
        if out.exists():
            skipped += 1
            continue
        ranked, meta = fetch_ranked(args.base, query, args.k)
        out.write_text(json.dumps({"qid": qid, "query": query, "ranked": ranked,
                                   "meta": meta}) + "\n")
        done += 1
        if done % 50 == 0:
            print(f"  {done} fetched...", file=sys.stderr)
    print(f"{args.bench}/{args.system}: {done} fetched, {skipped} resumed "
          f"-> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
