#!/usr/bin/env python3
"""Stage 1: Literature Links, the one format both repositories vendor.

docs/PIPELINE.md sections 3 (stage 1) and 6. Every record this pipeline writes is
validated against the vendored release before it is sealed, and a sealed set's export
report records which release passed it.

WikiLean already vendors the release and already has a checker over it; this module is
the pipeline's front door to that pair, so the repository has one checker and one pin:

* the pin is ``wiki/src/vendor/literature-links/provenance.json`` (tag v1.0.1, commit
  789af9af, the schema's sha256);
* the schema's bytes are checked against that file before anything is validated, so a
  vendored copy that drifted from its provenance refuses rather than passing records
  against an unknown schema;
* the checker is ``site/native_annotations.validate_link_record``, which validates the
  record against the vendored JSON schema and the two numeric invariants of the format.

It does **not** reach into formal-companion's ``vendor/literature_links``. The two are
the same release at the same commit today, which is why batch 1 reproduces; keeping the
pin here is what makes that a fact rather than an assumption.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "wiki" / "src" / "vendor" / "literature-links"
PROVENANCE = VENDOR / "provenance.json"
SCHEMA = VENDOR / "companion.schema.json"

#: What the pin must say. An upgrade of the vendored release changes these and
#: scripts/pipeline/test_links.py with them, in one deliberate change; a pipeline change
#: never moves them as a side effect (docs/PIPELINE.md section 6, rule 4).
PINNED_TAG = "v1.0.1"
PINNED_VERSION = "1.0.1"
PINNED_COMMIT = "789af9af85ae7590d7149cb8312f09cfac0752b4"


class LinksProblem(RuntimeError):
    """The vendored Literature Links release is not the one this pipeline is pinned to."""


_CHECKER: tuple[Any, type] | None = None


def pin() -> dict:
    """The vendored release, with the schema's bytes checked against its provenance.

    Returns the provenance object. Raises :class:`LinksProblem` when the vendored schema
    is not the one ``provenance.json`` names, or when the provenance is not the release
    this pipeline is pinned to.
    """
    try:
        provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LinksProblem(f"the vendored Literature Links provenance is unreadable: {exc}") from exc
    except ValueError as exc:
        raise LinksProblem(f"the vendored Literature Links provenance is not JSON: {exc}") from exc
    digest = hashlib.sha256(SCHEMA.read_bytes()).hexdigest()
    if digest != provenance.get("sha256"):
        raise LinksProblem("the vendored Literature Links schema does not match its provenance.json sha256")
    expected = {"tag": PINNED_TAG, "version": PINNED_VERSION, "commit": PINNED_COMMIT}
    wrong = {key: provenance.get(key) for key, value in expected.items() if provenance.get(key) != value}
    if wrong:
        raise LinksProblem(
            f"the vendored Literature Links release is {wrong}, not the pinned "
            f"{expected} (docs/PIPELINE.md section 6: upgrading the pin is its own change)")
    return provenance


def _checker() -> tuple[Any, type]:
    """``(validate_link_record, NativeAnnotationError)``, after :func:`pin` has checked the schema."""
    global _CHECKER
    if _CHECKER is None:
        import sys

        pin()
        sys.path.insert(0, str(ROOT / "site"))
        try:
            from native_annotations import NativeAnnotationError, validate_link_record
        finally:
            sys.path.pop(0)
        _CHECKER = (validate_link_record, NativeAnnotationError)
    return _CHECKER


def validate(record: Any) -> list[str]:
    """The vendored checker's problems with one record; empty when it is valid."""
    validate_link_record, error = _checker()
    try:
        validate_link_record(record)
    except error as exc:
        return [str(exc)]
    return []


def validate_all(records: Iterable[Any]) -> list[str]:
    """Every record's problems, each prefixed with the record's id."""
    out: list[str] = []
    for record in records:
        rid = record.get("id") if isinstance(record, dict) else None
        out.extend(f"{rid}: {problem}" for problem in validate(record))
    return out
