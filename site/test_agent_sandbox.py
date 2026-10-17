#!/usr/bin/env python3
"""The pipeline's Claude agents run sandboxed, never with bypassed permissions.

Under permission_mode "bypassPermissions" the SDK's allowed_tools only lists
tools to auto-approve, so an allowlist next to it restricts nothing: agents fed
Wikipedia text ran Bash beside wiki/.dev.vars. These checks fail when any
tracked Python file reintroduces the bypass (with or without an allowlist),
builds ClaudeAgentOptions outside site/agent_sandbox.py, or when the sandbox
helper stops restricting tools, permissions, settings or the working directory.
"""
from __future__ import annotations

import ast
import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("WIKILEAN_SEARCH_TOOLS", "0")

import agent_sandbox  # noqa: E402

# Spelled in pieces so this file does not match its own scan.
BYPASS_MODE = "bypass" + "Permissions"
SKIP_FLAGS = {"--dangerously-" + "skip-permissions", "--allow-dangerously-" + "skip-permissions"}
ALLOWLIST_KEYS = {"allowed_tools", "allowedTools", "--allowedTools", "--allowed-tools", "tools", "--tools"}
SCAN_ROOTS = ("site", "brain", "manage", "bot", "catalog", "bench", "wikifunctions", "scripts", "wiki")
SANDBOX_MODULE = "site/agent_sandbox.py"


def tracked_python() -> list[Path]:
    try:
        out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--", "*.py"],
                             check=True, capture_output=True, text=True).stdout
        paths = [REPO / p for p in out.split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        paths = [p for root in SCAN_ROOTS for p in (REPO / root).rglob("*.py")
                 if ".venv" not in p.parts and "node_modules" not in p.parts]
    return sorted(p for p in paths if p.is_file())


def _const(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def scan(path: Path, rel: str | None = None) -> list[str]:
    """Return the sandbox violations in one Python file (``rel`` names it)."""
    rel = rel or path.relative_to(REPO).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    found: list[str] = []
    for node in ast.walk(tree):
        value = _const(node)
        if value == BYPASS_MODE:
            found.append(f"{rel}:{node.lineno}: permission mode {BYPASS_MODE!r}")
        elif value in SKIP_FLAGS:
            found.append(f"{rel}:{node.lineno}: CLI flag {value}")
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name == "ClaudeAgentOptions" and rel != SANDBOX_MODULE:
                found.append(f"{rel}:{node.lineno}: ClaudeAgentOptions built outside {SANDBOX_MODULE}; "
                             "use agent_sandbox.sandboxed_options")
            keys = {kw.arg for kw in node.keywords if kw.arg}
            mode = next((kw.value for kw in node.keywords if kw.arg == "permission_mode"), None)
            if mode is not None and _const(mode) == BYPASS_MODE and keys & ALLOWLIST_KEYS:
                found.append(f"{rel}:{node.lineno}: {BYPASS_MODE} combined with a tool allowlist")
        if isinstance(node, ast.Dict):
            entries = {_const(k): v for k, v in zip(node.keys, node.values) if k is not None and _const(k)}
            mode = entries.get("permission_mode") or entries.get("permissionMode")
            if mode is not None and _const(mode) == BYPASS_MODE and set(entries) & ALLOWLIST_KEYS:
                found.append(f"{rel}:{node.lineno}: {BYPASS_MODE} combined with a tool allowlist")
    return found


class RepositoryScan(unittest.TestCase):
    def test_no_tracked_python_bypasses_agent_permissions(self):
        files = tracked_python()
        self.assertGreater(len(files), 50, "the scan found too few Python files to be meaningful")
        scanned = {p.relative_to(REPO).parts[0] for p in files}
        for root in ("site", "brain", "manage", "bot"):
            self.assertIn(root, scanned)
        violations = [v for path in files for v in scan(path)]
        self.assertEqual(violations, [])

    def test_scan_catches_the_original_defect(self):
        with tempfile.TemporaryDirectory() as raw:
            bad = Path(raw) / "bad_agent.py"
            bad.write_text(
                "from claude_agent_sdk import ClaudeAgentOptions\n"
                "opts = dict(model='m', allowed_tools=[], permission_mode=" + repr(BYPASS_MODE) + ")\n"
                "o = ClaudeAgentOptions(allowed_tools=['Read'], permission_mode=" + repr(BYPASS_MODE) + ")\n"
                "c = {'permission_mode': " + repr(BYPASS_MODE) + ", 'allowed_tools': []}\n"
                "argv = ['claude', '-p', 'x', '--allowedTools', 'Read', " + repr(sorted(SKIP_FLAGS)[1]) + "]\n",
                encoding="utf-8")
            found = scan(bad, "site/bad_agent.py")
        joined = "\n".join(found)
        self.assertEqual(joined.count("combined with a tool allowlist"), 3, joined)
        self.assertIn("ClaudeAgentOptions built outside", joined)
        self.assertIn("CLI flag", joined)


class SandboxedOptions(unittest.TestCase):
    def test_judgment_agent_gets_no_tools_and_an_empty_private_cwd(self):
        options = agent_sandbox.sandboxed_options(model="m", system_prompt="s", tools=[], max_turns=4)
        self.assertEqual(options.tools, [])
        self.assertEqual(options.allowed_tools, [])
        self.assertEqual(options.permission_mode, "dontAsk")
        self.assertEqual(options.setting_sources, [])
        for name in ("Bash", "Write", "Edit", "WebFetch", "WebSearch", "Task", "Agent", "Skill"):
            self.assertIn(name, options.disallowed_tools)
        cwd = Path(options.cwd)
        self.assertEqual(cwd, agent_sandbox.sandbox_dir())
        self.assertNotIn(REPO.resolve(), [cwd, *cwd.parents])
        self.assertEqual(cwd.stat().st_mode & 0o077, 0)
        self.assertFalse(cwd.is_symlink())
        strict = getattr(options, "strict_mcp_config", False) or "strict-mcp-config" in options.extra_args
        self.assertTrue(strict)

    def test_read_tools_are_scoped_to_cwd_and_mcp_tools_are_named(self):
        with tempfile.TemporaryDirectory() as raw:
            options = agent_sandbox.sandboxed_options(
                model="m", system_prompt="s", tools=["Read", "Grep", "Glob"], max_turns=60,
                cwd=raw, allowed_tools=["mcp__wikilean__decl_exists"], mcp_servers={"wikilean": object()})
            self.assertEqual(Path(options.cwd), Path(raw).resolve())
        self.assertEqual(options.tools, ["Read", "Grep", "Glob", "ToolSearch"])
        self.assertEqual(options.allowed_tools, ["mcp__wikilean__decl_exists", "ToolSearch"])
        for name in ("Read", "Grep", "Glob"):
            self.assertNotIn(name, options.allowed_tools, "a bare allow rule would approve reads anywhere")
            self.assertNotIn(name, options.disallowed_tools)
        self.assertIn("Bash", options.disallowed_tools)
        self.assertEqual(options.permission_mode, "dontAsk")

    def test_scoped_bash_keeps_bash_visible_but_unapproved(self):
        with tempfile.TemporaryDirectory() as raw:
            options = agent_sandbox.sandboxed_options(
                model="m", system_prompt="s", tools=["Read", "Bash"], max_turns=2, cwd=raw,
                allowed_tools=["Bash(python3 /abs/cli.py:*)"])
        self.assertIn("Bash", options.tools)
        self.assertNotIn("Bash", options.disallowed_tools)
        self.assertEqual(options.allowed_tools, ["Bash(python3 /abs/cli.py:*)"])

    def test_rejects_unscoped_approvals_and_repository_cwds(self):
        for rule in ("Bash", "Read", "Write", "WebFetch"):
            with self.assertRaises(ValueError):
                agent_sandbox.sandboxed_options(model="m", system_prompt="s", tools=[rule],
                                                allowed_tools=[rule], max_turns=1)
        for cwd in (REPO, REPO / "site", REPO / "wiki"):
            with self.assertRaises(ValueError):
                agent_sandbox.sandboxed_options(model="m", system_prompt="s", tools=[], max_turns=1, cwd=cwd)
        with self.assertRaises(ValueError):
            agent_sandbox.sandboxed_options(model="m", system_prompt="s", tools=["mcp__x__y"], max_turns=1)

    def test_refuses_an_sdk_without_the_tools_option(self):
        import dataclasses
        import claude_agent_sdk

        @dataclasses.dataclass
        class OldOptions:
            allowed_tools: list = dataclasses.field(default_factory=list)
            permission_mode: str | None = None
            cwd: str | None = None

        with patch.object(claude_agent_sdk, "ClaudeAgentOptions", OldOptions):
            with self.assertRaisesRegex(RuntimeError, "too old"):
                agent_sandbox.sandboxed_options(model="m", system_prompt="s", tools=[], max_turns=1)

    def test_sandbox_refuses_a_symlinked_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "elsewhere"
            target.mkdir()
            link = Path(raw) / f"wikilean-agent-sandbox-{os.getuid()}"
            link.symlink_to(target)
            with patch.object(agent_sandbox.tempfile, "gettempdir", return_value=raw):
                with self.assertRaisesRegex(RuntimeError, "not a real directory"):
                    agent_sandbox.sandbox_dir()


def _capture_query(captured: list):
    async def fake_query(*, prompt, options):
        captured.append(options)
        if False:  # pragma: no cover - makes this an async generator
            yield None
    return fake_query


class PipelineAgents(unittest.TestCase):
    def test_batch_annotate_agents_run_sandboxed(self):
        import batch_annotate as ba
        captured: list = []
        with tempfile.TemporaryDirectory() as raw, patch.object(ba, "query", _capture_query(captured)):
            asyncio.run(ba.run_agent("system", "user", None, [], 12))
            asyncio.run(ba.run_agent("system", "user", Path(raw), ["Read", "Grep", "Glob", "mcp__wikilean__loogle"],
                                     60, mcp_servers={"wikilean": object()}))
            mathlib = Path(raw).resolve()
        agent1, agent2 = captured
        self.assertEqual(agent1.tools, [])
        self.assertEqual(Path(agent1.cwd), agent_sandbox.sandbox_dir())
        self.assertEqual(agent2.tools, ["Read", "Grep", "Glob", "ToolSearch"])
        self.assertEqual(agent2.allowed_tools, ["mcp__wikilean__loogle", "ToolSearch"])
        self.assertEqual(Path(agent2.cwd), mathlib)
        for options in captured:
            self.assertEqual(options.permission_mode, "dontAsk")
            self.assertIn("Bash", options.disallowed_tools)

    def test_agent1_is_called_without_a_repository_cwd(self):
        import batch_annotate as ba
        import test_native_annotations as fixtures
        first = fixtures.item(status="not_formalized", links=[], unlinked=[fixtures.occurrence()])
        final = fixtures.item()
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            (directory / "Synthetic_Article.42.sections.json").write_text('{"sections": []}')
            (directory / "Synthetic_Article.42.html").write_text(fixtures.HTML)
            fake = AsyncMock(side_effect=[({"annotations": [first]}, {}), ({"annotations": [final]}, {})])
            with patch.object(ba, "CACHE", directory), patch.object(ba, "ANNOT", directory), \
                    patch.object(ba, "OUT", directory), patch.object(ba, "fetch_html", return_value=True), \
                    patch.object(ba, "run_script", return_value=(0, "")), patch.object(ba, "run_agent", fake):
                result = asyncio.run(ba.annotate_one({"title": fixtures.TITLE}, asyncio.Semaphore(1), {},
                                                     target_revid=42))
        self.assertNotIn("error", result)
        agent1_call, agent2_call = fake.call_args_list
        self.assertIsNone(agent1_call.args[2])
        self.assertEqual(agent1_call.args[3], [])
        self.assertEqual(agent2_call.args[2], ba.MATHLIB)

    def test_brain_sync_agents_run_sandboxed(self):
        sys.path.insert(0, str(REPO / "brain"))
        try:
            import sync_agents
        finally:
            sys.path.remove(str(REPO / "brain"))
        captured: list = []
        import claude_agent_sdk
        state = {"tokens": 0, "consec_err": 0, "abort": False}
        with patch.object(claude_agent_sdk, "query", _capture_query(captured)):
            asyncio.run(sync_agents._run_agent("system", "user", state))
        (options,) = captured
        self.assertEqual(options.tools, [])
        self.assertEqual(options.allowed_tools, [])
        self.assertEqual(options.permission_mode, "dontAsk")
        self.assertEqual(Path(options.cwd), agent_sandbox.sandbox_dir())


if __name__ == "__main__":
    unittest.main()
