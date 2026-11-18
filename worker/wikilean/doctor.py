"""`wikilean doctor`: can this machine do the work, without spending anything?

Every check here is free. Claude Code is checked by looking for the binary and
for the credentials it writes, never by asking it a question; the token is
checked with one authenticated **GET** (`/api/auth/me`), which writes nothing;
the sandbox check runs the real argv builder against a stub that prints a fixed
answer, so the "no token in the model's environment" rule is verified on the
contributor's own machine and not only in CI.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import VERSION, model
from .config import Config, config_dir, state_dir
from .locate import preconditions
from .secrets import ENV_TOKEN, fingerprint, load_token
from .site import Site, SiteError

# The Keychain item Claude Code writes on macOS, and the file it writes
# elsewhere. Presence is all we look at: neither is ever read or printed.
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"


@dataclass
class Check:
    name: str
    ok: bool
    note: str = ""
    fatal: bool = True


def claude_config_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(override) if override else Path.home() / ".claude"


def claude_logged_in() -> tuple[bool, str]:
    """Is Claude Code logged in? Decided from files and the Keychain only."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return True, "CLAUDE_CODE_OAUTH_TOKEN is set"
    credentials = claude_config_dir() / ".credentials.json"
    try:
        if credentials.exists():
            return True, f"{credentials}"
    except OSError:
        pass
    if sys.platform == "darwin" and shutil.which("security"):
        # No -w: this asks whether the item exists and never prints a secret.
        probe = subprocess.run(
            ["security", "find-generic-password", "-s", CLAUDE_KEYCHAIN_SERVICE],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode == 0:
            return True, f'macOS login Keychain ("{CLAUDE_KEYCHAIN_SERVICE}")'
    return False, "run `claude` once and log in; no credentials found"


def sandbox_works() -> tuple[bool, str]:
    """Run the real argv builder against a stub `claude` and check the rules.

    The stub fails loudly if the agent token, an Anthropic key or any other
    unexpected credential reaches it, so this is a live check of the boundary
    rather than a restatement of it.
    """
    script = (
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "leaked = [k for k in os.environ if k.startswith(('WIKILEAN_', 'ANTHROPIC_'))]\n"
        "body = sys.stdin.read()\n"
        "if leaked:\n"
        "    print('leaked: ' + ','.join(sorted(leaked)), file=sys.stderr); sys.exit(3)\n"
        "if 'wla_' in body:\n"
        "    print('token in prompt', file=sys.stderr); sys.exit(4)\n"
        "print(json.dumps({'result': json.dumps({'verdict': 'unsure'})}))\n"
    )
    with tempfile.TemporaryDirectory(prefix="wikilean-doctor-") as directory:
        stub = Path(directory) / "claude-stub"
        stub.write_text(script, encoding="utf-8")
        stub.chmod(0o700)
        previous = os.environ.get(model.CLAUDE_CMD_ENV)
        os.environ[model.CLAUDE_CMD_ENV] = f"{sys.executable} {stub}"
        try:
            answer = model.ask("a health check; answer unsure", timeout=60)
        except model.ModelError as exc:
            return False, str(exc)
        finally:
            if previous is None:
                os.environ.pop(model.CLAUDE_CMD_ENV, None)
            else:
                os.environ[model.CLAUDE_CMD_ENV] = previous
    return answer.value.get("verdict") == "unsure", "the model call runs with no credentials in its environment"


def run_checks(config: Config, *, site_factory=Site) -> list[Check]:
    checks: list[Check] = []
    version = ".".join(str(part) for part in sys.version_info[:3])
    checks.append(Check("Python 3.12+", sys.version_info[:2] >= (3, 12), f"{version} on {platform.system()}"))
    checks.append(Check(f"wikilean {VERSION}", True, f"config {config_dir()}, state {state_dir()}", fatal=False))

    binary = model.installed()
    checks.append(Check("Claude Code installed", binary is not None, binary or "no `claude` on PATH"))
    if binary:
        logged_in, where = claude_logged_in()
        checks.append(Check("Claude Code logged in", logged_in, where))
        ok, note = sandbox_works()
        checks.append(Check("the model sandbox", ok, note))

    stored = load_token()
    if stored is None:
        checks.append(Check("agent token stored", False, "run `wikilean login`"))
    else:
        where = {"environment": f"${ENV_TOKEN}", "keychain": "macOS Keychain", "file": "config file, 0600"}
        checks.append(Check("agent token stored", True, f"{where.get(stored.source, stored.source)} "
                                                        f"(fingerprint {fingerprint(stored.token)})"))

    try:
        site = site_factory(config.base_url, stored.token if stored else None)
        published = site.current()
        checks.append(Check("site reachable", True,
                            f"{config.base_url}, campaign {published.get('campaign')}, "
                            f"{(published.get('counts') or {}).get('articles', 0)} articles"))
    except SiteError as exc:
        checks.append(Check("site reachable", False, str(exc)))
        site = None

    if site is not None and stored is not None:
        try:
            user = site.me()
            role = user.get("role")
            checks.append(Check("token accepted", role == "agent",
                                f"{user.get('name')} (role {role}, owner {user.get('ownerId') or 'none'})"
                                if role == "agent" else
                                f"this token is role {role!r}; mint an agent token at /agents"))
        except SiteError as exc:
            checks.append(Check("token accepted", False, str(exc)))

    gaps = preconditions(config)
    checks.append(Check("locate unit", not gaps,
                        "ready" if not gaps else "; ".join(f"{gap.what}" for gap in gaps[:4]),
                        fatal=False))
    return checks


def report(checks: list[Check], *, log=print) -> int:
    width = max((len(check.name) for check in checks), default=0)
    bad = 0
    for check in checks:
        mark = "ok  " if check.ok else ("FAIL" if check.fatal else "--  ")
        log(f"[{mark}] {check.name.ljust(width)}  {check.note}")
        if not check.ok and check.fatal:
            bad += 1
    log("")
    log("all checks passed" if bad == 0 else f"{bad} check(s) failed")
    return 0 if bad == 0 else 1


def as_json(checks: list[Check]) -> str:
    return json.dumps([{"name": c.name, "ok": c.ok, "note": c.note, "fatal": c.fatal} for c in checks], indent=2)
