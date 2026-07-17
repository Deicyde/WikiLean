#!/usr/bin/env python3
"""erdosproblems.com ingest via teorth/erdosproblems (Apache-2.0).

The machine-readable join table for Thomas Bloom's Erdős-problems database is
`data/problems.yaml` in Tao's GitHub mirror — problem number, status
(open/proved/disproved), prize, OEIS A-numbers, topic tags, formalization
state. The website's own prose is NOT redistributed (it is not in the YAML);
we store link facts + our own constructed titles only.

Outputs:
  catalog/data/external/erdos_pages.jsonl   ext contract pages, db "erdos"
      id = problem number, title "Erdős Problem N", kind_hint = status
  catalog/data/erdos_joins.jsonl            committed join table
      {"erdos","status","prize"?,"oeis":[..],"tags":[..],"formalized"?}

Anchoring: erdos pages carry no CC0 Wikidata qid (no Wikidata property yet);
they anchor through decl:FormalConjectures:* -> xref:erdos:<n> edges minted by
build_common's formal-conjectures layer, and through fold-verified agent
anchors. Deterministic, no LLM.

Run: python3 brain/ingest/erdosproblems.py
Env: BRAIN_ERDOS_CHECKOUT (default /Users/jack/Desktop/LEAN/erdosproblems)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

REPO_URL = "https://github.com/teorth/erdosproblems.git"
CHECKOUT = Path(os.environ.get(
    "BRAIN_ERDOS_CHECKOUT", "/Users/jack/Desktop/LEAN/erdosproblems"))
PAGE_URL = "https://www.erdosproblems.com/{}"
JOINS_OUT = common.REPO / "catalog" / "data" / "erdos_joins.jsonl"


def ensure_checkout() -> str:
    if not (CHECKOUT / ".git").exists():
        subprocess.run(["git", "clone", "--depth", "1", REPO_URL, str(CHECKOUT)],
                       check=True)
    else:
        stamp = CHECKOUT / ".git" / "FETCH_HEAD"
        if not stamp.exists() or time.time() - stamp.stat().st_mtime > 20 * 3600:
            r = subprocess.run(["git", "-C", str(CHECKOUT), "pull", "--ff-only"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                print(f"[erdos] pull failed (using existing checkout): "
                      f"{r.stderr.strip()[:200]}", file=sys.stderr)
    out = subprocess.run(["git", "-C", str(CHECKOUT), "rev-parse", "HEAD"],
                         capture_output=True, text=True)
    return out.stdout.strip()


def main() -> int:
    commit = ensure_checkout()
    src = CHECKOUT / "data" / "problems.yaml"
    problems = yaml.safe_load(src.read_text())
    if not isinstance(problems, list) or not problems:
        raise RuntimeError(f"unexpected problems.yaml shape at {src}")

    pages: list[dict] = []
    joins: list[dict] = []
    for p in problems:
        n = str(p.get("number") or "").strip()
        if not n:
            continue
        status = ((p.get("status") or {}).get("state") or "").strip() or None
        pages.append({k: v for k, v in {
            "db": "erdos", "id": n, "title": f"Erdős Problem {n}",
            "url": PAGE_URL.format(n),
            "kind_hint": status,
        }.items() if v is not None})
        prize = (p.get("prize") or "").strip()
        joins.append({k: v for k, v in {
            "erdos": n, "status": status,
            "prize": prize if prize and prize != "no" else None,
            "oeis": p.get("oeis") or None,
            "tags": p.get("tags") or None,
            "formalized": (p.get("formalized") or {}).get("state") or None,
        }.items() if v is not None})

    common.emit("erdos", pages, [], extra_meta={
        "source_pin": f"teorth/erdosproblems data/problems.yaml @ {commit}",
        "source_license": "Apache-2.0 (github.com/teorth/erdosproblems); "
                          "erdosproblems.com prose is Thomas Bloom's and is "
                          "not redistributed — link facts and constructed "
                          "titles only",
    })
    common._volume_guard(JOINS_OUT, "join", len(joins))
    common.write_jsonl(JOINS_OUT, {
        "source": "teorth/erdosproblems data/problems.yaml",
        "license": "Apache-2.0",
        "commit": commit,
        "fetched_at": common.now_iso(),
        "n_problems": len(joins),
    }, joins)
    print(f"[erdos] wrote {len(pages)} pages + {len(joins)} join rows "
          f"@ {commit[:12]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
