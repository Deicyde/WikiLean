"""Experimental assertion history, identity, commutation and pilot parity."""
import copy
import itertools
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
import assertion_kernel as k
import replay_authority as replay_cli
import shadow_assertions as shadow


def aid(value):
    return k.contracts.domain_hash("fixture-assertion", {"key": value})


class AssertionKernelTest(unittest.TestCase):
    def creation(self, value="first", actor="fixture-author", **payload):
        body = {"src": "Q1", "dst": "decl:Mathlib:Nat.add_comm", "kind": "formalizes", "attributes": {"confidence": "high"}, **payload}
        return k.make_operation("assert_relationship", aid(value), body, actor={"kind": "fixture", "id": actor})

    def apply(self, previous, operations):
        fixture = k.make_fixture(previous, operations)
        return k.replay_ledger({"schema": k.LEDGER_SCHEMA, "fixtures": [fixture]}, initial=previous)

    def test_frozen_v1_history_vector_full_and_incremental_roots(self):
        path = Path(__file__).parent / "authority/fixtures/assertion-history-v1.json"
        vector = json.loads(path.read_bytes())
        ledger = vector["ledger"]; incremental = k.empty_replay()
        for index in range(len(ledger["fixtures"]) + 1):
            full = k.replay_ledger({"schema": k.LEDGER_SCHEMA, "fixtures": ledger["fixtures"][:index]})
            if index:
                incremental = k.replay_ledger({"schema": k.LEDGER_SCHEMA, "fixtures": [ledger["fixtures"][index - 1]]}, initial=incremental)
            self.assertEqual(full, incremental)
            self.assertEqual({"state_root": full.semantic_root, "chain_root": full.chain_root}, vector["expected_prefix_roots"][index])
        self.assertEqual(k.result_document(incremental)["counts"], vector["expected_counts"])

    def test_creation_retract_restore_retains_exact_inactive_history(self):
        initial = k.empty_replay(); created = self.apply(initial, [self.creation()])
        retract = k.make_operation("retract_assertion", aid("first"), {"reason": "recorded rejection"}, expected_revision=1)
        inactive = self.apply(created, [retract]); assertion = inactive.state["assertions"][0]
        self.assertFalse(assertion["active"]); self.assertEqual(assertion["revision"], 2)
        self.assertNotEqual(inactive.semantic_root, initial.semantic_root)
        restore = k.make_operation("restore_assertion", aid("first"), {"retraction_id": retract["operation_id"]}, expected_revision=2)
        restored = self.apply(inactive, [restore]); assertion = restored.state["assertions"][0]
        self.assertTrue(assertion["active"]); self.assertEqual(assertion["revision"], 3)
        self.assertEqual(assertion["retractions"], [{"retraction_id": retract["operation_id"], "restoration_id": restore["operation_id"]}])
        self.assertNotEqual(created.semantic_root, restored.semantic_root)
        full = k.replay_ledger({"schema": k.LEDGER_SCHEMA, "fixtures": list(restored.fixtures)})
        self.assertEqual(full, restored)

    def test_independent_equivalent_assertions_remain_distinct_after_retraction(self):
        a, b = self.creation("a", "alice"), self.creation("b", "bob", attributes={"confidence": "medium", "evidence": "independent"})
        state = self.apply(k.empty_replay(), [a, b])
        self.assertEqual(len(state.state["assertions"]), 2)
        self.assertEqual(state.state["assertions"][0]["semantic_key"], state.state["assertions"][1]["semantic_key"])
        retract = k.make_operation("retract_assertion", aid("a"), {"reason": "alice withdrew"}, expected_revision=1)
        after = self.apply(state, [retract])
        active = [r for r in after.state["assertions"] if r["active"]]
        self.assertEqual([r["assertion_id"] for r in active], [aid("b")])

    def test_registered_independent_operations_commute_but_chain_order_does_not(self):
        a, b = self.creation("a"), self.creation("b")
        self.assertTrue(k.independently_commute(a, b))
        left = self.apply(k.empty_replay(), [a, b]); right = self.apply(k.empty_replay(), [b, a])
        self.assertEqual(left.semantic_root, right.semantic_root)
        self.assertEqual(left.state, right.state)
        self.assertNotEqual(left.chain_root, right.chain_root)
        retract = k.make_operation("retract_assertion", aid("a"), {"reason": "withdrawn"}, expected_revision=1)
        self.assertFalse(k.independently_commute(a, retract))

    def test_all_registered_operation_classes_commute_on_disjoint_histories(self):
        initial = self.apply(k.empty_replay(), [self.creation("to-retract"), self.creation("to-restore")])
        retraction = k.make_operation("retract_assertion", aid("to-restore"), {"reason": "prior withdrawal"}, expected_revision=1)
        initial = self.apply(initial, [retraction])
        operations = [
            k.make_operation("assert_entity", aid("new-entity"), {"entity": "Q3", "attributes": {}}),
            self.creation("new-relationship"),
            k.make_operation("retract_assertion", aid("to-retract"), {"reason": "withdrawal"}, expected_revision=1),
            k.make_operation("restore_assertion", aid("to-restore"), {"retraction_id": retraction["operation_id"]}, expected_revision=2),
        ]
        for a, b in itertools.combinations(operations, 2):
            self.assertTrue(k.independently_commute(a, b))
        reference = self.apply(initial, operations)
        for permutation in itertools.permutations(operations):
            result = self.apply(initial, permutation)
            self.assertEqual(result.state, reference.state)
            self.assertEqual(result.semantic_root, reference.semantic_root)

    def test_stale_revision_wrong_predecessor_or_expected_absence_fail_atomically(self):
        state = self.apply(k.empty_replay(), [self.creation()])
        saved = copy.deepcopy(state)
        for revision in (2, 3):
            operation = k.make_operation("retract_assertion", aid("first"), {"reason": "withdrawn"}, expected_revision=revision)
            with self.assertRaisesRegex(k.AssertionError, "revision/absence"): self.apply(state, [operation])
        with self.assertRaises(k.AssertionError): self.apply(state, [self.creation("first", "different-actor")])
        fixture = k.make_fixture(k.empty_replay(), [self.creation("another")])
        with self.assertRaisesRegex(k.AssertionError, "predecessor/root"):
            k.replay_ledger({"schema": k.LEDGER_SCHEMA, "fixtures": [fixture]}, initial=state)
        self.assertEqual(state, saved)

    def test_duplicate_or_reused_operation_ids_and_content_tamper_fail(self):
        operation = self.creation()
        with self.assertRaisesRegex(k.AssertionError, "duplicate operation"): k.make_fixture(k.empty_replay(), [operation, operation])
        state = self.apply(k.empty_replay(), [operation])
        with self.assertRaisesRegex(k.AssertionError, "already occupies"): self.apply(state, [operation])
        changed = copy.deepcopy(operation); changed["payload"]["attributes"]["confidence"] = "low"
        with self.assertRaisesRegex(k.AssertionError, "content identity"): k.validate_operation(changed)

    def test_unknown_versions_fields_generated_cells_and_unregistered_kinds_fail(self):
        operation = self.creation()
        for changed in ({**operation, "schema": "wikilean.operation/v1"}, {**operation, "extra": 1}, {**operation, "operation_type": "supersede_assertion"}, {**operation, "operation_type": []}):
            with self.assertRaises(k.AssertionError): k.validate_operation(changed)
        for field in ("src", "dst"):
            with self.assertRaisesRegex(k.AssertionError, "generated cell"): self.creation(**{field: "cell:Q1"})
        for kind in ("depends", "links", "contains", "typo", []):
            with self.assertRaises(k.AssertionError): self.creation(kind=kind)
        fixture = k.make_fixture(k.empty_replay(), [operation]); fixture["schema"] = "wikilean.changeset/v1"
        with self.assertRaisesRegex(k.AssertionError, "not experimental"): k.validate_fixture(fixture)

    def test_restore_requires_current_exact_retraction_and_cannot_reuse_old_one(self):
        created = self.apply(k.empty_replay(), [self.creation()])
        first = k.make_operation("retract_assertion", aid("first"), {"reason": "first"}, expected_revision=1)
        inactive = self.apply(created, [first])
        wrong = k.make_operation("restore_assertion", aid("first"), {"retraction_id": aid("wrong")}, expected_revision=2)
        with self.assertRaisesRegex(k.AssertionError, "exact current"): self.apply(inactive, [wrong])
        restore = k.make_operation("restore_assertion", aid("first"), {"retraction_id": first["operation_id"]}, expected_revision=2)
        active = self.apply(inactive, [restore])
        second = k.make_operation("retract_assertion", aid("first"), {"reason": "second"}, expected_revision=3)
        inactive_again = self.apply(active, [second])
        obsolete = k.make_operation("restore_assertion", aid("first"), {"retraction_id": first["operation_id"]}, expected_revision=4)
        with self.assertRaisesRegex(k.AssertionError, "exact current"): self.apply(inactive_again, [obsolete])

    def test_missing_double_retracted_and_inactive_id_reallocation_fail(self):
        retract = k.make_operation("retract_assertion", aid("first"), {"reason": "withdrawn"}, expected_revision=1)
        with self.assertRaises(k.AssertionError): self.apply(k.empty_replay(), [retract])
        inactive = self.apply(self.apply(k.empty_replay(), [self.creation()]), [retract])
        again = k.make_operation("retract_assertion", aid("first"), {"reason": "twice"}, expected_revision=2)
        with self.assertRaisesRegex(k.AssertionError, "only an active"): self.apply(inactive, [again])
        with self.assertRaises(k.AssertionError): self.apply(inactive, [self.creation("first", "different")])

    def test_forged_incremental_checkpoint_cannot_authorize_its_own_state_hash(self):
        state = self.apply(k.empty_replay(), [self.creation()])
        changed = copy.deepcopy(state.state); changed["assertions"][0]["active"] = False
        forged = replace(state, state=changed, semantic_root=k.state_root(changed))
        with self.assertRaisesRegex(k.AssertionError, "complete retained history"):
            k.replay_ledger({"schema": k.LEDGER_SCHEMA, "fixtures": []}, initial=forged)
        changed = copy.deepcopy(state.state); changed["assertions"][0]["revision"] = True
        self.assertEqual(changed, state.state)  # Python equality is insufficient.
        forged = replace(state, state=changed)
        with self.assertRaisesRegex(k.AssertionError, "complete retained history"):
            k.replay_ledger({"schema": k.LEDGER_SCHEMA, "fixtures": []}, initial=forged)

    def test_entity_assertion_and_derived_conflict_footprints(self):
        operation = k.make_operation("assert_entity", aid("entity"), {"entity": "Q1", "attributes": {"label": "Universe"}})
        state = self.apply(k.empty_replay(), [operation])
        footprint = k.conflict_footprint(operation)
        self.assertIn("assertion/" + aid("entity"), footprint["writes"])
        self.assertIn("assertion/" + aid("entity") + "/revision/0", footprint["reads"])
        self.assertEqual(state.receipts[0]["footprints"], [{"operation_id": operation["operation_id"], **footprint}])

    def test_failed_later_operation_does_not_publish_partial_batch(self):
        initial = k.empty_replay(); saved = copy.deepcopy(initial)
        invalid = k.make_operation("retract_assertion", aid("missing"), {"reason": "withdrawn"}, expected_revision=1)
        with self.assertRaises(k.AssertionError): self.apply(initial, [self.creation(), invalid])
        self.assertEqual(initial, saved)

    def pilot_files(self, reverse=False):
        container = {"qid": "Q1", "path": "Mathlib/Algebra", "match_kind": "field", "skeptic": "accept", "confidence": "high"}
        discovery = {"src": "Q2", "dst": "decl:Mathlib:Nat.add_comm", "kind": "formalizes", "verified": True,
            "module": "Mathlib.Data.Nat.Basic", "confidence": "medium", "evidence": {"match_kind": "exact", "note": "retained"}}
        pairs = [(shadow.PILOT_PATHS[0], [container, container]), (shadow.PILOT_PATHS[1], [discovery])]
        if reverse: pairs.reverse()
        return {path: ("\n".join(json.dumps(row, sort_keys=reverse, indent=None if reverse else 0).replace("\n", " ") for row in rows) + "\n").encode() for path, rows in pairs}

    def test_shadow_pilot_preserves_duplicate_contributions_and_json_order(self):
        ledger, result, parity = shadow.import_rows("a" * 40, self.pilot_files())
        other_ledger, other, other_parity = shadow.import_rows("a" * 40, self.pilot_files(True))
        self.assertEqual(ledger, other_ledger); self.assertEqual(result, other); self.assertEqual(parity, other_parity)
        self.assertEqual(len(result.state["assertions"]), 3)
        self.assertEqual(parity[shadow.PILOT_PATHS[0]]["rows"], 2)
        for p in parity.values(): self.assertEqual(p["legacy_root"], p["shadow_root"])

    def test_real_cli_full_incremental_and_noncanonical_json_serialization_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self.apply(k.empty_replay(), [self.creation()])
            second = self.apply(first, [self.creation("second")])
            paths = {}
            for name, fixtures in (("first", first.fixtures), ("second", second.fixtures[1:]), ("full", second.fixtures)):
                paths[name] = root / (name + ".json")
                paths[name].write_text(json.dumps({"schema": k.LEDGER_SCHEMA, "fixtures": list(fixtures)}, indent=2))
            self.assertEqual(replay_cli.run(paths["full"]), replay_cli.run(paths["second"], paths["first"]))
            program = Path(k.__file__).parent / "validate_authority.py"
            result = subprocess.run([sys.executable, "-I", str(program), "--ledger", str(paths["full"])], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout); self.assertFalse(report["authority"]); self.assertEqual(report["counts"]["assertions"], 2)

    def test_cli_rejects_duplicate_json_keys_symlinks_and_oversized_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); path = root / "ledger.json"
            path.write_text('{"schema":"first","schema":"second","fixtures":[]}')
            with self.assertRaisesRegex(ValueError, "duplicate"): replay_cli.read_ledger(path)
            linked = root / "linked.json"; linked.symlink_to(path)
            with self.assertRaises(OSError): replay_cli.read_ledger(linked)
            with mock.patch.object(k, "MAX_DOCUMENT_BYTES", 8):
                with self.assertRaisesRegex(k.AssertionError, "bounded regular file"): replay_cli.read_ledger(path)

    def test_shadow_reads_exact_native_git_contributions_and_preserves_dirty_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.check_output(["/usr/bin/git", "-C", str(root), *args], stderr=subprocess.PIPE).decode().strip()
            git("init", "--quiet"); git("config", "user.name", "Fixture"); git("config", "user.email", "fixture@example.invalid")
            for path, raw in self.pilot_files().items():
                dest = root / path; dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(raw)
            git("add", *shadow.PILOT_PATHS); git("commit", "--quiet", "-m", "Exact pilot input")
            commit = git("rev-parse", "HEAD")
            dirty = root / shadow.PILOT_PATHS[1]; dirty.write_text("deliberately invalid worktree contents\n")
            before = git("status", "--porcelain")
            ledger, report = shadow.shadow(root, commit)
            self.assertEqual(report["parity"][shadow.PILOT_PATHS[0]]["rows"], 2)
            self.assertEqual(report["parity"][shadow.PILOT_PATHS[1]]["rows"], 1)
            self.assertEqual(git("status", "--porcelain"), before)
            self.assertEqual(dirty.read_text(), "deliberately invalid worktree contents\n")
            with self.assertRaisesRegex(k.AssertionError, "expected Git commit"):
                shadow.shadow(root, "f" * 40)
            state = k.replay_ledger(ledger)
            discovery = next(a for a in state.state["assertions"] if a["payload"]["attributes"]["source_path"] == shadow.PILOT_PATHS[1])
            self.assertEqual(discovery["payload"]["src"], "Q2")
            self.assertEqual(discovery["payload"]["dst"], "decl:Mathlib:Nat.add_comm")
            self.assertEqual(discovery["payload"]["kind"], "formalizes")


if __name__ == "__main__":
    unittest.main()
