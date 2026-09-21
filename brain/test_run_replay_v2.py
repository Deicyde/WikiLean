#!/usr/bin/env python3
"""Hermetic tests for the sealed full-DAG replay executor."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


HERE = Path(__file__).resolve().parent
TOOLS = HERE / "tools"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(TOOLS))

import build_context  # noqa: E402
import execution_environment as execution_env  # noqa: E402
import run_offline  # noqa: E402
import run_replay_v2 as runner  # noqa: E402
from test_build_context import _document  # noqa: E402


def _environment_document(reducer_git_commit: str) -> dict[str, object]:
    runtime = runner._development_runtime_facts()
    operating_system = runtime["os"]
    value: dict[str, object] = {
        "schema": execution_env.EXECUTION_ENVIRONMENT_SCHEMA,
        "environment_id": "sha256:" + "0" * 64,
        "profile": execution_env.DEVELOPMENT_HOST_PROFILE,
        "runtime": runtime,
        "runner": {
            "name": "wikilean-replay",
            "version": "2.0.0",
            "git_commit": reducer_git_commit,
            "files_root": runner._runner_files_root(),
        },
        "python": execution_env.probe_python_runtime(
            executable_path=Path(sys.executable).resolve()
        ),
        "dependency_lock": {
            "schema": execution_env.DEPENDENCY_LOCK_SCHEMA,
            "packages": [
                {
                    "name": "numpy",
                    "version": "2.3.2",
                    "locked_artifact_sha256": "4" * 64,
                    "installed_tree_root": "sha256:" + "5" * 64,
                }
            ],
        },
        "sqlite": {
            "version": "3.50.4",
            "source_id": "2030-01-02 03:04:05 " + "6" * 64,
            "extension_file_sha256": "7" * 64,
            "compile_options": ["ENABLE_FTS5", "THREADSAFE=1"],
        },
        "locale": {
            "lang": "C.UTF-8",
            "lc_all": "C.UTF-8",
            "timezone": "UTC",
            "preferred_encoding": "utf-8",
            "filesystem_encoding": "utf-8",
            "utf8_mode": 1,
            "python_hash_seed": "0",
            "hash_sentinel": "123456789",
        },
        "sandbox": {
            "backend": (
                "darwin-sandbox-exec"
                if operating_system == "darwin"
                else "linux-bubblewrap"
            ),
            "reported_version": (
                None if operating_system == "darwin" else "1.0.0"
            ),
            "executable_sha256": "8" * 64,
            "policy_id": "brain-replay-v1",
            "policy_root": execution_env.sandbox_policy_root(
                runner._sandbox_policy_document(
                    "darwin-sandbox-exec"
                    if operating_system == "darwin"
                    else "linux-bubblewrap"
                )
            ),
            "network": "disabled",
        },
    }
    return execution_env.seal_execution_environment(value)


class ReplayRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve() / "workspace"
        self.base.mkdir()
        self.base.chmod(0o700)
        for name in ("code", "input", "output", "scratch"):
            path = self.base / name
            path.mkdir()
            path.chmod(0o700)

        document = copy.deepcopy(_document(self.base))
        for binding in document["bindings"]:
            for member in binding["members"]:
                path = Path(member["materialized_path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                data = (member["object"] + "\n").encode("utf-8")
                path.write_bytes(data)
                member["bytes"] = len(data)
                member["sha256"] = hashlib.sha256(data).hexdigest()

        for stage in document["stages"]:
            program = self.base / "code" / stage["program"]
            program.parent.mkdir(parents=True, exist_ok=True)
            program.write_text("# sealed fixture reducer\n", encoding="utf-8")

        self.reducer_files = tuple(
            (
                path.relative_to(self.base / "code").as_posix(),
                path.stat().st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in sorted((self.base / "code").rglob("*"))
            if path.is_file()
        )

        self.environment_document = _environment_document(
            document["replay"]["reducer"]["git_commit"]
        )
        self.environment_bytes = execution_env.canonical_json_bytes(
            self.environment_document
        )
        document["replay"]["reducer"]["environment_sha256"] = hashlib.sha256(
            self.environment_bytes
        ).hexdigest()
        document["generation_id"] = build_context.generation_identity(document)
        self.context = build_context.BuildContext.from_document(document)
        self.context_path = self.base / "build-context.json"
        self.context_path.write_bytes(
            build_context.canonical_json_bytes(self.context.to_document())
        )
        self.context_path.chmod(0o444)
        self.environment_path = self.base / runner.EXECUTION_ENVIRONMENT_NAME
        self.environment_path.write_bytes(self.environment_bytes)
        self.environment_path.chmod(0o444)
        self._make_read_only(self.base / "input")
        self._make_read_only(self.base / "code")
        self.isolation = runner.IsolationBoundary(
            self.environment_document["sandbox"]["backend"], ("fixture",)
        )
        self.probe_document = {
            "schema": execution_env.LIVE_PROBE_SCHEMA,
            "python": copy.deepcopy(self.environment_document["python"]),
            "numpy": {
                key: copy.deepcopy(value)
                for key, value in self.environment_document["dependency_lock"][
                    "packages"
                ][0].items()
                if key != "locked_artifact_sha256"
            },
            "sqlite": copy.deepcopy(self.environment_document["sqlite"]),
            "locale": copy.deepcopy(self.environment_document["locale"]),
        }
        self.probe_bytes = execution_env.canonical_json_bytes(self.probe_document)
        self.probe_invocations: list[
            tuple[tuple[str, ...], Path, dict[str, str], runner.IsolationBoundary]
        ] = []

    def tearDown(self) -> None:
        self._make_writable(self.base)
        self.temp.cleanup()

    @staticmethod
    def _make_read_only(root: Path) -> None:
        for directory, _names, filenames in os.walk(root, topdown=False):
            directory_path = Path(directory)
            for name in filenames:
                (directory_path / name).chmod(0o444)
            directory_path.chmod(0o555)

    @staticmethod
    def _make_writable(root: Path) -> None:
        if not root.exists():
            return
        for directory, names, filenames in os.walk(root, topdown=True):
            directory_path = Path(directory)
            try:
                directory_path.chmod(0o700)
            except OSError:
                pass
            for name in names:
                path = directory_path / name
                if not path.is_symlink():
                    try:
                        path.chmod(0o700)
                    except OSError:
                        pass
            for name in filenames:
                path = directory_path / name
                if not path.is_symlink():
                    try:
                        path.chmod(0o600)
                    except OSError:
                        pass

    def _mkdir_output_parent(self, path: Path) -> None:
        current = self.context.roots.output
        for part in path.parent.relative_to(current).parts:
            current = current / part
            current.mkdir(exist_ok=True)
            current.chmod(0o700)

    def _write_stage_outputs(self, stage_id: str) -> None:
        stage = self.context.stage(stage_id)
        for output in stage.outputs:
            path = self.context.output_for(stage_id, output.path)
            self._mkdir_output_parent(path)
            if output.kind == "file":
                path.write_bytes((stage_id + "\n").encode("utf-8"))
                path.chmod(0o600 if stage_id == "sqlite-with-cells" else 0o644)
            else:
                path.mkdir()
                path.chmod(0o700)
                child = path / "fixture.json"
                child.write_bytes(b"{}")
                child.chmod(0o644)

    @staticmethod
    def _stage_id(command: tuple[str, ...]) -> str:
        return command[command.index("--stage-id") + 1]

    def _successful_probe(
        self,
        command: tuple[str, ...],
        cwd: Path,
        child_environment: dict[str, str],
        isolation: runner.IsolationBoundary,
    ) -> tuple[int, bytes, bytes]:
        probe_path = Path(command[7])
        self.assertTrue(probe_path.is_file())
        self.assertEqual(stat.S_IMODE(probe_path.stat().st_mode), 0o444)
        support_module = probe_path.parent / "execution_environment.py"
        self.assertTrue(support_module.is_file())
        self.assertEqual(stat.S_IMODE(support_module.stat().st_mode), 0o444)
        self.probe_invocations.append(
            (command, cwd, dict(child_environment), isolation)
        )
        return 0, self.probe_bytes, b""

    def _run(self, **kwargs):
        arguments = {
            "reducer_files": self.reducer_files,
            "expected_generation_id": self.context.generation_id,
            "expected_offline_pack_id": self.context.replay.offline_pack_id,
            "expected_source_set_root": self.context.replay.source_set_root,
            "expected_reducer_inventory_id": self.context.replay.reducer_inventory_id,
            "expected_reducer_git_commit": self.context.replay.reducer_git_commit,
            "expected_configuration_sha256": self.context.replay.configuration_sha256,
            "expected_environment_sha256": self.context.replay.environment_sha256,
            "_probe_executor": self._successful_probe,
            "_sandbox_probe": lambda _isolation: copy.deepcopy(
                self.environment_document["sandbox"]
            ),
            **kwargs,
        }
        with mock.patch.object(runner, "require_isolated_startup"):
            return runner.run_replay_v2(self.context_path, **arguments)

    def _replace_environment(self, data: bytes, *, rebind: bool) -> None:
        if self.environment_path.exists() and not self.environment_path.is_symlink():
            self.environment_path.chmod(0o600)
        self.environment_path.write_bytes(data)
        self.environment_path.chmod(0o444)
        if not rebind:
            return
        document = self.context.to_document()
        document["replay"]["reducer"]["environment_sha256"] = hashlib.sha256(
            data
        ).hexdigest()
        document["generation_id"] = build_context.generation_identity(document)
        self.context = build_context.BuildContext.from_document(document)
        self.context_path.chmod(0o600)
        self.context_path.write_bytes(
            build_context.canonical_json_bytes(self.context.to_document())
        )
        self.context_path.chmod(0o444)

    def _set_environment_document(self, value: dict[str, object]) -> None:
        self.environment_document = execution_env.seal_execution_environment(value)
        self.environment_bytes = execution_env.canonical_json_bytes(
            self.environment_document
        )
        self.probe_document = {
            "schema": execution_env.LIVE_PROBE_SCHEMA,
            "python": copy.deepcopy(self.environment_document["python"]),
            "numpy": {
                key: copy.deepcopy(item)
                for key, item in self.environment_document["dependency_lock"][
                    "packages"
                ][0].items()
                if key != "locked_artifact_sha256"
            },
            "sqlite": copy.deepcopy(self.environment_document["sqlite"]),
            "locale": copy.deepcopy(self.environment_document["locale"]),
        }
        self.probe_bytes = execution_env.canonical_json_bytes(self.probe_document)
        self._replace_environment(self.environment_bytes, rebind=True)

    def _assert_pre_execution_rejected(self, pattern: str) -> None:
        with mock.patch.object(runner, "_select_isolation") as select, mock.patch.object(
            runner, "_execute"
        ) as execute:
            with self.assertRaisesRegex(runner.ReplayExecutionError, pattern):
                self._run()
        select.assert_not_called()
        execute.assert_not_called()

    def test_executes_exact_schedule_with_sanitized_environment(self) -> None:
        calls: list[tuple[tuple[str, ...], dict[str, str], str]] = []

        def execute(command, cwd, environment, isolation) -> int:
            stage_id = self._stage_id(command)
            calls.append((command, dict(environment), isolation.name))
            self.assertEqual(cwd, self.context.roots.code)
            self._write_stage_outputs(stage_id)
            return 0

        with mock.patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "secret",
                "BRAIN_EXTERNAL_DIR": "/host/decoy",
                "PYTHONPATH": "/host/injection",
            },
            clear=False,
        ):
            launcher = self.base.parent / "python-launcher"
            launcher.symlink_to(Path(sys.executable))
            result = self._run(
                interpreter=launcher,
                _executor=execute,
                _isolation=self.isolation,
            )

        expected_ids = tuple(stage.id for stage in self.context.stages)
        self.assertEqual(result.stages, expected_ids)
        self.assertEqual([self._stage_id(call[0]) for call in calls], list(expected_ids))
        self.assertEqual(
            calls[-1][2], self.environment_document["sandbox"]["backend"]
        )
        for (command, environment, _name), stage in zip(calls, self.context.stages):
            self.assertEqual(command[0], str(Path(sys.executable).resolve()))
            self.assertEqual(command[1:6], ("-P", "-S", "-s", "-B", "-c"))
            self.assertEqual(command[6], runner._RUN_STAGE)
            self.assertEqual(Path(command[7]), self.context.code(stage.program))
            context_index = command.index("--build-context")
            self.assertEqual(
                command[8:context_index],
                stage.argv,
            )
            self.assertEqual(command[context_index + 1], str(self.context_path))
            self.assertEqual(environment["PYTHONHASHSEED"], "0")
            self.assertEqual(environment["PYTHONUTF8"], "1")
            self.assertEqual(environment["PYTHONIOENCODING"], "utf-8")
            self.assertEqual(environment["WIKILEAN_OFFLINE"], "1")
            for name in (
                "BLIS_NUM_THREADS",
                "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS",
                "OMP_NUM_THREADS",
                "OMP_THREAD_LIMIT",
                "OPENBLAS_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS",
            ):
                self.assertEqual(environment[name], "1")
            self.assertNotIn("ANTHROPIC_API_KEY", environment)
            self.assertNotIn("BRAIN_EXTERNAL_DIR", environment)
            self.assertEqual(
                environment["PYTHONPATH"],
                runner._runtime_pythonpath(Path(sys.executable).resolve()),
            )
            self.assertNotIn("/host/injection", environment["PYTHONPATH"])
        self.assertIn("brain-page", result.stages)
        self.assertEqual(len(self.probe_invocations), 2)
        probe_command, probe_cwd, probe_environment, probe_isolation = (
            self.probe_invocations[0]
        )
        self.assertEqual(probe_cwd, self.context.roots.code)
        self.assertIs(probe_isolation, self.isolation)
        self.assertEqual(probe_command[:7], calls[0][0][:7])
        self.assertEqual(Path(probe_command[7]).name, runner.PROBE_PROGRAM.name)
        self.assertEqual(
            Path(probe_command[7]).parent.parent, self.context.roots.scratch
        )
        scheme_paths = runner._runtime_scheme_paths(Path(sys.executable).resolve())
        self.assertEqual(
            probe_command[8:],
            (
                "--purelib",
                str(scheme_paths["purelib"]),
                "--platlib",
                str(scheme_paths["platlib"]),
            ),
        )
        self.assertEqual(probe_environment, calls[0][1])
        self.assertEqual(list(self.context.roots.scratch.iterdir()), [])

    def test_exact_child_probe_uses_parent_venv_scheme_and_ignores_scripts(
        self,
    ) -> None:
        fixture_root = self.base.parent / "probe-venv"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "venv",
                "--without-pip",
                str(fixture_root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        venv_python = fixture_root / "bin" / "python"
        scheme_result = subprocess.run(
            [
                str(venv_python),
                "-I",
                "-c",
                (
                    "import json,sys,sysconfig;"
                    "print(json.dumps({'prefix':sys.prefix,"
                    "'base_prefix':sys.base_prefix,'paths':sysconfig.get_paths()},"
                    "sort_keys=True))"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(scheme_result.returncode, 0, scheme_result.stderr)
        scheme_document = json.loads(scheme_result.stdout)
        self.assertNotEqual(
            Path(scheme_document["prefix"]).resolve(),
            Path(scheme_document["base_prefix"]).resolve(),
        )
        purelib = Path(scheme_document["paths"]["purelib"]).resolve()
        package = purelib / "numpy"
        libraries = purelib / "numpy.libs"
        dynamic_libraries = purelib / "numpy.dylibs"
        metadata = purelib / "numpy-2.3.2.dist-info"
        scripts = fixture_root / "bin"
        headers = fixture_root / "include" / "numpy"
        for path in (package, libraries, dynamic_libraries, metadata, headers):
            path.mkdir(parents=True, exist_ok=True)
        package.joinpath("__init__.py").write_text(
            "__version__ = '2.3.2'\n", encoding="utf-8"
        )
        package.joinpath("unrecorded.py").write_text("VALUE = 1\n", encoding="utf-8")
        libraries.joinpath("libnumpy.so").write_bytes(b"fixture so\n")
        dynamic_libraries.joinpath("libnumpy.dylib").write_bytes(b"fixture dylib\n")
        metadata.joinpath("METADATA").write_text(
            "Metadata-Version: 2.1\nName: numpy\nVersion: 2.3.2\n",
            encoding="utf-8",
        )
        f2py = scripts / "f2py"
        numpy_config = scripts / "numpy-config"
        header = headers / "arrayobject.h"
        f2py.write_bytes(b"#!/bin/sh\n")
        numpy_config.write_bytes(b"#!/bin/sh\n")
        header.write_bytes(b"fixture header\n")

        def relative_to_purelib(path: Path) -> str:
            return Path(os.path.relpath(path, purelib)).as_posix()

        record_entries = (
            "numpy/__init__.py",
            "numpy-2.3.2.dist-info/METADATA",
            "numpy-2.3.2.dist-info/RECORD",
            relative_to_purelib(f2py),
            relative_to_purelib(numpy_config),
            relative_to_purelib(header),
        )
        metadata.joinpath("RECORD").write_text(
            "".join(f"{path},,\n" for path in record_entries),
            encoding="utf-8",
        )

        expected_files: dict[str, Path] = {}
        for tree in (package, libraries, dynamic_libraries, metadata):
            for path in sorted(tree.rglob("*")):
                if path.is_file():
                    expected_files[
                        "site-packages/" + path.relative_to(purelib).as_posix()
                    ] = path
        expected_root = execution_env.numpy_installed_tree_root(expected_files)
        outer_program = textwrap.dedent(
            f"""
            import json
            import sys
            from pathlib import Path

            sys.path.insert(0, {str(TOOLS)!r})
            import run_replay_v2 as runner

            interpreter = Path(sys.executable).resolve(strict=True)
            scheme = runner._runtime_scheme_paths(interpreter)
            command = runner._python_command(
                interpreter,
                runner.PROBE_PROGRAM,
                "--purelib",
                str(scheme["purelib"]),
                "--platlib",
                str(scheme["platlib"]),
            )
            returncode, stdout, stderr = runner._capture_process(
                command,
                Path({str(self.base.parent)!r}),
                runner._environment(interpreter),
                timeout_seconds=10,
                stdout_limit=runner.PROBE_STDOUT_LIMIT,
                stderr_limit=runner.PROBE_STDERR_LIMIT,
            )
            print(json.dumps({{
                "parent_prefix": sys.prefix,
                "parent_base_prefix": sys.base_prefix,
                "returncode": returncode,
                "stdout": stdout.decode("utf-8"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            }}, sort_keys=True))
            """
        )

        def run_probe() -> dict[str, object]:
            completed = subprocess.run(
                [str(venv_python), "-I", "-c", outer_program],
                env={
                    "HOME": str(self.base.parent),
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PATH": "/nonexistent",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "TZ": "UTC",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            wrapper = json.loads(completed.stdout)
            self.assertNotEqual(wrapper["parent_prefix"], wrapper["parent_base_prefix"])
            self.assertEqual(wrapper["returncode"], 0, wrapper["stderr"])
            return json.loads(wrapper["stdout"])

        first = run_probe()
        self.assertEqual(first["numpy"]["version"], "2.3.2")
        self.assertEqual(first["numpy"]["installed_tree_root"], expected_root)
        f2py.write_bytes(b"#!/bin/sh\necho changed\n")
        numpy_config.write_bytes(b"#!/bin/sh\necho changed\n")
        header.write_bytes(b"changed header\n")
        self.assertEqual(
            run_probe()["numpy"]["installed_tree_root"],
            expected_root,
        )

    def test_environment_file_is_required_before_execution(self) -> None:
        self.environment_path.unlink()
        self._assert_pre_execution_rejected("prepared closure")

    def test_environment_digest_tampering_is_rejected_before_execution(self) -> None:
        self._replace_environment(self.environment_bytes + b"\n", rebind=False)
        self._assert_pre_execution_rejected("does not match.*environment_sha256")

    def test_noncanonical_environment_is_rejected_before_execution(self) -> None:
        noncanonical = json.dumps(
            self.environment_document,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        self._replace_environment(noncanonical, rebind=True)
        self._assert_pre_execution_rejected("not canonical-json-v1")

    def test_invalid_environment_is_rejected_before_execution(self) -> None:
        invalid = copy.deepcopy(self.environment_document)
        invalid["environment_id"] = "sha256:" + "0" * 64
        data = execution_env.canonical_json_bytes(invalid)
        self._replace_environment(data, rebind=True)
        self._assert_pre_execution_rejected("environment_id: expected")

    def test_environment_runner_git_must_match_context(self) -> None:
        mismatched = copy.deepcopy(self.environment_document)
        mismatched["runner"]["git_commit"] = "c" * 40
        data = execution_env.canonical_json_bytes(
            execution_env.seal_execution_environment(mismatched)
        )
        self._replace_environment(data, rebind=True)
        self._assert_pre_execution_rejected("runner Git commit does not match")

    def test_parent_runtime_runner_and_python_must_match_before_sandbox(self) -> None:
        mutations = (
            (
                "runtime",
                lambda value: value["runtime"].update(
                    host_fingerprint="sha256:" + "f" * 64
                ),
                "live runtime identity",
            ),
            (
                "runner",
                lambda value: value["runner"].update(
                    files_root="sha256:" + "f" * 64
                ),
                "runner file closure",
            ),
            (
                "python",
                lambda value: value["python"].update(
                    executable_file_sha256="f" * 64
                ),
                "parent Python interpreter",
            ),
        )
        original = copy.deepcopy(self.environment_document)
        for label, mutate, pattern in mutations:
            with self.subTest(label=label):
                changed = copy.deepcopy(original)
                mutate(changed)
                self._set_environment_document(changed)
                self._assert_pre_execution_rejected(pattern)
        self._set_environment_document(original)

    def test_authoritative_oci_requires_private_launcher_evidence(self) -> None:
        authoritative = copy.deepcopy(self.environment_document)
        authoritative["profile"] = execution_env.AUTHORITATIVE_OCI_PROFILE
        authoritative["runtime"] = {
            "kind": "oci-image",
            "os": "linux",
            "architecture": platform.machine().lower(),
            "manifest_digest": "sha256:" + "d" * 64,
        }
        authoritative["sandbox"].update(
            backend="linux-bubblewrap", reported_version="0.11.0"
        )
        self._set_environment_document(authoritative)
        self._assert_pre_execution_rejected("requires explicit trusted runtime evidence")

        evidence = {
            "schema": execution_env.TRUSTED_RUNTIME_EVIDENCE_SCHEMA,
            "profile": execution_env.AUTHORITATIVE_OCI_PROFILE,
            "runtime": copy.deepcopy(authoritative["runtime"]),
        }
        with mock.patch.object(
            runner, "_host_operating_system", return_value="linux"
        ), mock.patch.object(
            runner.platform,
            "machine",
            return_value=authoritative["runtime"]["architecture"],
        ):
            self.assertEqual(
                runner._runtime_facts(
                    execution_env.AUTHORITATIVE_OCI_PROFILE, evidence
                ),
                authoritative["runtime"],
            )
        self.assertNotIn(
            "trusted_runtime_evidence",
            {action.dest for action in runner._parser()._actions},
        )

    def test_sandbox_identity_mismatch_stops_before_probe_or_stage(self) -> None:
        sandbox = copy.deepcopy(self.environment_document["sandbox"])
        sandbox["policy_root"] = "sha256:" + "f" * 64
        probe = mock.Mock()
        execute = mock.Mock()
        with self.assertRaisesRegex(
            runner.ReplayExecutionError, "sealed descriptor.*sandbox.policy_root"
        ):
            self._run(
                _isolation=self.isolation,
                _sandbox_probe=lambda _isolation: sandbox,
                _probe_executor=probe,
                _executor=execute,
            )
        probe.assert_not_called()
        execute.assert_not_called()

    def test_probe_failures_and_mismatches_stop_before_stage_one(self) -> None:
        mismatched = copy.deepcopy(self.probe_document)
        mismatched["numpy"]["version"] = "2.3.3"
        cases = (
            (
                "malformed",
                lambda *_args: (0, b"{", b""),
                "probe output is invalid",
            ),
            (
                "noncanonical-extra-bytes",
                lambda *_args: (0, self.probe_bytes + b"\n", b""),
                "not canonical-json-v1",
            ),
            (
                "extra-member",
                lambda *_args: (
                    0,
                    execution_env.canonical_json_bytes(
                        {**self.probe_document, "unexpected": True}
                    ),
                    b"",
                ),
                "unknown keys",
            ),
            (
                "nonzero",
                lambda *_args: (19, b"", b"probe failed"),
                "failed with status 19",
            ),
            (
                "timeout",
                lambda *args: (_ for _ in ()).throw(
                    subprocess.TimeoutExpired(args[0], 1)
                ),
                "timed out",
            ),
            (
                "mismatch",
                lambda *_args: (
                    0,
                    execution_env.canonical_json_bytes(mismatched),
                    b"",
                ),
                r"descriptor at \$\.numpy\.version",
            ),
            (
                "oversized",
                lambda *_args: (0, b"x" * (runner.PROBE_STDOUT_LIMIT + 1), b""),
                "byte limit",
            ),
        )
        for label, probe, pattern in cases:
            with self.subTest(label=label):
                execute = mock.Mock()
                with self.assertRaisesRegex(runner.ReplayExecutionError, pattern):
                    self._run(
                        _isolation=self.isolation,
                        _probe_executor=probe,
                        _executor=execute,
                    )
                execute.assert_not_called()
                self.assertEqual(list(self.context.roots.output.iterdir()), [])
                self.assertEqual(list(self.context.roots.scratch.iterdir()), [])

    def test_final_probe_rejects_environment_change_after_last_stage(self) -> None:
        changed = copy.deepcopy(self.probe_document)
        changed["sqlite"]["version"] = "3.50.5"
        responses = iter(
            (
                self.probe_bytes,
                execution_env.canonical_json_bytes(changed),
            )
        )
        stages: list[str] = []

        def probe(command, cwd, child_environment, isolation):
            self._successful_probe(command, cwd, child_environment, isolation)
            return 0, next(responses), b""

        def execute(command, _cwd, _environment, _isolation) -> int:
            stage_id = self._stage_id(command)
            stages.append(stage_id)
            self._write_stage_outputs(stage_id)
            return 0

        with self.assertRaisesRegex(
            runner.ReplayExecutionError,
            r"changed during replay at \$\.sqlite\.version",
        ):
            self._run(
                _isolation=self.isolation,
                _probe_executor=probe,
                _executor=execute,
            )
        self.assertEqual(stages, [stage.id for stage in self.context.stages])
        self.assertEqual(len(self.probe_invocations), 2)
        self.assertEqual(list(self.context.roots.scratch.iterdir()), [])

    def test_final_probe_rechecks_host_runtime_after_last_stage(self) -> None:
        changed_runtime = copy.deepcopy(self.environment_document["runtime"])
        changed_runtime["host_fingerprint"] = "sha256:" + "f" * 64
        stages: list[str] = []

        def execute(command, _cwd, _environment, _isolation) -> int:
            stage_id = self._stage_id(command)
            stages.append(stage_id)
            self._write_stage_outputs(stage_id)
            return 0

        with mock.patch.object(
            runner,
            "_development_runtime_facts",
            side_effect=(
                copy.deepcopy(self.environment_document["runtime"]),
                changed_runtime,
            ),
        ):
            with self.assertRaisesRegex(
                runner.ReplayExecutionError,
                "live runtime identity changed during replay",
            ):
                self._run(
                    _isolation=self.isolation,
                    _executor=execute,
                )
        self.assertEqual(stages, [stage.id for stage in self.context.stages])
        self.assertEqual(len(self.probe_invocations), 1)

    def test_probe_process_capture_enforces_deadline_and_byte_limits(self) -> None:
        process_environment = {
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/nonexistent",
        }
        with self.assertRaisesRegex(runner.ReplayExecutionError, "byte limit"):
            runner._capture_process(
                (sys.executable, "-I", "-c", "import os;os.write(1,b'x'*64)"),
                self.base,
                process_environment,
                timeout_seconds=5,
                stdout_limit=8,
                stderr_limit=8,
            )
        with self.assertRaisesRegex(runner.ReplayExecutionError, "timed out"):
            runner._capture_process(
                (sys.executable, "-I", "-c", "import time;time.sleep(5)"),
                self.base,
                process_environment,
                timeout_seconds=0.05,
                stdout_limit=8,
                stderr_limit=8,
            )

    def test_stage_timeout_kills_its_process_group_without_waiting(self) -> None:
        process = mock.Mock(pid=4242)
        process.poll.return_value = None
        process.wait.side_effect = (
            subprocess.TimeoutExpired(("sandbox", "stage"), 0.25),
            0,
        )
        isolation = runner.IsolationBoundary("fixture", ("sandbox",))
        with mock.patch.object(
            runner.subprocess, "Popen", return_value=process
        ) as popen, mock.patch.object(runner.os, "killpg") as killpg:
            with self.assertRaisesRegex(
                runner.ReplayExecutionError,
                "timed out after 0.25 seconds",
            ):
                runner._execute(
                    ("python", "stage.py"),
                    self.base,
                    {"PATH": "/nonexistent"},
                    isolation,
                    timeout_seconds=0.25,
                )
        popen.assert_called_once_with(
            ["sandbox", "python", "stage.py"],
            cwd=self.base,
            env={"PATH": "/nonexistent"},
            start_new_session=True,
        )
        killpg.assert_called_once_with(4242, signal.SIGKILL)
        self.assertEqual(
            process.wait.call_args_list,
            [mock.call(timeout=0.25), mock.call(timeout=5)],
        )
        process.kill.assert_not_called()

    def test_production_executor_receives_configured_stage_timeout(self) -> None:
        observed: list[float] = []

        def execute(
            command,
            _cwd,
            _environment,
            _isolation,
            *,
            timeout_seconds,
        ) -> int:
            observed.append(timeout_seconds)
            self._write_stage_outputs(self._stage_id(command))
            return 0

        with mock.patch.object(runner, "_execute", side_effect=execute):
            self._run(
                _isolation=self.isolation,
                stage_timeout_seconds=12.5,
            )
        self.assertEqual(
            observed,
            [12.5] * len(self.context.stages),
        )

        for invalid in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    runner.ReplayExecutionError,
                    "finite positive number",
                ):
                    self._run(
                        _isolation=self.isolation,
                        stage_timeout_seconds=invalid,
                    )
    def test_environment_must_be_read_only_and_singly_linked(self) -> None:
        self.environment_path.chmod(0o644)
        self._assert_pre_execution_rejected("must have mode 0o444")

    def test_environment_hardlink_is_rejected_before_execution(self) -> None:
        source = self.base.parent / "environment-hardlink.json"
        source.write_bytes(self.environment_bytes)
        source.chmod(0o444)
        self.environment_path.unlink()
        os.link(source, self.environment_path)
        self._assert_pre_execution_rejected("must not be hard-linked")

    def test_environment_symlink_is_rejected_before_execution(self) -> None:
        source = self.base.parent / "environment-symlink-target.json"
        source.write_bytes(self.environment_bytes)
        source.chmod(0o444)
        self.environment_path.unlink()
        self.environment_path.symlink_to(source)
        self._assert_pre_execution_rejected(
            "execution environment.*(unavailable|stable regular file)"
        )

    def test_environment_directory_is_rejected_before_execution(self) -> None:
        self.environment_path.unlink()
        self.environment_path.mkdir()
        self.environment_path.chmod(0o444)
        self._assert_pre_execution_rejected(
            "execution environment.*(unavailable|stable regular file)"
        )

    def test_fails_fast_on_stage_error(self) -> None:
        calls: list[str] = []

        def execute(command, _cwd, _environment, _isolation) -> int:
            stage_id = self._stage_id(command)
            calls.append(stage_id)
            if stage_id == "cells":
                return 19
            self._write_stage_outputs(stage_id)
            return 0

        with self.assertRaisesRegex(runner.ReplayExecutionError, "cells.*19"):
            self._run(
                _executor=execute,
                _isolation=self.isolation,
            )
        self.assertEqual(calls, ["base-graph", "top-level-shards", "cells"])

    def test_later_stage_cannot_modify_a_predecessor_output(self) -> None:
        def execute(command, _cwd, _environment, _isolation) -> int:
            stage_id = self._stage_id(command)
            self._write_stage_outputs(stage_id)
            if stage_id == "top-level-shards":
                predecessor = self.context.output_for(
                    "base-graph", "brain/data/nodes.jsonl"
                )
                predecessor.write_bytes(b"mutated")
                predecessor.chmod(0o644)
            return 0

        with self.assertRaisesRegex(
            runner.ReplayExecutionError, "modified predecessor output"
        ):
            self._run(
                _executor=execute,
                _isolation=self.isolation,
            )

    def test_rejects_missing_wrong_or_undeclared_outputs(self) -> None:
        def missing(command, _cwd, _environment, _isolation) -> int:
            stage_id = self._stage_id(command)
            if stage_id == "base-graph":
                stage = self.context.stage(stage_id)
                for output in stage.outputs[1:]:
                    path = self.context.output_for(stage_id, output.path)
                    self._mkdir_output_parent(path)
                    path.write_bytes(b"partial")
                    path.chmod(0o644)
            return 0

        with self.assertRaisesRegex(runner.ReplayExecutionError, "did not create"):
            self._run(
                _executor=missing,
                _isolation=self.isolation,
            )

        self._make_writable(self.context.roots.output)
        for child in list(self.context.roots.output.iterdir()):
            if child.is_dir():
                import shutil

                shutil.rmtree(child)
            else:
                child.unlink()
        self.context.roots.output.chmod(0o700)

        def undeclared(command, _cwd, _environment, _isolation) -> int:
            stage_id = self._stage_id(command)
            self._write_stage_outputs(stage_id)
            rogue = self.context.roots.output / "rogue.txt"
            rogue.write_bytes(b"rogue")
            rogue.chmod(0o644)
            return 0

        with self.assertRaisesRegex(runner.ReplayExecutionError, "undeclared output"):
            self._run(
                _executor=undeclared,
                _isolation=self.isolation,
            )

    def test_rejects_preexisting_output_or_scratch_before_execution(self) -> None:
        for root, name in (
            (self.context.roots.output, "existing"),
            (self.context.roots.scratch, "scratch"),
        ):
            with self.subTest(root=root):
                path = root / name
                path.write_bytes(b"competitor")
                with mock.patch.object(runner, "_execute") as execute:
                    with self.assertRaisesRegex(
                        runner.ReplayExecutionError, "must be empty"
                    ):
                        self._run(
                            _isolation=self.isolation,
                        )
                execute.assert_not_called()
                path.unlink()

        extra = self.base / "ambient-secret"
        extra.write_bytes(b"not part of the prepared replay")
        self._assert_pre_execution_rejected("prepared closure")
        extra.unlink()

    def test_rejects_input_tampering_and_writable_reducer_code(self) -> None:
        member_path = self.context.members("concept-graph")[0]
        member_path.chmod(0o600)
        member_path.write_bytes(b"tampered")
        member_path.chmod(0o444)
        with self.assertRaisesRegex(runner.ReplayExecutionError, "bytes disagree"):
            self._run(
                _executor=lambda *_args: 0,
                _isolation=self.isolation,
            )

        original = next(
            member for binding in self.context.bindings
            if binding.input_id == "concept-graph" for member in binding.members
        )
        data = (original.object_name + "\n").encode("utf-8")
        member_path.chmod(0o600)
        member_path.write_bytes(data)
        member_path.chmod(0o444)
        program = self.context.code(self.context.stages[0].program)
        original_program = program.read_bytes()
        program.chmod(0o600)
        program.write_bytes(b"# tampered reducer\n")
        program.chmod(0o444)
        with self.assertRaisesRegex(runner.ReplayExecutionError, "verified pack"):
            self._run(
                _executor=lambda *_args: 0,
                _isolation=self.isolation,
            )

        program.chmod(0o600)
        program.write_bytes(original_program)
        program.chmod(0o644)
        with self.assertRaisesRegex(runner.ReplayExecutionError, "must have mode 0o444"):
            self._run(
                _executor=lambda *_args: 0,
                _isolation=self.isolation,
            )

    def test_platform_boundary_is_mandatory(self) -> None:
        interpreter = Path(sys.executable).resolve()
        with mock.patch.object(runner.sys, "platform", "unsupported"):
            with self.assertRaisesRegex(
                runner.ReplayExecutionError, "no supported kernel isolation"
            ):
                runner._select_isolation(self.context, interpreter)

        with mock.patch.object(runner, "_real_file"):
            boundary = runner._darwin_boundary(self.context, interpreter)
        profile = boundary.prefix[-1]
        self.assertEqual(boundary.prefix[:2], ("/usr/bin/sandbox-exec", "-p"))
        self.assertIn("(deny default)", profile)
        self.assertIn("(deny network*)", profile)
        self.assertIn("(deny process-fork)", profile)
        self.assertNotIn("(allow process*)", profile)
        self.assertIn("file-write", profile)
        self.assertIn(str(self.base), profile)
        self.assertIn(str(self.context.roots.output), profile)
        self.assertIn(str(self.context.roots.scratch), profile)
        self.assertEqual(boundary.executable, Path("/usr/bin/sandbox-exec"))
        self.assertEqual(
            boundary.policy,
            runner._sandbox_policy_document("darwin-sandbox-exec"),
        )

        with mock.patch.object(runner, "_real_file"):
            linux = runner._linux_boundary(self.context, interpreter)
        triples = tuple(zip(linux.prefix, linux.prefix[1:], linux.prefix[2:]))
        self.assertNotIn(("--ro-bind", "/", "/"), triples)
        self.assertIn(("--ro-bind", str(self.base), str(self.base)), triples)
        self.assertNotIn("/run", linux.prefix)
        self.assertNotIn("/var/run", linux.prefix)
        if Path("/lib").exists():
            self.assertIn(
                ("--ro-bind", str(Path("/lib").resolve()), "/lib"), triples
            )
        self.assertEqual(linux.executable, Path("/usr/bin/bwrap"))
        self.assertEqual(
            linux.policy,
            runner._sandbox_policy_document("linux-bubblewrap"),
        )

    def test_sandbox_facts_bind_executable_version_and_structural_policy(self) -> None:
        executable = self.base.parent / "fixture-bwrap"
        executable.write_bytes(b"fixture sandbox executable")
        boundary = runner.IsolationBoundary(
            "linux-bubblewrap",
            (str(executable), "--unshare-all"),
            executable,
            runner._sandbox_policy_document("linux-bubblewrap"),
        )
        with mock.patch.object(
            runner, "_sandbox_reported_version", return_value="0.11.0"
        ):
            facts = runner._sandbox_facts(boundary)
        self.assertEqual(
            facts["executable_sha256"],
            hashlib.sha256(executable.read_bytes()).hexdigest(),
        )
        self.assertEqual(facts["reported_version"], "0.11.0")
        self.assertEqual(
            facts["policy_root"],
            execution_env.sandbox_policy_root(boundary.policy),
        )

    def test_runner_file_closure_covers_entry_preparation_and_probe_chain(self) -> None:
        self.assertEqual(
            set(runner.RUNNER_FILES),
            {
                "brain/build_context.py",
                "brain/tools/authority_contracts.py",
                "brain/tools/execution_environment.py",
                "brain/tools/prepare_replay_v2.py",
                "brain/tools/probe_execution_environment.py",
                "brain/tools/run_offline.py",
                "brain/tools/run_replay_v2.py",
            },
        )
        self.assertEqual(
            runner._runner_files_root(),
            execution_env.runner_files_root(runner.RUNNER_FILES),
        )

    def test_output_state_contains_content_digests(self) -> None:
        stage_id = self.context.stages[0].id
        self._write_stage_outputs(stage_id)
        state = runner._output_state(self.context, (stage_id,))
        relative = self.context.stage(stage_id).outputs[0].path
        path = self.context.output_for(stage_id, relative)
        self.assertEqual(
            state[relative][-1], hashlib.sha256(path.read_bytes()).hexdigest()
        )

    def test_rejects_wrong_replay_identity_and_interpreter(self) -> None:
        with self.assertRaisesRegex(
            runner.ReplayExecutionError, "requested replay identity"
        ):
            self._run(
                expected_generation_id="sha256:" + "f" * 64,
                _executor=lambda *_args: 0,
                _isolation=self.isolation,
            )

        with self.assertRaisesRegex(
            runner.ReplayExecutionError, "interpreter running the runner"
        ):
            self._run(
                interpreter=Path("/bin/sh"),
                _executor=lambda *_args: 0,
                _isolation=self.isolation,
            )

    def test_rejects_pack_reducer_identity_mismatch_before_workspace_validation(
        self,
    ) -> None:
        cases = (
            ("expected_reducer_git_commit", "f" * 40),
            ("expected_configuration_sha256", "e" * 64),
            ("expected_environment_sha256", "d" * 64),
        )
        for argument, value in cases:
            with self.subTest(argument=argument), mock.patch.object(
                runner, "_validate_workspace"
            ) as validate, mock.patch.object(
                runner, "_select_isolation"
            ) as select, mock.patch.object(
                runner, "_execute"
            ) as execute:
                with self.assertRaisesRegex(
                    runner.ReplayExecutionError, "requested replay identity"
                ):
                    self._run(**{argument: value})
                validate.assert_not_called()
                select.assert_not_called()
                execute.assert_not_called()

    def test_hostile_startup_pythonpath_cannot_select_runtime_inputs(self) -> None:
        evil = self.base.parent / "evil" / "site-packages"
        evil.mkdir(parents=True)
        marker = self.base.parent / "startup-hook-ran"
        (evil / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(evil)
        for program in (TOOLS / "run_replay_v2.py", TOOLS / "run_offline.py"):
            result = subprocess.run(
                [sys.executable, "-I", str(program), "--help"],
                cwd=self.base.parent,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists())

        with mock.patch.object(runner.sys, "path", [str(evil), *runner.sys.path]):
            roots = runner._runtime_roots(Path(sys.executable).resolve())
        self.assertNotIn(evil.resolve(), roots)

        with self.assertRaisesRegex(
            runner.ReplayExecutionError, "invoke Python with -I"
        ):
            runner.require_isolated_startup()

    def test_offline_runner_v2_prepares_then_executes_without_extra_args(self) -> None:
        manifest = self.base / "pack-v2.json"
        manifest.write_bytes(
            build_context.canonical_json_bytes(
                {"schema": run_offline.PACK_SCHEMA_V2}
            )
        )
        manifest.chmod(0o444)
        workspace = self.base.parent / "fresh-workspace"
        prepared = SimpleNamespace(
            context_path=workspace / "build-context.json",
            generation_id="sha256:" + "1" * 64,
            offline_pack_id="sha256:" + "2" * 64,
            source_set_root="sha256:" + "3" * 64,
            reducer_inventory_id="sha256:" + "4" * 64,
            reducer_git_commit="a" * 40,
            configuration_sha256="b" * 64,
            environment_sha256="c" * 64,
            reducer_files=(("brain/replay.py", 7, "d" * 64),),
        )
        with mock.patch.object(
            run_offline.prepare_replay_v2, "prepare_replay_v2"
        ) as prepare_unisolated:
            with self.assertRaisesRegex(
                runner.ReplayExecutionError, "isolated launcher"
            ):
                run_offline.run(
                    manifest,
                    root=self.base,
                    workspace=workspace,
                    authority_git_commit="a" * 40,
                    authority_root="sha256:" + "b" * 64,
                    semantic_epoch="brain-v3",
                )
        prepare_unisolated.assert_not_called()

        with mock.patch.object(
            run_offline.run_replay_v2,
            "require_isolated_startup",
        ), mock.patch.object(
            run_offline.prepare_replay_v2,
            "prepare_replay_v2",
        ) as prepare_invalid_timeout:
            with self.assertRaisesRegex(
                runner.ReplayExecutionError,
                "stage timeout must be a finite positive number",
            ):
                run_offline.run(
                    manifest,
                    root=self.base,
                    workspace=workspace,
                    authority_git_commit="a" * 40,
                    authority_root="sha256:" + "b" * 64,
                    semantic_epoch="brain-v3",
                    stage_timeout_seconds=float("nan"),
                )
        prepare_invalid_timeout.assert_not_called()

        with mock.patch.object(
            run_offline.run_replay_v2,
            "require_isolated_startup",
        ), mock.patch.object(
            run_offline.prepare_replay_v2,
            "prepare_replay_v2",
            return_value=prepared,
        ) as prepare, mock.patch.object(
            run_offline.run_replay_v2,
            "run_replay_v2",
        ) as execute:
            self.assertEqual(
                run_offline.run(
                    manifest,
                    root=self.base,
                    workspace=workspace,
                    authority_git_commit="a" * 40,
                    authority_root="sha256:" + "b" * 64,
                    semantic_epoch="brain-v3",
                    prior_state_root="sha256:" + "c" * 64,
                    interpreter=Path(sys.executable),
                    stage_timeout_seconds=12.5,
                ),
                0,
            )
        prepare.assert_called_once_with(
            manifest.resolve(),
            workspace,
            pack_root=self.base.resolve(),
            authority_git_commit="a" * 40,
            authority_root="sha256:" + "b" * 64,
            semantic_epoch="brain-v3",
            prior_state_root="sha256:" + "c" * 64,
        )
        execute.assert_called_once_with(
            prepared.context_path,
            reducer_files=prepared.reducer_files,
            expected_generation_id=prepared.generation_id,
            expected_offline_pack_id=prepared.offline_pack_id,
            expected_source_set_root=prepared.source_set_root,
            expected_reducer_inventory_id=prepared.reducer_inventory_id,
            expected_reducer_git_commit=prepared.reducer_git_commit,
            expected_configuration_sha256=prepared.configuration_sha256,
            expected_environment_sha256=prepared.environment_sha256,
            interpreter=Path(sys.executable),
            stage_timeout_seconds=12.5,
        )

        with mock.patch.object(
            run_offline.run_replay_v2, "require_isolated_startup"
        ):
            with self.assertRaisesRegex(
                run_offline.VerificationError, "stage arguments are sealed"
            ):
                run_offline.run(
                    manifest,
                    root=self.base,
                    arguments=["--unexpected"],
                    workspace=workspace,
                    authority_git_commit="a" * 40,
                    authority_root="sha256:" + "b" * 64,
                    semantic_epoch="brain-v3",
                )


if __name__ == "__main__":
    unittest.main()
