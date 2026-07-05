#!/usr/bin/env python3
"""Build a local embedding index over the WikiLean math-QID universe.

Reads catalog/data/wikidata_universe.jsonl (~11.7k rows of
{qid, label, classes:[QID], enwiki_slug}), composes a meaning-bearing embed-text
per QID, embeds it with a LOCAL sentence-transformers model (all-MiniLM-L6-v2,
384-d, CPU-fine), and writes two versioned, atomically-replaced artifacts:

  catalog/data/wikidata_embeddings.npz        float32 (N, d) matrix + qids
  catalog/data/wikidata_embeddings.meta.jsonl  {qid,label,description} per row

These power the `semantic` subcommand of the wikidata-search skill (queried
locally, no network at query time) which Agent 2 reaches through the
`wikidata_semantic` tool. See docs/wikidata-semantic-retrieval.md (Option B).

Design notes / deviations from the sketch:
- The universe rows carry NO Wikidata description and NO enwiki summary. Fetching
  either would be a network call PER ROW (~11.7k calls) which violates the
  offline/fast build contract in the doc ("skip if it needs a fetch-per-row").
  So the embed-text is composed from what we have offline: the label, a
  humanized enwiki slug, and the PARENT-CLASS labels. The class chain is the
  part the doc calls out as decisive ("what separates continuity from its
  broader parents"), and it is fully resolvable offline: the universe references
  only 12 distinct class QIDs, resolved from the universe's own labels plus a
  small static fallback map for the type-classes that never appear as rows.
- The .meta.jsonl `description` field carries the composed embed-text context
  (humanized slug + class labels) so the agent still gets disambiguating prose
  in the tool output even though it is not a Wikidata description string.

Stdlib + numpy + sentence-transformers only. Run offline; nightly rebuilds it
when the universe is newer (see site/ops/nightly-moderate.sh).

Usage:
    catalog/.venv/bin/python3 catalog/build_wikidata_embeddings.py [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

# Artifact schema version — bump when the embed-text composition or model
# changes so a stale .npz is detectable (stored in the .npz + meta header).
INDEX_VERSION = 1
MODEL_NAME = "all-MiniLM-L6-v2"
EMBED_DIM = 384

_HERE = Path(__file__).resolve().parent
DATA = _HERE / "data"
UNIVERSE = DATA / "wikidata_universe.jsonl"
OUT_NPZ = DATA / "wikidata_embeddings.npz"
OUT_META = DATA / "wikidata_embeddings.meta.jsonl"

# Fallback labels for the type-class QIDs that the universe references but never
# lists as rows (so they can't be resolved from the universe's own labels).
# There are only a handful; resolved once via the wikidata-search skill's
# `entity` subcommand and baked in to keep the build fully offline. If a new
# class QID appears in the universe and is not here, it is simply omitted from
# the embed-text (the label + slug still carry meaning) — no build failure.
CLASS_LABEL_FALLBACK = {
    "Q65943": "theorem",
    "Q24034552": "mathematical concept",
    "Q976981": "formula",
    "Q1936384": "branch of mathematics",
    "Q319141": "conjecture",
    "Q1166625": "mathematical problem",
    "Q186509": "mathematical constant",
    "Q6498784": "mathematical expression",
    "Q21550639": "geometric concept",
    "Q11538": "mathematical proof",
    "Q20026918": "mathematical theory",
    "Q207505": "lemma",
}


def _humanize_slug(slug: str) -> str:
    """'Fermat's_little_theorem' -> 'Fermat's little theorem'. Best-effort."""
    if not slug:
        return ""
    return slug.replace("_", " ").strip()


def load_universe(limit: int | None = None) -> list[dict]:
    if not UNIVERSE.exists():
        sys.exit(f"error: {UNIVERSE} not found")
    rows = []
    with UNIVERSE.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("qid") and r.get("label"):
                rows.append(r)
            if limit and len(rows) >= limit:
                break
    return rows


def build_class_label_map(rows: list[dict]) -> dict[str, str]:
    """QID -> label, preferring the universe's own labels, then the fallback."""
    m = {r["qid"]: r["label"] for r in rows if r.get("qid") and r.get("label")}
    out = dict(CLASS_LABEL_FALLBACK)
    out.update(m)  # a QID that IS a row wins over the static fallback
    return out


def compose_embed_text(row: dict, class_label: dict[str, str]) -> tuple[str, str]:
    """Return (embed_text, context) for a universe row.

    embed_text is what we embed; context is the human-readable disambiguator
    stored in the meta as `description`.
    """
    label = (row.get("label") or "").strip()
    slug_human = _humanize_slug(row.get("enwiki_slug") or "")
    classes = row.get("classes") or []
    class_names = []
    for c in classes:
        name = class_label.get(c)
        if name and name.lower() != label.lower() and name not in class_names:
            class_names.append(name)

    # embed-text: label, then the humanized slug (adds surface phrasing when it
    # differs from the label), then the parent-class labels (the type signal).
    parts = [label]
    if slug_human and slug_human.lower() != label.lower():
        parts.append(slug_human)
    if class_names:
        parts.append("(" + ", ".join(class_names) + ")")
    embed_text = " — ".join(parts)

    # context / meta description: a short prose disambiguator for the agent.
    ctx_bits = []
    if class_names:
        ctx_bits.append(", ".join(class_names))
    if slug_human and slug_human.lower() != label.lower():
        ctx_bits.append(f"enwiki: {slug_human}")
    context = "; ".join(ctx_bits)
    return embed_text, context


def _atomic_write_bytes(path: Path, writer) -> None:
    """Write via a temp file in the same dir + os.replace (atomic on POSIX)."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            writer(fh)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _atomic_write_text(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Build the local Wikidata math-QID embedding index.")
    ap.add_argument("--limit", type=int, default=None, help="embed only the first N rows (debug)")
    ap.add_argument("--batch-size", type=int, default=256, help="encode batch size")
    args = ap.parse_args(argv)

    t0 = time.time()
    rows = load_universe(args.limit)
    if not rows:
        sys.exit("error: no rows loaded from universe")
    print(f"loaded {len(rows)} rows from {UNIVERSE.name}", file=sys.stderr)

    class_label = build_class_label_map(rows)
    texts, meta = [], []
    for r in rows:
        embed_text, context = compose_embed_text(r, class_label)
        texts.append(embed_text)
        meta.append({"qid": r["qid"], "label": r["label"], "description": context})

    # Import here so --help works without the (heavy) model dependency loaded.
    try:
        from sentence_transformers import SentenceTransformer
    except Exception as e:  # pragma: no cover - env guard
        sys.exit(f"error: sentence-transformers not available: {e}\n"
                 f"install with: {sys.prefix}/bin/pip install sentence-transformers")

    print(f"loading model {MODEL_NAME} …", file=sys.stderr)
    model = SentenceTransformer(MODEL_NAME)

    print(f"embedding {len(texts)} texts …", file=sys.stderr)
    emb = model.encode(
        texts,
        batch_size=args.batch_size,
        normalize_embeddings=True,   # cosine == dot product at query time
        show_progress_bar=True,
        convert_to_numpy=True,
    ).astype(np.float32)

    if emb.shape != (len(rows), EMBED_DIM):
        sys.exit(f"error: unexpected embedding shape {emb.shape}, "
                 f"expected {(len(rows), EMBED_DIM)}")

    qids = np.array([m["qid"] for m in meta], dtype=object)

    # Write the matrix (+ qids + a tiny header) atomically.
    def _write_npz(fh):
        np.savez(
            fh,
            embeddings=emb,
            qids=qids,
            model=np.array(MODEL_NAME),
            version=np.array(INDEX_VERSION),
            dim=np.array(EMBED_DIM),
        )
    _atomic_write_bytes(OUT_NPZ, _write_npz)

    meta_text = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in meta)
    _atomic_write_text(OUT_META, meta_text)

    dt = time.time() - t0
    print(f"wrote {OUT_NPZ.name}  shape={emb.shape} dtype={emb.dtype}", file=sys.stderr)
    print(f"wrote {OUT_META.name}  rows={len(meta)}", file=sys.stderr)
    print(f"done in {dt:.1f}s  (model={MODEL_NAME}, version={INDEX_VERSION})", file=sys.stderr)


if __name__ == "__main__":
    main()
