#!/usr/bin/env python3
"""Run and attest the exact P1B CI gates without production credentials.

The recorder is intentionally narrower than the activation-bundle builder.  It
validates one clean promotion checkout, runs the repository's three required CI
commands, validates the checkout again, and emits one canonical JSON document
to stdout.  It never invokes Wrangler or any Brain deployment command.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


EVIDENCE_SCHEMA = "wikilean.brain-activation-ci/v2"
ENVIRONMENT_POLICY = "wikilean.brain-activation-ci-environment/v2"
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
GIT_VERSION_RE = re.compile(r"^git version [^\r\n]+$")
NODE_22_RE = re.compile(r"^v22\.[0-9]+\.[0-9]+$")
NPM_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
PYTHON_312_RE = re.compile(r"^Python 3\.12(?:\.[0-9]+)?(?:[^\r\n]*)$")
DEFAULT_COMMAND_TIMEOUT = 3600.0
GIT_TIMEOUT = 60.0
VERSION_TIMEOUT = 30.0

_PASSTHROUGH_ENVIRONMENT = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_CTYPE",
        "NODE_EXTRA_CA_CERTS",
        "NO_PROXY",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TEMP",
        "TMP",
        "TMPDIR",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


class ActivationCIError(RuntimeError):
    """The activation CI evidence could not be produced safely."""


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ActivationCIError(f"{label} must be a JSON object")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ActivationCIError(
            f"{label} fields are invalid (missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)})"
        )


def _validate_command_evidence(
    value: object,
    *,
    name: str,
    argv: list[str],
    cwd: Path,
    environment_overrides: Mapping[str, str],
) -> None:
    command = _object(value, f"CI command {name}")
    _exact_keys(
        command,
        {
            "name",
            "argv",
            "cwd",
            "environment_overrides",
            "returncode",
            "stdout",
            "stdout_bytes",
            "stdout_sha256",
            "stderr",
            "stderr_bytes",
            "stderr_sha256",
        },
        f"CI command {name}",
    )
    if (
        command.get("name") != name
        or command.get("argv") != argv
        or command.get("cwd") != str(cwd)
        or command.get("environment_overrides") != dict(environment_overrides)
        or command.get("returncode") != 0
    ):
        raise ActivationCIError(f"CI command {name} does not match the required invocation")
    for stream in ("stdout", "stderr"):
        text = command.get(stream)
        if not isinstance(text, str):
            raise ActivationCIError(f"CI command {name} {stream} must be a string")
        raw = text.encode("utf-8", errors="strict")
        if command.get(f"{stream}_bytes") != len(raw):
            raise ActivationCIError(f"CI command {name} {stream} byte count mismatch")
        if command.get(f"{stream}_sha256") != hashlib.sha256(raw).hexdigest():
            raise ActivationCIError(f"CI command {name} {stream} digest mismatch")


def _recorded_version(value: object, label: str) -> str:
    command = _object(value, label)
    stdout = command.get("stdout")
    stderr = command.get("stderr")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise ActivationCIError(f"{label} output must be strings")
    selected = stdout if stdout.strip() else stderr
    return selected.strip()


def validate_ci_evidence(
    document: Mapping[str, object],
    *,
    expected_repo_root: Path,
    expected_git_commit: str,
) -> None:
    """Validate one complete recorder result against the activation authority."""
    _exact_keys(
        document,
        {
            "schema",
            "ok",
            "authority",
            "repo_root",
            "environment",
            "tools",
            "checkout",
            "checks",
        },
        "activation CI evidence",
    )
    if document.get("schema") != EVIDENCE_SCHEMA or document.get("ok") is not True:
        raise ActivationCIError("activation CI evidence is not a successful recorder result")
    repository = expected_repo_root.resolve(strict=False)
    if document.get("repo_root") != str(repository):
        raise ActivationCIError("activation CI evidence names the wrong promotion checkout")
    authority = _object(document.get("authority"), "activation CI authority")
    _exact_keys(authority, {"git_commit", "branch"}, "activation CI authority")
    if authority.get("git_commit") != expected_git_commit or authority.get("branch") not in {
        "main",
        "detached",
    }:
        raise ActivationCIError("activation CI authority does not match the candidate")
    environment = _object(document.get("environment"), "activation CI environment")
    _exact_keys(
        environment,
        {
            "policy",
            "credentials_inherited",
            "git_overrides_inherited",
            "deployment_enabled",
            "caller_path_inherited",
            "tool_paths_pinned",
        },
        "activation CI environment",
    )
    if environment != {
        "policy": ENVIRONMENT_POLICY,
        "credentials_inherited": False,
        "git_overrides_inherited": False,
        "deployment_enabled": False,
        "caller_path_inherited": False,
        "tool_paths_pinned": True,
    }:
        raise ActivationCIError("activation CI environment policy is unsafe")
    checkout = _object(document.get("checkout"), "activation CI checkout")
    _exact_keys(
        checkout,
        {
            "clean_before",
            "clean_after",
            "head_before",
            "head_after",
            "main_before",
            "main_after",
        },
        "activation CI checkout",
    )
    if checkout != {
        "clean_before": True,
        "clean_after": True,
        "head_before": expected_git_commit,
        "head_after": expected_git_commit,
        "main_before": expected_git_commit,
        "main_after": expected_git_commit,
    }:
        raise ActivationCIError("activation CI checkout fence is inconsistent")

    tools = _object(document.get("tools"), "activation CI tools")
    _exact_keys(tools, {"git", "node", "npm", "python"}, "activation CI tools")
    git = _object(tools.get("git"), "activation CI Git tool")
    node = _object(tools.get("node"), "activation CI Node tool")
    npm = _object(tools.get("npm"), "activation CI npm tool")
    python = _object(tools.get("python"), "activation CI Python tool")
    _exact_keys(git, {"path", "version", "probe"}, "activation CI Git tool")
    _exact_keys(node, {"path", "version", "probe"}, "activation CI Node tool")
    _exact_keys(npm, {"path", "version", "probe"}, "activation CI npm tool")
    _exact_keys(python, {"path", "version", "probe"}, "activation CI Python tool")
    git_version = git.get("version")
    node_version = node.get("version")
    npm_version = npm.get("version")
    python_version = python.get("version")
    git_path = git.get("path")
    node_path = node.get("path")
    npm_path = npm.get("path")
    python_path = python.get("path")
    if not isinstance(git_version, str) or GIT_VERSION_RE.fullmatch(git_version) is None:
        raise ActivationCIError("activation CI evidence has an invalid Git version")
    if not isinstance(node_version, str) or NODE_22_RE.fullmatch(node_version) is None:
        raise ActivationCIError("activation CI evidence did not use Node 22")
    if not isinstance(npm_version, str) or NPM_VERSION_RE.fullmatch(npm_version) is None:
        raise ActivationCIError("activation CI evidence has an invalid npm version")
    if not isinstance(python_version, str) or PYTHON_312_RE.fullmatch(python_version) is None:
        raise ActivationCIError("activation CI evidence did not use Python 3.12")
    for value, label in (
        (git_path, "Git"),
        (node_path, "Node"),
        (npm_path, "npm"),
        (python_path, "Python"),
    ):
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ActivationCIError(f"activation CI {label} path must be absolute")
    assert isinstance(git_path, str)
    assert isinstance(node_path, str)
    assert isinstance(npm_path, str)
    assert isinstance(python_path, str)
    _validate_command_evidence(
        git.get("probe"),
        name="git_version",
        argv=[git_path, "--version"],
        cwd=repository,
        environment_overrides={},
    )
    _validate_command_evidence(
        node.get("probe"),
        name="node_version",
        argv=[node_path, "--version"],
        cwd=repository / "wiki",
        environment_overrides={},
    )
    _validate_command_evidence(
        npm.get("probe"),
        name="npm_version",
        argv=[npm_path, "--version"],
        cwd=repository / "wiki",
        environment_overrides={},
    )
    _validate_command_evidence(
        python.get("probe"),
        name="python_version",
        argv=[python_path, "--version"],
        cwd=repository,
        environment_overrides={},
    )
    if _recorded_version(git.get("probe"), "activation CI Git probe") != git_version:
        raise ActivationCIError("activation CI Git version differs from its probe output")
    if _recorded_version(node.get("probe"), "activation CI Node probe") != node_version:
        raise ActivationCIError("activation CI Node version differs from its probe output")
    if _recorded_version(npm.get("probe"), "activation CI npm probe") != npm_version:
        raise ActivationCIError("activation CI npm version differs from its probe output")
    if _recorded_version(python.get("probe"), "activation CI Python probe") != python_version:
        raise ActivationCIError("activation CI Python version differs from its probe output")

    checks = document.get("checks")
    if not isinstance(checks, list) or len(checks) != 3:
        raise ActivationCIError("activation CI evidence must contain exactly three gates")
    expected = (
        ("npm_ci", [npm_path, "ci"], repository / "wiki", {}),
        ("worker_ci", [npm_path, "run", "test:ci"], repository / "wiki", {}),
        (
            "python_ci",
            ["./scripts/ci-python.sh"],
            repository,
            {"PYTHON": python_path},
        ),
    )
    for value, (name, argv, cwd, overrides) in zip(checks, expected, strict=True):
        _validate_command_evidence(
            value,
            name=name,
            argv=argv,
            cwd=cwd,
            environment_overrides=overrides,
        )


@dataclass(frozen=True)
class RunResult:
    args: tuple[str, ...]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class CommandRunner:
    """Run one bounded command in a private process group."""

    @staticmethod
    def _signal_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return

    @classmethod
    def _terminate_process_group(
        cls, process: subprocess.Popen[bytes]
    ) -> tuple[bytes, bytes]:
        cls._signal_group(process, signal.SIGTERM)
        try:
            return process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            cls._signal_group(process, signal.SIGKILL)
            try:
                return process.communicate(timeout=5)
            except subprocess.TimeoutExpired as exc:  # pragma: no cover - kernel failure
                raise ActivationCIError("command process group did not terminate after SIGKILL") from exc

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        env: Mapping[str, str],
    ) -> RunResult:
        command = tuple(str(value) for value in args)
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return RunResult(command, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            stdout, stderr = self._terminate_process_group(process)
            return RunResult(command, None, stdout, stderr, timed_out=True)
        except BaseException:
            try:
                self._terminate_process_group(process)
            except BaseException:
                pass
            raise


@dataclass(frozen=True)
class CheckoutState:
    head: str
    main: str
    branch: str


def canonical_json_bytes(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ActivationCIError(f"cannot encode canonical CI evidence: {exc}") from exc
    return (rendered + "\n").encode("utf-8")


def sanitized_environment(
    source: Mapping[str, str],
    *,
    hermetic_home: Path,
    npm_cache: Path,
    tool_bin: Path,
    npm_global_config: Path,
    npm_user_config: Path,
) -> dict[str, str]:
    """Remove inherited application/Git credentials and mutable Git overrides."""

    # An allowlist is easier to audit than trying to enumerate every provider's
    # token spelling.  Proxy and CA variables are retained because npm is the
    # one deliberately networked command; no value is serialized as evidence.
    environment = {
        name: value
        for name, value in source.items()
        if name in _PASSTHROUGH_ENVIRONMENT
    }

    environment.update(
        {
            "CI": "1",
            "FORCE_COLOR": "0",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "HOME": str(hermetic_home),
            "LC_ALL": "C.UTF-8",
            "NO_COLOR": "1",
            "NPM_CONFIG_AUDIT": "false",
            "NPM_CONFIG_CACHE": str(npm_cache),
            "NPM_CONFIG_COLOR": "false",
            "NPM_CONFIG_FUND": "false",
            "NPM_CONFIG_GLOBALCONFIG": str(npm_global_config),
            "NPM_CONFIG_UPDATE_NOTIFIER": "false",
            "NPM_CONFIG_USERCONFIG": str(npm_user_config),
            "PATH": str(tool_bin) + os.pathsep + os.defpath,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "TZ": "UTC",
            "WIKILEAN_BRAIN_AGENTS": "0",
            "WIKILEAN_BRAIN_DEPLOY": "0",
        }
    )
    return environment


class ActivationCIRecorder:
    def __init__(
        self,
        *,
        repo_root: Path,
        git: Path,
        node: Path,
        npm: Path,
        python: Path,
        runner: CommandRunner | None = None,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
        source_environment: Mapping[str, str] | None = None,
    ) -> None:
        self.repo = repo_root.expanduser().resolve(strict=True)
        self.git = self._absolute_path(git, "Git")
        self.node = self._absolute_path(node, "Node")
        self.npm = self._absolute_path(npm, "npm")
        candidate_python = python.expanduser()
        if not candidate_python.is_absolute():
            candidate_python = self.repo / candidate_python
        self.python = Path(os.path.abspath(candidate_python))
        self.wiki = self.repo / "wiki"
        self.runner = runner or CommandRunner()
        self.command_timeout = command_timeout
        self.source_environment = dict(os.environ if source_environment is None else source_environment)
        self.environment: dict[str, str] = {}

    @staticmethod
    def _absolute_path(path: Path, label: str) -> Path:
        candidate = path.expanduser()
        if not candidate.is_absolute():
            raise ActivationCIError(f"selected {label} path must be absolute")
        return Path(os.path.abspath(candidate))

    @staticmethod
    def _validate_executable(path: Path, label: str) -> None:
        try:
            target = path.resolve(strict=True) if path.is_symlink() else path
        except OSError as exc:
            raise ActivationCIError(f"selected {label} executable is invalid: {exc}") from exc
        if not target.is_file():
            raise ActivationCIError(f"selected {label} executable is not a regular file")
        if not os.access(path, os.X_OK):
            raise ActivationCIError(f"selected {label} executable is not executable")

    def _validate_options(self) -> None:
        if not math.isfinite(self.command_timeout) or self.command_timeout <= 0:
            raise ActivationCIError("command timeout must be finite and positive")
        for path, label in (
            (self.wiki / "package.json", "wiki/package.json"),
            (self.wiki / "package-lock.json", "wiki/package-lock.json"),
            (self.repo / "scripts" / "ci-python.sh", "scripts/ci-python.sh"),
        ):
            if path.is_symlink() or not path.is_file():
                raise ActivationCIError(f"required checkout file is missing or symlinked: {label}")
        for executable, label in (
            (self.git, "Git"),
            (self.node, "Node"),
            (self.npm, "npm"),
            (self.python, "Python"),
        ):
            self._validate_executable(executable, label)
        if not os.access(self.repo / "scripts" / "ci-python.sh", os.X_OK):
            raise ActivationCIError("scripts/ci-python.sh is not executable")

    def _create_tool_shims(self, tool_bin: Path) -> None:
        tool_bin.mkdir(mode=0o700)
        for name, target in (
            ("git", self.git),
            ("node", self.node),
            ("npm", self.npm),
            ("python", self.python),
            ("python3", self.python),
        ):
            (tool_bin / name).symlink_to(target)

    def _run(self, args: Sequence[str], *, cwd: Path, timeout: float) -> RunResult:
        return self.runner.run(
            args,
            cwd=cwd,
            timeout=timeout,
            env=self.environment,
        )

    def _require(self, args: Sequence[str], *, cwd: Path, label: str, timeout: float) -> RunResult:
        result = self._run(args, cwd=cwd, timeout=timeout)
        expected = tuple(str(value) for value in args)
        if result.args != expected:
            raise ActivationCIError(
                f"{label} runner reported argv {result.args!r}, expected {expected!r}"
            )
        if not result.ok:
            state = "timed out" if result.timed_out else f"returned {result.returncode}"
            detail = (result.stderr or result.stdout).decode("utf-8", errors="replace")[-2000:]
            suffix = f": {detail.strip()}" if detail.strip() else ""
            raise ActivationCIError(f"{label} {state}{suffix}")
        return result

    def _git_text(self, *args: str, allow_failure: bool = False) -> str:
        result = self._run(
            [str(self.git), "-C", str(self.repo), *args],
            cwd=self.repo,
            timeout=GIT_TIMEOUT,
        )
        if not result.ok:
            if allow_failure:
                return ""
            raise ActivationCIError(
                f"git {' '.join(args)} failed: "
                + result.stderr.decode("utf-8", errors="replace").strip()
            )
        try:
            return result.stdout.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise ActivationCIError(f"git {' '.join(args)} emitted non-UTF-8 output") from exc

    def _checkout_state(self, phase: str) -> CheckoutState:
        top = Path(self._git_text("rev-parse", "--show-toplevel")).resolve(strict=True)
        if top != self.repo:
            raise ActivationCIError(f"{phase} checkout root mismatch: {top}")
        head = self._git_text("rev-parse", "HEAD")
        main = self._git_text("rev-parse", "refs/heads/main")
        if GIT_COMMIT_RE.fullmatch(head) is None or GIT_COMMIT_RE.fullmatch(main) is None:
            raise ActivationCIError(f"{phase} checkout returned a malformed Git authority")
        branch = self._git_text(
            "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True
        )
        dirty = self._git_text("status", "--porcelain=v1", "--untracked-files=all")
        if dirty:
            raise ActivationCIError(f"{phase} promotion checkout is dirty:\n{dirty}")
        if head != main:
            raise ActivationCIError(
                f"{phase} promotion checkout HEAD {head} does not equal refs/heads/main {main}"
            )
        if branch not in {"", "main"}:
            raise ActivationCIError(
                f"{phase} promotion checkout is on {branch!r}, not main or detached main"
            )
        git_dir_text = self._git_text("rev-parse", "--absolute-git-dir")
        git_dir = Path(git_dir_text)
        if not git_dir.is_absolute():
            raise ActivationCIError(f"{phase} checkout returned a non-absolute Git directory")
        if any(
            path.exists()
            for path in (git_dir / "MERGE_HEAD", git_dir / "rebase-merge", git_dir / "rebase-apply")
        ):
            raise ActivationCIError(f"{phase} promotion checkout has a merge or rebase in progress")
        return CheckoutState(head=head, main=main, branch=branch)

    @staticmethod
    def _decode_output(raw: bytes, label: str) -> str:
        try:
            return raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ActivationCIError(f"{label} emitted non-UTF-8 output") from exc

    def _command_evidence(
        self,
        name: str,
        result: RunResult,
        cwd: Path,
        *,
        environment_overrides: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        if not result.ok or result.returncode is None:
            raise ActivationCIError(f"cannot attest unsuccessful command {name}")
        stdout = self._decode_output(result.stdout, f"{name} stdout")
        stderr = self._decode_output(result.stderr, f"{name} stderr")
        return {
            "name": name,
            "argv": list(result.args),
            "cwd": str(cwd),
            "environment_overrides": dict(environment_overrides or {}),
            "returncode": result.returncode,
            "stdout": stdout,
            "stdout_bytes": len(result.stdout),
            "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
            "stderr": stderr,
            "stderr_bytes": len(result.stderr),
            "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
        }

    @staticmethod
    def _version_text(result: RunResult, label: str) -> str:
        raw = result.stdout if result.stdout.strip() else result.stderr
        try:
            return raw.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise ActivationCIError(f"{label} version output is not UTF-8") from exc

    def _validate_post_state(self, before: CheckoutState) -> CheckoutState:
        after = self._checkout_state("post-CI")
        if after != before:
            raise ActivationCIError(
                "promotion checkout authority or branch changed while CI was running"
            )
        return after

    def record(self) -> dict[str, object]:
        self._validate_options()
        with tempfile.TemporaryDirectory(prefix="wikilean-activation-ci.") as temporary:
            temporary_root = Path(temporary).resolve()
            hermetic_home = temporary_root / "home"
            npm_cache = temporary_root / "npm-cache"
            tool_bin = temporary_root / "tools"
            npm_global_config = temporary_root / "npm-globalrc"
            npm_user_config = temporary_root / "npm-userrc"
            hermetic_home.mkdir(mode=0o700)
            npm_cache.mkdir(mode=0o700)
            npm_global_config.touch(mode=0o600)
            npm_user_config.touch(mode=0o600)
            self._create_tool_shims(tool_bin)
            self.environment = sanitized_environment(
                self.source_environment,
                hermetic_home=hermetic_home,
                npm_cache=npm_cache,
                tool_bin=tool_bin,
                npm_global_config=npm_global_config,
                npm_user_config=npm_user_config,
            )

            before = self._checkout_state("pre-CI")
            completed: list[dict[str, object]] = []
            git_probe: RunResult | None = None
            node_probe: RunResult | None = None
            npm_probe: RunResult | None = None
            python_probe: RunResult | None = None
            try:
                git_probe = self._require(
                    [str(self.git), "--version"],
                    cwd=self.repo,
                    label="Git version check",
                    timeout=VERSION_TIMEOUT,
                )
                git_version = self._version_text(git_probe, "Git")
                if GIT_VERSION_RE.fullmatch(git_version) is None:
                    raise ActivationCIError(
                        f"activation CI got an invalid Git version: {git_version!r}"
                    )

                node_probe = self._require(
                    [str(self.node), "--version"],
                    cwd=self.wiki,
                    label="Node version check",
                    timeout=VERSION_TIMEOUT,
                )
                node_version = self._version_text(node_probe, "Node")
                if NODE_22_RE.fullmatch(node_version) is None:
                    raise ActivationCIError(f"activation CI requires Node 22, got {node_version!r}")

                npm_probe = self._require(
                    [str(self.npm), "--version"],
                    cwd=self.wiki,
                    label="npm version check",
                    timeout=VERSION_TIMEOUT,
                )
                npm_version = self._version_text(npm_probe, "npm")
                if NPM_VERSION_RE.fullmatch(npm_version) is None:
                    raise ActivationCIError(
                        f"activation CI got an invalid npm version: {npm_version!r}"
                    )

                python_probe = self._require(
                    [str(self.python), "--version"],
                    cwd=self.repo,
                    label="Python version check",
                    timeout=VERSION_TIMEOUT,
                )
                python_version = self._version_text(python_probe, "Python")
                if PYTHON_312_RE.fullmatch(python_version) is None:
                    raise ActivationCIError(
                        f"activation CI requires Python 3.12, got {python_version!r}"
                    )

                npm_ci = self._require(
                    [str(self.npm), "ci"],
                    cwd=self.wiki,
                    label="npm ci",
                    timeout=self.command_timeout,
                )
                completed.append(self._command_evidence("npm_ci", npm_ci, self.wiki))

                worker_ci = self._require(
                    [str(self.npm), "run", "test:ci"],
                    cwd=self.wiki,
                    label="npm run test:ci",
                    timeout=self.command_timeout,
                )
                completed.append(
                    self._command_evidence("worker_ci", worker_ci, self.wiki)
                )

                python_environment = dict(self.environment)
                python_environment["PYTHON"] = str(self.python)
                previous_environment = self.environment
                self.environment = python_environment
                try:
                    python_ci = self._require(
                        ["./scripts/ci-python.sh"],
                        cwd=self.repo,
                        label="PYTHON=<selected> ./scripts/ci-python.sh",
                        timeout=self.command_timeout,
                    )
                finally:
                    self.environment = previous_environment
                completed.append(
                    self._command_evidence(
                        "python_ci",
                        python_ci,
                        self.repo,
                        environment_overrides={"PYTHON": str(self.python)},
                    )
                )
            except ActivationCIError as exc:
                try:
                    self._validate_post_state(before)
                except ActivationCIError as post_exc:
                    raise ActivationCIError(f"{exc}; post-CI validation also failed: {post_exc}") from exc
                raise

            after = self._validate_post_state(before)
            assert (
                git_probe is not None
                and node_probe is not None
                and npm_probe is not None
                and python_probe is not None
            )
            return {
                "schema": EVIDENCE_SCHEMA,
                "ok": True,
                "authority": {
                    "git_commit": before.head,
                    "branch": before.branch or "detached",
                },
                "repo_root": str(self.repo),
                "environment": {
                    "policy": ENVIRONMENT_POLICY,
                    "credentials_inherited": False,
                    "git_overrides_inherited": False,
                    "deployment_enabled": False,
                    "caller_path_inherited": False,
                    "tool_paths_pinned": True,
                },
                "tools": {
                    "git": {
                        "path": str(self.git),
                        "version": self._version_text(git_probe, "Git"),
                        "probe": self._command_evidence(
                            "git_version", git_probe, self.repo
                        ),
                    },
                    "node": {
                        "path": str(self.node),
                        "version": self._version_text(node_probe, "Node"),
                        "probe": self._command_evidence(
                            "node_version", node_probe, self.wiki
                        ),
                    },
                    "npm": {
                        "path": str(self.npm),
                        "version": self._version_text(npm_probe, "npm"),
                        "probe": self._command_evidence(
                            "npm_version", npm_probe, self.wiki
                        ),
                    },
                    "python": {
                        "path": str(self.python),
                        "version": self._version_text(python_probe, "Python"),
                        "probe": self._command_evidence(
                            "python_version", python_probe, self.repo
                        ),
                    },
                },
                "checkout": {
                    "clean_before": True,
                    "clean_after": True,
                    "head_before": before.head,
                    "head_after": after.head,
                    "main_before": before.main,
                    "main_after": after.main,
                },
                "checks": completed,
            }


def record_activation_ci(
    *,
    repo_root: Path,
    git: Path,
    node: Path,
    npm: Path,
    python: Path,
    runner: CommandRunner | None = None,
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    source_environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    return ActivationCIRecorder(
        repo_root=repo_root,
        git=git,
        node=node,
        npm=npm,
        python=python,
        runner=runner,
        command_timeout=command_timeout,
        source_environment=source_environment,
    ).record()


def _parse_timeout(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--git", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--npm", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument(
        "--command-timeout",
        type=_parse_timeout,
        default=DEFAULT_COMMAND_TIMEOUT,
        help="per-command timeout in seconds (default: 3600)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        evidence = record_activation_ci(
            repo_root=args.repo_root,
            git=args.git,
            node=args.node,
            npm=args.npm,
            python=args.python,
            command_timeout=args.command_timeout,
        )
    except (ActivationCIError, OSError) as exc:
        sys.stderr.buffer.write(
            canonical_json_bytes(
                {"schema": EVIDENCE_SCHEMA, "ok": False, "error": str(exc)}
            )
        )
        return 1
    sys.stdout.buffer.write(canonical_json_bytes(evidence))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
