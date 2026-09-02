#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Mapping, Sequence

import brain_activation_ci as activation_ci


COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40


class FakeRunner:
    def __init__(
        self,
        repo: Path,
        git: Path,
        node: Path,
        npm: Path,
        python: Path,
        *,
        head: str = COMMIT,
        main: str = COMMIT,
        branch: str = "main",
        status_values: Sequence[str] = ("", ""),
        failure: tuple[str, ...] | None = None,
        git_version: bytes = b"git version 2.51.0\n",
        node_version: bytes = b"v22.23.2\n",
        npm_version: bytes = b"10.9.8\n",
        python_version: bytes = b"Python 3.12.14+meta\n",
    ) -> None:
        self.repo = repo
        self.git = git
        self.node = node
        self.npm = npm
        self.python = python
        self.head = head
        self.main = main
        self.branch = branch
        self.status_values = list(status_values)
        self.failure = failure
        self.git_version = git_version
        self.node_version = node_version
        self.npm_version = npm_version
        self.python_version = python_version
        self.calls: list[tuple[tuple[str, ...], Path, float, dict[str, str]]] = []
        self.path_resolutions: list[dict[str, Path]] = []
        self.status_calls = 0

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        env: Mapping[str, str],
    ) -> activation_ci.RunResult:
        command = tuple(str(value) for value in args)
        self.calls.append((command, cwd, timeout, dict(env)))
        self.path_resolutions.append(
            {
                name: Path(resolved).resolve()
                for name in ("git", "node", "npm")
                if (resolved := shutil.which(name, path=env.get("PATH"))) is not None
            }
        )
        if command == self.failure:
            return activation_ci.RunResult(command, 7, b"partial stdout\n", b"fixture failure\n")

        git_prefix = (str(self.git), "-C", str(self.repo))
        if command[:3] == git_prefix:
            tail = command[3:]
            if tail == ("rev-parse", "--show-toplevel"):
                stdout = f"{self.repo}\n".encode()
            elif tail == ("rev-parse", "HEAD"):
                stdout = f"{self.head}\n".encode()
            elif tail == ("rev-parse", "refs/heads/main"):
                stdout = f"{self.main}\n".encode()
            elif tail == ("symbolic-ref", "--quiet", "--short", "HEAD"):
                if not self.branch:
                    return activation_ci.RunResult(command, 1, b"", b"")
                stdout = f"{self.branch}\n".encode()
            elif tail == ("status", "--porcelain=v1", "--untracked-files=all"):
                index = min(self.status_calls, len(self.status_values) - 1)
                stdout = (self.status_values[index] + ("\n" if self.status_values[index] else "")).encode()
                self.status_calls += 1
            elif tail == ("rev-parse", "--absolute-git-dir"):
                stdout = f"{self.repo / '.git'}\n".encode()
            else:  # pragma: no cover - catches an accidental command expansion
                raise AssertionError(f"unexpected git command: {command}")
            return activation_ci.RunResult(command, 0, stdout, b"")

        if command == (str(self.git), "--version"):
            return activation_ci.RunResult(command, 0, self.git_version, b"")
        if command == (str(self.node), "--version"):
            return activation_ci.RunResult(command, 0, self.node_version, b"")
        if command == (str(self.npm), "--version"):
            return activation_ci.RunResult(command, 0, self.npm_version, b"")
        if command == (str(self.python), "--version"):
            return activation_ci.RunResult(command, 0, self.python_version, b"")
        if command == (str(self.npm), "ci"):
            return activation_ci.RunResult(command, 0, b"installed all packages\n", b"npm warning\n")
        if command == (str(self.npm), "run", "test:ci"):
            return activation_ci.RunResult(command, 0, "worker ✓\n".encode(), b"")
        if command == ("./scripts/ci-python.sh",):
            return activation_ci.RunResult(command, 0, b"python checks passed\n", b"")
        raise AssertionError(f"unexpected command: {command}")


class ActivationCIFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo"
        (self.repo / "wiki").mkdir(parents=True)
        (self.repo / "scripts").mkdir()
        (self.repo / ".git").mkdir()
        (self.repo / "wiki" / "package.json").write_text("{}\n", encoding="utf-8")
        (self.repo / "wiki" / "package-lock.json").write_text("{}\n", encoding="utf-8")
        script = self.repo / "scripts" / "ci-python.sh"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        self.git = self._executable("approved/git")
        self.node = self._executable("approved/node")
        self.npm = self._executable("approved/npm")
        self.python = self._executable("approved/python3.12")
        self.hostile_bin = self.root / "hostile-bin"
        for name in ("git", "node", "npm"):
            self._executable(f"hostile-bin/{name}")

    def _executable(self, relative: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def close(self) -> None:
        self.temporary.cleanup()

    def runner(self, **kwargs: object) -> FakeRunner:
        return FakeRunner(
            self.repo,
            self.git,
            self.node,
            self.npm,
            self.python,
            **kwargs,
        )

    def recorder(self, runner: FakeRunner) -> activation_ci.ActivationCIRecorder:
        return activation_ci.ActivationCIRecorder(
            repo_root=self.repo,
            git=self.git,
            node=self.node,
            npm=self.npm,
            python=self.python,
            runner=runner,
            command_timeout=123.0,
            source_environment={
                "PATH": str(self.hostile_bin),
                "ANTHROPIC_API_KEY": "do-not-inherit",
                "CLOUDFLARE_API_TOKEN": "do-not-inherit",
                "GIT_DIR": "/hostile/git-dir",
                "GIT_CONFIG_COUNT": "1",
                "GITHUB_TOKEN": "do-not-inherit",
                "NPM_TOKEN": "do-not-inherit",
                "UNRELATED_SAFE_VALUE": "retained",
            },
        )


class ActivationCITests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ActivationCIFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_success_records_canonical_complete_command_evidence(self) -> None:
        runner = self.fixture.runner()
        evidence = self.fixture.recorder(runner).record()
        activation_ci.validate_ci_evidence(
            evidence,
            expected_repo_root=self.fixture.repo,
            expected_git_commit=COMMIT,
        )

        self.assertEqual(evidence["schema"], activation_ci.EVIDENCE_SCHEMA)
        self.assertTrue(evidence["ok"])
        self.assertEqual(evidence["authority"]["git_commit"], COMMIT)
        self.assertEqual(evidence["repo_root"], str(self.fixture.repo))
        self.assertEqual(
            [item["argv"] for item in evidence["checks"]],
            [
                [str(self.fixture.npm), "ci"],
                [str(self.fixture.npm), "run", "test:ci"],
                ["./scripts/ci-python.sh"],
            ],
        )
        self.assertEqual(
            [item["cwd"] for item in evidence["checks"]],
            [
                str(self.fixture.repo / "wiki"),
                str(self.fixture.repo / "wiki"),
                str(self.fixture.repo),
            ],
        )
        self.assertEqual(evidence["checks"][0]["stdout"], "installed all packages\n")
        self.assertEqual(evidence["checks"][0]["stderr"], "npm warning\n")
        self.assertEqual(evidence["checks"][1]["stdout"], "worker ✓\n")
        self.assertEqual(
            evidence["checks"][2]["environment_overrides"],
            {"PYTHON": str(self.fixture.python)},
        )
        self.assertEqual(evidence["tools"]["git"]["path"], str(self.fixture.git))
        self.assertEqual(evidence["tools"]["node"]["path"], str(self.fixture.node))
        self.assertEqual(evidence["tools"]["npm"]["path"], str(self.fixture.npm))
        self.assertEqual(evidence["tools"]["git"]["version"], "git version 2.51.0")
        self.assertEqual(evidence["tools"]["node"]["version"], "v22.23.2")
        self.assertEqual(evidence["tools"]["npm"]["version"], "10.9.8")
        self.assertEqual(evidence["tools"]["python"]["version"], "Python 3.12.14+meta")

        canonical = activation_ci.canonical_json_bytes(evidence)
        self.assertEqual(canonical, activation_ci.canonical_json_bytes(json.loads(canonical)))
        self.assertTrue(canonical.endswith(b"\n"))

        probes_and_gates = [
            call
            for call in runner.calls
            if "-C" not in call[0][:3]
        ]
        self.assertEqual(
            [call[0] for call in probes_and_gates],
            [
                (str(self.fixture.git), "--version"),
                (str(self.fixture.node), "--version"),
                (str(self.fixture.npm), "--version"),
                (str(self.fixture.python), "--version"),
                (str(self.fixture.npm), "ci"),
                (str(self.fixture.npm), "run", "test:ci"),
                ("./scripts/ci-python.sh",),
            ],
        )
        for command, _, timeout, environment in runner.calls:
            self.assertGreater(timeout, 0)
            for forbidden in (
                "ANTHROPIC_API_KEY",
                "CLOUDFLARE_API_TOKEN",
                "GIT_DIR",
                "GIT_CONFIG_COUNT",
                "GITHUB_TOKEN",
                "NPM_TOKEN",
                "UNRELATED_SAFE_VALUE",
            ):
                self.assertNotIn(forbidden, environment)
            self.assertEqual(environment["GIT_NO_REPLACE_OBJECTS"], "1")
            self.assertEqual(environment["GIT_OPTIONAL_LOCKS"], "0")
            self.assertEqual(environment["WIKILEAN_BRAIN_DEPLOY"], "0")
            self.assertNotIn(str(self.fixture.hostile_bin), environment["PATH"].split(os.pathsep))
            if command == ("./scripts/ci-python.sh",):
                self.assertEqual(environment["PYTHON"], str(self.fixture.python))
            else:
                self.assertNotIn("PYTHON", environment)

    def test_preexisting_dirty_checkout_fails_before_tool_commands(self) -> None:
        runner = self.fixture.runner(
            status_values=(" M docs/ROADMAP.md",),
        )
        with self.assertRaisesRegex(activation_ci.ActivationCIError, "pre-CI promotion checkout is dirty"):
            self.fixture.recorder(runner).record()
        self.assertFalse(
            any(
                call[0][0] in {str(self.fixture.node), str(self.fixture.npm)}
                for call in runner.calls
            )
        )

    def test_wrong_main_authority_fails_before_tool_commands(self) -> None:
        runner = self.fixture.runner(
            head=COMMIT,
            main=OTHER_COMMIT,
        )
        with self.assertRaisesRegex(activation_ci.ActivationCIError, "does not equal refs/heads/main"):
            self.fixture.recorder(runner).record()
        self.assertFalse(
            any(
                call[0][0] in {str(self.fixture.node), str(self.fixture.npm)}
                for call in runner.calls
            )
        )

    def test_failed_gate_is_not_attested_and_post_state_is_checked(self) -> None:
        runner = self.fixture.runner(
            failure=(str(self.fixture.npm), "run", "test:ci"),
        )
        with self.assertRaisesRegex(activation_ci.ActivationCIError, "npm run test:ci returned 7"):
            self.fixture.recorder(runner).record()
        self.assertEqual(runner.status_calls, 2)
        self.assertFalse(any(call[0] == ("./scripts/ci-python.sh",) for call in runner.calls))

    def test_post_ci_dirtiness_fails_closed(self) -> None:
        runner = self.fixture.runner(
            status_values=("", "?? generated.txt"),
        )
        with self.assertRaisesRegex(activation_ci.ActivationCIError, "post-CI promotion checkout is dirty"):
            self.fixture.recorder(runner).record()

    def test_detached_main_is_accepted(self) -> None:
        runner = self.fixture.runner(branch="")
        evidence = self.fixture.recorder(runner).record()
        self.assertEqual(evidence["authority"]["branch"], "detached")

    def test_toolchain_versions_are_strict(self) -> None:
        node_runner = self.fixture.runner(
            node_version=b"v23.1.0\n",
        )
        with self.assertRaisesRegex(activation_ci.ActivationCIError, "requires Node 22"):
            self.fixture.recorder(node_runner).record()

        python_runner = self.fixture.runner(
            python_version=b"Python 3.13.0\n",
        )
        with self.assertRaisesRegex(activation_ci.ActivationCIError, "requires Python 3.12"):
            self.fixture.recorder(python_runner).record()

    def test_invalid_timeout_is_rejected(self) -> None:
        runner = self.fixture.runner()
        recorder = activation_ci.ActivationCIRecorder(
            repo_root=self.fixture.repo,
            git=self.fixture.git,
            node=self.fixture.node,
            npm=self.fixture.npm,
            python=self.fixture.python,
            runner=runner,
            command_timeout=float("nan"),
        )
        with self.assertRaisesRegex(activation_ci.ActivationCIError, "finite and positive"):
            recorder.record()

    def test_validator_rejects_tampered_command_output(self) -> None:
        runner = self.fixture.runner()
        evidence = self.fixture.recorder(runner).record()
        evidence["checks"][1]["stdout"] = "forged green output\n"
        with self.assertRaisesRegex(activation_ci.ActivationCIError, "mismatch"):
            activation_ci.validate_ci_evidence(
                evidence,
                expected_repo_root=self.fixture.repo,
                expected_git_commit=COMMIT,
            )

    def test_caller_path_tool_shadows_are_ignored(self) -> None:
        runner = self.fixture.runner()
        evidence = self.fixture.recorder(runner).record()
        self.assertFalse(evidence["environment"]["caller_path_inherited"])
        expected = {
            "git": self.fixture.git.resolve(),
            "node": self.fixture.node.resolve(),
            "npm": self.fixture.npm.resolve(),
        }
        self.assertTrue(runner.path_resolutions)
        for resolutions in runner.path_resolutions:
            self.assertEqual(resolutions, expected)
        for command, _, _, environment in runner.calls:
            self.assertNotIn(str(self.fixture.hostile_bin), environment["PATH"])
            if command[0] in {str(self.fixture.git), str(self.fixture.node), str(self.fixture.npm)}:
                self.assertTrue(Path(command[0]).is_absolute())

    def test_relative_approved_tool_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(activation_ci.ActivationCIError, "Git path must be absolute"):
            activation_ci.ActivationCIRecorder(
                repo_root=self.fixture.repo,
                git=Path("git"),
                node=self.fixture.node,
                npm=self.fixture.npm,
                python=self.fixture.python,
            )

    def test_validator_rejects_unpinned_tool_path(self) -> None:
        runner = self.fixture.runner()
        evidence = self.fixture.recorder(runner).record()
        evidence["tools"]["npm"]["path"] = "npm"
        with self.assertRaisesRegex(activation_ci.ActivationCIError, "npm path must be absolute"):
            activation_ci.validate_ci_evidence(
                evidence,
                expected_repo_root=self.fixture.repo,
                expected_git_commit=COMMIT,
            )

    def test_validator_rejects_wrong_authority(self) -> None:
        runner = self.fixture.runner()
        evidence = self.fixture.recorder(runner).record()
        with self.assertRaisesRegex(activation_ci.ActivationCIError, "candidate"):
            activation_ci.validate_ci_evidence(
                evidence,
                expected_repo_root=self.fixture.repo,
                expected_git_commit=OTHER_COMMIT,
            )

    def test_validator_binds_advertised_versions_to_probe_output(self) -> None:
        runner = self.fixture.runner()
        evidence = self.fixture.recorder(runner).record()
        evidence["tools"]["node"]["version"] = "v22.99.99"
        with self.assertRaisesRegex(activation_ci.ActivationCIError, "probe output"):
            activation_ci.validate_ci_evidence(
                evidence,
                expected_repo_root=self.fixture.repo,
                expected_git_commit=COMMIT,
            )


if __name__ == "__main__":
    unittest.main()
