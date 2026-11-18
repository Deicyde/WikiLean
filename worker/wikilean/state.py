"""Small persistent state: where the walk is, what it has already judged, and
whether the site asked us to wait.

Three things live here that the journal cannot answer:

* **The cursor.** Without it a loop re-reads the same article every round.
* **What has been judged and produced no write.** A confirmed absence leaves
  no row anywhere — that is the honest outcome, since the overlay has no shape
  for "still nothing here" — so without a local note the next round would
  spend another model call on the same passage forever.
* **How often a passage got no usable answer** (a timeout, or a reply that is
  not the agreed JSON). After MAX_MODEL_FAILURES it is set aside like a judged
  passage, for the same reason.

The index cache is here too: the shards change when a build is published, not
between rounds, and re-reading them every round would spend the plane's
anonymous ``articles-meta`` budget for nothing.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import ensure_dir, state_dir, write_private

# A judged passage is not offered again for this long.
SEEN_TTL = 30 * 24 * 60 * 60
MAX_SEEN = 5000
INDEX_TTL = 60 * 60
# A passage that got no usable answer this many times is set aside as if judged.
MAX_MODEL_FAILURES = 2


def state_path() -> Path:
    return state_dir() / "state.json"


def index_path() -> Path:
    from .config import cache_dir

    return cache_dir() / "index.json"


def load() -> dict:
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save(value: dict) -> None:
    write_private(state_path(), json.dumps(value, indent=2, sort_keys=True) + "\n")


def mark_seen(key: str, *, now: float | None = None) -> None:
    now = time.time() if now is None else now
    data = load()
    seen = data.get("seen")
    seen = seen if isinstance(seen, dict) else {}
    seen[key] = int(now)
    if len(seen) > MAX_SEEN:
        for stale in sorted(seen, key=lambda k: seen[k])[: len(seen) - MAX_SEEN]:
            seen.pop(stale, None)
    data["seen"] = seen
    failures = data.get("failures")
    if isinstance(failures, dict):
        failures.pop(key, None)
    save(data)


def note_failure(key: str) -> int:
    """Count one call on ``key`` that got no usable answer; return the count.

    The count is dropped once the passage is set aside (:func:`mark_seen`), so
    only passages with a failure and no verdict yet are kept, never more than
    MAX_SEEN of them.
    """
    data = load()
    failures = data.get("failures")
    failures = failures if isinstance(failures, dict) else {}
    previous = failures.get(key)
    count = (previous if isinstance(previous, int) else 0) + 1
    if len(failures) >= MAX_SEEN and key not in failures:
        failures = {}
    failures[key] = count
    data["failures"] = failures
    save(data)
    return count


def seen_keys(*, now: float | None = None) -> set[str]:
    now = time.time() if now is None else now
    seen = load().get("seen")
    if not isinstance(seen, dict):
        return set()
    return {key for key, when in seen.items() if isinstance(when, int) and now - when < SEEN_TTL}


def cursor() -> int:
    value = load().get("cursor")
    return value if isinstance(value, int) and value >= 0 else 0


def set_cursor(value: int) -> None:
    data = load()
    data["cursor"] = max(0, int(value))
    save(data)


def pause_until(value: float | None = None) -> float:
    """Read, or set, the moment the site may be written to again."""
    data = load()
    if value is not None:
        data["pause_until"] = float(value)
        save(data)
        return float(value)
    current = data.get("pause_until")
    return float(current) if isinstance(current, (int, float)) else 0.0


def cached_index(now: float | None = None) -> list[dict] | None:
    now = time.time() if now is None else now
    try:
        raw = json.loads(index_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        return None
    fetched = raw.get("fetched_at")
    if not isinstance(fetched, (int, float)) or now - fetched > INDEX_TTL:
        return None
    return [entry for entry in raw["entries"] if isinstance(entry, dict)]


def cache_index(entries: list[dict], *, campaign: str = "", now: float | None = None) -> None:
    now = time.time() if now is None else now
    path = index_path()
    ensure_dir(path.parent)
    payload: dict[str, Any] = {"fetched_at": now, "campaign": campaign, "entries": entries}
    write_private(path, json.dumps(payload, sort_keys=True) + "\n")
