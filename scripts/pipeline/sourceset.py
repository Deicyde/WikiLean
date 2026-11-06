#!/usr/bin/env python3
"""Stage 9a: a sourceset — an append-only, versioned manifest of web sources.

docs/PIPELINE.md section 3 (stage 9a). Ported from formal-companion's
``scripts/sources/sourceset.py``, narrowed to what WikiLean's pipeline needs: no
workspace object, no registry, no command tree. A sourceset is a directory:

    <dir>/manifest-<n>.jsonl    one version; a header line, then entries by source_id
    <dir>/sources/<id>/         the raw bytes, the canonical body and the rendered views
    <dir>/state.json            the latest version, its counts, and what each run wrote

**Append-only is the whole point.** A sealed record set pins its sourceset by
``(id, manifest_sha256)`` and every record of the set states a ``html_sha256`` and a
``layer_sha256`` that came from it, so a version that re-wrote one of those values would
make a published set unreproducible. Version *n+1* therefore holds every source of
version *n* with every non-null field unchanged (:func:`append_only_problems`), names
version *n*'s sha as ``previous_sha256``, and is written only if no other writer got
there first (:meth:`Sourceset.write_version`). A null field may be filled in — an
enumerated page has no body until it is fetched and no layer until it is rendered — and
a determined flag never changes.

A version is a **snapshot** (``cf-sourceset/1``: every entry) or a **delta**
(``cf-sourceset-delta/1``: only the entries that are new or changed since version *n-1*,
its header still counting every source). Thirty thousand entries are ten megabytes, so a
version is a delta unless it changes half the sources or follows :data:`MAX_DELTA_CHAIN`
deltas; reading one replays the deltas onto the snapshot before them, checking the
``previous_sha256`` chain and the append-only rule at every step. The pinned sha is the
version file's own, and through that chain it fixes every entry.

**The two schema strings stay formal-companion's**, as the record set's do, and for the
same reason (docs/PIPELINE.md section 3, stage 6): the format is the contract, not the
producer's name. Batch 1's ``wpm3`` is a seven-version chain ending in a delta whose
sha256 is what both of its sealed sets name, and WikiLean must be able to read it.

Nothing here touches the network (:mod:`wikinet` does) or renders (:mod:`render_web`
does).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import sources as sources_mod  # noqa: E402
import vocab  # noqa: E402

SCHEMA = "cf-sourceset/1"
DELTA_SCHEMA = "cf-sourceset-delta/1"
STATE_SCHEMA = "wikilean.pipeline.sourceset-state/1"
#: a version is a snapshot after this many deltas in a row, so reading the newest
#: replays at most this many files onto one snapshot
MAX_DELTA_CHAIN = 64
#: a version that changes more than this share of the sources is written as a snapshot
SNAPSHOT_SHARE = 0.5
#: the run history keeps this many rows, newest last
MAX_HISTORY = 1000
#: a source directory's files whose shas an entry records
SHA_FILES = {"raw_sha256": "source.raw.html", "html_sha256": "body.html",
             "layer_sha256": "layer.txt"}
#: keys of state.json that are no section
_STATE_RESERVED = frozenset({"schema", "id", "family", "manifest", "counts", "updated_at",
                             "commands", "history"})
#: the keys an entry may carry, and the ones it must
ENTRY_KEYS = ("adopted_from", "assessment", "family", "fetch", "flags", "html_sha256",
              "layer_sha256", "licence_class", "origin", "pageid", "raw_sha256",
              "renderer", "revision", "source_id", "title", "wikidata")
ENTRY_REQUIRED = ("family", "licence_class", "origin", "pageid", "revision", "source_id",
                  "title")

#: characters some line splitters break on that ``json.dumps(ensure_ascii=False)``
#: leaves raw; a manifest line escapes them so every reader sees one line per entry
_LINE_BREAKERS = {"\u0085": "\\u0085", "\u2028": "\\u2028", "\u2029": "\\u2029"}


class SourcesetError(ValueError):
    """A sourceset that is missing, malformed, or would stop being append-only."""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_line(obj: dict) -> str:
    """One manifest line: sorted keys, no spaces, UTF-8 as is except U+0085, U+2028, U+2029."""
    line = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    for raw, escaped in _LINE_BREAKERS.items():
        line = line.replace(raw, escaped)
    return line


def split_lines(text: str) -> list[str]:
    """A manifest's lines: split on LF only, the one separator a manifest is written with."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# an entry
# --------------------------------------------------------------------------

def _is_sha(value) -> bool:
    return isinstance(value, str) and vocab.HEX64.match(value) is not None


def entry_problems(entry) -> list[str]:
    """Why ``entry`` is no sourceset entry, by name; ``[]`` when it is one."""
    out: list[str] = []
    if not isinstance(entry, dict):
        return [f"an entry is an object, not {type(entry).__name__}"]
    unknown = sorted(set(entry) - set(ENTRY_KEYS))
    if unknown:
        out.append(f"unknown key(s) {', '.join(unknown)}")
    for key in ENTRY_REQUIRED:
        if entry.get(key) is None:
            out.append(f"{key} is required")
    source_id = entry.get("source_id")
    try:
        fam = sources_mod.family(entry.get("family"))
    except sources_mod.SourceProblem as exc:
        out.append(str(exc))
        return out
    if entry.get("licence_class") != fam.licence_class:
        out.append(f"licence_class {entry.get('licence_class')!r} is not family "
                   f"{fam.id}'s ({fam.licence_class})")
    try:
        if fam.source_id(entry.get("pageid"), entry.get("revision")) != source_id:
            out.append(f"source_id {source_id!r} does not agree with pageid "
                       f"{entry.get('pageid')!r} and revision {entry.get('revision')!r}")
    except sources_mod.SourceProblem as exc:
        out.append(str(exc))
    if not vocab.member(entry.get("origin"), vocab.SOURCE_ORIGINS):
        out.append(f"origin {entry.get('origin')!r} is not one of "
                   f"{sorted(vocab.SOURCE_ORIGINS)}")
    if not isinstance(entry.get("title"), str):
        out.append("title is a string")
    wikidata = entry.get("wikidata")
    if wikidata is not None and not (isinstance(wikidata, str)
                                     and vocab.QID_RE.match(wikidata)):
        out.append(f"wikidata {wikidata!r} is no QID")
    for key in SHA_FILES:
        value = entry.get(key)
        if value is not None and not _is_sha(value):
            out.append(f"{key} is 64 lowercase hex or null, not {value!r}")
    renderer = entry.get("renderer")
    if renderer is not None and not (isinstance(renderer, str)
                                     and vocab.RENDERER_RE.match(renderer)):
        out.append(f"renderer {renderer!r} is no renderer id")
    if (entry.get("layer_sha256") is None) != (renderer is None):
        out.append("a layer and the renderer that made it are recorded together")
    flags = entry.get("flags")
    if flags is not None:
        if not isinstance(flags, dict):
            out.append("flags is an object")
        else:
            for name, value in sorted(flags.items()):
                if name not in vocab.PAGE_FLAGS:
                    out.append(f"flags has no {name!r} (known: {sorted(vocab.PAGE_FLAGS)})")
                elif value not in (True, False, None):
                    out.append(f"flag {name} is true, false or null, not {value!r}")
    assessment = entry.get("assessment")
    if assessment is not None and not isinstance(assessment, dict):
        out.append("assessment is an object or null")
    return out


def is_adopted(entry: dict) -> bool:
    """Did a consumer pin this source — adopted outright, or its bytes adopted later?"""
    return (entry.get("origin") == vocab.SOURCE_ORIGIN_ADOPTED
            or bool(entry.get("adopted_from")))


def is_concept_article(entry: dict) -> bool:
    """Is no flag of the page true? A redirect, a list or a biography is no concept article."""
    flags = entry.get("flags") or {}
    return not any(flags.get(name) is True for name in vocab.PAGE_FLAGS)


def skip_reason(entry: dict) -> str | None:
    """Why a plan leaves this source out, or None (:data:`vocab.SKIP_REASONS`)."""
    if not is_concept_article(entry):
        true_flags = sorted(name for name in vocab.PAGE_FLAGS
                            if (entry.get("flags") or {}).get(name) is True)
        return f"{vocab.SKIP_NO_CONCEPT} ({', '.join(true_flags)})"
    if not entry.get("layer_sha256"):
        return vocab.SKIP_NOT_RENDERED
    return None


# --------------------------------------------------------------------------
# a version
# --------------------------------------------------------------------------

@dataclass
class Manifest:
    """One version of a sourceset, as read from disk."""

    sourceset_id: str
    version: int
    path: Path | None
    sha256: str | None
    header: dict
    entries: dict[str, dict] = field(default_factory=dict)
    #: how many deltas in a row end at this version (0: it is a snapshot)
    chain: int = 0

    @property
    def is_delta(self) -> bool:
        return self.header.get("schema") == DELTA_SCHEMA

    @property
    def family(self) -> str:
        return str(self.header.get("family") or "")

    def get(self, source_id: str) -> dict | None:
        return self.entries.get(source_id)

    def __iter__(self):
        return iter(self.entries[k] for k in sorted(self.entries))

    def __len__(self) -> int:
        return len(self.entries)

    def pin(self) -> dict:
        """What a run and a seal record: ``{id, version, manifest_sha256}``."""
        return {"id": self.sourceset_id, "version": self.version,
                "manifest_sha256": self.sha256}

    def preferred(self) -> dict[int, str]:
        """``{pageid: source_id}``: the one source per page a run plans.

        A revision a consumer pinned is adopted INSTEAD of one the sourceset chose, so a
        page with an adopted source plans it (the highest adopted revision when there
        are several), and any other page plans its highest enumerated revision.
        """
        best: dict[int, tuple[int, int, str]] = {}
        for entry in self.entries.values():
            key = (1 if is_adopted(entry) else 0, entry["revision"], entry["source_id"])
            if entry["pageid"] not in best or key > best[entry["pageid"]]:
                best[entry["pageid"]] = key
        return {pageid: key[2] for pageid, key in sorted(best.items())}

    def counts(self) -> dict:
        """What ``state.json`` records of this version."""
        pages = {entry["pageid"]: entry for entry in self.entries.values()}
        rendered = sum(1 for e in self.entries.values() if e.get("layer_sha256"))
        return {
            "sources": len(self.entries),
            "pages": len(pages),
            "fetched": sum(1 for e in self.entries.values() if e.get("html_sha256")),
            "rendered": rendered,
            "adopted": sum(1 for e in self.entries.values() if is_adopted(e)),
            "concept_articles": sum(1 for e in pages.values() if is_concept_article(e)),
            "by_class": dict(sorted(Counter(
                str((e.get("assessment") or {}).get("class")) for e in pages.values()).items())),
            "by_importance": dict(sorted(Counter(
                str((e.get("assessment") or {}).get("importance"))
                for e in pages.values()).items())),
        }


def append_only_problems(base: Manifest, entries: dict[str, dict]) -> list[str]:
    """Why ``entries`` cannot follow ``base``: a dropped source or a changed value."""
    out: list[str] = []
    for sid, old in sorted(base.entries.items()):
        new = entries.get(sid)
        if new is None:
            out.append(f"{sid}: version {base.version} holds it and the new version does "
                       f"not (a sourceset never drops a source)")
            continue
        for key in sorted(set(old) | set(new)):
            was = old.get(key)
            if key == "flags" and isinstance(was, dict) and isinstance(new.get(key), dict):
                # each flag is a value of its own: a determined one never changes, an
                # undetermined (null) one may be determined later
                for flag, value in sorted(was.items()):
                    if value is not None and new[key].get(flag) != value:
                        out.append(f"{sid}: flag {flag} was {value!r} in version "
                                   f"{base.version} and would become "
                                   f"{new[key].get(flag)!r} (a recorded value never changes)")
                continue
            if was is not None and new.get(key) != was:
                out.append(f"{sid}: {key} was {was!r} in version {base.version} and would "
                           f"become {new.get(key)!r} (a recorded value never changes)")
    return out


def check_new_id(sourceset_id) -> str:
    """Refuse a NEW sourceset id that a sealed record set could not name.

    A run's export seals ``sourceset.id``, and its consumer refuses a seal whose id is
    outside the record set's token grammar — after every run over the sourceset has been
    paid for. So the id is refused here, where it is chosen.
    """
    if not vocab.TOKEN_RE.match(str(sourceset_id or "")):
        raise SourcesetError(f"sourceset id {sourceset_id!r} is not {vocab.TOKEN_RULE}; a "
                             f"record set's seal names the sourceset by this id and its "
                             f"consumer refuses any other")
    return str(sourceset_id)


def _write_new(path: Path, text: str) -> None:
    """Create ``path`` with ``text`` atomically, refusing when it already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise SourcesetError(f"{path.name} appeared while this command ran: another "
                                 f"writer added a version first; re-run it and it builds "
                                 f"on the newest version") from None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# the sourceset
# --------------------------------------------------------------------------

class Sourceset:
    """One sourceset directory."""

    def __init__(self, directory: str | Path, sourceset_id: str | None = None):
        self.dir = Path(directory)
        self.id = str(sourceset_id or self.dir.name)

    # -- creation and loading ------------------------------------------
    @classmethod
    def create(cls, directory: str | Path, sourceset_id: str, family: str,
               *, created: str | None = None) -> "Sourceset":
        """A new sourceset: ``manifest-1.jsonl`` with the header and no sources."""
        check_new_id(sourceset_id)
        sources_mod.family(family)          # refuses an unknown family
        ss = cls(directory, sourceset_id)
        if ss.versions():
            raise SourcesetError(f"sourceset {sourceset_id!r} already exists at {ss.dir}")
        header = {"schema": SCHEMA, "id": sourceset_id, "version": 1, "family": family,
                  "previous_sha256": None, "created": created or now(), "sources": 0}
        _write_new(ss.manifest_path(1), canonical_line(header) + "\n")
        ss.refresh_state(command="create")
        return ss

    @classmethod
    def open(cls, directory: str | Path, sourceset_id: str | None = None) -> "Sourceset":
        ss = cls(directory, sourceset_id)
        if not ss.versions():
            raise SourcesetError(f"no sourceset manifest in {ss.dir}")
        return ss

    def manifest_path(self, version: int) -> Path:
        return self.dir / f"manifest-{version}.jsonl"

    def source_dir(self, source_id: str) -> Path:
        return self.dir / "sources" / source_id

    def versions(self) -> list[int]:
        out = []
        if self.dir.is_dir():
            for path in self.dir.glob("manifest-*.jsonl"):
                tail = path.name[len("manifest-"):-len(".jsonl")]
                if tail.isdigit() and str(int(tail)) == tail and int(tail) >= 1:
                    out.append(int(tail))
        return sorted(out)

    def latest(self) -> Manifest:
        versions = self.versions()
        if not versions:
            raise SourcesetError(f"sourceset {self.id!r} has no manifest")
        return self.load(versions[-1])

    def load(self, version: int) -> Manifest:
        """Read and check one version: header, order, entries, and the chain before it.

        A delta is replayed onto the version before it, back to the nearest snapshot:
        every link's ``previous_sha256`` must be the sha of the file before it, and every
        delta must follow its predecessor append-only.
        """
        path = self.manifest_path(version)
        if not path.is_file():
            raise SourcesetError(f"sourceset {self.id!r} has no version {version} "
                                 f"(on disk: {self.versions()})")
        chain: list[tuple[int, Path, bytes, dict, list]] = []
        number = version
        while True:
            link = self.manifest_path(number)
            if not link.is_file():
                raise SourcesetError(f"sourceset {self.id!r} version {version} needs "
                                     f"version {number}, which is not on disk: a delta is "
                                     f"read from the versions before it")
            raw = link.read_bytes()
            header, rows = self._parse(link, raw)
            chain.append((number, link, raw, header, rows))
            if header.get("schema") != DELTA_SCHEMA:
                break
            if number == 1:
                raise SourcesetError(f"{link}: version 1 cannot be a delta")
            number -= 1
            if len(chain) > MAX_DELTA_CHAIN + 1:
                raise SourcesetError(f"{path}: more than {MAX_DELTA_CHAIN} deltas in a row")
        manifest = None
        for number, link, raw, header, rows in reversed(chain):
            manifest = self._build(number, link, raw, header, rows, manifest)
        first = chain[-1][0]
        if first > 1:
            before = self.manifest_path(first - 1)
            if before.is_file() and chain[-1][3].get("previous_sha256") != sha256_file(before):
                raise SourcesetError(f"{chain[-1][1]}: previous_sha256 is not the sha of "
                                     f"version {first - 1} (a version was rewritten)")
        return manifest

    def _parse(self, path: Path, raw: bytes) -> tuple[dict, list]:
        lines = split_lines(raw.decode("utf-8"))
        if not lines:
            raise SourcesetError(f"{path} is empty")
        try:
            header = json.loads(lines[0])
            rows = [json.loads(line) for line in lines[1:] if line.strip()]
        except ValueError as exc:
            raise SourcesetError(f"{path}: not JSON lines ({exc})") from None
        return (header if isinstance(header, dict) else {"schema": None}), rows

    def _build(self, version: int, path: Path, raw: bytes, header: dict, rows: list,
               before: "Manifest | None") -> Manifest:
        """One version from its file, given the version before it when it is a delta."""
        problems: list[str] = []
        delta = header.get("schema") == DELTA_SCHEMA
        if header.get("schema") not in (SCHEMA, DELTA_SCHEMA):
            problems.append(f"line 1 is not a {SCHEMA} or {DELTA_SCHEMA} header")
        elif header.get("id") != self.id or header.get("version") != version:
            problems.append(f"the header says sourceset {header.get('id')!r} version "
                            f"{header.get('version')!r}")
        lines: dict[str, dict] = {}
        previous = None
        for number, row in enumerate(rows, 2):
            for problem in entry_problems(row):
                problems.append(f"line {number}: {problem}")
            sid = row.get("source_id") if isinstance(row, dict) else None
            if not isinstance(sid, str):
                continue
            if previous is not None and sid <= previous:
                problems.append(f"line {number}: {sid} is out of order or repeated")
            previous = sid
            if row.get("family") != header.get("family"):
                problems.append(f"line {number}: family {row.get('family')!r} in a "
                                f"{header.get('family')!r} sourceset")
            lines[sid] = row
        if delta:
            if before is None:
                problems.append("a delta needs the version before it")
                entries = dict(lines)
            else:
                if header.get("previous_sha256") != before.sha256:
                    problems.append(f"previous_sha256 is not the sha of version "
                                    f"{version - 1} (a version was rewritten)")
                if header.get("family") != before.family:
                    problems.append(f"family {header.get('family')!r} after a "
                                    f"{before.family!r} version")
                # only the delta's lines can break the rule: check those, not every entry
                touched = Manifest(self.id, before.version, None, None, {},
                                   {sid: before.entries[sid] for sid in lines
                                    if sid in before.entries})
                problems.extend(append_only_problems(touched, lines))
                unchanged = [sid for sid, row in lines.items() if before.entries.get(sid) == row]
                if unchanged:
                    problems.append(f"{len(unchanged)} line(s) repeat an entry unchanged "
                                    f"({unchanged[0]}): a delta holds only what changed")
                entries = {**before.entries, **lines}
        else:
            entries = lines
        if header.get("sources") != len(entries):
            problems.append(f"the header counts {header.get('sources')!r} sources, the "
                            f"version holds {len(entries)}")
        if problems:
            raise SourcesetError(f"{path}: " + "; ".join(problems[:10])
                                 + (f" (and {len(problems) - 10} more)"
                                    if len(problems) > 10 else ""))
        chain = 0 if not delta else (before.chain + 1 if before else 1)
        return Manifest(self.id, version, path, hashlib.sha256(raw).hexdigest(), header,
                        entries, chain)

    # -- writing a version ---------------------------------------------
    def write_version(self, entries: dict[str, dict], *, created: str | None = None,
                      force_snapshot: bool = False) -> Manifest:
        """Write the next version from the whole entry set, append-only, and return it.

        ``entries`` is every source the sourceset will hold, not only what changed: the
        delta is computed here, so a caller cannot write one that drops a source by
        forgetting it.
        """
        versions = self.versions()
        if not versions:
            raise SourcesetError(f"no sourceset manifest in {self.dir}: create it first")
        base = self.load(versions[-1])
        for sid, entry in sorted(entries.items()):
            problems = entry_problems(entry)
            if entry.get("source_id") != sid:
                problems.append(f"the entry under {sid!r} says source_id "
                                f"{entry.get('source_id')!r}")
            if problems:
                raise SourcesetError(f"{sid}: " + "; ".join(problems))
            if entry.get("family") != base.family:
                raise SourcesetError(f"{sid}: family {entry.get('family')!r} in a "
                                     f"{base.family!r} sourceset")
        problems = append_only_problems(base, entries)
        if problems:
            raise SourcesetError("; ".join(problems[:10])
                                 + (f" (and {len(problems) - 10} more)"
                                    if len(problems) > 10 else ""))
        changed = {sid: entry for sid, entry in entries.items()
                   if base.entries.get(sid) != entry}
        if not changed:
            raise SourcesetError(f"version {base.version} already says this: a version is "
                                 f"written once and never again")
        version = base.version + 1
        snapshot = (force_snapshot or base.chain >= MAX_DELTA_CHAIN
                    or len(changed) > SNAPSHOT_SHARE * max(len(entries), 1))
        header = {"schema": SCHEMA if snapshot else DELTA_SCHEMA, "id": self.id,
                  "version": version, "family": base.family,
                  "previous_sha256": base.sha256, "created": created or now(),
                  "sources": len(entries)}
        if not snapshot:
            header["changed"] = len(changed)
        rows = entries if snapshot else changed
        text = "".join(canonical_line(header) + "\n")
        text += "".join(canonical_line(rows[sid]) + "\n" for sid in sorted(rows))
        _write_new(self.manifest_path(version), text)
        return self.load(version)

    # -- the source files ----------------------------------------------
    def source_problems(self, entry: dict) -> list[str]:
        """Why a source's directory does not hold the bytes its entry records."""
        out: list[str] = []
        directory = self.source_dir(entry["source_id"])
        for key, name in sorted(SHA_FILES.items()):
            want = entry.get(key)
            if want is None:
                continue
            path = directory / name
            if not path.is_file():
                out.append(f"{name} is missing, and the entry records its sha")
                continue
            got = sha256_file(path)
            if got != want:
                out.append(f"{name} is {got[:12]}… and the entry records {want[:12]}…")
        return out

    # -- state ----------------------------------------------------------
    def state_path(self) -> Path:
        return self.dir / "state.json"

    def read_state(self) -> dict:
        try:
            value = json.loads(self.state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def refresh_state(self, *, command: str = "", section: dict | None = None,
                      at: str | None = None) -> dict:
        """Rewrite ``state.json`` from the newest version, and record this command."""
        state = self.read_state()
        manifest = self.latest()
        stamp = at or now()
        state.update({"schema": STATE_SCHEMA, "id": self.id, "family": manifest.family,
                      "manifest": manifest.pin(), "counts": manifest.counts(),
                      "updated_at": stamp})
        if command:
            commands = dict(state.get("commands") or {})
            commands[command] = {"at": stamp, "version": manifest.version}
            state["commands"] = commands
            history = list(state.get("history") or [])
            history.append({"at": stamp, "command": command, "version": manifest.version})
            state["history"] = history[-MAX_HISTORY:]
            if section is not None:
                state[command] = section
        write_json(self.state_path(), state)
        return state


# --------------------------------------------------------------------------
# the command
# --------------------------------------------------------------------------

def _status(ss: Sourceset, *, verify: bool) -> int:
    manifest = ss.latest()
    pin = manifest.pin()
    print(f"{pin['id']} version {pin['version']} manifest {pin['manifest_sha256']}")
    counts = manifest.counts()
    for key in ("sources", "pages", "fetched", "rendered", "adopted", "concept_articles"):
        print(f"  {key}: {counts[key]}")
    if not verify:
        return 0
    bad = 0
    for entry in manifest:
        problems = ss.source_problems(entry)
        if problems:
            bad += 1
            print(f"  {entry['source_id']}: {'; '.join(problems)}")
    print(f"  sources whose files disagree with their entry: {bad}")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", required=True, type=Path, help="the sourceset directory")
    parser.add_argument("--id", help="its id (the directory's name by default)")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="write manifest-1.jsonl")
    create.add_argument("--family", required=True)
    status = sub.add_parser("status", help="the pin and the counts")
    status.add_argument("--verify", action="store_true",
                        help="check every source's files against its entry")
    sub.add_parser("pin", help="print the pin a run records, as JSON")
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            ss = Sourceset.create(args.dir, args.id or Path(args.dir).name, args.family)
            print(f"{ss.id}: manifest-1.jsonl")
            return 0
        ss = Sourceset.open(args.dir, args.id)
        if args.command == "pin":
            print(json.dumps(ss.latest().pin(), indent=1, sort_keys=True))
            return 0
        return _status(ss, verify=args.verify)
    except (SourcesetError, sources_mod.SourceProblem) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
