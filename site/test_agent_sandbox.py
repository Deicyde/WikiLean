#!/usr/bin/env python3
"""The pipeline's Claude agents run sandboxed, never with bypassed permissions.

Under permission_mode "bypassPermissions" the SDK's allowed_tools only lists
tools to auto-approve, so an allowlist next to it restricts nothing: agents fed
Wikipedia text ran Bash beside wiki/.dev.vars. These checks fail when any
tracked Python file reintroduces the bypass (with or without an allowlist),
builds ClaudeAgentOptions outside site/agent_sandbox.py, gives a sandboxed agent
Bash (a scoped Bash rule still reads any file through `cat < file`), spawns a
pipeline `claude` CLI without its tool-less flags, or when the sandbox helper
stops restricting tools, permissions, settings, Claude's own directories or the
working directory.
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
# Pipeline code (not the operator-run bench/ research harness): a `claude` CLI
# spawned here must be tool-less, settings-free and MCP-strict, as bot/triage.py is.
PIPELINE_ROOTS = ("site", "brain", "manage", "bot", "catalog", "wikifunctions", "scripts")
CLI_REQUIRED_FLAGS = ("--tools", "--setting-sources", "--strict-mcp-config")


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


def _literal_strings(node: ast.AST) -> list[ast.AST] | None:
    """The elements of a list/tuple literal, or None for anything else."""
    return list(node.elts) if isinstance(node, (ast.List, ast.Tuple)) else None


def _leading_text(node: ast.AST) -> str | None:
    """The literal text a string expression starts with (f-strings included)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        first = node.values[0] if node.values else None
        return first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else ""
    return None


def _sandboxed_call_problems(rel: str, node: ast.Call) -> list[str]:
    found = []
    for kw in node.keywords:
        elts = _literal_strings(kw.value)
        if elts is None:
            continue
        if kw.arg == "tools":
            for elt in elts:
                name = _const(elt)
                if name is not None and name not in agent_sandbox.PERMITTED_TOOLS:
                    found.append(f"{rel}:{node.lineno}: sandboxed agent given built-in tool {name!r}; "
                                 "expose it as an in-process MCP tool instead")
        elif kw.arg == "allowed_tools":
            for elt in elts:
                text = _leading_text(elt)
                if text is not None and not (text.startswith("mcp__") or text == agent_sandbox.TOOL_SEARCH):
                    found.append(f"{rel}:{node.lineno}: sandboxed agent allow rule {text!r}… "
                                 "(only MCP tool names may be pre-approved)")
    return found


def _cli_argv_problems(rel: str, node: ast.AST) -> list[str]:
    elts = _literal_strings(node)
    if not elts or _const(elts[0]) != "claude":
        return []
    flags = {_const(e) for e in elts}
    if len(elts) > 1 and _const(elts[1]) in {"-v", "--version"}:
        return []
    missing = [flag for flag in CLI_REQUIRED_FLAGS if flag not in flags]
    if not missing:
        return []
    return [f"{rel}:{node.lineno}: pipeline `claude` CLI spawned without {', '.join(missing)} "
            "(bot/triage.py's tool-less form)"]


def scan(path: Path, rel: str | None = None) -> list[str]:
    """Return the sandbox violations in one Python file (``rel`` names it)."""
    rel = rel or path.relative_to(REPO).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    found: list[str] = []
    # Test files build fake argv lists (CompletedProcess(["claude"], ...)) and
    # deliberately bad sandboxed_options calls; they never run an agent.
    is_test = rel.rsplit("/", 1)[-1].startswith("test_")
    pipeline = rel.split("/", 1)[0] in PIPELINE_ROOTS and not is_test
    for node in ast.walk(tree):
        if pipeline:
            found += _cli_argv_problems(rel, node)
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
            if name == "sandboxed_options" and not is_test:
                found += _sandboxed_call_problems(rel, node)
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

    def test_scan_catches_bash_and_scoped_rules_on_a_sandboxed_agent(self):
        # catalog/tag_with_mathlib.py once ran tools=[..., "Bash"] with two
        # Bash(python3 <cli>:*) rules; `cat < file` passed both.
        with tempfile.TemporaryDirectory() as raw:
            bad = Path(raw) / "bad_tagger.py"
            bad.write_text(
                "from agent_sandbox import sandboxed_options\n"
                "CLI = '/abs/cli.py'\n"
                "o = sandboxed_options(model='m', system_prompt='s', max_turns=1,\n"
                "    tools=['Read', 'Grep', 'Glob', 'Bash'],\n"
                "    allowed_tools=[f'Bash(python3 {CLI}:*)', 'Bash(python3:*)', 'mcp__x__y'])\n",
                encoding="utf-8")
            found = scan(bad, "catalog/bad_tagger.py")
        joined = "\n".join(found)
        self.assertIn("given built-in tool 'Bash'", joined)
        self.assertEqual(joined.count("allow rule"), 2, joined)
        self.assertNotIn("mcp__x__y", joined)

    def test_scan_requires_tool_less_flags_on_pipeline_claude_cli_calls(self):
        with tempfile.TemporaryDirectory() as raw:
            bad = Path(raw) / "bad_cli.py"
            bad.write_text(
                "cmd = ['claude', '-p', 'prompt', '--output-format', 'json']\n"
                "ok = ['claude', '-p', 'x', '--tools', '', '--setting-sources', '', '--strict-mcp-config']\n"
                "version = ['claude', '-v']\n",
                encoding="utf-8")
            found = scan(bad, "bot/bad_cli.py")
            self.assertEqual(len(found), 1, found)
            self.assertIn("--tools, --setting-sources, --strict-mcp-config", found[0])
            # The operator-run research harness is outside the pipeline rule.
            self.assertEqual(scan(bad, "bench/bad_cli.py"), [])


class SandboxedOptions(unittest.TestCase):
    def test_judgment_agent_gets_no_tools_and_a_private_cwd(self):
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

    def test_no_agent_gets_bash_or_any_built_in_allow_rule(self):
        # The CLI never checks input-redirect targets (`cat < file`) against the
        # working directory or the deny rules, so a Bash rule scoped to one
        # script still reads any file. Bash is refused outright, scoped or not.
        with tempfile.TemporaryDirectory() as raw:
            for tools in (["Bash"], ["Read", "Bash"], ["Write"], ["WebFetch"], ["LS"], ["Task"]):
                with self.assertRaisesRegex(ValueError, "tools may list only"):
                    agent_sandbox.sandboxed_options(model="m", system_prompt="s", tools=tools,
                                                    max_turns=1, cwd=raw)
            for rule in ("Bash(python3 /abs/cli.py:*)", "Bash(python3:*)", "Bash", "Read",
                         "Read(//etc/**)", "WebFetch(domain:example.com)", "mcp__x__y(z)"):
                with self.assertRaisesRegex(ValueError, "MCP tools and ToolSearch only"):
                    agent_sandbox.sandboxed_options(model="m", system_prompt="s", tools=["Read"],
                                                    allowed_tools=[rule], max_turns=1, cwd=raw)

    def test_read_agents_cannot_read_claude_session_or_memory_directories(self):
        # Outside cwd the CLI still auto-allows reads of ~/.claude/projects/<cwd>/
        # (every session transcript run there), agent memory, tasks and the
        # claude-<uid> temp root; Read(...) deny rules are checked first.
        home_config = (Path.home() / ".claude").as_posix()
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as config:
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": config, "CLAUDE_CODE_TMPDIR": raw}):
                options = agent_sandbox.sandboxed_options(
                    model="m", system_prompt="s", tools=["Read", "Grep", "Glob"], max_turns=1, cwd=raw)
                judgment = agent_sandbox.sandboxed_options(model="m", system_prompt="s", tools=[], max_turns=1)
            denied = options.disallowed_tools
            self.assertIn(f"Read(/{home_config}/**)", denied)
            self.assertIn(f"Read(/{Path(config).resolve().as_posix()}/**)", denied)
            self.assertIn(f"Read(//tmp/claude-{os.getuid()}/**)", denied)
            self.assertIn(f"Read(/{Path(raw).as_posix()}/claude-{os.getuid()}/**)", denied)
        for rule in denied:
            if rule.startswith("Read("):
                self.assertTrue(rule.startswith("Read(//") and rule.endswith("/**)"), rule)
        self.assertEqual(judgment.disallowed_tools, list(agent_sandbox.DANGEROUS_TOOLS))
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/odd(path)"}):
            with self.assertRaisesRegex(RuntimeError, "cannot express a deny rule"):
                agent_sandbox.claude_read_deny_rules()

    def test_rejects_repository_cwds_their_ancestors_and_home(self):
        for cwd in (REPO, REPO / "site", REPO / "wiki", REPO.parent, Path(REPO.anchor),
                    Path.home(), Path.home().parent):
            with self.assertRaises(ValueError, msg=str(cwd)):
                agent_sandbox.sandboxed_options(model="m", system_prompt="s", tools=["Read"],
                                                max_turns=1, cwd=cwd)
        with self.assertRaises(ValueError):
            agent_sandbox.sandboxed_options(model="m", system_prompt="s", tools=["mcp__x__y"], max_turns=1)

    def test_rejects_the_home_directory_and_its_ancestors_wherever_the_repo_is(self):
        with tempfile.TemporaryDirectory() as raw:
            top = Path(raw).resolve()
            home = top / "home" / "user"
            (home / "work").mkdir(parents=True)
            with patch.object(agent_sandbox.Path, "home", return_value=home):
                for cwd in (home, top / "home", top):
                    with self.assertRaisesRegex(ValueError, "home directory", msg=str(cwd)):
                        agent_sandbox.check_workdir(cwd)
                self.assertEqual(agent_sandbox.check_workdir(home / "work"), home / "work")

    def test_protected_roots_cover_every_worktree_and_the_explicit_list(self):
        # A linked worktree's main checkout (the nightly host's checkout, say)
        # and its sibling worktrees each hold their own wiki/.dev.vars.
        with tempfile.TemporaryDirectory() as raw:
            top = Path(raw).resolve()
            main, linked, other, extra = (top / "main", top / "linked", top / "other", top / "extra")
            for wt in ("linked", "other"):
                meta = main / ".git" / "worktrees" / wt
                meta.mkdir(parents=True)
                (meta / "gitdir").write_text(f"{top / wt / '.git'}\n", encoding="utf-8")
                (top / wt).mkdir()
                (top / wt / ".git").write_text(f"gitdir: {meta}\n", encoding="utf-8")
            extra.mkdir()
            (top / "mathlib4").mkdir()
            with patch.object(agent_sandbox, "REPO", linked), \
                    patch.dict(os.environ, {agent_sandbox.PROTECTED_ROOTS_ENV: f"{extra}{os.pathsep}"}):
                roots = agent_sandbox.protected_roots()
                self.assertEqual(roots, [linked, main, other, extra])
                for cwd in (main, main / "wiki", other / "site", extra, top):
                    with self.assertRaises(ValueError, msg=str(cwd)):
                        agent_sandbox.check_workdir(cwd)
                self.assertEqual(agent_sandbox.check_workdir(top / "mathlib4"), top / "mathlib4")

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

    def test_sandbox_is_created_private_and_empty(self):
        with tempfile.TemporaryDirectory() as raw:
            with patch.object(agent_sandbox.tempfile, "gettempdir", return_value=raw):
                sandbox = agent_sandbox.sandbox_dir()
            self.assertEqual(sandbox, (Path(raw) / f"wikilean-agent-sandbox-{os.getuid()}").resolve())
            self.assertEqual(sandbox.stat().st_mode & 0o777, 0o700)
            self.assertEqual(list(sandbox.iterdir()), [])

    def test_sandbox_tightens_a_group_or_world_readable_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            loose = Path(raw) / f"wikilean-agent-sandbox-{os.getuid()}"
            loose.mkdir()
            os.chmod(loose, 0o755)
            with patch.object(agent_sandbox.tempfile, "gettempdir", return_value=raw):
                sandbox = agent_sandbox.sandbox_dir()
            self.assertEqual(sandbox.stat().st_mode & 0o777, 0o700)

    def test_sandbox_refuses_another_users_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            with patch.object(agent_sandbox.tempfile, "gettempdir", return_value=raw), \
                    patch.object(agent_sandbox, "_uid", return_value=os.getuid() + 1):
                with self.assertRaisesRegex(RuntimeError, "belongs to another user"):
                    agent_sandbox.sandbox_dir()

    def test_sandbox_refuses_a_temp_dir_inside_a_checkout(self):
        with tempfile.TemporaryDirectory() as raw:
            checkout = Path(raw).resolve()
            with patch.object(agent_sandbox.tempfile, "gettempdir", return_value=str(checkout / "tmp")), \
                    patch.object(agent_sandbox, "protected_roots", return_value=[checkout]):
                (checkout / "tmp").mkdir()
                with self.assertRaisesRegex(RuntimeError, "is inside the repository"):
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

    def test_catalog_tagger_runs_with_mcp_search_tools_and_no_bash(self):
        sys.path.insert(0, str(REPO / "catalog"))
        try:
            import tag_with_mathlib as tagger
        finally:
            sys.path.remove(str(REPO / "catalog"))
        import search_tools
        captured: list = []
        with tempfile.TemporaryDirectory() as raw:
            mathlib = Path(raw) / "mathlib4"
            mathlib.mkdir()
            out = Path(raw) / "tagged.jsonl"
            with patch.object(tagger, "query", _capture_query(captured)), \
                    patch.object(tagger, "MATHLIB", mathlib), \
                    patch.object(tagger, "CORRECTIONS", Path(raw) / "none.jsonl"), \
                    patch("builtins.print"):
                asyncio.run(tagger.run([{"title": "Vector space", "wikidata_qid": "Q125977"}],
                                       {}, out, 1, "m", 4))
            (options,) = captured
            self.assertEqual(Path(options.cwd), mathlib.resolve())
        names = [f"mcp__{search_tools.TAGGER_SERVER_NAME}__{n}" for n in
                 ("mathlib_semantic", "loogle", "decl_exists", "wikidata_search", "wikidata_entity",
                  "wikidata_sitelinks")]
        self.assertEqual(options.tools, ["Read", "Grep", "Glob", "ToolSearch"])
        self.assertEqual(options.allowed_tools, names + ["ToolSearch"])
        self.assertEqual(set(options.mcp_servers), {search_tools.TAGGER_SERVER_NAME})
        self.assertEqual(options.permission_mode, "dontAsk")
        for name in agent_sandbox.DANGEROUS_TOOLS:
            self.assertIn(name, options.disallowed_tools)
        self.assertNotIn("Bash", options.system_prompt)
        self.assertNotIn("python3 ", options.system_prompt)

    def test_tagger_tools_run_one_fixed_subcommand_with_a_validated_argument(self):
        import search_tools
        server, names = search_tools.build_tagger_server()
        self.assertIsNotNone(server)
        handlers = {t.name: t.handler for t in search_tools.tagger_tools()}
        self.assertEqual(sorted(handlers), sorted(n.rsplit("__", 1)[1] for n in names))
        calls: list = []
        with patch.object(search_tools, "_run_cli", lambda cli, *argv, **kw: calls.append((cli.name, argv)) or "ok"):
            good = {
                "mathlib_semantic": ({"query": "dual vector space"}, ("mathlib_search.py", ("semantic", "dual vector space"))),
                "loogle": ({"query": "|- ?a + ?b = ?b + ?a"}, ("mathlib_search.py", ("loogle", "|- ?a + ?b = ?b + ?a"))),
                "decl_exists": ({"name": "Module.Dual"}, ("mathlib_search.py", ("decl", "Module.Dual", "--live"))),
                "wikidata_search": ({"label": "dual space"}, ("wikidata.py", ("search", "dual space"))),
                "wikidata_entity": ({"qid": "Q752487"}, ("wikidata.py", ("entity", "Q752487"))),
                "wikidata_sitelinks": ({"qid": "Q752487"}, ("wikidata.py", ("sitelinks", "Q752487"))),
            }
            for name, (args, expected) in good.items():
                calls.clear()
                out = asyncio.run(handlers[name](args))
                self.assertEqual(out["content"][0]["text"], "ok", name)
                self.assertEqual(calls, [expected], name)
            bad = {
                "mathlib_semantic": ["", "-x", "--refresh", "a\nb", "x" * 501, None],
                "loogle": ["--limit=9999", "a\x00b"],
                "decl_exists": ["Module Dual", "--refresh", "-"],
                "wikidata_search": ["-", "\t"],
                "wikidata_entity": ["Q0", "q1", "Q1;Q2", "Q1 Q2", "--json", "P31"],
                "wikidata_sitelinks": ["Q", "Q12x", "-Q1"],
            }
            calls.clear()
            for name, values in bad.items():
                key = next(iter(good[name][0]))
                for value in values:
                    out = asyncio.run(handlers[name]({key: value}))
                    self.assertIn("invalid input", out["content"][0]["text"], (name, value))
            self.assertEqual(calls, [], "an invalid argument must never reach a CLI")

    def test_catalog_smoke_agent_reads_only(self):
        sys.path.insert(0, str(REPO / "catalog"))
        try:
            import smoke_agent
        finally:
            sys.path.remove(str(REPO / "catalog"))
        captured: list = []
        with tempfile.TemporaryDirectory() as raw, \
                patch.object(smoke_agent, "query", _capture_query(captured)), \
                patch.object(smoke_agent, "MATHLIB", raw), patch("builtins.print"):
            self.assertEqual(asyncio.run(smoke_agent.main()), 1)  # no ResultMessage from the fake
        (options,) = captured
        self.assertEqual(options.tools, ["Read", "Grep", "Glob"])
        self.assertEqual(options.allowed_tools, [])
        self.assertIn("Bash", options.disallowed_tools)

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
