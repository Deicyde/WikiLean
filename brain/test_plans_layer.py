#!/usr/bin/env python3
"""Offline tests of the plans layer (brain/plans_layer.py) and its build plumbing.

The export under test is the real producer's: scripts/tauceti_plans builds it from its own
fixtures in a temporary directory (local git only), so a change to the producer's bytes or
logical_root shows up here as well as in the producer's tests.

Run: python3 brain/test_plans_layer.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import build_common as bc  # noqa: E402
import build_snapshot  # noqa: E402
import plans_layer as pl  # noqa: E402
from scripts.tauceti_plans import build as producer  # noqa: E402
from scripts.tauceti_plans.test_build import make_inputs  # noqa: E402

ALPHA = "tauceti:TauCetiRoadmap/Alpha"
BETA = "tauceti:TauCetiRoadmap/Beta"
GAMMA = "tauceti:Completed/Gamma"
A_L0 = f"{ALPHA}#layer-0-abhyankars-lemma"
A_L1 = f"{ALPHA}#layer-1--second"
A_L2 = f"{ALPHA}#layer-2-third"
B_L0 = f"{BETA}#layer-0-base"


def setUpModule() -> None:
    global TMP, EXPORT
    TMP = Path(tempfile.mkdtemp(prefix="plans-layer-test-"))
    inputs = make_inputs(str(TMP))
    EXPORT = TMP / "export"
    producer.build(inputs, str(EXPORT))


def tearDownModule() -> None:
    shutil.rmtree(TMP, ignore_errors=True)


def copy_export(dest: Path) -> Path:
    shutil.copytree(EXPORT, dest)
    return dest


def reseal(directory: Path) -> None:
    """Recompute a (deliberately edited) export's file entries and logical_root."""
    manifest = json.loads((directory / pl.MANIFEST).read_text())
    for name in pl.EXPORT_FILES:
        data = (directory / name).read_bytes()
        manifest["files"][name] = {
            "bytes": len(data), "rows": data.count(b"\n"),
            "sha256": pl.hashlib.sha256(data).hexdigest()}
    manifest["logical_root"] = pl.logical_root(manifest)
    (directory / pl.MANIFEST).write_text(json.dumps(manifest))


def rewrite_rows(directory: Path, name: str, edit) -> None:
    rows = [json.loads(line) for line in (directory / name).read_text().splitlines()]
    rows = edit(rows)
    (directory / name).write_bytes(pl.jsonl_bytes(sorted(rows, key=lambda r: r["id"])))
    reseal(directory)


class LoadExport(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_producers_export_verifies_with_the_same_root(self) -> None:
        export = pl.load_export(EXPORT)
        manifest = json.loads((EXPORT / pl.MANIFEST).read_text())
        self.assertEqual(export.logical_root, manifest["logical_root"])
        self.assertEqual(export.logical_root, producer.logical_root(manifest))
        self.assertEqual(len(export.rows["roadmaps.jsonl"]), 3)

    def test_no_directory_means_no_export(self) -> None:
        self.assertIsNone(pl.load_export(self.tmp / "absent"))

    def test_an_extra_or_missing_file_is_refused(self) -> None:
        d = copy_export(self.tmp / "extra")
        (d / "PINS.json").write_text("{}")
        with self.assertRaisesRegex(pl.PlansExportError, "unexpected"):
            pl.load_export(d)
        d = copy_export(self.tmp / "missing")
        (d / "xrefs.jsonl").unlink()
        with self.assertRaisesRegex(pl.PlansExportError, "missing"):
            pl.load_export(d)

    def test_a_changed_byte_is_refused(self) -> None:
        d = copy_export(self.tmp / "tampered")
        data = (d / "layers.jsonl").read_bytes().replace(b'"done"', b'"todo"', 1)
        (d / "layers.jsonl").write_bytes(data)
        with self.assertRaisesRegex(pl.PlansExportError, "layers.jsonl does not match"):
            pl.load_export(d)

    def test_a_manifest_edit_without_a_new_root_is_refused(self) -> None:
        d = copy_export(self.tmp / "root")
        manifest = json.loads((d / pl.MANIFEST).read_text())
        manifest["counts"]["layers_by_state"]["done"] = 99
        (d / pl.MANIFEST).write_text(json.dumps(manifest))
        with self.assertRaisesRegex(pl.PlansExportError, "logical_root"):
            pl.load_export(d)

    def test_a_refetch_with_new_clocks_keeps_the_root(self) -> None:
        d = copy_export(self.tmp / "clocks")
        manifest = json.loads((d / pl.MANIFEST).read_text())
        for pin in manifest["inputs"].values():
            if isinstance(pin, dict) and "fetched_at" in pin:
                pin["fetched_at"] = "2030-01-01T00:00:00Z"
        (d / pl.MANIFEST).write_text(json.dumps(manifest))
        self.assertEqual(pl.load_export(d).logical_root, pl.load_export(EXPORT).logical_root)

    def test_rows_out_of_order_are_refused(self) -> None:
        d = copy_export(self.tmp / "order")
        lines = (d / "roadmaps.jsonl").read_bytes().splitlines(keepends=True)
        (d / "roadmaps.jsonl").write_bytes(b"".join(reversed(lines)))
        reseal(d)
        with self.assertRaisesRegex(pl.PlansExportError, "out of order"):
            pl.load_export(d)

    def test_a_hidden_file_such_as_a_finder_ds_store_is_ignored(self) -> None:
        d = copy_export(self.tmp / "hidden")
        (d / ".DS_Store").write_bytes(b"\0\0\0\1Bud1")
        self.assertEqual(pl.load_export(d).logical_root, pl.load_export(EXPORT).logical_root)

    def test_a_symlinked_file_is_refused(self) -> None:
        d = copy_export(self.tmp / "link")
        real = self.tmp / "edges.real"
        shutil.move(d / "edges.jsonl", real)
        (d / "edges.jsonl").symlink_to(real)
        with self.assertRaisesRegex(pl.PlansExportError, "cannot open edges.jsonl"):
            pl.load_export(d)


class BuildLayer(unittest.TestCase):
    NODE_IDS = {"decl:Mathlib:Nat.succ_le", "decl:TauCeti:TauCeti.Foo.bar", "xref:stacks:0A1B",
                "lit:0704.0001"}

    @classmethod
    def setUpClass(cls) -> None:
        cls.export = pl.load_export(EXPORT)
        cls.layer = pl.build_layer(cls.export, node_ids=set(cls.NODE_IDS))
        cls.nodes = {n["id"]: n for n in cls.layer.nodes}
        cls.edges = {(e["kind"], e["src"], e["dst"]): e for e in cls.layer.edges}
        cls.held = {pl.edge_key(h["edge"]): h for h in cls.layer.held}

    def test_containers_place_top_level_roadmaps_by_topic(self) -> None:
        ids = [c["id"] for c in self.layer.containers]
        self.assertEqual(ids, [pl.PLANS_ROOT, pl.TAUCETI_ROOT, "path:Plans/TauCeti/math.AG",
                               "path:Plans/TauCeti/math.NT", "path:Plans/TauCeti/unclassified"])
        for parent, child in [(pl.PLANS_ROOT, pl.TAUCETI_ROOT),
                              (pl.TAUCETI_ROOT, "path:Plans/TauCeti/math.NT"),
                              ("path:Plans/TauCeti/math.NT", ALPHA),
                              ("path:Plans/TauCeti/unclassified", GAMMA),
                              (ALPHA, A_L0), (BETA, B_L0)]:
            self.assertIn(("contains", parent, child), self.edges)
        root = {c["id"]: c for c in self.layer.containers}[pl.PLANS_ROOT]
        self.assertEqual((root["n_roadmaps"], root["n_layers"]), (3, 5))

    def test_every_plan_node_has_exactly_one_parent(self) -> None:
        parents: dict[str, list[str]] = {}
        for kind, src, dst in self.edges:
            if kind == "contains":
                parents.setdefault(dst, []).append(src)
        for nid in self.nodes:
            self.assertEqual(len(parents.get(nid, [])), 1, nid)

    def test_plan_nodes_carry_the_export_fields_and_no_prose(self) -> None:
        alpha, layer = self.nodes[ALPHA], self.nodes[A_L0]
        self.assertEqual((alpha["type"], alpha["plan_kind"], alpha["label"], alpha["topic"]),
                         ("plan", "roadmap", "Alpha theory", "math.NT"))
        self.assertEqual((layer["plan_kind"], layer["key"], layer["state"], layer["ordinal"]),
                         ("layer", "Layer 0", "done", 0))
        self.assertEqual(layer["source"]["line"], 10)
        self.assertIn("TauCeti.Foo.ba", layer["planned_names"])
        self.assertNotIn("atlas_url", layer)
        self.assertEqual(self.nodes[B_L0]["unqualified_names"],
                         [{"name": "Foo.bar", "candidates": ["TauCeti.Foo.bar"],
                           "candidates_total": 1, "candidate_libraries": ["tauceti"]}])

    def test_public_depends_is_an_edge_and_atlas_only_evidence_is_held(self) -> None:
        edge = self.edges[("depends", ALPHA, BETA)]
        self.assertEqual(edge["provenance"], {"source": "tauceti_plans",
                                              "method": "atlas-verified-line",
                                              "pin": self.export.logical_root})
        self.assertIs(edge["evidence"]["evidence_public"], True)
        self.assertIn(("depends", A_L1, B_L0), self.edges)
        held = self.held[("depends", GAMMA, BETA)]
        self.assertEqual(held["reason"], "evidence_not_public")
        self.assertEqual(held["row"], f"depends:{GAMMA}->{BETA}")
        self.assertNotIn(("depends", GAMMA, BETA), self.edges)

    def test_mentions_need_their_declaration_node(self) -> None:
        exact = self.edges[("mentions", A_L0, "decl:Mathlib:Nat.succ_le")]
        self.assertEqual(exact["provenance"]["method"], "exact")
        self.assertEqual(exact["evidence"]["role"], "plan")
        scoped = self.edges[("mentions", ALPHA, "decl:TauCeti:TauCeti.Foo.bar")]
        self.assertEqual(scoped["provenance"]["method"], "declared-scope")
        self.assertEqual(scoped["evidence"]["via_namespace"], "TauCeti")
        held = self.held[("mentions", A_L0, "decl:Mathlib:Nat.pred")]
        self.assertEqual((held["reason"], held["missing"]),
                         ("endpoint_missing", "decl:Mathlib:Nat.pred"))
        self.assertFalse(any(k[0] == "mentions" and "planned" in str(k) for k in self.edges))

    def test_stacks_rows_are_xref_and_arxiv_rows_are_cites(self) -> None:
        self.assertIn(("xref", A_L0, "xref:stacks:0A1B"), self.edges)
        self.assertIn(("cites", A_L0, "lit:0704.0001"), self.edges)
        self.assertEqual(self.held[("cites", A_L0, "lit:2101.01234")]["reason"],
                         "endpoint_missing")
        self.assertEqual(self.held[("xref", A_L2, "xref:stacks:0B3C")]["missing"],
                         "xref:stacks:0B3C")

    def test_nothing_is_both_emitted_and_held_and_the_counts_add_up(self) -> None:
        self.assertFalse(set(self.edges) & set(self.held))
        export = self.export.rows
        public = sum(1 for r in export["edges.jsonl"] if r["evidence_public"])
        resolved = sum(1 for r in export["decl_refs.jsonl"]
                       if r["resolution"] in ("mathlib", "tauceti"))
        dups = self.layer.stats["mentions_duplicate_rows"]
        n = lambda kind: sum(1 for k in self.edges if k[0] == kind)  # noqa: E731
        h = lambda kind: sum(1 for k in self.held if k[0] == kind)  # noqa: E731
        self.assertEqual(n("depends"), public)
        self.assertEqual(n("depends") + h("depends"), len(export["edges.jsonl"]))
        self.assertEqual(n("mentions") + h("mentions") + dups, resolved)
        self.assertEqual(n("xref") + n("cites") + h("xref") + h("cites"),
                         len(export["xrefs.jsonl"]))

    def test_a_build_is_deterministic(self) -> None:
        again = pl.build_layer(pl.load_export(EXPORT), node_ids=set(self.NODE_IDS))
        self.assertEqual(pl.jsonl_bytes(again.nodes + again.edges + again.held),
                         pl.jsonl_bytes(self.layer.nodes + self.layer.edges + self.layer.held))

    def test_plan_ids_may_not_collide_with_other_nodes(self) -> None:
        with self.assertRaisesRegex(pl.PlansExportError, "collide"):
            pl.build_layer(self.export, node_ids={ALPHA})

    def test_two_names_for_one_declaration_make_one_edge_the_exact_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = copy_export(Path(tmp) / "dup")

            def add_exact(rows):
                src = next(r for r in rows if r["id"] == f"{ALPHA}::Foo.bar")
                rows.append({**src, "id": f"{ALPHA}::TauCeti.Foo.bar",
                             "name": "TauCeti.Foo.bar", "resolved_name": None,
                             "via_namespace": None})
                return [{k: v for k, v in r.items() if v is not None} for r in rows]
            rewrite_rows(d, "decl_refs.jsonl", add_exact)
            layer = pl.build_layer(pl.load_export(d), node_ids=set(self.NODE_IDS))
        mentions = [e for e in layer.edges if e["kind"] == "mentions" and e["src"] == ALPHA
                    and e["dst"] == "decl:TauCeti:TauCeti.Foo.bar"]
        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0]["provenance"]["method"], "exact")
        self.assertEqual(layer.stats["mentions_duplicate_rows"], 1)

    def test_a_row_naming_an_unknown_layer_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = copy_export(Path(tmp) / "orphan")

            def orphan(rows):
                rows[0] = {**rows[0], "layer": f"{ALPHA}#no-such-layer"}
                return rows
            rewrite_rows(d, "xrefs.jsonl", orphan)
            with self.assertRaisesRegex(pl.PlansExportError, "names no roadmap or layer"):
                pl.build_layer(pl.load_export(d), node_ids=set())


class Facets(unittest.TestCase):
    def test_plan_nodes_take_bit_16_and_their_containers_aggregate_it(self) -> None:
        nodes = [{"id": pl.PLANS_ROOT, "type": "container", "label": "Plans"},
                 {"id": ALPHA, "type": "plan", "plan_kind": "roadmap", "label": "A"},
                 {"id": "lit:0704.0001", "type": "literature", "label": "p"}]
        edges = [pl._edge(pl.PLANS_ROOT, ALPHA, "contains", "t", "r", {}),
                 pl._edge(ALPHA, "lit:0704.0001", "cites", "t", "r", {})]
        bc.apply_facets(nodes, edges, [])
        bc.aggregate_facets(nodes, edges)
        by_id = {n["id"]: n for n in nodes}
        self.assertEqual(by_id[ALPHA]["f"], bc.F_PLAN | bc.F_LITERATURE)
        self.assertEqual(by_id[pl.PLANS_ROOT]["fa"], bc.F_PLAN | bc.F_LITERATURE)
        self.assertEqual(bc.F_PLAN, 1 << 16)
        self.assertNotIn(bc.F_PLAN, (bc.F_FORMALIZED, bc.F_PARTIAL, *bc.F_DB_BIT.values()))

    def test_a_plan_xref_marks_the_plan_and_leaves_the_cited_page_as_it_was(self) -> None:
        page = {"id": "xref:stacks:0A1B", "type": "ext", "db": "stacks", "label": "t"}
        nodes = [{"id": A_L0, "type": "plan", "plan_kind": "layer", "label": "L"}, page]
        edges = [pl._edge(A_L0, page["id"], "xref", "pattern-scan", "r", {})]
        bc.apply_facets(nodes, edges, [])
        self.assertEqual(nodes[0]["f"], bc.F_PLAN | bc.F_ANY_XREF)
        self.assertEqual(page["f"], bc.F_EXT | bc.F_DB_BIT["stacks"])

    def test_plans_is_a_reserved_library_root(self) -> None:
        self.assertIn("Plans", bc.RESERVED_LIBRARY_ROOTS)
        self.assertEqual(pl.PLANS_ROOT, "path:Plans")


class HeldList(unittest.TestCase):
    def test_write_then_read_round_trips_and_the_digest_covers_the_rows(self) -> None:
        layer = pl.build_layer(pl.load_export(EXPORT), node_ids=set())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / pl.HELD_NAME
            pl.write_held(path, layer.held, logical_root="r" * 64, snapshot_id="s")
            meta, rows = pl.read_held(path)
        self.assertEqual(rows, layer.held)
        self.assertEqual(meta["sha256"], pl.held_summary(layer.held)["sha256"])
        self.assertEqual((meta["logical_root"], meta["snapshot_id"]), ("r" * 64, "s"))


class Publish(unittest.TestCase):
    """build_snapshot publishes the held list and keeps the one it replaces."""

    def _publish(self, data_dir: Path, held: list[dict], root: str) -> None:
        meta = {"schema": "brain/SCHEMA.md", "generated_at": "x",
                "plans": {"state": "present", "logical_root": root}}
        fake = ([{"id": "Q1", "type": "concept"}], [], meta, held)
        with mock.patch.object(build_snapshot, "build", return_value=fake):
            build_snapshot._publish_live_graph(data_dir=data_dir, sqlite_output=None)

    def test_the_previous_held_list_is_the_one_this_build_replaced(self) -> None:
        layer = pl.build_layer(pl.load_export(EXPORT), node_ids=set())
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            self._publish(data, layer.held, "a" * 64)
            self.assertFalse((data / pl.HELD_PREVIOUS_NAME).exists())
            first = (data / pl.HELD_NAME).read_bytes()
            self._publish(data, layer.held[:1], "b" * 64)
            self.assertEqual((data / pl.HELD_PREVIOUS_NAME).read_bytes(), first)
            meta, rows = pl.read_held(data / pl.HELD_NAME)
            self.assertEqual((meta["logical_root"], len(rows)), ("b" * 64, 1))
            nodes_meta = json.loads((data / "nodes.jsonl").read_text().splitlines()[0])["_meta"]
            self.assertEqual(meta["snapshot_id"], nodes_meta["snapshot_id"])

    def test_a_stale_previous_list_goes_when_there_is_no_current_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            (data / pl.HELD_PREVIOUS_NAME).write_text("stale\n")
            self._publish(data, [], "a" * 64)
            self.assertFalse((data / pl.HELD_PREVIOUS_NAME).exists())


class Accounting(unittest.TestCase):
    """check_plans (test_acceptance P17) across two builds."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        self.data.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stage(self, export: Path) -> pl.PlansExport:
        target = self.data / "tauceti_plans"
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(export, target)
        return pl.load_export(target)

    def build(self, export: pl.PlansExport, node_ids: set[str]) -> dict:
        """One build's outputs, rotated the way build_snapshot publishes them."""
        layer = pl.build_layer(export, node_ids=node_ids)
        held_path = self.data / pl.HELD_NAME
        if held_path.exists():
            shutil.copy2(held_path, self.data / pl.HELD_PREVIOUS_NAME)
        pl.write_held(held_path, layer.held, logical_root=export.logical_root, snapshot_id="s")
        return {
            "nodes_meta": {"snapshot_id": "s",
                           "plans": {"state": "present", "logical_root": export.logical_root,
                                     "held": pl.held_summary(layer.held)}},
            "n_plan_nodes": len(layer.nodes),
            "node_ids": node_ids | {n["id"] for n in layer.nodes + layer.containers},
            "plan_edge_keys": {pl.edge_key(e) for e in layer.edges},
            "data_dir": self.data,
        }

    def test_no_export_skips(self) -> None:
        ok, details = pl.check_plans(nodes_meta={"plans": {"state": "absent", "reason": "r"}},
                                     n_plan_nodes=0, node_ids=set(), plan_edge_keys=set(),
                                     data_dir=self.data)
        self.assertEqual(ok, "skip")

    def test_an_export_the_build_did_not_read_fails(self) -> None:
        self.stage(EXPORT)
        ok, details = pl.check_plans(nodes_meta={}, n_plan_nodes=0, node_ids=set(),
                                     plan_edge_keys=set(), data_dir=self.data)
        self.assertIs(ok, False)
        self.assertIn("did not read it", details[0])

    def test_first_build_passes_and_says_accounting_starts(self) -> None:
        ok, details = pl.check_plans(**self.build(self.stage(EXPORT), set()))
        self.assertIs(ok, True, details)
        self.assertTrue(any("first build" in d for d in details))

    def test_a_held_edge_whose_node_appears_is_emitted_and_accounted(self) -> None:
        export = self.stage(EXPORT)
        self.build(export, set())
        ok, details = pl.check_plans(**self.build(export, {"decl:Mathlib:Nat.pred"}))
        self.assertIs(ok, True, details)
        self.assertTrue(any(d.startswith("mentions/endpoint_missing") and "1 emitted" in d
                            for d in details), details)

    def test_a_held_edge_that_vanishes_without_emission_fails(self) -> None:
        export = self.stage(EXPORT)
        self.build(export, set())
        args = self.build(export, set())
        meta, rows = pl.read_held(self.data / pl.HELD_NAME)
        kept = [r for r in rows if r["edge"]["dst"] != "decl:Mathlib:Nat.pred"]
        pl.write_held(self.data / pl.HELD_NAME, kept, logical_root=export.logical_root,
                      snapshot_id="s")
        args["nodes_meta"]["plans"]["held"] = pl.held_summary(kept)
        ok, details = pl.check_plans(**args)
        self.assertIs(ok, False)
        self.assertTrue(any("neither emitted nor held" in d for d in details), details)
        self.assertTrue(any("disappeared without being emitted" in d for d in details), details)

    def test_a_first_build_that_drops_a_candidate_fails(self) -> None:
        export = self.stage(EXPORT)
        args = self.build(export, set())
        args["plan_edge_keys"].discard(("depends", ALPHA, BETA))
        ok, details = pl.check_plans(**args)
        self.assertIs(ok, False)
        self.assertTrue(any("neither emitted nor held" in d for d in details), details)

    def test_an_emitted_plan_edge_the_export_does_not_propose_fails(self) -> None:
        args = self.build(self.stage(EXPORT), set())
        args["plan_edge_keys"].add(("mentions", ALPHA, "decl:Mathlib:Invented"))
        ok, details = pl.check_plans(**args)
        self.assertIs(ok, False)
        self.assertTrue(any("does not propose" in d for d in details), details)

    def test_an_export_without_evidence_public_holds_every_depends_and_passes(self) -> None:
        # Run 1's export predates the field: a missing evidence_public is not public.
        d = copy_export(self.tmp / "no-public-field")

        def drop_field(rows):
            for r in rows:
                r.pop("evidence_public", None)
                r["evidence"].pop("evidence_public", None)
            return rows
        rewrite_rows(d, "edges.jsonl", drop_field)
        export = self.stage(d)
        args = self.build(export, set())
        self.assertFalse(any(k[0] == "depends" for k in args["plan_edge_keys"]))
        ok, details = pl.check_plans(**args)
        self.assertIs(ok, True, details)

    def test_a_held_name_that_now_resolves_elsewhere_is_gone_not_lost(self) -> None:
        # A Tau Ceti declaration upstreamed to Mathlib: the decl_refs row keeps its id, its
        # held mention to decl:TauCeti:* is no longer proposed, and a new one is.
        self.build(self.stage(EXPORT), set())
        d = copy_export(self.tmp / "upstreamed")

        def upstream(rows):
            for r in rows:
                if r["id"] == f"{ALPHA}::Foo.bar":
                    r["resolution"] = "mathlib"
                    r["resolved_by"] = ["mathlib_decls"]
                    for key in ("resolved_name", "via_namespace", "stated", "stated_reason"):
                        r.pop(key, None)
            return rows
        rewrite_rows(d, "decl_refs.jsonl", upstream)
        ok, details = pl.check_plans(**self.build(self.stage(d), set()))
        self.assertIs(ok, True, details)
        self.assertTrue(any(x.startswith("mentions/endpoint_missing") and "1 gone" in x
                            and "1 newly held" in x for x in details), details)

    def test_a_held_edge_whose_export_row_is_gone_is_accounted(self) -> None:
        self.build(self.stage(EXPORT), set())
        d = copy_export(self.tmp / "newer")
        rewrite_rows(d, "decl_refs.jsonl",
                     lambda rows: [r for r in rows if r["id"] != f"{A_L0}::Nat.pred"])
        ok, details = pl.check_plans(**self.build(self.stage(d), set()))
        self.assertIs(ok, True, details)
        self.assertTrue(any(d.startswith("mentions/endpoint_missing") and "1 gone" in d
                            for d in details), details)

    def test_a_held_list_that_does_not_match_the_nodes_meta_fails(self) -> None:
        args = self.build(self.stage(EXPORT), set())
        args["nodes_meta"]["plans"]["held"] = {"rows": 0, "counts": {}, "sha256": "0" * 64}
        ok, details = pl.check_plans(**args)
        self.assertIs(ok, False)

    def test_a_held_edge_that_is_also_emitted_fails(self) -> None:
        args = self.build(self.stage(EXPORT), set())
        _meta, rows = pl.read_held(self.data / pl.HELD_NAME)
        args["plan_edge_keys"].add(pl.edge_key(rows[0]["edge"]))
        ok, details = pl.check_plans(**args)
        self.assertIs(ok, False)
        self.assertTrue(any("also an emitted edge" in d for d in details), details)


class LiveBuildWiring(unittest.TestCase):
    """The pieces of build_common that only a live build reaches."""

    def test_replay_code_never_imports_the_plans_layer_at_module_level(self) -> None:
        # Replay runs from the v2/v3 inventories' declared code scope, which does not
        # list brain/plans_layer.py: only the live path may import it.
        for module in ("build_common.py", "build_snapshot.py"):
            text = (HERE / module).read_text()
            self.assertNotIn("\nimport plans_layer", text)
            self.assertNotIn("\nfrom plans_layer", text)
        for name in ("v2", "v3", "v3-folded"):
            scope = json.loads(
                (HERE / "authority" / f"reducer-inputs-{name}.json").read_text())["scope"]
            self.assertNotIn("brain/plans_layer.py", scope)

    def test_a_replay_build_meta_has_no_plans_block(self) -> None:
        source = (HERE / "build_common.py").read_text()
        self.assertIn("if plans_meta is not None:", source)
        self.assertIn('meta["plans"] = plans_meta', source)

    def test_the_v1_inventory_declares_the_export_and_the_code(self) -> None:
        v1 = json.loads((HERE / "authority" / "reducer-inputs-v1.json").read_text())
        self.assertIn("brain/plans_layer.py", v1["scope"])
        declared = {e.get("path") or e.get("path_pattern") for e in v1["inputs"]}
        self.assertIn("brain/data/tauceti_plans/*.jsonl", declared)
        self.assertIn("brain/data/tauceti_plans/manifest.json", declared)
        classes = {e.get("path") or e.get("path_pattern"): e["class"] for e in v1["inputs"]
                   if "class" in e}
        self.assertEqual(classes["brain/data/tauceti_plans/*.jsonl"], "immutable_source_object")

    def test_replay_inventories_do_not_declare_the_export(self) -> None:
        for name in ("v2", "v3", "v3-folded"):
            text = (HERE / "authority" / f"reducer-inputs-{name}.json").read_text()
            self.assertNotIn("tauceti_plans", text, name)
        self.assertNotIn("tauceti-plans", bc.BASE_GRAPH_BINDING_CONTRACT)

    def test_the_registry_names_the_provenance_source(self) -> None:
        reg = json.loads((HERE.parent / "catalog" / "data" / "source_registry.json").read_text())
        entry = reg["frontier_sources"][pl.SOURCE]
        for field in ("name", "homepage", "layer", "kind", "our_provenance",
                      "target_license", "ingest"):
            self.assertTrue(entry.get(field), field)


if __name__ == "__main__":
    unittest.main()
