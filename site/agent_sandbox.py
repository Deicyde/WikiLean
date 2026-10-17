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
  (``[]`` means none). A tool not listed is absent from its context.
* ``disallowed_tools`` denies every shell, write, network and sub-agent tool
  that is not in ``tools``, so a CLI that ignored ``tools`` still could not
  expose them.
* ``permission_mode="dontAsk"`` replaces the bypass: a call that is not
  pre-approved is denied rather than run. Read, Grep and Glob are pre-approved
  only inside ``cwd``, so a read outside it is refused. ``allowed_tools`` may
  name MCP tools and scoped rules such as ``Bash(python3 /abs/cli.py:*)``, never
  a bare built-in that would approve it everywhere.
* ``setting_sources=[]`` and a strict MCP config load no user, project or local
  settings (their permission allow rules, hooks and CLAUDE.md) and no MCP
  servers other than the ones passed in.
* ``cwd`` defaults to a private, empty sandbox directory outside the
  repository. A ``cwd`` inside the WikiLean repository is refused, because the
  repository holds ``wiki/.dev.vars``.
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

# Built-in tools that can run commands, write files, reach the network or
# spawn further agents. Each one not in an agent's ``tools`` is also denied.
# Skill is listed because ``tools`` does not govern it (bench/run_benchmark.py).
DANGEROUS_TOOLS = (
    "Bash", "BashOutput", "KillShell", "KillBash", "PowerShell", "Monitor",
    "Write", "Edit", "MultiEdit", "NotebookEdit",
    "WebFetch", "WebSearch",
    "Task", "Agent", "Skill", "SlashCommand",
    "CronCreate", "CronDelete", "RemoteTrigger",
    "EnterWorktree", "ExitWorktree",
)

# Bare built-in names that, as an allow rule, approve the tool for any input.
_UNSCOPED_BUILTINS = frozenset(DANGEROUS_TOOLS) | {"Read", "Grep", "Glob", "LS"}

# Deferred MCP tools are loaded through ToolSearch, so it rides along whenever
# MCP tools are allowed. It only searches and loads tool schemas.
TOOL_SEARCH = "ToolSearch"

_REQUIRED_FIELDS = ("tools", "allowed_tools", "disallowed_tools",
                    "permission_mode", "setting_sources", "cwd")


def _uid() -> int | None:
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid else None


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def sandbox_dir() -> Path:
    """Return the private, empty directory agents run in.

    It lives in the system temporary directory, outside the repository, is
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
    if _inside(resolved, REPO.resolve()):
        raise RuntimeError(f"agent sandbox {resolved} is inside the repository {REPO}")
    return resolved


def _check_allowed(allowed: Iterable[str]) -> list[str]:
    out = []
    for rule in allowed:
        if not isinstance(rule, str) or not rule:
            raise ValueError(f"allowed tool rule must be a non-empty string: {rule!r}")
        if rule in _UNSCOPED_BUILTINS:
            raise ValueError(
                f"allowed_tools may not approve {rule!r} for every input; "
                "list it in tools and scope any approval, e.g. 'Bash(python3 /abs/cli.py:*)'")
        out.append(rule)
    return out


def sandboxed_options(*, model: str | None, system_prompt: str, tools: Iterable[str],
                      max_turns: int, cwd: str | Path | None = None,
                      allowed_tools: Iterable[str] = (),
                      mcp_servers: dict | None = None) -> Any:
    """Build the ClaudeAgentOptions for one sandboxed agent call.

    ``tools`` lists built-in tool names (``[]`` for a judgment-only agent).
    ``allowed_tools`` lists MCP tool names and scoped permission rules that run
    without a prompt. ``cwd`` is the only directory the read tools may see;
    ``None`` selects :func:`sandbox_dir`.
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
        if name.startswith("mcp__") or "(" in name:
            raise ValueError(f"tools lists built-in tool names only, not {name!r}")
    allowed = _check_allowed(allowed_tools)
    if any(rule.startswith("mcp__") for rule in allowed) and TOOL_SEARCH not in base:
        base.append(TOOL_SEARCH)
    if TOOL_SEARCH in base and TOOL_SEARCH not in allowed:
        allowed.append(TOOL_SEARCH)
    denied = [name for name in DANGEROUS_TOOLS if name not in base]

    workdir = sandbox_dir() if cwd is None else Path(cwd).resolve()
    if _inside(workdir, REPO.resolve()):
        raise ValueError(
            f"agent cwd {workdir} is inside the repository, which holds wiki/.dev.vars; "
            "pass the paths an agent needs in its prompt or use the sandbox")

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
