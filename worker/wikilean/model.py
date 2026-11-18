"""The one non-deterministic step: the contributor's own `claude -p`.

The boundary this module draws, and why it is drawn here rather than at the
call sites:

* **The prompt carries untrusted text.** A passage is a quotation from a
  Wikipedia revision anybody may edit. So the child gets no tools at all
  (``--tools ""``), no project or user settings (``--setting-sources ""``), no
  MCP servers (``--strict-mcp-config``) and no session to carry anything into
  the next one. This is bot/triage.py's tool-less form, which is the shape
  WikiLean already requires of a pipeline `claude` call (CLAUDE.md, "Security /
  credentials"), and everything the model needs is in the prompt.
* **The child's environment is an allowlist, never a copy.** The WikiLean agent
  token cannot reach it, because no name that could hold one is on the list —
  and `ANTHROPIC_API_KEY` is off it too, so the call bills the contributor's
  subscription instead of failing silently (CLAUDE.md, "Max-auth gotcha").
* **The answer is data, not instruction.** :func:`ask` returns a parsed object
  and nothing else; the vocabulary checks live in review.py and tag.py, and a
  reply that fails them is skipped and journalled, never sent.

The prompt goes in on stdin, so a long passage cannot overflow argv and no part
of it is visible in ``ps``.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass

# bot/triage.py CLAUDE_ENV_KEYS, verbatim. Every name a `claude` call may need
# and nothing else: no API key, no GitHub token, no WIKILEAN_AGENT_TOKEN. This
# is an allowlist on purpose — a deny-list grows a hole every time somebody
# invents a new variable.
CLAUDE_ENV_KEYS: tuple[str, ...] = (
    "ALL_PROXY", "CI", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "CLAUDE_CODE_MAX_OUTPUT_CHARS", "CLAUDE_CODE_OAUTH_TOKEN",
    "GITHUB_ACTIONS", "HOME", "HTTP_PROXY", "HTTPS_PROXY", "LANG",
    "LC_ALL", "LC_CTYPE", "LOGNAME", "NODE_EXTRA_CA_CERTS", "NO_PROXY",
    "PATH", "RUNNER_TEMP", "SHELL", "SSL_CERT_DIR", "SSL_CERT_FILE",
    "TEMP", "TMP", "TMPDIR", "TZ", "USER", "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "XDG_STATE_HOME",
    "all_proxy", "http_proxy", "https_proxy", "no_proxy",
)

# The binary, overridable so a test can put a stub in its place.
CLAUDE_CMD_ENV = "WIKILEAN_CLAUDE_CMD"
# Named rather than written inline below, so that the only ["claude", …]
# literal in this package is the full argv in claude_argv — which is the one
# site/test_agent_sandbox.py scans for its flags.
CLAUDE_BINARY = "claude"


class ModelError(Exception):
    """The model was not asked, or did not answer in the agreed shape."""


@dataclass(frozen=True)
class Answer:
    value: dict
    # Everything the journal records about the call. No prompt text: a
    # quotation belongs to Wikipedia, and its digest is enough to tell two
    # rounds apart.
    model: str
    duration_ms: int


def claude_command() -> list[str]:
    raw = os.environ.get(CLAUDE_CMD_ENV, "").strip()
    return shlex.split(raw) if raw else [CLAUDE_BINARY]


def claude_environment(environ: dict | None = None) -> dict[str, str]:
    """The explicit environment the non-deterministic child may see."""
    source = os.environ if environ is None else environ
    return {name: source[name] for name in CLAUDE_ENV_KEYS if name in source}


def claude_argv(model: str = "") -> list[str]:
    """The argv for one tool-less, session-less, settings-less judgement.

    Written as a literal list beginning "claude" because that is what the
    repository's own gate reads: site/test_agent_sandbox.py scans every
    pipeline Python file for such a list and fails the build unless it carries
    --tools, --setting-sources and --strict-mcp-config. WIKILEAN_CLAUDE_CMD
    replaces the binary afterwards and can never replace a flag.
    """
    argv = [
        "claude",
        "-p",
        "--output-format", "json",
        "--safe-mode",
        "--tools", "",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    argv[:1] = claude_command()
    if model:
        argv += ["--model", model]
    return argv


def installed() -> str | None:
    """The `claude` binary's path, or None. Spends nothing."""
    command = claude_command()
    return shutil.which(command[0]) if command else None


def extract_json(text: str) -> dict:
    """The first JSON object in a reply, however much prose surrounds it."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ModelError("the reply contains no JSON object")
    try:
        parsed = json.loads(text[start:end + 1])
    except ValueError as exc:
        raise ModelError(f"the reply is not JSON: {exc}") from None
    if not isinstance(parsed, dict):
        raise ModelError("the reply is not a JSON object")
    return parsed


def ask(prompt: str, *, model: str = "", timeout: float = 300.0,
        runner=subprocess.run) -> Answer:
    """Ask once and parse the answer. Raises ModelError; never raises data."""
    import time

    argv = claude_argv(model)
    environment = claude_environment()
    started = time.monotonic()
    try:
        done = runner(argv, input=prompt, capture_output=True, text=True,
                      env=environment, timeout=timeout)
    except FileNotFoundError:
        raise ModelError("no `claude` on PATH: install Claude Code and log in") from None
    except subprocess.TimeoutExpired:
        raise ModelError(f"the model did not answer within {timeout:.0f}s") from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise ModelError(f"could not run `claude`: {type(exc).__name__}") from None
    elapsed = int((time.monotonic() - started) * 1000)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise ModelError(f"`claude` exited {done.returncode}: {detail[-1][:200] if detail else 'no output'}")
    body = (done.stdout or "").strip()
    if not body:
        raise ModelError("`claude` printed nothing (is it logged in? run `wikilean doctor`)")
    # --output-format json wraps the reply: {"result": "...", ...}. A plain
    # reply is accepted too, so a different Claude Code build still works.
    named = model
    try:
        envelope = json.loads(body)
        if isinstance(envelope, dict) and envelope.get("is_error"):
            raise ModelError("the model reported an error result")
        text = envelope.get("result", body) if isinstance(envelope, dict) else body
        # The envelope names the model that actually answered; that is what the
        # research export wants, and it beats guessing. Empty when the build
        # does not say, and then the tag simply carries no model.
        if isinstance(envelope, dict) and isinstance(envelope.get("model"), str):
            named = envelope["model"]
    except ValueError:
        text = body
    if not isinstance(text, str):
        text = json.dumps(text)
    return Answer(extract_json(text), named, elapsed)
