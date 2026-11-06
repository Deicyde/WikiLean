#!/usr/bin/env python3
"""A harvested run's corpus directory: which files are documents, and the receipt.

docs/PIPELINE.md section 3 (stages 8 and 10). A run's harvest lands under
``corpus/<run-id>/`` as ``<task-id>/<source-id>.json``, one document per task and
source, beside the ``RECEIPT.json`` the harvest wrote. This module is the one
definition of what that directory holds, so the harvest that writes the receipt
(stage 10) and the tally that reads it (stage 8) never disagree on which files are
documents — a disagreement there is a corpus silently a file short, which is the
oldest way for a count to come out "cleaner" than the truth.

Ported from ``harvest/files.py`` (:data:`NON_FINDING_FILES`, the listing rule) and
``locate/inputs.py`` (``harvested_documents``, ``receipt``, ``require_gated``).
Standard library only, and no workspace object: a run directory is a path.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import vocab  # noqa: E402

#: The harvest's own receipt, named in full so nothing guesses at it.
RECEIPT_NAME = "RECEIPT.json"
#: Files that live beside the documents in a corpus directory and are not documents.
#: The gate carries an identical literal, because it runs where nothing is importable;
#: the two move together, or a re-gate of a harvested corpus disagrees with its receipt.
NON_DOCUMENT_FILES = frozenset({
    RECEIPT_NAME, "run.yaml", "run.json", "manifest.json", "tally.json",
    "journal-harvest.json", "gate.json", "gate.local.json", "queue_remaining.json"})


class CorpusProblem(ValueError):
    """A corpus directory that cannot be read as one, named rather than skipped."""


def is_document_name(rel: str) -> bool:
    """Is this posix path, relative to a corpus directory, a harvested document?

    ``*.json``, not one of :data:`NON_DOCUMENT_FILES` by basename at any depth, and no
    path component hidden behind a dot.
    """
    parts = [part for part in str(rel).split("/") if part]
    return (bool(parts) and parts[-1].endswith(".json")
            and parts[-1] not in NON_DOCUMENT_FILES
            and not any(part.startswith(".") for part in parts))


def document_name(path: Path, directory: Path) -> str:
    """A document's name in a receipt: its posix path relative to the corpus."""
    return Path(path).relative_to(Path(directory)).as_posix()


def document_files(directory: Path) -> list[Path]:
    """The corpus's documents, sorted by relative path; regular files only.

    A symlink is left out: the harvest's remote listing leaves links out too, and
    following one here counts a document twice, or a file from outside the corpus once.
    """
    root = Path(directory)
    if not root.is_dir():
        return []
    found: list[tuple[str, Path]] = []
    for here, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if not name.startswith("."))
        for name in files:
            path = Path(here) / name
            rel = document_name(path, root)
            if is_document_name(rel) and path.is_file() and not path.is_symlink():
                found.append((rel, path))
    return [path for _rel, path in sorted(found)]


def documents(directory: Path) -> list[tuple[str, dict]]:
    """``[(relative path, document)]`` of a harvested run, in listing order."""
    root = Path(directory)
    out: list[tuple[str, dict]] = []
    for path in document_files(root):
        rel = document_name(path, root)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CorpusProblem(f"{rel} in {root} is not readable JSON ({exc})") from None
        if not isinstance(doc, dict):
            raise CorpusProblem(f"{rel} in {root} is not a JSON object")
        out.append((rel, doc))
    return out


def receipt(directory: Path) -> dict | None:
    """The harvest receipt of a corpus directory, or None when it was never harvested."""
    path = Path(directory) / RECEIPT_NAME
    if not path.is_file():
        return None
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CorpusProblem(f"{path} is not readable JSON ({exc})") from None
    return got if isinstance(got, dict) else None


def require_gated(directory: Path, run_id: str, what: str) -> dict:
    """The receipt of a run that is a complete, gated harvest, or refuse by name."""
    got = receipt(directory)
    if got is None:
        raise CorpusProblem(f"{what} {run_id!r} has no {RECEIPT_NAME} under {directory}")
    if not got.get("complete"):
        raise CorpusProblem(f"{what} {run_id!r}: the harvest receipt is not complete")
    gate = got.get("gate")
    if not isinstance(gate, dict) or not gate.get("ok"):
        raise CorpusProblem(f"{what} {run_id!r} was not gated, or its gate failed: a document "
                            "is frozen only once the gate passed it")
    return got


def gate_summary(directory: Path, run_id: str) -> dict:
    """What the tally records of a run's harvest: the receipt's bytes, not its contents.

    ``{receipt, complete, gate_ok[, harvested_at, receipt_sha256, skipped]}``. The sha
    is of the receipt file itself, so a reader can tell outcomes built from this harvest
    from outcomes of an earlier one.
    """
    import keys  # noqa: PLC0415 — local: keys imports nothing from here

    summary: dict = {"receipt": False, "complete": None, "gate_ok": None}
    got = receipt(directory)
    if got is None:
        return summary
    gate = got.get("gate")
    summary = {"receipt": True, "complete": bool(got.get("complete")),
               "gate_ok": bool(gate.get("ok")) if isinstance(gate, dict) else None,
               "harvested_at": got.get("harvested_at"),
               "receipt_sha256": keys.sha256_file(Path(directory) / RECEIPT_NAME)}
    skipped = (gate or {}).get("skipped") if isinstance(gate, dict) else None
    if isinstance(skipped, dict):
        # an inventory's skips, counted at harvest: what the gate's coverage floor let
        # go unkept
        summary["skipped"] = skipped
    return summary


def document_mode(doc: dict) -> str:
    """A document's ``mode``, refusing one this pipeline does not derive."""
    mode = doc.get("mode")
    if not vocab.member(mode, vocab.MODES):
        raise CorpusProblem(f"a harvested document's mode is one of "
                            f"{', '.join(sorted(vocab.MODES))}, not {mode!r}")
    return str(mode)


def source_of(rel: str, doc: dict) -> str:
    """The source a document is of, refusing one filed under another source's name."""
    sid = str(doc.get("source_id") or "")
    if not sid:
        raise CorpusProblem(f"{rel}: a locate document names its source_id")
    if not rel.endswith(f"/{sid}.json"):
        raise CorpusProblem(f"{rel}: the document for {sid} lives at <task-id>/{sid}.json")
    return sid


def task_of(rel: str) -> str:
    """The task a document came back from: the directory it lands in."""
    parts = rel.split("/")
    return parts[-2] if len(parts) >= 2 else ""
