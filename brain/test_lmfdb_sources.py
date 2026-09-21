"""Read-only database transaction, evidence and measured driver boundary tests."""
from __future__ import annotations

import copy
import importlib._bootstrap_external
import importlib.util
import io
import json
import os
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lmfdb_source_evidence as core
import lmfdb_sources as cli

WHEN = "2026-09-08T22:00:00Z"


class LmfdbSourcesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.programs = {n: (core.ROOT / n).read_bytes() for n in core.TOOL_FILES}
        self.packages = {"pg8000/__init__.py": b"# fixture source\n"}
        self.runtime = {"python": {"implementation": "CPython", "version": "3.12.13", "cache_tag": "cpython-312",
            "soabi": "cpython-312-darwin", "executable_file_sha256": "a" * 64},
            "postgres_driver": {"packages": {name: {"distribution": distribution, "version": version}
                for name, (distribution, version) in cli.dependencies.PACKAGES.items()},
                "module_loading": "captured-source-only; no bytecode cache",
                "files": [{"path": n, "sha256": core.sha(raw), "bytes": len(raw)} for n, raw in self.packages.items()]}}
        self.profile = {"files": [{"path": n, "sha256": core.sha(raw)} for n, raw in self.programs.items()],
            "policy": copy.deepcopy(core.POLICY), "runtime": self.runtime}
        self.profile["profile_id"] = core.profile_id(self.profile)
        registry = self.root / "profiles.json"
        registry.write_bytes(core.canonical({"schema": core.PROFILE_SCHEMA, "current_profile": self.profile["profile_id"], "profiles": [self.profile]}))
        patch = mock.patch.object(core, "REGISTRY", registry); patch.start(); self.addCleanup(patch.stop)
        self.certificate = b"fixture DER certificate"
        self.plan = {"schema": core.PLAN_SCHEMA, "source": core.SOURCE, "uri": core.URI,
            "minimum_rows": 2, "peer_certificate_sha256": core.sha(self.certificate)}
        self.tool = {"schema": core.TOOL_SCHEMA, "profile_id": self.profile["profile_id"], "files": self.profile["files"],
            "python_startup": "CPython 3.12.13 -I -S", "runtime": self.runtime}
        self.response = {"schema": "wikilean.lmfdb-query-response/v1", "read_only": "on", "isolation": "repeatable read",
            "server_version": "18.4", "snapshot": "10:12:11", "row_count": 2, "row_json_bytes": 200, "ambiguous_latest_ids": 0,
            "columns": [{"name": name, "type": kind} for name, kind in {"id": "text", "title": "text", "content": "text", "links": "ARRAY",
                "timestamp": "timestamp without time zone", "status": "smallint", "type": "smallint"}.items()],
            "rows": [{"id": "alpha", "title": "Cafe\u0301", "content": "{{ KNOWL('beta') }}", "links": ["beta"], "timestamp": "2026-09-01T12:00:00"},
                {"id": "beta", "title": "Beta", "content": None, "links": None, "timestamp": None}]}

    def body(self, response=None):
        return json.dumps(response or self.response, ensure_ascii=False).encode()

    def transport(self, raw):
        return {"peer_certificate_sha256": self.plan["peer_certificate_sha256"], "tls_version": "TLSv1.3", "cipher": "TLS_AES_256_GCM_SHA384",
            "transaction_rolled_back": True, "response_bytes": len(raw), "response_sha256": core.sha(raw)}

    def capture(self):
        raw = self.body()
        return core.capture_files(self.plan, raw, self.transport(raw), self.certificate, self.tool, self.programs, self.packages, WHEN)

    def export(self):
        return core.build_export({n: raw for n, raw in self.capture().items() if n != "manifest.json"}, self.profile, self.programs, WHEN)

    def test_complete_readonly_capture_and_export_replay_original_bytes(self):
        capture = core.archive.publish(self.capture(), self.root / "captures", core.CAPTURE_SCHEMA)
        core.verify_capture(capture)
        target = core.archive.publish(self.export(), self.root / "exports", core.EXPORT_SCHEMA)
        result = core.verify_export(target)
        self.assertEqual(result["facts"]["knowls"], 2)
        self.assertEqual((target / "acquisition/raw/query-response.json").read_bytes(), self.body())
        source = json.loads((target / "source-manifest.json").read_bytes())
        raw = next(o for o in source["objects"] if o["name"] == "knowl_query_response")
        self.assertEqual(raw["roles"], ["normalized", "raw"])
        self.assertEqual(source["pin"]["value"], raw["sha256"])
        self.assertEqual(len(source["evidence"]["request_parameter_preimages"]), 1)

    def test_snapshot_schema_truncation_latest_ambiguity_and_row_budget_fail(self):
        cases = [{"read_only": "off"}, {"isolation": "read committed"}, {"snapshot": "unknown"}, {"ambiguous_latest_ids": 1},
            {"row_count": 1}, {"row_count": 3}, {"row_json_bytes": core.POLICY["maximum_row_json_bytes"] + 1},
            {"rows": self.response["rows"][:1]}, {"columns": self.response["columns"][:-1]},
            {"rows": [self.response["rows"][0], self.response["rows"][0]]}]
        for changed in cases:
            raw = self.body({**self.response, **changed})
            with self.subTest(changed=changed), self.assertRaises(core.EvidenceError):
                core.normalize(self.plan, raw, self.transport(raw), self.certificate)

    def test_wrong_pin_incomplete_rollback_or_response_identity_fail(self):
        raw = self.body()
        for changed in ({"peer_certificate_sha256": "f" * 64}, {"tls_version": "TLSv1"}, {"transaction_rolled_back": False},
                {"response_sha256": "b" * 64}, {"response_bytes": len(raw) - 1}):
            with self.subTest(changed=changed), self.assertRaises(core.EvidenceError):
                core.normalize(self.plan, raw, {**self.transport(raw), **changed}, self.certificate)

    def test_tool_runtime_and_rehashed_dependency_substitution_rejected(self):
        runtime = copy.deepcopy(self.runtime)
        changed = {"pg8000/__init__.py": b"raise SystemExit('substitution')\n"}
        runtime["postgres_driver"]["files"] = [{"path": n, "sha256": core.sha(raw), "bytes": len(raw)} for n, raw in changed.items()]
        with self.assertRaisesRegex(core.EvidenceError, "reviewed whole generation"):
            core.validate_tool({**self.tool, "runtime": runtime}, changed)
        for startup in ("CPython 3.13.1 -I -S", "CPython 3.12.13", "CPython 3.12.14 -I -S"):
            with self.assertRaises(core.EvidenceError): core.validate_tool({**self.tool, "python_startup": startup}, self.packages)
        runtime = copy.deepcopy(self.runtime); runtime["python"].pop("soabi")
        with self.assertRaises(core.contracts.execution_environment_contract.ExecutionEnvironmentError):
            core.validate_tool({**self.tool, "runtime": runtime}, self.packages)

    def test_program_plan_certificate_and_rehashed_export_tampering_fail(self):
        files = self.capture(); files.pop("manifest.json")
        for path in ("implementation/brain/lmfdb_sources.py", "peer-certificate.der", "requests/" + core.request(self.plan)["parameters_sha256"] + ".json"):
            changed = {**files, path: files[path] + b" "}
            with self.assertRaises(core.EvidenceError): core.verify_capture_files(changed)
        files = self.export(); files.pop("manifest.json"); files["source-fragment.json"] += b" "
        target = core.archive.publish(core.archive.manifest_files(files, core.EXPORT_SCHEMA), self.root / "bad", core.EXPORT_SCHEMA)
        with self.assertRaises(core.EvidenceError): core.verify_export(target)

    def test_read_budget_rejects_advertised_oversize_before_io(self):
        stream = mock.Mock(wraps=io.BytesIO(b"abcdef"))
        bounded = cli.BoundedFile(stream, 4)
        for size in (-1, 5, 2 ** 40):
            with self.assertRaises(core.EvidenceError): bounded.read(size)
        stream.read.assert_not_called()
        self.assertEqual(bounded.read(3), b"abc")
        with self.assertRaises(core.EvidenceError): bounded.read(2)
        self.assertEqual(bounded.read(1), b"d")

    def test_tls_pin_checked_before_driver_receives_socket(self):
        wrapped = mock.Mock()
        wrapped.getpeercert.return_value = self.certificate
        wrapped.version.return_value = "TLSv1.3"
        wrapped.cipher.return_value = ("TLS_AES_256_GCM_SHA384", "TLSv1.3", 256)
        context = mock.Mock(); context.wrap_socket.return_value = wrapped
        with mock.patch.object(cli.ssl, "SSLContext", return_value=context):
            with self.assertRaisesRegex(core.EvidenceError, "reviewed explicit pin"):
                cli.PinnedTLS("f" * 64).wrap_socket(mock.Mock(), server_hostname="devmirror.lmfdb.xyz")
            wrapped.close.assert_called_once()
            good = cli.PinnedTLS(core.sha(self.certificate))
            self.assertIsInstance(good.wrap_socket(mock.Mock(), server_hostname="devmirror.lmfdb.xyz"), cli.BoundedSocket)
            self.assertEqual(good.certificate, self.certificate)
            self.assertEqual(context.minimum_version, cli.ssl.TLSVersion.TLSv1_2)
            with self.assertRaises(core.EvidenceError): good.wrap_socket(mock.Mock(), server_hostname="other.example")

    def fake_driver(self, *, bad_result=False, rollback_failure=False):
        connection, driver = mock.Mock(), mock.Mock()
        def connect(**options):
            self.connection_options = options
            tls = options["ssl_context"]
            tls.certificate = self.certificate
            tls.metadata = {k: v for k, v in self.transport(self.body()).items() if k in {"peer_certificate_sha256", "tls_version", "cipher"}}
            return connection
        driver.Connection.side_effect = connect
        def run(sql):
            if sql == core.SQL: return [] if bad_result else [[self.body().decode()]]
            if sql == core.END and rollback_failure: raise ValueError("rollback failed")
            return []
        connection.run.side_effect = run
        return driver, connection

    def test_actual_query_driver_contract_uses_only_fixed_readonly_transaction(self):
        driver, connection = self.fake_driver()
        result = cli.query(self.plan, driver)
        self.assertEqual(result[0], self.body())
        self.assertEqual([call.args[0] for call in connection.run.call_args_list], [core.BEGIN, core.SQL, core.END])
        self.assertEqual(self.connection_options["startup_params"], core.parameters(self.plan)["startup_parameters"])
        self.assertEqual(self.connection_options["application_name"], core.parameters(self.plan)["application_name"])
        self.assertIn("default_transaction_read_only=on", self.connection_options["startup_params"]["options"])
        connection.close.assert_called_once()

    def test_query_failure_always_attempts_rollback_and_close(self):
        driver, connection = self.fake_driver(bad_result=True)
        with self.assertRaises(core.EvidenceError): cli.query(self.plan, driver)
        self.assertEqual([call.args[0] for call in connection.run.call_args_list], [core.BEGIN, core.SQL, core.END])
        connection.close.assert_called_once()

    def test_failed_rollback_retains_received_body_and_never_publishes_authority(self):
        driver, connection = self.fake_driver(rollback_failure=True)
        plan_path = self.root / "plan.json"; plan_path.write_bytes(core.canonical(self.plan))
        with mock.patch.object(cli, "require_startup"), mock.patch.object(cli.dependencies, "load_driver", return_value=driver), \
                mock.patch.object(cli, "runtime_identity", return_value=(self.tool, self.programs, self.packages)), self.assertRaises(ValueError):
            cli.acquire(plan_path, self.root / "captures", self.root)
        self.assertFalse((self.root / "captures").exists())
        failed = next((self.root / "captures-incomplete").iterdir())
        files, _ = core.archive.read_bundle(failed, "wikilean.lmfdb-incomplete-attempt/v1")
        self.assertEqual(files["received-query-response.json"], self.body())
        self.assertFalse(json.loads(files["failure.json"])["authority"])
        self.assertFalse(any("receipt" in name for name in files))
        connection.close.assert_called_once()

    def test_runtime_or_plan_mutation_after_query_cannot_publish(self):
        driver, _ = self.fake_driver()
        plan_path = self.root / "plan.json"; plan_path.write_bytes(core.canonical(self.plan))
        with mock.patch.object(cli, "require_startup"), mock.patch.object(cli.dependencies, "load_driver", return_value=driver), \
                mock.patch.object(cli, "runtime_identity", side_effect=[(self.tool, self.programs, self.packages), ({}, {}, {})]), \
                self.assertRaisesRegex(core.EvidenceError, "changed during acquisition"):
            cli.acquire(plan_path, self.root / "captures", self.root)
        self.assertFalse((self.root / "captures").exists())

    def test_close_failure_retains_complete_received_response_and_attempts_both_closes(self):
        driver, connection = self.fake_driver()
        original = driver.Connection.side_effect
        socket = mock.Mock(); socket.close.side_effect = OSError("socket cleanup failed")
        def connect(**options):
            result = original(**options)
            options["ssl_context"].socket = socket
            return result
        driver.Connection.side_effect = connect
        connection.close.side_effect = ValueError("connection cleanup failed")
        plan_path = self.root / "plan.json"; plan_path.write_bytes(core.canonical(self.plan))
        with mock.patch.object(cli, "require_startup"), mock.patch.object(cli.dependencies, "load_driver", return_value=driver), \
                mock.patch.object(cli, "runtime_identity", return_value=(self.tool, self.programs, self.packages)), self.assertRaises(ValueError) as caught:
            cli.acquire(plan_path, self.root / "captures", self.root)
        self.assertFalse((self.root / "captures").exists())
        failed = next((self.root / "captures-incomplete").iterdir())
        files, _ = core.archive.read_bundle(failed, "wikilean.lmfdb-incomplete-attempt/v1")
        self.assertEqual(files["received-query-response.json"], self.body())
        connection.close.assert_called_once(); socket.close.assert_called_once()
        self.assertIn("OSError", caught.exception.__notes__[0])

    def test_cleanup_errors_preserve_original_query_failure(self):
        driver, connection = self.fake_driver(bad_result=True)
        connection.close.side_effect = ValueError("cleanup failed")
        with self.assertRaisesRegex(core.EvidenceError, "unexpected shape") as caught:
            cli.query(self.plan, driver)
        self.assertIn("ValueError", caught.exception.__notes__[0])

    def test_source_only_driver_ignores_timestamp_valid_malicious_bytecode(self):
        source_root = Path(sysconfig.get_paths()["purelib"]).resolve()
        retained = cli.dependencies.capture_files(source_root)
        target = self.root / "packages"; target.mkdir()
        for name, raw in retained.items():
            path = target / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
        init = target / "pg8000/__init__.py"
        bytecode = Path(importlib.util.cache_from_source(str(init)))
        bytecode.parent.mkdir()
        evil = compile("raise AssertionError('unmeasured valid bytecode executed')", str(init), "exec")
        bytecode.write_bytes(importlib._bootstrap_external._code_to_timestamp_pyc(evil, int(init.stat().st_mtime), init.stat().st_size))
        script = ("import sys\nfrom pathlib import Path\nsys.path.insert(0," + repr(str(core.ROOT / "brain")) + ")\n"
            "import lmfdb_source_dependencies as d\nd.load_driver(Path(" + repr(str(target)) + "))\n"
            "runtime,files=d.capture()\nassert len(files)>80\nimport importlib.metadata\nassert importlib.metadata.version('scramp')=='1.4.17'\n"
            "import six.moves.urllib_parse\nassert six.moves.urllib_parse.quote('a b')=='a%20b'\nprint('retained-source-pass')\n")
        result = subprocess.run([sys.executable, "-I", "-S", "-c", script], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("retained-source-pass", result.stdout)

    def test_driver_metadata_symlink_and_preloaded_namespace_rejected(self):
        with mock.patch.dict(sys.modules, {"pg8000": mock.Mock()}), self.assertRaisesRegex(ValueError, "fresh isolated"):
            cli.dependencies.load_driver(self.root)
        retained = cli.dependencies.capture_files(Path(sysconfig.get_paths()["purelib"]).resolve())
        target = self.root / "packages"; target.mkdir()
        for name, raw in retained.items():
            path = target / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
        metadata = target / "pg8000-1.31.5.dist-info/METADATA"
        metadata.write_bytes(metadata.read_bytes().replace(b"Version: 1.31.5", b"Version: 0.0.0"))
        with self.assertRaisesRegex(ValueError, "metadata differs"): cli.dependencies.capture_files(target)
        metadata.write_bytes(retained["pg8000-1.31.5.dist-info/METADATA"])
        link = target / "pg8000/unsafe.py"; link.symlink_to(metadata)
        with self.assertRaisesRegex(ValueError, "symlink"): cli.dependencies.capture_files(target)

    def test_actual_v3_compiler_accepts_readonly_snapshot_parent(self):
        import test_compile_offline_pack_v2 as fixtures
        target = core.archive.publish(self.export(), self.root / "exports", core.EXPORT_SCHEMA)
        fragment = json.loads((target / "source-fragment.json").read_bytes())
        fixture = fixtures.OfflinePackCompilerTest(); fixture.setUp(); self.addCleanup(fixture.tearDown)
        fixture._upgrade_plan_v3()
        source = fragment["sources"][0]
        body = next(o for o in source["objects"] if o["name"] == "knowl_query_response")
        (fixture.external / "lmfdb-response.json").write_bytes((target / body["path"]).read_bytes())
        fixture.inventory["inputs"].append({"id": "lmfdb-response", "class": "immutable_source_object", "cardinality": "one", "root": "external",
            "path": "lmfdb-response.json", "consumers": ["brain/replay.py"], "purpose": "read-only LMFDB snapshot fixture", "requirement": "required"})
        fixture.inventory["inputs"].sort(key=lambda x: x["id"])
        fixture.inventory["inventory_id"] = core.contracts.reducer_input_inventory_identity(fixture.inventory)
        fixture.plan["inventory_id"] = fixture.inventory["inventory_id"]
        fixture.plan["sources"] = sorted([*fixture.plan["sources"], source], key=lambda x: x["source"])
        fixture.plan["input_bindings"].append({"input_id": "lmfdb-response", "state": "present", "sources": [core.SOURCE],
            "members": [{"path": "lmfdb-response.json", "source": core.SOURCE, "object": "knowl_query_response"}]})
        fixture.plan["input_bindings"].sort(key=lambda x: x["input_id"])
        fixture.inventory_path.write_bytes(core.canonical(fixture.inventory)); fixture.plan_path.write_bytes(core.canonical(fixture.plan))
        packed = fixtures.compiler.compile_offline_pack_v2(fixture.plan_path.resolve(), fixture.inventory_path.resolve(),
            (fixture.base / "lmfdb-pack").resolve(), roots={"repo": fixture.repo.resolve(), "external": fixture.external.resolve(), fragment["physical_root"]: target}, git_executable="/usr/bin/git")
        manifest, _ = core.contracts.load_canonical_json(packed.manifest_path)
        core.contracts.verify_offline_pack_files(manifest, packed.root, manifest_path=packed.manifest_path)


if __name__ == "__main__":
    unittest.main()
