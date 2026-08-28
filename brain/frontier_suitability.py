#!/usr/bin/env python3
"""Deterministic suitability policy for the Frontier queue.

Structural Frontier membership is defined elsewhere: every cell without a decl organ
belongs to exactly one Frontier area. This module answers a separate product question:
should that cell be presented as an actionable formalization candidate, or retained as
a review-needed row below the candidates?
"""
from __future__ import annotations

import json
import re
from pathlib import Path

TIERS = {"candidate", "deprioritized"}
REASONS = {
    "existing_formal_coverage",
    "not_formalization_target",
    "broad_scope",
    "ambiguous_scope",
    "too_elementary",
    "review_needed",
    "no_concept_target",
}
REASON_PRECEDENCE = (
    "existing_formal_coverage",
    "not_formalization_target",
    "broad_scope",
    "ambiguous_scope",
    "too_elementary",
    "review_needed",
    "no_concept_target",
)

# Existing offline field/discipline policy plus the historical-overview class used
# by the Frontier review worklist. P31 inference stays deliberately narrow; reviewed
# edge cases belong in the override file.
BROAD_CLASSES = {
    "Q1936384",   # branch of mathematics
    "Q11862829",  # academic discipline
    "Q2267705",   # field of study
    "Q4671286",   # academic major
    "Q1047113",   # specialty
    "Q20026918",  # mathematical theory
    "Q17524420",  # aspect of history
}
NON_TARGET_CLASSES = {"Q5"}  # human
BROAD_LABEL_RE = re.compile(
    r"^(?:history of|timeline of|list of|outline of)\b"
    r"|^(?:pure |discrete )?mathematics$"
    r"|^(?:real )?analysis$"
    r"|^(?:geometry|algebra|calculus)$",
    re.I,
)
NUMERIC_LABEL_RE = re.compile(r"^[+\-−]?\d+(?:\s*\(number\))?$")
HIGH_PROXIMITY_WEIGHT = 500
HIGH_PROXIMITY_DEGREE = 100


def iter_jsonl(path: Path):
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if "_meta" in row and len(row) == 1:
                continue
            yield row


def load_overrides(path: Path, known_qids: set[str]) -> dict[str, dict]:
    """Load reviewed QID overrides, failing closed on malformed policy data."""
    if not path.exists():
        raise ValueError(f"missing Frontier suitability overrides: {path}")
    out: dict[str, dict] = {}
    previous_qid_number = 0
    for row in iter_jsonl(path):
        qid = row.get("qid")
        tier = row.get("tier")
        reason = row.get("reason")
        rationale = row.get("rationale")
        if not isinstance(qid, str) or not re.fullmatch(r"Q[1-9]\d*", qid):
            raise ValueError(f"invalid Frontier suitability override QID: {qid!r}")
        qid_number = int(qid[1:])
        if qid_number <= previous_qid_number:
            raise ValueError("Frontier suitability overrides must be unique and "
                             f"numerically QID-sorted: {qid}")
        previous_qid_number = qid_number
        if qid not in known_qids:
            raise ValueError(f"unknown Frontier suitability override QID: {qid}")
        if tier not in TIERS:
            raise ValueError(f"invalid Frontier suitability tier for {qid}: {tier!r}")
        if tier == "candidate":
            if reason is not None:
                raise ValueError(f"candidate override {qid} must have null reason")
        elif reason not in REASONS - {"no_concept_target"}:
            raise ValueError(f"invalid Frontier suitability reason for {qid}: {reason!r}")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError(f"Frontier suitability override {qid} needs a rationale")
        out[qid] = {"tier": tier, "reason": reason, "rationale": rationale.strip()}
    return out


def review_signals(
    qid: str | None,
    label: str,
    classes: set[str],
    secondary_only: set[str],
    direct_weight: int,
    degree: int,
) -> list[str]:
    """Return non-gating diagnostic signals for the wrong-altitude worklist."""
    signals = []
    if qid in secondary_only:
        signals.append("secondary_only")
    if qid and BROAD_CLASSES & classes:
        signals.append("broad_wikidata_class")
    if BROAD_LABEL_RE.search(label):
        signals.append("broad_label")
    if direct_weight >= HIGH_PROXIMITY_WEIGHT and degree >= HIGH_PROXIMITY_DEGREE:
        signals.append("high_proximity_hub")
    return signals


def _inferred_reason(node: dict) -> str | None:
    display = node.get("display") or {}
    status = display.get("status")
    if status in {"partial", "formalized"}:
        return "existing_formal_coverage"
    # Current status is authoritative. A not_formalized concept can still carry
    # related/invocation declaration hints; those are navigation, not coverage.
    if status is None and any(
        decl.get("match_kind") in {"exact", "generalization", "special_case"}
        for decl in (node.get("unit") or {}).get("decls") or []
    ):
        return "existing_formal_coverage"
    classes = set((node.get("altitude_evidence") or {}).get("p31") or [])
    if classes & BROAD_CLASSES:
        return "broad_scope"
    if classes & NON_TARGET_CLASSES:
        return "not_formalization_target"
    return None


def classify_cell(
    cell: dict,
    nodes: dict[str, dict],
    overrides: dict[str, dict],
    *,
    direct_weight: int,
    degree: int,
) -> dict[str, str | bool | None]:
    """Classify one decl-less cell without changing its structural membership."""
    qids = [o.get("id") for o in cell.get("organs", [])
            if o.get("kind") == "concept" and o.get("id")]
    if not qids:
        return {"candidate": False, "reason": "no_concept_target"}

    label = cell.get("label") or cell.get("id") or ""
    inferred = []
    for qid in qids:
        override = overrides.get(qid)
        if override:
            inferred.append(override["reason"] if override["tier"] == "deprioritized"
                            else None)
            continue
        reason = _inferred_reason(nodes.get(qid) or {})
        if reason is None and BROAD_LABEL_RE.search(label):
            reason = "broad_scope"
        if reason is None and NUMERIC_LABEL_RE.fullmatch(label.strip()):
            reason = "too_elementary"
        if reason is None and direct_weight >= HIGH_PROXIMITY_WEIGHT \
                and degree >= HIGH_PROXIMITY_DEGREE:
            reason = "review_needed"
        inferred.append(reason)

    # A multi-concept cell stays actionable if any one of its concept organs is a
    # viable target. Deprioritizing the whole atom requires every organ to agree.
    if any(reason is None for reason in inferred):
        return {"candidate": True, "reason": None}
    reason_order = {reason: i for i, reason in enumerate(REASON_PRECEDENCE)}
    reason = min(inferred, key=lambda value: reason_order[value])
    return {"candidate": False, "reason": reason}
