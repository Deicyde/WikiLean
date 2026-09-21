#!/usr/bin/env python3
"""Replay only experimental assertion fixtures; emit a non-authoritative result."""
from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import assertion_kernel as kernel


def read_ledger(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        kernel.require(stat.S_ISREG(before.st_mode) and before.st_size <= kernel.MAX_DOCUMENT_BYTES,
            "ledger must be a bounded regular file")
        raw = stream.read(kernel.MAX_DOCUMENT_BYTES + 1)
        after = os.fstat(stream.fileno())
        kernel.require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
            (after.st_size, after.st_mtime_ns, after.st_ctime_ns), "ledger changed while reading")
    kernel.require(len(raw) <= kernel.MAX_DOCUMENT_BYTES, "ledger exceeds bound")
    return kernel.contracts.parse_artifact_json_bytes(raw, location="experimental ledger")


def run(path, prefix=None):
    previous = kernel.replay_ledger(read_ledger(prefix)) if prefix is not None else None
    return kernel.replay_ledger(read_ledger(path), initial=previous)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--prefix-ledger", type=Path)
    args = parser.parse_args(argv)
    try:
        result = run(args.ledger, args.prefix_ledger)
        sys.stdout.buffer.write(kernel.canonical(kernel.result_document(result)) + b"\n")
    except (ValueError, OSError) as exc:
        print("Experimental assertion replay failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
