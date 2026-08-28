#!/usr/bin/env python3
"""Hermetic tests for the Frontier queue suitability policy."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from frontier_suitability import classify_cell, load_overrides


def concept_node(status="not_formalized", *, decls=None, p31=None):
    return {
        "type": "concept",
        "display": {"status": status},
        "unit": {"decls": decls or []},
        "altitude_evidence": {"p31": p31 or []},
    }


def concept_cell(label="Precise object", *qids):
    qids = qids or ("Q1",)
    return {
        "id": "cell:" + qids[0],
        "label": label,
        "organs": [{"kind": "concept", "id": qid} for qid in qids],
    }


class ClassifyCellTest(unittest.TestCase):
    def classify(self, cell, nodes, overrides=None, weight=1, degree=1):
        return classify_cell(cell, nodes, overrides or {},
                             direct_weight=weight, degree=degree)

    def test_precise_unformalized_concept_is_candidate(self):
        self.assertEqual(
            self.classify(concept_cell("Split-complex number"),
                          {"Q1": concept_node()}),
            {"candidate": True, "reason": None},
        )

    def test_current_coverage_is_deprioritized(self):
        for node in (
            concept_node("partial"),
            concept_node("formalized"),
            {"type": "concept", "display": {},
             "unit": {"decls": [{"name": "AlreadyThere", "match_kind": "exact"}]},
             "altitude_evidence": {"p31": []}},
        ):
            with self.subTest(node=node):
                self.assertEqual(
                    self.classify(concept_cell(), {"Q1": node}),
                    {"candidate": False, "reason": "existing_formal_coverage"},
                )

    def test_not_formalized_status_beats_related_decl_hints(self):
        node = concept_node(decls=[{"name": "Nearby", "match_kind": "related"}])
        self.assertEqual(self.classify(concept_cell(), {"Q1": node}),
                         {"candidate": True, "reason": None})

    def test_broad_class_and_label_are_deprioritized(self):
        self.assertEqual(
            self.classify(concept_cell("Ordinary label"),
                          {"Q1": concept_node(p31=["Q1936384"])}),
            {"candidate": False, "reason": "broad_scope"},
        )
        self.assertEqual(
            self.classify(concept_cell("History of widgets"),
                          {"Q1": concept_node()}),
            {"candidate": False, "reason": "broad_scope"},
        )
        for label in ("Hurwitz's theorem (complex analysis)",
                      "Kőnig's theorem (graph theory)",
                      "Euler's theorem in geometry"):
            with self.subTest(label=label):
                self.assertEqual(
                    self.classify(concept_cell(label), {"Q1": concept_node()}),
                    {"candidate": True, "reason": None},
                )

    def test_numeric_and_extreme_hub_cells_are_review_needed(self):
        self.assertEqual(
            self.classify(concept_cell("1000 (number)"), {"Q1": concept_node()}),
            {"candidate": False, "reason": "too_elementary"},
        )
        self.assertEqual(
            self.classify(concept_cell(), {"Q1": concept_node()},
                          weight=500, degree=100),
            {"candidate": False, "reason": "review_needed"},
        )

    def test_multi_concept_cell_stays_candidate_if_one_concept_is_viable(self):
        cell = concept_cell("Mixed atom", "Q1", "Q2")
        nodes = {"Q1": concept_node("partial"), "Q2": concept_node()}
        self.assertEqual(self.classify(cell, nodes),
                         {"candidate": True, "reason": None})

    def test_override_precedes_inference(self):
        node = concept_node("partial", p31=["Q1936384"])
        self.assertEqual(
            self.classify(concept_cell(), {"Q1": node},
                          {"Q1": {"tier": "candidate", "reason": None}}),
            {"candidate": True, "reason": None},
        )
        self.assertEqual(
            self.classify(concept_cell(), {"Q1": concept_node()},
                          {"Q1": {"tier": "deprioritized",
                                  "reason": "ambiguous_scope"}}),
            {"candidate": False, "reason": "ambiguous_scope"},
        )

    def test_cell_without_concept_is_not_actionable(self):
        cell = {"id": "cell:orphan", "label": "Orphan", "organs": []}
        self.assertEqual(self.classify(cell, {}),
                         {"candidate": False, "reason": "no_concept_target"})


class LoadOverridesTest(unittest.TestCase):
    def write(self, *rows):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        with tmp:
            tmp.write(json.dumps({"_meta": {"schema": 1}}) + "\n")
            for row in rows:
                tmp.write(json.dumps(row) + "\n")
        self.addCleanup(Path(tmp.name).unlink, missing_ok=True)
        return Path(tmp.name)

    def test_loads_valid_reviewed_override(self):
        path = self.write({"qid": "Q1", "tier": "deprioritized",
                           "reason": "ambiguous_scope", "rationale": "Reviewed."})
        self.assertEqual(load_overrides(path, {"Q1"})["Q1"]["reason"],
                         "ambiguous_scope")

    def test_rejects_unknown_qid_and_unsorted_or_duplicate_rows(self):
        unknown = self.write({"qid": "Q2", "tier": "candidate",
                              "reason": None, "rationale": "Reviewed."})
        with self.assertRaisesRegex(ValueError, "unknown"):
            load_overrides(unknown, {"Q1"})
        duplicate = self.write(
            {"qid": "Q1", "tier": "candidate", "reason": None,
             "rationale": "Reviewed."},
            {"qid": "Q1", "tier": "candidate", "reason": None,
             "rationale": "Reviewed again."},
        )
        with self.assertRaisesRegex(ValueError, "unique"):
            load_overrides(duplicate, {"Q1"})
        unsorted = self.write(
            {"qid": "Q2", "tier": "candidate", "reason": None,
             "rationale": "Reviewed."},
            {"qid": "Q1", "tier": "candidate", "reason": None,
             "rationale": "Reviewed."},
        )
        with self.assertRaisesRegex(ValueError, "sorted"):
            load_overrides(unsorted, {"Q1", "Q2"})

    def test_rejects_invalid_tier_reason_and_empty_rationale(self):
        cases = [
            {"qid": "Q1", "tier": "maybe", "reason": None,
             "rationale": "Reviewed."},
            {"qid": "Q1", "tier": "deprioritized", "reason": "unknown",
             "rationale": "Reviewed."},
            {"qid": "Q1", "tier": "candidate", "reason": "broad_scope",
             "rationale": "Reviewed."},
            {"qid": "Q1", "tier": "candidate", "reason": None,
             "rationale": ""},
        ]
        for row in cases:
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    load_overrides(self.write(row), {"Q1"})


if __name__ == "__main__":
    unittest.main()
