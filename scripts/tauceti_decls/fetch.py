"""Acquisition for this producer: trough with data/tauceti/, and decls/tauceti.bmp (network).
See scripts/brain_producers/fetch_decls.py; the inputs directory may be shared with producer 1."""

from __future__ import annotations

from scripts.brain_producers.fetch_decls import FetchError, fetch as _fetch

__all__ = ["FetchError", "fetch"]


def fetch(inputs: str, pins_file: str | None = None, opener=None) -> dict:
    return _fetch(inputs, "tauceti_decls", "tauceti", pins_file, opener)
