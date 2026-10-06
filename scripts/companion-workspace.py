#!/usr/bin/env python3
"""Local setup entry point; the workspace launcher pins this checkout and Python."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from brain.companion_workspace import main

raise SystemExit(main())
