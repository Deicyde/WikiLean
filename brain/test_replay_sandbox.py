#!/usr/bin/env python3
"""Kernel-level smoke test for the sealed Brain replay sandbox.

The test auto-skips when the current host cannot create the platform sandbox.
Set ``WIKILEAN_REQUIRE_REPLAY_SANDBOX`` to ``darwin`` or ``linux`` to turn an
unsupported platform, missing tool, or unusable kernel facility into a failure.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any


HERE = Path(__file__).resolve().parent
TOOLS = HERE / "tools"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(TOOLS))

import run_replay_v2 as runner  # noqa: E402


REQUIRE_ENV = "WIKILEAN_REQUIRE_REPLAY_SANDBOX"
SUPPORTED_REQUIREMENTS = {"darwin", "linux"}

_PROBE_SOURCE = r'''#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path


workspace = Path(sys.argv[1])
ambient = Path(sys.argv[2])
ambient_sink = Path(sys.argv[3])
temporary_marker = Path(sys.argv[4])
port = int(sys.argv[5])
platform_name = sys.argv[6]
report_path = workspace / "output" / "sandbox-report.json"
results = {}


def attempt(name, operation):
    try:
        value = operation()
    except BaseException as exc:
        results[name] = {
            "ok": False,
            "type": type(exc).__name__,
            "errno": getattr(exc, "errno", None),
        }
    else:
        if not isinstance(value, (str, int, float, bool, type(None))):
            value = repr(value)
        results[name] = {"ok": True, "value": value}


def connect_to_host():
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    connection.settimeout(1.0)
    try:
        connection.connect(("127.0.0.1", port))
    finally:
        connection.close()
    return "connected"


def fork_once():
    child = os.fork()
    if child == 0:
        os._exit(0)
    _pid, status = os.waitpid(child, 0)
    return status


def effective_capabilities():
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("CapEff:"):
            return line.split(":", 1)[1].strip()
    raise RuntimeError("CapEff is absent from /proc/self/status")


attempt("read_input", lambda: (workspace / "input" / "sealed.txt").read_text(encoding="utf-8"))
attempt("read_code", lambda: (workspace / "code" / "probe.py").read_text(encoding="utf-8"))
attempt("sqlite_runtime", lambda: __import__("sqlite3").sqlite_version)
attempt("write_output", lambda: (workspace / "output" / "allowed.txt").write_text("output", encoding="utf-8"))
attempt("write_scratch", lambda: (workspace / "scratch" / "allowed.txt").write_text("scratch", encoding="utf-8"))

# These fixtures are deliberately writable by the host user. A denial therefore
# demonstrates the kernel boundary rather than ordinary Unix mode bits.
attempt("create_input", lambda: (workspace / "input" / "created.txt").write_text("bad", encoding="utf-8"))
attempt("overwrite_input", lambda: (workspace / "input" / "sealed.txt").write_text("bad", encoding="utf-8"))
attempt("create_code", lambda: (workspace / "code" / "created.py").write_text("bad", encoding="utf-8"))
attempt("overwrite_context", lambda: (workspace / "build-context.json").write_text("bad", encoding="utf-8"))
attempt("write_workspace_root", lambda: (workspace / "rogue.txt").write_text("bad", encoding="utf-8"))
attempt("read_ambient", lambda: ambient.read_text(encoding="utf-8"))
attempt("write_ambient", lambda: ambient_sink.write_text("bad", encoding="utf-8"))
attempt("symlink_escape", lambda: (workspace / "output" / "escape" / "escaped.txt").write_text("bad", encoding="utf-8"))
attempt("write_temporary", lambda: temporary_marker.write_text("temporary", encoding="utf-8"))
attempt("network_connect", connect_to_host)

if platform_name == "darwin":
    attempt("fork", fork_once)
elif platform_name == "linux":
    attempt("effective_capabilities", effective_capabilities)

report_path.write_text(json.dumps(results, sort_keys=True), encoding="utf-8")
'''


def _platform_name() -> str | None:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return None


def _preflight_command(platform_name: str) -> tuple[str, ...]:
    if platform_name == "darwin":
        return (
            "/usr/bin/sandbox-exec",
            "-p",
            "(version 1) (allow default)",
            "/usr/bin/true",
        )
    return (
        "/usr/bin/bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--cap-drop",
        "ALL",
        "--ro-bind",
        "/",
        "/",
        "--",
        "/usr/bin/true",
    )


def _capability_failure(platform_name: str) -> str | None:
    executable = Path(_preflight_command(platform_name)[0])
    try:
        metadata = executable.lstat()
    except OSError as exc:
        return f"{executable} is unavailable: {exc}"
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return f"{executable} is not a real regular file"
    try:
        result = subprocess.run(
            _preflight_command(platform_name),
            env={
                "HOME": "/nonexistent",
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin",
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"sandbox capability probe could not run: {exc}"
    if result.returncode == 0:
        return None
    detail = (result.stderr or result.stdout).strip().replace("\n", " ")
    if len(detail) > 500:
        detail = detail[:497] + "..."
    return (
        f"sandbox capability probe exited {result.returncode}"
        + (f": {detail}" if detail else "")
    )


class ReplaySandboxKernelTest(unittest.TestCase):
    def _skip_or_fail(self, reason: str, required: str | None) -> None:
        if required is not None:
            self.fail(reason)
        self.skipTest(reason)

    @staticmethod
    def _assert_allowed(results: dict[str, Any], name: str) -> None:
        result = results.get(name)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise AssertionError(f"sandbox unexpectedly denied {name}: {result!r}")

    @staticmethod
    def _assert_denied(results: dict[str, Any], name: str) -> None:
        result = results.get(name)
        if not isinstance(result, dict) or result.get("ok") is not False:
            raise AssertionError(f"sandbox unexpectedly allowed {name}: {result!r}")

    def test_real_replay_sandbox_boundary(self) -> None:
        # Compile before any capability skip so the embedded reducer probe is
        # still syntax-checked on hosts that cannot enter a nested sandbox.
        compile(_PROBE_SOURCE, "<replay-sandbox-probe>", "exec")

        required = os.environ.get(REQUIRE_ENV) or None
        if required not in SUPPORTED_REQUIREMENTS | {None}:
            self.fail(
                f"{REQUIRE_ENV} must be one of darwin or linux, got {required!r}"
            )
        platform_name = _platform_name()
        if platform_name is None:
            self._skip_or_fail(
                f"no replay sandbox test is defined for {sys.platform!r}", required
            )
        assert platform_name is not None
        if required is not None and required != platform_name:
            self.fail(
                f"{REQUIRE_ENV} requires {required}, but this host is {platform_name}"
            )
        unavailable = _capability_failure(platform_name)
        if unavailable is not None:
            self._skip_or_fail(unavailable, required)

        with tempfile.TemporaryDirectory(prefix="wikilean-replay-sandbox-") as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            code = workspace / "code"
            input_root = workspace / "input"
            output = workspace / "output"
            scratch = workspace / "scratch"
            outside = root / "outside"
            for path in (code, input_root, output, scratch, outside):
                path.mkdir(parents=True)
                path.chmod(0o700)
            workspace.chmod(0o700)

            probe = code / "probe.py"
            sealed_input = input_root / "sealed.txt"
            context_path = workspace / "build-context.json"
            ambient = root / "ambient.txt"
            ambient_sink = root / "ambient-write.txt"
            escaped = outside / "escaped.txt"
            probe.write_text(_PROBE_SOURCE, encoding="utf-8")
            sealed_input.write_text("sealed", encoding="utf-8")
            context_path.write_text("{}", encoding="utf-8")
            ambient.write_text("host secret", encoding="utf-8")
            for path in (probe, sealed_input, context_path, ambient):
                path.chmod(0o600)
            (output / "escape").symlink_to(outside, target_is_directory=True)

            # Prove the test fixtures are writable/readable without the sandbox.
            self.assertEqual(ambient.read_text(encoding="utf-8"), "host secret")
            for parent in (workspace, code, input_root):
                host_check = parent / "host-write-check.txt"
                host_check.write_text("ok", encoding="utf-8")
                host_check.unlink()
            ambient_sink.write_text("ok", encoding="utf-8")
            ambient_sink.unlink()

            temporary_marker = Path("/tmp") / (
                "wikilean-replay-sandbox-" + uuid.uuid4().hex
            )
            self.addCleanup(temporary_marker.unlink, missing_ok=True)
            try:
                temporary_marker.write_text("host check", encoding="utf-8")
                temporary_marker.unlink()
            except OSError as exc:
                self._skip_or_fail(
                    f"host /tmp is unavailable for the isolation probe: {exc}",
                    required,
                )

            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(2)
            listener.settimeout(1.0)
            port = listener.getsockname()[1]
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                accepted, _address = listener.accept()
                accepted.close()

            context = SimpleNamespace(
                roots=SimpleNamespace(
                    code=code,
                    input=input_root,
                    output=output,
                    scratch=scratch,
                )
            )
            interpreter = Path(sys.executable).resolve(strict=True)
            boundary = runner._select_isolation(context, interpreter)
            self.assertEqual(
                boundary.name,
                "darwin-sandbox-exec"
                if platform_name == "darwin"
                else "linux-bubblewrap",
            )
            command = (
                str(interpreter),
                "-P",
                "-S",
                "-s",
                "-B",
                "-c",
                runner._RUN_STAGE,
                str(probe),
                str(workspace),
                str(ambient),
                str(ambient_sink),
                str(temporary_marker),
                str(port),
                platform_name,
            )
            returncode = runner._execute(
                command,
                code,
                runner._environment(interpreter),
                boundary,
            )
            self.assertEqual(returncode, 0, "production sandbox probe failed to run")

            report_path = output / "sandbox-report.json"
            self.assertTrue(report_path.is_file(), "sandbox probe did not publish its report")
            results = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertIsInstance(results, dict)

            for name in (
                "read_input",
                "read_code",
                "sqlite_runtime",
                "write_output",
                "write_scratch",
            ):
                self._assert_allowed(results, name)
            for name in (
                "create_input",
                "overwrite_input",
                "create_code",
                "overwrite_context",
                "write_workspace_root",
                "read_ambient",
                "write_ambient",
                "symlink_escape",
                "network_connect",
            ):
                self._assert_denied(results, name)

            self.assertEqual(sealed_input.read_text(encoding="utf-8"), "sealed")
            self.assertEqual(context_path.read_text(encoding="utf-8"), "{}")
            self.assertEqual(ambient.read_text(encoding="utf-8"), "host secret")
            self.assertFalse(ambient_sink.exists())
            self.assertFalse(escaped.exists())

            if platform_name == "darwin":
                self._assert_denied(results, "write_temporary")
                self._assert_denied(results, "fork")
            else:
                self._assert_allowed(results, "write_temporary")
                self._assert_allowed(results, "effective_capabilities")
                capabilities = results["effective_capabilities"].get("value")
                self.assertIsInstance(capabilities, str)
                self.assertEqual(int(capabilities, 16), 0)
            self.assertFalse(
                temporary_marker.exists(),
                "sandboxed /tmp write escaped into the host namespace",
            )

            listener.setblocking(False)
            with self.assertRaises(BlockingIOError):
                listener.accept()


if __name__ == "__main__":
    unittest.main()
