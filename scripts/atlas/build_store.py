#!/usr/bin/env python3
"""Build WikiLean's read-only formalization-atlas store from one or more sealed locate record sets.

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
    <out>/atlas/v1/reader/<pageid>.html         optional (--reader-html): the revision, its passages marked

Usage (CPython 3.12.13 or a later 3.12, standard library plus the CI's pinned jsonschema, through
site/native_annotations.py; any other interpreter is refused, exit 3, because the reader's checks
rely on that html.parser):

    python3 scripts/atlas/build_store.py --record-set <set-dir> --out <store-dir>
    python3 scripts/atlas/build_store.py --record-set <set-dir> --out <store-dir> --check
    python3 scripts/atlas/build_store.py --record-set <set-dir> --out <store-dir> --replace
    python3 scripts/atlas/build_store.py --record-set <set-a> --record-set <set-b> \
        --campaign <campaign-id> --out <store-dir>
    python3 scripts/atlas/build_store.py --record-set <set-dir> --out <store-dir> \
        [--passage-text <file>] [--signatures <file>] [--provenance <file>] [--reader-html <file>] \
        [--audit-summary <file>]

A set is cf-locate-recordset/1, or cf-locate-recordset/2 (formal-companion
references/locate-documents.md §6, "The record set of status rule 2"): /1 plus SEAL.json's
status_rule (2 or 3), the revision-free passage_key and pair_key on its records and the
coverage claim as a bundle, each recomputed here, with every status derived by the seal's
rule. A /1 set builds byte for byte as before. Several sets that share a universe,
sourceset, producer commit, schema and status rule, and no page, build into one store
(combined_sets): each article keeps its own seal's campaign and record set.

Five optional side files (apply_supplements) add what a record set does not hold, each checked
against the records it describes: a passage's full text (its span's layer slice, by its
span_sha256), a card's Lean signature (by library, commit and declaration, from a corpus of the
store's universe), the models, operator and dates of the runs that made each set and of the
inventory run that chose its passages, each article's pinned revision with its passages marked
(its text the records' layer, each passage's marks its span; attach_reader_html) with each image
it shows credited as its licence asks, and what an audit of the build's links estimated, each
figure a share with its interval (by the seal sha256 of each record set it read; audit_view).
Without them the store is byte for byte what it was before they existed.

The build is a pure function of the record set's bytes, of the side files given and of
--built-at (which defaults to SEAL.json's `created`, the latest one's for several sets, so a
dashboard then dates the export, not a build), so --check rebuilds in memory and
compares byte for byte. Exit codes: 0 success, 1 --check found differences, 2 the record
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
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "wiki" / "src" / "vendor" / "literature-links"

LOCATE = "org.mathlib-initiative.formal-companion.locate"
SEAL_SCHEMA = "cf-locate-recordset/1"
# The record set of status rules 2 and 3 (formal-companion references/locate-documents.md §6,
# "The record set of status rule 2"; scripts/locate/export.py SEAL_SCHEMA_BUNDLE): /1's shapes
# plus the revision-free passage_key on every record, a location record's pair_key, a coverage
# claim as its bundle, and SEAL.json's status_rule. Status rule 3 (R-BROAD) exports the same
# shapes; only the statuses its records carry were derived by the later rule.
SEAL_SCHEMA_BUNDLE = "cf-locate-recordset/2"
# The derivation a record set's statuses were derived by (cflib.vocab STATUS_RULE_D1,
# STATUS_RULE_BUNDLE, STATUS_RULE_BROAD): 1 is every /1 set; a /2 set's seal names 2 or 3.
STATUS_RULE_D1 = 1
STATUS_RULE_BUNDLE = 2
STATUS_RULE_BROAD = 3
BUNDLE_STATUS_RULES = (STATUS_RULE_BUNDLE, STATUS_RULE_BROAD)
STORE_PREFIX = "atlas/v1"
ARTICLE_SCHEMA = "wikilean.atlas.article/1"
# The mapping (which passage is formalized where) is WikiLean's, released under CC0 like its
# annotations; the text it quotes is its source's, under the licence each record's subject end
# names. Each article view states both (`licence`), so its JSON says which is which.
MAPPING_LICENCE = "CC0-1.0"
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
# Under /2 the claim is a bundle (records.bundle_ext, cflib.vocab COVERAGE_RECORD_KEYS_BUNDLE):
# the three keys, the bundle's relation (its weakest part's, one of BUNDLE_RELATIONS), its
# conjuncts [{id, text, part}] (MIN_CONJUNCTS..MAX_CONJUNCTS, ids c1..cN in order, each beside
# the one part stating it, every part stating at least one) and its bundle_key.
COVERAGE_KEYS_BUNDLE = (*COVERAGE_KEYS, "relation", "conjuncts", "bundle_key")
BUNDLE_RELATIONS = frozenset({"exactMatch", "closeMatch", "broadMatch"})
CONJUNCT_KEYS = frozenset({"id", "text", "part"})
MIN_CONJUNCTS = 2
MAX_CONJUNCTS = 5
# A cap on a conjunct's text, which the export holds to its free-text rule (the longest in
# Tier A batch 1 is 198 characters); the builder only keeps an unbounded string off the page.
MAX_CONJUNCT_CHARS = 1000
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
# The exported quote caps (proposal §2.7 and §10): the verbatim-quotation cap that the licence
# class of an open source allows a record (formal-companion POLICY.md, "quotation, not
# reproduction"). The export quotes a passage's first QUOTE_MAX_WORDS words, so a longer
# passage's quote is its capped head (quote_capped).
QUOTE_MAX_WORDS = 12
# The whitespace a passage span may end in after its last word: the export right-strips it from
# the quote, but span_sha256 hashes it (formal-companion scripts/locate/records.py, cap_quote and
# passage_records), so quote_extent tries every run of one or two whitespace characters (what
# str.isspace, the export's rstrip and its \s word split, all treat as space; none lies above
# U+3000). A span that ends in a longer run reads as a head, the safe side.
_SPACE_CHARS = tuple(ch for ch in map(chr, range(0x3001)) if ch.isspace())
TRAILING_SPACE = ("", *_SPACE_CHARS, *(a + b for a in _SPACE_CHARS for b in _SPACE_CHARS))
_TRAILING_SPACE_BYTES = tuple(run.encode("utf-8") for run in TRAILING_SPACE)
# The longest of those runs: a span whose text runs further past the quote's suffix than this
# is longer than any text quote_extent's hash path tries.
MAX_TRAILING_SPACE = max(map(len, TRAILING_SPACE))
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
# A /2 seal: /1's keys and the status rule its records' statuses were derived by
# (formal-companion scripts/locate/export.py SEAL_KEYS_BUNDLE).
SEAL_KEYS_BUNDLE = (*SEAL_KEYS, "status_rule")
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
# Under /2 every record also carries its passage_key and a location record its pair_key
# (formal-companion references/locate-documents.md §6); an absence or unresolved record has no pair.
RECORD_EXT_KEYS_BUNDLE = (*RECORD_EXT_KEYS, "passage_key")
SHAPE_EXT_KEYS_BUNDLE = {"location": (*SHAPE_EXT_KEYS["location"], "pair_key"), "absence": ("receipt",),
                         "unresolved": ()}
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


#: The interpreter the builder runs under. ReaderScan's tree and attribute rules were checked against
#: the HTML5-aligned html.parser of CPython 3.12.13; an older one reads attributes by another grammar
#: (3.9's takes "=+" between a name and its value), so the same checks could pass markup a browser
#: reads differently. CI runs a later 3.12 (scripts/ci-python.sh asserts 3.12).
REQUIRED_PYTHON = (3, 12, 13)


def interpreter_problem(version: tuple[int, ...] | None = None, implementation: str | None = None) -> str | None:
    """Why this interpreter may not build a store (None: it may): CPython 3.12, 3.12.13 or later."""
    version = tuple(sys.version_info[:3]) if version is None else tuple(version[:3])
    implementation = sys.implementation.name if implementation is None else implementation
    if implementation == "cpython" and version[:2] == REQUIRED_PYTHON[:2] and version >= REQUIRED_PYTHON:
        return None
    need = ".".join(map(str, REQUIRED_PYTHON))
    return (f"build_store needs CPython {need} or a later 3.12: ReaderScan relies on that html.parser "
            f"(this is {implementation} {'.'.join(map(str, version))})")


class AtlasError(Exception):
    """The record set breaks the input contract; the message names every problem found."""


class StatusRefused(ValueError):
    """derive_passage_status's refusal: inputs the builder refuses in a record set (the producer's CfError)."""


# ---- small helpers -----------------------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def passage_key(pageid: int, span_sha256: str) -> str:
    """A /2 record's revision-free passage key: sha256("<pageid>\\n<span_sha256>")
    (formal-companion cflib.hashing.passage_key, restated in scripts/locate/records.py)."""
    return sha256_hex(f"{pageid}\n{span_sha256}".encode("utf-8"))


def pair_key(passage: str, repository: str, declaration: str) -> str:
    """A /2 location's pair key: sha256("<passage_key>\\n<repository>\\n<declaration>"), the
    repository the lean end's uri (cflib.hashing.pair_key)."""
    return sha256_hex("\n".join([passage, repository, declaration]).encode("utf-8"))


def bundle_key(passage: str, pair_keys: list[str]) -> str:
    """A /2 bundle's key: the passage key and its parts' pair keys, sorted, one per line
    (cflib.hashing.bundle_key)."""
    return sha256_hex("\n".join([passage, *sorted(pair_keys)]).encode("utf-8"))


def seal_status_rule(seal: dict) -> int:
    """The status rule a sealed set's statuses were derived by: 1 for /1, the seal's under /2."""
    return seal["status_rule"] if seal.get("schema") == SEAL_SCHEMA_BUNDLE else STATUS_RULE_D1


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


def utc_time(value: Any) -> datetime | None:
    """An ISO 8601 UTC time (CREATED_RE) as a time, or None when it is not one (a 13th month, say).
    Times are compared as times, never as strings: "02:00:00.5Z" is after "02:00:00Z"."""
    if not (isinstance(value, str) and CREATED_RE.match(value)):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


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
    # /1's keys exactly, or under /2 those and status_rule (a /1 seal naming one is refused by name).
    bundled = seal.get("schema") == SEAL_SCHEMA_BUNDLE
    for message in key_problems(seal, SEAL_KEYS_BUNDLE if bundled else SEAL_KEYS):
        problems.add("SEAL.json", message)
    # A missing key is reported once, above; the checks below judge the values present.
    if "schema" in seal and seal["schema"] not in (SEAL_SCHEMA, SEAL_SCHEMA_BUNDLE):
        problems.add("SEAL.json", f"schema must be {SEAL_SCHEMA!r} (or {SEAL_SCHEMA_BUNDLE!r} with its status_rule)")
    if bundled and "status_rule" in seal and (type(seal["status_rule"]) is not int
                                              or seal["status_rule"] not in BUNDLE_STATUS_RULES):
        problems.add("SEAL.json", f"status_rule must be {STATUS_RULE_BUNDLE} or {STATUS_RULE_BROAD} under "
                                  f"{SEAL_SCHEMA_BUNDLE!r}, the derivation its records' statuses came from "
                                  f"(status rule {STATUS_RULE_D1} exports {SEAL_SCHEMA!r})")
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
    # The seal's status rule (seal_status_rule): 1 for a /1 set, whose records keep /1's shapes.
    status_rule: int = STATUS_RULE_D1

    @property
    def bundled(self) -> bool:
        """Whether the set is /2: revision-free keys on its records, a coverage claim as a bundle."""
        return self.status_rule >= STATUS_RULE_BUNDLE


def _locate_ext(node: dict) -> dict | None:
    ext = node.get("extensions")
    value = ext.get(LOCATE) if isinstance(ext, dict) else None
    return value if isinstance(value, dict) else None


def _ends_with_space(head: Any, target: bytes) -> bool:
    """Whether the text hashed into ``head`` (a sha256 object), followed by no whitespace or by
    one of the TRAILING_SPACE runs, has the digest ``target``."""
    for run in _TRAILING_SPACE_BYTES:
        candidate = head.copy()
        candidate.update(run)
        if candidate.digest() == target:
            return True
    return False


def quote_extent(quote: dict, span_sha256: Any, span: list[int] | None = None) -> dict[str, bool]:
    """How much of its passage a quote shows, as two passage fields (docs/ATLAS.md section 4).

    ``quote_capped`` is true when the quote is the capped head of a longer passage rather than
    all of it, and ``passage_ends_in_suffix`` is true when it is such a head and the rest of the
    passage lies within the quote's suffix, which the passage card shows after it.

    The export's ``exact`` is the passage from its start to the earlier of the end of its
    QUOTE_MAX_WORDS-th word and the passage's own end, right-stripped; its ``suffix``, when the
    quote needs one to be unique, is the text right after ``exact``, which may run past the
    passage's end; and the subject end's ``span_sha256`` is the sha256 of the whole passage
    span, trailing whitespace included (formal-companion scripts/locate/records.py,
    ``cap_quote`` and ``passage_records``). So a quote under the cap is the whole passage (the
    passage ended first). A quote at the cap is the whole passage exactly when the span is the
    quote followed by whitespace only, which is tried for every TRAILING_SPACE run; and failing
    that, the passage ends in the suffix exactly when the span is the quote followed by the
    suffix's first characters, or by all of it and a TRAILING_SPACE run.

    F5's ``span`` (formal-companion 722b30f), when the record carries one, is a shortcut to the
    same answer: the span's text after ``exact`` is ``tail = (end - start) - len(exact)`` code
    points. When the suffix covers that tail, the quote gives the span's whole text, which
    span_problems has held to ``span_sha256``; when the tail runs past the suffix by more than
    any TRAILING_SPACE run, no text the hash path tries is that long, so the quote is a head;
    and a tail that ends one or two characters past the suffix is left to ``span_sha256``, the
    only field that says whether those characters are whitespace. The builder requires a 64-hex
    ``span_sha256`` (subject_problems); called without one, and without a span that decides, a
    quote at the cap reads as a head whose passage goes on past its suffix, so a cut passage is
    never shown as whole. The page marks a head with an ellipsis outside the quotation; the
    quote itself is never changed.
    """
    whole = {"quote_capped": False, "passage_ends_in_suffix": False}
    head = {"quote_capped": True, "passage_ends_in_suffix": False}
    ends = {"quote_capped": True, "passage_ends_in_suffix": True}
    exact = quote["exact"]
    if len(WORD_SPACE.split(exact.strip())) < QUOTE_MAX_WORDS:
        return whole
    suffix = quote.get("suffix")
    suffix = suffix if isinstance(suffix, str) else ""
    if span is not None:
        tail = span[1] - span[0] - len(exact)
        if 0 <= tail <= len(suffix):
            rest = suffix[:tail]
            return whole if not rest or rest.isspace() else ends
        if tail > len(suffix) + MAX_TRAILING_SPACE:
            return head
    # A span that runs past a suffix holding any non-whitespace proves the passage goes on past
    # the quote, so the hash below may say where it ends but never that the quote is all of it:
    # such a set is inconsistent producer data, and a cut passage is never shown as whole.
    never_whole = span is not None and span[1] - span[0] - len(exact) > len(suffix) and bool(suffix.strip())
    found = _extent_from_hash(exact, suffix, span_sha256, whole, head, ends)
    return head if never_whole and found is whole else found


def _extent_from_hash(exact: str, suffix: str, span_sha256: Any, whole: dict[str, bool],
                      head: dict[str, bool], ends: dict[str, bool]) -> dict[str, bool]:
    """quote_extent's answer from ``span_sha256`` alone, for a quote at the cap."""
    if not (isinstance(span_sha256, str) and HEX64.match(span_sha256)):
        return head
    target = bytes.fromhex(span_sha256)
    base = hashlib.sha256(exact.encode("utf-8"))
    if _ends_with_space(base, target):
        return whole
    if not suffix:
        return head
    # The span is the quote followed by suffix[:k] for some k, or by the whole suffix and the
    # whitespace the suffix stopped before; whitespace alone after the quote is a whole passage.
    for k in range(1, len(suffix) + 1):
        candidate = base.copy()
        candidate.update(suffix[:k].encode("utf-8"))
        if candidate.digest() == target or (k == len(suffix) and _ends_with_space(candidate, target)):
            return whole if suffix[:k].isspace() else ends
    return head


def quote_capped(quote: dict, span_sha256: Any, span: list[int] | None = None) -> bool:
    """Whether a passage's quote is the capped head of a longer passage rather than all of it
    (quote_extent's ``quote_capped``)."""
    return quote_extent(quote, span_sha256, span)["quote_capped"]


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


BUNDLE_RULE = ('coverage, on a passage with a claim, is its bundle {"complete": true, "parts": [<location_key>, ...], '
               '"challenge", "relation", "conjuncts": [{"id", "text", "part"}], "bundle_key"} under '
               f'{SEAL_SCHEMA_BUNDLE}, and is absent (never null) on a passage without one')


def bundle_problems(claim: dict, parts: Any, relation_required: bool) -> list[str]:
    """Why a /2 claim is not a bundle: its relation, conjuncts and bundle_key (their shapes only).

    The relation is the bundle's, its weakest part's, one of BUNDLE_RELATIONS. The conjuncts are
    MIN_CONJUNCTS..MAX_CONJUNCTS objects {id, text, part}, ids c1..cN in order, each text
    non-empty, each part one of the claim's parts and every part stating at least one; the
    bundle_key is 64 lowercase hex, recomputed over the parts' pair keys in build_passages. Each
    is judged when present: a record's claim must carry all three (coverage_claim's key check
    names a missing one), and the producer's derive_status, which derive_passage_status mirrors,
    requires only the relation (cflib.vocab._bundle_fields), so ``relation_required`` names it.
    """
    found = []
    if "relation" not in claim:
        if relation_required:
            found.append("coverage.relation is missing: a bundle carries its relation, its weakest part's")
    elif not member(claim["relation"], BUNDLE_RELATIONS):
        found.append(f"coverage.relation must be one of {sorted(BUNDLE_RELATIONS)} (the bundle's, its weakest "
                     "part's: every part states its conjuncts strongly)")
    if "conjuncts" in claim:
        conjuncts = claim["conjuncts"]
        if not isinstance(conjuncts, list) or not MIN_CONJUNCTS <= len(conjuncts) <= MAX_CONJUNCTS:
            found.append(f"coverage.conjuncts must be {MIN_CONJUNCTS} to {MAX_CONJUNCTS} {{id, text, part}} objects "
                         "(a bundle stands only on a compound statement)")
        else:
            named = set()
            for number, conjunct in enumerate(conjuncts, 1):
                if not isinstance(conjunct, dict) or set(conjunct) != CONJUNCT_KEYS:
                    found.append(f"coverage.conjuncts[{number - 1}] must be exactly {{id, text, part}}")
                    continue
                if conjunct["id"] != f"c{number}":
                    found.append(f"coverage.conjuncts are c1..c{len(conjuncts)} in order; conjunct {number} is "
                                 f"{conjunct['id']!r}")
                message = _string_problem(conjunct["text"], MAX_CONJUNCT_CHARS)
                if message:
                    found.append(f"coverage.conjuncts {conjunct['id']!r} text {message} (at most "
                                 f"{MAX_CONJUNCT_CHARS} characters)")
                if isinstance(parts, list) and conjunct["part"] not in parts:
                    found.append(f"coverage.conjuncts {conjunct['id']!r} names part {str(conjunct['part'])[:12]}, "
                                 "which is not one of the bundle's parts")
                if isinstance(conjunct["part"], str):
                    named.add(conjunct["part"])
            if isinstance(parts, list):
                idle = [key for key in parts if key not in named]
                if idle:
                    found.append(f"coverage: bundle part {str(idle[0])[:12]} states no conjunct (a bundle's parts "
                                 "partition its conjuncts, each stating at least one)")
    if "bundle_key" in claim and (not isinstance(claim["bundle_key"], str) or not HEX64.match(claim["bundle_key"])):
        found.append("coverage.bundle_key must be 64 lowercase hex: the sha256 of the passage key and the parts' "
                     "pair keys, sorted, one per line")
    return found


def coverage_claim(ext: dict, at: str, problems: Problems, status_rule: int = STATUS_RULE_D1,
                   exported: bool = True) -> dict | None:
    """One record's copy of its passage's coverage claim (decision D1), or None when it carries none.

    The claim's own shape is checked here: exactly complete, parts and challenge; complete is
    true; parts names at least MIN_COVERAGE_PARTS distinct location keys; challenge is a challenge
    value. That each part is a hasPart location of the same passage is checked over the passage's
    records (coverage_part_problems). A malformed claim is reported and dropped.

    Under status rules 2 and 3 (/2) the claim is a bundle: COVERAGE_KEYS_BUNDLE exactly when
    ``exported`` (a record's claim), within them otherwise (derive_passage_status, the producer's
    signature), and bundle_problems judges what the bundle adds.
    """
    if "coverage" not in ext:
        return None
    claim = ext["coverage"]
    bundled = status_rule >= STATUS_RULE_BUNDLE
    if not isinstance(claim, dict):
        problems.add(at, BUNDLE_RULE if bundled else COVERAGE_RULE)
        return None
    if not bundled:
        found = [f"coverage: {message}" for message in key_problems(claim, COVERAGE_KEYS)]
    elif exported:
        found = [f"coverage: {message}" for message in key_problems(claim, COVERAGE_KEYS_BUNDLE)]
    else:
        found = [f"coverage: {message}" for message in key_problems(claim, COVERAGE_KEYS,
                                                                    COVERAGE_KEYS_BUNDLE[len(COVERAGE_KEYS):])]
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
    if bundled:
        found.extend(bundle_problems(claim, parts, relation_required=not exported))
    for message in found:
        problems.add(at, message)
    if found:
        return None
    out = {"complete": True, "parts": list(parts), "challenge": claim["challenge"]}
    if bundled:
        # The bundle's own fields, as exported (a record always carries all three; the
        # one-passage form may lack conjuncts and bundle_key, as the producer's call may).
        out["relation"] = claim["relation"]
        for key in ("conjuncts", "bundle_key"):
            if key in claim:
                out[key] = [dict(c) for c in claim[key]] if key == "conjuncts" else claim[key]
    return out


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


PASSAGE_KEY_RULE = ('passage_key must be sha256("<pageid>\\n<span_sha256>") of its subject end, 64 lowercase hex '
                    f"({SEAL_SCHEMA_BUNDLE}: every record carries its passage's revision-free key)")
PAIR_KEY_RULE = ('pair_key must be sha256("<passage_key>\\n<repository>\\n<declaration>"), the repository the lean '
                 f"end's uri, 64 lowercase hex ({SEAL_SCHEMA_BUNDLE}: every location record carries one)")


def record_passage_key(ext: dict, source: dict, span_sha: str | None, at: str, problems: Problems) -> str | None:
    """A /2 record's passage_key, recomputed from its source's page id and its span_sha256; None if refused."""
    value = ext.get("passage_key")
    if not isinstance(value, str) or not HEX64.match(value):
        problems.add(at, PASSAGE_KEY_RULE)
        return None
    if span_sha is not None and HEX64.match(span_sha) and value != passage_key(source["pageid"], span_sha):
        problems.add(at, f"{PASSAGE_KEY_RULE}; this one is not")
        return None
    return value


def identifiers_problems(end: dict, source: dict, prominence: str | None) -> list[str]:
    """The subject end's optional identifiers under /2: the article's QID on a subject passage only.

    The maintainer's decision B5 (formal-companion scripts/locate/records.py, article_qid): only a
    subject passage's records carry their source's wikidata, as identifiers.wikidata, and not every
    one does (a challenge_pending record, or a source with no QID, carries none), so absence is
    never refused.
    """
    if "identifiers" not in end:
        return []
    found = []
    qid = source["wikidata"]
    if qid is None or end["identifiers"] != {"wikidata": qid}:
        found.append("the subject end's identifiers must be {\"wikidata\": <its source's QID>} (sources.jsonl "
                     "wikidata; decision B5)")
    if prominence != "subject":
        found.append("only a subject passage's records carry the article's QID as identifiers.wikidata "
                     "(decision B5)")
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
    # Under /2 every shape adds passage_key and a location pair_key; under /1 both are refused by name.
    shapes = SHAPE_EXT_KEYS_BUNDLE if ctx.bundled else SHAPE_EXT_KEYS
    record_keys = RECORD_EXT_KEYS_BUNDLE if ctx.bundled else RECORD_EXT_KEYS
    shape_keys = shapes[shape] if shape else tuple(k for keys in shapes.values() for k in keys)
    for message in (*foreign_extensions(record, "the record", rid),
                    *unexpected_keys(ext, record_keys + RECORD_EXT_OPTIONAL + shape_keys, SHAPE_LEVELS[shape],
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
    coverage = coverage_claim(ext, at, problems, ctx.status_rule)

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
    key = None
    if ctx.bundled:
        key = record_passage_key(ext, source, span_sha, at, problems)
        for message in identifiers_problems(end, source, prominence):
            problems.add(at, message)

    base = {"item": item, "id": record.get("id"), "kind": kind, "section": section, "prominence": prominence,
            # /2's revision-free passage key (None under /1), a passage field like the rest.
            "passage_key": key,
            "quote": {key: quote[key] for key in ("exact", "prefix", "suffix") if key in quote},
            # Passage fields every record of the passage carries alike (build_passages). The view
            # model copies neither: quote_extent reads them to tell a capped quote from a whole
            # passage, and the records keep them verbatim.
            "span": span, "span_sha256": span_sha,
            "subject_uri": uri, "challenge": challenge, "coverage": coverage,
            # The source's licence, as the subject end names it: the article view states it once.
            "licence": subject_ext.get("licence") if isinstance(subject_ext.get("licence"), str) else None}
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
    if ctx.bundled:
        # The revision- and commit-free key a verdict on this pair is stored by (wiki/src/atlas.ts
        # copies it into each feedback row), recomputed from the passage key, uri and declaration.
        pair = ext.get("pair_key")
        if not isinstance(pair, str) or not HEX64.match(pair):
            problems.add(at, PAIR_KEY_RULE)
            pair = None
        elif base["passage_key"] is not None and isinstance(end.get("uri"), str) \
                and pair != pair_key(base["passage_key"], end["uri"], location["declaration"]):
            problems.add(at, f"{PAIR_KEY_RULE}; this one is not")
            pair = None
        view["pair_key"] = pair
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


def _theorem_row(loc: dict, status_rule: int = STATUS_RULE_D1) -> str:
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
    # RingEquiv); the table's "broadMatch to a definition" stays partial under status rules 1
    # and 2, and formalizes it under 3 (R-BROAD: the relation alone, never the declaration's
    # kind, decides between the strong three). Data resting on sorry has proof_status
    # statement_only, above.
    if loc["relation"] in DATA_MATCH_RELATIONS or status_rule >= STATUS_RULE_BROAD:
        return "formalized"
    return "partial"


def _definition_row(loc: dict, status_rule: int = STATUS_RULE_D1) -> str:
    """One location of a definition passage (cflib.vocab._definition_row)."""
    if loc["relation"] in PART_RELATIONS:
        return "partial"
    if loc["relation"] == "broadMatch":
        # Status rule 3 (R-BROAD): a broadMatch to a definition formalizes it, as an exact or
        # close match does; a broadMatch to anything else stays partial, as under rules 1 and 2.
        return "formalized" if status_rule >= STATUS_RULE_BROAD and loc["proof_status"] == "definition" else "partial"
    # exactMatch, closeMatch
    if loc["proof_status"] == "definition":
        return "formalized"
    if loc["proof_status"] in UNPROVED:
        return "statement_only"
    # A proved theorem stating a definition passage: not in the table; §1.11 says partial.
    return "partial"


def location_status(kind: str, loc: dict, status_rule: int = STATUS_RULE_D1) -> str | None:
    """The status one location gives a passage of `kind`; None for a relatedMatch, which never counts."""
    if loc["relation"] == "relatedMatch":
        return None
    rows = []
    if kind in THEOREM_KINDS or kind in EITHER_KINDS:
        rows.append(_theorem_row(loc, status_rule))
    if kind in DEFINITION_KINDS or kind in EITHER_KINDS:
        rows.append(_definition_row(loc, status_rule))
    return min(rows, key=STATUS_RANK.__getitem__) if rows else None


def part_finished(part: dict) -> bool:
    """Whether a coverage claim's part is finished, by the round-three reading of D1.

    Finished: proved, or data (a definition without the statement flag). Unfinished:
    statement_only, wanted or axiom_dependent, or a Prop-valued definition (the flag set),
    whatever its proof_status: it states a clause and proves none.
    """
    return part["proof_status"] == "proved" or (part["proof_status"] == "definition" and not part["statement"])


def coverage_location(row: str, parts: list[dict], relation: str = COVERAGE_ACTS_AS) -> dict:
    """The one location a survived claim acts as, on the theorem or definition row.

    It is a fixed stand-in, not any one part's own proof status and flag: when every part is
    finished it is proved on the theorem row and data (definition, no flag) on the definition
    row, so the claim formalizes the passage on either row (so a data part counts, as an exact
    match to data does, Q2; and the definition row has the statement_only case an exact match to
    an unfinished declaration has, Q3); when any part is unfinished it is a statement_only
    declaration on both rows. Read literally, "the least finished part" would be wrong on the
    definition row: a claim of proved parts formalizes a definition passage, where an exact match
    to a proved theorem is partial, and one with a Prop-valued-definition part is statement_only,
    where an exact match to that definition formalizes it.

    Its relation is exactMatch under status rule 1 (decision D1) and, under status rules 2 and 3,
    the bundle's own relation, its weakest part's (decision Q9; cflib.vocab.derive_status).
    """
    if not all(part_finished(part) for part in parts):
        proof_status = "statement_only"
    else:
        proof_status = "proved" if row == "theorem" else "definition"
    return {"relation": relation, "proof_status": proof_status, "statement": False}


def coverage_status(kind: str, claim: dict | None, locations: list[dict],
                    status_rule: int = STATUS_RULE_D1) -> str | None:
    """The status a coverage claim gives a passage of `kind` on its own; None when it gives none.

    Only a claim whose challenge is survived counts: one pending, unjudged (not_required,
    sampled_out) or downgraded upgrades nothing, and a refuted claim is not exported at all. A
    survived claim is judged as its coverage_location on the passage kind's row of the table, an
    example or `other` passage taking the better of the two rows exactly as for an ordinary
    location (Q1), so under status rule 1 it gives formalized when every part is finished and
    statement_only otherwise. Under status rules 2 and 3 the stand-in has the bundle's relation,
    so a broadMatch bundle is judged as one broadMatch location of that rule.
    """
    if claim is None or claim["challenge"] != COVERAGE_SURVIVED:
        return None
    by_key = {loc["location_key"]: loc for loc in locations}
    parts = [by_key.get(key) for key in claim["parts"]]
    if (len(parts) < MIN_COVERAGE_PARTS or any(part is None or part["relation"] != COVERAGE_RELATION
                                               for part in parts)):
        return None  # refused by coverage_part_problems; never counted
    relation = claim["relation"] if status_rule >= STATUS_RULE_BUNDLE else COVERAGE_ACTS_AS
    rows = []
    if kind in THEOREM_KINDS or kind in EITHER_KINDS:
        rows.append(_theorem_row(coverage_location("theorem", parts, relation), status_rule))
    if kind in DEFINITION_KINDS or kind in EITHER_KINDS:
        rows.append(_definition_row(coverage_location("definition", parts, relation), status_rule))
    return min(rows, key=STATUS_RANK.__getitem__) if rows else None


def derive_status(kind: str, locations: list[dict], coverage: dict | None = None,
                  status_rule: int = STATUS_RULE_D1) -> tuple[str | None, Any]:
    """Proposal §2.6's derived status of a located passage, recomputed from its locations.

    A port of formal-companion's cflib.vocab.derive_status (scripts/cflib/vocab.py, which
    implements references/vocabulary.md §1.11's decisions for the cases the table leaves
    open, decision D1 among them): each location that counts gets a status from its kind's
    row of the table, an example or `other` passage taking the better of the two rows; a
    survived coverage claim over the passage's hasPart locations counts as one more exactMatch
    location, a fixed stand-in that is finished only when every part is (coverage_location,
    coverage_status); and the passage takes the best of them. `locations` are in rank order.

    ``status_rule`` is the seal's (seal_status_rule): 1 is all of the above; 2 makes a survived
    bundle one more location of its own relation; 3 (R-BROAD) is 2 with a broadMatch to data
    formalizing a theorem passage and a broadMatch to a definition formalizing a definition one.

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
        status = location_status(kind, loc, status_rule)
        if status is not None and (best is None or STATUS_RANK[status] < STATUS_RANK[best]):
            best, basis = status, loc
    claimed = coverage_status(kind, coverage, locations, status_rule)
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
        bundle = f" {coverage['relation']} bundle" if "relation" in coverage else " coverage claim"
        text += f", and a{bundle} over {len(coverage['parts'])} parts (challenge {coverage['challenge']})"
    return text


def status_problems(kind: str | None, status: str, locations: list[dict],
                    coverage: dict | None = None, status_rule: int = STATUS_RULE_D1) -> tuple[list[str], Any]:
    """Compare a located passage's stamped status with the recomputed one; returns (problems, basis)."""
    if status == "not_formalized":
        return ["not_formalized needs a checked absence (noMatch record)"], None
    if not member(kind, KINDS):
        return [], None  # already reported by classify_record
    derived, basis = derive_status(kind, locations, coverage, status_rule)
    if derived is None:
        return [f"proposal §2.6 gives a {kind} passage with {_shape(locations)} no status: it is unresolved, "
                f"and its records carry no status (docs/ATLAS.md, contract note 9)"], None
    if derived != status:
        rule = "" if status_rule == STATUS_RULE_D1 else f", status rule {status_rule}"
        return [f"the records say {status}, but the derived-status rules (proposal §2.6 and vocabulary.md "
                f"§1.11{rule}) derive {derived} for a {kind} passage with {_shape(locations, coverage)}"], None
    return [], basis


def derive_passage_status(kind: Any, locations: list[dict], absent: bool, coverage: Any = None,
                          status_rule: int = STATUS_RULE_D1) -> str | None:
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

    ``status_rule`` is the producer's keyword of the same name, the seal's: under 2 and 3 the
    claim is a bundle with its relation, and conjuncts and bundle_key are checked when given, as
    the producer checks them (fixtures/status_parity-2.json and -3.json, its tables for them).
    """
    if type(status_rule) is not int or status_rule not in (STATUS_RULE_D1, *BUNDLE_STATUS_RULES):
        raise StatusRefused(f"status rule {status_rule!r} is not one this builder derives by")
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
        claim = coverage_claim({"coverage": coverage}, "the claim", problems, status_rule, exported=False)
        found = problems.items or coverage_part_problems(claim, rows)
        if found:
            raise StatusRefused("; ".join(found))
    if absent:
        return "not_formalized"
    return derive_status(kind, rows, claim, status_rule)[0]


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


def bundle_key_problems(claim: dict, passage: str | None, locations: list[dict]) -> list[str]:
    """Whether a /2 bundle's bundle_key is the sha256 of its passage key and its parts' pair keys."""
    pairs = {loc["location_key"]: loc.get("pair_key") for loc in locations}
    keys = [pairs.get(key) for key in claim["parts"]]
    if passage is None or not all(isinstance(key, str) for key in keys):
        return []  # a refused passage or pair key is reported already
    if claim["bundle_key"] != bundle_key(passage, keys):
        return ["coverage.bundle_key is not the sha256 of the passage key and the parts' pair keys, sorted, one "
                "per line (cflib.hashing.bundle_key)"]
    return []


def build_passages(classified: list[dict], source: dict, problems: Problems,
                   status_rule: int = STATUS_RULE_D1) -> list[dict]:
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
            # same claim, or none does. So are the subject end's span and span_sha256, which the
            # page's ellipsis reads (quote_extent), so that no record order decides it.
            for key in ("kind", "quote", "section", "prominence", "coverage", "span", "span_sha256", "passage_key"):
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
            if not found and status_rule >= STATUS_RULE_BUNDLE:
                found = bundle_key_problems(claim, first["passage_key"], locations)
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
                found, basis = status_problems(first["kind"], status, locations, claim, status_rule)
                for message in found:
                    problems.add(at, message)
        coverage = None
        if claim is not None:
            # The claim as exported, plus what it gives on its own (formalized or statement_only
            # once it survived, null otherwise) and whether it is what justifies the passage's
            # status; its parts' challenge values then read as confirmed by it.
            coverage = {**claim, "status": (coverage_status(first["kind"], claim, locations, status_rule)
                                            if member(first["kind"], KINDS) else None),
                        "basis": basis == COVERAGE_BASIS}
            locations = confirmed_parts(claim, locations)
        absence = None
        if absences:
            absence = {"universe": None, "universe_sha256": None, "libraries": None,
                       "receipt": absences[0]["receipt"], "challenge": absences[0]["challenge"]}
        extra = {"passage_key": first["passage_key"]} if status_rule >= STATUS_RULE_BUNDLE else {}
        passages.append({
            **extra,
            "id": pid, "item": first["item"], "kind": first["kind"], "section": first["section"],
            "prominence": first["prominence"], "quote": first["quote"],
            # quote_capped: the quote is the capped head of a longer passage, so the page marks
            # it; passage_ends_in_suffix: the rest of that passage is within the quote's suffix.
            **quote_extent(first["quote"], first["span_sha256"], first["span"]),
            "subject_uri": first["subject_uri"], "outcome": outcome, "status": status, "locations": locations, "absence": absence,
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


def _prominence_counts() -> dict[str, dict]:
    """Passages and their statuses per prominence: the subject and central passages are the
    article's statements, which the pages count apart from the supporting ones (worked
    examples, setup sentences), so a headline is not set by the supporting passages."""
    return {prominence: {"passages": 0, "by_status": _status_counts()} for prominence in sorted(PROMINENCES)}


def article_view(source: dict, passages: list[dict], universe: dict, seal: dict, seal_sha: str,
                 records_sha: str, licence: str | None = None) -> dict:
    """The article's view model. Runs only on a set that passed every check.

    `licence` is the source's, as its records' subject ends name it: the view's `licence` says
    that the text it quotes (each passage's quote, and its text when a side file gives it) is
    under it, and the mapping under MAPPING_LICENCE.
    """
    by_status = _status_counts()
    by_prominence = _prominence_counts()
    by_library: dict[str, dict[str, int]] = {}
    by_proof = Counter()
    by_relation = Counter()
    n_locations = n_related = 0
    for passage in passages:
        by_status[passage["status"] or "unresolved"] += 1
        # Every record of a set that passed carries its passage's prominence.
        bucket = by_prominence[passage["prominence"]]
        bucket["passages"] += 1
        bucket["by_status"][passage["status"] or "unresolved"] += 1
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
    view = {
        "schema": ARTICLE_SCHEMA,
        "pageid": source["pageid"], "title": source["title"], "revision": source["revision"],
        "source_id": source["source_id"], "wikidata": source["wikidata"], "assessment": source["assessment"],
        "source": {"html_sha256": source["html_sha256"], "layer_sha256": source["layer_sha256"],
                   "renderer": source["renderer"]},
        "campaign": seal["campaign"],
        "universe": {"id": universe["id"], "sha256": universe["sha256"], "frozen": universe["frozen"],
                     "libraries": len(universe["libraries"])},
        "record_set": seal_sha, "records_sha256": records_sha,
        "licence": {"text": licence, "mapping": MAPPING_LICENCE},
        "summary": {"passages": len(passages), "locations": n_locations, "related": n_related, "by_status": by_status,
                    "by_prominence": by_prominence, "by_library": by_library, "by_proof_status": dict(by_proof),
                    "by_relation": dict(by_relation)},
        "passages": passages,
    }
    if seal.get("schema") == SEAL_SCHEMA_BUNDLE:
        # What a page's provenance names beside the campaign token, optional within v1 (a /1
        # set's view is unchanged): the seal's set, schema, status rule, export time and the
        # producer's commit. The seal records no model and no operator, so neither is here.
        view["seal"] = seal_summary(seal)
    return view


def seal_summary(seal: dict) -> dict:
    """A /2 seal's provenance fields, as a view model and current.json's record_sets carry them."""
    return {"set_id": seal["set_id"], "schema": seal["schema"], "status_rule": seal["status_rule"],
            "created": seal["created"], "skill_commit": seal["skill_commit"]}


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


@dataclass
class BuiltSet:
    """One sealed record set that passed every check, and its articles' view models."""
    seal: dict
    seal_sha: str
    universe: dict
    universe_bytes: bytes
    # (view model, records file bytes) per source, in sources.jsonl order.
    articles: list[tuple[dict, bytes]]
    # Optional (--reader-html): page id -> the article's marked revision, reader/<pageid>.html.
    reader_files: dict[int, bytes] = field(default_factory=dict)
    # And page id -> that revision's text layer, which the body's text was proven to be (its sha256
    # is the records' layer_sha256): what a passage's context is checked against.
    reader_layers: dict[int, str] = field(default_factory=dict)


def build_set(set_dir: Path, validate: Any, schema_error: Any) -> BuiltSet:
    """Check one sealed record set and build its articles' view models (AtlasError when refused)."""
    seal, seal_sha, files = read_record_set(set_dir)
    problems = Problems()
    universe = load_universe(seal, files, problems)
    sources = load_sources(files, problems)
    problems.raise_if_any()

    ctx = Context(universe=universe, files=files, validate=validate, schema_error=schema_error,
                  status_rule=seal_status_rule(seal))
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
        licences = sorted({r["licence"] for r in classified if r["licence"] is not None})
        if len(licences) > 1:
            problems.add(path, f"every record of an article names one licence, its source's (the subject end's "
                               f"licence): {', '.join(licences)}")
        built.append((source, build_passages(classified, source, problems, ctx.status_rule),
                      licences[0] if len(licences) == 1 else None))
    for receipt in sorted(p for p in files if p.startswith("receipts/")):
        if receipt not in ctx.receipts_used:
            problems.add(receipt, "receipt referenced by no absence record")
        elif receipt != f"receipts/{sha256_hex(files[receipt])}.json":
            problems.add(receipt, "a receipt's file name is the sha256 of its bytes")
    # Every check has run: a refused set never reaches the view code, which assumes the contract.
    problems.raise_if_any()
    articles = []
    for source, passages, licence in built:
        records = files[f"records/{source['source_id']}.jsonl"]
        articles.append((article_view(source, passages, universe, seal, seal_sha, sha256_hex(records), licence),
                         records))
    return BuiltSet(seal=seal, seal_sha=seal_sha, universe=universe, universe_bytes=files["universe.json"],
                    articles=articles)


# What several record sets built into one store share (combined_sets), as SEAL.json names it.
SHARED_SEAL_FIELDS = ("schema", "universe", "sourceset", "skill_commit")
COMBINED_RULE = ("record sets built into one store share their universe (universe.json byte for byte), sourceset, "
                 "skill_commit, schema and status_rule, and no page id or title key")


def combined_sets(sets: list[BuiltSet]) -> list[BuiltSet]:
    """Several record sets as one store's sets, in set_id order; AtlasError unless they agree.

    Two locate runs need two tallies and two exports, so a batch can arrive as several sealed
    sets. They build into one store only when nothing about the Lean side differs (one universe,
    one sourceset, one producer commit, one schema and status rule) and their pages are disjoint
    by page id and by title key, so every index entry and article has exactly one set. Each
    article keeps its own seal's campaign and record_set, which the feedback route checks.
    """
    problems = Problems()
    ordered = sorted(sets, key=lambda built: built.seal["set_id"])
    first = ordered[0]
    seen_ids: set[str] = set()
    seen_shas: set[str] = set()
    pages: dict[int, str] = {}
    titles: dict[str, str] = {}
    for built in ordered:
        set_id = built.seal["set_id"]
        if set_id in seen_ids:
            problems.add(f"record set {set_id}", "set_id appears twice: each record set is built in once")
        if built.seal_sha in seen_shas:
            problems.add(f"record set {set_id}", "the same SEAL.json is given twice")
        seen_ids.add(set_id)
        seen_shas.add(built.seal_sha)
        if built.universe_bytes != first.universe_bytes:
            problems.add(f"record set {set_id}", f"universe.json differs from {first.seal['set_id']}'s: {COMBINED_RULE}")
        for key in (*SHARED_SEAL_FIELDS, "status_rule"):
            if built.seal.get(key) != first.seal.get(key):
                problems.add(f"record set {set_id}", f"SEAL.json {key} differs from {first.seal['set_id']}'s: "
                                                     f"{COMBINED_RULE}")
        for article, _ in built.articles:
            other = pages.setdefault(article["pageid"], set_id)
            if other != set_id:
                problems.add(f"record set {set_id}", f"page {article['pageid']} is also in {other}: {COMBINED_RULE}")
            other = titles.setdefault(title_key(article["title"]), set_id)
            if other != set_id:
                problems.add(f"record set {set_id}", f"title {article['title']!r} has a title key also in {other}: "
                                                     f"{COMBINED_RULE}")
    problems.raise_if_any()
    return ordered


def record_set_entry(seal: dict, seal_sha: str) -> dict:
    """current.json's record_set: one seal's identity (and, under /2, the rule its statuses came by)."""
    entry = {"seal_sha256": seal_sha, "set_id": seal["set_id"], "created": seal["created"],
             "skill_commit": seal["skill_commit"], "byte_budget": seal["byte_budget"],
             "sourceset": {"id": seal["sourceset"]["id"], "manifest_sha256": seal["sourceset"]["manifest_sha256"]}}
    if seal.get("schema") == SEAL_SCHEMA_BUNDLE:
        entry.update(schema=seal["schema"], status_rule=seal["status_rule"])
    return entry


# ---- supplements: optional side files merged into the view (docs/ATLAS.md, "Supplements") -----
#
# A record set carries a passage's first 12 words, a declaration's name and the runs' ids. Optional
# side files, each read only and each checked against the records it describes, add what the
# record set does not hold: the passage's full text, the declaration's signature, who and what
# made each record, the article's marked revision, and what an audit of the links measured. A
# build without them is byte for byte the build it always was; a side file that does not check
# out is refused like a broken record set, never merged in part.

PASSAGE_TEXT_SCHEMA = "wikilean.atlas.passage-text/1"
SIGNATURES_SCHEMA = "wikilean.atlas.signatures/1"
PROVENANCE_SCHEMA = "wikilean.atlas.provenance/1"
READER_HTML_SCHEMA = "wikilean.atlas.reader-html/1"
AUDIT_SUMMARY_SCHEMA = "wikilean.atlas.audit-summary/1"
SUPPLEMENT_FLAGS = {"passage_text": "--passage-text", "signatures": "--signatures", "provenance": "--provenance",
                    "reader_html": "--reader-html", "audit_summary": "--audit-summary"}
SUPPLEMENT_SCHEMAS = {"passage_text": PASSAGE_TEXT_SCHEMA, "signatures": SIGNATURES_SCHEMA,
                      "provenance": PROVENANCE_SCHEMA, "reader_html": READER_HTML_SCHEMA,
                      "audit_summary": AUDIT_SUMMARY_SCHEMA}

# A passage's text as segments that spell its layer slice in order.
SEGMENT_SHAPES = (frozenset({"text"}), frozenset({"ref"}), frozenset({"tex"}), frozenset({"tex", "display"}),
                  frozenset({"tex", "cut"}))
MAX_SEGMENTS = 1000
MAX_TEX_CHARS = 4000       # atlas-math's MAX_TEX_LENGTH: a longer formula would not render
# A passage's context (the rest of its sentence, or the sentence leading into a list item):
# what the page shows around it, at most this many characters of the layer a side.
MAX_CONTEXT_CHARS = 2000
CONTEXT_KEYS = {"before": frozenset({"layer", "segments", "trimmed", "lead_in"}),
                "after": frozenset({"layer", "segments", "trimmed"})}
TEX_ANNOTATION = re.compile(r"\{\\(?:displaystyle|textstyle|scriptstyle)(?![A-Za-z])")
TEX_STYLE_OR_SPACE = re.compile(r"\s+|\\(?:displaystyle|textstyle|scriptstyle)(?![A-Za-z])")
SEGMENT_RULE = ('segments spell the passage\'s layer slice in order: {"text"} and {"ref"} are its text verbatim, '
                '{"tex"} (with "display": true for a display formula) is one formula, standing for its MathML text '
                'and the TeX annotation after it, which holds its TeX, and {"tex", "cut"} is a formula the '
                'passage\'s edge cuts, "cut" being its text inside the passage')

# A declaration's signature: the Lean header the corpus's pretty-printed type lays out.
LEAN_KEYWORDS = frozenset({"theorem", "def", "abbrev", "instance", "class", "structure", "inductive", "axiom",
                           "opaque"})
MAX_SIGNATURE_CHARS = 4000
MAX_TYPE_CHARS = 20000
LEAN_NAME_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,300}\Z")

# Who and what made a record: the runs a campaign token names, and the inventory run whose
# passages the locate run searched (the run.yaml's inventory_run), which chose each passage, its
# kind and its prominence.
RUN_MODES = {"locate": "locate", "challenge": "locate-challenge", "inventory": "locate-inventory"}
SAMPLING_KEYS = ("seed", "sample_rate", "strong_sample_rate", "absence_sample_rate")

# What an audit of the build's links measured (--audit-summary): the record sets it read, the run
# that judged a sample of them (its id, operator, window and models, and whether it was blind and
# whether it was independent of the runs it judged) and its figures. Each figure estimates a share
# of the build: the share judged right and its 95% interval, the weights its items were drawn and
# weighed by (null for none) with the Kish effective sample size they leave (the number drawn when
# there are none), the items drawn and the population they stand for. The pages show it under
# "About this data"; without it they say the build carries no accuracy figure.
AUDIT_SUMMARY_KEYS = ("schema", "record_sets", "audit", "items", "figures")
AUDIT_RUN_KEYS = ("run_id", "operator", "window_utc", "models", "blind", "independent")
AUDIT_FIGURE_KEYS = ("id", "label", "share", "ci95", "weights", "n_eff", "drawn", "population")
MAX_AUDIT_MODELS = 4
MAX_AUDIT_FIGURES = 12
AUDIT_LABEL_RE = re.compile(r"^[A-Za-z0-9 ,.()\u00b7/+:;'&%_-]{1,160}\Z")
RUN_ID_RE = re.compile(rf"^{RUN_ID}\Z")


@dataclass
class Supplements:
    """The side files a build merges, each as (parsed document, sha256 of its bytes); None when not given."""
    passage_text: tuple[dict, str] | None = None
    signatures: tuple[dict, str] | None = None
    provenance: tuple[dict, str] | None = None
    reader_html: tuple[dict, str] | None = None
    audit_summary: tuple[dict, str] | None = None

    @classmethod
    def read(cls, passage_text: Path | None = None, signatures: Path | None = None,
             provenance: Path | None = None, reader_html: Path | None = None,
             audit_summary: Path | None = None) -> "Supplements | None":
        """Read the side files given (AtlasError when one cannot be read or names another schema)."""
        paths = {"passage_text": passage_text, "signatures": signatures, "provenance": provenance,
                 "reader_html": reader_html, "audit_summary": audit_summary}
        if all(path is None for path in paths.values()):
            return None
        return cls(**{kind: read_supplement(Path(path), kind) for kind, path in paths.items() if path is not None})

    def given(self) -> list[str]:
        return [kind for kind in SUPPLEMENT_FLAGS if getattr(self, kind) is not None]


def read_supplement(path: Path, kind: str) -> tuple[dict, str]:
    flag, schema = SUPPLEMENT_FLAGS[kind], SUPPLEMENT_SCHEMAS[kind]
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise AtlasError(f"{flag} {path}: cannot be read ({exc.strerror or exc})") from exc
    try:
        doc = strict_json(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AtlasError(f"{flag} {path}: not UTF-8 JSON ({exc})") from exc
    if not isinstance(doc, dict) or doc.get("schema") != schema:
        raise AtlasError(f"{flag} {path}: schema must be {schema!r}")
    return doc, sha256_hex(data)


def record_rows(records: bytes) -> list[dict]:
    """What a supplement joins on, per line of a records file whose set passed every check."""
    rows = []
    for line in records.decode("utf-8").splitlines():
        record = json.loads(line)
        ext = record["extensions"][LOCATE]
        subject = record["subject"][0]["extensions"][LOCATE]
        relation = record.get("relation")
        quote = (record["subject"][0].get("location") or {}).get("quote") or {}
        rows.append({"id": record["id"], "quote": {key: quote.get(key) for key in ("exact", "prefix", "suffix") if key in quote},
                     "shape": "unresolved" if relation is None else "absence" if relation == "noMatch" else "location",
                     "pageid": subject["pageid"], "span": subject.get("span"), "span_sha256": subject["span_sha256"],
                     "passage_key": ext.get("passage_key"), "location_key": ext.get("location_key"),
                     "pair_key": ext.get("pair_key"), "challenge": ext["challenge"]})
    return rows


def tex_group_end(s: str, i: int) -> int:
    """The end (past "}") of the balanced-brace group that starts at s[i] == "{", or -1 (atlas-math groupEnd)."""
    depth = 0
    j = i
    while j < len(s):
        ch = s[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return -1


def text_segments(segments: Any, layer: str) -> tuple[list[dict] | None, list[str]]:
    """The segments, checked to spell `layer` exactly: every prose, reference and cut piece is the
    layer's own text at the point the segments before it reach, and every whole formula runs from
    there to the end of the next TeX annotation, which holds its TeX (style commands and whitespace
    aside). So no word is shown that the verified slice does not say, and none of it is skipped."""
    if not isinstance(segments, list) or not 1 <= len(segments) <= MAX_SEGMENTS:
        return None, [f"segments must be a list of 1 to {MAX_SEGMENTS} segments"]
    out, cursor = [], 0
    for n, seg in enumerate(segments):
        if not isinstance(seg, dict) or frozenset(seg) not in SEGMENT_SHAPES:
            return None, [f"segment {n}: {SEGMENT_RULE}"]
        if "tex" in seg:
            tex = seg["tex"]
            if not isinstance(tex, str) or not tex.strip() or len(tex) > MAX_TEX_CHARS:
                return None, [f"segment {n}: tex must be a formula's TeX, at most {MAX_TEX_CHARS} characters"]
            if "display" in seg and seg["display"] is not True:
                return None, [f"segment {n}: display, when present, is true"]
        if "tex" in seg and "cut" not in seg:
            match = TEX_ANNOTATION.search(layer, cursor)
            end = tex_group_end(layer, match.start()) if match else -1
            if end < 0 or TEX_STYLE_OR_SPACE.sub("", seg["tex"]) not in TEX_STYLE_OR_SPACE.sub("", layer[match.start():end]):
                return None, [f"segment {n}: {SEGMENT_RULE}"]
            cursor = end
        else:
            piece = seg.get("text", seg.get("ref", seg.get("cut")))
            if not isinstance(piece, str) or not piece or not layer.startswith(piece, cursor):
                return None, [f"segment {n}: {SEGMENT_RULE}"]
            cursor += len(piece)
        out.append({key: seg[key] for key in sorted(seg)})
    if cursor != len(layer):
        return None, [f"the segments stop before the layer slice does: {SEGMENT_RULE}"]
    return out, []


def passage_text_entry(entry: Any, pid: str, row: dict, layer_text: str | None = None) -> tuple[dict | None, list[str]]:
    """One --passage-text entry as the passage's `text`, checked against its record and, when the
    build proved the article's text layer (`layer_text`, from its reader html), its context against
    that layer."""
    if not isinstance(entry, dict):
        return None, ["an entry is an object"]
    found = []
    if entry.get("id") != pid:
        found.append(f"id must be the passage's record id {pid!r}")
    if "pageid" in entry and entry["pageid"] != row["pageid"]:
        found.append("pageid must be the record's")
    if entry.get("span_sha256") != row["span_sha256"]:
        found.append("span_sha256 must be the record's span_sha256")
    if "span" in entry and row["span"] is not None and entry["span"] != row["span"]:
        found.append("span must be the record's span")
    layer = entry.get("layer")
    if not isinstance(layer, str) or sha256_hex(layer.encode("utf-8")) != row["span_sha256"]:
        found.append("layer must be the passage's text: the layer slice whose UTF-8 bytes the record's "
                     "span_sha256 is the sha256 of")
        return None, found
    segments, messages = text_segments(entry.get("segments"), layer)
    found += messages
    view = {"layer": layer, "segments": segments}
    # Optional context: the layer text just before and after the passage that the page shows
    # around it. The record hashes only the passage, so the builder checks the context's own
    # shape (its segments spell its text; it neither repeats nor overlaps the passage; the
    # record's quote prefix and suffix, where it has them, are where they must be). When the
    # build also proved the revision's layer (the article's reader html, whose text hashes to the
    # records' layer_sha256) and the record gives the passage's span, each context must be that
    # layer's own text right before or after the span; without them the side file's maker vouches
    # for it.
    for side in ("before", "after"):
        if side not in entry:
            continue
        context, messages = context_entry(entry[side], side, row)
        found += [f"{side}: {message}" for message in messages]
        if context is not None and layer_text is not None and row["span"] is not None:
            problem = context_layer_problem(context["layer"], side, row["span"], layer_text)
            if problem:
                found.append(f"{side}: {problem}")
                continue
        if context is not None:
            view[side] = context
    return (None, found) if found else (view, [])


def context_layer_problem(context: str, side: str, span: list[int], layer_text: str) -> str | None:
    """Why a context is not the revision's text right `side` the passage's span on the proven layer,
    or None. A space at the edge that meets the passage may differ (the layer has one where a
    block ends; the context may keep it or not)."""
    start, end = span
    if side == "before":
        head = layer_text[:start]
        if head.endswith(context) or head.rstrip(" ").endswith(context.rstrip(" ")):
            return None
    else:
        tail = layer_text[end:]
        if tail.startswith(context) or tail.lstrip(" ").startswith(context.lstrip(" ")):
            return None
    return (f"is not the revision's text right {side} the passage: the layer its reader html proves has other "
            f"text there (a context is the layer next to the passage's span)")


def context_entry(raw: Any, side: str, row: dict) -> tuple[dict | None, list[str]]:
    """One side of a passage's context, checked as far as the record allows."""
    if not isinstance(raw, dict) or not set(raw) <= CONTEXT_KEYS[side] or "layer" not in raw or "segments" not in raw:
        return None, [f"is an object of {sorted(CONTEXT_KEYS[side])}, with layer and segments"]
    layer = raw["layer"]
    if not isinstance(layer, str) or not 0 < len(layer) <= MAX_CONTEXT_CHARS:
        return None, [f"layer is the layer text {side} the passage, 1 to {MAX_CONTEXT_CHARS} characters"]
    found = []
    for flag in ("trimmed", "lead_in"):
        if flag in raw and raw[flag] is not True:
            found.append(f"{flag}, when present, is true")
    quote = row.get("quote") or {}
    affix = quote.get("prefix" if side == "before" else "suffix")
    # The quote is the passage's head: its prefix meets the passage's start, and its suffix
    # meets the passage's end only when the quote is the whole passage.
    whole = isinstance(quote.get("exact"), str) and sha256_hex(quote["exact"].encode("utf-8")) == row["span_sha256"]
    if isinstance(affix, str) and affix and (side == "before" or whole):
        near = affix[-len(layer):] if side == "before" else affix[:len(layer)]
        if not (layer.endswith(near) if side == "before" else layer.startswith(near)):
            found.append(f"the record's quote {'prefix' if side == 'before' else 'suffix'} must be where this "
                         f"context meets the passage")
    segments, messages = text_segments(raw["segments"], layer)
    found += messages
    if found:
        return None, found
    view = {"layer": layer, "segments": segments}
    view.update({flag: True for flag in ("trimmed", "lead_in") if raw.get(flag) is True})
    return view, []


def attach_passage_text(doc: dict, sets: list[BuiltSet], problems: Problems) -> tuple[int, int]:
    """Give each passage the side file has a verified entry for its full text; the others keep the quote.
    Returns how many passages got their text, and how many of those have a status (the pages show only
    a passage with one, so the dashboard counts the text it shows from these)."""
    entries = doc.get("passages")
    if not isinstance(entries, dict):
        problems.add("--passage-text", "passages must be an object of entries keyed by passage_key")
        return 0, 0
    attached = shown = 0
    for built in sets:
        for view, records in built.articles:
            rows = {row["id"]: row for row in record_rows(records)}
            for passage in view["passages"]:
                row = rows[passage["id"]]
                key = passage_key(row["pageid"], row["span_sha256"])
                if key not in entries:
                    continue
                text, messages = passage_text_entry(entries[key], passage["id"], row,
                                                    built.reader_layers.get(view["pageid"]))
                for message in messages:
                    problems.add(f"--passage-text: passages[{key}]", message)
                if text is not None:
                    passage["text"] = text
                    attached += 1
                    shown += passage["status"] in STATUSES
    return attached, shown


def signature_entry(entry: Any, loc: dict) -> tuple[dict | None, list[str]]:
    """One --signatures entry as the location's `signature`, checked against the card."""
    if not isinstance(entry, dict):
        return None, ["an entry is an object"]
    found = []
    sig, kind, elided = entry.get("signature"), entry.get("kind"), entry.get("elided")
    if not member(kind, LEAN_KEYWORDS):
        found.append(f"kind must be one of {sorted(LEAN_KEYWORDS)}")
    if not isinstance(sig, str) or not 0 < len(sig) <= MAX_SIGNATURE_CHARS:
        found.append(f"signature must be a string of at most {MAX_SIGNATURE_CHARS} characters")
    elif isinstance(kind, str):
        head = f"{kind} {loc['declaration']}"
        if not (sig == head or sig.startswith(head + " ") or sig.startswith(head + "\n")):
            found.append("signature must start with its keyword and the card's declaration")
    if type(elided) is not bool:
        found.append("elided must be true or false")
    if entry.get("proof_status") != loc["proof_status"]:
        found.append(f"proof_status {entry.get('proof_status')!r} is not the card's {loc['proof_status']!r}: "
                     "the corpus row describes another declaration")
    full = entry.get("type")
    if elided is True and not (isinstance(full, str) and 0 < len(full) <= MAX_TYPE_CHARS):
        found.append(f"an elided signature keeps the whole type in type (at most {MAX_TYPE_CHARS} characters)")
    deprecated = entry.get("deprecated_to")
    if deprecated is not None and not (isinstance(deprecated, str) and LEAN_NAME_RE.match(deprecated)):
        found.append("deprecated_to, when present, is a declaration name")
    if found:
        return None, found
    view: dict = {"text": sig, "elided": elided}
    if elided:
        view["type"] = full
    if deprecated is not None:
        view["deprecated_to"] = deprecated
    return view, []


def attach_signatures(doc: dict, sets: list[BuiltSet], universe: dict,
                      problems: Problems) -> tuple[dict | None, int, int]:
    """Give each location the side file names (library, commit, declaration) for its signature."""
    source = doc.get("source")
    if not isinstance(source, dict) or source.get("universe_sha256") != universe["sha256"]:
        problems.add("--signatures", f"source.universe_sha256 must be the store's universe {universe['sha256']}: "
                                     "a signature is the type at the commit a card links")
        return None, 0, 0
    identity = {"corpus_sha256": source.get("corpus_sha256"), "corpus_built": source.get("corpus_built"),
                "universe": source.get("universe")}
    if not HEX64.match(str(identity["corpus_sha256"])) or not CREATED_RE.match(str(identity["corpus_built"])) \
            or identity["universe"] != universe["id"]:
        problems.add("--signatures", "source names its corpus: corpus_sha256 (64 hex), corpus_built (an ISO 8601 "
                                     "UTC time) and universe (the store's universe id)")
        return None, 0, 0
    table = doc.get("signatures")
    if not isinstance(table, dict):
        problems.add("--signatures", "signatures must be an object: library -> commit -> declaration -> entry")
        return None, 0, 0
    count, declarations = 0, set()
    for built in sets:
        for view, _ in built.articles:
            for passage in view["passages"]:
                for loc in passage["locations"]:
                    commits = table.get(loc["library"])
                    names = commits.get(loc["commit"]) if isinstance(commits, dict) else None
                    if not isinstance(names, dict) or loc["declaration"] not in names:
                        continue
                    signature, messages = signature_entry(names[loc["declaration"]], loc)
                    where = f"--signatures: signatures[{loc['library']}][{loc['commit']}][{loc['declaration']}]"
                    for message in messages:
                        problems.add(where, message)
                    if signature is not None:
                        loc["signature"] = signature
                        count += 1
                        declarations.add((loc["library"], loc["commit"], loc["declaration"]))
    return identity, count, len(declarations)


def _dig(value: Any, *keys: str) -> Any:
    """value[k1][k2]..., or None as soon as a level is not an object."""
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _role(roles: Any, name: str) -> tuple[Any, Any]:
    return _dig(roles, name, "model"), _dig(roles, name, "model_name")


def run_view(runs: Any, run_id: str, role: str, seal: dict, universe: dict, problems: Problems) -> dict | None:
    """One run of --provenance as the page cites it: its models, operator and dates, checked.

    What the side file says of the run's model and operator is its maker's reading of the run's
    run.yaml and receipt: checked here for consistency (each role's model, the models the run's own
    calls name) and against the seal where the seal can say (the skill commit, the universe, and a
    harvest no later than the export), but the seal itself records neither."""
    run = runs.get(run_id) if isinstance(runs, dict) else None
    where = f"--provenance: runs[{run_id}]"
    named = ("the locate run names it as its inventory run" if role == "inventory"
             else f"the campaign names it as the {role} run")
    if not isinstance(run, dict):
        problems.add(where, "is missing: the side file describes every run the record set's campaign names, and "
                            "the inventory run its locate run names")
        return None
    found = []
    if run.get("run_id") != run_id:
        found.append(f"run_id must be {run_id!r}")
    if run.get("mode") != RUN_MODES[role]:
        found.append(f"mode must be {RUN_MODES[role]!r}: {named}")
    model, name, operator = run.get("model"), run.get("model_name"), run.get("operator")
    worker, reviewer = _role(run.get("roles"), "worker"), _role(run.get("roles"), "reviewer")
    if not (isinstance(model, str) and TOKEN_RE.match(model)) or not (isinstance(name, str) and DISPLAY_NAME_RE.match(name)):
        found.append(f"model and model_name must be a model id ({TOKEN_RULE}) and its printable name")
    if worker != (model, name):
        found.append("roles.worker must be the run's model and model_name")
    if not (isinstance(reviewer[0], str) and TOKEN_RE.match(reviewer[0])
            and isinstance(reviewer[1], str) and DISPLAY_NAME_RE.match(reviewer[1])):
        found.append("roles.reviewer names its model and model_name")
    observed = _dig(run, "model_evidence", "observed", "messages_by_model")
    configured = {name for name in (model, reviewer[0]) if isinstance(name, str)}
    if not isinstance(observed, dict) or not observed or not set(observed) <= configured:
        found.append("model_evidence.observed.messages_by_model must name only the models the run's roles were "
                     "configured with: a model the run's own calls do not bear out is never cited")
    if not (isinstance(operator, str) and TOKEN_RE.match(operator)):
        found.append(f"operator must be the login that ran it ({TOKEN_RULE})")
    start, end = _dig(run, "window_utc", "start"), _dig(run, "window_utc", "end")
    began, harvested, exported = utc_time(start), utc_time(end), utc_time(seal.get("created"))
    if not (began is not None and harvested is not None and began <= harvested
            and _dig(run, "times", "harvested_at") == end):
        found.append("window_utc is {start, end}: ISO 8601 UTC times, start first, end the harvest (times.harvested_at)")
    elif exported is None or harvested > exported:
        found.append("window_utc.end, the harvest, must be no later than the seal's created: a record set holds what "
                     "its runs had harvested when it was exported")
    if _dig(run, "skill", "commit") != seal["skill_commit"]:
        found.append("skill.commit must be the seal's skill_commit")
    if _dig(run, "universe", "sha256") != universe["sha256"]:
        found.append("universe.sha256 must be the store's universe")
    sampling = None
    if role == "challenge":
        raw = run.get("sampling")
        sampling = {key: raw.get(key) for key in SAMPLING_KEYS if key in raw} if isinstance(raw, dict) else {}
        if type(sampling.get("seed")) is not int or any(
                not (type(value) in (int, float) and 0 <= value <= 1) for key, value in sampling.items() if key != "seed"):
            found.append("sampling is {seed: <int>, sample_rate, strong_sample_rate, absence_sample_rate: 0 to 1}")
    for message in found:
        problems.add(where, message)
    if found:
        return None
    view = {"run_id": run_id, "mode": run["mode"], "model": model, "model_name": name,
            "reviewer_model": reviewer[0], "reviewer_model_name": reviewer[1], "operator": operator,
            "started": start, "harvested": end, "skill_commit": seal["skill_commit"]}
    if sampling is not None:
        view["sampling"] = sampling
    return view


def record_provenance_problems(described: Any, rows: list[dict], locate: str, challenge: str | None) -> list[str]:
    """Whether the side file's per-line entries describe exactly these records."""
    if not isinstance(described, list) or len(described) != len(rows):
        return [f"lists {len(described) if isinstance(described, list) else 'no'} records, not the "
                f"{len(rows)} lines of the records file"]
    found = []
    for line, (entry, row) in enumerate(zip(described, rows)):
        # A record a challenge run judged names that run; one it sampled out may name it apart.
        rechecked = row["challenge"] in ("survived", "downgraded")
        expected = {"line": line, "shape": row["shape"], "locate_run": locate, "verdict": row["challenge"],
                    "rechecked": rechecked, "run": challenge if rechecked else None,
                    "sampled_out_of": challenge if row["challenge"] == "sampled_out" else None}
        verdict = _dig(entry, "challenge")
        got = {"line": _dig(entry, "line"), "shape": _dig(entry, "shape"), "locate_run": _dig(entry, "locate_run"),
               **{key: _dig(verdict, key) for key in ("verdict", "rechecked", "run")},
               "sampled_out_of": _dig(verdict, "sampled_out_of") if isinstance(verdict, dict)
               and "sampled_out_of" in verdict else expected["sampled_out_of"]}
        keys = [key for key in ("passage_key", "location_key", "pair_key") if row[key] is not None]
        if got != expected or any(_dig(entry, key) != row[key] for key in keys):
            found.append(f"line {line} does not describe the record on that line (its line, shape, keys, locate "
                         "run and challenge verdict and run)")
    return found


def attach_provenance(doc: dict, sets: list[BuiltSet], universe: dict, problems: Problems) -> list[dict]:
    """Give each article the runs that made its records; returns every run cited, for current.json."""
    described_sets = doc.get("record_sets") if isinstance(doc.get("record_sets"), dict) else {}
    described_records = doc.get("records") if isinstance(doc.get("records"), dict) else {}
    cited: dict[str, dict] = {}
    for built in sets:
        seal = built.seal
        entry = described_sets.get(seal["set_id"])
        where = f"--provenance: record_sets[{seal['set_id']}]"
        if not isinstance(entry, dict):
            problems.add(where, "is missing: the side file describes every record set of the build")
            continue
        runs = re.match(r"^run:([^+]+)(?:\+(.+))?\Z", seal["campaign"])
        locate, challenge = entry.get("locate_run"), entry.get("challenge_run")
        if entry.get("seal_sha256") != built.seal_sha or entry.get("campaign") != seal["campaign"]:
            problems.add(where, "seal_sha256 and campaign must be the record set's own")
            continue
        if runs and (locate, challenge) != (runs.group(1), runs.group(2)):
            problems.add(where, "locate_run and challenge_run must be the runs the campaign token names")
            continue
        if not isinstance(locate, str) or not (challenge is None or isinstance(challenge, str)):
            problems.add(where, "locate_run names a run, and challenge_run a run or null")
            continue
        locate_view = run_view(doc.get("runs"), locate, "locate", seal, universe, problems)
        challenge_view = run_view(doc.get("runs"), challenge, "challenge", seal, universe, problems) if challenge else None
        # The inventory run that chose the passages the locate run searched, when its run.yaml names one.
        inventory = _dig(doc.get("runs"), locate, "inventory_run")
        if inventory is not None and not (isinstance(inventory, str) and RUN_ID_RE.match(inventory)):
            problems.add(f"--provenance: runs[{locate}]", "inventory_run names the inventory run whose passages it "
                                                          "searched, or is null")
            continue
        inventory_view = run_view(doc.get("runs"), inventory, "inventory", seal, universe, problems) if inventory else None
        if challenge and challenge_view is not None:
            challenged = doc["runs"][challenge].get("challenged_runs")
            if not isinstance(challenged, list) or locate not in challenged:
                problems.add(f"--provenance: runs[{challenge}]", f"challenged_runs must name {locate}")
                continue
        if locate_view is None or (challenge and challenge_view is None) or (inventory and inventory_view is None):
            continue
        # The runs follow one another: the inventory's harvest before the locate run starts, and the
        # challenge after the locate run it re-checked was harvested.
        if inventory_view is not None and utc_time(inventory_view["harvested"]) > utc_time(locate_view["started"]):
            problems.add(f"--provenance: runs[{inventory}]", f"window_utc.end must be no later than the start of "
                                                             f"{locate}, the locate run that searched its passages")
            continue
        if challenge_view is not None and utc_time(challenge_view["started"]) < utc_time(locate_view["harvested"]):
            problems.add(f"--provenance: runs[{challenge}]", f"window_utc.start must be no earlier than the harvest of "
                                                             f"{locate}, the locate run it re-checked")
            continue
        for view, records in built.articles:
            for message in record_provenance_problems(described_records.get(str(view["pageid"])), record_rows(records),
                                                      locate, challenge):
                problems.add(f"--provenance: records[{view['pageid']}]", message)
            view["provenance"] = {"locate": locate_view, "challenge": challenge_view}
            if inventory_view is not None:
                view["provenance"]["inventory"] = inventory_view
        for run in (inventory_view, locate_view, challenge_view):
            if run is not None:
                cited[run["run_id"]] = run
    return [cited[run_id] for run_id in sorted(cited)]


def recheck_tallies(sets: list[BuiltSet]) -> dict[str, dict]:
    """What each challenge run re-checked across the build, from the articles it challenged:
    [re-checked, of] for the Lean locations (related declarations aside) and the absences of
    central (subject or central) passages and of supporting ones. The challenge samples the
    supporting passages, so its rates alone do not say how much of the build it re-checked."""
    out: dict[str, dict] = {}
    for built in sets:
        for view, _ in built.articles:
            run = (view.get("provenance") or {}).get("challenge")
            if not run:
                continue
            t = out.setdefault(run["run_id"], {"central": {"locations": [0, 0], "absences": [0, 0]},
                                               "supporting": {"locations": [0, 0], "absences": [0, 0]}})
            for passage in view["passages"]:
                group = t["central" if passage.get("prominence") in ("subject", "central") else "supporting"]
                for loc in passage["locations"]:
                    if loc["relation"] == "relatedMatch":
                        continue
                    group["locations"][1] += 1
                    group["locations"][0] += loc["challenge"] in ("survived", "downgraded")
                absence = passage.get("absence")
                if absence:
                    group["absences"][1] += 1
                    group["absences"][0] += absence["challenge"] in ("survived", "downgraded")
    return out


def audit_share(value: Any) -> float | None:
    """A share: a finite number from 0 to 1, never a bool; None when it is not one."""
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1 else None


def audit_figure_problems(n: int, figure: dict, items: int | None) -> list[str]:
    """What is wrong with one --audit-summary figure (AUDIT_FIGURE_KEYS), as messages."""
    found = []
    if not (isinstance(figure["id"], str) and TOKEN_RE.match(figure["id"])):
        found.append(f"figure {n}: id must be {TOKEN_RULE}")
    if not (isinstance(figure["label"], str) and AUDIT_LABEL_RE.match(figure["label"])):
        found.append(f"figure {n}: label must be plain text, letters, digits, spaces and ,.()·/+:;'&%_- "
                     "(at most 160 characters)")
    share, ci = audit_share(figure["share"]), figure["ci95"]
    low, high = (audit_share(ci[0]), audit_share(ci[1])) if isinstance(ci, list) and len(ci) == 2 else (None, None)
    if share is None or low is None or high is None or not low <= share <= high:
        found.append(f"figure {n}: share is the share judged right, from 0 to 1, and ci95 its 95% interval "
                     "[low, high], low <= share <= high")
    drawn, population, weights, n_eff = figure["drawn"], figure["population"], figure["weights"], figure["n_eff"]
    if not (type(drawn) is int and drawn >= 1 and (items is None or drawn <= items)
            and type(population) is int and population >= drawn):
        found.append(f"figure {n}: drawn is how many of the sample's items it rests on (1 to items), and population "
                     "how many the share is of (at least drawn)")
    elif not (weights is None or (isinstance(weights, str) and AUDIT_LABEL_RE.match(weights))) \
            or not (type(n_eff) in (int, float) and math.isfinite(n_eff) and 0 < n_eff <= drawn
                    and (weights is not None or n_eff == drawn)):
        found.append(f"figure {n}: weights says what the items were weighed by, plain text, or is null for none, and "
                     "n_eff is the Kish effective sample size, above 0 and at most drawn (drawn itself without weights)")
    return found


def audit_view(doc: dict, sets: list[BuiltSet], problems: Problems) -> dict | None:
    """--audit-summary as the pages cite it: the run that audited the build's links and its figures, checked.

    The run is the maker's reading of the audit's own records, as a run's provenance is; it is checked
    for its shape and against the seals, which an audit must post-date: it judges the sets as exported."""
    where = "--audit-summary"
    found = [f"unknown key {key!r}" for key in sorted(set(doc) - set(AUDIT_SUMMARY_KEYS))]
    want = sorted(built.seal_sha for built in sets)
    shas = doc.get("record_sets")
    if not (isinstance(shas, list) and all(isinstance(sha, str) for sha in shas) and sorted(shas) == want):
        found.append(f"record_sets must list the seal sha256 of each record set of this build, each once "
                     f"({', '.join(want)}): an audit's figures describe the sets it read")
    exported = [utc_time(built.seal.get("created")) for built in sets]
    run = doc.get("audit")
    models: list[dict] = []
    if not isinstance(run, dict) or set(run) != set(AUDIT_RUN_KEYS):
        found.append(f"audit is the run that judged the sample: exactly {list(AUDIT_RUN_KEYS)}")
        run = {}
    else:
        if not (isinstance(run["run_id"], str) and RUN_ID_RE.match(run["run_id"])):
            found.append("audit.run_id must be a run id (letters, digits and ._-, at most 64)")
        if not (isinstance(run["operator"], str) and TOKEN_RE.match(run["operator"])):
            found.append(f"audit.operator must be the login that ran it ({TOKEN_RULE})")
        began, ended = utc_time(_dig(run, "window_utc", "start")), utc_time(_dig(run, "window_utc", "end"))
        if not (isinstance(run["window_utc"], dict) and set(run["window_utc"]) == {"start", "end"}
                and began is not None and ended is not None and began <= ended):
            found.append("audit.window_utc is {start, end}: ISO 8601 UTC times, start first")
        elif None in exported or began < max(exported):
            found.append("audit.window_utc.start must be no earlier than the created time of the latest seal it read: "
                         "an audit judges the record sets as they were exported")
        if type(run["blind"]) is not bool or type(run["independent"]) is not bool:
            found.append("audit.blind and audit.independent are true or false: whether its judges were blind to the "
                         "runs and to each other, and whether they were independent of the runs they judged")
        raw = run["models"]
        if not (isinstance(raw, list) and 1 <= len(raw) <= MAX_AUDIT_MODELS
                and all(isinstance(m, dict) and set(m) == {"model", "model_name"}
                        and isinstance(m["model"], str) and TOKEN_RE.match(m["model"])
                        and isinstance(m["model_name"], str) and DISPLAY_NAME_RE.match(m["model_name"]) for m in raw)
                and len({m["model"] for m in raw}) == len(raw)):
            found.append(f"audit.models lists 1 to {MAX_AUDIT_MODELS} models that judged, each {{model, model_name}}: "
                         "a model id and its printable name, each model once")
        else:
            models = [{"model": m["model"], "model_name": m["model_name"]} for m in raw]
    items = doc.get("items")
    if type(items) is not int or items < 1:
        found.append("items must be the number of items the audit judged, at least 1")
        items = None
    figures = doc.get("figures")
    out_figures: list[dict] = []
    if not isinstance(figures, list) or not 1 <= len(figures) <= MAX_AUDIT_FIGURES:
        found.append(f"figures must list 1 to {MAX_AUDIT_FIGURES} figures")
    else:
        for n, figure in enumerate(figures):
            if not isinstance(figure, dict) or set(figure) != set(AUDIT_FIGURE_KEYS):
                found.append(f"figure {n} is exactly {list(AUDIT_FIGURE_KEYS)}")
                continue
            found += audit_figure_problems(n, figure, items)
            out_figures.append({key: figure[key] for key in AUDIT_FIGURE_KEYS})
        if len({f["id"] for f in out_figures if isinstance(f["id"], str)}) != len(out_figures):
            found.append("each figure's id is its own")
    for message in found:
        problems.add(where, message)
    if found:
        return None
    return {"run_id": run["run_id"], "operator": run["operator"], "started": run["window_utc"]["start"],
            "ended": run["window_utc"]["end"], "models": models, "blind": run["blind"],
            "independent": run["independent"], "items": items, "figures": out_figures}


# ---- the reader: each article's pinned revision with its passages marked (--reader-html) -----
#
# A record set carries a passage as a span on its revision's text layer, never the article. The
# side file holds, per article, the revision itself (the sourceset's body.html, whose sha256 every
# record pins as html_sha256), sanitised for serving, with each passage's text wrapped in
#     <mark class="atl" data-p="<item hex>" data-status="<status>">
# split at element boundaries, a formula or a citation marker wrapped whole, and the passage's
# first mark carrying id="p-<item hex>" (the card list's anchor). The builder takes none of it on
# trust. It reads the HTML with the standard library's html.parser and
#   - refuses any element outside READER_TAGS, any attribute outside READER_ATTRS (data-* and
#     aria-* aside; event handlers and the other active attributes never), any URL a page would
#     fetch that is not https on Wikimedia's upload hosts, any link that is not an in-page
#     fragment, an atlas article (one slug segment, no dot segment) or an http(s) page on another
#     host than the site's (reader_link_problem), the page's own ids and classes, and any
#     TemplateStyles rule not scoped to .mw-parser-output (reader_css_problems, on the CSS as a
#     browser tokenizes it, so no escape hides a keyword);
#   - refuses markup a browser's tree builder would read differently from html.parser
#     (ReaderScan._tree): a style element or an HTML element inside MathML, an element closed out
#     of order or left open, a block in a paragraph, a table part out of place, and the like;
#   - rebuilds the revision's text layer from it the way web-html/3 does (the decoded text in
#     document order, nothing from style, one LF dropped after pre and listing, each White_Space
#     run one space, the ends stripped): it must hash to the records' layer_sha256, so every
#     character served is the pinned revision's;
#   - places every mark on that layer: a passage's marks, in document order, cover exactly its
#     span (the layer slice whose sha256 is its record's span_sha256) with at most a space between
#     two of them; each carries its passage's status; none nests in another, crosses an element
#     boundary or shares a character with another passage's; a passage with no status has none
#     (no highlight until it is read);
#   - shows no file that is not free: every file it fetches (src, srcset, poster, a TemplateStyles
#     url()) is a Wikimedia Commons file or an English Wikipedia upload, never a Wikimedia or
#     Foundation mark (by its name, or its file page's trademarked restriction), and is named in
#     the entry's `images` with its licence and restrictions; an English Wikipedia upload only
#     with a free licence and categories that make it free everywhere (reader_images).
# A passage with no mark is unplaced (the side file lists the same ones, with its reason). The
# view gains reader {sha256, bytes, marks, passages, unplaced[, images]}, and the store the file
# reader/<pageid>.html, the side file's html byte for byte.

READER_WS = frozenset(chr(code) for code in (
    *range(0x09, 0x0E), 0x20, 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F, 0x205F, 0x3000))
READER_WS_RUN = re.compile("[" + "".join(re.escape(ch) for ch in sorted(READER_WS)) + "]+")
# ASCII whitespace, the only space HTML's attribute parsers (a srcset's candidates) and CSS's tokenizer
# (a url()) skip. A browser keeps any other space character (U+00A0, U+3000 and the like) inside a
# URL, which then resolves against the page, where Python's str.strip() and a Unicode \s would drop
# it and check another URL. So URLs are split at these alone, and a URL attribute holding another
# space character is refused (READER_URL_ATTRS).
READER_ASCII_WS = "\t\n\f\r "
READER_ASCII_WS_RUN = re.compile(r"[\t\n\f\r ]+")
READER_URL_ATTRS = frozenset({"href", "cite", "src", "srcset", "poster"})
MAX_READER_HTML_BYTES = 4 * 1024 * 1024
READER_TAGS = frozenset((
    "a abbr audio b bdi bdo big blockquote br caption cite code col colgroup dd del dfn div dl dt em figcaption "
    "figure h2 h3 h4 h5 h6 hr i img ins kbd li mark ol p pre q s samp small source span strong style sub sup "
    "table tbody td tfoot th thead time tr track u ul var video wbr "
    # MathML (every formula keeps its MathML and its TeX annotation)
    "math semantics annotation mrow mi mn mo ms mtext mspace mfrac msqrt mroot msub msup msubsup munder mover "
    "munderover mtable mtr mtd mstyle mpadded mphantom menclose merror mmultiscripts mprescripts none").split())
READER_MATHML = frozenset(("math semantics annotation mrow mi mn mo ms mtext mspace mfrac msqrt mroot msub msup "
                           "msubsup munder mover munderover mtable mtr mtd mstyle mpadded mphantom menclose merror "
                           "mmultiscripts mprescripts none").split())
READER_VOID = frozenset({"br", "col", "hr", "img", "source", "track", "wbr"})
READER_EAT_LF = frozenset({"pre", "listing"})
READER_ACTIVE_ATTRS = frozenset({"srcdoc", "formaction", "action", "background", "ping", "autofocus", "http-equiv",
                                 "is", "nonce", "manifest", "contenteditable", "tabindex", "accesskey", "popover",
                                 "autoplay", "download"})
# The attributes a reader body may carry, by name: what Wikipedia's parse HTML and its MathML use on
# READER_TAGS (every one batch 1's 48 bodies hold, and their like), each inert or value-checked in
# ReaderScan._attribute, plus data-* and aria-* (READER_ATTR_FAMILY). Anything else is refused, so an
# attribute a browser gives an effect (attributionsrc sends a request to the hosts it names,
# browsingtopics and sharedstoragewritable hand the image host browser state) is refused until it
# is reviewed and listed here.
READER_ATTRS = frozenset((
    # HTML: global, links, images and media, tables, lists, edits and dates
    "class id lang dir title role style translate typeof about property resource rel "
    "href target hreflang type cite alt src srcset sizes width height decoding loading referrerpolicy "
    "poster controls preload muted loop srclang kind label "
    "colspan rowspan scope headers abbr axis span align valign char charoff bgcolor border cellpadding "
    "cellspacing frame rules summary nowrap clear start reversed value datetime "
    # MathML (presentation markup and its annotations)
    "xmlns display alttext encoding mathvariant mathcolor mathbackground mathsize displaystyle scriptlevel "
    "scriptminsize scriptsizemultiplier accent accentunder fence separator stretchy symmetric largeop "
    "movablelimits form lspace rspace minsize maxsize linethickness numalign denomalign bevelled "
    "columnalign columnlines columnspacing columnspan rowalign rowlines rowspacing framespacing equalrows "
    "equalcolumns side minlabelspacing depth voffset notation subscriptshift superscriptshift").split())
READER_ATTR_FAMILY = re.compile(r"^(?:data-[a-z0-9][a-z0-9._-]*|aria-[a-z]+)\Z")
READER_FETCH = ("https://upload.wikimedia.org/", "https://thumb.wikimedia.org/")
# What a reader body may show: a file of Wikimedia Commons or an English Wikipedia upload, by its
# upload URL (the original, a thumbnail or a transcode, with any query). Commons hosts only files
# that are free in the United States and in their source country; an English Wikipedia upload may
# be non-free (fair use) or free in the United States only, so one is shown only when the entry's
# `images` names it with a free licence and with categories that make it free everywhere
# (READER_IMAGE_KEYS), as its maker took them from the MediaWiki API. Every file the body fetches is
# named in `images`: its project, its licence (by name, with a link to the licence where it has one)
# and its author, which the page lists for attribution. The Wikimedia projects' marks and the
# Foundation's (their logos, wordmarks and icons, the Wikipedia W) are refused whatever their host and
# however they are listed: the Foundation's trademark policy covers them whatever their copyright licence.
READER_MEDIA_URL = re.compile(r"^https://(?:upload|thumb)\.wikimedia\.org/wikipedia/(commons|en)/"
                              r"(?:(?:thumb|transcoded)/)?[0-9a-f]/[0-9a-f]{2}/([^/?#\s]+)(?:/[^/?#\s]+)?"
                              r"(?:\?[^#\s]*)?\Z")
# A file is such a mark by its name when the name starts with a project's or the Foundation's name as
# a word (Wikipedia-W-bold-in-square.svg, Wikipedia_wordmark.svg, Wiktionary_small.svg,
# MediaWiki-2020-icon.svg, Wiki_letter_w.svg, Wikimedia_Foundation_logo.svg: "Wiki" alone counts, a
# longer word such as "Wikipedian" does not), or when it names a project or the Foundation and a logo,
# wordmark or icon in either order (Commons-logo.svg, Notification-icon-Wikipedia-logo.svg,
# WMF_logo.svg). Whatever its name, a file whose page marks it trademarked is refused too
# (READER_BARRED_RESTRICTIONS), so a mark under another name is caught by its file page.
READER_MARK_LEAD = ("Wiki|Wikimedia|Wikipedia|Wiktionary|Wikibooks|Wikiquote|Wikisource|Wikiversity|Wikivoyage|"
                    "Wikinews|Wikidata|Wikispecies|Wikifunctions|Wikimania|Wikitech|MediaWiki")
READER_MARK_PROJECT = READER_MARK_LEAD + "|Commons|Meta|Meta-Wiki|Incubator|WMF"
READER_WIKIMEDIA_MARK = re.compile(
    rf"^(?i:{READER_MARK_LEAD})(?![a-z])"
    rf"|^(?=.*(?<![A-Za-z])(?i:{READER_MARK_PROJECT})(?![a-z]))"
    r"(?=.*(?:(?<![A-Za-z])(?i:logo|wordmark|icon)|Logo|Wordmark|Icon)(?![a-z]))")
# The free licences, as the API's License field names them (lower case): public domain and CC0,
# Creative Commons BY and BY-SA of any version and port, the GFDL, the GPL and LGPL, the Free Art
# Licence and the like. NonCommercial and NoDerivatives licences are not free, nor is none.
READER_FREE_LICENCE = re.compile(r"^(?:pd(?:-[a-z0-9.-]+)?|cc0(?:-1\.0)?|cc-by(?:-sa)?(?:-[0-9.]+(?:-[a-z]{2,3})?)?|"
                                 r"gfdl(?:-[a-z0-9.-]+)?|l?gpl(?:-[a-z0-9.+-]+)?|fal|ogl(?:-[a-z0-9.-]+)?|attribution)\Z")
READER_IMAGE_PROJECTS = ("commons", "en")
# What `images` gives each file, by its project: its restrictions, and an English Wikipedia upload also
# its categories, as the API lists them. A free licence id alone does not make an upload free
# everywhere: one free in the United States only (freedom of panorama, a work out of copyright there
# alone) keeps its free licence id, and English Wikipedia files it under these categories, so an upload
# is shown only when its categories include READER_FREE_CATEGORY and none of READER_NOT_FREE_CATEGORIES.
READER_IMAGE_KEYS = {"commons": frozenset({"project", "licence", "licence_id", "licence_url", "author",
                                           "restrictions"}),
                     "en": frozenset({"project", "licence", "licence_id", "licence_url", "author", "restrictions",
                                      "categories"})}
# A Commons file is free by its licence id (READER_FREE_LICENCE) or, when the API gives it none, by its
# licence's name alone, as Commons names a few free licences (the GPL and LGPL, the GFDL, the
# "Attribution" licence, copyrighted free use, the public domain). Any other name, or no licence at
# all, is refused: Commons hosting a file is not a licence, and a file is shown only with one.
READER_FREE_LICENCE_NAMES = frozenset({"public domain", "pd", "cc0", "gpl", "lgpl", "gfdl", "attribution",
                                       "copyrighted free use", "free art license", "fal"})
# The licences that ask for the author's name with every copy (Creative Commons BY and BY-SA of every
# version, the GFDL, the GPL and LGPL, the Free Art Licence, the Open Government Licence, the
# "Attribution" licence), by id or, without one, by name: every free licence READER_FREE_LICENCE names
# but the public domain and CC0. A file under one is shown only with its author. Those with a licence
# text of their own (Creative Commons, the GFDL, the GPL and LGPL) are shown only with a link to it, as
# their terms ask.
READER_ATTRIBUTION_LICENCE = re.compile(r"^(?:cc-by(?:-sa)?(?:-[0-9.]+(?:-[a-z]{2,3})?)?|gfdl(?:-[a-z0-9.-]+)?|"
                                        r"l?gpl(?:-[a-z0-9.+-]+)?|fal|ogl(?:-[a-z0-9.-]+)?|attribution)\Z")
READER_LICENCE_TEXT = re.compile(r"^(?:cc-by|gfdl|l?gpl)")
# A file's author as its file page credits them (the API's Artist with its markup stripped): plain text.
READER_AUTHOR_RE = re.compile(r"^(?=\S)[^\x00-\x1f\x7f<>]{1,300}(?<=\S)\Z")
# The licence's own page (the API's LicenseUrl), https on the hosts that publish these licences.
READER_LICENCE_URL = re.compile(r"^https://([A-Za-z0-9.-]+)/[A-Za-z0-9._~%/+-]*\Z")
READER_LICENCE_HOSTS = frozenset({"creativecommons.org", "www.gnu.org", "gnu.org", "artlibre.org"})
# A file page's restrictions beside its licence, as the API's extmetadata Restrictions field lists them
# (split at "|"; [] for none): "trademarked", "personality", "insignia" and the like. A trademarked file
# is a mark, the Foundation's or another's, and is refused whatever its name or licence; the others
# restrict a use, not the copy, and leave a file free.
READER_BARRED_RESTRICTIONS = frozenset({"trademarked"})
READER_RESTRICTION_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}\Z")
MAX_READER_RESTRICTIONS = 16
READER_FREE_CATEGORY = "Category:All free media"
READER_NOT_FREE_CATEGORIES = frozenset({
    "Category:All free in US media", "Category:Wikipedia files not suitable for Commons",
    "Category:Wikipedia images using freedom of panorama", "Category:All non-free media"})
READER_CATEGORY_RE = re.compile(r"^Category:[^\x00-\x1f\x7f<>\[\]{}|]{1,250}\Z")
MAX_READER_CATEGORIES = 200
READER_FILE_NAME = re.compile(r"^[^\x00-\x1f\x7f/#?<>\[\]{}|\s]{1,240}\Z")
# A link (href or cite) is an in-page fragment, an atlas article or an http(s) page on another host.
# An atlas article is /atlas/<pageid> or /atlas/<pageid>/<slug>, the slug one path segment as atlasSlug
# (atlas.ts) writes it: the characters encodeURIComponent leaves and %XX escapes, never a dot segment
# (".", "..", or either spelled with %2e), which a browser resolves away, so /atlas/1/../../<path> and
# its %2e%2e form reach another path of the site. A fragment may follow.
READER_ATLAS_LINK = re.compile(r"^/atlas/[1-9][0-9]{0,9}(?:/((?:[A-Za-z0-9_.!~*'()-]|%[0-9A-Fa-f]{2})+))?"
                               r"(?:#[^\s\"'<>\\]*)?\Z")
# An http(s) link's host is plain ASCII letters, digits, dots and hyphens, with no user, escape or
# backslash, so the host checked is the host a browser goes to (it reads https://<this site>\@x/ as
# this site and percent-decodes a host), and the URL holds no whitespace or backslash.
READER_WEB_LINK = re.compile(r"^https?://([A-Za-z0-9.-]+)(?::[0-9]{1,5})?(?:[/?#][^\s\\]*)?\Z")
# The site that serves the reader: an absolute link to it reaches every other path of the site, so the
# reader links into the site only through atlas articles.
READER_SITE_HOSTS = frozenset({"wikilean.jackmccarthy.org"})
READER_LINK_RULE = "a {name} must be an in-page fragment, an http(s) page or an atlas article, on a link"
# CSS is matched on its text with the comments out and every string a placeholder (reader_css_text),
# so a pattern sees the keywords and functions a browser sees: an escape outside a string, which can
# spell any of them (u\72l(, @\69mport), is refused there. These fetch, run or escape the article:
# @import and the functions that take a URL as a string (image-set, image, src); url() is checked by
# its value (reader_css_urls).
READER_CSS_ACTIVE = (r"@import|expression\s*\(|javascript:|behavior\s*:|-moz-binding|image-set\s*\(|"
                     r"(?<![\w-])(?:image|src|attr|element|cross-fade)\s*\(")
READER_STYLE_ATTR = re.compile(READER_CSS_ACTIVE + r"|(?<![\w-])url\s*\(|position\s*:\s*(?:fixed|sticky)|[{}]", re.I)
READER_CSS_BANNED = re.compile(READER_CSS_ACTIVE + r"|position\s*:\s*fixed", re.I)
# A url() as CSS tokenizes it: ASCII letters in any case, and only CSS's whitespace (READER_ASCII_WS)
# around the URL, so a U+00A0 before it stays in the URL checked, as it stays in the one fetched.
READER_CSS_URL_CALL = re.compile(r"(?<![\w-])url\(", re.I | re.ASCII)
READER_CSS_URL = re.compile(r"(?<![\w-])url\([\t\n\f\r ]*(?:\"#(\d+)\"|([^\t\n\f\r \"'()]*))[\t\n\f\r ]*\)",
                            re.I | re.ASCII)
READER_CSS_HEX = re.compile(r"([0-9a-fA-F]{1,6})(?:\r\n|[ \t\r\n\f])?")
READER_SCOPE = re.compile(r"\.mw-parser-output(?![\w-])")
# Characters a browser's tokenizer changes or drops (NUL becomes U+FFFD), so html.parser and the
# browser would read different text or tag names.
READER_CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")
# What a browser's tree builder does that html.parser does not (HTML's "in body" rules): a start
# tag of these closes an open <p> (READER_CLOSES_P), and these scopes stop that search.
READER_CLOSES_P = frozenset(("address article aside blockquote center details dialog dir div dl fieldset "
                             "figcaption figure footer header hgroup main menu nav ol p pre listing search "
                             "section summary table ul h1 h2 h3 h4 h5 h6 hr xmp").split())
READER_SCOPES = frozenset({"table", "td", "th", "caption", "button", "math", "object", "applet", "marquee",
                           "template", "html"})
READER_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
# A table's own children (anything else, or text other than whitespace, a browser moves out in
# front of the table).
READER_TABLE_KIDS = {"table": frozenset({"caption", "colgroup", "thead", "tbody", "tfoot", "tr", "style"}),
                     "thead": frozenset({"tr", "style"}), "tbody": frozenset({"tr", "style"}),
                     "tfoot": frozenset({"tr", "style"}), "tr": frozenset({"td", "th", "style"}),
                     "colgroup": frozenset({"col"})}
READER_TABLE_PARENTS = {"caption": ("table",), "colgroup": ("table",), "thead": ("table",), "tbody": ("table",),
                        "tfoot": ("table",), "tr": ("table", "thead", "tbody", "tfoot"), "td": ("tr",),
                        "th": ("tr",), "col": ("colgroup",)}
READER_MARK_ATTRS = frozenset({"class", "data-p", "data-status", "id"})
READER_ANCHOR_ID = re.compile(r"^p-[0-9a-f]{8,16}\Z")
# The page's own ids and classes (atlas-render: atlas-*, atlr-*; the site header: wl-*) are never the
# article's: its stylesheets position .atlr-peek and .atlr-fab fixed over the page, make
# .atlas-chip[data-filter] a filter control and .wl-header the sticky header. A class is matched
# per token, split at ASCII whitespace as a browser splits it, and in any case.
READER_CHROME_ID = re.compile(r"^(?:atlas-|atlr-|wl-)")
READER_CHROME_CLASS = re.compile(r"^(?:atlas-|atlr-|wl-)", re.I)
READER_CLASS_SPLIT = re.compile(r"[\t\n\f\r ]+")
ITEM_HEX_RE = re.compile(r"^In-text #([0-9a-f]{8,16})\Z")
READER_ENTRY_KEYS = frozenset({"source_id", "revision", "html_sha256", "layer_sha256", "renderer", "html", "marks",
                               "passages", "unplaced", "images"})


def reader_css_text(css: str) -> tuple[str, list[str], list[str]]:
    """CSS as a browser tokenizes it: (its text, its strings, problems).

    The text has each comment as a space and each string as the placeholder "#<n>", n indexing the
    strings' decoded values, so a pattern on it matches only what is outside comments and strings.
    Outside a string nothing may be escaped: an escape spells any keyword, function or selector
    (u\\72l( is url(, @\\69mport is @import), and no pattern on the text would see it.
    """
    out: list[str] = []
    strings: list[str] = []
    found: list[str] = []
    i, n = 0, len(css)
    while i < n:
        ch = css[i]
        if css.startswith("/*", i):
            end = css.find("*/", i + 2)
            if end < 0:
                found.append("a style comment is never closed")
                break
            out.append(" ")
            i = end + 2
        elif ch in "\"'":
            value: list[str] = []
            j, closed = i + 1, False
            while j < n:
                c = css[j]
                if c == ch:
                    closed = True
                    break
                if c in "\n\r\f":
                    break
                if c == "\\":
                    if j + 1 >= n:
                        break
                    if css[j + 1] in "\n\r\f":           # an escaped newline continues the string
                        j += 3 if css.startswith("\r\n", j + 1) else 2
                        continue
                    hexa = READER_CSS_HEX.match(css, j + 1)
                    if hexa:
                        code = int(hexa.group(1), 16)
                        value.append(chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else "�")
                        j = hexa.end()
                    else:
                        value.append(css[j + 1])
                        j += 2
                    continue
                value.append(c)
                j += 1
            if not closed:
                found.append("a style string is never closed")
                break
            out.append(f'"#{len(strings)}"')
            strings.append("".join(value))
            i = j + 1
        elif ch == "\\":
            found.append("a CSS escape outside a string (an escape can spell any keyword, function or selector)")
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out), strings, found


def reader_css_urls(text: str, strings: list[str], where: str, media: list[str] | None = None) -> list[str]:
    """Every url() of a scanned CSS text must fetch https on Wikimedia's upload hosts; each one that
    does is added to `media`, so the file it names is held to the reader's image rules."""
    found = []
    readable = 0
    for match in READER_CSS_URL.finditer(text):
        readable += 1
        url = strings[int(match.group(1))] if match.group(1) is not None else match.group(2)
        if not url.startswith(READER_FETCH):
            found.append(f"{where} fetches {url[:60]!r}: only https on {', '.join(READER_FETCH)}")
        elif media is not None:
            media.append(url)
    if readable != len(READER_CSS_URL_CALL.findall(text)):
        found.append(f"{where} has a url() that is not one quoted or plain URL")
    return found


def reader_split_selectors(head: str) -> list[str]:
    """A selector list split at its own commas, not those inside :is(), :not() or an attribute."""
    parts, depth, piece = [], 0, []
    for ch in head:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(piece))
            piece = []
        else:
            piece.append(ch)
    parts.append("".join(piece))
    return parts


def reader_selector_scoped(selector: str) -> bool:
    """A selector reaches only .mw-parser-output and what it holds.

    The scope must be a compound of the selector itself, not an argument of :not(), :has(), :is()
    or :where() or the value of an attribute (those match elements outside it: html:has(.mw-parser-
    output) .banner is the banner), and the combinator after its compound is a descendant or child
    one, never a sibling one.
    """
    bare = re.sub(r"\[[^\]]*\]", "", selector)
    while True:
        inner = re.sub(r"\([^()]*\)", "", bare)
        if inner == bare:
            break
        bare = inner
    if "(" in bare or ")" in bare:
        return False
    scope = READER_SCOPE.search(bare)
    if scope is None:
        return False
    rest = bare[scope.end():]
    after = rest[re.match(r"[^\s>+~]*", rest).end():].lstrip()
    return not after.startswith(("+", "~"))


def reader_css_problems(css: str, media: list[str] | None = None) -> list[str]:
    """A TemplateStyles block's problems: what it must not hold, and every rule scoped to .mw-parser-output.
    The URLs its url() calls fetch are added to `media`."""
    text, strings, found = reader_css_text(css)
    if "<" in css:
        found.append("a style element holds '<' (TemplateStyles never do; markup there is a parser trick)")
    if READER_CSS_BANNED.search(text):
        found.append("a style element holds @import, expression(), a javascript: URL, a binding, an image-set(), "
                     "image(), src(), attr() or element(), or a fixed position")
    found += reader_css_urls(text, strings, "a style element", media)
    stack: list[str] = []
    buffer: list[str] = []
    for ch in text:
        if ch == "{":
            head = "".join(buffer).strip()
            buffer = []
            if head.startswith("@"):
                if stack and stack[-1] == "rule":
                    found.append(f"the at-rule {head[:40]!r} is nested in a rule")
                elif not re.match(r"@(?:media|supports)(?![\w-])", head):
                    found.append(f"the at-rule {head[:40]!r} is not allowed (only @media and @supports)")
                stack.append("at")
                continue
            if stack and stack[-1] == "rule":
                # CSS nesting: `& + footer` inside a scoped rule is a sibling of the article.
                found.append(f"the rule {head[:40]!r} is nested in another rule (CSS nesting is not allowed)")
            else:
                for selector in reader_split_selectors(head):
                    if not reader_selector_scoped(selector):
                        found.append(f"the selector {selector.strip()[:60]!r} is not scoped to .mw-parser-output")
            stack.append("rule")
        elif ch == "}":
            buffer = []
            if not stack:
                found.append("a style element closes a block it never opened")
            else:
                stack.pop()
        elif ch == ";" and (not stack or stack[-1] == "at"):
            statement = "".join(buffer).strip()
            buffer = []
            if statement:
                found.append(f"the statement {statement[:40]!r} stands outside any rule")
        else:
            buffer.append(ch)
    if stack:
        found.append("a style element leaves a block open")
    elif "".join(buffer).strip():
        # The last statement needs no ';' (`@import "x"` at the end of the block still imports).
        found.append(f"the statement {''.join(buffer).strip()[:40]!r} stands outside any rule")
    return found


def reader_link_problem(name: str, tag: str, url: str) -> str | None:
    """Why a link attribute (href or cite, its value decoded) is not one a reader body may carry, or None.

    An href is on an <a>. A fragment stays on the page; an atlas article keeps to READER_ATLAS_LINK
    and has no dot segment; an http(s) page keeps to READER_WEB_LINK and is on another host than
    READER_SITE_HOSTS.
    """
    rule = f"{READER_LINK_RULE.format(name=name)} (a <{tag}> has {url[:60]!r})"
    if name == "href" and tag != "a":
        return rule
    if url.startswith("#"):
        return None
    if url.startswith("/atlas/"):
        match = READER_ATLAS_LINK.match(url)
        if match is None:
            return ("a link into the atlas is /atlas/<pageid> or /atlas/<pageid>/<slug>, its slug one path segment "
                    f"as atlasSlug writes it ({url[:60]!r} is not)")
        slug = match.group(1)
        if slug is not None and slug.lower().replace("%2e", ".") in (".", ".."):
            return f"a link into the atlas has a dot segment, which a browser resolves out of the article ({url[:60]!r})"
        return None
    web = READER_WEB_LINK.match(url)
    if web is None:
        return rule
    if web.group(1).lower().rstrip(".") in READER_SITE_HOSTS:
        return (f"a link to this site itself ({url[:60]!r}): the reader links into the site only as "
                "/atlas/<pageid>/<slug>")
    return None


def reader_style_attr_problems(value: str) -> list[str]:
    """A style attribute's problems: no escape, no URL of any kind, nothing that fetches or escapes the article."""
    text, strings, found = reader_css_text(value)
    if READER_STYLE_ATTR.search(text) or "<" in value:
        found.append("a style attribute holds url(), image-set(), expression(), a javascript: URL, a binding, "
                     "a block or a fixed or sticky position")
    return found


class ReaderScan(HTMLParser):
    """One pass over a reader body: what it may hold, its text layer's raw text, and its marks.

    ``statuses`` maps each passage's item hex to the status its marks must carry. The raw text is
    web-html/3's: decoded text in document order, nothing from style, one LF dropped after pre and
    listing (the canonical body has no script, template or comment, and the scan refuses them).
    """

    def __init__(self, statuses: dict[str, str], no_status: frozenset[str] = frozenset()):
        super().__init__(convert_charrefs=True)
        self.statuses = statuses
        self.no_status = no_status                          # item hexes of passages with no status
        self.pieces: list[str] = []
        self.length = 0
        self.stack: list[str] = []
        self.css: list[str] = []
        self.eat_lf = False
        self.mark: dict | None = None
        self.marks: list[tuple[int, int, str]] = []        # (raw start, raw end, item hex), document order
        self.anchored: dict[str, bool] = {}                 # item hex -> its first mark carries id="p-<hex>"
        self.found: dict[str, int] = {}                     # problem -> how many times
        self.media: list[tuple[str, str]] = []              # (tag, URL) of every file the body fetches
        self.css_media: list[str] = []                      # the URLs its TemplateStyles fetch

    def flag(self, message: str) -> None:
        self.found[message] = self.found.get(message, 0) + 1

    def problems(self) -> list[str]:
        return [message if n == 1 else f"{message} ({n} times)" for message, n in self.found.items()]

    # -- attributes ------------------------------------------------------------------------
    def _attribute(self, tag: str, name: str, value: str) -> None:
        if name == "name":
            # <img name="getElementById"> shadows document.getElementById for every script on the page.
            self.flag(f"the name attribute (on a <{tag}>) is not allowed: a named element shadows the page's "
                      "document properties")
        elif (name.startswith("on") or name in READER_ACTIVE_ATTRS or name.startswith("xlink:") or ":" in name
              or not (name in READER_ATTRS or READER_ATTR_FAMILY.match(name))):
            self.flag(f"the {name} attribute (on a <{tag}>) is not allowed")
        elif name in READER_URL_ATTRS and any(ch in READER_WS and ch not in READER_ASCII_WS for ch in value):
            self.flag(f"a <{tag}> {name} holds a space character other than ASCII whitespace (a browser keeps it in "
                      "the URL, which then is not the URL checked)")
        elif name in ("href", "cite"):
            problem = reader_link_problem(name, tag, value)
            if problem:
                self.flag(problem)
        elif name in ("src", "poster"):
            if not value.startswith(READER_FETCH):
                self.flag(f"a <{tag}> {name} must be https on {' or '.join(READER_FETCH)} (it is {value[:60]!r})")
            else:
                self.media.append((tag, value))
        elif name == "srcset":
            for candidate in value.split(","):
                # A candidate's URL runs to the first ASCII whitespace (HTML's srcset parser).
                url = READER_ASCII_WS_RUN.split(candidate.strip(READER_ASCII_WS))[0]
                if not url.startswith(READER_FETCH):
                    self.flag(f"a srcset URL must be https on {' or '.join(READER_FETCH)} (it is {url[:60]!r})")
                else:
                    self.media.append((tag, url))
        elif name == "style":
            for message in reader_style_attr_problems(value):
                self.flag(message)
        elif name == "id" and tag != "mark" and (READER_ANCHOR_ID.match(value) or READER_CHROME_ID.match(value)):
            self.flag(f"the id {value!r} is the page's own (a passage anchor or the page's chrome)")
        elif name == "class":
            for token in READER_CLASS_SPLIT.split(value):
                if READER_CHROME_CLASS.match(token):
                    self.flag(f"the class {token!r} is the page's own (its stylesheets and scripts give the page's "
                              "classes their place and behaviour)")
        elif name == "referrerpolicy" and value != "no-referrer":
            self.flag(f"a referrerpolicy must be no-referrer (a <{tag}> has {value[:30]!r})")

    def _open_mark(self, attrs: dict[str, str]) -> None:
        if set(attrs) - READER_MARK_ATTRS or attrs.get("class") != "atl":
            self.flag('a mark carries class="atl", data-p, data-status and (the first of a passage) its id, nothing else')
        key = attrs.get("data-p") or ""
        if key in self.no_status:
            self.flag("a mark on a passage with no status (it gets no highlight until it is read; unplaced lists it)")
            key = ""
        elif key not in self.statuses:
            self.flag("a mark's data-p names no passage of this article (its item hex)")
            key = ""
        elif attrs.get("data-status") != self.statuses[key]:
            self.flag("a mark's data-status is not its passage's status")
        if self.mark is not None:
            self.flag("a mark is nested in another")
        if key and key not in self.anchored:
            self.anchored[key] = attrs.get("id") == f"p-{key}"
        elif "id" in attrs:
            self.flag("only a passage's first mark carries its id")
        if "id" in attrs and attrs["id"] != f"p-{key}":
            self.flag("a mark's id is p-<its data-p>")
        self.mark = {"key": key, "start": self.length, "depth": len(self.stack)}

    # -- events ----------------------------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, True)

    def _tree(self, tag: str) -> None:
        """Refuse a start tag a browser's tree builder would place elsewhere than html.parser does.

        html.parser keeps every element where its tags are; a browser closes, reopens and moves
        elements by HTML's tree-construction rules. Where the two trees differ, what the builder
        checked is not what the page holds: inside MathML a <style>'s content is markup, not CSS,
        and an HTML element there leaves the formula; a <p> closes before a block; a table moves
        what is not its own child out in front of it. The canonical bodies do none of this.
        """
        stack = self.stack
        if "math" in stack:
            if tag == "style":
                self.flag("a style element inside a formula (inside MathML its content is markup, not CSS)")
            elif tag not in READER_MATHML:
                self.flag(f"a <{tag}> inside a formula (a browser moves an HTML element out of the MathML)")
            return
        if tag in READER_MATHML and tag != "math":
            self.flag(f"a <{tag}> outside a formula")
        parent = stack[-1] if stack else None
        if parent in READER_TABLE_KIDS and tag not in READER_TABLE_KIDS[parent]:
            self.flag(f"a <{tag}> directly in a <{parent}> (a browser moves it out in front of the table)")
        if tag in READER_TABLE_PARENTS and parent not in READER_TABLE_PARENTS[tag]:
            self.flag(f"a <{tag}> outside its place in a table (a browser drops or moves it)")
        if tag == "a" and "a" in stack:
            self.flag("a link inside a link (a browser closes the first)")
        if tag in READER_CLOSES_P:
            for name in reversed(stack):
                if name in READER_SCOPES:
                    break
                if name == "p":
                    self.flag(f"a <{tag}> inside a <p> (a browser closes the paragraph before it)")
                    break
        if tag in READER_HEADINGS and parent in READER_HEADINGS:
            self.flag("a heading inside a heading (a browser closes the first)")
        if tag == "li" or tag in ("dd", "dt"):
            lists = ("ul", "ol") if tag == "li" else ("dl",)
            for name in reversed(stack):
                if name in lists or name in READER_SCOPES:
                    break
                if (name == "li") if tag == "li" else name in ("dd", "dt"):
                    self.flag(f"a <{tag}> inside a <{name}> of the same list (a browser closes the first)")
                    break

    def _start(self, tag: str, attrs: list, closed: bool) -> None:
        self.eat_lf = False
        if tag not in READER_TAGS:
            self.flag(f"a <{tag}> element is not allowed in reader HTML")
        else:
            self._tree(tag)
        values = {name: value if value is not None else "" for name, value in attrs}
        if len(values) != len(attrs):
            self.flag(f"a <{tag}> repeats an attribute")
        for name, value in values.items():
            self._attribute(tag, name, value)
        if tag == "a" and "target" in values:
            if values["target"] != "_blank" or "noopener" not in values.get("rel", "").split():
                self.flag('a link that opens a new page has target="_blank" and rel noopener')
        if tag == "mark":
            if closed:
                self.flag("a mark is self-closing")
                return
            self._open_mark(values)
        if tag in READER_VOID:
            return
        if closed:
            # HTML5 honours the self-closing flag only in MathML (and SVG, which is not allowed here).
            if tag not in READER_MATHML:
                self.flag(f"a self-closing <{tag}/>, which a browser reads as an open element")
            return
        self.stack.append(tag)
        if tag in READER_EAT_LF:
            self.eat_lf = True

    def handle_endtag(self, tag):
        self.eat_lf = False
        if tag in READER_VOID:
            # A browser reads </br> as <br> and drops the others.
            self.flag(f"an end tag </{tag}> of a void element")
            return
        at = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i] == tag), -1)
        if at < 0:
            self.flag(f"a </{tag}> closes nothing")
            return
        if at != len(self.stack) - 1:
            # Every element is closed by its own end tag, innermost first: a browser closes, and for
            # <a>, <b>, <i> and the like reopens, what an end tag skips, past the article's end.
            self.flag(f"a </{tag}> closes a <{tag}> with a <{self.stack[-1]}> still open inside it")
        if self.mark is not None:
            depth = self.mark["depth"]
            if tag == "mark" and at == depth:
                if len(self.stack) - 1 > depth:
                    self.flag("a mark ends with elements inside it still open")
                if self.mark["key"]:
                    self.marks.append((self.mark["start"], self.length, self.mark["key"]))
                self.mark = None
            elif at <= depth:
                self.flag("an element ends inside a mark it encloses (a mark crosses an element boundary)")
        if tag == "style":
            for message in reader_css_problems("".join(self.css), self.css_media):
                self.flag(message)
            self.css = []
        del self.stack[at:]

    def handle_data(self, data):
        if self.eat_lf:
            self.eat_lf = False
            for newline in ("\r\n", "\r", "\n"):
                if data.startswith(newline):
                    data = data[len(newline):]
                    break
        if "style" in self.stack:
            self.css.append(data)
            return
        if self.stack and self.stack[-1] in READER_TABLE_KIDS and data.strip(" \t\n\r\f"):
            self.flag(f"text directly in a <{self.stack[-1]}> (a browser moves it out in front of the table)")
        if data:
            self.pieces.append(data)
            self.length += len(data)

    def handle_comment(self, data):
        self.flag("a comment (the canonical body has none)")

    def handle_decl(self, decl):
        self.flag("a declaration (a reader body is a fragment)")

    def handle_pi(self, data):
        self.flag("a processing instruction")

    def unknown_decl(self, data):
        self.flag("a CDATA section or unknown declaration")

    def finish(self) -> str:
        self.close()
        if self.mark is not None:
            self.flag("a mark is never closed")
        if "style" in self.stack:
            for message in reader_css_problems("".join(self.css), self.css_media):
                self.flag(message)
            self.flag("a style element is never closed")
        if self.stack:
            # An element left open runs on into the page: <table><tr><td> takes in the panel, and an
            # <a href> makes the page after the article one link.
            self.flag(f"an element is never closed (<{'>, <'.join(self.stack[-3:])}>)")
        return "".join(self.pieces)


def reader_layer(raw: str) -> tuple[str, Any]:
    """web-html/3's layer of a raw text, and the layer offset of each non-White_Space raw offset."""
    runs = [(m.start(), m.end()) for m in READER_WS_RUN.finditer(raw)]
    ends = [end for _, end in runs]
    dropped = [0]
    for start, end in runs:
        dropped.append(dropped[-1] + (end - start if start == 0 else end - start - 1))

    def offset(i: int) -> int:
        return i - dropped[bisect_right(ends, i)]

    return READER_WS_RUN.sub(" ", raw).strip(" "), offset


def reader_licence_url(url: Any) -> bool:
    """Whether `url` is a licence's own page the reader may link (READER_LICENCE_URL, READER_LICENCE_HOSTS)."""
    if not (isinstance(url, str) and len(url) <= 300):
        return False
    m = READER_LICENCE_URL.match(url)
    return m is not None and m.group(1) in READER_LICENCE_HOSTS


def reader_images(entry: dict, media: list[tuple[str, str]]) -> tuple[list[dict], list[str]]:
    """The files a body shows, each with its project, licence and author: (the view's list, problems).

    Every URL must name a file (READER_MEDIA_URL), never a Wikimedia or Foundation mark by its name
    (READER_WIKIMEDIA_MARK), that the entry's `images` names under its own project with its
    restrictions, none of them barred (READER_BARRED_RESTRICTIONS: a trademarked file is a mark
    whatever its name); an English Wikipedia upload must have a free licence there
    (READER_FREE_LICENCE) and categories that make it free everywhere (READER_FREE_CATEGORY, none
    of READER_NOT_FREE_CATEGORIES), and a Commons file a free licence by its id or, with none, by its
    name (READER_FREE_LICENCE_NAMES). A file under a licence that asks for its author's name has one
    (READER_ATTRIBUTION_LICENCE), and one whose licence has a text of its own links it (licence_url).
    `images` names no file the body does not show.
    """
    found: list[str] = []
    listed = entry.get("images", {})
    if not isinstance(listed, dict):
        return [], ["images must map each file the html shows to {project, licence, licence_id, licence_url, author, "
                    "restrictions} (an English Wikipedia upload's also to its categories)"]
    shown: dict[str, str] = {}
    for tag, url in media:
        m = READER_MEDIA_URL.match(url)
        if m is None:
            found.append(f"a <{tag}> fetches {url[:80]!r}, which is no file of Wikimedia Commons or of English "
                         "Wikipedia (an upload URL: the original, a thumbnail or a transcode)")
            continue
        name = unquote(m.group(2))
        if READER_WIKIMEDIA_MARK.search(name):
            found.append(f"File:{name} is a Wikimedia logo or mark, which the reader leaves out (the Foundation's "
                         "trademark policy covers them)")
            continue
        if shown.setdefault(name, m.group(1)) != m.group(1):
            found.append(f"File:{name} is fetched from both Commons and English Wikipedia")
    out = []
    for name, project in sorted(shown.items()):
        info = listed.get(name)
        if not isinstance(info, dict) or set(info) != READER_IMAGE_KEYS[project] or info["project"] != project:
            what = "a Commons file" if project == "commons" else "an English Wikipedia upload"
            found.append(f"images names no licence for File:{name} ({what}): every file the html shows is listed, "
                         "under its project, as {project, licence, licence_id, licence_url, author, restrictions}, an "
                         "English Wikipedia upload with its categories too")
            continue
        licence, licence_id = info["licence"], info["licence_id"]
        if not (licence is None or (isinstance(licence, str) and DISPLAY_NAME_RE.match(licence))) \
                or not (licence_id is None or isinstance(licence_id, str)):
            found.append(f"images[{name}]: licence is the licence's printable name or null, licence_id its id or null")
            continue
        restrictions = info["restrictions"]
        if not isinstance(restrictions, list) or len(restrictions) > MAX_READER_RESTRICTIONS \
                or not all(isinstance(r, str) and READER_RESTRICTION_RE.match(r) for r in restrictions) \
                or len(set(restrictions)) != len(restrictions):
            found.append(f"images[{name}]: restrictions is the list of the file page's restrictions as the API gives "
                         f"them (lower-case words such as \"trademarked\", each once, at most {MAX_READER_RESTRICTIONS}; "
                         "[] for none)")
            continue
        marked = sorted(set(restrictions) & READER_BARRED_RESTRICTIONS)
        if marked:
            found.append(f"File:{name}'s file page marks it {marked[0]}, so it is a mark, which the reader leaves out "
                         "whatever its name or licence")
            continue
        free = isinstance(licence_id, str) and READER_FREE_LICENCE.match(licence_id) is not None
        if project == "en" and not (free and isinstance(licence, str)):
            found.append(f"File:{name} is an English Wikipedia upload without a free licence (licence_id "
                         f"{licence_id!r}): only a free one may be shown")
            continue
        if project == "en":
            categories = info["categories"]
            if not isinstance(categories, list) or len(categories) > MAX_READER_CATEGORIES \
                    or not all(isinstance(c, str) and READER_CATEGORY_RE.match(c) for c in categories):
                found.append(f"images[{name}]: categories is the list of the upload's categories as the API gives "
                             f"them (\"Category:...\", at most {MAX_READER_CATEGORIES})")
                continue
            barred = sorted(set(categories) & READER_NOT_FREE_CATEGORIES)
            if barred or READER_FREE_CATEGORY not in categories:
                why = (f"it is in {barred[0]!r}" if barred else f"it is not in {READER_FREE_CATEGORY!r}")
                found.append(f"File:{name} is an English Wikipedia upload that is not free everywhere ({why}): only "
                             "one free in its source country as well as in the United States may be shown")
                continue
        if project == "commons" and not isinstance(licence, str):
            found.append(f"File:{name} is a Commons file whose licence images does not name: a file is shown only "
                         "with its licence, and Commons hosting it is not one")
            continue
        if project == "commons" and licence_id is not None and not free:
            found.append(f"File:{name}'s licence id {licence_id!r} is not a free licence")
            continue
        if project == "commons" and licence_id is None and licence.lower() not in READER_FREE_LICENCE_NAMES:
            found.append(f"File:{name}'s licence {licence!r} has no licence id and is no free licence by its name "
                         f"({', '.join(sorted(READER_FREE_LICENCE_NAMES))})")
            continue
        author, licence_url = info["author"], info["licence_url"]
        if not (author is None or (isinstance(author, str) and READER_AUTHOR_RE.match(author))):
            found.append(f"images[{name}]: author is the file's author as its file page credits them, plain text (at "
                         "most 300 characters, no markup), or null")
            continue
        if not (licence_url is None or reader_licence_url(licence_url)):
            found.append(f"images[{name}]: licence_url is the licence's own page, https on "
                         f"{', '.join(sorted(READER_LICENCE_HOSTS))}, or null")
            continue
        terms = (licence_id if isinstance(licence_id, str) else licence).lower()
        if READER_ATTRIBUTION_LICENCE.match(terms) and author is None:
            found.append(f"File:{name} is under {licence}, which asks for its author's name with every copy, and "
                         "images names no author")
            continue
        if READER_LICENCE_TEXT.match(terms) and licence_url is None:
            found.append(f"File:{name} is under {licence}, whose terms ask for a link to the licence with every copy, "
                         "and images gives no licence_url")
            continue
        if not READER_FILE_NAME.match(name):
            found.append(f"File:{name[:60]} is not a file name the page can cite")
            continue
        out.append({"file": name, "project": project, "licence": licence, "licence_url": licence_url,
                    "author": author})
    for name in sorted(set(listed) - set(shown)):
        found.append(f"images lists File:{str(name)[:80]}, which the html does not show")
    return out, found


def passage_status_word(passage: dict) -> str:
    """The status a passage's marks carry: the page's (atlas-render passageStatus). A passage with
    none ("unresolved") carries no mark: it gets no highlight until it is read."""
    if passage["outcome"] == "unresolved" or passage.get("status") not in STATUSES:
        return "unresolved"
    return passage["status"]


def reader_entry(entry: Any, view: dict, rows: list[dict]) -> tuple[dict | None, bytes | None, str | None, list[str]]:
    """One --reader-html article, checked against its view and records: (reader view, file bytes, the text
    layer its body was proven to be, problems)."""
    if not isinstance(entry, dict):
        return None, None, None, ["an article is an object"]
    found = [f"unknown key {key!r}" for key in sorted(set(entry) - READER_ENTRY_KEYS)]
    source = view["source"]
    for key, want in (("source_id", view["source_id"]), ("revision", view["revision"]),
                      ("html_sha256", source["html_sha256"]), ("layer_sha256", source["layer_sha256"])):
        if entry.get(key) != want:
            found.append(f"{key} must be the article's {want!r}: the revision whose body the records pin")
    if source["renderer"] is not None and entry.get("renderer") != source["renderer"]:
        found.append(f"renderer must be the records' {source['renderer']!r}")
    html = entry.get("html")
    try:
        data = html.encode("utf-8") if isinstance(html, str) else b""
    except UnicodeEncodeError:      # a lone surrogate from the JSON
        data = b""
    if not isinstance(html, str) or not 0 < len(data) <= MAX_READER_HTML_BYTES:
        found.append(f"html must be the article's body, at most {MAX_READER_HTML_BYTES} bytes of UTF-8")
        return None, None, None, found
    if READER_CONTROL.search(html):
        found.append("html holds a control character (a browser reads NUL and the like differently from html.parser)")
    span_of = {row["id"]: (row["span"], row["span_sha256"]) for row in rows}
    statuses: dict[str, str] = {}
    no_status: set[str] = set()
    passage_of: dict[str, dict] = {}
    for passage in view["passages"]:
        match = ITEM_HEX_RE.match(str(passage.get("item", "")))
        if not match or match.group(1) in passage_of:
            found.append(f"{passage['id']}: its item must be In-text #<hex>, one hex per passage of the article")
            continue
        # A passage with no status (unresolved) gets no highlight until it is read: it has no mark.
        word = passage_status_word(passage)
        if word == "unresolved":
            no_status.add(match.group(1))
        else:
            statuses[match.group(1)] = word
        passage_of[match.group(1)] = passage
    scan = ReaderScan(statuses, frozenset(no_status))
    scan.feed(html)
    raw = scan.finish()
    found += scan.problems()
    images, messages = reader_images(entry, scan.media + [("style", url) for url in scan.css_media])
    found += messages
    layer, offset = reader_layer(raw)
    if sha256_hex(layer.encode("utf-8")) != source["layer_sha256"]:
        found.append("its text, read as web-html/3 reads a body, is not the revision's layer (layer_sha256): "
                     "the html must change no character of the pinned revision")
        return None, None, None, found
    ranges: dict[str, list[tuple[int, int]]] = {}
    for start, end, key in scan.marks:
        first = next((i for i in range(start, end) if raw[i] not in READER_WS), None)
        if first is None:
            continue    # a space between two runs of the passage, wrapped like the rest
        last = next(i for i in range(end - 1, start - 1, -1) if raw[i] not in READER_WS)
        ranges.setdefault(key, []).append((offset(first), offset(last) + 1))
    for key, anchored in scan.anchored.items():
        if not anchored:
            found.append(f"{passage_of[key]['id']}: its first mark carries id=\"p-{key}\" (the card list's anchor)")
    placed = []
    for key, pieces in ranges.items():
        passage = passage_of[key]
        span, span_sha = span_of[passage["id"]]
        gaps = [layer[a[1]:b[0]] for a, b in zip(pieces, pieces[1:])]
        start, end = pieces[0][0], pieces[-1][1]
        if any(b[0] < a[1] for a, b in zip(pieces, pieces[1:])) or any(gap not in ("", " ") for gap in gaps):
            found.append(f"{passage['id']}: its marks, in document order, leave text of the passage unmarked or "
                         "run backwards")
        elif sha256_hex(layer[start:end].encode("utf-8")) != span_sha or (span is not None and [start, end] != span):
            found.append(f"{passage['id']}: its marks cover layer[{start}:{end}], which is not its span "
                         "(the layer slice its record's span_sha256 hashes)")
        else:
            placed.append((start, end, passage["id"]))
    placed.sort()
    for (_, end_a, a), (start_b, _, b) in zip(placed, placed[1:]):
        if start_b < end_a:
            found.append(f"{a} and {b}: two passages' marks share text")
    unplaced = [passage["id"] for key, passage in passage_of.items() if key not in ranges]
    listed = entry.get("unplaced")
    if not isinstance(listed, list) or not all(isinstance(u, dict) and isinstance(u.get("item"), str)
                                               and isinstance(u.get("reason"), str) for u in listed) \
            or sorted(u["item"] for u in listed) != sorted(passage_of[k]["item"] for k in passage_of if k not in ranges):
        found.append("unplaced must list, as {item, reason}, exactly the passages the html leaves unmarked")
    n_marks = sum(1 for _ in scan.marks)
    for key, value in (("marks", n_marks), ("passages", len(ranges))):
        if key in entry and entry[key] != value:
            found.append(f"{key} says {entry[key]!r}; the html has {value}")
    if found:
        return None, None, None, found
    view = {"sha256": sha256_hex(data), "bytes": len(data), "marks": n_marks, "passages": len(placed),
            "unplaced": unplaced}
    if images:
        view["images"] = images
    return view, data, layer, []


def attach_reader_html(doc: dict, sets: list[BuiltSet], problems: Problems) -> dict[str, int]:
    """Give each article the side file has a verified body for its reader; counts of what was attached."""
    entries = doc.get("articles")
    counts = {"articles": 0, "passages": 0, "marks": 0}
    if not isinstance(entries, dict):
        problems.add("--reader-html", "articles must be an object of entries keyed by page id")
        return counts
    for built in sets:
        for view, records in built.articles:
            pageid = view["pageid"]
            if str(pageid) not in entries:
                continue
            reader, data, layer, messages = reader_entry(entries[str(pageid)], view, record_rows(records))
            for message in messages:
                problems.add(f"--reader-html: articles[{pageid}]", message)
            if reader is not None:
                view["reader"] = reader
                built.reader_files[pageid] = data
                built.reader_layers[pageid] = layer
                counts["articles"] += 1
                counts["passages"] += reader["passages"]
                counts["marks"] += reader["marks"]
    return counts


def apply_supplements(sets: list[BuiltSet], universe: dict, supplements: Supplements) -> dict:
    """Merge the side files into the sets' view models; current.json's `supplements` and `provenance`.

    Every article names what was merged (`supplements`, each side file's schema and sha256); a
    passage's `text`, a location's `signature` and an article's `provenance` appear only where the
    side file has a checked entry, and `audit` (in every article and in current.json) only with an
    audit summary. AtlasError, listing every problem, when one does not check out.
    """
    problems = Problems()
    identity: dict[str, dict] = {}
    counts: dict[str, dict] = {}
    extra: dict = {}
    for kind in supplements.given():
        doc, sha = getattr(supplements, kind)
        identity[kind] = {"schema": doc["schema"], "sha256": sha}
    # The reader html first: the text layer it proves for an article is what that article's passage
    # contexts are checked against (passage_text_entry).
    if supplements.reader_html is not None:
        counts["reader_html"] = attach_reader_html(supplements.reader_html[0], sets, problems)
        if not counts["reader_html"]["articles"] and not problems.items:
            problems.add("--reader-html", "matches no article of this build (articles are keyed by page id)")
    if supplements.passage_text is not None:
        attached, shown = attach_passage_text(supplements.passage_text[0], sets, problems)
        if not attached:
            problems.add("--passage-text", "matches no passage of this build (entries are keyed by passage_key)")
        counts["passage_text"] = {"passages": attached, "shown": shown}
    if supplements.signatures is not None:
        source, located, declarations = attach_signatures(supplements.signatures[0], sets, universe, problems)
        if source is not None and not located:
            problems.add("--signatures", "matches no location of this build (library -> commit -> declaration)")
        identity["signatures"].update(source or {})
        counts["signatures"] = {"locations": located, "declarations": declarations}
    if supplements.provenance is not None:
        runs = attach_provenance(supplements.provenance[0], sets, universe, problems)
        tallies = recheck_tallies(sets)
        extra["provenance"] = {"runs": [{**run, "rechecked": tallies[run["run_id"]]} if run["run_id"] in tallies else run
                                        for run in runs]}
    audit = audit_view(supplements.audit_summary[0], sets, problems) if supplements.audit_summary is not None else None
    problems.raise_if_any()
    for built in sets:
        for view, _ in built.articles:
            view["supplements"] = {kind: dict(value) for kind, value in identity.items()}
            if audit is not None:
                # Each article page cites the audit from its own view, as it cites its runs.
                view["audit"] = audit
    if audit is not None:
        extra["audit"] = audit
    extra["supplements"] = {kind: {**value, **counts.get(kind, {})} for kind, value in identity.items()}
    return extra


def build(set_dir: Path | list[Path], built_at: str | None = None, campaign: str | None = None,
          supplements: Supplements | None = None) -> dict[str, bytes]:
    """Return the store tree, {relative path: bytes}, for one sealed record set or several.

    One set builds exactly as it always has, under its seal's campaign. Several (a list of
    directories) build into one store when combined_sets accepts them: ``campaign`` names the
    combined build (required, a CAMPAIGN_RE token), current.json's record_set is the first set's
    by set_id and its record_sets lists every set, and each article keeps its own seal's campaign
    and record_set. built_at defaults to the latest seal's created. ``supplements`` merges the side
    files given (apply_supplements); without them the tree is exactly what it was before they existed.
    """
    dirs = [set_dir] if isinstance(set_dir, (str, Path)) else list(set_dir)
    if not dirs:
        raise AtlasError("no record set to build")
    if built_at is not None and not CREATED_RE.match(built_at):
        raise AtlasError("--built-at must be an ISO 8601 UTC time ending in Z")
    if len(dirs) == 1 and campaign is not None:
        raise AtlasError("--campaign names a build of several record sets; one set builds under its seal's campaign")
    # A combined build is no one run's, so its name is a campaign id, never a run token.
    if len(dirs) > 1 and (not isinstance(campaign, str) or not CAMPAIGN_RE.match(campaign)
                          or campaign.startswith("run:")):
        raise AtlasError("a build of several record sets needs --campaign: the campaign id naming the combined "
                         "build (letters, digits and ._:-, at most 128, not starting 'run:')")
    validate, schema_error, provenance = schema_validator()
    sets = []
    for directory in dirs:
        try:
            sets.append(build_set(Path(directory), validate, schema_error))
        except AtlasError as exc:
            if len(dirs) == 1:
                raise
            raise AtlasError(f"record set {directory}:\n{exc}") from exc
    if len(sets) > 1:
        sets = combined_sets(sets)
    first = sets[0]
    seal, seal_sha, universe = first.seal, first.seal_sha, first.universe
    if built_at is None:
        built_at = max(built.seal["created"] for built in sets)
    articles = [pair for built in sets for pair in built.articles]
    # Optional within v1, like record_sets: a build without side files adds nothing.
    extra = apply_supplements(sets, universe, supplements) if supplements is not None else {}

    tree: dict[str, bytes] = {}
    prefix = STORE_PREFIX + "/"
    tree[f"{prefix}universes/{universe['sha256']}.json"] = canonical(universe)
    shards: dict[str, list[list]] = {}
    totals = {"articles": len(articles), "passages": 0, "locations": 0, "related": 0, "by_status": _status_counts(),
              "by_prominence": _prominence_counts(), "by_library": {}, "by_importance": {}}
    reader_files = {pageid: data for built in sets for pageid, data in built.reader_files.items()}
    for article, records in articles:
        pageid = article["pageid"]
        tree[f"{prefix}articles/{pageid}.json"] = canonical(article)
        tree[f"{prefix}records/{pageid}.jsonl"] = records
        if pageid in reader_files:
            tree[f"{prefix}reader/{pageid}.html"] = reader_files[pageid]
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
        for prominence, counts in summary["by_prominence"].items():
            totals["by_prominence"][prominence]["passages"] += counts["passages"]
            for status, count in counts["by_status"].items():
                totals["by_prominence"][prominence]["by_status"][status] += count
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
    current = {
        "schema": CURRENT_SCHEMA, "campaign": seal["campaign"] if len(sets) == 1 else campaign,
        "built_at": built_at, "builder": BUILDER,
        "universe": {"id": universe["id"], "sha256": universe["sha256"], "frozen": universe["frozen"],
                     "libraries": len(universe["libraries"])},
        "record_set": record_set_entry(seal, seal_sha),
        "literature_links": {"version": provenance["version"], "commit": provenance["commit"],
                             "schema_sha256": provenance["sha256"]},
        "counts": totals,
        "index": {"hex": INDEX_HEX, "shards": sorted(shards), "entries": len(articles)},
    }
    manifest = {"schema": MANIFEST_SCHEMA, "record_set": seal_sha}
    if len(sets) > 1:
        # Optional within v1: readers look fields up by name, and record_set keeps its shape.
        current["record_sets"] = [{**record_set_entry(built.seal, built.seal_sha), "campaign": built.seal["campaign"]}
                                  for built in sets]
        manifest["record_sets"] = [built.seal_sha for built in sets]
    current.update(extra)
    tree[f"{prefix}current.json"] = canonical(current)
    manifest["files"] = {path: {"bytes": len(data), "sha256": sha256_hex(data)} for path, data in sorted(tree.items())}
    tree[f"{prefix}manifest.json"] = canonical(manifest)
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
    problem = interpreter_problem()
    if problem:
        # Before anything is read: like any other builder failure, nothing is written (exit 3).
        print(f"atlas: {problem}; nothing was written", file=sys.stderr)
        return 3
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--record-set", type=Path, required=True, action="append",
                        help="sealed record set directory; repeat it to build several sets into one store")
    parser.add_argument("--out", type=Path, required=True, help="store directory (holds atlas/v1/...)")
    parser.add_argument("--built-at", help="ISO 8601 UTC build time for current.json (default: SEAL created)")
    parser.add_argument("--campaign", help="the campaign id naming a build of several record sets "
                                           "(current.json's campaign; each article keeps its own seal's)")
    parser.add_argument("--passage-text", type=Path, metavar="FILE",
                        help=f"optional side file ({PASSAGE_TEXT_SCHEMA}): each passage's full text, checked against "
                             "its record's span_sha256")
    parser.add_argument("--signatures", type=Path, metavar="FILE",
                        help=f"optional side file ({SIGNATURES_SCHEMA}): each card's Lean signature, by library, "
                             "commit and declaration in the store's universe")
    parser.add_argument("--provenance", type=Path, metavar="FILE",
                        help=f"optional side file ({PROVENANCE_SCHEMA}): the models, operator and dates of the runs "
                             "that made each record set")
    parser.add_argument("--reader-html", type=Path, metavar="FILE",
                        help=f"optional side file ({READER_HTML_SCHEMA}): each article's pinned revision, sanitised, "
                             "with its passages marked; its text must be the records' layer and each passage's marks "
                             "its span")
    parser.add_argument("--audit-summary", type=Path, metavar="FILE",
                        help=f"optional side file ({AUDIT_SUMMARY_SCHEMA}): what an audit of this build's links "
                             "measured, the run that judged them and the record sets it read")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="compare --out with a fresh build; write nothing")
    mode.add_argument("--replace", action="store_true", help="replace an existing --out")
    args = parser.parse_args(argv)
    try:
        sets = args.record_set
        supplements = Supplements.read(args.passage_text, args.signatures, args.provenance, args.reader_html,
                                       args.audit_summary)
        tree = build(sets[0] if len(sets) == 1 else sets, args.built_at, args.campaign, supplements=supplements)
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
