#!/usr/bin/env python3
"""Validate experimental assertion history without accepting Git/D1 authority."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay_authority as replay


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--prefix-ledger", type=Path)
    args = parser.parse_args(argv)
    try:
        result = replay.run(args.ledger, args.prefix_ledger)
        document = replay.kernel.result_document(result)
        report = {"schema": "wikilean.experimental-assertion-validation/v1", "valid": True, "authority": False,
            **{k: document[k] for k in ("state_root", "chain_root", "counts")}}
        sys.stdout.buffer.write(replay.kernel.canonical(report) + b"\n")
    except (ValueError, OSError) as exc:
        print("Experimental assertion validation failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
