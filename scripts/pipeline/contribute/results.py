#!/usr/bin/env python3
"""The results leg: what a contributor machine sends back, and where it lands.

docs/PIPELINE.md section 3 (stage 11). A run host's results come home over ssh. A
contributor machine has no ssh, so the same files go into the work repo under
``results/<package>/<machine-id>/`` and come back out at the coordinator's collect::

    results/<package>/<machine-id>/documents/<task-id>/<source-id>.json   the gated documents
    results/<package>/<machine-id>/host/gate.jsonl        the gate's own log
    results/<package>/<machine-id>/host/envelopes/*.json  one per agent call
    results/<package>/<machine-id>/host/contracts/...     the turn records
    results/<package>/<machine-id>/set/                   an optional sealed record set
    results/<package>/<machine-id>/RESULT.json            what ran, and every file's sha256

**Never in the results: article text.** A locate document quotes at most the export's
cap and is what the whole pipeline is for; a layer, a canonical body or a raw response is
the article. :data:`REFUSED_NAMES` is the list this refuses **by name** rather than
copying, and nothing is filtered silently: a file that is neither a document nor one of
the records above stops the copy and is named.

**The sealed set, when there is one**, is checked by ``scripts/articles/
verify_submission.py`` — the intake contract, offline — on both sides: the contributor
before it pushes, so a defect is seen before the coordinator is the first to find it,
and the coordinator on collect, because a result is untrusted input. The refusal codes
and HTTP statuses are docs/INTAKE.md section 3's, so the work-repo leg and the
submission endpoint that will replace it refuse the same sets for the same reasons.
"""
from __future__ import annotations

import json
import re
import stat
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent
ROOT = PIPELINE.parents[1]
for _path in (str(HERE), str(PIPELINE), str(ROOT / "scripts" / "articles")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import keys  # noqa: E402

#: Where a result's documents and the run's own records sit.
DOCUMENTS = "documents"
HOST = "host"
#: Where an optional sealed record set sits, as the intake verifier reads it.
RECORD_SET = "set"
RESULT_JSON = "RESULT.json"
RESULT_SCHEMA = "wikilean.contribute.result/1"
#: A file name that is article text, whatever directory it sits in: refused, never copied.
REFUSED_NAMES = ("layer.txt", "body.html", "source.raw.html", "text.txt", "head.txt",
                 "reading.txt", "layer_map.json", "sections.json", "blocks.json")


class ResultProblem(ValueError):
    """A result this transport refuses to send or to take."""


def refuse_article_text(paths: list[Path]) -> None:
    """Refuse the copy when any path is one of :data:`REFUSED_NAMES`."""
    named = sorted({p.name for p in paths if p.name in REFUSED_NAMES})
    if named:
        raise ResultProblem(
            f"the result would carry article text ({', '.join(named)}); a result carries the "
            "gated documents, the run's own records and an optional sealed record set, and "
            "nothing of the rendered source leaves the contributor machine")


def regular_files(root: Path) -> list[Path]:
    """Inspect the whole result tree before reading or copying any payload bytes."""
    root = Path(root)
    if root.is_symlink():
        raise ResultProblem(f"{root.name}: a symlink")
    if not root.is_dir():
        return []
    files = []
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        rel = path.relative_to(root).as_posix()
        if stat.S_ISLNK(mode):
            raise ResultProblem(f"{rel}: a symlink")
        if stat.S_ISREG(mode):
            files.append(path)
        elif not stat.S_ISDIR(mode):
            raise ResultProblem(f"{rel}: not a regular file or directory")
    return files


def copy_tree(src: Path, dest: Path, pattern: str = "*") -> list[Path]:
    """Copy ``src`` under ``dest``; returns what landed, article text refused."""
    src, dest = Path(src), Path(dest)
    files = [p for p in regular_files(src)
             if p.match(pattern) and "__pycache__" not in p.parts]
    refuse_article_text(files)
    out = []
    for path in files:
        target = dest / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        out.append(target)
    return out


def sha_manifest(root: Path) -> dict[str, str]:
    """``{path relative to root: sha256}`` of every file under it, sorted."""
    root = Path(root)
    return {p.relative_to(root).as_posix(): keys.sha256_file(p)
            for p in regular_files(root) if p != root / RESULT_JSON}


def write_receipt(root: Path, payload: dict) -> Path:
    """Write ``RESULT.json``: what ran, and the sha256 of every file beside it."""
    root = Path(root)
    receipt = {"schema": RESULT_SCHEMA, **payload, "files": sha_manifest(root)}
    path = root / RESULT_JSON
    path.write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path


def check_receipt(root: Path) -> list[str]:
    """What a coordinator checks of a fetched result: every file the receipt names."""
    root = Path(root)
    try:
        actual = {p.relative_to(root).as_posix(): p for p in regular_files(root)}
    except (OSError, ResultProblem) as exc:
        return [str(exc)]
    path = actual.get(RESULT_JSON)
    if path is None:
        return [f"{RESULT_JSON}: missing"]
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{RESULT_JSON}: unreadable JSON ({exc})"]
    if not isinstance(receipt, dict):
        return [f"{RESULT_JSON}: must be an object"]
    if receipt.get("schema") != RESULT_SCHEMA:
        return [f"{RESULT_JSON}: schema is {receipt.get('schema')!r}, not {RESULT_SCHEMA!r}"]
    named = receipt.get("files")
    if not isinstance(named, dict):
        return [f"{RESULT_JSON}: files must be an object"]
    problems = []
    for rel, sha in named.items():
        parts = rel.split("/")
        if (not rel or "\\" in rel or any(ord(c) < 32 for c in rel)
                or Path(rel).is_absolute() or any(p in ("", ".", "..") for p in parts)
                or rel == RESULT_JSON):
            problems.append(f"{rel!r}: not a canonical relative payload path")
        elif not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            problems.append(f"{rel}: not a SHA-256")
    # Validate every name before joining paths or reading payloads. A manifest from
    # another machine has no authority over any file outside this inspected tree.
    if problems:
        return problems
    for rel, sha in sorted(named.items()):
        target = actual.get(rel)
        if target is None:
            problems.append(f"{rel}: missing")
        elif keys.sha256_file(target) != sha:
            problems.append(f"{rel}: sha256 does not match {RESULT_JSON}")
    problems.extend(f"{rel}: not in {RESULT_JSON}"
                    for rel in sorted(set(actual) - set(named) - {RESULT_JSON}))
    problems.extend(f"{name}: article text, which a result never carries"
                    for name in sorted({Path(rel).name for rel in named}
                                       & set(REFUSED_NAMES)))
    return problems


# ---- the sealed set, through the intake contract -------------------------------------


def verify_record_set(set_dir: Path, *, machine_id: str, received_at: str,
                      campaigns: tuple[str, ...] = (),
                      universes: tuple[str, ...] = ()) -> dict:
    """Run docs/INTAKE.md section 3 over a sealed set, as the submission endpoint will.

    Returns ``{"accepted": True, "submission": {...}, "records": n}`` or raises
    :class:`ResultProblem` carrying the refusal's code, HTTP status, retry rule and
    reason — the same answer on the contributor's side and on the coordinator's, because
    it is the same function (``scripts/articles/verify_submission.py``, which delegates
    every record-set check to the store builder so intake and the store cannot drift).
    """
    import verify_submission as intake

    scope = intake.KeyScope(machine_id=machine_id, campaigns=tuple(campaigns),
                            universes=tuple(universes))
    outcome = intake.verify(Path(set_dir), scope=scope, received_at=received_at)
    if isinstance(outcome, intake.Refusal):
        raise ResultProblem(outcome.report())
    return {"accepted": True, "verifier": outcome.submission.get("verifier"),
            "checker_version": outcome.submission.get("checker_version"),
            "seal_sha256": outcome.submission.get("seal_sha256"),
            "set_id": outcome.submission.get("set_id"),
            "campaign": outcome.submission.get("campaign"),
            "status_rule": outcome.submission.get("status_rule"),
            "records": len(outcome.records)}
