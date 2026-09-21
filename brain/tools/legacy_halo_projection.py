#!/usr/bin/env python3
"""Prepare the old frontier's halo input from exact baseline cells and synapses.

This executes five hash-pinned legacy pure functions. It does not run the old
halo CLI, source acquisition, or a Brain build, and grants no baseline approval.
"""
from __future__ import annotations

import __future__
import argparse
import ast
import builtins
import hashlib
import json
import os
import re
import stat
import sys
from collections import defaultdict
from pathlib import Path

LEGACY_COMMIT = "ebac34dc1d07b66ce97692c31a914a084328f5df"
PROGRAM_SHA256 = "f83757dc8db0675d651dacd9d5625a9d1bf25b2ca141de4965be5039a52a037e"
FUNCTIONS = frozenset({"_jsonl", "load_brain", "load_adjacency", "cell_qid", "build_ring"})
MAX_INPUT_BYTES = 512 * 1024 * 1024


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def project(program, cells, synapses):
    require(sha(program) == PROGRAM_SHA256, "legacy halo program differs from the reviewed exact commit")
    require(len(cells) <= MAX_INPUT_BYTES and len(synapses) <= MAX_INPUT_BYTES, "baseline input exceeds bound")

    class CapturedInput:
        def __init__(self, raw):
            self.raw = raw

        def read_text(self):
            return self.raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")

    tree = ast.parse(program.decode("utf-8"), filename="manage/halo.py")
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in FUNCTIONS]
    constants = [n for n in tree.body if isinstance(n, ast.Assign) and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "_QID_RE"]
    require(len(functions) == len(FUNCTIONS) and {n.name for n in functions} == FUNCTIONS and len(constants) == 1,
        "legacy halo function/constant boundary differs")
    selected = ast.Module(body=[*constants, *functions], type_ignores=[])
    require(not any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(selected)), "ambient import in halo slice")
    namespace = {"__builtins__": {k: v for k, v in vars(builtins).items()
        if k not in {"open", "eval", "exec", "compile", "__import__", "input", "breakpoint"}},
        "json": json, "re": re, "defaultdict": defaultdict,
        "CELLS": CapturedInput(cells), "SYNAPSES": CapturedInput(synapses)}
    exec(compile(selected, "manage/halo.py", "exec", flags=__future__.annotations.compiler_flag), namespace)
    loaded, _ = namespace["load_brain"]()
    all_neighbors, dep_neighbors, count = namespace["load_adjacency"]()
    rows = namespace["build_ring"](loaded, all_neighbors, dep_neighbors, {})
    # Preserve the old algorithm's complete rows. The old frontier consumes only
    # items[].cell and non-null items[].all_frac, without consulting rank.
    raw = json.dumps({"items": rows}, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
    report = {"schema": "wikilean.legacy-halo-preparation/v1", "authority": False,
        "baseline_approved": False, "legacy_commit": LEGACY_COMMIT, "program_sha256": PROGRAM_SHA256,
        "inputs": {name: {"sha256": sha(data), "bytes": len(data)} for name, data in (("cells", cells), ("synapses", synapses))},
        "centrality": "explicitly empty; frontier cell-to-all_frac is independent; ranking and centrality diagnostic fields use this map",
        "counts": {"cells": len(loaded), "cell_cell_synapses": count, "ring_rows": len(rows)},
        "projection": {"sha256": sha(raw), "bytes": len(raw)}}
    return raw, report


def read_exact(path, expected, maximum):
    require(re.fullmatch(r"[a-f0-9]{64}", expected) is not None, "invalid expected input SHA-256")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_size <= maximum, "input must be a bounded regular file")
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
        (after.st_size, after.st_mtime_ns, after.st_ctime_ns), "input changed while reading")
    require(len(raw) <= maximum and sha(raw) == expected, "input bytes differ from expected SHA-256")
    return raw


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--program", type=Path, required=True)
    for name in ("cells", "synapses"):
        parser.add_argument("--" + name, type=Path, required=True)
        parser.add_argument("--" + name + "-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        program = read_exact(args.program, PROGRAM_SHA256, 1024 * 1024)
        raw, report = project(program, read_exact(args.cells, args.cells_sha256, MAX_INPUT_BYTES),
            read_exact(args.synapses, args.synapses_sha256, MAX_INPUT_BYTES))
        sys.stdout.buffer.write(raw)
        print(json.dumps(report, sort_keys=True, separators=(",", ":")), file=sys.stderr)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print("Legacy halo preparation failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
