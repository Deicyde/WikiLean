"""Sandboxed claude-agent-sdk options for every pipeline agent.

Pipeline agents read untrusted text (Wikipedia articles, external database
pages, Lean docstrings), which is a prompt-injection surface. Under
permission_mode "bypassPermissions" the SDK's ``allowed_tools`` only lists tools
to auto-approve; it restricts nothing. Agents that were meant to be tool-less
therefore ran Bash, with ``site/`` as their working directory, next to
``wiki/.dev.vars`` (the PIPELINE_TOKEN bearer).

``sandboxed_options`` is the one place pipeline code builds a
``ClaudeAgentOptions`` (``site/test_agent_sandbox.py`` enforces that). It
restricts each agent positively:

* ``tools`` is the base set of built-in tools the model can see at all
  (``[]`` means none). Only the read tools (Read, Grep, Glob) and ToolSearch
  may be listed. **No agent gets Bash**: the CLI never checks input-redirect
  targets (``cat < file``) against the working directory or the deny rules, so
  a Bash rule scoped to one script still reads any file. Agents call search
  CLIs through in-process MCP tools with validated arguments instead
  (``site/search_tools.py``).
* ``disallowed_tools`` denies every shell, write, network and sub-agent tool,
  so a CLI that ignored ``tools`` still could not expose them. When read tools
  are present it also denies reads of Claude's own directories (the config
  directory with its session transcripts, agent memory and tasks, and the
  ``claude-<uid>`` temp root), which the CLI otherwise allows outside ``cwd``.
* ``permission_mode="dontAsk"`` replaces the bypass: a call that is not
  pre-approved is denied rather than run. Read, Grep and Glob are pre-approved
  only inside ``cwd``, so a read outside it is refused. ``allowed_tools`` may
  name MCP tools (``mcp__…``) and ToolSearch only.
* ``setting_sources=[]`` and a strict MCP config load no user, project or local
  settings (their permission allow rules, hooks and CLAUDE.md) and no MCP
  servers other than the ones passed in.
* ``cwd`` defaults to a private, empty sandbox directory outside the
  repository. A ``cwd`` inside, or an ancestor of, any WikiLean checkout is
  refused (each can hold ``wiki/.dev.vars``), and so are the home directory and
  its ancestors. The checkouts are this repository, every git worktree that
  shares its object store, and the paths in ``WIKILEAN_PROTECTED_ROOTS``.
"""
from __future__ import annotations

import dataclasses
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterable

REPO = Path(__file__).resolve().parent.parent

PERMISSION_MODE = "dontAsk"

# Bumped whenever the agents' environment changes (tool sets, permission mode,
# settings, cwd, deny rules). site/moderate.py hashes it into prompt_sha, so
# research cohorts before and after an environment change stay distinguishable
# even when no prompt text moved. sandbox-v1: this module (positive tool sets,
# dontAsk, no settings, sandbox cwd, no Bash, Claude-directory deny rules).
SANDBOX_VERSION = "sandbox-v1"

# Built-in tools that can run commands, write files, reach the network or
# spawn further agents. Every agent has all of them denied.
# Skill is listed because ``tools`` does not govern it (bench/run_benchmark.py).
DANGEROUS_TOOLS = (
    "Bash", "BashOutput", "KillShell", "KillBash", "PowerShell", "Monitor",
    "Write", "Edit", "MultiEdit", "NotebookEdit",
    "WebFetch", "WebSearch",
    "Task", "Agent", "Skill", "SlashCommand",
    "CronCreate", "CronDelete", "RemoteTrigger",
    "EnterWorktree", "ExitWorktree",
)

# Deferred MCP tools are loaded through ToolSearch, so it rides along whenever
# MCP tools are allowed. It only searches and loads tool schemas.
TOOL_SEARCH = "ToolSearch"

# The read tools: pre-approved only inside cwd under dontAsk. The CLI checks
# all three against Read(...) permission rules (deny rules included).
READ_TOOLS = ("Read", "Grep", "Glob")

# The only built-in tools an agent's ``tools`` may list.
PERMITTED_TOOLS = frozenset(READ_TOOLS) | {TOOL_SEARCH}

PROTECTED_ROOTS_ENV = "WIKILEAN_PROTECTED_ROOTS"

_REQUIRED_FIELDS = ("tools", "allowed_tools", "disallowed_tools",
                    "permission_mode", "setting_sources", "cwd")


def _uid() -> int | None:
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid else None


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _overlaps(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root``, inside it, or one of its ancestors."""
    return _inside(path, root) or _inside(root, path)


def _gitdir_of(checkout: Path) -> Path | None:
    """The git directory of a checkout: ``.git`` itself, or the target of a
    linked worktree's ``.git`` file (``gitdir: <path>``)."""
    dot_git = checkout / ".git"
    try:
        if dot_git.is_dir():
            return dot_git
        line = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    target = Path(line[len("gitdir:"):].strip())
    return (checkout / target).resolve() if not target.is_absolute() else target.resolve()


def protected_roots() -> list[Path]:
    """Every checkout an agent cwd must stay clear of.

    This repository; when it is a linked worktree, the main checkout that owns
    the shared ``.git``; every other worktree registered there; and the paths in
    ``WIKILEAN_PROTECTED_ROOTS`` (``os.pathsep``-separated). Each can hold
    ``wiki/.dev.vars``. Reads only small git metadata files, never runs git.
    """
    roots = [REPO.resolve()]
    gitdir = _gitdir_of(REPO)
    if gitdir is not None:
        common = gitdir
        if gitdir.parent.name == "worktrees":
            common = gitdir.parent.parent
        if common.name == ".git":
            roots.append(common.parent.resolve())
        try:
            registered = sorted((common / "worktrees").iterdir())
        except OSError:
            registered = []
        for entry in registered:
            try:
                target = Path((entry / "gitdir").read_text(encoding="utf-8").strip())
            except OSError:
                continue
            if target.is_absolute():
                roots.append(target.parent.resolve())
    for raw in os.environ.get(PROTECTED_ROOTS_ENV, "").split(os.pathsep):
        if raw.strip():
            roots.append(Path(raw.strip()).expanduser().resolve())
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def check_workdir(workdir: Path) -> Path:
    """Refuse a working directory that could expose a checkout's secrets.

    ``workdir`` must not be inside or an ancestor of any protected root, and
    must not be the home directory or one of its ancestors (``/`` included).
    Returns the resolved path.
    """
    resolved = workdir.resolve()
    for root in protected_roots():
        if _overlaps(resolved, root):
            raise ValueError(
                f"agent cwd {resolved} overlaps the WikiLean checkout {root}, which can hold "
                "wiki/.dev.vars; pass the paths an agent needs in its prompt or use the sandbox")
    home = Path.home().resolve()
    if _inside(home, resolved):
        raise ValueError(f"agent cwd {resolved} is the home directory or one of its ancestors")
    return resolved


def claude_read_deny_rules() -> list[str]:
    """``Read(...)`` deny rules for Claude's own directories.

    The CLI allows reads there even outside cwd (project session transcripts
    under ``<config>/projects/``, agent and auto memory, tasks, teams, and the
    per-project temp directory under ``<tmp>/claude-<uid>/``). Deny rules are
    checked first, and the CLI applies Read rules to Grep and Glob as well.
    """
    dirs: list[Path] = []
    home_config = Path.home() / ".claude"
    dirs += [home_config, home_config.resolve()]
    config = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if config:
        dirs += [Path(config).expanduser(), Path(config).expanduser().resolve()]
    uid = _uid()
    tmp_name = f"claude-{uid if uid is not None else 0}"
    tmp_bases = [Path("/tmp")]
    custom_tmp = os.environ.get("CLAUDE_CODE_TMPDIR", "").strip()
    if custom_tmp:
        tmp_bases.append(Path(custom_tmp).expanduser())
    for base in tmp_bases:
        dirs += [base / tmp_name, base.resolve() / tmp_name]
    rules: list[str] = []
    for directory in dirs:
        if not directory.is_absolute():
            continue
        text = directory.as_posix()
        # The CLI splits --disallowedTools on commas and spaces outside
        # parentheses, so a path holding a parenthesis or a control character
        # cannot ride in a rule.
        if any(ch in text for ch in ",()") or any(ord(ch) < 0x20 or 0x7f <= ord(ch) <= 0x9f for ch in text):
            raise RuntimeError(f"cannot express a deny rule for {text!r}; refusing to run an agent "
                               "that could read Claude's session files")
        rule = f"Read(/{text}/**)"
        if rule not in rules:
            rules.append(rule)
    return rules


def sandbox_dir() -> Path:
    """Return the private, empty directory agents run in.

    It lives in the system temporary directory, outside every checkout, is
    created with mode 0700, and is never a symlink or another user's directory.
    Sandboxed agents cannot write, so it stays empty.
    """
    uid = _uid()
    base = Path(tempfile.gettempdir()) / f"wikilean-agent-sandbox-{uid if uid is not None else 'user'}"
    try:
        base.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = os.lstat(base)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"agent sandbox {base} is not a real directory")
    if uid is not None and info.st_uid != uid:
        raise RuntimeError(f"agent sandbox {base} belongs to another user")
    if info.st_mode & 0o077:
        os.chmod(base, 0o700)
    resolved = base.resolve()
    for root in protected_roots():
        if _overlaps(resolved, root):
            raise RuntimeError(f"agent sandbox {resolved} is inside the repository {root}")
    return resolved


def _check_allowed(allowed: Iterable[str]) -> list[str]:
    out = []
    for rule in allowed:
        if not isinstance(rule, str) or not rule:
            raise ValueError(f"allowed tool rule must be a non-empty string: {rule!r}")
        if not (rule.startswith("mcp__") or rule == TOOL_SEARCH) or "(" in rule:
            raise ValueError(
                f"allowed_tools may name MCP tools and ToolSearch only, not {rule!r}: a built-in "
                "allow rule (even a scoped Bash(...) one) widens what the agent can reach")
        out.append(rule)
    return out


def sandboxed_options(*, model: str | None, system_prompt: str, tools: Iterable[str],
                      max_turns: int, cwd: str | Path | None = None,
                      allowed_tools: Iterable[str] = (),
                      mcp_servers: dict | None = None) -> Any:
    """Build the ClaudeAgentOptions for one sandboxed agent call.

    ``tools`` lists built-in tool names from Read, Grep, Glob and ToolSearch
    (``[]`` for a judgment-only agent). ``allowed_tools`` lists the MCP tool
    names that run without a prompt. ``cwd`` is the only directory the read
    tools may see; ``None`` selects :func:`sandbox_dir`.
    """
    from claude_agent_sdk import ClaudeAgentOptions

    have = {f.name for f in dataclasses.fields(ClaudeAgentOptions)}
    missing = [name for name in _REQUIRED_FIELDS if name not in have]
    if missing:
        raise RuntimeError(
            "claude-agent-sdk is too old to sandbox agents (ClaudeAgentOptions lacks "
            f"{', '.join(missing)}); refusing to run an unrestricted agent")

    base = [str(t) for t in tools]
    for name in base:
        if name not in PERMITTED_TOOLS:
            raise ValueError(
                f"tools may list only {', '.join(sorted(PERMITTED_TOOLS))}, not {name!r}; "
                "expose anything else as an in-process MCP tool with validated arguments")
    allowed = _check_allowed(allowed_tools)
    if any(rule.startswith("mcp__") for rule in allowed) and TOOL_SEARCH not in base:
        base.append(TOOL_SEARCH)
    if TOOL_SEARCH in base and TOOL_SEARCH not in allowed:
        allowed.append(TOOL_SEARCH)
    denied = list(DANGEROUS_TOOLS)
    if any(name in READ_TOOLS for name in base):
        denied += claude_read_deny_rules()

    workdir = sandbox_dir() if cwd is None else check_workdir(Path(cwd))

    kwargs: dict[str, Any] = dict(
        model=model,
        system_prompt=system_prompt,
        tools=base,
        allowed_tools=allowed,
        disallowed_tools=denied,
        permission_mode=PERMISSION_MODE,
        setting_sources=[],
        cwd=str(workdir),
        max_turns=max_turns,
    )
    if mcp_servers:
        kwargs["mcp_servers"] = mcp_servers
    if "strict_mcp_config" in have:
        kwargs["strict_mcp_config"] = True
    else:
        kwargs["extra_args"] = {"strict-mcp-config": None}
    return ClaudeAgentOptions(**kwargs)
