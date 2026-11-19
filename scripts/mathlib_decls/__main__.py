"""python3 -m scripts.mathlib_decls {fetch,build} ... (see docs/BRAIN-PRODUCERS.md)."""

import sys

from scripts.brain_producers.cli import main

if __name__ == "__main__":
    sys.exit(main("scripts.mathlib_decls"))
