#!/usr/bin/env python3
"""Fail-closed generation coherence across preflight, compilation and pack reads."""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
import sys
import subprocess
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))

import authority_contracts as contracts
import compile_offline_pack_v2 as compiler
import preflight_offline_pack_v2 as preflight
import test_compile_offline_pack_v2 as fixtures


class InventoryCoherenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.OfflinePackCompilerTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        f = self.fixture
        f._upgrade_plan_v3()
        (f.external / "other_pages.jsonl").write_bytes(fixtures.SHARED_BYTES)
        f.inventory["schema"] = contracts.REDUCER_INPUT_INVENTORY_SCHEMA_V3
        f.inventory["coherence_groups"] = [{
            "id": "observations",
            "kind": "shared-acquisition",
            "inputs": ["optional_external", "source"],
        }]
        f.plan["input_bindings"][1].update({
            "state": "present",
            "sources": ["external-fixture"],
            "members": [{"object": "normalized", "path": "other_pages.jsonl", "source": "external-fixture"}],
        })
        self.write()

    def write(self) -> None:
        f = self.fixture
        f.inventory["inventory_id"] = contracts.reducer_input_inventory_identity(f.inventory)
        f.plan["inventory_id"] = f.inventory["inventory_id"]
        fixtures._write_canonical(f.inventory_path, f.inventory)
        fixtures._write_canonical(f.plan_path, f.plan)

    def check(self) -> None:
        f = self.fixture
        contracts.validate_inventory_coherence(
            f.inventory, f.plan["input_bindings"],
            {s["source"]: s for s in f.plan["sources"]}, schema=f.plan["schema"],
        )

    def preflight(self) -> dict:
        f = self.fixture
        return preflight.preflight_offline_pack_v2(
            f.plan_path, f.inventory_path, f.base / "store",
            roots={"repo": f.repo, "external": f.external},
            as_of=dt.datetime(2030, 1, 2, tzinfo=dt.timezone.utc),
        )

    def test_real_inventory_has_explicit_wikidata_generation(self) -> None:
        v2, _ = contracts.load_canonical_json(HERE / "authority/reducer-inputs-v2.json")
        v3, _ = contracts.load_canonical_json(HERE / "authority/reducer-inputs-v3.json")
        contracts.validate_reducer_input_inventory(v2)
        contracts.validate_reducer_input_inventory(v3)
        self.assertNotEqual(v2["inventory_id"], v3["inventory_id"])
        self.assertNotIn("coherence_groups", v2)
        old = {item["id"]: item for item in v2["inputs"]}
        new = {item["id"]: item for item in v3["inputs"]}
        self.assertEqual(old["brain-community-edges"]["class"], "curated_git_input")
        self.assertEqual(new["brain-community-edges"]["class"], "immutable_source_object")
        self.assertEqual(v3["coherence_groups"], [{
            "id": "wikidata-observation", "kind": "shared-acquisition",
            "inputs": ["wikidata-descriptions", "wikidata-edges", "wikidata-universe"],
        }])
        import jsonschema
        schema = json.loads((HERE / "authority/schemas/reducer-input-inventory/v3.json").read_text())
        jsonschema.Draft202012Validator(schema).validate(v3)
        result = subprocess.run(
            [sys.executable, str(HERE / "tools/verify_source_set.py"),
             "--manifest", str(HERE / "authority/reducer-inputs-v3.json")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["inventory_id"], v3["inventory_id"])

    def test_group_contract_rejects_undefined_duplicate_or_curated_inputs(self) -> None:
        for inputs in (["source"], ["source", "source"], ["source", "unknown"], ["curated", "source"]):
            with self.subTest(inputs=inputs):
                bad = copy.deepcopy(self.fixture.inventory)
                bad["coherence_groups"][0]["inputs"] = inputs
                bad["inventory_id"] = contracts.reducer_input_inventory_identity(bad)
                with self.assertRaises(contracts.VerificationError):
                    contracts.validate_reducer_input_inventory(bad)

    def test_v2_contract_cannot_silently_accept_v3_constraints(self) -> None:
        bad = copy.deepcopy(self.fixture.inventory)
        bad["schema"] = contracts.REDUCER_INPUT_INVENTORY_SCHEMA_V2
        bad["inventory_id"] = contracts.reducer_input_inventory_identity(bad)
        with self.assertRaisesRegex(contracts.VerificationError, "coherence_groups"):
            contracts.validate_reducer_input_inventory(bad)

    def test_coherent_fixture_passes_preflight_compile_and_independent_verifier(self) -> None:
        self.check()
        report = self.preflight()
        self.assertTrue(report["compile_ready"])
        result = self.fixture._compile()
        pack, _ = contracts.load_canonical_json(result.manifest_path)
        self.assertEqual(pack["inventory"]["path"], "inventory/reducer-inputs-v3.json")
        contracts.verify_offline_pack_files(contracts.validate_offline_pack(pack), result.root, manifest_path=result.manifest_path)
        again = self.fixture._compile()
        self.assertTrue(again.reused)
        self.assertEqual(result.offline_pack_id, again.offline_pack_id)

    def test_absent_optional_group_member_fails_before_staging(self) -> None:
        f = self.fixture
        (f.external / "other_pages.jsonl").unlink()
        f.plan["input_bindings"][1].update({"state": "absent", "members": []})
        self.write()
        for operation in (self.preflight, f._compile):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(ValueError, "shared acquisition requires present input"):
                    operation()
        self.assertFalse((f.base / "store").exists())

    def test_different_declared_and_member_sources_fail(self) -> None:
        binding = self.fixture.plan["input_bindings"][1]
        binding["members"][0]["source"] = "curated-fixture"
        with self.assertRaisesRegex(contracts.VerificationError, "mixed member"):
            self.check()

    def test_group_cannot_be_split_across_manifests(self) -> None:
        f = self.fixture
        second = copy.deepcopy(f.plan["sources"][1])
        second["source"] = "other-acquisition"
        f.plan["sources"].append(second)
        binding = f.plan["input_bindings"][1]
        binding["sources"] = ["other-acquisition"]
        binding["members"][0]["source"] = "other-acquisition"
        with self.assertRaisesRegex(contracts.VerificationError, "same source manifest"):
            self.check()

    def test_curated_or_evidenceless_source_cannot_bypass_acquisition(self) -> None:
        source = next(s for s in self.fixture.plan["sources"] if s["source"] == "external-fixture")
        source["source_kind"] = "curated_git_tree"
        with self.assertRaisesRegex(contracts.VerificationError, "acquired_dataset"):
            self.check()
        source["source_kind"] = "acquired_dataset"
        del source["evidence"]
        with self.assertRaisesRegex(contracts.VerificationError, "missing v3 evidence"):
            self.check()

    def test_legacy_plan_cannot_bypass_v3_evidence_rules(self) -> None:
        f = self.fixture
        f.plan["schema"] = compiler.SOURCE_PLAN_SCHEMA
        for source in f.plan["sources"]:
            source.pop("evidence", None)
        self.write()
        for operation in (self.preflight, f._compile):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(ValueError, "inventory/v3 requires"):
                    operation()

    def test_rehashed_pack_cannot_remove_a_group_member(self) -> None:
        result = self.fixture._compile()
        pack, _ = contracts.load_canonical_json(result.manifest_path)
        binding = next(b for b in pack["input_bindings"] if b["input_id"] == "optional_external")
        binding.update({"state": "absent", "members": []})
        pack["source_set_root"] = contracts.source_set_root_v3(
            pack["inventory"]["inventory_id"],
            [ref["source_manifest_id"] for ref in pack["source_manifests"]],
            pack["input_bindings"],
        )
        pack["offline_pack_id"] = contracts.offline_pack_identity(pack)
        with self.assertRaisesRegex(contracts.VerificationError, "shared acquisition requires present input"):
            contracts.verify_offline_pack_files(contracts.validate_offline_pack(pack), result.root, manifest_path=result.manifest_path)


if __name__ == "__main__":
    unittest.main()
