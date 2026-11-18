"""The contributor's WikiLean agent token: where it is kept, and the rules.

The token is minted once at https://wikilean.jackmccarthy.org/agents and is the
only credential this CLI holds. Three rules, enforced here and tested:

1. It is never a command-line argument — not ours, and not a child process's.
   `security find-generic-password` is given the service and account names
   only; the token travels on that child's stdout, which we capture.
2. It is never printed. `wikilean login` echoes nothing, and every other
   command shows at most the fingerprint below.
3. It never reaches the model. model.py builds the child's environment from an
   allowlist that cannot contain it; nothing here puts it in os.environ.

Two stores. On macOS the login Keychain, through `security`; everywhere else
(and on macOS when `security` refuses) a file in the config directory, created
0600 inside a 0700 directory.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import config_dir, ensure_dir, write_private

# The Keychain item. The account is a fixed string rather than the login name:
# one WikiLean token per machine, found the same way on every machine.
KEYCHAIN_SERVICE = "wikilean-agent-token"
KEYCHAIN_ACCOUNT = "wikilean"

# auth.ts AGENT_TOKEN_RE, verbatim: "wla_" and 43 more base64url characters.
TOKEN_PREFIX = "wla_"
TOKEN_LENGTH = 4 + 43

ENV_TOKEN = "WIKILEAN_AGENT_TOKEN"


class TokenError(Exception):
    """The token is missing, malformed, or could not be stored."""


def valid_token(token: str) -> bool:
    if len(token) != TOKEN_LENGTH or not token.startswith(TOKEN_PREFIX):
        return False
    body = token[len(TOKEN_PREFIX):]
    return all(ch.isascii() and (ch.isalnum() or ch in "_-") for ch in body)


def fingerprint(token: str) -> str:
    """A short, non-reversible label for a token, safe to print and journal."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def token_file() -> Path:
    return config_dir() / "agent-token"


def _security() -> str | None:
    """The macOS `security` binary, or None when this is not a Mac with one."""
    if sys.platform != "darwin":
        return None
    for candidate in ("/usr/bin/security", "security"):
        try:
            probe = subprocess.run([candidate, "help"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        # `security help` prints its usage on stderr and exits non-zero; the
        # question is only whether the binary ran at all.
        if probe.returncode is not None:
            return candidate
    return None


@dataclass(frozen=True)
class Stored:
    token: str
    # "keychain", "file" or "environment".
    source: str


def _keychain_read(security: str) -> str | None:
    try:
        found = subprocess.run(
            [security, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if found.returncode != 0:
        return None
    return found.stdout.strip() or None


def _keychain_write(security: str, token: str) -> None:
    """Store the token, passing it on the child's stdin and never on its argv.

    `security add-generic-password -w` with no value after it reads the
    password from the terminal, or from standard input when that is a pipe.
    """
    done = subprocess.run(
        [security, "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT,
         "-D", "WikiLean agent token", "-w"],
        input=token + "\n", capture_output=True, text=True, timeout=60,
    )
    if done.returncode != 0:
        raise TokenError((done.stderr or done.stdout).strip()[:200] or "security refused the item")


def _keychain_delete(security: str) -> bool:
    try:
        done = subprocess.run(
            [security, "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def load_token(*, allow_environment: bool = True) -> Stored | None:
    """The stored token: the environment, then the Keychain, then the file."""
    if allow_environment:
        value = os.environ.get(ENV_TOKEN, "").strip()
        if value:
            return Stored(value, "environment")
    security = _security()
    if security:
        found = _keychain_read(security)
        if found:
            return Stored(found, "keychain")
    try:
        text = token_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Stored(text, "file") if text else None


def store_token(token: str, *, prefer: str = "auto") -> str:
    """Save the token and return the store that took it.

    ``prefer`` is "auto" (Keychain on macOS, file elsewhere), "keychain" or
    "file". A Keychain refusal under "auto" falls back to the file and says so
    through the returned value; under "keychain" it raises.
    """
    if not valid_token(token):
        raise TokenError("that is not a WikiLean agent token (expected 'wla_' and 43 more characters)")
    if prefer not in ("auto", "keychain", "file"):
        raise TokenError(f"unknown store {prefer!r}")
    if prefer != "file":
        security = _security()
        if security:
            try:
                _keychain_write(security, token)
                return "keychain"
            except (TokenError, OSError, subprocess.SubprocessError) as exc:
                if prefer == "keychain":
                    raise TokenError(f"the Keychain refused the token: {exc}") from exc
        elif prefer == "keychain":
            raise TokenError("no macOS `security` command here; use --store file")
    ensure_dir(config_dir())
    write_private(token_file(), token + "\n")
    return "file"


def delete_token() -> list[str]:
    """Remove the token from every store, and name the ones it was in."""
    removed: list[str] = []
    security = _security()
    if security and _keychain_delete(security):
        removed.append("keychain")
    path = token_file()
    if path.exists():
        path.unlink()
        removed.append("file")
    return removed


def read_token_from(stream, *, prompt: str = "WikiLean agent token: ") -> str:
    """Read a token without echoing it and without putting it in argv.

    A terminal gets a hidden prompt; a pipe is read as one line, so
    ``printf '%s' "$token" | wikilean login --stdin`` works in a script.
    """
    if stream.isatty():
        import getpass

        return getpass.getpass(prompt).strip()
    return stream.readline().strip()
