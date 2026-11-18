#!/usr/bin/env python3
"""Package a converted declaration index for publication: a bundle, a manifest, a licence file.

docs/PIPELINE.md section 3 (stage 8) and the public-editing plan's decision 10. The
index is built — ``index_from_corpus.py`` writes ``decl-index.jsonl`` and the search
corpus beside it — and a contributor on path 3 cannot start without both. Together
they are about **half a gigabyte per universe**, which is too big for git, too big for
a Worker asset, and therefore something somebody has to *publish*. This module is the
step before that: it takes what the converter wrote and lays it out as one versioned,
checksummed, licence-labelled directory, so whatever host is picked is handed an
artefact rather than two loose paths.

    <out>/
      MANIFEST.json            the bundle: schema, version, universe, every file's sha256
      LICENCES.spdx.json       one SPDX record per library of the universe
      LICENCES.md              the same table, for a person reading it beside the download
      README.md                what the bundle is and which part of it to take
      index/decl-index.jsonl   the index the tally and the gate join on
      search/*.tsv, README.md  the search corpus a sandboxed worker Greps
      names/declarations.tsv   the names-only subset: no Lean source text
      names/wikidata.tsv
      names/README.md
      licences/<library>/      each library's own LICENSE, COPYING and NOTICE files,
                               with --licence-texts

**It uploads nothing.** No network, no R2, no Worker. ``scripts/articles/upload_contrib.py``
puts a finished bundle in the contributor bucket, keyed by the sha256 of its
``MANIFEST.json``; this writes a directory and says what is in it.

**Why there is a names-only subset.** ``signatures.tsv`` and ``docstrings.tsv`` carry
**Lean source text from every library of the universe, under each library's own
licence** — handing them to a contributor is a distribution, not an internal read. A
declaration *name*, its kind, its module and its line are facts about a public
repository and carry far less licence weight, so the two files that hold only those
are written again on their own: that subset, and not the whole corpus, is what the
site's autocomplete asset should be built from.

**Why there is a licence manifest at all.** A universe states each library's SPDX id,
and a bundle that travels without them makes the person who publishes it guess. A copy
handed to a contributor cannot be recalled, so the licence texts themselves travel too:
``--licence-texts <dir>`` takes ``<dir>/<library id>/`` per library, holding the
``LICENSE``/``LICENCE``/``COPYING`` file and any ``NOTICE`` file from its repository at
the pinned commit (this stays offline, so it cannot fetch them), and refuses a library
with none. Where
a library records no licence this writes ``NOASSERTION`` — SPDX's own word for "not
asserted here" — and lists it, because an unknown licence is a thing to go and find
out, never a permission. Where a licence or the universe's own exclusion list says a
library's text may not be used for machine learning or for evaluation, the library is
**flagged**, and the flag is in the manifest, in the readable table and in this
command's own output.

    python3 scripts/pipeline/package_index.py --index <decl-index.jsonl> \\
        --search <dir> --universe <universe.yaml|json> --out <dir> \\
        [--licence-texts <dir>] [--version <token>] \\
        [--provenance <index-provenance.json>] [--created <ISO-Z>] \\
        [--restricted <library>=<reason>] [--link]

Exit codes: 0 the bundle is written, 1 it is written and a library's use is restricted
or unknown (the list is on stdout), 2 the inputs were refused before anything was
written, 3 the packager failed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import index_from_corpus as converter  # noqa: E402
import vocab  # noqa: E402

BUNDLE_SCHEMA = "wikilean.pipeline.index-bundle/2"
LICENCE_SCHEMA = "wikilean.pipeline.index-licences/1"
MANIFEST_NAME = "MANIFEST.json"
LICENCES_JSON = "LICENCES.spdx.json"
LICENCES_MD = "LICENCES.md"
README_NAME = "README.md"
#: Where each part of the bundle goes.
INDEX_DIR = "index"
SEARCH_DIR = "search"
NAMES_DIR = "names"
LICENCE_TEXTS_DIR = "licences"
#: A library's licence files, as a repository names them: ``LICENSE``, ``LICENCE.md``,
#: ``COPYING``, ``LICENSE-APACHE``. ``NOTICE`` files ride along; at least one of the
#: others is required.
_LICENCE_FILE = re.compile(r"^(?:licen[cs]e|copying)(?:[._-].*)?$", re.IGNORECASE)
_NOTICE_FILE = re.compile(r"^notice(?:[._-].*)?$", re.IGNORECASE)
#: The converter's own search files, taken by name and never globbed, so a stray file
#: beside them does not ride into a published bundle.
SEARCH_FILES = converter.SEARCH_FILES
#: The two of them that carry **no Lean source text** — a declaration's name, kind,
#: proof status, module and line, and the ``@[wikidata]`` QIDs it is tagged with. This
#: is the subset the editor's autocomplete is built from (the plan's section 5.3).
NAMES_FILES = ("declarations.tsv", "wikidata.tsv")
#: The two that do carry it, named here so the licence note can say which they are.
SOURCE_TEXT_FILES = ("signatures.tsv", "docstrings.tsv")
#: SPDX's own word for a licence this bundle does not assert.
NOASSERTION = "NOASSERTION"

#: SPDX ids whose terms restrict what a bundle's reader may do with the text — the
#: question decision 10 has to answer before anything is published. ``NonCommercial``
#: and ``NoDerivatives`` are the two that bite: an evaluation run is use, and a
#: derived index is a derivative. Matched on the id, case-insensitively.
RESTRICTED_SPDX = {
    "nc": "a NonCommercial term: evaluation and ML use are not clearly permitted",
    "nd": "a NoDerivatives term: a derived index or corpus is a derivative work",
}
#: Words that make an exclusion's own reason a restriction rather than a convenience.
#: The universe records why it left a library out, and when the reason is the licence
#: that reason is about any *other* copy of the same repository too.
_RESTRICTION_WORDS = re.compile(
    r"non-?commercial|\bnc\b|no-?ml|machine learning|\bml\b|evaluation|"
    r"no-?deriv|research[- ]only|not for use|proprietary|all rights reserved|"
    r"licen[cs]e|copyright|\bterms\b", re.IGNORECASE)
#: An ``owner/repo`` pair as a reason or a repo URL spells it.
_REPO_PAIR = re.compile(r"([A-Za-z0-9][\w.-]*)/([A-Za-z0-9][\w.-]*)")
#: A URL inside an exclusion's prose reason, so its ``owner/repo`` is read the same way
#: a library's own ``repo`` field is rather than as two words that happen to be
#: adjacent.
_URL = re.compile(r"\b(?:https?://|git@)\S+")
#: Host names that are not an ``owner`` — the first pair a URL yields is
#: ``<host>/<owner>``, which would join two libraries of one owner into one repository.
_NOT_AN_OWNER = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org",
                 "sr.ht", "git.sr.ht", "www.github.com")


class PackageProblem(ValueError):
    """An input this packager refuses, named rather than packaged anyway."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---- the universe's libraries and their licences ---------------------------------------


def read_universe(path: Path) -> dict:
    """A universe document, from JSON or from the planner's YAML spelling.

    JSON is the shape a sealed record set carries and the shape the tests use; YAML is
    what ``cf corpus`` writes. PyYAML is optional here, as it is in :mod:`tally`: a
    host without it is told to hand in the JSON spelling rather than being given a
    second parser that might read the file differently.
    """
    path = Path(path)
    if not path.is_file():
        raise PackageProblem(f"no universe document at {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        value = json.loads(text)
    else:
        try:
            import yaml  # noqa: PLC0415 — optional, and only for the planner's spelling
        except ImportError:
            raise PackageProblem(
                f"{path} is YAML and this interpreter has no PyYAML; hand in the "
                "universe's JSON spelling instead (a sealed record set carries one as "
                "universe.json)") from None
        value = yaml.safe_load(text)
    if not isinstance(value, dict) or not isinstance(value.get("libraries"), list):
        raise PackageProblem(f"{path} is not a universe document (no libraries list)")
    return value


def _repo_of_url(url: str) -> str:
    """``owner/repo`` of a repository URL, lower-cased; empty when it is not one.

    The last two path components, less a trailing ``.git`` — so
    ``https://github.com/o/r``, ``https://github.com/o/r.git`` and
    ``git@github.com:o/r.git`` are one repository, and a bare host is none. Only the
    last two: the whole URL run through :data:`_REPO_PAIR` would also yield
    ``github.com/<owner>``, which joins two libraries that share an owner and not a
    repository.
    """
    text = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", str(url or "").strip())
    parts = [part for part in text.rstrip("/").replace(":", "/").split("/") if part]
    if len(parts) < 2:
        return ""
    owner, repo = parts[-2].lower(), parts[-1].lower()
    if repo.endswith(".git"):
        repo = repo[:-len(".git")]
    return "" if owner in _NOT_AN_OWNER or not repo else f"{owner}/{repo}"


def _repo_pairs(text: str) -> set[str]:
    """Every ``owner/repo`` a free-text reason names, lower-cased.

    An exclusion's reason is prose and may name a repository as a bare ``owner/repo``
    or as a URL. Both are read: every URL goes through :func:`_repo_of_url`, and the
    rest of the text through :data:`_REPO_PAIR` with the URLs taken out first, so a
    URL contributes its repository and not also its ``<host>/<owner>``.
    """
    text = str(text or "")
    out = {pair for pair in (_repo_of_url(url) for url in _URL.findall(text)) if pair}
    rest = _URL.sub(" ", text)
    out |= {f"{m.group(1).lower()}/{m.group(2).lower()}"
            for m in _REPO_PAIR.finditer(rest)}
    return out


#: A library id's version suffix, as a universe spells one: ``atlas-v1``, ``atlas-v12``.
_VERSION_SUFFIX = re.compile(r"^(?P<stem>.+?)-v[0-9]+$")


def _stem(library_id: str) -> str:
    """A library id with its ``-v<N>`` suffix taken off, if it has one."""
    found = _VERSION_SUFFIX.match(str(library_id or ""))
    return found.group("stem") if found else str(library_id or "")


def _versioned_sibling(one: str, other: str) -> bool:
    """Are these two ids versions of one library? ``atlas``/``atlas-v1``, ``-v1``/``-v2``.

    Deliberately narrow: two ids are siblings when they are equal **after** their
    ``-v<N>`` suffixes come off, and at least one of them had one. The obvious looser
    rule — "share the leading token" — would make one licence-excluded ``ff-*``
    library flag all forty-four of them, which is a review queue nobody reads.
    """
    one, other = str(one or ""), str(other or "")
    if not one or not other or one == other:
        return False
    return _stem(one) == _stem(other)


def spdx_restriction(spdx: str) -> str | None:
    """Why this SPDX id restricts use, or None. ``NOASSERTION`` is unknown, not restricted."""
    parts = {part.lower() for part in re.split(r"[-+.]", str(spdx or ""))}
    for token, why in RESTRICTED_SPDX.items():
        if token in parts:
            return f"{spdx} carries {why}"
    return None


def excluded_restrictions(universe: dict) -> list[dict]:
    """The universe's own exclusions that are about a licence, with what they name.

    A universe records why it left a library out. When the reason is the licence, it is
    a statement about that *repository*, and a second copy of the same repository that
    the universe did include is a thing to look at before publishing — which is exactly
    u1's case, where ATLAS v1 is excluded for its licence and ATLAS v2 is in.
    """
    out = []
    for entry in universe.get("excluded") or []:
        if not isinstance(entry, dict):
            continue
        reason = str(entry.get("reason") or "")
        if not _RESTRICTION_WORDS.search(reason):
            continue
        out.append({"id": str(entry.get("id") or ""), "reason": reason,
                    "repos": sorted(_repo_pairs(reason) |
                                    ({_repo_of_url(entry.get("repo"))}
                                     if entry.get("repo") else set()))})
    return out


def library_licences(universe: dict, *, stated: dict[str, str] | None = None) -> list[dict]:
    """One licence row per library of the universe, in id order.

    ``spdx`` is the universe's own ``licence.spdx``, or :data:`NOASSERTION` where the
    library records none. ``restrictions`` is a list of reasons, from three places that
    are kept apart so a reader can see which is which:

    * the SPDX id itself (:func:`spdx_restriction`);
    * the universe's own exclusion list, where a library excluded **for a licence
      reason** is from the same repository as this one, or is a ``-v<N>`` version of
      it (:func:`excluded_restrictions`) — a **review** flag, not a verdict: it says
      the operator has already written a licence problem down about these bytes, and
      somebody should read the repository and settle it before they are published;
    * ``--restricted <library>=<reason>``, which is the operator stating one directly.
    """
    exclusions = excluded_restrictions(universe)
    stated = dict(stated or {})
    rows = []
    for lib in universe.get("libraries") or []:
        if not isinstance(lib, dict):
            continue
        lib_id = str(lib.get("id") or "")
        licence = lib.get("licence")
        if isinstance(licence, dict):
            spdx = str(licence.get("spdx") or "") or NOASSERTION
        elif isinstance(licence, str) and licence.strip():
            # ARTICLES.md section 3 fixes `licence` as `{"spdx": <id>}`, but a universe
            # written by hand may spell it as the bare id. Read it rather than calling
            # a licence that is plainly there NOASSERTION, which would hide a term.
            spdx = licence.strip()
        else:
            spdx = NOASSERTION
        repo = str(lib.get("repo") or "")
        restrictions: list[dict] = []
        why = spdx_restriction(spdx)
        if why is not None:
            restrictions.append({"source": "spdx", "reason": why})
        mine = _repo_of_url(repo)
        for excluded in exclusions:
            same_repo = bool(mine) and mine in set(excluded["repos"])
            if not (same_repo or _versioned_sibling(lib_id, excluded["id"])):
                continue
            how = ("from the same repository as this one" if same_repo
                   else "which is another version of the same library")
            restrictions.append({
                "source": "universe.excluded",
                "reason": f"the universe excludes {excluded['id']} {how} for a licence "
                          f"reason — {excluded['reason']}"})
        if lib_id in stated:
            restrictions.append({"source": "operator", "reason": stated[lib_id]})
        rows.append({
            "id": lib_id,
            "repo": repo or None,
            "commit": str(lib.get("commit") or "") or None,
            "spdx": spdx,
            "licence_recorded": spdx != NOASSERTION,
            "restricted": bool(restrictions),
            "restrictions": restrictions,
        })
    rows.sort(key=lambda row: row["id"])
    for name in sorted(stated):
        if name not in {row["id"] for row in rows}:
            raise PackageProblem(
                f"--restricted names {name!r} and the universe has no such library "
                f"(it has {len(rows)}: {', '.join(r['id'] for r in rows[:6])})")
    # A library with no licence recorded is **not** a refusal: NOASSERTION is a
    # legitimate thing to write down, and the point of writing it down is that
    # somebody then goes and finds out. The command's exit code says so instead.
    return rows


def spdx_document(universe: dict, rows: list[dict], *, bundle: str,
                  created: str | None) -> dict:
    """The licence manifest, one SPDX-shaped package per library.

    ``filesAnalyzed`` is false and `licenseConcluded` is ``NOASSERTION`` throughout:
    this packager read a universe's recorded licence, it did not scan a repository, and
    saying otherwise in an SPDX field would be a claim nobody here checked.
    """
    return {
        "schema": LICENCE_SCHEMA,
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": bundle,
        "created": created,
        "creators": ["Tool: scripts/pipeline/package_index.py"],
        "comment": ("Declared licences are the universe's own licence.spdx per library; "
                    "nothing here was concluded from a file scan, so every "
                    "licenseConcluded is NOASSERTION. A library the universe records no "
                    "licence for is NOASSERTION in licenseDeclared too and is listed in "
                    "no_licence_recorded."),
        "universe": {"id": universe.get("id"),
                     "universe_sha256": universe.get("universe_sha256"),
                     "frozen": universe.get("frozen")},
        "libraries": len(rows),
        "no_licence_recorded": [row["id"] for row in rows if not row["licence_recorded"]],
        "use_restricted": [row["id"] for row in rows if row["restricted"]],
        "packages": [{
            "SPDXID": f"SPDXRef-Package-{row['id']}",
            "name": row["id"],
            "downloadLocation": row["repo"] or "NOASSERTION",
            "versionInfo": row["commit"] or "NOASSERTION",
            "filesAnalyzed": False,
            "licenseDeclared": row["spdx"],
            "licenseConcluded": NOASSERTION,
            "wikilean": {"restricted": row["restricted"],
                         "restrictions": row["restrictions"]},
        } for row in rows],
    }


def licences_markdown(rows: list[dict], universe: dict,
                      texts: dict[str, list[str]] | None = None) -> str:
    """The same table, for whoever reads it beside the download."""
    head = (f"# Licences — the declaration index and search corpus of "
            f"{universe.get('id') or 'this universe'}\n\n"
            "One row per library of the universe. **`spdx` is the licence the universe "
            "records**, not a licence anybody scanned for here; `NOASSERTION` means the "
            "universe records none and somebody has to go and find out before these "
            "bytes are published.\n\n"
            "`search/signatures.tsv` and `search/docstrings.tsv` carry **Lean source "
            "text** from these libraries, under these licences. `names/` carries none: "
            "a declaration's name, kind, module and line only.\n\n"
            "| library | SPDX | commit | repository | use restricted |\n"
            "|---|---|---|---|---|\n")
    body = "".join(
        f"| `{row['id']}` | {row['spdx']} | `{(row['commit'] or '')[:12]}` | "
        f"{row['repo'] or '—'} | {'**yes**' if row['restricted'] else 'no'} |\n"
        for row in rows)
    flagged = [row for row in rows if row["restricted"]]
    unknown = [row for row in rows if not row["licence_recorded"]]
    tail = ""
    if flagged:
        tail += "\n## Libraries whose use is restricted\n\n"
        for row in flagged:
            tail += f"- **`{row['id']}`** ({row['spdx']})\n"
            for restriction in row["restrictions"]:
                tail += f"  - *{restriction['source']}*: {restriction['reason']}\n"
        tail += ("\nPublishing either of the two source-text files above is a "
                 "distribution of these libraries' text. Decide per library: exclude "
                 "it, or publish it with the term stated beside the download.\n")
    if unknown:
        tail += ("\n## Libraries with no licence recorded (`NOASSERTION`)\n\n"
                 + "".join(f"- `{row['id']}`\n" for row in unknown)
                 + "\nNo licence recorded is not a permission. Each of these needs its "
                   "SPDX id put into the universe before the bundle is published.\n")
    if texts:
        tail += ("\n## Licence texts\n\nEach library's own licence files, from its "
                 "repository at the commit above:\n\n"
                 + "".join(f"- `{name}`: " + ", ".join(f"[{Path(rel).name}]({rel})"
                                                      for rel in texts[name]) + "\n"
                           for name in sorted(texts)))
    return head + body + tail


# ---- the bundle ------------------------------------------------------------------------


NAMES_README = """\
# The names-only subset

`declarations.tsv` and `wikidata.tsv` of the search corpus, and nothing else.

| file | columns |
|---|---|
| `declarations.tsv` | `library` · `declaration` · `kind` · `proof_status` · `module` · `path:line` |
| `wikidata.tsv` | `library` · `declaration` · a `@[wikidata]` QID it carries |

**No Lean source text is in these two files** — no signature, no docstring, no proof.
A declaration's name, kind, proof status and location are facts about a public
repository at a public commit. That is why this subset is kept apart from
`../search/`, which does carry source text under each library's own licence
(`../LICENCES.md`).

This is the subset the site's declaration autocomplete should be built from.
"""

BUNDLE_README = """\
# WikiLean declaration index bundle

One universe's declaration index and search corpus, as one versioned directory with a
sha256 for every file (`MANIFEST.json`) and a licence record for every library
(`LICENCES.spdx.json`, `LICENCES.md`).

| directory | what it is | who reads it |
|---|---|---|
| `index/` | `decl-index.jsonl`, the declaration index | the tally (stage 8) and the gate (stage 7) join proof statuses from it |
| `search/` | the tab-separated search corpus | a sandboxed worker Greps it; **it carries Lean source text** |
| `names/` | `declarations.tsv` and `wikidata.tsv` only | the site's autocomplete; **no source text** |

Check what you downloaded before you use it:

```sh
python3 -c "
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
m = json.loads((root / 'MANIFEST.json').read_text())
bad = []
for rel, entry in sorted(m['files'].items()):
    h = hashlib.sha256()
    with open(root / rel, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    if h.hexdigest() != entry['sha256']:
        bad.append(rel)
print('OK' if not bad else 'MISMATCH: ' + ', '.join(bad))
" <this directory>
```

Nothing in this bundle was uploaded by the command that wrote it. Where it is
published, and how a contributor is given it, is the operator's decision.
"""


def _place(source: Path, target: Path, *, link: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()
    if link:
        try:
            os.link(source, target)
            return
        except OSError:
            pass
    shutil.copyfile(source, target)


#: The directories a bundle is made of. Everything else in the target directory is
#: somebody else's, which is why a target holding anything else is refused.
PARTS = (INDEX_DIR, SEARCH_DIR, NAMES_DIR, LICENCE_TEXTS_DIR)
#: And the files beside them.
PART_FILES = (MANIFEST_NAME, LICENCES_JSON, LICENCES_MD, README_NAME)


def _within(path: Path, directory: Path) -> bool:
    """Is ``path`` ``directory`` itself, or under it?"""
    return path == directory or directory in path.parents


def _is_a_bundle(out: Path) -> bool:
    """A directory this packager may write again: an earlier bundle, or its wreckage.

    A finished bundle has a ``MANIFEST.json``. One whose write failed part way has
    only some of the parts and no manifest, and refusing *that* would mean a copy
    interrupted by a full disk could never be retried without a manual ``rm -rf``.
    Anything else in the directory belongs to somebody else and is left alone.
    """
    if (out / MANIFEST_NAME).is_file():
        return True
    return all(entry.name in PARTS or entry.name in PART_FILES
               for entry in out.iterdir())


def licence_texts(directory: Path, rows: list[dict]) -> dict[str, list[Path]]:
    """Each library's licence files under ``directory/<library id>/``, refused if any lacks one.

    Only regular files named like a licence or a notice are taken, never globbed by
    hand: a stray file beside them does not ride into a published bundle. A
    subdirectory that names no library of the universe is refused too, since a typo in
    a library id would otherwise leave that library's real texts out unnoticed.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise PackageProblem(f"--licence-texts {directory} is not a directory")
    ids = [row["id"] for row in rows]
    strays = sorted(entry.name for entry in directory.iterdir()
                    if entry.name not in ids and not entry.name.startswith("."))
    if strays:
        raise PackageProblem(
            f"--licence-texts {directory} holds {', '.join(strays[:5])}, which name(s) no "
            "library of the universe; each subdirectory is one library id")
    out: dict[str, list[Path]] = {}
    missing = []
    for lib_id in ids:
        folder = directory / lib_id
        found = sorted(entry for entry in folder.iterdir()
                       if entry.is_file() and not entry.is_symlink()
                       and (_LICENCE_FILE.match(entry.name) or _NOTICE_FILE.match(entry.name))
                       ) if folder.is_dir() and not folder.is_symlink() else []
        if not any(_LICENCE_FILE.match(entry.name) for entry in found):
            missing.append(lib_id)
            continue
        out[lib_id] = found
    if missing:
        raise PackageProblem(
            f"--licence-texts has no LICENSE, LICENCE or COPYING file for {len(missing)} "
            f"librar(ies): {', '.join(missing[:8])}{' ...' if len(missing) > 8 else ''}. Copy "
            "each one from the library's repository at its pinned commit into "
            f"{directory}/<library id>/; a bundle handed out without its licences is "
            "refused here rather than published")
    return out


def _without_paths(value):
    """The converter's provenance with every absolute ``path`` or ``dir`` cut to its name.

    The shas and counts are what a reader can check; where the operator kept the corpus
    on their own machine is not, and a published manifest has no business naming it.
    """
    if isinstance(value, dict):
        return {key: (Path(item).name
                      if key in ("path", "dir") and isinstance(item, str)
                      and (os.path.isabs(item) or item.startswith("~"))
                      else _without_paths(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_without_paths(item) for item in value]
    return value


def index_universe(index: Path) -> str | None:
    """The ``universe_sha256`` the index's header line states, or None if it states none."""
    with open(index, encoding="utf-8") as handle:
        first = handle.readline()
    try:
        header = json.loads(first)
    except ValueError:
        return None
    value = header.get("universe_sha256") if isinstance(header, dict) else None
    return str(value) if value else None


def default_version(universe: dict, index_sha: str) -> str:
    """A version nobody has to remember: the universe and the index's own sha256.

    Content-derived on purpose. Two runs of the converter over one corpus write one
    bundle version, and a rebuilt index gets a new one without anybody choosing it.
    """
    sha = str(universe.get("universe_sha256") or "")[:12] or "nouniverse"
    return f"{sha}-{index_sha[:12]}"


def package(*, index: Path, search: Path, universe: Path, out: Path,
            version: str | None = None, provenance: Path | None = None,
            created: str | None = None, restricted: dict[str, str] | None = None,
            link: bool = False, licences: Path | None = None) -> dict:
    """Write the bundle and return its ``MANIFEST.json``."""
    index, search, out = Path(index), Path(search), Path(out)
    if not index.is_file():
        raise PackageProblem(f"no declaration index at {index}; build it with "
                             "index_from_corpus.py --out")
    if not search.is_dir():
        raise PackageProblem(f"no search corpus directory at {search}; build it with "
                             "index_from_corpus.py --search")
    for name in SEARCH_FILES:
        if not (search / name).is_file():
            raise PackageProblem(f"the search corpus has no {name} at {search / name}; "
                                 "index and search corpus are written by one command so "
                                 "they cannot drift, and half of one is not publishable")
    universe_doc = read_universe(Path(universe))
    stated = index_universe(index)
    expected = str(universe_doc.get("universe_sha256") or "")
    if stated and expected and stated != expected:
        raise PackageProblem(
            f"{index} is the index of universe {stated[:12]} and {universe} is universe "
            f"{expected[:12]}: the licences and the pins would describe another index")
    if created is not None and not vocab.CREATED_RE.match(created):
        raise PackageProblem(f"--created {created!r} is not an ISO 8601 UTC time ending in Z")

    # A bundle written over its own input eats it: _place unlinks the target before it
    # copies, and for `--index <bundle>/index/decl-index.jsonl --out <bundle>` the
    # target *is* the source. Refuse rather than discover it afterwards.
    inside = [str(path) for path in (index, search)
              if _within(Path(path).resolve(), out.resolve())]
    if inside:
        raise PackageProblem(
            f"{inside[0]} is inside {out}, and this writes its inputs into that "
            "directory: packaging a bundle over itself would delete the index it is "
            "copying. Name an --out beside the inputs, not above them")

    index_sha = sha256_file(index)
    version = version or default_version(universe_doc, index_sha)
    if not vocab.TOKEN_RE.match(version):
        raise PackageProblem(f"--version {version!r} is not {vocab.TOKEN_RULE}")
    if out.exists() and any(out.iterdir()) and not _is_a_bundle(out):
        raise PackageProblem(
            f"{out} is not empty and is not a bundle this wrote; name an empty "
            "directory, or an earlier bundle to write again. (A bundle is a directory "
            f"holding {MANIFEST_NAME}, or holding nothing but this packager's own "
            f"parts: {', '.join(sorted(PARTS))}.)")

    # Everything that can be refused is refused before half a gigabyte is copied: the
    # licence rows, the operator's own restrictions and the provenance's index sha.
    rows = library_licences(universe_doc, stated=restricted)
    texts = licence_texts(Path(licences), rows) if licences is not None else {}
    converted = None
    if provenance is not None:
        converted = json.loads(Path(provenance).read_text(encoding="utf-8"))
        if not isinstance(converted, dict):
            raise PackageProblem(f"{provenance} is not an index provenance document")
        block = converted.get("index")
        if block is not None and not isinstance(block, dict):
            raise PackageProblem(
                f"{provenance} records index as {block!r}; index_from_corpus.py writes "
                "an object with its path, sha256 and row count, and a provenance this "
                "cannot read is one that checks nothing")
        recorded = block.get("sha256") if isinstance(block, dict) else None
        if recorded is not None and str(recorded) != index_sha:
            raise PackageProblem(
                f"{provenance} records index sha256 {str(recorded)[:12]} and {index} "
                f"hashes to {index_sha[:12]}: the provenance is of another index")
        converted = _without_paths(converted)
    out.mkdir(parents=True, exist_ok=True)
    # An earlier bundle's parts go, so a renamed index or a shrunken search corpus
    # cannot leave a stale file behind for the new MANIFEST.json to vouch for.
    for name in PARTS:
        shutil.rmtree(out / name, ignore_errors=True)

    _place(index, out / INDEX_DIR / index.name, link=link)
    for name in SEARCH_FILES:
        _place(search / name, out / SEARCH_DIR / name, link=link)
    for name in NAMES_FILES:
        _place(search / name, out / NAMES_DIR / name, link=link)
    (out / NAMES_DIR / README_NAME).write_text(NAMES_README, encoding="utf-8")
    (out / README_NAME).write_text(BUNDLE_README, encoding="utf-8")
    copied: dict[str, list[str]] = {}
    for lib_id, paths in texts.items():
        for source in paths:
            rel = f"{LICENCE_TEXTS_DIR}/{lib_id}/{source.name}"
            _place(source, out / rel, link=False)
            copied.setdefault(lib_id, []).append(rel)

    bundle_id = f"{universe_doc.get('id') or 'universe'}/{version}"
    spdx = spdx_document(universe_doc, rows, bundle=bundle_id, created=created)
    (out / LICENCES_JSON).write_text(
        json.dumps(spdx, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    (out / LICENCES_MD).write_text(licences_markdown(rows, universe_doc, copied),
                                   encoding="utf-8")

    files = {}
    for path in sorted(out.rglob("*")):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        files[path.relative_to(out).as_posix()] = {"sha256": sha256_file(path),
                                                   "bytes": path.stat().st_size}
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "bundle": bundle_id,
        "version": version,
        "created": created,
        "universe": {"id": universe_doc.get("id"),
                     "universe_sha256": universe_doc.get("universe_sha256"),
                     "frozen": universe_doc.get("frozen"),
                     "libraries": len(rows)},
        "index": {"path": f"{INDEX_DIR}/{index.name}", "sha256": index_sha},
        "parts": {
            INDEX_DIR: "the declaration index the tally and the gate join on",
            SEARCH_DIR: "the search corpus a sandboxed worker Greps; carries Lean "
                        "source text under each library's own licence",
            NAMES_DIR: "declarations and @[wikidata] tags only; no Lean source text",
        },
        "source_text_files": [f"{SEARCH_DIR}/{name}" for name in SOURCE_TEXT_FILES],
        "licences": {
            "document": LICENCES_JSON,
            "libraries": len(rows),
            "no_licence_recorded": spdx["no_licence_recorded"],
            "use_restricted": spdx["use_restricted"],
            # Each library's own licence files, by library id; empty without --licence-texts,
            # and then the bundle is for reading here, not for handing out.
            "texts": dict(sorted(copied.items())),
        },
        "converted": converted,
        "bytes": sum(entry["bytes"] for entry in files.values()),
        "files": dict(sorted(files.items())),
        "uploaded": False,
        "note": "Nothing here was uploaded. Publication is the operator's decision.",
    }
    (out / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def describe(manifest: dict) -> str:
    """What the command prints: the sizes, and anything the licences flag."""
    files = manifest["files"]
    lines = [f"bundle {manifest['bundle']}: {len(files)} file(s), "
             f"{manifest['bytes'] / (1 << 20):.1f} MiB"]
    for rel in sorted(files):
        lines.append(f"  {files[rel]['bytes'] / (1 << 20):9.2f} MiB  {rel}")
    licences = manifest["licences"]
    lines.append(f"  licences: {licences['libraries']} librar(ies), "
                 f"{len(licences['no_licence_recorded'])} with none recorded, "
                 f"{len(licences['use_restricted'])} with a use restriction, "
                 f"{len(licences.get('texts') or {})} with their licence texts")
    for name in licences["use_restricted"]:
        lines.append(f"    RESTRICTED: {name}")
    for name in licences["no_licence_recorded"]:
        lines.append(f"    NOASSERTION: {name}")
    lines.append("  nothing was uploaded")
    return "\n".join(lines)


def _restricted_pairs(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for value in values:
        library, sep, reason = value.partition("=")
        if not sep or not library.strip() or not reason.strip():
            raise PackageProblem(f"--restricted takes <library>=<reason>, not {value!r}")
        out[library.strip()] = reason.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--index", required=True, type=Path,
                        help="the decl-index.jsonl index_from_corpus.py wrote")
    parser.add_argument("--search", required=True, type=Path,
                        help="the search corpus directory it wrote beside the index")
    parser.add_argument("--universe", required=True, type=Path,
                        help="the universe the index is pinned to (json, or yaml with "
                             "PyYAML); its libraries' licences go in the manifest")
    parser.add_argument("--out", required=True, type=Path, help="the bundle directory")
    parser.add_argument("--licence-texts", type=Path, metavar="DIR",
                        help="DIR/<library id>/ holds each library's LICENSE or COPYING "
                             "file and any NOTICE, from its repository at the pinned "
                             "commit; required for a bundle that is handed out")
    parser.add_argument("--version", default=None,
                        help="the bundle version (default: the universe and index shas)")
    parser.add_argument("--provenance", type=Path,
                        help="index_from_corpus.py's provenance, carried in the manifest")
    parser.add_argument("--created", default=None,
                        help="an ISO 8601 UTC time for the manifest; omitted = null, "
                             "which keeps the bundle a function of its inputs")
    parser.add_argument("--restricted", action="append", default=[], metavar="LIB=REASON",
                        help="state a use restriction on a library (repeatable)")
    parser.add_argument("--link", action="store_true",
                        help="hard-link the large files instead of copying them; only "
                             "for a bundle that is read and then thrown away")
    args = parser.parse_args(argv)
    try:
        manifest = package(index=args.index, search=args.search, universe=args.universe,
                           out=args.out, version=args.version, provenance=args.provenance,
                           created=args.created,
                           restricted=_restricted_pairs(args.restricted), link=args.link,
                           licences=args.licence_texts)
    except (PackageProblem, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(describe(manifest))
    # The bundle's identity: the contributor bucket keys it by this, and the Worker
    # serves it by this (scripts/articles/upload_contrib.py).
    print(f"  manifest sha256 {sha256_file(Path(args.out) / MANIFEST_NAME)}")
    licences = manifest["licences"]
    return 1 if (licences["use_restricted"] or licences["no_licence_recorded"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
