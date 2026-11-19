"""Acquisition for this producer: trough with data/mathlib/, and decls/mathlib.bmp (network).
See scripts/brain_producers/fetch_decls.py; the inputs directory may be shared with producer 1."""

from __future__ import annotations

from scripts.brain_producers.fetch_decls import FetchError, fetch as _fetch

__all__ = ["FetchError", "fetch"]


def fetch(inputs: str, pins_file: str | None = None, opener=None) -> dict:
    return _fetch(inputs, "mathlib_decls", "mathlib", pins_file, opener)
