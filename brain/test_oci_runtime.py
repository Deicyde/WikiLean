#!/usr/bin/env python3
"""Hermetic OCI integrity and launch-boundary tests; no container daemon needed."""
from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / "tools")]
import execution_environment as environment
import launch_replay_oci as launcher
import oci_runtime as oci
import run_replay_v2 as runner
import package_oci_runtime as packaging
from test_execution_environment import valid_environment


def policy_fixture() -> dict:
    wheel = b"immutable test wheel bytes"
    return {"schema": oci.POLICY_SCHEMA, "architecture": "x86_64",
            "python": "/usr/local/bin/python3.12",
            "numpy": {"version": "2.3.2", "wheel": "numpy-2.3.2-cp312-cp312-manylinux_2_28_x86_64.whl",
                      "sha256": hashlib.sha256(wheel).hexdigest(), "bytes": len(wheel)},
            "cpu": {"baseline": ["SSE", "SSE2", "SSE3"], "disable": ["AVX", "AVX2"],
                    "openblas_core": "Prescott", "blas_library": "site-packages/numpy.libs/libscipy_openblas.so",
                    "blas_symbol": "scipy_openblas_get_corename64_"}}


class OCIImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.layout = self.root / "layout"
        (self.layout / "blobs" / "sha256").mkdir(parents=True)
        self.wheels = self.root / "wheels"
        self.wheels.mkdir()
        self.policy = policy_fixture()
        (self.wheels / self.policy["numpy"]["wheel"]).write_bytes(b"immutable test wheel bytes")
        self.layer_bytes = b"fixture layer content" * 31
        self.layer = self.blob(gzip.compress(self.layer_bytes, mtime=0), oci.LAYER_MEDIA + "+gzip")
        self.config = {"architecture": "amd64", "os": "linux",
                       "config": {"Labels": {oci.POLICY_LABEL: hashlib.sha256(environment.canonical_json_bytes(self.policy)).hexdigest()}},
                       "rootfs": {"type": "layers", "diff_ids": ["sha256:" + hashlib.sha256(self.layer_bytes).hexdigest()]}}
        self.config_descriptor = self.blob(json.dumps(self.config).encode(), oci.CONFIG_MEDIA)
        self.manifest = {"schemaVersion": 2, "mediaType": oci.MANIFEST_MEDIA,
                         "config": self.config_descriptor, "layers": [self.layer]}
        self.descriptor = valid_environment(environment.AUTHORITATIVE_OCI_PROFILE)
        package = self.descriptor["dependency_lock"]["packages"][0]
        package["locked_artifact_sha256"] = self.policy["numpy"]["sha256"]
        self.seal_manifest()

    def blob(self, raw: bytes, media: str) -> dict:
        digest = hashlib.sha256(raw).hexdigest()
        (self.layout / "blobs" / "sha256" / digest).write_bytes(raw)
        return {"mediaType": media, "digest": "sha256:" + digest, "size": len(raw)}

    def seal_manifest(self) -> None:
        self.manifest_digest = self.blob(json.dumps(self.manifest).encode(), oci.MANIFEST_MEDIA)["digest"]
        self.descriptor["runtime"]["manifest_digest"] = self.manifest_digest
        self.descriptor = environment.seal_execution_environment(self.descriptor)

    def verify(self):
        return oci.verify_image(self.layout, self.manifest_digest, self.policy, self.wheels, self.descriptor)

    def test_exact_platform_manifest_config_layers_and_wheel_are_verified(self):
        verified = self.verify()
        self.assertEqual(verified.config_digest, self.config_descriptor["digest"])
        self.assertEqual(verified.runtime(), self.descriptor["runtime"])
        self.assertEqual(verified.diff_ids, tuple(self.config["rootfs"]["diff_ids"]))

    def test_index_and_floating_reference_are_rejected(self):
        self.manifest["mediaType"] = "application/vnd.oci.image.index.v1+json"
        self.seal_manifest()
        with self.assertRaisesRegex(oci.OCIRuntimeError, "platform manifest"):
            self.verify()
        with self.assertRaises(environment.ExecutionEnvironmentError):
            oci.verify_image(self.layout, "python:latest", self.policy, self.wheels, self.descriptor)

    def test_changed_config_layer_and_wheel_stop_verification(self):
        for path in (self.layout / "blobs" / "sha256" / self.config_descriptor["digest"][7:],
                     self.layout / "blobs" / "sha256" / self.layer["digest"][7:],
                     self.wheels / self.policy["numpy"]["wheel"]):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b"!")
                with self.assertRaises(oci.OCIRuntimeError):
                    self.verify()
                path.write_bytes(original)

    def test_config_digest_rechecked_after_initial_blob_check(self):
        original_read = oci.read_control
        def changed_read(path):
            value, raw = original_read(path)
            if path.name == self.config_descriptor["digest"][7:]:
                raw += b" "
            return value, raw
        with mock.patch.object(oci, "read_control", side_effect=changed_read):
            with self.assertRaisesRegex(oci.OCIRuntimeError, "config changed"):
                self.verify()

    def test_diffid_and_external_url_are_rejected(self):
        self.config["rootfs"]["diff_ids"] = ["sha256:" + "d" * 64]
        self.manifest["config"] = self.blob(json.dumps(self.config).encode(), oci.CONFIG_MEDIA)
        self.seal_manifest()
        with self.assertRaisesRegex(oci.OCIRuntimeError, "DiffID"):
            self.verify()
        self.manifest["layers"][0]["urls"] = ["https://example.invalid/layer"]
        self.seal_manifest()
        with self.assertRaisesRegex(oci.OCIRuntimeError, "OCI descriptor"):
            self.verify()

    def test_expanded_layer_limit_is_enforced(self):
        with mock.patch.object(oci, "EXPANDED_LAYER_LIMIT", 400):
            with self.assertRaisesRegex(oci.OCIRuntimeError, "exceeds limit"):
                self.verify()

    def test_symlink_blob_is_rejected(self):
        path = self.layout / "blobs" / "sha256" / self.layer["digest"][7:]
        target = self.root / "outside"
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaises(environment.ExecutionEnvironmentError):
            self.verify()

    def test_implicit_volume_and_unbound_policy_are_rejected(self):
        for field, value in (("Volumes", {"/escape": {}}), ("Labels", {})):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.config)
                changed["config"][field] = value
                self.manifest["config"] = self.blob(json.dumps(changed).encode(), oci.CONFIG_MEDIA)
                self.seal_manifest()
                with self.assertRaises(oci.OCIRuntimeError):
                    self.verify()

    def test_lock_platform_and_policy_are_bound(self):
        for mutate in (
            lambda value: value["runtime"].update(architecture="aarch64"),
            lambda value: value["dependency_lock"]["packages"][0].update(locked_artifact_sha256="f" * 64),
        ):
            original = copy.deepcopy(self.descriptor)
            mutate(self.descriptor)
            self.descriptor = environment.seal_execution_environment(self.descriptor)
            with self.assertRaises(oci.OCIRuntimeError):
                self.verify()
            self.descriptor = original

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaisesRegex(oci.OCIRuntimeError, "duplicate"):
            oci._json(b'{"schema":1,"schema":2}', "fixture")


class OCILaunchBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.policy = policy_fixture()
        self.image = oci.VerifiedImage("sha256:" + "a" * 64, "sha256:" + "b" * 64,
                                       "x86_64", ("sha256:" + "c" * 64,), "d" * 64)
        self.arguments, self.child, self.mounts = launcher.create_arguments(
            self.image, Path("/private/workspace"), Path("/private/pack"), self.policy,
            uid=1000, gid=1000, name="wikilean-fixture", memory_bytes=1024**3)
        self.cid = "e" * 64
        self.container = {"Id": self.cid, "Image": self.image.config_digest,
                          "Config": {"Image": self.image.config_digest, "User": "1000:1000",
                                     "Entrypoint": ["/usr/bin/env"], "Cmd": self.child,
                                     "WorkingDir": "/private/workspace", "OpenStdin": True, "Tty": False},
                          "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False,
                                         "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges", "seccomp=unconfined"],
                                         "IpcMode": "none", "CgroupnsMode": "private", "PidsLimit": 512,
                                         "Memory": 1024**3, "MemorySwap": 1024**3,
                                         "Tmpfs": {"/tmp": "rw,nosuid,nodev,noexec,size=64m,mode=1777"}},
                          "Mounts": [{**mount, "Type": "bind", "Propagation": "rprivate"} for mount in self.mounts],
                          "State": {"Status": "created", "Running": False}}

    def verify(self, value=None, status="created"):
        return launcher.verify_container(value or self.container, image=self.image, child=self.child,
                                         mounts=self.mounts, uid=1000, gid=1000, memory_bytes=1024**3,
                                         status=status, cid=self.cid)

    def test_create_uses_exact_local_image_and_no_inherited_environment(self):
        self.verify()
        self.assertEqual(self.arguments[self.arguments.index("--pull") + 1], "never")
        self.assertIn(self.image.config_digest, self.arguments)
        self.assertEqual(self.child[0], "-i")
        self.assertIn("NPY_DISABLE_CPU_FEATURES=AVX,AVX2", self.child)
        self.assertIn("OPENBLAS_CORETYPE=Prescott", self.child)
        with mock.patch.dict(os.environ, {"DOCKER_HOST": "tcp://attacker", "LD_PRELOAD": "/evil", "OPENBLAS_NUM_THREADS": "99"}):
            self.assertEqual(launcher.launch_environment(self.policy)["OPENBLAS_NUM_THREADS"], "1")
            self.assertNotIn("DOCKER_HOST", launcher.launch_environment(self.policy))

    def test_every_material_isolation_mutation_is_rejected(self):
        edits = [
            ("Image", None, "sha256:" + "f" * 64),
            ("HostConfig", "NetworkMode", "host"), ("HostConfig", "ReadonlyRootfs", False),
            ("HostConfig", "Privileged", True), ("HostConfig", "CapAdd", ["SYS_ADMIN"]),
            ("HostConfig", "SecurityOpt", ["seccomp=unconfined"]), ("HostConfig", "PidMode", "host"),
            ("HostConfig", "Devices", [{"PathOnHost": "/dev/mem"}]),
            ("HostConfig", "Tmpfs", {}), ("Config", "User", "0:0"),
            ("Config", "Cmd", ["-i", "/bin/sh"]), ("State", "Running", True),
        ]
        for parent, key, replacement in edits:
            with self.subTest(parent=parent, key=key):
                changed = copy.deepcopy(self.container)
                if key is None:
                    changed[parent] = replacement
                else:
                    changed[parent][key] = replacement
                with self.assertRaises(launcher.OCILaunchError):
                    self.verify(changed)

    def test_mount_injection_writable_input_and_propagation_are_rejected(self):
        for mutate in (
            lambda value: value["Mounts"].append({"Type": "bind", "Source": "/", "Destination": "/host", "RW": True}),
            lambda value: value["Mounts"][0].update(RW=True),
            lambda value: value["Mounts"][0].update(Propagation="shared"),
        ):
            changed = copy.deepcopy(self.container)
            mutate(changed)
            with self.assertRaises(launcher.OCILaunchError):
                self.verify(changed)

    def test_failed_or_oom_exit_never_becomes_success(self):
        self.container["State"].update(Status="exited", ExitCode=0)
        self.verify(status="exited")
        for key, value in (("ExitCode", 1), ("OOMKilled", True), ("Error", "runtime failure")):
            changed = copy.deepcopy(self.container)
            changed["State"][key] = value
            with self.assertRaises(launcher.OCILaunchError):
                self.verify(changed, status="exited")

    def test_engine_image_must_match_verified_config_and_diffids(self):
        observation = {"Id": self.image.config_digest, "Os": "linux", "Architecture": "amd64",
                       "RootFS": {"Type": "layers", "Layers": list(self.image.diff_ids)},
                       "Config": {"Labels": {oci.POLICY_LABEL: self.image.policy_sha256}}}
        launcher.verify_engine_image(observation, self.image)
        observation["RootFS"]["Layers"] = []
        with self.assertRaisesRegex(launcher.OCILaunchError, "rootfs"):
            launcher.verify_engine_image(observation, self.image)

    def test_policy_rejects_floating_versions_partial_features_and_unknown_fields(self):
        for mutate in (
            lambda value: value["numpy"].update(version="latest"),
            lambda value: value["cpu"].update(disable=["AVX", "AVX"]),
            lambda value: value["cpu"].update(disable=[{}]),
            lambda value: value["cpu"].update(openblas_core="Haswell"),
            lambda value: value["cpu"].update(blas_library="../../evil.so"),
            lambda value: value.update(extra="ignored"),
        ):
            changed = copy.deepcopy(self.policy)
            mutate(changed)
            with self.assertRaises((oci.OCIRuntimeError, environment.ExecutionEnvironmentError)):
                oci.validate_policy(changed)

    def test_active_numpy_dispatch_is_rejected_before_blas_loading(self):
        numpy = mock.Mock()
        numpy.__version__ = "2.3.2"
        numpy._core._multiarray_umath.__cpu_baseline__ = self.policy["cpu"]["baseline"]
        numpy._core._multiarray_umath.__cpu_dispatch__ = self.policy["cpu"]["disable"]
        numpy._core._multiarray_umath.__cpu_features__ = {"AVX": False, "AVX2": True}
        with mock.patch.dict(os.environ, oci.numerical_environment(self.policy)):
            with self.assertRaisesRegex(oci.OCIRuntimeError, "enabled CPU dispatch"):
                oci.verify_numerical_runtime(self.policy, numpy_module=numpy)

    def test_arm_baseline_requires_exact_actual_openblas_reported_core(self):
        policy = copy.deepcopy(self.policy)
        policy["architecture"] = "aarch64"
        policy["cpu"].update(baseline=["ASIMD", "NEON", "NEON_FP16", "NEON_VFPV4"],
                             disable=["ASIMDFHM", "ASIMDHP", "SVE"], openblas_core="ARMV8")
        numpy = mock.Mock()
        numpy.__version__ = policy["numpy"]["version"]
        core = numpy._core._multiarray_umath
        core.__cpu_baseline__ = policy["cpu"]["baseline"]
        core.__cpu_dispatch__ = policy["cpu"]["disable"]
        core.__cpu_features__ = {feature: False for feature in policy["cpu"]["disable"]}
        library = mock.Mock()
        function = getattr(library, policy["cpu"]["blas_symbol"])
        with mock.patch.dict(os.environ, oci.numerical_environment(policy)), \
             mock.patch.object(environment, "secure_file_digest"), mock.patch.object(oci.ctypes, "CDLL", return_value=library):
            function.return_value = b"armv8"
            oci.verify_numerical_runtime(policy, numpy_module=numpy)
            for report in (b"ARMV8", b"ARMV8SVE", b"neoversen1"):
                function.return_value = report
                with self.assertRaisesRegex(oci.OCIRuntimeError, "different CPU core"):
                    oci.verify_numerical_runtime(policy, numpy_module=numpy)

    def test_no_evidence_file_or_remote_engine_flag_exists(self):
        flags = {action.dest for action in launcher.parser()._actions}
        self.assertNotIn("trusted_runtime_evidence", flags)
        self.assertNotIn("image", flags)
        direct_flags = {action.dest for action in runner._parser()._actions}
        self.assertNotIn("numerical_policy", direct_flags)
        self.assertNotIn("trusted_runtime_evidence", direct_flags)

    def test_real_child_output_limit_and_timeout_are_enforced(self):
        with tempfile.TemporaryDirectory() as root:
            for code, timeout, limit, pattern in (
                ("print('x'*10000)", 3, 1000, "byte limit"),
                ("import time;time.sleep(5)", 0.05, 1000, "timed out"),
            ):
                with self.assertRaisesRegex(launcher.OCILaunchError, pattern):
                    launcher._bounded_process([sys.executable, "-I", "-c", code], env={}, cwd=Path(root),
                                              timeout=timeout, limit=limit)

    def test_uncertain_create_cleanup_checks_ownership_before_removal(self):
        name = "wikilean-replay-" + "a" * 32
        observation = {"Id": self.cid, "Name": "/" + name,
                       "Config": {"Labels": {launcher.LAUNCH_LABEL: name}}}
        engine = mock.Mock()
        engine.command.side_effect = [(0, json.dumps([observation]).encode(), b""), (0, b"", b"")]
        launcher.cleanup_container(engine, name, name)
        self.assertEqual(engine.command.call_args.args[0], ["container", "rm", "--force", self.cid])
        observation["Config"]["Labels"] = {}
        engine.command.reset_mock()
        engine.command.side_effect = [(0, json.dumps([observation]).encode(), b"")]
        with self.assertRaisesRegex(launcher.OCILaunchError, "not owned"):
            launcher.cleanup_container(engine, name, name)
        self.assertEqual(engine.command.call_count, 1)

    def test_build_recipe_requires_base_digest_and_offline_hash_locked_wheel(self):
        with self.assertRaisesRegex(ValueError, "repository@sha256"):
            packaging.dockerfile("python:3.12", self.policy)
        recipe = packaging.dockerfile("registry.example/python@sha256:" + "f" * 64, self.policy)
        self.assertIn("--network=none", recipe)
        self.assertIn("--no-index --no-deps --require-hashes", recipe)
        self.assertIn("--force-reinstall --no-compile", recipe)
        self.assertNotIn("apt-get", recipe)
        self.assertNotIn("curl", recipe)

    def test_packaging_freezes_exact_committed_files_and_never_replaces_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            policy_path = root / "policy.json"
            policy_path.write_bytes(environment.canonical_json_bytes(self.policy))
            wheel = root / self.policy["numpy"]["wheel"]
            wheel.write_bytes(b"immutable test wheel bytes")
            destination = root / "context"
            calls = []
            def committed(command, **kwargs):
                calls.append(command)
                self.assertEqual(kwargs["env"]["GIT_NO_LAZY_FETCH"], "1")
                self.assertEqual(kwargs["env"]["GIT_ALLOW_PROTOCOL"], "")
                if "config" in command:
                    return 0, b"core.repositoryformatversion\n0\0", b""
                if command[-1] == "--show-toplevel":
                    return 0, str(root).encode() + b"\n", b""
                if command[-1] == "a" * 40 + "^{commit}":
                    return 0, b"a" * 40 + b"\n", b""
                return 0, ("# " + command[-1] + "\n").encode(), b""
            with mock.patch.object(launcher, "_bounded_process", side_effect=committed):
                record = packaging.package(git=Path("/usr/bin/git"), repo=root, commit="a" * 40,
                    base_image="example.test/base@sha256:" + "f" * 64, policy_path=policy_path,
                    wheelhouse=root, destination=destination)
            blobs = [command for command in calls if "cat-file" in command]
            self.assertEqual(len(blobs), len(runner.RUNNER_FILES))
            self.assertTrue(all(command[1] == "--no-replace-objects" and command[-1].startswith("a" * 40 + ":") for command in blobs))
            for item in record["runner_files"]:
                path = destination / "runner" / item["path"]
                self.assertEqual(environment.secure_file_digest(path), (item["sha256"], item["bytes"]))
                self.assertEqual(path.stat().st_mode & 0o777, 0o444)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o555)
            original = (destination / "build-context.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "fresh"):
                packaging.package(git=Path("/usr/bin/git"), repo=root, commit="a" * 40,
                    base_image="example.test/base@sha256:" + "f" * 64, policy_path=policy_path,
                    wheelhouse=root, destination=destination)
            self.assertEqual((destination / "build-context.json").read_bytes(), original)

    def test_packaging_rejects_promisor_and_partial_clone_before_object_reads(self):
        for key in ("remote.origin.promisor", "remote.origin.partialclonefilter", "extensions.partialClone"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                repo = root / "repo"
                repo.mkdir()
                def git(*arguments):
                    subprocess.run(["/usr/bin/git", "-C", str(repo), *arguments], check=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
                git("init")
                sentinel = root / "network-helper-invoked"
                helper = root / "remote-helper"
                helper.write_text("#!/bin/sh\n/usr/bin/touch '" + str(sentinel) + "'\nexit 1\n")
                helper.chmod(0o700)
                git("config", "remote.origin.url", "ext::" + str(helper))
                git("config", "protocol.ext.allow", "always")
                # Included settings must be rejected too, before a missing
                # commit/blob could cause Git to consult a remote helper.
                included = root / "included.config"
                subprocess.run(["/usr/bin/git", "config", "--file", str(included), key, "true"], check=True)
                git("config", "include.path", str(included))
                policy_path = root / "policy.json"
                policy_path.write_bytes(environment.canonical_json_bytes(self.policy))
                (root / self.policy["numpy"]["wheel"]).write_bytes(b"immutable test wheel bytes")
                destination = root / "context"
                with mock.patch.object(launcher, "_bounded_process", wraps=launcher._bounded_process) as process:
                    with self.assertRaisesRegex(ValueError, "promisor and partial-clone"):
                        packaging.package(git=Path("/usr/bin/git"), repo=repo, commit="a" * 40,
                            base_image="example.test/base@sha256:" + "f" * 64, policy_path=policy_path,
                            wheelhouse=root, destination=destination)
                self.assertEqual(len(process.call_args_list), 1)
                self.assertIn("config", process.call_args.args[0])
                self.assertFalse(sentinel.exists())
                self.assertFalse(destination.exists())


class BuiltinSQLiteContractTests(unittest.TestCase):
    def fixture(self):
        value = valid_environment()
        value["schema"] = environment.EXECUTION_ENVIRONMENT_SCHEMA_V2
        del value["sqlite"]["extension_file_sha256"]
        value["sqlite"]["linkage"] = {"kind": "builtin", "module_owner_sha256": "d" * 64}
        return environment.seal_execution_environment(value)

    def test_builtin_environment_is_explicitly_v2_and_v1_remains_frozen(self):
        value = self.fixture()
        self.assertIs(environment.validate_execution_environment(value), value)
        schema = json.loads((HERE / "authority/schemas/execution-environment/v2.json").read_text())
        import jsonschema
        jsonschema.Draft202012Validator(schema).validate(value)
        old_identity = value["environment_id"]
        value["schema"] = environment.EXECUTION_ENVIRONMENT_SCHEMA
        value["environment_id"] = environment.execution_environment_identity(value)
        self.assertNotEqual(old_identity, value["environment_id"])
        with self.assertRaisesRegex(environment.ExecutionEnvironmentError, "extension_file_sha256"):
            environment.validate_execution_environment(value)

    def test_v2_probe_and_unknown_linkage_fields_fail_closed(self):
        value = self.fixture()
        projection = environment.live_environment_projection(value)
        probe = {"schema": environment.LIVE_PROBE_SCHEMA_V2,
                 **{key: projection[key] for key in ("python", "numpy", "sqlite", "locale")}}
        environment.validate_live_probe_document(probe)
        probe["sqlite"]["linkage"]["guessed_extension_sha256"] = "d" * 64
        with self.assertRaisesRegex(environment.ExecutionEnvironmentError, "unknown keys"):
            environment.validate_live_probe_document(probe)

    def test_actual_host_sqlite_has_explicit_measured_linkage(self):
        import _sqlite3
        measured = environment.probe_sqlite_runtime()
        if getattr(_sqlite3, "__file__", None) is not None:
            self.assertIn("extension_file_sha256", measured)
        else:
            paths = []
            def record(path):
                paths.append(path)
                return environment.secure_file_digest(path)
            measured = environment.probe_sqlite_runtime(file_digest=record)
            self.assertEqual(measured["linkage"]["kind"], "builtin")
            self.assertEqual(len(paths), 1)
            self.assertEqual(measured["linkage"]["module_owner_sha256"], environment.secure_file_digest(paths[0])[0])
            self.assertNotIn("extension_file_sha256", measured)


if __name__ == "__main__":
    unittest.main()
