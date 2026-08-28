#!/usr/bin/env python3
"""Compatibility alias for the unified BRAIN snapshot build.

Nodes and edges are one logical graph generation. Building either artifact alone
can leave consumers with a mixed snapshot, so this command now rebuilds both
JSONL streams and the generated SQLite index in one pass.
"""
from __future__ import annotations

from build_snapshot import main


if __name__ == "__main__":
    raise SystemExit(main())
