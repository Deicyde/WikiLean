#!/usr/bin/env python3
"""Build WikiLean's read-only formalization-atlas store from one sealed locate record set.

New code. Design and contracts: docs/ATLAS.md. The input is the sealed record set
the formal-companion skill's locate pipeline exports, in exactly the record-set
format the two sides agreed (docs/ATLAS.md section 3: its proposal
docs/proposals/wikipedia-locate.md §2.3, §2.6, §2.7 and §11.1, and its
references/locate-documents.md §6, made precise); any other shape is refused with
a message naming the key or rule. The output is a static tree the Worker reads
through the optional ATLAS binding (wiki/src/atlas.ts):

    <out>/atlas/v1/current.json                 campaign, universe, counts, build time
    <out>/atlas/v1/manifest.json                every other file with bytes and sha256
    <out>/atlas/v1/universes/<universe-sha>.json the universe: libraries, repos, commits
    <out>/atlas/v1/articles/<pageid>.json       one article's view model
    <out>/atlas/v1/records/<pageid>.jsonl       that article's Literature Links records, verbatim
    <out>/atlas/v1/index/<shard>.json           title -> page id + summary, hash-sharded

Usage (Python 3.12, standard library plus the CI's pinned jsonschema, through
site/native_annotations.py):

    python3 scripts/atlas/build_store.py --record-set <set-dir> --out <store-dir>
    python3 scripts/atlas/build_store.py --record-set <set-dir> --out <store-dir> --check
    python3 scripts/atlas/build_store.py --record-set <set-dir> --out <store-dir> --replace

The build is a pure function of the record set's bytes (and of --built-at, which
defaults to SEAL.json's `created`), so --check rebuilds in memory and compares
byte for byte. Exit codes: 0 success, 1 --check found differences, 2 the record
set was refused, 3 the builder itself failed (a bug; the store is untouched). Nothing here touches the network, D1 or any Worker binding;
uploading the tree is a separate operator action (docs/ATLAS.md, "Publishing").
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "wiki" / "src" / "vendor" / "literature-links"

LOCATE = "org.mathlib-initiative.formal-companion.locate"
SEAL_SCHEMA = "cf-locate-recordset/1"
STORE_PREFIX = "atlas/v1"
ARTICLE_SCHEMA = "wikilean.atlas.article/1"
CURRENT_SCHEMA = "wikilean.atlas.current/1"
UNIVERSE_SCHEMA = "wikilean.atlas.universe/1"
INDEX_SCHEMA = "wikilean.atlas.index/1"
MANIFEST_SCHEMA = "wikilean.atlas.manifest/1"
BUILDER = "scripts/atlas/build_store.py/1"

# Index shards: the first INDEX_HEX hex digits of sha256(title_key). Part of the
# v1 store contract (wiki/src/atlas.ts computes the same shard for a title);
# changing it is a v2 store.
INDEX_HEX = 1
MAX_INDEX_SHARD_BYTES = 1024 * 1024
# Readers look fields up by name, so a field may be appended within v1.
INDEX_FIELDS = ["title", "pageid", "revision", "passages", "formalized", "statement_only",
                "partial", "not_formalized", "unresolved", "importance", "libraries"]

# Vocabularies of the locate proposal (§2.2, §2.4, §2.6, §2.7, §5).
KINDS = frozenset({"definition", "theorem", "conjecture", "example", "construction", "notation", "other"})
PROMINENCES = frozenset({"subject", "central", "supporting"})
LOCATION_RELATIONS = ("exactMatch", "closeMatch", "broadMatch", "narrowMatch", "hasPart", "relatedMatch")
PART_RELATIONS = frozenset({"hasPart", "narrowMatch"})
PROOF_STATUSES = frozenset({"proved", "statement_only", "axiom_dependent", "wanted", "definition"})
# Proof statuses that state something without a finished proof (proposal §2.6's statement_only row).
UNPROVED = frozenset({"statement_only", "wanted", "axiom_dependent"})
# Which row of proposal §2.6's table judges a passage kind. The table names theorem,
# conjecture and definition passages only; the rest is decided in formal-companion's
# references/vocabulary.md §1.11 (cflib.vocab THEOREM_/DEFINITION_/EITHER_PASSAGE_KINDS):
# a construction or a notation introduces an object or a symbol, so it is a definition
# passage, and an example or an `other` passage is judged by both rows, the better winning.
THEOREM_KINDS = frozenset({"theorem", "conjecture"})
DEFINITION_KINDS = frozenset({"definition", "construction", "notation"})
EITHER_KINDS = frozenset({"example", "other"})
# A library location counts as complete when its declaration is proved or is a definition.
COMPLETE = frozenset({"proved", "definition"})
# The maintainer's decision D1 (2026-10-18; formal-companion references/vocabulary.md §1.11 and
# docs/ATLAS.md section 3, check 6), verbatim: "D1: yes. Formalized when the hasPart locations
# together cover every clause (challenger confirms coverage), and when an exact/closeMatch is to
# an equivalence or data declaration that states the theorem (e.g. CRT's RingEquiv)."
# The relations whose match to data (a definition without the statement flag) formalizes a theorem:
DATA_MATCH_RELATIONS = frozenset({"exactMatch", "closeMatch"})
# A coverage claim: the passage's hasPart locations that together state every clause. In an
# exported record it is extensions[locate].coverage = {complete: true, parts: [location_key, ...],
# challenge}, on every record of the passage or on none; it counts only once it survived its
# challenge, and a claim the challenger refutes is removed, its parts staying hasPart.
COVERAGE_KEYS = ("complete", "parts", "challenge")
COVERAGE_RELATION = "hasPart"
MIN_COVERAGE_PARTS = 2
COVERAGE_SURVIVED = "survived"
# derive_status's basis when the coverage claim, not one location, justifies the status.
COVERAGE_BASIS = "coverage"
# The reading of D1 the lead set in parity round three (2026-10-18), which resolves the open
# questions Q1-Q3 (docs/ATLAS.md, check 6 and contract note 11; formal-companion
# references/vocabulary.md §1.11): a survived claim acts, for derive_status, as ONE more location
# of this relation, a fixed stand-in rather than any one part (coverage_location): a proved
# declaration on the theorem row and a definition on the definition row when every part is
# finished, a statement_only declaration on both rows otherwise.
COVERAGE_ACTS_AS = "exactMatch"
# Confirming a claim confirms its parts: in the view model, a part whose own challenge is one of
# these (no challenge judged it on its own) reads survived once its claim survived. A part another
# verdict judged keeps that verdict, and a challenge_pending part stays pending: a prominence
# overturn to subject or central put it under a rule requiring its own verdict, and the reopen
# wins over the claim (formal-companion review 4).
CONFIRMED_WITH_CLAIM = frozenset({"not_required", "sampled_out"})
# The challenge verdicts (proposal §2.7 and §5; cflib.vocab CHALLENGE_VERDICTS). The last three
# are records no challenge judged (CHALLENGE_UNJUDGED), and the Worker reads each as unchallenged:
# challenge_pending is a record a prominence overturn to subject or central put under a rule
# after its challenge ran, so a challenge is owed and none has judged it yet.
CHALLENGES = frozenset({"survived", "downgraded", "not_required", "sampled_out", "challenge_pending"})
CONFIDENCES = frozenset({"high", "medium", "low"})
STATUSES = ("formalized", "statement_only", "partial", "not_formalized")
# Best first, for "the best such location" (cflib.vocab _STATUS_RANK).
STATUS_RANK = {status: rank for rank, status in enumerate(STATUSES)}
SUMMARY_STATUSES = (*STATUSES, "unresolved")
MAX_LOCATIONS = 5          # K, proposal §2.7
QUOTE_MAX_WORDS = 12       # exported quote caps, proposal §2.7 and §10
AFFIX_MAX_CHARS = 32
MIN_ABSENCE_QUERIES = 3    # gate rule 5, proposal §4

HEX40 = re.compile(r"^[0-9a-f]{40}\Z")
HEX64 = re.compile(r"^[0-9a-f]{64}\Z")
ITEM_RE = re.compile(r"^In-text #[0-9a-f]{8,16}\Z")
SOURCE_ID_RE = re.compile(r"^enwiki-([1-9][0-9]*)-([1-9][0-9]*)\Z")
# A token (SEAL's set_id, universe.id and sourceset.id, a library or exclusion id): a letter or
# digit, then letters, digits and ._:-, at most 128 characters in all (docs/ATLAS.md section 3).
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
TOKEN_RULE = "a token: a letter or digit, then letters, digits and ._:- (at most 128 characters)"
# SEAL.json's campaign: the campaign id, or for a run outside any campaign
# "run:<run-id>[+<challenge-run-id>]". A campaign id never starts "run:", and a run id
# has no ":" or "+", so the two forms cannot be confused. wiki/src/atlas.ts CAMPAIGN_RE
# is the same pattern (the feedback route checks a posted campaign against it).
RUN_ID = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
CAMPAIGN_RE = re.compile(rf"^(?:run:{RUN_ID}(?:\+{RUN_ID})?|(?!run:)[A-Za-z0-9][A-Za-z0-9._:-]{{0,127}})\Z")
# The agreed record-set format (docs/ATLAS.md section 3): every key of SEAL.json and of
# universe.json, each library and each exclusion. Any other key is refused by name.
SEAL_KEYS = ("schema", "set_id", "campaign", "universe", "sourceset", "skill_commit", "created",
             "byte_budget", "files")
SEAL_UNIVERSE_KEYS = ("id", "universe_sha256")
SEAL_SOURCESET_KEYS = ("id", "manifest_sha256")
UNIVERSE_KEYS = ("id", "universe_sha256", "frozen", "libraries", "excluded")
LIBRARY_KEYS = ("id", "brain_lib", "repo", "commit", "toolchain", "roots", "licence")
LIBRARY_OPTIONAL = ("display_name",)
EXCLUDED_KEYS = ("id", "reason")
# Libraries are keyed by id, and a repository may be several of them: proposal §2.3 lets a
# universe split one repository into libraries, one root each (lean-core's Init and Std at one
# commit), and WikiLean agreed to it on 2026-10-19 (docs/ATLAS.md, contract note 13). So only a
# true duplicate is refused: an id twice, or one root of one repository in two libraries (or
# twice in one), whatever their commits. A repository and a root are compared as written.
LIBRARY_RULE = ("libraries are keyed by id, each id once, and a repository may be several libraries, "
                "but each of its roots belongs to one library (docs/ATLAS.md section 3)")
# An absence names what was searched: each distinct (repo, commit) of the universe by one
# minimal lean end, and nothing else, so libraries split from one repository at one commit are
# one end (formal-companion 124a587, records.absence_ends).
ABSENCE_END_KEYS = ("type", "uri", "version")
# The agreed keys of the locate extension at each of the three places a record carries it
# (docs/ATLAS.md section 3, "The locate extension's keys"): the record itself, by its shape; its
# subject end; and a location's lean end. Any other key is refused, naming it, the place and the
# record id, and so is any extension namespace beside the locate one. A required key that is
# missing is named by the check of its value.
RECORD_EXT_KEYS = ("item", "universe", "universe_sha256", "challenge", "section", "prominence")
# coverage is a passage field (check 6): a claim on an absence or an unresolved record is refused
# there, because every part of a claim is a hasPart location of its own passage.
RECORD_EXT_OPTIONAL = ("coverage",)
SHAPE_EXT_KEYS = {"location": ("location_key", "rank", "confidence"), "absence": ("receipt",), "unresolved": ()}
SHAPE_LEVELS = {"location": "a location record's locate extension", "absence": "an absence record's locate extension",
                "unresolved": "an unresolved record's locate extension", None: "the record's locate extension"}
RECEIPT_KEYS = ("sha256", "queries", "candidates")
SUBJECT_EXT_KEYS = ("pageid", "html_sha256", "renderer", "span_sha256", "licence")
# The passage's whole span and the sha of the layer it indexes, in the shape formal-companion's
# split-frontend commit 722b30f ("F5") writes them: optional, and together or not at all.
SUBJECT_EXT_OPTIONAL = ("span", "layer_sha256")
LEAN_EXT_KEYS = ("library", "path", "line", "proof_status")
LEAN_EXT_OPTIONAL = ("end_line", "statement")
SPAN_RULE = ("span must be the passage's half-open code-point offsets on the layer, [start, end] with "
             "integers 0 <= start < end (formal-companion 722b30f)")
CREATED_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z\Z")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
# Whitespace for title keys, spelled out so wiki/src/atlas.ts matches it exactly.
TITLE_SPACE = re.compile(r"[ \t\n\r\f\v\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000_]+")
WORD_SPACE = re.compile(r"\s+")
# A library repository: https, a dotted host (no port, no user), then one to four path segments
# of 1-100 letters, digits and _.~- each, none of them "." or "..", with no trailing slash, query
# or fragment (docs/ATLAS.md section 3). The Worker builds links from it, so it is strict.
REPO_RE = re.compile(r"^https://[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?:/[A-Za-z0-9_.~-]{1,100}){1,4}\Z")
REPO_RULE = ("an https repository URL: a dotted host with no port, then one to four path segments of "
             "1-100 letters, digits and _.~- each, none of them '.' or '..', and no trailing slash, "
             "query or fragment (e.g. https://github.com/<owner>/<repo>)")
# Length caps on universe.json strings (docs/ATLAS.md section 3): a library's brain_lib, toolchain
# and each root are names, and an exclusion's reason is a sentence or two of prose.
MAX_NAME_CHARS = 300
MAX_REASON_CHARS = 1000
# A library's display_name: printable ASCII letters, digits, spaces and a few marks, so no
# invisible or look-alike character can make it read as another library's name
# (wiki/src/libraries.ts applies the same pattern and ignores a name that fails it).
DISPLAY_NAME_RE = re.compile(r"^[A-Za-z0-9 ,.()\u00b7/+:'&_-]{1,100}\Z")
# sources.jsonl `renderer`, e.g. "web-html/1".
RENDERER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,63}\Z")


class AtlasError(Exception):
    """The record set breaks the input contract; the message names every problem found."""


class StatusRefused(ValueError):
    """derive_passage_status's refusal: inputs the builder refuses in a record set (the producer's CfError)."""


# ---- small helpers -----------------------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    """The store's one JSON encoding: sorted keys, compact, UTF-8, trailing newline."""
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def _no_constants(name: str) -> Any:
    raise ValueError(f"non-finite JSON number {name}")


def _finite_float(text: str) -> float:
    # 1e400 parses as inf, which canonical() cannot write: refuse it here, as NaN is.
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"non-finite JSON number {text}")
    return value


def strict_json(text: str) -> Any:
    """Parse JSON refusing duplicate keys and non-finite numbers (the spec requires unique keys).

    Every failure is a ValueError, which the callers report as a refusal: nesting too deep
    for the parser included.
    """
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constants,
                          parse_float=_finite_float)
    except RecursionError as exc:
        raise ValueError("nested too deeply") from exc


def member(value: Any, allowed: frozenset[str] | tuple[str, ...]) -> bool:
    """`value in allowed` for a vocabulary of strings; a list or dict is simply not a member."""
    return isinstance(value, str) and value in allowed


def key_problems(value: dict, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> list[str]:
    """The agreed format's key set for one object: what is missing and what is not in it."""
    found = []
    missing = [key for key in required if key not in value]
    extra = sorted(key for key in value if key not in required and key not in optional)
    if missing:
        found.append("missing " + ", ".join(repr(key) for key in missing))
    if extra:
        found.append("unexpected " + ", ".join(repr(key) for key in extra)
                     + " (not in the agreed record-set format, docs/ATLAS.md section 3)")
    return found


def title_key(title: str) -> str:
    """A Wikipedia title in comparison form: NFC, underscores as spaces, single spaces, trimmed."""
    return TITLE_SPACE.sub(" ", unicodedata.normalize("NFC", title)).strip(" ")


def title_shard(title: str) -> str:
    return sha256_hex(title_key(title).encode("utf-8"))[:INDEX_HEX]


def safe_relative(path: str) -> bool:
    pure = PurePosixPath(path)
    return (bool(path) and not path.startswith("/") and "\\" not in path and pure.as_posix() == path
            and all(part not in {"", ".", ".."} for part in pure.parts))


def is_https(uri: Any) -> bool:
    return isinstance(uri, str) and re.match(r"^https://[A-Za-z0-9.-]+(?::[0-9]+)?/\S*$", uri) is not None


def is_repo_url(uri: Any) -> bool:
    """A repository URL the Worker can name and link: no dot segments, no query, no fragment."""
    return (isinstance(uri, str) and REPO_RE.match(uri) is not None
            and all(part not in {".", ".."} for part in uri.split("/")[3:]))


def names_revision(uri: Any, revision: int) -> bool:
    """True when `uri` is an en.wikipedia.org permanent link whose only oldid is exactly `revision`."""
    if not isinstance(uri, str):
        return False
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.scheme != "https" or parts.netloc != "en.wikipedia.org":
        return False
    if parts.path != "/w/index.php" and not parts.path.startswith("/wiki/"):
        return False
    return parse_qs(parts.query, keep_blank_values=True).get("oldid") == [str(revision)]


def schema_validator():
    """The vendored Literature Links checker, the one WikiLean already uses (site/native_annotations.py)."""
    provenance = json.loads((VENDOR / "provenance.json").read_text(encoding="utf-8"))
    schema_bytes = (VENDOR / "companion.schema.json").read_bytes()
    if sha256_hex(schema_bytes) != provenance["sha256"]:
        raise AtlasError("the vendored Literature Links schema does not match its provenance.json sha256")
    sys.path.insert(0, str(ROOT / "site"))
    try:
        from native_annotations import NativeAnnotationError, validate_link_record
    finally:
        sys.path.pop(0)
    return validate_link_record, NativeAnnotationError, provenance


# ---- the sealed record set ----------------------------------------------------------------


@dataclass
class Problems:
    """Collects contract violations so one run reports all of them (capped)."""
    items: list[str] = field(default_factory=list)
    cap: int = 200

    def add(self, where: str, message: str) -> None:
        if len(self.items) < self.cap:
            self.items.append(f"{where}: {message}")

    def raise_if_any(self) -> None:
        if self.items:
            more = "" if len(self.items) < self.cap else f"\n(stopped after {self.cap} problems)"
            raise AtlasError("\n".join(self.items) + more)


def read_record_set(set_dir: Path) -> tuple[dict, str, dict[str, bytes]]:
    """Read SEAL.json and every file it lists; refuse unlisted files, symlinks and sha mismatches."""
    problems = Problems()
    if not set_dir.is_dir():
        raise AtlasError(f"{set_dir}: not a directory")
    seal_path = set_dir / "SEAL.json"
    if not seal_path.is_file() or seal_path.is_symlink():
        raise AtlasError(f"{set_dir}: SEAL.json is missing")
    seal_bytes = seal_path.read_bytes()
    try:
        seal = strict_json(seal_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AtlasError(f"SEAL.json: not valid JSON ({exc})") from exc
    if not isinstance(seal, dict):
        raise AtlasError("SEAL.json: not an object")
    for message in key_problems(seal, SEAL_KEYS):
        problems.add("SEAL.json", message)
    # A missing key is reported once, above; the checks below judge the values present.
    if "schema" in seal and seal["schema"] != SEAL_SCHEMA:
        problems.add("SEAL.json", f"schema must be {SEAL_SCHEMA!r}")
    if "set_id" in seal and (not isinstance(seal["set_id"], str) or not TOKEN_RE.match(seal["set_id"])):
        problems.add("SEAL.json", f"set_id must be {TOKEN_RULE}")
    if "campaign" in seal and (not isinstance(seal["campaign"], str) or not CAMPAIGN_RE.match(seal["campaign"])):
        problems.add("SEAL.json", "campaign must be a non-empty token: the campaign id (letters, digits and "
                                  "._:-, at most 128, not starting 'run:'), or for a run outside any campaign "
                                  "'run:<run-id>' or 'run:<run-id>+<challenge-run-id>' (run ids of letters, "
                                  "digits and ._-, at most 64); never null")
    universe = seal.get("universe")
    if "universe" in seal and not isinstance(universe, dict):
        problems.add("SEAL.json", "universe must be {id, universe_sha256}")
    elif isinstance(universe, dict):
        for message in key_problems(universe, SEAL_UNIVERSE_KEYS):
            problems.add("SEAL.json: universe", message)
        if "id" in universe and (not isinstance(universe["id"], str) or not TOKEN_RE.match(universe["id"])):
            problems.add("SEAL.json", f"universe.id must be {TOKEN_RULE}")
        if "universe_sha256" in universe and (not isinstance(universe["universe_sha256"], str)
                                              or not HEX64.match(universe["universe_sha256"])):
            problems.add("SEAL.json", "universe.universe_sha256 must be 64 lowercase hex")
    sourceset = seal.get("sourceset")
    if "sourceset" in seal and not isinstance(sourceset, dict):
        problems.add("SEAL.json", "sourceset must be {id, manifest_sha256}")
    elif isinstance(sourceset, dict):
        for message in key_problems(sourceset, SEAL_SOURCESET_KEYS):
            problems.add("SEAL.json: sourceset", message)
        if "id" in sourceset and (not isinstance(sourceset["id"], str) or not TOKEN_RE.match(sourceset["id"])):
            problems.add("SEAL.json", f"sourceset.id must be {TOKEN_RULE}")
        if "manifest_sha256" in sourceset and (not isinstance(sourceset["manifest_sha256"], str)
                                               or not HEX64.match(sourceset["manifest_sha256"])):
            problems.add("SEAL.json", "sourceset.manifest_sha256 must be 64 lowercase hex")
    if "skill_commit" in seal and (not isinstance(seal["skill_commit"], str) or not HEX40.match(seal["skill_commit"])):
        problems.add("SEAL.json", "skill_commit must be a 40-hex commit")
    if "created" in seal and (not isinstance(seal["created"], str) or not CREATED_RE.match(seal["created"])):
        problems.add("SEAL.json", "created must be an ISO 8601 UTC time ending in Z")
    budget = seal.get("byte_budget")
    if "byte_budget" in seal and (type(budget) is not int or budget < 0):
        problems.add("SEAL.json", "byte_budget must be the per-source byte budget the export used: "
                                  "an integer, 0 when the export had none")
    listed = seal.get("files")
    if not isinstance(listed, dict) or not listed:
        problems.add("SEAL.json", "files must map each relative path to its sha256")
        listed = {}
    problems.raise_if_any()

    present: set[str] = set()
    for path in sorted(set_dir.rglob("*")):
        relative = path.relative_to(set_dir).as_posix()
        if path.is_symlink():
            problems.add(relative, "symlinks are not allowed in a sealed record set")
        elif path.is_file() and relative != "SEAL.json":
            present.add(relative)
    files: dict[str, bytes] = {}
    for relative, digest in sorted(listed.items()):
        if not isinstance(relative, str) or not safe_relative(relative):
            problems.add("SEAL.json", f"unsafe path {relative!r}")
            continue
        if not isinstance(digest, str) or not HEX64.match(digest):
            problems.add("SEAL.json", f"{relative}: sha256 must be 64 lowercase hex")
            continue
        if relative not in present:
            problems.add(relative, "listed in SEAL.json but missing")
            continue
        data = (set_dir / relative).read_bytes()
        if sha256_hex(data) != digest:
            problems.add(relative, "sha256 does not match SEAL.json")
            continue
        files[relative] = data
    for relative in sorted(present - set(listed)):
        problems.add(relative, "present but not listed in SEAL.json")
    problems.raise_if_any()
    return seal, sha256_hex(seal_bytes), files


def _string_problem(value: Any, cap: int) -> str | None:
    """Why `value` is not a non-empty string of at most `cap` characters, or None when it is."""
    if not isinstance(value, str) or not value.strip():
        return "must be a non-empty string"
    if len(value) > cap:
        return f"is {len(value)} characters, longer than {cap}"
    return None


def load_universe(seal: dict, files: dict[str, bytes], problems: Problems) -> dict:
    """universe.json in the agreed shape: a list of libraries sorted by id (docs/ATLAS.md section 3).

    Libraries are keyed by id, and one repository may be several of them (LIBRARY_RULE): only an
    id twice, or a root of one repository in two libraries, is refused.
    """
    where = "universe.json"
    if "universe.yaml" in files:
        problems.add("universe.yaml", "not part of the agreed record-set format: the universe is universe.json")
    if where not in files:
        raise AtlasError("universe.json is missing from the record set")
    try:
        raw = strict_json(files[where].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AtlasError(f"universe.json: not valid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise AtlasError(f"{where}: not an object")
    for message in key_problems(raw, UNIVERSE_KEYS):
        problems.add(where, message)
    if "sha256" in raw:
        problems.add(where, "the universe's sha is universe_sha256, not sha256")
    if raw.get("id") != seal["universe"]["id"]:
        problems.add(where, "id differs from SEAL.json universe.id")
    if raw.get("universe_sha256") != seal["universe"]["universe_sha256"]:
        problems.add(where, "universe_sha256 differs from SEAL.json universe.universe_sha256")
    frozen = raw.get("frozen")
    if not isinstance(frozen, str) or not DATE_RE.match(frozen):
        problems.add(where, "frozen must be a YYYY-MM-DD date")
    libraries_raw = raw.get("libraries")
    if isinstance(libraries_raw, dict):
        raise AtlasError(f"{where}: libraries must be a list of library objects sorted by id, "
                         "not an object keyed by library id")
    if not isinstance(libraries_raw, list) or not libraries_raw:
        raise AtlasError(f"{where}: libraries must be a non-empty list of library objects sorted by id")
    libraries: list[dict] = []
    seen_ids: set[str] = set()
    # (repo, root) -> the id of the library that holds it (LIBRARY_RULE).
    root_owner: dict[tuple[str, str], str] = {}
    previous_id: str | None = None
    for index, lib in enumerate(libraries_raw):
        at = f"{where}: libraries[{index}]"
        if not isinstance(lib, dict):
            problems.add(at, "not an object")
            continue
        # Refused by name first, so a producer learns why (docs/ATLAS.md, contract note 6).
        if "blob_url_template" in lib:
            problems.add(at, "blob_url_template is not supported: the Worker builds every source link "
                             "from the repository itself (docs/ATLAS.md, contract note 6)")
        if "src_dir" in lib:
            problems.add(at, "src_dir is not supported: a lean end's path is relative to the repository root")
        for message in key_problems({k: v for k, v in lib.items() if k not in ("blob_url_template", "src_dir")},
                                    LIBRARY_KEYS, LIBRARY_OPTIONAL):
            problems.add(at, message)
        lib_id, repo, commit = lib.get("id"), lib.get("repo"), lib.get("commit")
        if not isinstance(lib_id, str) or not TOKEN_RE.match(lib_id):
            problems.add(at, f"id must be {TOKEN_RULE}")
            continue
        if lib_id in seen_ids:
            problems.add(at, f"library id {lib_id!r} appears twice: {LIBRARY_RULE}")
        elif previous_id is not None and lib_id < previous_id:
            problems.add(at, f"libraries must be sorted by id, each once ({lib_id!r} comes after {previous_id!r})")
        seen_ids.add(lib_id)
        previous_id = lib_id if previous_id is None or lib_id > previous_id else previous_id
        if not is_repo_url(repo):
            problems.add(at, f"repo must be {REPO_RULE}")
        if not isinstance(commit, str) or not HEX40.match(commit):
            problems.add(at, "commit must be a 40-hex commit")
        entry: dict[str, Any] = {"id": lib_id, "repo": repo, "commit": commit}
        for key in ("brain_lib", "toolchain"):
            message = _string_problem(lib.get(key), MAX_NAME_CHARS)
            if message:
                problems.add(at, f"{key} {message} (at most {MAX_NAME_CHARS} characters)")
            else:
                entry[key] = lib[key]
        roots = lib.get("roots")
        if (not isinstance(roots, list) or not roots
                or any(_string_problem(root, MAX_NAME_CHARS) for root in roots)):
            problems.add(at, f"roots must be a non-empty list of module roots, each a non-empty string of at "
                             f"most {MAX_NAME_CHARS} characters")
        else:
            entry["roots"] = list(roots)
            if is_repo_url(repo):
                for root in roots:
                    owner = root_owner.setdefault((repo, root), lib_id)
                    if owner != lib_id:
                        problems.add(at, f"root {root!r} of {repo} is also library {owner!r}'s: {LIBRARY_RULE}")
                if len(set(roots)) != len(roots):
                    problems.add(at, f"roots names one root twice: {LIBRARY_RULE}")
        licence = lib.get("licence")
        if (not isinstance(licence, dict) or set(licence) != {"spdx"}
                or _string_problem(licence["spdx"], MAX_NAME_CHARS)):
            problems.add(at, "licence must be {spdx: <SPDX id>}, never null: a library goes into a sealed "
                             "universe with its licence known")
        else:
            entry["licence"] = licence["spdx"]
        display = lib.get("display_name")
        if display is not None:
            if not isinstance(display, str) or not DISPLAY_NAME_RE.match(display):
                problems.add(at, "display_name must be at most 100 printable ASCII letters, digits, spaces "
                                 "and ,.()/+:'&_- or \u00b7 (no invisible or look-alike characters), or null")
            else:
                entry["display_name"] = display
        libraries.append(entry)
    excluded = []
    excluded_raw = raw.get("excluded")
    if not isinstance(excluded_raw, list):
        problems.add(where, "excluded must be a list of {id, reason} (empty when nothing is excluded)")
        excluded_raw = []
    for index, item in enumerate(excluded_raw):
        at = f"{where}: excluded[{index}]"
        if not isinstance(item, dict):
            problems.add(at, "must be {id, reason}")
            continue
        for message in key_problems(item, EXCLUDED_KEYS):
            problems.add(at, message)
        good = True
        if not isinstance(item.get("id"), str) or not TOKEN_RE.match(item["id"]):
            problems.add(at, f"id must be {TOKEN_RULE}")
            good = False
        message = _string_problem(item.get("reason"), MAX_REASON_CHARS)
        if message:
            problems.add(at, f"reason {message} (at most {MAX_REASON_CHARS} characters)")
            good = False
        if good:
            excluded.append({"id": item["id"], "reason": item["reason"]})
    return {"schema": UNIVERSE_SCHEMA, "id": raw.get("id"), "sha256": raw.get("universe_sha256"),
            "frozen": frozen, "libraries": libraries, "excluded": excluded}


def load_sources(files: dict[str, bytes], problems: Problems) -> list[dict]:
    if "sources.jsonl" not in files:
        raise AtlasError("sources.jsonl is missing from the record set")
    sources: list[dict] = []
    seen_ids: set[str] = set()
    seen_pages: set[int] = set()
    seen_titles: dict[str, int] = {}
    for number, line in enumerate(files["sources.jsonl"].decode("utf-8").splitlines(), 1):
        at = f"sources.jsonl:{number}"
        if not line.strip():
            problems.add(at, "blank line")
            continue
        try:
            row = strict_json(line)
        except ValueError as exc:
            problems.add(at, f"not valid JSON ({exc})")
            continue
        if not isinstance(row, dict):
            problems.add(at, "not an object")
            continue
        source_id, pageid, revision = row.get("source_id"), row.get("pageid"), row.get("revision")
        match = SOURCE_ID_RE.match(source_id) if isinstance(source_id, str) else None
        if not match or type(pageid) is not int or type(revision) is not int \
                or match.group(1) != str(pageid) or match.group(2) != str(revision):
            problems.add(at, "source_id must be enwiki-<pageid>-<revision> and match pageid and revision")
            continue
        if source_id in seen_ids or pageid in seen_pages:
            problems.add(at, f"page {pageid} appears twice (one revision per page per record set)")
            continue
        seen_ids.add(source_id)
        seen_pages.add(pageid)
        title = row.get("title")
        if not isinstance(title, str) or not title_key(title) or len(title) > 512:
            problems.add(at, "title must be a non-empty string")
            continue
        # The index and the article-page box look a page up by title key, so two pages
        # with one key would make one of them unreachable by title.
        other = seen_titles.setdefault(title_key(title), pageid)
        if other != pageid:
            problems.add(at, f"title {title!r} has the same title key as page {other} (one page per title)")
        for key in ("html_sha256", "layer_sha256"):
            if not HEX64.match(str(row.get(key, ""))):
                problems.add(at, f"{key} must be 64 hex")
        wikidata = row.get("wikidata")
        if wikidata is not None and (not isinstance(wikidata, str) or not re.match(r"^Q[1-9][0-9]*$", wikidata)):
            problems.add(at, "wikidata must be a QID or null")
        assessment = row.get("assessment")
        clean_assessment = None
        if isinstance(assessment, dict):
            clean_assessment = {key: assessment[key].strip().lower() if key == "importance" else assessment[key].strip()
                                for key in ("class", "importance")
                                if isinstance(assessment.get(key), str) and assessment[key].strip()}
        elif assessment is not None:
            problems.add(at, "assessment must be an object or null")
        renderer = row.get("renderer")
        if renderer is not None and (not isinstance(renderer, str) or not RENDERER_RE.match(renderer)):
            problems.add(at, "renderer must be a short token such as web-html/1, or null")
            renderer = None
        sources.append({"source_id": source_id, "pageid": pageid, "revision": revision, "title": title,
                        "wikidata": wikidata, "html_sha256": row.get("html_sha256"),
                        "layer_sha256": row.get("layer_sha256"), "renderer": renderer,
                        "assessment": clean_assessment or None})
    record_files = {p for p in files if p.startswith("records/")}
    expected = {f"records/{s['source_id']}.jsonl" for s in sources}
    for missing in sorted(expected - record_files):
        problems.add(missing, "listed source has no records file")
    for extra in sorted(record_files - expected):
        problems.add(extra, "records file for a source not in sources.jsonl")
    for path in sorted(files):
        top = path.split("/", 1)[0]
        # universe.yaml (no longer part of the format) is reported by load_universe.
        if path not in ("universe.json", "universe.yaml", "sources.jsonl") and top not in ("records", "receipts"):
            problems.add(path, "unexpected file (a record set holds universe, sources, records/ and receipts/)")
    return sources


# ---- records -------------------------------------------------------------------------------


@dataclass
class Context:
    universe: dict
    files: dict[str, bytes]
    validate: Any
    schema_error: Any
    location_keys: set[str] = field(default_factory=set)
    receipts_used: set[str] = field(default_factory=set)


def _locate_ext(node: dict) -> dict | None:
    ext = node.get("extensions")
    value = ext.get(LOCATE) if isinstance(ext, dict) else None
    return value if isinstance(value, dict) else None


def record_label(record: dict) -> str:
    """How a message names a record: its id (the qualified passage id), which the schema makes a string."""
    rid = record.get("id")
    return rid if isinstance(rid, str) else "(without an id)"


def _named(keys: list[str]) -> str:
    return ", ".join(repr(key) for key in keys)


def unexpected_keys(value: dict, allowed: tuple[str, ...], level: str, rid: str) -> list[str]:
    """An unknown key at one level of a record, named with that level and the record's id."""
    extra = sorted(key for key in value if key not in allowed)
    if not extra:
        return []
    return [f"record {rid}: unexpected {'key' if len(extra) == 1 else 'keys'} {_named(extra)} in {level} "
            "(not in the agreed record-set format, docs/ATLAS.md section 3)"]


def foreign_extensions(node: dict, level: str, rid: str) -> list[str]:
    """An extension namespace other than the locate one, on the record or on one of its ends."""
    extensions = node.get("extensions")
    extra = sorted(key for key in extensions if key != LOCATE) if isinstance(extensions, dict) else []
    if not extra:
        return []
    return [f"record {rid}: unexpected {'extension' if len(extra) == 1 else 'extensions'} {_named(extra)} on "
            f"{level}: the agreed record-set format carries only {LOCATE!r} (docs/ATLAS.md section 3)"]


def subject_problems(ext: dict, source: dict) -> tuple[list[int] | None, list[str]]:
    """The subject end's locate extension beyond pageid and html_sha256: (its span or None, problems).

    renderer is the source's renderer token, licence the source's SPDX id and span_sha256 the
    sha256 of the passage's span (proposal §2.7). The optional span (half-open code-point offsets
    on the layer, never text) and layer_sha256 (the sha of that layer, the source's) come together
    (formal-companion 722b30f); a malformed span is reported and dropped.
    """
    found = []
    renderer = ext.get("renderer")
    if (not isinstance(renderer, str) or not RENDERER_RE.match(renderer)
            or (source["renderer"] is not None and renderer != source["renderer"])):
        found.append("the subject end's renderer must be its source's renderer token (sources.jsonl renderer, "
                     "e.g. web-html/1)")
    message = _string_problem(ext.get("licence"), MAX_NAME_CHARS)
    if message:
        found.append(f"the subject end's licence, the source's SPDX licence id, {message} "
                     f"(at most {MAX_NAME_CHARS} characters)")
    span_sha = ext.get("span_sha256")
    if not isinstance(span_sha, str) or not HEX64.match(span_sha):
        found.append("the subject end's span_sha256, the sha256 of the passage's span, must be 64 lowercase hex")
    if ("span" in ext) != ("layer_sha256" in ext):
        found.append("span and layer_sha256 come together or not at all: a span is offsets on the layer "
                     "whose sha is layer_sha256 (formal-companion 722b30f)")
    span = None
    if "span" in ext:
        raw = ext["span"]
        if (isinstance(raw, list) and len(raw) == 2 and all(type(offset) is int for offset in raw)
                and 0 <= raw[0] < raw[1]):
            span = [raw[0], raw[1]]
        else:
            found.append(SPAN_RULE)
    if "layer_sha256" in ext:
        layer = ext["layer_sha256"]
        if not isinstance(layer, str) or not HEX64.match(layer):
            found.append("layer_sha256 must be 64 lowercase hex: the sha256 of the layer the span indexes")
        elif layer != source["layer_sha256"]:
            found.append("layer_sha256 must be the source's layer_sha256 (sources.jsonl): the span indexes "
                         "that layer")
    return span, found


def span_problems(span: list[int], quote: dict, span_sha256: Any) -> list[str]:
    """Whether a passage's quote lies in its span, and the span's text agrees with span_sha256
    wherever the quote gives that text in full.

    The export's quote is the head of its passage: exact is the span's text from its start, at
    most QUOTE_MAX_WORDS words, right-stripped, and the suffix the text right after exact
    (formal-companion scripts/locate/records.py, cap_quote). So exact is never longer than the
    span, and when the span's tail after exact is no longer than the suffix, exact and the
    suffix's first characters are the span's whole text, whose sha256 is span_sha256.
    """
    exact = quote["exact"]
    length = span[1] - span[0]
    tail = length - len(exact)
    if tail < 0:
        return [f"the quote's exact is {len(exact)} characters, longer than its span [{span[0]}, {span[1]}] "
                f"({length}): the quote is the head of its passage"]
    suffix = quote.get("suffix", "")
    if isinstance(span_sha256, str) and HEX64.match(span_sha256) and tail <= len(suffix):
        if sha256_hex((exact + suffix[:tail]).encode("utf-8")) != span_sha256:
            given = "exact" if tail == 0 else f"exact and the first {tail} characters of its suffix"
            return [f"span_sha256 is not the sha256 of the span's text, which the quote gives in full ({given}): "
                    "the span or the quote is not this passage's"]
    return []


def _quote_problems(quote: Any) -> list[str]:
    if not isinstance(quote, dict) or not isinstance(quote.get("exact"), str):
        return ["subject location must be a quote with exact"]
    found = []
    if len(WORD_SPACE.split(quote["exact"].strip())) > QUOTE_MAX_WORDS:
        found.append(f"quote.exact exceeds {QUOTE_MAX_WORDS} words (proposal §2.7 cap)")
    for affix in ("prefix", "suffix"):
        value = quote.get(affix, "")
        if not isinstance(value, str):
            found.append(f"quote.{affix} must be a string")
        elif len(value) > AFFIX_MAX_CHARS:
            found.append(f"quote.{affix} exceeds {AFFIX_MAX_CHARS} characters (proposal §2.7 cap)")
    return found


COVERAGE_RULE = ('coverage, on a passage with a claim, is {"complete": true, "parts": [<location_key>, ...], '
                 '"challenge": <challenge value>}, and is absent (never null) on a passage without one')


def coverage_claim(ext: dict, at: str, problems: Problems) -> dict | None:
    """One record's copy of its passage's coverage claim (decision D1), or None when it carries none.

    The claim's own shape is checked here: exactly complete, parts and challenge; complete is
    true; parts names at least MIN_COVERAGE_PARTS distinct location keys; challenge is a challenge
    value. That each part is a hasPart location of the same passage is checked over the passage's
    records (coverage_part_problems). A malformed claim is reported and dropped.
    """
    if "coverage" not in ext:
        return None
    claim = ext["coverage"]
    if not isinstance(claim, dict):
        problems.add(at, COVERAGE_RULE)
        return None
    found = [f"coverage: {message}" for message in key_problems(claim, COVERAGE_KEYS)]
    if "complete" in claim and claim["complete"] is not True:
        found.append("coverage.complete must be true: the claim is that the parts together state every clause")
    parts = claim.get("parts")
    if "parts" in claim:
        if not isinstance(parts, list) or not all(isinstance(key, str) and HEX64.match(key) for key in parts):
            found.append("coverage.parts must be a list of location_keys (64 hex), one per hasPart location")
        elif len(set(parts)) != len(parts):
            found.append("coverage.parts names one location twice")
        elif len(parts) < MIN_COVERAGE_PARTS:
            found.append(f"coverage.parts names {len(parts)} location(s): a coverage claim needs at least "
                         f"{MIN_COVERAGE_PARTS} hasPart locations")
    if "challenge" in claim and claim["challenge"] is None:
        # The producer's derive_status refuses one too (cflib.vocab COVERAGE_RECORD_KEYS): a claim
        # no challenge has judged yet upgrades nothing, so its holder passes no claim at all.
        found.append("coverage.challenge is null: a record's claim carries its challenge value, and an owed "
                     "claim (no challenge judged it yet) is not exported")
    elif "challenge" in claim and not member(claim["challenge"], CHALLENGES):
        found.append(f"coverage.challenge must be one of {sorted(CHALLENGES)} (only survived counts)")
    for message in found:
        problems.add(at, message)
    if found:
        return None
    return {"complete": True, "parts": list(parts), "challenge": claim["challenge"]}


def coverage_part_problems(claim: dict, locations: list[dict]) -> list[str]:
    """Why a passage's coverage claim does not name only hasPart locations of that passage."""
    relation_of = {loc["location_key"]: loc["relation"] for loc in locations if loc["location_key"]}
    found = []
    for key in claim["parts"]:
        relation = relation_of.get(key)
        if relation is None:
            found.append(f"coverage names {key}, which is no location of this passage: every part is one of "
                         f"its {COVERAGE_RELATION} locations")
        elif relation != COVERAGE_RELATION:
            found.append(f"coverage names {key}, whose relation is {relation}: every part is a "
                         f"{COVERAGE_RELATION} location of this passage")
    return found


def classify_record(record: Any, source: dict, ctx: Context, at: str, problems: Problems) -> dict | None:
    """Validate one record (schema, then the locate contract) and reduce it to what the view needs."""
    try:
        ctx.validate(record)
    except ctx.schema_error as exc:
        problems.add(at, str(exc))
        return None
    ext = _locate_ext(record)
    if ext is None:
        problems.add(at, f"extensions[{LOCATE!r}] is missing")
        return None
    rid = record_label(record)
    # The record's own keys depend on its shape, which its relation decides (below); a relation
    # that is no locate relation is refused there, and its extension is judged by every shape's keys.
    relation = record.get("relation")
    shape = ("unresolved" if relation is None else "absence" if relation == "noMatch"
             else "location" if member(relation, LOCATION_RELATIONS) else None)
    shape_keys = SHAPE_EXT_KEYS[shape] if shape else tuple(k for keys in SHAPE_EXT_KEYS.values() for k in keys)
    for message in (*foreign_extensions(record, "the record", rid),
                    *unexpected_keys(ext, RECORD_EXT_KEYS + RECORD_EXT_OPTIONAL + shape_keys, SHAPE_LEVELS[shape],
                                     rid)):
        problems.add(at, message)
    item = ext.get("item")
    if not isinstance(item, str) or not ITEM_RE.match(item):
        problems.add(at, "locate extension item must be 'In-text #<8-16 hex>'")
        return None
    if record.get("id") != f"{source['source_id']}::{item}":
        problems.add(at, "id must be <source_id>::<item>")
    universe = ctx.universe
    if ext.get("universe") != universe["id"] or ext.get("universe_sha256") != universe["sha256"]:
        problems.add(at, "universe and universe_sha256 must name the record set's universe")
    challenge = ext.get("challenge")
    if not member(challenge, CHALLENGES):
        problems.add(at, f"challenge must be one of {sorted(CHALLENGES)}")
        challenge = None
    kind = record.get("kind")
    if not member(kind, KINDS):
        problems.add(at, f"kind must be one of {sorted(KINDS)}")
        kind = None
    # Every record carries its passage's section (the heading path, [] in the lead) and
    # prominence: the page marks the article's subject passages by it.
    section = ext.get("section")
    if not isinstance(section, list) or not all(isinstance(s, str) and s.strip() for s in section):
        problems.add(at, "section must be its passage's heading path: a list of heading strings, [] in the lead")
        section = None
    prominence = ext.get("prominence")
    if not member(prominence, PROMINENCES):
        problems.add(at, f"prominence must be one of {sorted(PROMINENCES)}")
        prominence = None
    coverage = coverage_claim(ext, at, problems)

    subjects = record["subject"]
    if len(subjects) != 1 or subjects[0].get("type") != "html":
        problems.add(at, "subject must be exactly one html end (the Wikipedia passage)")
        return None
    end = subjects[0]
    uri = end.get("uri", "")
    if not names_revision(uri, source["revision"]) or end.get("version") != str(source["revision"]):
        problems.add(at, "subject must name the source's revision: an en.wikipedia.org permanent link whose "
                         "oldid is exactly the revision, and version equal to it")
    subject_ext = _locate_ext(end) or {}
    for message in (*foreign_extensions(end, "the subject end", rid),
                    *unexpected_keys(subject_ext, SUBJECT_EXT_KEYS + SUBJECT_EXT_OPTIONAL,
                                     "the subject end's locate extension", rid)):
        problems.add(at, message)
    pageid = subject_ext.get("pageid")
    if type(pageid) is not int or pageid != source["pageid"] \
            or subject_ext.get("html_sha256") != source["html_sha256"]:
        problems.add(at, "subject extension pageid and html_sha256 must match sources.jsonl")
    span, messages = subject_problems(subject_ext, source)
    for message in messages:
        problems.add(at, message)
    span_sha = subject_ext.get("span_sha256") if isinstance(subject_ext.get("span_sha256"), str) else None
    location = end.get("location")
    quote = location.get("quote") if isinstance(location, dict) else None
    messages = _quote_problems(quote)
    for message in messages:
        problems.add(at, message)
    if messages:
        return None
    if span is not None:
        for message in span_problems(span, quote, span_sha):
            problems.add(at, message)

    base = {"item": item, "id": record.get("id"), "kind": kind, "section": section, "prominence": prominence,
            "quote": {key: quote[key] for key in ("exact", "prefix", "suffix") if key in quote},
            # Passage fields every record of the passage carries alike (build_passages); the view
            # model does not copy them, and the records keep them verbatim.
            "span": span, "span_sha256": span_sha,
            "subject_uri": uri, "challenge": challenge, "coverage": coverage}
    objects = record["object"]
    if relation is None:
        if objects or "status" in record:
            problems.add(at, "an unresolved record has object [] and no status or relation")
        return {**base, "type": "unresolved"}
    if relation == "noMatch":
        return _absence(record, base, objects, ext, ctx, at, problems)
    if relation in LOCATION_RELATIONS:
        return _location(record, base, objects, ext, relation, ctx, at, problems)
    problems.add(at, f"relation {relation!r} is not a locate relation")
    return None


def _absence(record: dict, base: dict, objects: list, ext: dict, ctx: Context, at: str,
             problems: Problems) -> dict | None:
    if record.get("status") != "not_formalized":
        problems.add(at, "a noMatch record has status not_formalized")
    # What was searched: each distinct (repo, commit) of the universe, however many libraries
    # it is (Init and Std split from lean4 at one commit are one search target, and one end).
    expected = {(lib["repo"], lib["commit"]) for lib in ctx.universe["libraries"]}
    got = []
    if any(end.get("type") != "lean" or set(end) != set(ABSENCE_END_KEYS) for end in objects):
        problems.add(at, "a checked absence names each repository it searched by a minimal lean end, "
                         '{"type": "lean", "uri", "version"} only (no location, role or extensions)')
    for end in objects:
        got.append((end.get("uri"), end.get("version")))
    if len(got) != len(set(got)) or set(got) != expected:
        problems.add(at, "a checked absence names every repository of the universe at its commit, once each: "
                         "one end per distinct (repo, commit) of its libraries, so libraries split from one "
                         "repository at one commit are one end")
    receipt = ext.get("receipt")
    if not isinstance(receipt, dict) or not HEX64.match(str(receipt.get("sha256", ""))):
        problems.add(at, "receipt must be {sha256, queries, candidates}")
        return None
    for message in unexpected_keys(receipt, RECEIPT_KEYS, "the absence's receipt", record_label(record)):
        problems.add(at, message)
    path = f"receipts/{receipt['sha256']}.json"
    if path not in ctx.files:
        problems.add(at, f"receipt {path} is not in the record set")
        return None
    ctx.receipts_used.add(path)
    try:
        body = strict_json(ctx.files[path].decode("utf-8"))
        queries, candidates = body["queries"], body["candidates"]
        assert isinstance(queries, list) and isinstance(candidates, list)
    except (ValueError, KeyError, AssertionError, UnicodeDecodeError, TypeError):
        problems.add(path, "a receipt is {queries: [...], candidates: [...]}")
        return None
    # Each count is its list's length in the receipt file, a repeated entry included.
    for name, items in (("queries", queries), ("candidates", candidates)):
        if type(receipt.get(name)) is not int:
            problems.add(at, f"receipt.{name} must be an integer: the length of the {name} list in {path}")
        elif receipt[name] != len(items):
            problems.add(at, f"receipt.{name} is {receipt.get(name)!r}, but {path} lists {len(items)} {name} "
                             "(each count is the length of that list in the receipt file, repeats included)")
    if len({q for q in queries if isinstance(q, str)}) < MIN_ABSENCE_QUERIES:
        problems.add(path, f"an absence lists at least {MIN_ABSENCE_QUERIES} distinct queries")
    return {**base, "type": "absence", "status": "not_formalized",
            "receipt": {"sha256": receipt["sha256"], "queries": len(queries), "candidates": len(candidates)}}


def _location(record: dict, base: dict, objects: list, ext: dict, relation: str, ctx: Context, at: str,
              problems: Problems) -> dict | None:
    status = record.get("status")
    # A passage whose only locations are relatedMatch, with no checked absence, has no
    # derived status (proposal §2.6, "(none)"), so its records carry none; whether this
    # passage is one is decided over all its records (build_passages).
    if not (member(status, STATUSES) or (status is None and relation == "relatedMatch")):
        problems.add(at, f"a location record carries a derived status, one of {list(STATUSES)} "
                         "(only a relatedMatch of a passage with no status carries none)")
        return None
    if len(objects) != 1:
        problems.add(at, "a location record has exactly one lean end")
        return None
    end = objects[0]
    location = end.get("location") or {}
    end_ext = _locate_ext(end) or {}
    rid = record_label(record)
    for message in (*foreign_extensions(end, "the lean end", rid),
                    *unexpected_keys(end_ext, LEAN_EXT_KEYS + LEAN_EXT_OPTIONAL, "the lean end's locate extension",
                                     rid)):
        problems.add(at, message)
    library = next((lib for lib in ctx.universe["libraries"] if lib["id"] == end_ext.get("library")), None)
    if end.get("type") != "lean" or not isinstance(location.get("declaration"), str) \
            or not isinstance(location.get("module"), str):
        problems.add(at, "the lean end names a declaration and its module (location.declaration and "
                         "location.module are required)")
        return None
    if library is None:
        problems.add(at, f"library {end_ext.get('library')!r} is not in the universe")
        return None
    if end.get("uri") != library["repo"] or end.get("version") != library["commit"]:
        problems.add(at, "the lean end's uri and version must be its library's repo and commit")
    path, line, end_line = end_ext.get("path"), end_ext.get("line"), end_ext.get("end_line")
    # library, path and line are required; only end_line is optional (docs/ATLAS.md section 3).
    if path is None:
        problems.add(at, "path is missing or null: a location's lean end carries library, path and line, "
                         "and only end_line is optional")
    elif not isinstance(path, str) or not safe_relative(path) or not path.endswith(".lean"):
        problems.add(at, "path must be a safe relative .lean path (relative to the repository root)")
    if line is None:
        problems.add(at, "line is missing or null: a location's lean end carries library, path and line, "
                         "and only end_line is optional (line must be an integer >= 1)")
    elif type(line) is not int or line < 1:
        problems.add(at, "line must be an integer >= 1")
        line = None  # malformed values are reported and then dropped, so no later check meets them
    # end_line is there when the library index records one (atlas's published index has
    # only line), and absent otherwise: never null.
    if "end_line" in end_ext and (type(end_line) is not int or (line is not None and end_line < line)):
        problems.add(at, "end_line, when the index has one, must be an integer >= line (omit it, never null, "
                         "when the index records only line)")
        end_line = None
    proof_status = end_ext.get("proof_status")
    if not member(proof_status, PROOF_STATUSES):
        problems.add(at, f"proof_status must be one of {sorted(PROOF_STATUSES)}")
        proof_status = None
    statement, message = statement_flag(end_ext.get("statement"), proof_status)
    if message:
        problems.add(at, message)
    key = ext.get("location_key")
    if not isinstance(key, str) or not HEX64.match(key):
        problems.add(at, "location_key must be 64 hex")
    elif key in ctx.location_keys:
        problems.add(at, f"location_key {key} appears twice")
    else:
        ctx.location_keys.add(key)
    if not isinstance(key, str):
        key = None
    rank = ext.get("rank")
    if type(rank) is not int or not 1 <= rank <= MAX_LOCATIONS:
        problems.add(at, f"rank must be an integer from 1 to {MAX_LOCATIONS}")
        rank = None
    confidence = ext.get("confidence")
    if not member(confidence, CONFIDENCES):
        problems.add(at, f"confidence must be one of {sorted(CONFIDENCES)}")
        confidence = None
    note = record.get("note")
    if relation == "closeMatch" and not (isinstance(note, str) and note.strip()):
        problems.add(at, "a closeMatch names its convention difference in note (proposal §2.6); one a "
                         "challenge downgrade produced carries the challenger's witness sentence")
    view = {
        "library": library["id"], "repo": library["repo"], "commit": library["commit"],
        "declaration": location["declaration"], "module": location["module"],
        "path": path, "line": line,
        "relation": relation, "proof_status": proof_status, "statement": statement, "rank": rank,
        "confidence": confidence, "challenge": base["challenge"],
        "location_key": key, "note": note if isinstance(note, str) and note.strip() else None,
    }
    if end_line is not None:  # optional in the view model too: absent means "line N" only
        view["end_line"] = end_line
    return {**base, "type": "location", "status": status, "location": view}


def statement_flag(value: Any, proof_status: str | None) -> tuple[bool, str | None]:
    """The lean end's optional `statement` flag (a Prop-valued definition, proposal §2.4).

    Returns (flag, problem). Absent or null is false, as in cflib.vocab.derive_status, which
    refuses a flag that is not a bool and a flag on a proved declaration: a Prop-valued
    definition has every proof status but proved (decide_proof_status), so no consistent
    record says that.
    """
    if value is None:
        return False, None
    if not isinstance(value, bool):
        return False, "statement must be true or false (the library index sets it on a Prop-valued definition)"
    if value and proof_status == "proved":
        return False, ("a proved declaration with the statement flag set: the flag marks a Prop-valued "
                       "definition, whose proof_status is definition, wanted, statement_only or axiom_dependent")
    return value, None


def _rank_key(loc: dict) -> int:
    # A malformed rank is already a reported problem (and dropped); sort it last.
    return loc["rank"] if type(loc["rank"]) is int else 99


def _theorem_row(loc: dict) -> str:
    """One location of a theorem or conjecture passage (cflib.vocab._theorem_row)."""
    if loc["relation"] in PART_RELATIONS:
        return "partial"
    # exactMatch, closeMatch, broadMatch
    if loc["statement"]:  # a Prop-valued definition states a theorem, never proves it
        return "statement_only"
    if loc["proof_status"] == "proved":
        return "formalized"
    if loc["proof_status"] in UNPROVED:
        return "statement_only"
    # Data (a definition without the statement flag): decision D1 makes an exact or close
    # match to an equivalence or data declaration that states the theorem formalized (CRT's
    # RingEquiv); the table's "broadMatch to a definition" stays partial. Data resting on sorry
    # has proof_status statement_only, above.
    return "formalized" if loc["relation"] in DATA_MATCH_RELATIONS else "partial"


def _definition_row(loc: dict) -> str:
    """One location of a definition passage (cflib.vocab._definition_row)."""
    if loc["relation"] in PART_RELATIONS or loc["relation"] == "broadMatch":
        return "partial"
    # exactMatch, closeMatch
    if loc["proof_status"] == "definition":
        return "formalized"
    if loc["proof_status"] in UNPROVED:
        return "statement_only"
    # A proved theorem stating a definition passage: not in the table; §1.11 says partial.
    return "partial"


def location_status(kind: str, loc: dict) -> str | None:
    """The status one location gives a passage of `kind`; None for a relatedMatch, which never counts."""
    if loc["relation"] == "relatedMatch":
        return None
    rows = []
    if kind in THEOREM_KINDS or kind in EITHER_KINDS:
        rows.append(_theorem_row(loc))
    if kind in DEFINITION_KINDS or kind in EITHER_KINDS:
        rows.append(_definition_row(loc))
    return min(rows, key=STATUS_RANK.__getitem__) if rows else None


def part_finished(part: dict) -> bool:
    """Whether a coverage claim's part is finished, by the round-three reading of D1.

    Finished: proved, or data (a definition without the statement flag). Unfinished:
    statement_only, wanted or axiom_dependent, or a Prop-valued definition (the flag set),
    whatever its proof_status: it states a clause and proves none.
    """
    return part["proof_status"] == "proved" or (part["proof_status"] == "definition" and not part["statement"])


def coverage_location(row: str, parts: list[dict]) -> dict:
    """The one exactMatch location a survived claim acts as, on the theorem or definition row.

    It is a fixed stand-in, not any one part's own proof status and flag: when every part is
    finished it is proved on the theorem row and data (definition, no flag) on the definition
    row, so the claim formalizes the passage on either row (so a data part counts, as an exact
    match to data does, Q2; and the definition row has the statement_only case an exact match to
    an unfinished declaration has, Q3); when any part is unfinished it is a statement_only
    declaration on both rows. Read literally, "the least finished part" would be wrong on the
    definition row: a claim of proved parts formalizes a definition passage, where an exact match
    to a proved theorem is partial, and one with a Prop-valued-definition part is statement_only,
    where an exact match to that definition formalizes it.
    """
    if not all(part_finished(part) for part in parts):
        proof_status = "statement_only"
    else:
        proof_status = "proved" if row == "theorem" else "definition"
    return {"relation": COVERAGE_ACTS_AS, "proof_status": proof_status, "statement": False}


def coverage_status(kind: str, claim: dict | None, locations: list[dict]) -> str | None:
    """The status a coverage claim gives a passage of `kind` on its own; None when it gives none.

    Only a claim whose challenge is survived counts: one pending, unjudged (not_required,
    sampled_out) or downgraded upgrades nothing, and a refuted claim is not exported at all. A
    survived claim is judged as its coverage_location on the passage kind's row of the table, an
    example or `other` passage taking the better of the two rows exactly as for an ordinary
    location (Q1), so it gives formalized when every part is finished and statement_only
    otherwise.
    """
    if claim is None or claim["challenge"] != COVERAGE_SURVIVED:
        return None
    by_key = {loc["location_key"]: loc for loc in locations}
    parts = [by_key.get(key) for key in claim["parts"]]
    if (len(parts) < MIN_COVERAGE_PARTS or any(part is None or part["relation"] != COVERAGE_RELATION
                                               for part in parts)):
        return None  # refused by coverage_part_problems; never counted
    rows = []
    if kind in THEOREM_KINDS or kind in EITHER_KINDS:
        rows.append(_theorem_row(coverage_location("theorem", parts)))
    if kind in DEFINITION_KINDS or kind in EITHER_KINDS:
        rows.append(_definition_row(coverage_location("definition", parts)))
    return min(rows, key=STATUS_RANK.__getitem__) if rows else None


def derive_status(kind: str, locations: list[dict], coverage: dict | None = None) -> tuple[str | None, Any]:
    """Proposal §2.6's derived status of a located passage, recomputed from its locations.

    A port of formal-companion's cflib.vocab.derive_status (scripts/cflib/vocab.py, which
    implements references/vocabulary.md §1.11's decisions for the cases the table leaves
    open, decision D1 among them): each location that counts gets a status from its kind's
    row of the table, an example or `other` passage taking the better of the two rows; a
    survived coverage claim over the passage's hasPart locations counts as one more exactMatch
    location, a fixed stand-in that is finished only when every part is (coverage_location,
    coverage_status); and the passage takes the best of them. `locations` are in rank order.

    Returns (status, basis): the one status the producer stamps, and what justifies it: the
    highest-ranked location reaching it, or COVERAGE_BASIS when only the coverage claim
    reaches it (a location reaching the same status is preferred). (None, None) means the
    table gives the passage no status: its only locations are relatedMatch.
    test_build_store.py checks this against a truth table generated from the producer, and
    derive_passage_status (these rules behind the builder's checks) against the hand-written
    table the two sides share.
    """
    best: str | None = None
    basis: Any = None
    for loc in locations:
        status = location_status(kind, loc)
        if status is not None and (best is None or STATUS_RANK[status] < STATUS_RANK[best]):
            best, basis = status, loc
    claimed = coverage_status(kind, coverage, locations)
    if claimed is not None and (best is None or STATUS_RANK[claimed] < STATUS_RANK[best]):
        best, basis = claimed, COVERAGE_BASIS
    return best, basis


def _shape(locations: list[dict], coverage: dict | None = None) -> str:
    def declaration(loc: dict) -> str:
        if not loc["statement"]:
            return str(loc["proof_status"])
        return "a Prop-valued definition" + ("" if loc["proof_status"] == "definition" else f" ({loc['proof_status']})")
    shown = [f"{loc['relation']} to {declaration(loc)}" for loc in locations if loc["relation"] != "relatedMatch"]
    text = ", ".join(shown) or "relatedMatch only"
    if coverage is not None:
        text += f", and a coverage claim over {len(coverage['parts'])} parts (challenge {coverage['challenge']})"
    return text


def status_problems(kind: str | None, status: str, locations: list[dict],
                    coverage: dict | None = None) -> tuple[list[str], Any]:
    """Compare a located passage's stamped status with the recomputed one; returns (problems, basis)."""
    if status == "not_formalized":
        return ["not_formalized needs a checked absence (noMatch record)"], None
    if not member(kind, KINDS):
        return [], None  # already reported by classify_record
    derived, basis = derive_status(kind, locations, coverage)
    if derived is None:
        return [f"proposal §2.6 gives a {kind} passage with {_shape(locations)} no status: it is unresolved, "
                f"and its records carry no status (docs/ATLAS.md, contract note 9)"], None
    if derived != status:
        return [f"the records say {status}, but the derived-status rules (proposal §2.6 and vocabulary.md "
                f"§1.11) derive {derived} for a {kind} passage with {_shape(locations, coverage)}"], None
    return [], basis


def derive_passage_status(kind: Any, locations: list[dict], absent: bool, coverage: Any = None) -> str | None:
    """One passage's derived status by the producer's signature, through the builder's own checks.

    The function the two sides' shared parity table is written for (fixtures/status_parity.json,
    a pinned copy of formal-companion's tests/data/locate/status-parity.json):
    `cflib.vocab.derive_status(passage_kind, locations, absent, coverage)`. `locations` are
    {location_key, relation, proof_status, statement} mappings, `absent` says the passage has a
    checked absence, and `coverage` is the claim exactly as a record carries it
    (extensions[locate].coverage), or None for none. It makes the checks build() makes on those
    same values, with the same functions and messages: the kind, each location's relation,
    proof_status and statement flag (statement_flag), a 64-hex location_key that no other location
    has, an absence beside relatedMatch locations only, the claim's shape (coverage_claim) and its
    parts (coverage_part_problems). Where build() would refuse the set it raises StatusRefused;
    otherwise it returns derive_status's status, "not_formalized" for a checked absence, or None
    (no status: unresolved). build() itself runs these checks record by record and reports every
    problem; this is the one-passage form, so the parity test runs the builder's rules, not a copy.
    """
    if not member(kind, KINDS):
        raise StatusRefused(f"kind must be one of {sorted(KINDS)}")
    rows = []
    for raw in locations:
        if not isinstance(raw, dict):
            raise StatusRefused("a location is an object {location_key, relation, proof_status, statement}")
        relation, proof_status = raw.get("relation"), raw.get("proof_status")
        if not member(relation, LOCATION_RELATIONS):
            raise StatusRefused(f"relation {relation!r} is not a locate relation")
        if not member(proof_status, PROOF_STATUSES):
            raise StatusRefused(f"proof_status must be one of {sorted(PROOF_STATUSES)}")
        statement, problem = statement_flag(raw.get("statement"), proof_status)
        if problem:
            raise StatusRefused(problem)
        key = raw.get("location_key")
        if not isinstance(key, str) or not HEX64.match(key):
            raise StatusRefused("location_key must be 64 hex")
        if any(row["location_key"] == key for row in rows):
            raise StatusRefused(f"location_key {key} appears twice")
        rows.append({"relation": relation, "proof_status": proof_status, "statement": statement,
                     "location_key": key})
    if absent and any(row["relation"] != "relatedMatch" for row in rows):
        raise StatusRefused("an absence may sit beside relatedMatch locations only")
    claim = None
    if coverage is not None:
        problems = Problems()
        claim = coverage_claim({"coverage": coverage}, "the claim", problems)
        found = problems.items or coverage_part_problems(claim, rows)
        if found:
            raise StatusRefused("; ".join(found))
    if absent:
        return "not_formalized"
    return derive_status(kind, rows, claim)[0]


def confirmed_parts(claim: dict, locations: list[dict]) -> list[dict]:
    """The passage's locations as the view model shows them, its survived claim's parts confirmed.

    Confirming a coverage claim confirms its parts (the round-three reading of D1): a part whose
    own challenge is not_required or sampled_out (CONFIRMED_WITH_CLAIM) reads survived once the
    claim survived. A part another verdict judged keeps it (downgraded, survived), as does one
    still owed a challenge (challenge_pending: a prominence overturn to subject or central in the
    claim's run put it under a rule requiring its own verdict, and the reopen wins over the claim,
    as the producer's outcomes.confirm_parts runs after its _reopen), and a claim that did not
    survive confirms nothing. A producer that already stamps its parts survived gets the same
    view, so the rule is applied here whichever value the part's records carry; the records
    themselves are copied verbatim.
    """
    if claim["challenge"] != COVERAGE_SURVIVED:
        return locations
    parts = set(claim["parts"])
    return [{**loc, "challenge": COVERAGE_SURVIVED}
            if loc["location_key"] in parts and loc["challenge"] in CONFIRMED_WITH_CLAIM else loc
            for loc in locations]


def build_passages(classified: list[dict], source: dict, problems: Problems) -> list[dict]:
    """Group records by passage id (first-appearance order) and check the outcome rules."""
    groups: dict[str, list[dict]] = {}
    for record in classified:
        groups.setdefault(record["id"], []).append(record)
    passages = []
    for pid, records in groups.items():
        at = f"records/{source['source_id']}.jsonl: {pid}"
        first = records[0]
        for other in records[1:]:
            # The coverage claim is a passage field like the rest: every record carries the
            # same claim, or none does. So are the span and span_sha256 of the subject end.
            for key in ("kind", "quote", "section", "prominence", "coverage", "span", "span_sha256"):
                if other[key] != first[key]:
                    problems.add(at, f"records of one passage disagree on {key}"
                                 + (" (every record of the passage carries the same claim, or none does)"
                                    if key == "coverage" else ""))
        unresolved = [r for r in records if r["type"] == "unresolved"]
        absences = [r for r in records if r["type"] == "absence"]
        locations = sorted((r["location"] for r in records if r["type"] == "location"),
                           key=lambda loc: (_rank_key(loc), loc["library"]))
        statuses = {r.get("status") for r in records if r["type"] != "unresolved"}
        if unresolved and len(records) > 1:
            problems.add(at, "an unresolved passage has exactly one record")
        if len(absences) > 1:
            problems.add(at, "a passage has at most one checked absence")
        if len(statuses) > 1:
            problems.add(at, "every record of a passage carries the same derived status"
                         + (" (only a passage whose locations are all relatedMatch, with no checked absence, "
                            "has records without one)" if None in statuses else ""))
        if len(locations) > MAX_LOCATIONS:
            problems.add(at, f"at most {MAX_LOCATIONS} locations per passage")
        ranks = [loc["rank"] for loc in locations if loc["rank"] is not None]
        if len(ranks) != len(set(ranks)):
            problems.add(at, "location ranks are distinct")
        status = next(iter(statuses)) if len(statuses) == 1 else None
        basis = None
        claim = first["coverage"]
        if claim is not None:
            found = coverage_part_problems(claim, locations)
            for message in found:
                problems.add(at, message)
            if found:
                claim = None  # refused above; the status check below judges the locations alone
        unresolved_info = {"challenge": unresolved[0]["challenge"]} if unresolved else None
        if unresolved:
            outcome, status = "unresolved", None
        elif absences:
            outcome = "absent"
            if any(loc["relation"] != "relatedMatch" for loc in locations):
                problems.add(at, "an absence may sit beside relatedMatch locations only")
        elif statuses == {None}:
            # Only relatedMatch records, none with a status, and no checked absence: what a
            # challenge that downgrades a passage's one location leaves. Nothing says it is
            # formalized and nobody checked it is absent, so it counts as unresolved (never
            # not_formalized), and its related declarations are kept.
            outcome = "unresolved"
            unresolved_info = {"challenge": locations[0]["challenge"] if locations else None}
        else:
            outcome = "located"
            if status is not None and first["coverage"] is claim:  # a refused claim is reported already
                found, basis = status_problems(first["kind"], status, locations, claim)
                for message in found:
                    problems.add(at, message)
        coverage = None
        if claim is not None:
            # The claim as exported, plus what it gives on its own (formalized or statement_only
            # once it survived, null otherwise) and whether it is what justifies the passage's
            # status; its parts' challenge values then read as confirmed by it.
            coverage = {**claim, "status": (coverage_status(first["kind"], claim, locations)
                                            if member(first["kind"], KINDS) else None),
                        "basis": basis == COVERAGE_BASIS}
            locations = confirmed_parts(claim, locations)
        absence = None
        if absences:
            absence = {"universe": None, "universe_sha256": None, "libraries": None,
                       "receipt": absences[0]["receipt"], "challenge": absences[0]["challenge"]}
        passages.append({
            "id": pid, "item": first["item"], "kind": first["kind"], "section": first["section"],
            "prominence": first["prominence"], "quote": first["quote"], "subject_uri": first["subject_uri"],
            "outcome": outcome, "status": status, "locations": locations, "absence": absence,
            # The location that justifies the status (derive_status), so a page names it
            # rather than whichever location ranks first; null when the coverage claim does.
            "basis": basis["location_key"] if isinstance(basis, dict) else None,
            "coverage": coverage,
            "unresolved": unresolved_info,
        })
    return passages


# ---- the store ----------------------------------------------------------------------------


def _status_counts() -> dict[str, int]:
    return {status: 0 for status in SUMMARY_STATUSES}


def article_view(source: dict, passages: list[dict], universe: dict, seal: dict, seal_sha: str,
                 records_sha: str) -> dict:
    """The article's view model. Runs only on a set that passed every check."""
    by_status = _status_counts()
    by_library: dict[str, dict[str, int]] = {}
    by_proof = Counter()
    by_relation = Counter()
    n_locations = n_related = 0
    for passage in passages:
        by_status[passage["status"] or "unresolved"] += 1
        if passage["absence"] is not None:
            passage["absence"].update({"universe": universe["id"], "universe_sha256": universe["sha256"],
                                       "libraries": len(universe["libraries"])})
        seen_here: set[str] = set()
        for loc in passage["locations"]:
            # A relatedMatch "states something else" (proposal §2.6): it is counted
            # apart, so "where is this formalized" never counts it as a location.
            entry = by_library.setdefault(loc["library"], {"locations": 0, "complete": 0, "incomplete": 0,
                                                           "passages": 0, "related": 0})
            if loc["relation"] == "relatedMatch":
                entry["related"] += 1
                n_related += 1
            else:
                entry["locations"] += 1
                # complete: proved or a definition; incomplete: stated without a finished proof.
                entry["complete" if loc["proof_status"] in COMPLETE else "incomplete"] += 1
                n_locations += 1
                if loc["library"] not in seen_here:
                    entry["passages"] += 1
                    seen_here.add(loc["library"])
            by_proof[loc["proof_status"]] += 1
            by_relation[loc["relation"]] += 1
    return {
        "schema": ARTICLE_SCHEMA,
        "pageid": source["pageid"], "title": source["title"], "revision": source["revision"],
        "source_id": source["source_id"], "wikidata": source["wikidata"], "assessment": source["assessment"],
        "source": {"html_sha256": source["html_sha256"], "layer_sha256": source["layer_sha256"],
                   "renderer": source["renderer"]},
        "campaign": seal["campaign"],
        "universe": {"id": universe["id"], "sha256": universe["sha256"], "frozen": universe["frozen"],
                     "libraries": len(universe["libraries"])},
        "record_set": seal_sha, "records_sha256": records_sha,
        "summary": {"passages": len(passages), "locations": n_locations, "related": n_related, "by_status": by_status,
                    "by_library": by_library, "by_proof_status": dict(by_proof), "by_relation": dict(by_relation)},
        "passages": passages,
    }


def check_budget(budget: int, path: str, files: dict[str, bytes], classified: list[dict],
                 problems: Problems) -> None:
    """A set sealed under a per-source byte budget keeps to it.

    The export measures a source as its records file plus the receipts its absences
    reference, each counted once; 0 means it ran with no budget, the default for a sealed
    set. A consumer with a cap of its own (WikiLean's later D1 import) asks the export for
    one, and SEAL records which budget was used, so a set that breaks it is refused here.
    """
    if not budget:
        return
    receipts = {f"receipts/{r['receipt']['sha256']}.json" for r in classified if r["type"] == "absence"}
    size = len(files[path]) + sum(len(files[receipt]) for receipt in receipts if receipt in files)
    if size > budget:
        problems.add(path, f"this source's records and receipts are {size} bytes, over the byte_budget of "
                           f"{budget} that SEAL.json says the export kept to")


def build(set_dir: Path, built_at: str | None = None) -> dict[str, bytes]:
    """Return the store tree, {relative path: bytes}, for one sealed record set."""
    validate, schema_error, provenance = schema_validator()
    seal, seal_sha, files = read_record_set(set_dir)
    problems = Problems()
    universe = load_universe(seal, files, problems)
    sources = load_sources(files, problems)
    problems.raise_if_any()
    if built_at is None:
        built_at = seal["created"]
    elif not CREATED_RE.match(built_at):
        raise AtlasError("--built-at must be an ISO 8601 UTC time ending in Z")

    ctx = Context(universe=universe, files=files, validate=validate, schema_error=schema_error)
    built: list[tuple[dict, list[dict]]] = []
    for source in sources:
        path = f"records/{source['source_id']}.jsonl"
        classified = []
        try:
            lines = files[path].decode("utf-8").splitlines()
        except UnicodeDecodeError:
            problems.add(path, "not UTF-8")
            continue
        for number, line in enumerate(lines, 1):
            at = f"{path}:{number}"
            if not line.strip():
                problems.add(at, "blank line")
                continue
            try:
                record = strict_json(line)
            except ValueError as exc:
                problems.add(at, f"not valid JSON ({exc})")
                continue
            reduced = classify_record(record, source, ctx, at, problems)
            if reduced is not None:
                classified.append(reduced)
        check_budget(seal["byte_budget"], path, files, classified, problems)
        built.append((source, build_passages(classified, source, problems)))
    for receipt in sorted(p for p in files if p.startswith("receipts/")):
        if receipt not in ctx.receipts_used:
            problems.add(receipt, "receipt referenced by no absence record")
        elif receipt != f"receipts/{sha256_hex(files[receipt])}.json":
            problems.add(receipt, "a receipt's file name is the sha256 of its bytes")
    # Every check has run: a refused set never reaches the view code, which assumes the contract.
    problems.raise_if_any()
    articles = [article_view(source, passages, universe, seal, seal_sha,
                             sha256_hex(files[f"records/{source['source_id']}.jsonl"]))
                for source, passages in built]

    tree: dict[str, bytes] = {}
    prefix = STORE_PREFIX + "/"
    tree[f"{prefix}universes/{universe['sha256']}.json"] = canonical(universe)
    shards: dict[str, list[list]] = {}
    totals = {"articles": len(articles), "passages": 0, "locations": 0, "related": 0, "by_status": _status_counts(),
              "by_library": {}, "by_importance": {}}
    for article in articles:
        pageid = article["pageid"]
        tree[f"{prefix}articles/{pageid}.json"] = canonical(article)
        tree[f"{prefix}records/{pageid}.jsonl"] = files[f"records/{article['source_id']}.jsonl"]
        summary = article["summary"]
        importance = (article["assessment"] or {}).get("importance")
        # Libraries holding a location (not only a related declaration), in universe order.
        located_in = [lib["id"] for lib in universe["libraries"]
                      if summary["by_library"].get(lib["id"], {}).get("passages")]
        shards.setdefault(title_shard(article["title"]), []).append(
            [article["title"], pageid, article["revision"], summary["passages"],
             *[summary["by_status"][s] for s in SUMMARY_STATUSES], importance, located_in])
        totals["passages"] += summary["passages"]
        totals["locations"] += summary["locations"]
        totals["related"] += summary["related"]
        for status, count in summary["by_status"].items():
            totals["by_status"][status] += count
        for lib, counts in summary["by_library"].items():
            entry = totals["by_library"].setdefault(lib, {"articles": 0, "passages": 0, "locations": 0,
                                                          "complete": 0, "incomplete": 0, "related": 0})
            entry["articles"] += 1 if counts["passages"] else 0
            for key in ("passages", "locations", "complete", "incomplete", "related"):
                entry[key] += counts[key]
        bucket = totals["by_importance"].setdefault(importance or "unknown",
                                                    {"articles": 0, "passages": 0, "by_status": _status_counts()})
        bucket["articles"] += 1
        bucket["passages"] += summary["passages"]
        for status, count in summary["by_status"].items():
            bucket["by_status"][status] += count
    for shard, entries in sorted(shards.items()):
        entries.sort(key=lambda entry: (title_key(entry[0]), entry[1]))
        data = canonical({"schema": INDEX_SCHEMA, "shard": shard, "fields": INDEX_FIELDS, "articles": entries})
        if len(data) > MAX_INDEX_SHARD_BYTES:
            raise AtlasError(f"index shard {shard} is {len(data)} bytes; the v1 store caps a shard at "
                             f"{MAX_INDEX_SHARD_BYTES} (raise INDEX_HEX in a v2 store)")
        tree[f"{prefix}index/{shard}.json"] = data
    tree[f"{prefix}current.json"] = canonical({
        "schema": CURRENT_SCHEMA, "campaign": seal["campaign"], "built_at": built_at, "builder": BUILDER,
        "universe": {"id": universe["id"], "sha256": universe["sha256"], "frozen": universe["frozen"],
                     "libraries": len(universe["libraries"])},
        "record_set": {"seal_sha256": seal_sha, "set_id": seal["set_id"], "created": seal["created"],
                       "skill_commit": seal["skill_commit"], "byte_budget": seal["byte_budget"],
                       "sourceset": {"id": seal["sourceset"]["id"],
                                     "manifest_sha256": seal["sourceset"]["manifest_sha256"]}},
        "literature_links": {"version": provenance["version"], "commit": provenance["commit"],
                             "schema_sha256": provenance["sha256"]},
        "counts": totals,
        "index": {"hex": INDEX_HEX, "shards": sorted(shards), "entries": len(articles)},
    })
    tree[f"{prefix}manifest.json"] = canonical({
        "schema": MANIFEST_SCHEMA, "record_set": seal_sha,
        "files": {path: {"bytes": len(data), "sha256": sha256_hex(data)} for path, data in sorted(tree.items())},
    })
    return tree


def write_tree(out: Path, tree: dict[str, bytes], replace: bool) -> None:
    """Write the tree into a fresh directory, then move it into place (never a half-written store).

    --out is the store directory itself: it holds atlas/v1/ and nothing else. --replace
    refuses a directory holding anything more, so pointing --out one level too high
    (wiki/public, a fixtures directory) cannot delete unrelated files.
    """
    if out.exists():
        if not out.is_dir() or out.is_symlink():
            raise AtlasError(f"{out} is not a directory")
        entries = sorted(p.name for p in out.iterdir())
        if entries and not replace:
            raise AtlasError(f"{out} exists and is not empty; pass --replace or --check")
        atlas = out / "atlas"
        foreign = [name for name in entries if name != "atlas"]
        if not foreign and atlas.exists():
            foreign = [f"atlas/{p.name}" for p in atlas.iterdir() if p.name != "v1"] if atlas.is_dir() else ["atlas"]
        if foreign:
            raise AtlasError(f"{out} holds more than an atlas store ({', '.join(foreign[:5])}); "
                             "--replace replaces only a directory that holds atlas/v1/ alone")
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".atlas-store-", dir=out.parent))
    retired = None
    try:
        for relative, data in tree.items():
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if out.exists():
            retired = Path(tempfile.mkdtemp(prefix=".atlas-retired-", dir=out.parent)) / "store"
            out.rename(retired)
        staging.rename(out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if retired is not None and retired.exists() and not out.exists():
            retired.rename(out)  # put the previous store back
        raise
    finally:
        if retired is not None:
            shutil.rmtree(retired.parent, ignore_errors=True)


def check_tree(out: Path, tree: dict[str, bytes]) -> list[str]:
    """Differences between a store directory on disk and the tree this build produces."""
    found = []
    on_disk = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()} if out.is_dir() else set()
    for relative in sorted(set(tree) | on_disk):
        if relative not in on_disk:
            found.append(f"missing: {relative}")
        elif relative not in tree:
            found.append(f"unexpected: {relative}")
        elif (out / relative).read_bytes() != tree[relative]:
            found.append(f"differs: {relative}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--record-set", type=Path, required=True, help="sealed record set directory")
    parser.add_argument("--out", type=Path, required=True, help="store directory (holds atlas/v1/...)")
    parser.add_argument("--built-at", help="ISO 8601 UTC build time for current.json (default: SEAL created)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="compare --out with a fresh build; write nothing")
    mode.add_argument("--replace", action="store_true", help="replace an existing --out")
    args = parser.parse_args(argv)
    try:
        tree = build(args.record_set, args.built_at)
        if args.check:
            differences = check_tree(args.out, tree)
            for line in differences:
                print(f"atlas --check: {line}", file=sys.stderr)
            if differences:
                return 1
            print(f"atlas --check: {args.out} matches ({len(tree)} files)")
            return 0
        write_tree(args.out, tree, args.replace)
    except AtlasError as exc:
        print(f"atlas: refusing the record set:\n{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - a builder bug must not look like "--check differs" (1)
        print(f"atlas: internal error, nothing was written ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 3
    print(f"atlas: wrote {len(tree)} files under {args.out / STORE_PREFIX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
