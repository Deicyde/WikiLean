"""The local journal: one JSON object a line, append only, 0600.

Every round the CLI runs and every request it sends is written to
``~/.local/state/wikilean/journal.jsonl`` before anything else happens, so a
contributor can always answer "what did this thing send, and when". It is
local: nothing uploads it.

The journal is also the one place a token could plausibly leak into, so every
value written goes through :func:`redact` first, and a test holds that line.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterator

from .config import ensure_dir, state_dir

# Anything shaped like an agent token, wherever it turns up in a value.
TOKEN_RE = re.compile(r"wla_[A-Za-z0-9_-]{20,}")
BEARER_RE = re.compile(r"(?i)bearer\s+\S+")
REDACTED = "[redacted]"

MAX_STRING = 2000


def journal_path() -> Path:
    return state_dir() / "journal.jsonl"


def redact(value: Any) -> Any:
    """Strip anything token-shaped out of a value about to be written down."""
    if isinstance(value, str):
        out = BEARER_RE.sub(f"Bearer {REDACTED}", TOKEN_RE.sub(REDACTED, value))
        return out if len(out) <= MAX_STRING else out[:MAX_STRING] + "…"
    if isinstance(value, dict):
        return {str(k): (REDACTED if _secret_key(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value))


def _secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(word in lowered for word in ("authorization", "token", "secret", "password", "cookie"))


def write(event: str, **fields: Any) -> dict:
    """Append one entry and return it."""
    entry = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
    entry.update({key: redact(value) for key, value in fields.items()})
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    path = journal_path()
    ensure_dir(path.parent)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(handle, "a", encoding="utf-8") as stream:
        stream.write(line + "\n")
    return entry


def read(limit: int = 20, *, event: str | None = None) -> list[dict]:
    """The last ``limit`` entries, oldest first; a corrupt line is skipped."""
    return list(_tail(_entries(), limit, event))


def _entries() -> Iterator[dict]:
    try:
        with journal_path().open("r", encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    yield parsed
    except OSError:
        return


def _tail(entries: Iterator[dict], limit: int, event: str | None) -> list[dict]:
    kept: list[dict] = []
    for entry in entries:
        if event is not None and entry.get("event") != event:
            continue
        kept.append(entry)
        if len(kept) > limit:
            kept.pop(0)
    return kept
