#!/usr/bin/env python3
"""A package as one file: the envelope the claims API hands a contributor.

docs/PIPELINE.md section 3 (stage 11). A package published through the claims API
(``publish_package.py --envelope``) travels as one JSON document, which the private
bucket stores under its own sha256 (``packages/v1/<sha256>.json``)::

    {"schema": "wikilean.contribute.package-transport/1", "run_id": "<run id>",
     "files": {"<relpath>": "<base64 of the file's bytes>", ...}}

``files`` holds ``package.json`` and every file it names, and nothing else. The
encoding is canonical (keys sorted, no whitespace, ASCII only), so the bytes are a
function of the package and their sha256 names one package. The files are base64
because a package may carry bytes that are not UTF-8.

**Reading one is where a contributor's machine meets bytes it did not make**, so
:func:`unpack` checks before it writes anything: the size, the canonical form, the key
set, each path's grammar, the base64, and ``package.json``'s run id. Then it writes
every file with exclusive creation into a fresh directory beside the target, so two
names that differ only in case collide on a case-insensitive disk instead of one
overwriting the other; runs :func:`package.read` over that directory; and only then
moves it into place.

No network here: ``scripts/articles/contrib_transport.py`` moves the bytes, and
``scripts/articles/upload_contrib.py package`` puts them in the bucket.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import package as package_mod  # noqa: E402

ENVELOPE_SCHEMA = "wikilean.contribute.package-transport/1"
#: The claims API's ceiling (wiki/src/claims.ts PACKAGE_MAX_BYTES).
MAX_ENVELOPE_BYTES = 64 * 1024 * 1024
#: More files than any planned run holds, and few enough that unpacking one cannot make
#: millions of directories out of 64 MiB of short names.
MAX_FILES = 20_000


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def encode(directory: Path) -> bytes:
    """The envelope of the package in ``directory``, which is read and checked first.

    What it returns has been through :func:`decode`, so a publisher learns here of an
    envelope no contributor could unpack.
    """
    directory = Path(directory)
    manifest = package_mod.read(directory)
    run_id = package_mod.check_run_id(manifest.get("run_id"))
    if manifest.get("package") != run_id:
        raise package_mod.PackageProblem(f"{directory}: package.json names package "
                                         f"{manifest.get('package')!r} and run {run_id!r}")
    names = sorted(manifest["files"]) + [package_mod.PACKAGE_JSON]
    data = canonical({"schema": ENVELOPE_SCHEMA, "run_id": run_id,
                      "files": {rel: base64.b64encode((directory / rel).read_bytes())
                                .decode("ascii") for rel in names}})
    if len(data) > MAX_ENVELOPE_BYTES:
        raise package_mod.PackageProblem(
            f"the envelope of {run_id} is {len(data)} bytes, over the claims API's "
            f"{MAX_ENVELOPE_BYTES}; plan fewer sources into one package")
    decode(data)
    return data


def save(data: bytes, out: Path) -> tuple[str, int]:
    """Write envelope bytes to ``out``, whole or not at all: ``(sha256, bytes)``."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    scratch = out.with_name(f".{out.name}.part")
    scratch.write_bytes(data)
    os.replace(scratch, out)
    return hashlib.sha256(data).hexdigest(), len(data)


def _object(pairs: list) -> dict:
    keys = [key for key, _value in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key appears twice in one object")
    return dict(pairs)


def _no_constant(name: str):
    raise ValueError(f"{name} is not JSON")


def _strict(data: bytes, what: str):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_object,
                          parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise package_mod.PackageProblem(f"{what} is not strict JSON ({exc})") from None


def decode(data: bytes) -> tuple[str, dict[str, bytes]]:
    """``(run id, {relpath: bytes})`` of an envelope, or refuse it by name.

    Every check that needs no disk: the size, the canonical form, exactly the keys
    ``schema``, ``run_id`` and ``files``, the schema, plain paths that stay distinct on a
    case-insensitive disk, canonical base64, and a ``package.json`` of this run whose
    ``files`` are exactly the other files carried and sit where :func:`package.shape`
    allows. The shas are :func:`package.read`'s, on disk, in :func:`unpack`.
    """
    if not isinstance(data, (bytes, bytearray)) or not 0 < len(data) <= MAX_ENVELOPE_BYTES:
        raise package_mod.PackageProblem(f"an envelope holds between 1 and "
                                         f"{MAX_ENVELOPE_BYTES} bytes")
    value = _strict(bytes(data), "the envelope")
    if not isinstance(value, dict) or set(value) != {"schema", "run_id", "files"}:
        raise package_mod.PackageProblem("an envelope is one object of exactly schema, "
                                         "run_id and files")
    if value["schema"] != ENVELOPE_SCHEMA:
        raise package_mod.PackageProblem(f"the envelope's schema is {value['schema']!r}, "
                                         f"not {ENVELOPE_SCHEMA!r}")
    run_id = package_mod.check_run_id(value["run_id"])
    if canonical(value) != bytes(data):
        raise package_mod.PackageProblem("the envelope is not in canonical form (sorted keys, "
                                         "no whitespace, ASCII), so its sha256 does not "
                                         "name one package")
    files = value["files"]
    if not isinstance(files, dict) or package_mod.PACKAGE_JSON not in files:
        raise package_mod.PackageProblem(f"the envelope carries no {package_mod.PACKAGE_JSON}")
    if len(files) > MAX_FILES:
        raise package_mod.PackageProblem(f"the envelope carries {len(files)} files, over "
                                         f"{MAX_FILES}")
    contents: dict[str, bytes] = {}
    folded: dict[str, str] = {}
    for rel, text in sorted(files.items()):
        if not package_mod.plain(rel):
            raise package_mod.PackageProblem(f"the envelope carries {rel[:120]!r}, which is "
                                             "not a plain path inside a package")
        if rel.casefold() in folded:
            raise package_mod.PackageProblem(f"{rel} and {folded[rel.casefold()]} are one "
                                             "path on a case-insensitive disk")
        folded[rel.casefold()] = rel
        if not isinstance(text, str):
            raise package_mod.PackageProblem(f"{rel}: not base64")
        try:
            contents[rel] = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            raise package_mod.PackageProblem(f"{rel}: not base64") from None
        # one spelling of the bytes, or one package would have many shas
        if base64.b64encode(contents[rel]).decode("ascii") != text:
            raise package_mod.PackageProblem(f"{rel}: not canonical base64")
    manifest = _strict(contents[package_mod.PACKAGE_JSON], package_mod.PACKAGE_JSON)
    if not isinstance(manifest, dict) or manifest.get("run_id") != run_id \
            or manifest.get("package") != run_id:
        raise package_mod.PackageProblem(f"the envelope's {package_mod.PACKAGE_JSON} is not "
                                         f"the package of run {run_id}")
    named = manifest.get("files")
    carried = set(contents) - {package_mod.PACKAGE_JSON}
    if not isinstance(named, dict) or carried != set(named):
        missing = sorted(set(named or {}) - carried) if isinstance(named, dict) else []
        extra = sorted(carried - set(named or {})) if isinstance(named, dict) else sorted(carried)
        raise package_mod.PackageProblem(
            f"the envelope and its {package_mod.PACKAGE_JSON} disagree"
            + (f"; named but not carried: {', '.join(missing[:3])}" if missing else "")
            + (f"; carried but not named: {', '.join(extra[:3])}" if extra else ""))
    problems = package_mod.shape(manifest)
    if problems:
        raise package_mod.PackageProblem("the envelope's package is not one a contributor "
                                         "runs: " + "; ".join(problems[:5]))
    return run_id, contents


def _same(directory: Path, contents: dict[str, bytes]) -> bool:
    """Whether ``directory`` already holds exactly these files and nothing else."""
    held = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()}
    return held == set(contents) and all(
        (directory / rel).read_bytes() == data for rel, data in contents.items())


def unpack(data: bytes, packages: Path) -> tuple[Path, dict]:
    """Write an envelope's package to ``<packages>/<run id>/``: ``(directory, manifest)``.

    Re-unpacking the same envelope finds it in place and changes nothing. A directory
    that holds anything else is refused, never overwritten: it may be a run's only copy.
    """
    run_id, contents = decode(data)
    packages = Path(packages)
    target = packages / run_id
    if target.exists():
        if target.is_dir() and not target.is_symlink() and _same(target, contents):
            return target, package_mod.read(target)
        raise package_mod.PackageProblem(f"{target} already holds another package; move it "
                                         "aside to unpack this one")
    packages.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=packages))
    try:
        for rel, blob in sorted(contents.items()):
            path = scratch / rel
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "xb") as stream:
                    stream.write(blob)
            except OSError as exc:
                raise package_mod.PackageProblem(
                    f"{rel} collides with another path the envelope carries "
                    f"({type(exc).__name__}): two names that differ only in case, or a "
                    "file where a directory goes") from None
        manifest = package_mod.read(scratch)
        os.rename(scratch, target)
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    return target, manifest
