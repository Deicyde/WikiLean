"""Verify old-layout copying from real v3 evidence and immutable Git objects."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "tools"))
import prepare_legacy_baseline as prepare
import test_authority_contracts as fixtures


class LegacyPreparationTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.V3EvidenceClosureTest(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.pack, self.manifest, *_ = self.fixture.make_v3_pack()
        self.pack_root = self.fixture.root.resolve()
        self.manifest = self.manifest.resolve()
        config = {"schema": "wikilean.brain-reducer-config/v1", "external_node_cap": 8,
                  "cell_attach_kinds": ["generalization", "special_case"],
                  "layout": {"enabled": True, "iterations": 200}}
        self.replace_config(config)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "old-repo"; self.repo.mkdir()
        self.git("init", "-q")
        for path in (*prepare.PROGRAM_PATHS, "manage/halo.py"):
            file = self.repo / path; file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("# old fixture " + path + "\n")
        self.git("add", *prepare.PROGRAM_PATHS, "manage/halo.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "old fixture")
        self.commit = self.git("rev-parse", "HEAD").decode().strip()
        self.tree = self.git("rev-parse", "HEAD^{tree}").decode().strip()
        self.destination = self.root / "prepared"

    def git(self, *args):
        return subprocess.check_output(["/usr/bin/git", "-C", str(self.repo), *args], stderr=subprocess.PIPE)

    def replace_config(self, config):
        path = self.pack_root / self.pack["configuration"]["path"]
        raw = prepare.contracts.canonical_json_bytes(config); path.write_bytes(raw)
        self.pack["configuration"].update(sha256=prepare.sha(raw), bytes=len(raw))
        self.pack["offline_pack_id"] = prepare.contracts.offline_pack_identity(self.pack)
        self.manifest.write_bytes(prepare.contracts.canonical_json_bytes(self.pack))

    def run_prepare(self):
        with mock.patch.object(prepare, "LEGACY_COMMIT", self.commit), mock.patch.object(prepare, "LEGACY_TREE", self.tree):
            return prepare.prepare(self.manifest, self.pack_root, self.repo, self.destination, self.pack["offline_pack_id"])

    def test_real_pack_and_git_layout_exclude_dirty_programs_and_old_caches(self):
        (self.repo / "brain/build_cells.py").write_text("raise RuntimeError('dirty worktree')\n")
        cache = self.repo / "brain/data/cells.jsonl"; cache.parent.mkdir(); cache.write_text("old cache")
        report = self.run_prepare()
        self.assertFalse(report["authority"]); self.assertFalse(report["executed"])
        self.assertFalse(report["baseline_approved"])
        self.assertEqual(report["pack"]["offline_pack_id"], self.pack["offline_pack_id"])
        self.assertEqual(report["input_count"], 2)
        self.assertEqual(report["configuration"]["external_node_cap"], 8)
        code = self.destination / "code"
        self.assertEqual((code / "brain/build_cells.py").read_text(), "# old fixture brain/build_cells.py\n")
        self.assertFalse((code / "brain/data/cells.jsonl").exists())
        self.assertEqual((code / "inputs/external/input.json").read_bytes(), b'{"rows":[1,2]}')
        self.assertEqual(report["absences"], [{"input_id": "optional_external", "logical_root": "external",
                                              "path_pattern": "inputs/external/*_pages.jsonl"}])
        for item in report["inputs"]:
            path = code / item["path"]
            self.assertEqual(prepare.contracts.digest_file(path), (item["sha256"], item["bytes"]))
            self.assertEqual(path.stat().st_mtime_ns, prepare.FIXED_MTIME_NS)
            self.assertEqual(path.stat().st_mode & 0o777, 0o444)
        self.assertEqual(json.loads((self.destination / "preparation.json").read_bytes()), report)

    def test_wrong_expected_pack_changed_objects_or_changed_old_head_leave_no_workspace(self):
        with self.assertRaisesRegex(ValueError, "expected exact"):
            prepare.prepare(self.manifest, self.pack_root, self.repo, self.destination, "sha256:" + "0" * 64)
        self.assertFalse(self.destination.exists())
        with mock.patch.object(prepare, "LEGACY_COMMIT", "0" * 40):
            with self.assertRaisesRegex(ValueError, "old commit/tree"):
                prepare.prepare(self.manifest, self.pack_root, self.repo, self.destination, self.pack["offline_pack_id"])
        self.assertFalse(self.destination.exists())
        obj = self.pack_root / self.pack["objects"][0]["path"]
        obj.write_bytes(obj.read_bytes() + b" ")
        with self.assertRaises(prepare.contracts.VerificationError): self.run_prepare()
        self.assertFalse(self.destination.exists())

    def test_different_layout_recipe_is_not_silently_defaulted(self):
        self.replace_config({"schema": "wikilean.brain-reducer-config/v1", "external_node_cap": 8,
            "cell_attach_kinds": ["generalization", "special_case"], "layout": {"enabled": True, "iterations": 20}})
        with self.assertRaisesRegex(ValueError, "old stage recipe"): self.run_prepare()
        self.assertFalse(self.destination.exists())

    def test_absence_alias_and_code_input_collision_are_rejected(self):
        pack, _, inventory, _, objects = prepare.loaded_pack(self.manifest, self.pack_root, self.pack["offline_pack_id"])
        for mode in ("alias", "recursive-alias", "code", "code-absence", "directory-absence"):
            with self.subTest(mode=mode):
                p = copy.deepcopy(pack); inv = copy.deepcopy(inventory)
                if mode in {"alias", "recursive-alias"}:
                    next(x for x in inv["inputs"] if x["id"] == "optional_external")["path_pattern"] = "*.json" if mode == "alias" else "**/*.json"
                elif mode == "code":
                    next(x for x in inv["inputs"] if x["id"] == "source")["root"] = "repo"
                    next(x for x in p["input_bindings"] if x["input_id"] == "source")["members"][0]["path"] = "brain/build_cells.py"
                else:
                    absent = next(x for x in inv["inputs"] if x["id"] == "optional_external")
                    absent["root"] = "repo"
                    absent["path_pattern"] = "brain/*.py" if mode == "code-absence" else "site/out"
                with self.assertRaises(ValueError): prepare.input_plan(p, inv, objects)

    def test_existing_destination_is_never_adopted(self):
        self.destination.mkdir(); marker = self.destination / "keep"; marker.write_text("keep")
        with self.assertRaises(FileExistsError): self.run_prepare()
        self.assertEqual(list(self.destination.iterdir()), [marker])

    def test_mixed_input_overlay_and_retained_support_are_rechecked_before_completion(self):
        inventory_path = self.pack_root / self.pack["inventory"]["path"]
        inventory = json.loads(inventory_path.read_bytes())
        declaration = next(item for item in inventory["inputs"] if item["id"] == "source")
        declaration.update(root="repo", path="brain/data/input.json")
        inventory["inventory_id"] = prepare.contracts.reducer_input_inventory_identity(inventory)
        raw = prepare.contracts.canonical_json_bytes(inventory)
        inventory_path.write_bytes(raw)
        self.pack["inventory"].update(sha256=prepare.sha(raw), bytes=len(raw), inventory_id=inventory["inventory_id"])
        next(item for item in self.pack["input_bindings"] if item["input_id"] == "source")["members"][0]["path"] = "brain/data/input.json"
        self.pack["source_set_root"] = prepare.contracts.source_set_root_v3(
            inventory["inventory_id"], [item["source_manifest_id"] for item in self.pack["source_manifests"]], self.pack["input_bindings"])
        self.pack["offline_pack_id"] = prepare.contracts.offline_pack_identity(self.pack)
        self.manifest.write_bytes(prepare.contracts.canonical_json_bytes(self.pack))

        original_utime = prepare.os.utime
        for mode in ("clean", "overlay-bytes", "support-bytes", "support-extra", "support-fifo"):
            with self.subTest(mode=mode):
                self.destination = self.root / ("prepared-" + mode)
                def tamper(path, **kwargs):
                    path = Path(path)
                    if mode == "overlay-bytes" and path == self.destination / "input/brain/data/input.json":
                        path.chmod(0o600); path.write_bytes(b"changed"); path.chmod(0o444)
                    elif path == self.destination / "support/manage/halo.py":
                        if mode == "support-bytes":
                            path.chmod(0o600); path.write_bytes(b"changed"); path.chmod(0o444)
                        elif mode == "support-extra":
                            path.with_name("ambient.py").write_text("ambient")
                        elif mode == "support-fifo":
                            prepare.os.mkfifo(path.with_name("ambient.py"))
                    return original_utime(path, **kwargs)
                with mock.patch.object(prepare.os, "utime", side_effect=tamper):
                    if mode == "clean":
                        self.run_prepare()
                        self.assertEqual((self.destination / "input/brain/data/input.json").read_bytes(), b'{"rows":[1,2]}')
                    else:
                        with self.assertRaisesRegex(ValueError, "overlay bytes|halo program|support closure|non-regular node"):
                            self.run_prepare()
                        self.assertFalse((self.destination / "preparation.json").exists())


if __name__ == "__main__":
    unittest.main()
