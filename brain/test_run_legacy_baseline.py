"""Diagnostic legacy execution: real process failures, closed inputs and native launch policy."""
import argparse
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "tools"))
import legacy_stage_executor as child
import run_legacy_baseline as producer
import test_prepare_legacy_baseline as fixtures


class LegacyDriverTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.evidence = self.root / "evidence"; self.evidence.mkdir()

    def test_subprocess_nonzero_retains_both_actual_streams(self):
        result = child.execute([sys.executable, "-I", "-c", "import sys;print('actual stdout');print('actual stderr',file=sys.stderr);sys.exit(7)"],
            self.root, {}, self.evidence, "failed", timeout=10)
        self.assertEqual(result["exit"], 7)
        self.assertEqual((self.evidence / "failed.stdout").read_bytes(), b"actual stdout\n")
        self.assertEqual(child.measure(self.evidence / "failed.stderr"), {k: result["stderr"][k] for k in ("bytes", "sha256")})

    def test_pipe_eof_linger_has_deadline_and_retained_partial_bytes(self):
        with self.assertRaises((ValueError, __import__("subprocess").TimeoutExpired)):
            child.execute([sys.executable, "-I", "-c", "import os,time;os.write(1,b'partial');os.close(1);os.close(2);time.sleep(30)"],
                self.root, {}, self.evidence, "linger", timeout=0.3)
        self.assertEqual((self.evidence / "linger.stdout").read_bytes(), b"partial")

    def test_output_limit_retains_bounded_prefix(self):
        with self.assertRaisesRegex(ValueError, "log exceeded"):
            child.execute([sys.executable, "-I", "-c", "import os;os.write(1,b'x'*100000)"],
                self.root, {}, self.evidence, "large", timeout=10, limit=80)
        self.assertEqual((self.evidence / "large.stdout").read_bytes(), b"x" * 80)

    def test_actual_reader_interruption_preserves_logs(self):
        original = child.os.read
        seen = []
        def interrupted(fd, maximum):
            if seen and seen[-1]:
                raise KeyboardInterrupt()
            data = original(fd, maximum)
            if data:
                seen.append(data)
            return data
        with mock.patch.object(child.os, "read", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                child.execute([sys.executable, "-I", "-c", "import os,time;os.write(1,b'partial');time.sleep(.1);os.write(1,b'later');time.sleep(30)"],
                    self.root, {}, self.evidence, "interrupt", timeout=10)
        self.assertEqual((self.evidence / "interrupt.stdout").read_bytes(), b"partial")

    def test_read_rejects_symlinks_fifo_and_changed_bytes(self):
        path = self.root / "input"; path.write_bytes(b"original")
        expected = child.measure(path)
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "identity differs"): child.read(path, expected)
        link = self.root / "link"; link.symlink_to(path)
        with self.assertRaises(OSError): child.read(link)
        fifo = self.root / "fifo"; os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "bounded and regular"): child.read(fifo)

    def test_mixed_directory_mounts_are_readonly_after_output_mounts(self):
        root = self.root / "prepared"
        record = producer.prepare_probe(root)
        prefix = child.boundary(root, record, ((Path("/usr/local"), Path("/usr/local")),))
        writable = prefix.index(str(root / "output/brain/data"))
        readonly = prefix.index(str(root / "input/brain/data/probe-input.jsonl"))
        self.assertLess(writable, readonly)
        self.assertEqual(prefix[readonly - 1], "--ro-bind")
        self.assertIn("--unshare-all", prefix)
        self.assertEqual(prefix[-5:], ["--remount-ro", "/", "--chdir", str(root / "code"), "--"])
        self.assertNotIn(str(self.evidence), prefix)

    def test_stage_environment_never_uses_ambient_old_paths(self):
        environment = child.stage_environment(self.root, {"external_node_cap": 32000}, {"PATH": "/nonexistent"})
        self.assertEqual(environment["BRAIN_MATHLIB_CHECKOUT"], str(self.root / "code/inputs/mathlib/Mathlib"))
        self.assertEqual(environment["BRAIN_DECL_ORACLE"], str(self.root / "code/inputs/decl_oracle/declaration-data.json"))
        self.assertEqual(environment["BRAIN_EXT_NODE_CAP"], "32000")
        self.assertNotIn("AWS_ACCESS_KEY_ID", environment)

    def test_seven_real_fixture_processes_insert_fresh_halo_after_final_sqlite(self):
        root = self.root / "prepared"; record = producer.prepare_probe(root)
        output = root / "output"
        (root / "support/manage").mkdir(parents=True)
        (root / "support/manage/halo.py").write_bytes(b"fixture halo")
        commands = []
        def command(python, program, *args):
            index = len(commands); commands.append((program.name, args))
            script = "from pathlib import Path\no=Path(" + repr(str(output)) + ")\n"
            if index == 2:
                script += "(o/'brain/data/cells.jsonl').write_bytes(b'cells')\n(o/'brain/data/synapses.jsonl').write_bytes(b'synapses')\n"
            if index == 3:
                script += "(o/'brain/data/brain.sqlite3').write_bytes(b'final old db')\n"
            if index == 4:
                script += "assert (o/'manage/data/halo.json').read_bytes()==b'fresh halo'\n"
            script += "print('stage " + str(index + 1) + "')\n"
            return [str(python), "-I", "-c", script]
        class Halo:
            MAX_INPUT_BYTES = 1000
            @staticmethod
            def project(program, cells, synapses):
                self.assertEqual((output / "brain/data/brain.sqlite3").read_bytes(), b"final old db")
                self.assertEqual((program, cells, synapses), (b"fixture halo", b"cells", b"synapses"))
                return b"fresh halo", {"authority": False}
        stages, halo = child.run_stages(root, record, [], Path(sys.executable), {}, self.evidence, command, Halo, timeout=10)
        self.assertEqual(len(stages), 7)
        self.assertEqual([r["program"] for r in stages], [p for p, _ in child.STAGES])
        self.assertEqual(commands[2][1], ("--attach", "generalization,special_case"))
        self.assertEqual(commands[3][1], ("--from-jsonl",))
        self.assertEqual(halo["output"]["sha256"], child.identity(b"fresh halo")["sha256"])
        self.assertTrue(all(r["exit"] == 0 for r in stages))

    def test_failed_stage_stops_before_next_process_and_never_emits_completion(self):
        root = self.root / "prepared"; record = producer.prepare_probe(root)
        calls = []
        def command(python, program, *args):
            calls.append(program)
            return [str(python), "-I", "-c", "import sys;print('failure');sys.exit(9)"]
        with self.assertRaisesRegex(ValueError, "legacy stage failed"):
            child.run_stages(root, record, [], Path(sys.executable), {}, self.evidence, command, None, timeout=10)
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads((self.evidence / "stage-1.json").read_bytes())["exit"], 9)
        self.assertFalse((self.evidence / "container-result.json").exists())

    def test_completed_layout_retains_original_inputs_and_rejects_changed_output(self):
        root = self.root / "prepared"; producer.prepare_probe(root)
        record = {"legacy": {"program_files": [{"path": "brain/probe.py", **child.measure(root / "code/brain/probe.py")}]},
                  "inputs": [{"path": "brain/data/probe-input.jsonl", **child.measure(root / "code/brain/data/probe-input.jsonl")}],
                  "absences": [{"path": "brain/data/absent.jsonl"}]}
        path = root / "output/brain/data/nodes.jsonl"; path.write_bytes(b"new output")
        outputs = [{"path": "brain/data/nodes.jsonl", **child.measure(path)}]
        producer.materialize_completed(root, self.root / "completed", record, outputs)
        self.assertEqual((self.root / "completed/brain/data/probe-input.jsonl").read_bytes(), b"synthetic readonly input\n")
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "identity differs"):
            producer.materialize_completed(root, self.root / "bad", record, outputs)

    def test_real_pack_correspondence_rejects_forged_lineage(self):
        fixture = fixtures.LegacyPreparationTest(methodName="runTest")
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        record = fixture.run_prepare()
        root = fixture.destination
        hashes = {p["path"]: p["sha256"] for p in record["legacy"]["program_files"]}
        args = argparse.Namespace(prepared=root, preparation_sha256=child.measure(root / "preparation.json")["sha256"],
            preparation_bytes=(root / "preparation.json").stat().st_size, manifest=fixture.manifest,
            pack_root=fixture.pack_root, expected_pack_id=fixture.pack["offline_pack_id"])
        with mock.patch.object(child, "LEGACY_COMMIT", fixture.commit), mock.patch.object(child, "LEGACY_TREE", fixture.tree), \
             mock.patch.object(child, "PROGRAM_HASHES", hashes), mock.patch.object(child, "HALO_SHA", record["halo_program"]["sha256"]):
            self.assertEqual(producer.verified_preparation(args), record)
            record["inputs"][0]["source_manifest_id"] = "sha256:" + "a" * 64
            (root / "preparation.json").chmod(0o600)
            (root / "preparation.json").write_bytes(child.canonical(record))
            measured = child.measure(root / "preparation.json")
            args.preparation_sha256, args.preparation_bytes = measured["sha256"], measured["bytes"]
            with self.assertRaisesRegex(ValueError, "closure differs from verified pack"):
                producer.verified_preparation(args)

    def test_probe_cli_has_no_old_execution_or_receipt_acceptance_flags(self):
        parser = producer.parser()
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            parser.parse_args(["probe", "--caller-success", "true"])
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            parser.parse_args(["execute"])


if __name__ == "__main__":
    unittest.main()
