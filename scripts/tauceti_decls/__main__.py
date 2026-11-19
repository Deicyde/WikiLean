"""python3 -m scripts.tauceti_decls {fetch,build} ... (see docs/BRAIN-PRODUCERS.md)."""

import sys

from scripts.brain_producers.cli import main

if __name__ == "__main__":
    sys.exit(main("scripts.tauceti_decls"))
