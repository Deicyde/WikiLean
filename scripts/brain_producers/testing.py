"""Fixture inputs for the declaration producers' offline tests (`fixtures/`, local git only)."""

from __future__ import annotations

import json
import os
import shutil

from scripts.tauceti_plans.build import URLS
from scripts.tauceti_plans.test_build import _git_repo

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
TAUCETI_REV = "cd41bffcb343151b3e58c89392e206b11fe78c1c"
MATHLIB_REV = "15cf10d19dfd4cdee9602318e271fa3ce99c454d"


def make_inputs(root: str, docs: dict | None = None, fetched_at: str = "2026-11-18T00:00:00Z") -> str:
    """trough as a one-commit git repo, both `.bmp`s, and a PINS.json whose entries carry
    `docs` ({input name: docs_commit})."""
    inputs = os.path.join(root, "inputs")
    os.makedirs(inputs)
    _git_repo(os.path.join(FIX, "trough"), os.path.join(inputs, "trough"))
    shutil.copytree(os.path.join(FIX, "decls"), os.path.join(inputs, "decls"))
    write_pins(inputs, docs or {}, fetched_at)
    return inputs


def write_pins(inputs: str, docs: dict, fetched_at: str, extra: dict | None = None) -> None:
    pins = {k: {"fetched_at": fetched_at} for k in URLS}
    for k, v in docs.items():
        pins[k]["docs_commit"] = v
    for k, v in (extra or {}).items():
        pins[k].update(v)
    with open(os.path.join(inputs, "PINS.json"), "w") as f:
        json.dump({"inputs": pins}, f)


def read_rows(out: str, name: str) -> list[dict]:
    with open(os.path.join(out, name), encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def read_tree(out: str) -> dict[str, bytes]:
    blobs = {}
    for name in sorted(os.listdir(out)):
        with open(os.path.join(out, name), "rb") as f:
            blobs[name] = f.read()
    return blobs
