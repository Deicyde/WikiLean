"""Functional parity and closed-input regressions for the captured proposal fold."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import proposal_fold_adapter as core
import fold_proposals as legacy
from test_fold_proposals import patched_fold_fixture

ROOT = Path(__file__).resolve().parent.parent
PROGRAM = (ROOT / "brain/fold_proposals.py").read_bytes()


def jl(rows):
    return b"".join(json.dumps(row, ensure_ascii=False).encode() + b"\n" for row in rows)


def fixture():
    proposals = [
        {"qid": "Q1", "path": "Mathlib/Test", "evidence": "container", "confidence": "high"},
        {"qid": "Q1", "decl": "Oracle.ok", "qid_label": "One", "evidence": "exact", "verdict": "accept"},
        {"qid": "Q2", "decl": "rescued", "qid_label": "Two", "evidence": "source"},
        {"action": "fc_link", "qid": "Q1", "decl": "short.good", "kind": "mentions", "evidence": "FC"},
        {"action": "fc_link", "qid": "Q2", "decl": "short.bad", "kind": "mentions", "evidence": "rejected"},
        {"action": "override", "qid": "Q1", "set": {"status": "partial"}, "reason": "grade", "verdict": "accept"},
        {"action": "ok", "qid": "Q1", "verdict": "reject", "verify_note": "dispute"},
    ]
    verified = [{**proposals[4], "verdict": "reject", "verify_note": "bad claim"}]
    files = {
        "brain/proposals/fixture.jsonl": jl(proposals),
        "brain/proposals/fixture.jsonl.verified.jsonl": jl(verified),
        "catalog/data/rebuild_grounding.json": json.dumps([{"qid": "Q1", "formalizations": []}]).encode(),
        "catalog/data/hierarchy.json": b'{"libraries":{"Mathlib":{"modules":{"Test":{"n_decls":2}}}}}',
        "catalog/data/source_registry.json": b'{"crossref_sources":{},"frontier_sources":{"formal_conjectures":{}}}',
        "catalog/data/wikidata_universe.jsonl": jl([{"qid": "Q1", "label": "One"}, {"qid": "Q2", "label": "Two"}]),
        "catalog/data/universe_extension.jsonl": b'\r\n',
        "catalog/data/grounding_overrides.jsonl": b'\r\n',
        "catalog/data/formal_conjectures.jsonl": jl([{"_meta": {}}, {"decl": "Namespace.short.good"}, {"decl": "Namespace.short.bad"}]),
        "oracle.json": b'{"declarations":{"Oracle.ok":{"docLink":"./Mathlib/Test.html#ok"}}}',
    }
    mathlib = {"Mathlib/Test.lean": b"theorem rescued : True := by trivial\n"}
    return files, mathlib


class FoldAdapterTest(unittest.TestCase):
    def test_complete_legacy_parity_with_overlay_rescue_retraction_and_overrides(self):
        files, mathlib = fixture()
        seed = jl([{"qid": "Q2", "decl": "Namespace.short.bad", "kind": "mentions", "evidence": "old"}])
        outputs, audit = core.fold(PROGRAM, files, mathlib, comparison_fc_seed=seed)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            for name, raw in {**files, **mathlib, "brain/data/fc_links.jsonl": seed}.items():
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
            with patched_fold_fixture(root / "catalog/data", root / "brain/proposals", root / "brain/data", root / "Mathlib", root / "oracle.json"), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(legacy, "known_qids", return_value={"Q1": {"label": "One"}, "Q2": {"label": "Two"}}):
                self.assertEqual(legacy.main(["--write-wikidata-request-plan", str(root / "request-plan.json")]), 0)
                self.assertEqual(legacy.main([]), 0)
            for path, raw in outputs.items():
                self.assertEqual(raw, (root / path).read_bytes(), path)
        unseeded, _ = core.fold(PROGRAM, files, mathlib)
        self.assertEqual(outputs, unseeded)
        self.assertEqual(audit["source_rescue_matches"], {"rescued": ["Mathlib/Test.lean"]})
        self.assertEqual(len(core.rows(outputs["brain/data/discovery_proposals.jsonl"])), 2)
        self.assertEqual(core.rows(outputs["brain/data/fc_links.jsonl"])[1]["decl"], "Namespace.short.good")
        self.assertTrue(outputs["catalog/data/grounding_overrides.jsonl"].startswith(b"\r\n"))

    def test_unknown_entity_requires_evidence_instead_of_zero(self):
        files, mathlib = fixture()
        files["brain/proposals/fixture.jsonl"] += jl([{"qid": "Q3", "decl": "Oracle.ok"}])
        with self.assertRaisesRegex(core.FoldError, "empty entity request plan"):
            core.fold(PROGRAM, files, mathlib)

    def test_seed_or_unbound_input_is_refused(self):
        files, mathlib = fixture()
        files["brain/data/fc_links.jsonl"] = b""
        with self.assertRaisesRegex(core.FoldError, "undeclared fold input"):
            core.fold(PROGRAM, files, mathlib)

    def test_unsupported_xref_repo_and_orphan_skeptic_reject_before_fold(self):
        for action in ("xref", "repo_link"):
            files, mathlib = fixture()
            files["brain/proposals/fixture.jsonl"] += jl([{"action": action}])
            with self.assertRaisesRegex(core.FoldError, "external/repository"):
                core.fold(PROGRAM, files, mathlib)
        files, mathlib = fixture()
        files["brain/proposals/orphan.jsonl.verified.jsonl"] = b""
        with self.assertRaisesRegex(core.FoldError, "orphan skeptic"):
            core.fold(PROGRAM, files, mathlib)

    def test_no_filesystem_or_network_is_available_to_algorithm(self):
        files, mathlib = fixture()
        with mock.patch("builtins.open", side_effect=AssertionError("ambient read")), \
                mock.patch("socket.socket", side_effect=AssertionError("network")), \
                mock.patch("subprocess.run", side_effect=AssertionError("process")):
            outputs, _ = core.fold(PROGRAM, files, mathlib)
        self.assertEqual(outputs["request-plan.json"], core.EMPTY_PLAN)

    def test_new_import_inside_selected_algorithm_is_rejected(self):
        files, mathlib = fixture()
        changed = PROGRAM.replace(b'    args = _parse_args(argv)', b'    import socket\n    args = _parse_args(argv)')
        with self.assertRaisesRegex(core.FoldError, "ambient import"):
            core.fold(changed, files, mathlib)


if __name__ == "__main__":
    unittest.main()
