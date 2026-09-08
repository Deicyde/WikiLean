"""Compare the bounded halo adapter with the exact full legacy program."""
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent / "tools"))
import legacy_halo_projection as projection

PROGRAM = Path(__file__).parent / "authority/fixtures/legacy-halo-ebac34dc.py.txt"


def jsonl(rows):
    return ("\n".join(json.dumps(row) for row in rows) + "\n").encode()


class LegacyHaloTest(unittest.TestCase):
    def inputs(self):
        cells = [{"_meta": {"generated_at": "original-baseline-clock"}},
            {"id": "cell:formal", "organs": [{"kind": "decl", "id": "decl:Mathlib:A"}]},
            {"id": "cell:page", "label": "Page", "anchor": "Q1", "organs": [{"kind": "page", "db": "nlab"}]},
            {"id": "cell:other", "organs": [{"kind": "concept", "id": "Q2"}]},
            {"id": "cell:third", "organs": [{"kind": "concept", "id": "Q3"}]},
            {"id": "cell:isolated", "label": "Isolated", "organs": [{"kind": "page", "db": "oeis"}]}]
        synapses = [{"_meta": {}},
            {"src": "cell:page", "dst": "cell:formal", "kinds": {"depends": 1}},
            {"src": "cell:other", "dst": "cell:page", "kinds": {"links": 3}},
            {"src": "cell:page", "dst": "cell:third", "kinds": {"links": 1}},
            {"src": "cell:page", "dst": "path:Mathlib", "kinds": {"contains": 1}}]
        return jsonl(cells), jsonl(synapses)

    def reference(self, root, centrality):
        # Execute the complete retained program without its CLI. Its sweep
        # dependency is unavailable, proving these pure calls do not need it.
        namespace = {"__file__": str(root / "manage/halo.py"), "__name__": "legacy_halo_reference"}
        before = list(sys.path)
        try:
            with mock.patch.dict(sys.modules, {"decl_existence_sweep": types.ModuleType("decl_existence_sweep")}):
                exec(compile(PROGRAM.read_bytes(), str(PROGRAM), "exec"), namespace)
            cells, _ = namespace["load_brain"]()
            all_nb, dep_nb, _ = namespace["load_adjacency"]()
            return namespace["build_ring"](cells, all_nb, dep_nb, centrality)
        finally:
            sys.path[:] = before

    def test_exact_full_legacy_program_parity_and_current_frontier_fractions(self):
        cells, synapses = self.inputs()
        raw, report = projection.project(PROGRAM.read_bytes(), cells, synapses)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); data = root / "brain/data"; data.mkdir(parents=True)
            (data / "cells.jsonl").write_bytes(cells); (data / "synapses.jsonl").write_bytes(synapses)
            reference = self.reference(root, {})
            self.assertEqual(json.loads(raw), {"items": reference})
            with_scores = self.reference(root, {"Q1": {"pagerank": 0.97, "centrality_pct": 0.8}})
            self.assertEqual({r["cell"]: r["all_frac"] for r in reference}, {r["cell"]: r["all_frac"] for r in with_scores})
        rows = {r["cell"]: r for r in json.loads(raw)["items"]}
        self.assertEqual(set(rows), {"cell:page", "cell:isolated"})
        self.assertEqual(rows["cell:page"]["all_frac"], 0.3333)
        self.assertEqual(rows["cell:page"]["depends_frac"], 1.0)
        self.assertIsNone(rows["cell:isolated"]["all_frac"])
        self.assertFalse(report["authority"]); self.assertFalse(report["baseline_approved"])
        self.assertEqual(report["counts"]["cell_cell_synapses"], 3)

    def test_rejects_unreviewed_program_and_changed_expected_input_bytes(self):
        cells, synapses = self.inputs()
        with self.assertRaisesRegex(ValueError, "reviewed exact commit"):
            projection.project(PROGRAM.read_bytes() + b"\n", cells, synapses)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cells.jsonl"; path.write_bytes(cells)
            with self.assertRaisesRegex(ValueError, "expected SHA"):
                projection.read_exact(path, "0" * 64, 10000)
            linked = Path(tmp) / "link"; linked.symlink_to(path)
            with self.assertRaises(OSError): projection.read_exact(linked, projection.sha(cells), 10000)

    def test_real_cli_emits_only_projection_and_separate_diagnostic_report(self):
        cells, synapses = self.inputs()
        expected, _ = projection.project(PROGRAM.read_bytes(), cells, synapses)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, raw in (("cells", cells), ("synapses", synapses)):
                (root / (name + ".jsonl")).write_bytes(raw)
            args = [sys.executable, "-I", projection.__file__, "--program", str(PROGRAM)]
            for name, raw in (("cells", cells), ("synapses", synapses)):
                args.extend(["--" + name, str(root / (name + ".jsonl")), "--" + name + "-sha256", projection.sha(raw)])
            run = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, expected)
            report = json.loads(run.stderr)
            self.assertFalse(report["baseline_approved"])
            self.assertEqual(report["projection"], {"sha256": projection.sha(run.stdout), "bytes": len(run.stdout)})


if __name__ == "__main__":
    unittest.main()
