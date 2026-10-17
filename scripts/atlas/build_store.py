#!/usr/bin/env python3
"""Build WikiLean's read-only formalization-atlas store from one sealed locate record set.

New code. Design and contracts: docs/ATLAS.md. The input is the sealed record set
the formal-companion skill's locate pipeline exports (its proposal
docs/proposals/wikipedia-locate.md, §2.6, §2.7 and §11.1; its
references/locate-documents.md §6). The output is a static tree the Worker reads
through the optional ATLAS binding (wiki/src/atlas.ts):

    <out>/atlas/v1/current.json                 campaign, universe, counts, build time
    <out>/atlas/v1/manifest.json                every other file with bytes and sha256
    <out>/atlas/v1/universes/<universe-sha>.json the universe: libraries, repos, commits
    <out>/atlas/v1/articles/<pageid>.json       one article's view model
    <out>/atlas/v1/records/<pageid>.jsonl       that article's Literature Links records, verbatim
    <out>/atlas/v1/index/<shard>.json           title -> page id + summary, hash-sharded

Usage (Python 3.12, standard library plus the CI's pinned jsonschema):

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
STRONG_RELATIONS = frozenset({"exactMatch", "closeMatch", "broadMatch"})
PART_RELATIONS = frozenset({"hasPart", "narrowMatch"})
PROOF_STATUSES = frozenset({"proved", "statement_only", "axiom_dependent", "wanted", "definition"})
# Proof statuses that state something without a finished proof (proposal §2.6's statement_only row).
UNPROVED = frozenset({"statement_only", "wanted", "axiom_dependent"})
# A library location counts as complete when its declaration is proved or is a definition.
COMPLETE = frozenset({"proved", "definition"})
CHALLENGES = frozenset({"survived", "downgraded", "not_required", "sampled_out"})
CONFIDENCES = frozenset({"high", "medium", "low"})
STATUSES = ("formalized", "statement_only", "partial", "not_formalized")
SUMMARY_STATUSES = (*STATUSES, "unresolved")
MAX_LOCATIONS = 5          # K, proposal §2.7
QUOTE_MAX_WORDS = 12       # exported quote caps, proposal §2.7 and §10
AFFIX_MAX_CHARS = 32
MIN_ABSENCE_QUERIES = 3    # gate rule 5, proposal §4

HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
ITEM_RE = re.compile(r"^In-text #[0-9a-f]{8,16}$")
SOURCE_ID_RE = re.compile(r"^enwiki-([1-9][0-9]*)-([1-9][0-9]*)$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
CREATED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Whitespace for title keys, spelled out so wiki/src/atlas.ts matches it exactly.
TITLE_SPACE = re.compile(r"[ \t\n\r\f\v\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000_]+")
WORD_SPACE = re.compile(r"\s+")
# A library repository: https, a dotted host, one to four path segments, none of them "." or "..".
REPO_RE = re.compile(r"^https://[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?:/[A-Za-z0-9_.~-]{1,100}){1,4}$")


class AtlasError(Exception):
    """The record set breaks the input contract; the message names every problem found."""


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


def strict_json(text: str) -> Any:
    """Parse JSON refusing duplicate keys and NaN/Infinity (the spec requires unique keys)."""
    return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constants)


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


def _yaml_load(text: str) -> Any:
    """yaml.safe_load, but a mapping with a repeated key is an error (as strict_json makes it for JSON)."""
    import yaml  # PyYAML is pinned in requirements-ci.txt

    class StrictLoader(yaml.SafeLoader):
        pass

    def mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
        loader.flatten_mapping(node)
        seen: set = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=True)
            try:
                repeated = key in seen
            except TypeError:  # an unhashable key; construct_mapping reports it
                continue
            if repeated:
                raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)

    StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    return yaml.load(text, Loader=StrictLoader)  # noqa: S506 - a SafeLoader subclass


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
    if seal.get("schema") != SEAL_SCHEMA:
        problems.add("SEAL.json", f"schema must be {SEAL_SCHEMA!r}")
    if not isinstance(seal.get("campaign"), str) or not TOKEN_RE.match(seal["campaign"]):
        problems.add("SEAL.json", "campaign must be a token of letters, digits and ._:- (at most 128)")
    universe = seal.get("universe")
    if (not isinstance(universe, dict) or not isinstance(universe.get("id"), str)
            or not TOKEN_RE.match(universe["id"]) or not HEX64.match(str(universe.get("sha256", "")))):
        problems.add("SEAL.json", "universe must be {id, sha256} with a 64-hex sha256")
    sourceset = seal.get("sourceset")
    if (not isinstance(sourceset, dict) or not isinstance(sourceset.get("id"), str)
            or not HEX64.match(str(sourceset.get("manifest_sha256", "")))):
        problems.add("SEAL.json", "sourceset must be {id, manifest_sha256}")
    if not HEX40.match(str(seal.get("skill_commit", ""))):
        problems.add("SEAL.json", "skill_commit must be a 40-hex commit")
    if not isinstance(seal.get("created"), str) or not CREATED_RE.match(seal["created"]):
        problems.add("SEAL.json", "created must be an ISO 8601 UTC time ending in Z")
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


def load_universe(seal: dict, files: dict[str, bytes], problems: Problems) -> dict:
    has_json, has_yaml = "universe.json" in files, "universe.yaml" in files
    if has_json == has_yaml:
        raise AtlasError("the record set must hold exactly one of universe.json and universe.yaml")
    if has_json:
        try:
            raw = strict_json(files["universe.json"].decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise AtlasError(f"universe.json: not valid JSON ({exc})") from exc
    else:
        try:
            import yaml  # PyYAML is pinned in requirements-ci.txt
        except ImportError as exc:  # pragma: no cover - the CI environment has it
            raise AtlasError("universe.yaml needs PyYAML; export universe.json instead") from exc
        try:
            raw = _yaml_load(files["universe.yaml"].decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise AtlasError(f"universe.yaml: not valid YAML ({exc})") from exc
    where = "universe.json" if has_json else "universe.yaml"
    if not isinstance(raw, dict):
        raise AtlasError(f"{where}: not an object")
    if raw.get("id") != seal["universe"]["id"]:
        problems.add(where, "id differs from SEAL.json universe.id")
    if raw.get("universe_sha256") != seal["universe"]["sha256"]:
        problems.add(where, "universe_sha256 differs from SEAL.json universe.sha256")
    frozen = raw.get("frozen")
    if hasattr(frozen, "isoformat"):  # YAML reads an unquoted date as a date
        frozen = frozen.isoformat()
    if not isinstance(frozen, str) or not DATE_RE.match(frozen):
        problems.add(where, "frozen must be a YYYY-MM-DD date")
    libraries_raw = raw.get("libraries")
    if not isinstance(libraries_raw, list) or not libraries_raw:
        raise AtlasError(f"{where}: libraries must be a non-empty list")
    libraries: list[dict] = []
    seen_ids: set[str] = set()
    seen_repos: set[str] = set()
    for index, lib in enumerate(libraries_raw):
        at = f"{where}: libraries[{index}]"
        if not isinstance(lib, dict):
            problems.add(at, "not an object")
            continue
        lib_id, repo, commit = lib.get("id"), lib.get("repo"), lib.get("commit")
        if not isinstance(lib_id, str) or not TOKEN_RE.match(lib_id):
            problems.add(at, "id must be a token")
            continue
        if lib_id in seen_ids:
            problems.add(at, f"duplicate library id {lib_id!r}")
        if not is_repo_url(repo):
            problems.add(at, "repo must be an https repository URL (host and path segments, no '.' or '..')")
        elif repo in seen_repos:
            problems.add(at, f"repo {repo} appears twice (one library per repository)")
        if not isinstance(commit, str) or not HEX40.match(commit):
            problems.add(at, "commit must be a 40-hex commit")
        seen_ids.add(lib_id)
        if isinstance(repo, str):
            seen_repos.add(repo)
        entry = {"id": lib_id, "repo": repo, "commit": commit}
        # Source links are built only from GitHub's blob scheme for the library's own
        # repository (wiki/src/libraries.ts), so a producer-supplied URL template could
        # only point a "Source" link somewhere else. It is refused, not ignored.
        if "blob_url_template" in lib:
            problems.add(at, "blob_url_template is not supported: the Worker builds every source link "
                             "from the repository itself (docs/ATLAS.md, contract note 6)")
        for key in ("display_name", "brain_lib", "toolchain", "src_dir"):
            value = lib.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip() or len(value) > 300:
                problems.add(at, f"{key} must be a non-empty string")
            else:
                entry[key] = value
        if "src_dir" in entry and not safe_relative(entry["src_dir"]):
            problems.add(at, "src_dir must be a safe relative path")
        if isinstance(lib.get("roots"), list) and all(isinstance(r, str) for r in lib["roots"]):
            entry["roots"] = list(lib["roots"])
        licence = lib.get("licence")
        if isinstance(licence, dict) and isinstance(licence.get("spdx"), str):
            entry["licence"] = licence["spdx"]
        libraries.append(entry)
    excluded = []
    for index, item in enumerate(raw.get("excluded") or []):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("reason"), str):
            problems.add(f"{where}: excluded[{index}]", "must be {id, reason}")
            continue
        entry = {"id": item["id"], "reason": item["reason"]}
        if is_repo_url(item.get("repo")):
            entry["repo"] = item["repo"]
        excluded.append(entry)
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
        sources.append({"source_id": source_id, "pageid": pageid, "revision": revision, "title": title,
                        "wikidata": wikidata, "html_sha256": row.get("html_sha256"),
                        "layer_sha256": row.get("layer_sha256"), "renderer": row.get("renderer"),
                        "assessment": clean_assessment or None})
    record_files = {p for p in files if p.startswith("records/")}
    expected = {f"records/{s['source_id']}.jsonl" for s in sources}
    for missing in sorted(expected - record_files):
        problems.add(missing, "listed source has no records file")
    for extra in sorted(record_files - expected):
        problems.add(extra, "records file for a source not in sources.jsonl")
    for path in sorted(files):
        top = path.split("/", 1)[0]
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
    item = ext.get("item")
    if not isinstance(item, str) or not ITEM_RE.match(item):
        problems.add(at, "locate extension item must be 'In-text #<8-16 hex>'")
        return None
    if record.get("id") != f"{source['source_id']}::{item}":
        problems.add(at, "id must be <source_id>::<item>")
    universe = ctx.universe
    if ext.get("universe") != universe["id"] or ext.get("universe_sha256") != universe["sha256"]:
        problems.add(at, "universe and universe_sha256 must name the record set's universe")
    if ext.get("challenge") not in CHALLENGES:
        problems.add(at, f"challenge must be one of {sorted(CHALLENGES)}")
    kind = record.get("kind")
    if kind not in KINDS:
        problems.add(at, f"kind must be one of {sorted(KINDS)}")
    section = ext.get("section")
    if section is not None and (not isinstance(section, list) or not all(isinstance(s, str) and s for s in section)):
        problems.add(at, "section must be a list of heading strings")
        section = None
    prominence = ext.get("prominence")
    if prominence is not None and prominence not in PROMINENCES:
        problems.add(at, f"prominence must be one of {sorted(PROMINENCES)}")
        prominence = None

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
    if subject_ext.get("pageid") != source["pageid"] or subject_ext.get("html_sha256") != source["html_sha256"]:
        problems.add(at, "subject extension pageid and html_sha256 must match sources.jsonl")
    location = end.get("location")
    quote = location.get("quote") if isinstance(location, dict) else None
    messages = _quote_problems(quote)
    for message in messages:
        problems.add(at, message)
    if messages:
        return None

    base = {"item": item, "id": record.get("id"), "kind": kind, "section": section, "prominence": prominence,
            "quote": {key: quote[key] for key in ("exact", "prefix", "suffix") if key in quote},
            "subject_uri": uri, "challenge": ext.get("challenge")}
    relation = record.get("relation")
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
    expected = {(lib["repo"], lib["commit"]) for lib in ctx.universe["libraries"]}
    got = []
    for end in objects:
        if end.get("type") != "lean" or "location" in end:
            problems.add(at, "a checked absence has whole-library lean ends (no location)")
        got.append((end.get("uri"), end.get("version")))
    if len(got) != len(set(got)) or set(got) != expected:
        problems.add(at, "a checked absence names every universe library at its commit, once each")
    receipt = ext.get("receipt")
    if not isinstance(receipt, dict) or not HEX64.match(str(receipt.get("sha256", ""))):
        problems.add(at, "receipt must be {sha256, queries, candidates}")
        return None
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
    if receipt.get("queries") != len(queries) or receipt.get("candidates") != len(candidates):
        problems.add(at, "receipt counts differ from the receipt file")
    if len({q for q in queries if isinstance(q, str)}) < MIN_ABSENCE_QUERIES:
        problems.add(path, f"an absence lists at least {MIN_ABSENCE_QUERIES} distinct queries")
    return {**base, "type": "absence", "status": "not_formalized",
            "receipt": {"sha256": receipt["sha256"], "queries": len(queries), "candidates": len(candidates)}}


def _location(record: dict, base: dict, objects: list, ext: dict, relation: str, ctx: Context, at: str,
              problems: Problems) -> dict | None:
    status = record.get("status")
    if status not in STATUSES:
        problems.add(at, f"a location record carries a derived status, one of {list(STATUSES)}")
        return None
    if len(objects) != 1:
        problems.add(at, "a location record has exactly one lean end")
        return None
    end = objects[0]
    location = end.get("location") or {}
    end_ext = _locate_ext(end) or {}
    library = next((lib for lib in ctx.universe["libraries"] if lib["id"] == end_ext.get("library")), None)
    if end.get("type") != "lean" or not isinstance(location.get("declaration"), str) \
            or not isinstance(location.get("module"), str):
        problems.add(at, "the lean end names a declaration and its module")
        return None
    if library is None:
        problems.add(at, f"library {end_ext.get('library')!r} is not in the universe")
        return None
    if end.get("uri") != library["repo"] or end.get("version") != library["commit"]:
        problems.add(at, "the lean end's uri and version must be its library's repo and commit")
    path, line, end_line = end_ext.get("path"), end_ext.get("line"), end_ext.get("end_line")
    if not isinstance(path, str) or not safe_relative(path) or not path.endswith(".lean"):
        problems.add(at, "path must be a safe relative .lean path")
    if type(line) is not int or type(end_line) is not int or not 1 <= line <= end_line:
        problems.add(at, "line and end_line must be integers with 1 <= line <= end_line")
    if end_ext.get("proof_status") not in PROOF_STATUSES:
        problems.add(at, f"proof_status must be one of {sorted(PROOF_STATUSES)}")
    key = ext.get("location_key")
    if not isinstance(key, str) or not HEX64.match(key):
        problems.add(at, "location_key must be 64 hex")
    elif key in ctx.location_keys:
        problems.add(at, f"location_key {key} appears twice")
    else:
        ctx.location_keys.add(key)
    rank = ext.get("rank")
    if type(rank) is not int or not 1 <= rank <= MAX_LOCATIONS:
        problems.add(at, f"rank must be an integer from 1 to {MAX_LOCATIONS}")
    if ext.get("confidence") not in CONFIDENCES:
        problems.add(at, f"confidence must be one of {sorted(CONFIDENCES)}")
    note = record.get("note")
    if relation == "closeMatch" and not (isinstance(note, str) and note.strip()):
        problems.add(at, "a closeMatch names its convention difference in note (proposal §2.6)")
    return {**base, "type": "location", "status": status, "location": {
        "library": library["id"], "repo": library["repo"], "commit": library["commit"],
        "declaration": location["declaration"], "module": location["module"],
        "path": path, "line": line, "end_line": end_line,
        "relation": relation, "proof_status": end_ext.get("proof_status"), "rank": rank,
        "confidence": ext.get("confidence"), "challenge": ext.get("challenge"),
        "location_key": key, "note": note if isinstance(note, str) and note.strip() else None,
    }}


def _rank_key(loc: dict) -> int:
    # A malformed rank is already a reported problem; sort it last rather than crash.
    return loc["rank"] if type(loc["rank"]) is int else 99


def _by_rank(locations: list[dict], relations: frozenset[str], proofs: frozenset[str] | None = None) -> list[dict]:
    return [loc for loc in locations if loc["relation"] in relations
            and (proofs is None or loc["proof_status"] in proofs)]


def derive_status(kind: str, locations: list[dict]) -> list[tuple[str, dict]]:
    """Proposal §2.6's derived status of a located passage, recomputed from its locations.

    `locations` are in rank order. Returns the acceptable (status, basis) pairs: the
    status the table gives and the highest-ranked location that justifies it. The
    table's rows are read first-match, top to bottom (formalized, statement_only,
    partial). Two pairs come back only where the table admits both readings, and an
    empty list means the table gives this passage no status. Kinds the table does not
    name (example, construction, notation, other) follow WikiLean's reading in
    docs/ATLAS.md contract note 7: the theorem and definition rules together.
    """
    strong = STRONG_RELATIONS
    if kind in ("theorem", "conjecture"):
        proved = _by_rank(locations, strong, frozenset({"proved"}))
        if proved:
            return [("formalized", proved[0])]
        # "the best such location is statement_only, wanted or axiom_dependent, or is a
        # Prop-valued definition stating a theorem passage"
        stated = [loc for loc in _by_rank(locations, strong, UNPROVED | {"definition"})
                  if not (loc["relation"] == "broadMatch" and loc["proof_status"] == "definition")]
        if stated:
            return [("statement_only", stated[0])]
        # A broadMatch to a definition is both a more general Prop-valued definition
        # (statement_only) and the partial row's "broadMatch to a definition".
        broad_def = _by_rank(locations, frozenset({"broadMatch"}), frozenset({"definition"}))
        parts = sorted(_by_rank(locations, PART_RELATIONS) + broad_def, key=_rank_key)
        if broad_def:
            return [("statement_only", broad_def[0]), ("partial", parts[0])]
        if parts:
            return [("partial", parts[0])]
        return []
    if kind == "definition":
        defined = _by_rank(locations, frozenset({"exactMatch", "closeMatch"}), frozenset({"definition"}))
        if defined:
            return [("formalized", defined[0])]
        stated = _by_rank(locations, frozenset({"exactMatch", "closeMatch"}), UNPROVED)
        if stated:
            return [("statement_only", stated[0])]
        broad_def = _by_rank(locations, frozenset({"broadMatch"}), frozenset({"definition"}))
        parts = sorted(_by_rank(locations, PART_RELATIONS) + broad_def, key=_rank_key)
        broad_unproved = _by_rank(locations, frozenset({"broadMatch"}), UNPROVED)
        if broad_unproved:
            # A more general declaration that is itself unfinished: both rows apply.
            return [("statement_only", broad_unproved[0]),
                    ("partial", sorted(parts + broad_unproved, key=_rank_key)[0])]
        if parts:
            return [("partial", parts[0])]
        return []
    # The other kinds take the theorem rule and the definition rule together: a proved
    # exact, close or broad match, or an exact or close match to a definition.
    complete = sorted(_by_rank(locations, strong, frozenset({"proved"}))
                      + _by_rank(locations, frozenset({"exactMatch", "closeMatch"}), frozenset({"definition"})),
                      key=_rank_key)
    if complete:
        return [("formalized", complete[0])]
    stated = _by_rank(locations, strong, UNPROVED)
    if stated:
        return [("statement_only", stated[0])]
    parts = sorted(_by_rank(locations, PART_RELATIONS)
                   + _by_rank(locations, frozenset({"broadMatch"}), frozenset({"definition"})), key=_rank_key)
    if parts:
        return [("partial", parts[0])]
    return []


def _shape(locations: list[dict]) -> str:
    shown = [f"{loc['relation']} to {loc['proof_status']}" for loc in locations if loc["relation"] != "relatedMatch"]
    return ", ".join(shown) or "relatedMatch only"


def status_problems(kind: str, status: str, locations: list[dict]) -> tuple[list[str], dict | None]:
    """Compare a located passage's stamped status with the recomputed one; returns (problems, basis)."""
    if status == "not_formalized":
        return ["not_formalized needs a checked absence (noMatch record)"], None
    if kind not in KINDS:
        return [], None  # already reported by classify_record
    derived = derive_status(kind, locations)
    if not derived:
        return [f"proposal §2.6 gives a {kind} passage with {_shape(locations)} no status "
                f"(docs/ATLAS.md, contract note 7)"], None
    for candidate, basis in derived:
        if candidate == status:
            return [], basis
    expected = " or ".join(candidate for candidate, _ in derived)
    return [f"the records say {status}, but proposal §2.6 derives {expected} for a {kind} passage "
            f"with {_shape(locations)}"], None


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
            for key in ("kind", "quote", "section", "prominence"):
                if other[key] != first[key]:
                    problems.add(at, f"records of one passage disagree on {key}")
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
            problems.add(at, "every record of a passage carries the same derived status")
        if len(locations) > MAX_LOCATIONS:
            problems.add(at, f"at most {MAX_LOCATIONS} locations per passage")
        ranks = [loc["rank"] for loc in locations]
        if len(ranks) != len(set(ranks)):
            problems.add(at, "location ranks are distinct")
        status = next(iter(statuses)) if len(statuses) == 1 else None
        if unresolved:
            outcome, status = "unresolved", None
        elif absences:
            outcome = "absent"
            if any(loc["relation"] != "relatedMatch" for loc in locations):
                problems.add(at, "an absence may sit beside relatedMatch locations only")
        basis = None
        if not unresolved and not absences:
            outcome = "located"
            if status is not None:
                found, basis = status_problems(first["kind"], status, locations)
                for message in found:
                    problems.add(at, message)
        absence = None
        if absences:
            absence = {"universe": None, "universe_sha256": None, "libraries": None,
                       "receipt": absences[0]["receipt"], "challenge": absences[0]["challenge"]}
        passages.append({
            "id": pid, "item": first["item"], "kind": first["kind"], "section": first["section"],
            "prominence": first["prominence"], "quote": first["quote"], "subject_uri": first["subject_uri"],
            "outcome": outcome, "status": status, "locations": locations, "absence": absence,
            # The location that justifies the status (derive_status), so a page names it
            # rather than whichever location ranks first.
            "basis": basis["location_key"] if basis else None,
            "unresolved": {"challenge": unresolved[0]["challenge"]} if unresolved else None,
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
        "record_set": {"seal_sha256": seal_sha, "created": seal["created"], "skill_commit": seal["skill_commit"],
                       "sourceset": seal["sourceset"]},
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
