#!/usr/bin/env python3
"""Hermetic tests for the sealed full-DAG replay executor."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
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
    operating_system = "darwin" if sys.platform == "darwin" else "linux"
    value: dict[str, object] = {
        "schema": execution_env.EXECUTION_ENVIRONMENT_SCHEMA,
        "environment_id": "sha256:" + "0" * 64,
        "profile": execution_env.DEVELOPMENT_HOST_PROFILE,
        "runtime": {
            "kind": "development-host",
            "os": operating_system,
            "architecture": "arm64" if operating_system == "darwin" else "x86_64",
            "runtime_root": "sha256:" + "1" * 64,
        },
        "runner": {
            "name": "wikilean-replay",
            "version": "2.0.0",
            "git_commit": reducer_git_commit,
            "files_root": "sha256:" + "2" * 64,
        },
        "python": {
            "implementation": "CPython",
            "version": "3.12.11",
            "cache_tag": "cpython-312",
            "soabi": "cpython-312-fixture",
            "executable_sha256": "3" * 64,
        },
        "dependency_lock": {
            "schema": execution_env.DEPENDENCY_LOCK_SCHEMA,
            "packages": [
                {
                    "name": "numpy",
                    "version": "2.3.2",
                    "artifact_sha256": "4" * 64,
                    "installed_files_root": "sha256:" + "5" * 64,
                }
            ],
        },
        "sqlite": {
            "version": "3.50.4",
            "source_id": "2030-01-02 03:04:05 " + "6" * 64,
            "binary_sha256": "7" * 64,
            "compile_options": ["ENABLE_FTS5", "THREADSAFE=1"],
        },
        "locale": {
            "lang": "C.UTF-8",
            "lc_all": "C.UTF-8",
            "timezone": "UTC",
            "preferred_encoding": "utf-8",
            "filesystem_encoding": "utf-8",
            "utf8_mode": 0,
            "python_hash_seed": "0",
        },
        "sandbox": {
            "backend": (
                "darwin-sandbox-exec"
                if operating_system == "darwin"
                else "linux-bubblewrap"
            ),
            "version": "1.0.0",
            "executable_sha256": "8" * 64,
            "policy_id": "brain-replay-v1",
            "policy_sha256": "9" * 64,
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
        self.isolation = runner.IsolationBoundary("fixture-kernel", ("fixture",))

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
        self.assertEqual(calls[-1][2], "fixture-kernel")
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
            self.assertEqual(environment["WIKILEAN_OFFLINE"], "1")
            self.assertNotIn("ANTHROPIC_API_KEY", environment)
            self.assertNotIn("BRAIN_EXTERNAL_DIR", environment)
            self.assertEqual(
                environment["PYTHONPATH"],
                runner._runtime_pythonpath(Path(sys.executable).resolve()),
            )
            self.assertNotIn("/host/injection", environment["PYTHONPATH"])
        self.assertIn("brain-page", result.stages)

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
