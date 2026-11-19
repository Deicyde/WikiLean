"""Acquisition for the declaration producers: the only step that uses the network.

Each producer needs trough (a sparse checkout holding `data/<lib>/`) and one doc-gen4 `.bmp`,
in the layout producer 1 uses, so all three may share one inputs directory:
  trough/                sparse clone of Deicyde/trough; `fetch` adds data/<lib> to its sparse set
  decls/<lib>.bmp        doc-gen4 declaration-data.bmp (JSON)
  PINS.json              shared: this fetch replaces its own entries (trough, <lib>_decls) and
                         keeps every other input's, on failure as well as on success

Without pins it fetches the newest state (trough's default branch, named explicitly: producer
1's re-fetch fix). With pins (a PINS.json or PINS.txt) it checks trough out at the pinned commit
and checks the `.bmp`'s sha256 and docs commit; on any mismatch it raises FetchError naming the
input, never writes the mismatched bytes, and leaves this producer's inputs unpinned — PINS.json
keeps only the entries this run never touched, and is removed when none are left. Every network
call goes through one opener (producer 1's `Opener`, plus `ensure_sparse`), so tests inject a fake.
"""

from __future__ import annotations

import json
import os
import subprocess

from scripts.brain_producers.common import git, sha256_bytes
from scripts.tauceti_plans.build import URLS, read_pins_file
from scripts.tauceti_plans.fetch import FetchError, Opener, _now, docs_commit

PINS_SCHEMA = "wikilean.brain.plans.pins/v1"
__all__ = ["DeclsOpener", "FetchError", "fetch", "write_pins"]


class DeclsOpener(Opener):
    """Producer 1's opener, plus widening an existing sparse checkout (which fetches blobs)."""

    def ensure_sparse(self, dest: str, path: str) -> None:
        sparse = subprocess.run(["git", "-C", dest, "config", "--get", "core.sparseCheckout"],
                                capture_output=True, text=True).stdout.strip()
        if sparse != "true":
            return  # a full checkout already holds every path
        listed = git(dest, "sparse-checkout", "list").decode("utf-8").split("\n")
        if path not in listed:
            git(dest, "sparse-checkout", "add", path)


def write_pins(out_path: str, inputs: dict) -> None:
    """Write PINS.json, or remove it when nothing is left pinned."""
    if not inputs:
        if os.path.exists(out_path):
            os.remove(out_path)
        return
    with open(out_path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"schema": PINS_SCHEMA, "inputs": inputs}, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(out_path + ".tmp", out_path)


def fetch(inputs: str, name: str, lib: str, pins_file: str | None = None,
          opener: Opener | None = None) -> dict:
    """Fetch trough (with data/<lib>) and `decls/<lib>.bmp` for input `name` (`tauceti_decls`
    or `mathlib_decls`); returns this fetch's pins and merges them into DIR/PINS.json."""
    opener = opener or DeclsOpener()
    want = read_pins_file(pins_file) if pins_file else None
    out_path = os.path.join(inputs, "PINS.json")
    kept: dict = {}
    if os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as f:
                kept = json.load(f).get("inputs") or {}
        except (OSError, ValueError, AttributeError):
            kept = {}
    # A failed fetch must leave none of its own two inputs pinned, and must not take another
    # producer's pins down with it: drop only `trough` and `<lib>_decls`, now, and merge below.
    others = {k: v for k, v in kept.items() if k not in ("trough", name)}
    if kept:
        write_pins(out_path, others)
    os.makedirs(os.path.join(inputs, "decls"), exist_ok=True)

    def pinned(input_name, key):
        return (want or {}).get(input_name, {}).get(key) if want is not None else None

    pins: dict = {}
    dest = os.path.join(inputs, "trough")
    sparse = f"data/{lib}"
    commit = pinned("trough", "commit")
    if want is not None and not commit:
        raise FetchError("trough: the pins name no commit")
    try:
        opener.clone(URLS["trough"], dest, sparse, commit)
        if hasattr(opener, "ensure_sparse"):
            opener.ensure_sparse(dest, sparse)
    except (subprocess.CalledProcessError, OSError) as e:
        raise FetchError(f"trough: could not check out {commit or 'the newest commit'} ({e})") from None
    head = git(dest, "rev-parse", "HEAD").decode().strip()
    if commit and head != commit:
        raise FetchError(f"trough: checked out {head}, pinned {commit}")
    if not git(dest, "ls-tree", "--name-only", "HEAD", f"{sparse}/source.json").strip():  # trees only
        raise FetchError(f"trough: {sparse}/source.json missing at {head}")
    pins["trough"] = {"commit": head, "fetched_at": _now()}

    data = opener.get(URLS[name])
    try:
        bmp = json.loads(data)  # doc-gen4's .bmp is JSON; refuse anything else
    except ValueError as e:
        raise FetchError(f"{name}: not JSON ({e})") from None
    sha = sha256_bytes(data)
    if want is not None and pinned(name, "sha256") != sha:
        raise FetchError(f"{name}: sha256 {sha} does not match pinned {pinned(name, 'sha256')}")
    docs = docs_commit(opener, name, URLS[name], bmp)
    expect = pinned(name, "docs_commit")
    if expect and docs != expect:
        raise FetchError(f"{name}: docs commit {docs} does not match pinned {expect}")
    path = os.path.join(inputs, "decls", f"{lib}.bmp")
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)
    pins[name] = {"sha256": sha, "docs_commit": docs, "fetched_at": _now()}

    write_pins(out_path, others | pins)
    return pins
