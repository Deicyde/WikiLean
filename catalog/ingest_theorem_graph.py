#!/usr/bin/env python3
"""Ingest TheoremGraph's judge-affirmed (formal Mathlib decl ↔ informal arXiv
statement) matches as a per-QID arXiv literature layer for the concept graph.

Source: uw-math-ai/theorem-matching (HuggingFace) — the curated match table
behind TheoremGraph (arXiv:2606.25363). License **CC-BY-SA-4.0** (stricter than
the parent math-graph's CC-BY-4.0). We store only the LINK FACTS (decl ↔
arxiv_id / informal_ref / paper_title + the judges' verdicts), never the papers'
copyrightable text, and attribute the source; WikiLean's own data stays CC0.

Join: TheoremGraph gives (formal_decl → arXiv statement); WikiLean nodes give
(QID → Mathlib decl) via the concept graph's primary_decl AND every decl cited
in that article's annotations. We join on the shared Mathlib decl name, so each
concept gains "this result is stated as Thm X.Y in arXiv:… " links.

Anti-slop: `theorem_matching.csv` is the FULL candidate sweep (sim ≥ 0.8), ~80%
of which its own judges reject. We keep only the **paper-affirmed** slice
(GPT-5.4 judge label ∈ {exact, inexact} — the paper's own 47.7% bar), and carry
BOTH judges' labels + the similarity band per link so the UI can badge
exact-vs-inexact and a human can audit. LLMs propose, humans publish.

Run:
  python3 catalog/ingest_theorem_graph.py --revision <40-hex-commit>
  python3 catalog/ingest_theorem_graph.py --revision <commit> --download
  python3 catalog/ingest_theorem_graph.py --revision <commit> --adopt-existing
  python3 catalog/ingest_theorem_graph.py --revision <commit> --tier exact

The revision may instead be supplied through
WIKILEAN_THEOREM_MATCHING_REVISION. Branches and tags (including main) are
rejected, and an existing cache is accepted only with a matching sidecar.
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import json
import os
from pathlib import Path

from huggingface_download import (
    ArtifactRequest,
    HuggingFaceArtifactError,
    ReviewedDatasetPin,
    adopt_existing_artifacts,
    fetch_huggingface_artifacts,
    load_reviewed_pin,
    require_reviewed_revision,
    resolve_revision,
    verified_artifact_set,
)

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
CACHE = HERE / ".cache" / "theorem_matching.csv"
CONCEPT_GRAPH = DATA / "concept_graph.json"
ANNOT = HERE.parent / "site" / "annotations"
OUT = DATA / "theoremgraph_links.json"

DATASET = "uw-math-ai/theorem-matching"
REMOTE_FILE = "theorem_matching.csv"
REVISION_ENV = "WIKILEAN_THEOREM_MATCHING_REVISION"
UA = "WikiLean-theoremgraph-ingest/1.0 (https://wikilean.jackmccarthy.org; jack.mccarthy.1@stonybrook.edu)"

AFFIRM = {"exact", "inexact"}          # the paper's GPT-5.4 "match" bar (47.7% globally)
MAX_LINKS_PER_QID = 12                  # keep the map artifact + panel readable
csv.field_size_limit(10 ** 9)


def source_request(pin: ReviewedDatasetPin) -> ArtifactRequest:
    return pin.request(REMOTE_FILE, CACHE)


def download(
    pin: ReviewedDatasetPin,
    revision: str,
) -> dict[str, object]:
    print(
        f"downloading {DATASET} {REMOTE_FILE} at immutable revision "
        f"{revision} (~108MB) …"
    )
    result = fetch_huggingface_artifacts(
        dataset=DATASET,
        revision=revision,
        requests=[source_request(pin)],
        user_agent=UA,
        force=True,
        timeout_seconds=300,
    )[0]
    return result.metadata


def wikilean_decls() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """→ (primary: decl→{qid}, cited: decl→{qid}) over the concept layer."""
    g = json.loads(CONCEPT_GRAPH.read_text())
    primary: dict[str, set[str]] = collections.defaultdict(set)
    slug2qid = {n["slug"]: n["qid"] for n in g["nodes"] if n.get("slug")}
    for n in g["nodes"]:
        if n.get("primary_decl") and n.get("qid"):
            primary[n["primary_decl"]].add(n["qid"])
    cited: dict[str, set[str]] = collections.defaultdict(set)
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
                cited[dec].add(qid)
    return primary, cited


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--download", action="store_true", help="(re)fetch the CSV"
    )
    mode.add_argument(
        "--adopt-existing",
        action="store_true",
        help="write a sidecar only after the cache matches the reviewed hash",
    )
    ap.add_argument(
        "--revision",
        default=os.environ.get(REVISION_ENV),
        help=f"exact 40-hex Hugging Face dataset commit (or {REVISION_ENV})",
    )
    ap.add_argument("--tier", choices=["affirmed", "exact"], default="affirmed",
                    help="affirmed = gpt54 ∈ {exact,inexact} (paper bar); exact = gpt54==exact")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        pin = load_reviewed_pin(DATASET)
        revision = require_reviewed_revision(
            resolve_revision(
                args.revision, environment_variable=REVISION_ENV
            ),
            pin,
        )
        request = source_request(pin)
        if args.adopt_existing:
            result = adopt_existing_artifacts(
                dataset=DATASET,
                revision=revision,
                requests=[request],
            )[0]
            print(
                f"adopted/verified {result.destination.name} "
                f"({int(result.metadata['size']) / 1e6:.0f} MB, "
                f"revision {revision})"
            )
            return 0
        elif args.download or not CACHE.exists():
            if not CACHE.exists() and not args.download:
                print(
                    f"no cached CSV at {CACHE}; fetching exact revision "
                    f"{revision} (pass --download to force refresh)"
                )
            download(pin, revision)
        with verified_artifact_set(
            dataset=DATASET,
            revision=revision,
            requests=[request],
        ) as verified:
            source_metadata = verified[0]
            print(
                f"verified cached {REMOTE_FILE} at immutable revision "
                f"{revision}"
            )
            return ingest(args, source_metadata)
    except HuggingFaceArtifactError as exc:
        raise SystemExit(f"FATAL: {exc}") from exc


def ingest(
    args: argparse.Namespace,
    source_metadata: dict[str, object],
) -> int:
    primary, cited = wikilean_decls()
    all_decls = set(primary) | set(cited)
    print(f"WikiLean decls: {len(primary)} primary / {len(all_decls)} total (primary ∪ cited)")

    keep = (lambda lbl: lbl in AFFIRM) if args.tier == "affirmed" else (lambda lbl: lbl == "exact")

    # decl → best affirmed match (the CSV is already rank-1 per formal_decl).
    links: dict[str, list[dict]] = collections.defaultdict(list)
    seen: dict[str, set[tuple[str, str]]] = collections.defaultdict(set)
    n_rows = n_aff = n_joined = 0
    with CACHE.open(newline="") as fh:
        for r in csv.DictReader(fh):
            n_rows += 1
            if not keep(r.get("gpt54_label", "")):
                continue
            n_aff += 1
            decl = r["formal_decl"]
            if decl not in all_decls:
                continue
            link = {
                "decl": decl,
                "arxiv_id": r["arxiv_id"],
                "ref": r.get("informal_ref") or "",
                "title": r.get("paper_title") or "",
                "sim": round(float(r["sim"]), 3) if r.get("sim") else None,
                "gpt54": r.get("gpt54_label"),
                "deepseek": r.get("deepseek_label"),
            }
            for qid in (primary.get(decl, set()) | cited.get(decl, set())):
                key = (decl, r["arxiv_id"])
                if key in seen[qid]:
                    continue
                seen[qid].add(key)
                links[qid].append({**link, "primary": qid in primary.get(decl, set())})
                n_joined += 1

    # sort each concept's links: primary decl first, exact before inexact, then sim.
    order = {"exact": 0, "inexact": 1}
    for qid, ls in links.items():
        ls.sort(key=lambda x: (not x["primary"], order.get(x["gpt54"], 9), -(x["sim"] or 0)))
        del ls[MAX_LINKS_PER_QID:]

    out = {
        "_meta": {
            "source": "uw-math-ai/theorem-matching (TheoremGraph)",
            "paper": "arXiv:2606.25363",
            "license": "CC-BY-SA-4.0",
            "attribution": "Matches from TheoremGraph (Math-Graph / theorem-matching, "
                           "UW Math-AI), arXiv:2606.25363, CC-BY-SA-4.0. Stored as link "
                           "facts only; arXiv papers retain their own licenses.",
            "tier": args.tier,
            "affirm_labels": sorted(AFFIRM) if args.tier == "affirmed" else ["exact"],
            "source_revision": source_metadata["revision"],
            "source_url": source_metadata["file_url"],
            "source_sha256": source_metadata["sha256"],
            "source_bytes": source_metadata["size"],
            "n_concepts": len(links),
            "n_links": sum(len(v) for v in links.values()),
        },
        "links": {q: links[q] for q in sorted(links)},
    }
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    tmp.replace(OUT)
    n_exact = sum(1 for v in links.values() for x in v if x["gpt54"] == "exact")
    print(f"rows={n_rows} affirmed={n_aff} joined_links={n_joined}")
    print(f"wrote {OUT.name}: {out['_meta']['n_links']} links across "
          f"{out['_meta']['n_concepts']} concepts ({n_exact} gpt54-exact) "
          f"[tier={args.tier}, {OUT.stat().st_size / 1024:.0f} KB]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
