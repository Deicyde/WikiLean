"""Acquisition for this producer (the only networked step): `rh/` under --inputs, a shallow,
blob-filtered clone of leanprover-community/riemann-hypothesis with only `src/` checked out, at
the newest commit of its default branch or at the pinned one, and the `rh` entry of PINS.json.

The repository's history is large and its `src/` is about 60,000 files, so the clone holds one
commit (`--depth 1`) and the blobs of `src/` only. `build` then reads those blobs offline. As in
the other producers, this fetch replaces only its own PINS.json entry, and on any failure leaves
it out (every other input's entry is kept).
"""

from __future__ import annotations

import json
import os
import subprocess

from scripts.brain_producers.common import git
from scripts.brain_producers.fetch_decls import write_pins
from scripts.rh_decls.build import INPUT, SRC, URL
from scripts.tauceti_plans.build import read_pins_file
from scripts.tauceti_plans.fetch import FetchError, _now
from scripts.tauceti_plans.fetch import Opener as PlansOpener

__all__ = ["FetchError", "Opener", "fetch"]


class Opener(PlansOpener):
    """Producer 1's network, with a shallower clone: its `clone` fetches the whole history, which
    for this repository is large. Tests point it at a local `file://` repository."""

    def clone(self, url: str, dest: str, sparse: str | None = None, commit: str | None = None) -> None:
        """Clone (or update) `url` into `dest`, one commit deep, with only `sparse` checked out,
        and detach HEAD at `commit`, else at the newest commit of the default branch."""
        if not os.path.isdir(os.path.join(dest, ".git")):
            subprocess.run(["git", "clone", "--quiet", "--no-checkout", "--depth", "1",
                            "--filter=blob:none", "--sparse", url, dest], check=True)
            if sparse:
                git(dest, "sparse-checkout", "set", sparse)
        # The default branch named explicitly (producer 1's re-fetch fix), or the pinned commit
        # by its id, which GitHub serves for any commit reachable from a branch.
        git(dest, "fetch", "--quiet", "--depth", "1", "origin", commit or "HEAD")
        git(dest, "checkout", "--quiet", "--detach", commit or "FETCH_HEAD")


def fetch(inputs: str, pins_file: str | None = None, opener: Opener | None = None) -> dict:
    opener = opener or Opener()
    want = read_pins_file(pins_file) if pins_file else None
    os.makedirs(inputs, exist_ok=True)
    out_path = os.path.join(inputs, "PINS.json")
    kept: dict = {}
    if os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as f:
                kept = json.load(f).get("inputs") or {}
        except (OSError, ValueError, AttributeError):
            kept = {}
        if not isinstance(kept, dict):  # unreadable, as above: nothing in it can be kept
            kept = {}
    others = {k: v for k, v in kept.items() if k != INPUT}
    if kept:
        write_pins(out_path, others)

    commit = (want.get(INPUT) or {}).get("commit") if want is not None else None
    if want is not None and not commit:
        raise FetchError(f"{INPUT}: the pins name no commit")
    dest = os.path.join(inputs, INPUT)
    try:
        opener.clone(URL, dest, SRC.rstrip("/"), commit)
        head = git(dest, "rev-parse", "HEAD").decode().strip()
        has_src = bool(git(dest, "ls-tree", "--name-only", "HEAD", SRC.rstrip("/")).strip())
    except (subprocess.CalledProcessError, OSError) as e:
        raise FetchError(f"{INPUT}: could not check out {commit or 'the newest commit'} ({e})") from None
    if commit and head != commit:
        raise FetchError(f"{INPUT}: checked out {head}, pinned {commit}")
    if not has_src:
        raise FetchError(f"{INPUT}: {SRC} missing at {head}")
    pins = {INPUT: {"commit": head, "fetched_at": _now()}}
    write_pins(out_path, others | pins)
    return pins
