#!/usr/bin/env python3
"""Shared, deterministic input loading + graph assembly for the BRAIN builders.

build_nodes.py and build_edges.py both call build() and each writes its own
artifact — the node set and the edge set are one joint computation (decl nodes
exist only for decls referenced by >=1 ontology edge), so the assembly lives
here rather than being duplicated or ordered across the two scripts.

Everything is derived from pinned catalog inputs; there is no LLM on this path.
Node/edge shapes are the brain/SCHEMA.md contract. provenance.source values are
keys of catalog/data/source_registry.json (SCHEMA "Provenance & licensing");
the concrete input artifact is named in provenance.method.
"""
from __future__ import annotations

import csv
import fcntl
import hashlib
import html
import json
import os
import re
import stat
import sys
import urllib.parse
from collections import Counter, defaultdict
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

from build_context import (
    BuildContext,
    BuildContextError,
    EXTERNAL_PAIR_META_FIELDS,
    EXTERNAL_PAIR_TRANSACTION_SCHEMA,
    ExternalPairError,
    InputBinding,
    InputMember,
    canonical_json_bytes,
    external_pair_control_paths,
    external_pair_db_from_journal,
    validate_external_pair,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "catalog" / "data"
CACHE = ROOT / "catalog" / ".cache"
BRAIN_DATA = HERE / "data"

csv.field_size_limit(10 ** 9)

INPUTS = {
    "concept_graph_v2.json": DATA / "concept_graph_v2.json",
    "rebuild_grounding.json": DATA / "rebuild_grounding.json",
    "hierarchy.json": DATA / "hierarchy.json",
    "wikidata_universe.jsonl": DATA / "wikidata_universe.jsonl",
    "universe_extension.jsonl": DATA / "universe_extension.jsonl",
    "wikidata_crossrefs.json": DATA / "wikidata_crossrefs.json",
    "theoremgraph_links.json": DATA / "theoremgraph_links.json",
    "decl_qid_roles_v2.json": DATA / "decl_qid_roles_v2.json",
    "decl_to_qid_v2.json": DATA / "decl_to_qid_v2.json",
    "wikidata_edges.jsonl": ROOT / "catalog" / "mathlib_deps" / "wikidata_edges.jsonl",
    "theorem_matching.csv": CACHE / "theorem_matching.csv",
    "statement_formal.csv": CACHE / "statement_formal.csv",
}
OPTIONAL_INPUTS = {
    "container_links.jsonl": BRAIN_DATA / "container_links.jsonl",
    "discovery_proposals.jsonl": BRAIN_DATA / "discovery_proposals.jsonl",
    "mathlib_tag_xrefs.jsonl": DATA / "mathlib_tag_xrefs.jsonl",
    "wikidata_descriptions.json": DATA / "wikidata_descriptions.json",
    # unsolved-problems frontier (brain/ingest/formal_conjectures.py +
    # brain/ingest/erdosproblems.py + fold_proposals fc_link rows) — each
    # fail-soft: missing file = that slice of the layer skipped
    "formal_conjectures.jsonl": DATA / "formal_conjectures.jsonl",
    "erdos_joins.jsonl": DATA / "erdos_joins.jsonl",
    "fc_links.jsonl": BRAIN_DATA / "fc_links.jsonl",
    # generic Lean-repo frontier (brain/ingest/lean_repo.py) — same fail-soft;
    # user-registered repos live under catalog/data/user_repos/*.jsonl (globbed,
    # not listed here). <key>_links.jsonl = fold-verified repo_link agent joins
    # (brain/fold_proposals.py), minted mentions-ONLY by _frontier_repo_layer.
    "tauceti.jsonl": DATA / "tauceti.jsonl",
    "tauceti_links.jsonl": DATA / "tauceti_links.jsonl",
}
REGISTRY = DATA / "source_registry.json"
USER_REPOS_DIR = DATA / "user_repos"
# Mirrors brain/ingest/lean_repo.py's harvest caps (defense in depth — a file
# someone hand-dropped into user_repos/ gets the same ceiling as a harvested
# one) and its owner/repo/lib validation (the /api/repos pinned contract).
USER_REPO_BUILD_CAP = 50
USER_REPO_DECL_CAP = 20_000
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}\Z")
_LEAN_LIB_RE = re.compile(r"^[A-Z][A-Za-z0-9_]{0,63}\Z")

# The edge set ships as TWO artifacts (GitHub's 100 MB per-file hard limit):
# EDGES_OUT = every kind EXCEPT `links`; EDGES_LINKS_OUT = only kind=='links'
# rows (gitignored, deterministically rebuilt from catalog/data/external/).
# Readers treat a missing EDGES_LINKS_OUT as empty.
EDGES_OUT = BRAIN_DATA / "edges.jsonl"
EDGES_LINKS_OUT = BRAIN_DATA / "edges_links.jsonl"

# "links" sorts last: page-level hyperlinks are the lowest-priority edge kind,
# and appending keeps pre-v2 edge ordering byte-identical. "invocation"
# (frontier decl -> Mathlib decl it names in its statement) is deterministic
# but weak — mentions-strength, never anything build_cells may fuse on.
KIND_ORDER = ["contains", "formalizes", "mentions", "depends", "relates",
              "xref", "cites", "matches", "invocation", "links"]

# ---- SCHEMA.md v2 facet bitmask `f` -----------------------------------------
F_GOLD_WIKIDATA = 1 << 0    # decl carries a gold @[wikidata] source tag
F_STACKS_ATTR = 1 << 1      # decl carries @[stacks]
F_KERODON_ATTR = 1 << 2     # decl carries @[kerodon]
F_ANY_XREF = 1 << 3         # node is src or dst of >=1 xref edge
F_FORMALIZED = 1 << 4       # concept display.status == formalized
F_PARTIAL = 1 << 5          # concept display.status == partial
F_ARTICLE = 1 << 6          # concept has an annotated WikiLean article
F_LITERATURE = 1 << 7       # node has >=1 cites/matches edge; lit PAPER nodes
                            # (lit:<arxiv_id>, no #ref) carry it natively
F_EXT = 1 << 8              # node is an ext page
F_HAS_SNIPPET = 1 << 15     # ext node stores a licensed content snippet
F_DB_BIT = {"lmfdb_knowl": 1 << 9, "nlab": 1 << 10, "mathworld": 1 << 11,
            "proofwiki": 1 << 12, "stacks": 1 << 13, "oeis": 1 << 14}

# links evidence.context, best-first (dedup keeps the strongest context)
CONTEXT_RANK = {"statement": 0, "proof": 1, "body": 2, "related": 3}

# The xref keys of SCHEMA's edge table (P14534/mathlib is `formalizes` territory,
# kgmid is a KG hub id, not an external DB page — neither becomes an xref edge).
XREF_KEYS = {
    "lmfdb_knowl": "P12987", "nlab": "P4215", "mathworld": "P2812",
    "proofwiki": "P6781", "eom": "P7554", "planetmath": "P7726",
    "oeis": "P829", "metamath": "P12888", "dlmf": "P11497", "msc": "P3285",
}

AFFIRM = {"exact", "inexact"}  # theoremgraph_links _meta.affirm_labels


BASE_GRAPH_BINDING_CONTRACT = {
    # input id: (root, cardinality, requirement, class, declared path/pattern)
    "annotations": (
        "repo", "many", "optional", "immutable_source_object",
        "site/annotations/[!.]*.json",
    ),
    "brain-container-links": (
        "repo", "one", "optional", "curated_git_input",
        "brain/data/container_links.jsonl",
    ),
    "brain-discovery-proposals": (
        "repo", "one", "optional", "curated_git_input",
        "brain/data/discovery_proposals.jsonl",
    ),
    "brain-ext-anchor-links": (
        "repo", "one", "optional", "curated_git_input",
        "brain/data/ext_anchor_links.jsonl",
    ),
    "brain-fc-links": (
        "repo", "one", "optional", "curated_git_input",
        "brain/data/fc_links.jsonl",
    ),
    "concept-graph": (
        "repo", "one", "required", "immutable_source_object",
        "catalog/data/concept_graph_v2.json",
    ),
    "decl-qid-roles": (
        "repo", "one", "required", "immutable_source_object",
        "catalog/data/decl_qid_roles_v2.json",
    ),
    "decl-renames": (
        "repo", "one", "optional", "curated_git_input",
        "catalog/data/decl_renames.jsonl",
    ),
    "decl-to-qid": (
        "repo", "one", "required", "immutable_source_object",
        "catalog/data/decl_to_qid_v2.json",
    ),
    "declaration-oracle": (
        "decl_oracle", "one", "optional", "immutable_source_object",
        "declaration-data.json",
    ),
    "erdos-joins": (
        "repo", "one", "optional", "immutable_source_object",
        "catalog/data/erdos_joins.jsonl",
    ),
    "external-arxiv-citations": (
        "external", "one", "optional", "immutable_source_object",
        "arxiv_citations.jsonl",
    ),
    "external-links": (
        "external", "many", "optional", "immutable_source_object",
        "*_links.jsonl",
    ),
    "external-pages": (
        "external", "many", "optional", "immutable_source_object",
        "*_pages.jsonl",
    ),
    "formal-conjectures": (
        "repo", "one", "optional", "immutable_source_object",
        "catalog/data/formal_conjectures.jsonl",
    ),
    "hierarchy": (
        "repo", "one", "required", "immutable_source_object",
        "catalog/data/hierarchy.json",
    ),
    "mathlib-ilean-tree": (
        "mathlib", "many", "optional", "immutable_source_object",
        ".lake/build/lib/lean/**/*.ilean",
    ),
    "mathlib-source-tree": (
        "mathlib", "many", "required", "immutable_source_object",
        "Mathlib/**/*.lean",
    ),
    "mathlib-tag-xrefs": (
        "repo", "one", "optional", "immutable_source_object",
        "catalog/data/mathlib_tag_xrefs.jsonl",
    ),
    "rebuild-grounding": (
        "repo", "one", "required", "curated_git_input",
        "catalog/data/rebuild_grounding.json",
    ),
    "slogan": (
        "repo", "one", "optional", "immutable_source_object",
        "catalog/.cache/slogan.csv",
    ),
    "source-registry": (
        "repo", "one", "required", "curated_git_input",
        "catalog/data/source_registry.json",
    ),
    "statement-formal": (
        "repo", "one", "required", "immutable_source_object",
        "catalog/.cache/statement_formal.csv",
    ),
    "tauceti": (
        "repo", "one", "optional", "immutable_source_object",
        "catalog/data/tauceti.jsonl",
    ),
    "tauceti-links": (
        "repo", "one", "optional", "curated_git_input",
        "catalog/data/tauceti_links.jsonl",
    ),
    "theorem-matching": (
        "repo", "one", "required", "immutable_source_object",
        "catalog/.cache/theorem_matching.csv",
    ),
    "theoremgraph-links": (
        "repo", "one", "required", "immutable_source_object",
        "catalog/data/theoremgraph_links.json",
    ),
    "universe-extension": (
        "repo", "one", "required", "curated_git_input",
        "catalog/data/universe_extension.jsonl",
    ),
    "user-repos": (
        "repo", "many", "optional", "immutable_source_object",
        "catalog/data/user_repos/*.jsonl",
    ),
    "wikidata-crossrefs": (
        "repo", "one", "required", "immutable_source_object",
        "catalog/data/wikidata_crossrefs.json",
    ),
    "wikidata-descriptions": (
        "repo", "one", "optional", "immutable_source_object",
        "catalog/data/wikidata_descriptions.json",
    ),
    "wikidata-edges": (
        "repo", "one", "required", "immutable_source_object",
        "catalog/mathlib_deps/wikidata_edges.jsonl",
    ),
    "wikidata-universe": (
        "repo", "one", "required", "immutable_source_object",
        "catalog/data/wikidata_universe.jsonl",
    ),
}
BASE_GRAPH_INPUT_IDS = tuple(sorted(BASE_GRAPH_BINDING_CONTRACT))

_REQUIRED_INPUT_KEYS = {
    "concept_graph_v2.json": "concept-graph",
    "rebuild_grounding.json": "rebuild-grounding",
    "hierarchy.json": "hierarchy",
    "wikidata_universe.jsonl": "wikidata-universe",
    "universe_extension.jsonl": "universe-extension",
    "wikidata_crossrefs.json": "wikidata-crossrefs",
    "theoremgraph_links.json": "theoremgraph-links",
    "decl_qid_roles_v2.json": "decl-qid-roles",
    "decl_to_qid_v2.json": "decl-to-qid",
    "wikidata_edges.jsonl": "wikidata-edges",
    "theorem_matching.csv": "theorem-matching",
    "statement_formal.csv": "statement-formal",
}

_OPTIONAL_INPUT_KEYS = {
    "container_links.jsonl": "brain-container-links",
    "discovery_proposals.jsonl": "brain-discovery-proposals",
    "mathlib_tag_xrefs.jsonl": "mathlib-tag-xrefs",
    "wikidata_descriptions.json": "wikidata-descriptions",
    "formal_conjectures.jsonl": "formal-conjectures",
    "erdos_joins.jsonl": "erdos-joins",
    "fc_links.jsonl": "brain-fc-links",
    "tauceti.jsonl": "tauceti",
    "tauceti_links.jsonl": "tauceti-links",
}


@dataclass(frozen=True, slots=True)
class BoundInputFile:
    """One exact materialized input member and its authority metadata."""

    input_id: str
    root: str
    logical_path: str
    path: Path
    source_path: Path
    member: InputMember

    @property
    def pin(self) -> str:
        return self.member.pin.value


@dataclass(frozen=True, slots=True)
class ContextBuildInputs:
    """Closed-world base-graph inputs projected from a verified build context."""

    generation_id: str
    external_node_cap: int
    bindings: Mapping[str, InputBinding]
    files: Mapping[str, tuple[BoundInputFile, ...]]

    @staticmethod
    def _verify_member(
        input_id: str,
        path: Path,
        member: InputMember,
        *,
        destination: Path | None = None,
    ) -> None:
        """Verify one member and optionally copy its exact bytes privately."""
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = destination_descriptor = -1
        destination_created = False
        success = False
        try:
            descriptor = os.open(path, flags)
            initial = os.fstat(descriptor)
            current = path.lstat()
            if (
                not stat.S_ISREG(initial.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or not os.path.samestat(initial, current)
            ):
                raise BuildContextError(
                    f"base-graph input {input_id!r} is not a stable regular file: {path}"
                )
            if destination is not None:
                destination_descriptor = os.open(
                    destination,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                destination_created = True
            digest = hashlib.sha256()
            byte_length = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                byte_length += len(chunk)
                if destination_descriptor >= 0:
                    pending = memoryview(chunk)
                    while pending:
                        written = os.write(destination_descriptor, pending)
                        if written <= 0:
                            raise OSError("short write while materializing sealed input")
                        pending = pending[written:]
            final = os.fstat(descriptor)
            final_path = path.lstat()
            if (
                not os.path.samestat(initial, final)
                or not os.path.samestat(final, final_path)
            ):
                raise BuildContextError(
                    f"base-graph input {input_id!r} changed during verification: {path}"
                )
            actual_digest = digest.hexdigest()
            if byte_length != member.byte_length or actual_digest != member.sha256:
                raise BuildContextError(
                    f"base-graph input {input_id!r} bytes do not match its sealed "
                    f"member: {path}"
                )
            if destination_descriptor >= 0:
                os.fchmod(destination_descriptor, 0o400)
                os.fsync(destination_descriptor)
                copied = os.fstat(destination_descriptor)
                copied_path = destination.lstat()
                if (
                    not stat.S_ISREG(copied.st_mode)
                    or stat.S_ISLNK(copied_path.st_mode)
                    or not os.path.samestat(copied, copied_path)
                ):
                    raise BuildContextError(
                        f"private base-graph input copy is not stable: {destination}"
                    )
            success = True
        except OSError as exc:
            raise BuildContextError(
                f"could not verify base-graph input {input_id!r} at {path}: {exc}"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if destination_descriptor >= 0:
                os.close(destination_descriptor)
            if destination_created and not success:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _ensure_private_parent(root: Path, destination: Path) -> None:
        root_metadata = root.lstat()
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
            raise BuildContextError(f"base-graph private input root is not usable: {root}")
        try:
            parts = destination.relative_to(root).parts
        except ValueError as exc:
            raise BuildContextError(
                f"private base-graph input path escapes scratch: {destination}"
            ) from exc
        current = root
        for part in parts:
            current = current / part
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                metadata = current.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise BuildContextError(
                        f"private base-graph input directory is not usable: {current}"
                    )
            else:
                current.chmod(0o700)

    @classmethod
    def from_context(
        cls,
        context: BuildContext,
        *,
        materialize_root: Path | None = None,
    ) -> ContextBuildInputs:
        by_id = {binding.input_id: binding for binding in context.bindings}
        missing = sorted(set(BASE_GRAPH_INPUT_IDS) - set(by_id))
        if missing:
            raise BuildContextError(
                "base-graph context is missing inputs: " + ", ".join(missing)
            )
        if materialize_root is not None:
            materialize_root = Path(materialize_root)
            try:
                materialize_root.mkdir(mode=0o700)
            except FileExistsError as exc:
                raise BuildContextError(
                    f"base-graph private input root already exists: {materialize_root}"
                ) from exc
            else:
                materialize_root.chmod(0o700)

        projected: dict[str, tuple[BoundInputFile, ...]] = {}
        for input_id in BASE_GRAPH_INPUT_IDS:
            binding = by_id[input_id]
            expected = BASE_GRAPH_BINDING_CONTRACT[input_id]
            actual = (
                binding.root,
                binding.cardinality,
                binding.requirement,
                binding.input_class,
                binding.declared_path,
            )
            if actual != expected:
                raise BuildContextError(
                    f"base-graph input {input_id!r} contract is {actual!r}, "
                    f"expected {expected!r}"
                )
            paths = context.members(input_id)
            if len(paths) != len(binding.members):
                raise BuildContextError(
                    f"input {input_id!r} path/member count differs"
                )
            materialized_paths: list[Path] = []
            for member, path in zip(binding.members, paths, strict=True):
                destination = None
                if materialize_root is not None:
                    destination = (
                        materialize_root
                        / binding.root
                        / Path(*member.logical_path.split("/"))
                    )
                    cls._ensure_private_parent(materialize_root, destination.parent)
                cls._verify_member(
                    input_id,
                    path,
                    member,
                    destination=destination,
                )
                materialized_paths.append(destination or path)
            projected[input_id] = tuple(
                BoundInputFile(
                    input_id=input_id,
                    root=binding.root,
                    logical_path=member.logical_path,
                    path=materialized_path,
                    source_path=source_path,
                    member=member,
                )
                for member, materialized_path, source_path in zip(
                    binding.members, materialized_paths, paths, strict=True
                )
            )
        return cls(
            generation_id=context.generation_id,
            external_node_cap=context.configuration.external_node_cap,
            bindings=MappingProxyType({key: by_id[key] for key in BASE_GRAPH_INPUT_IDS}),
            files=MappingProxyType(projected),
        )

    def members(self, input_id: str) -> tuple[BoundInputFile, ...]:
        return self.files[input_id]

    def verify(self) -> None:
        """Recheck every member before publication closes the read/use window."""
        for input_id in BASE_GRAPH_INPUT_IDS:
            for item in self.members(input_id):
                self._verify_member(input_id, item.path, item.member)

    def verify_sources(self) -> None:
        """Recheck sealed source paths after reduction for fail-closed auditing."""
        for input_id in BASE_GRAPH_INPUT_IDS:
            for item in self.members(input_id):
                self._verify_member(input_id, item.source_path, item.member)

    def require_one(self, input_id: str) -> BoundInputFile:
        binding = self.bindings[input_id]
        members = self.members(input_id)
        if binding.cardinality != "one" or binding.state != "present" or len(members) != 1:
            raise BuildContextError(
                f"required base-graph input {input_id!r} is not one present file"
            )
        return members[0]

    def optional_one(self, input_id: str) -> BoundInputFile | None:
        binding = self.bindings[input_id]
        members = self.members(input_id)
        if binding.cardinality != "one" or binding.requirement != "optional":
            raise BuildContextError(
                f"base-graph input {input_id!r} is not optional cardinality-one"
            )
        if binding.state == "absent":
            return None
        if len(members) != 1:
            raise BuildContextError(
                f"base-graph input {input_id!r} is not exactly one file"
            )
        return members[0]

    def metadata(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for input_id in BASE_GRAPH_INPUT_IDS:
            binding = self.bindings[input_id]
            members = self.members(input_id)
            logical = [
                {
                    "bytes": item.member.byte_length,
                    "object": item.member.object_name,
                    "path": item.logical_path,
                    "pin": item.member.pin.to_document(),
                    "sha256": item.member.sha256,
                    "source_manifest_id": item.member.source_manifest_id,
                }
                for item in members
            ]
            result[input_id] = {
                "binding_sha256": hashlib.sha256(canonical_json_bytes(logical)).hexdigest(),
                "bytes": sum(item.member.byte_length for item in members),
                "members": len(members),
                "state": binding.state,
            }
        return result


def _pin(name: str) -> str:
    """ISO date (UTC) of the input file's mtime — the per-edge version pin."""
    return datetime.fromtimestamp(INPUTS.get(name, OPTIONAL_INPUTS.get(name)).stat().st_mtime,
                                  tz=timezone.utc).date().isoformat()


def _majority(counter: Counter) -> str | None:
    if not counter:
        return None
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _mathlib_checkout() -> Path:
    """Root of the live mathlib4 checkout (read-only; the tagging bot's)."""
    return Path(os.environ.get(
        "BRAIN_MATHLIB_CHECKOUT", "/Users/jack/Desktop/LEAN/mathlib4/Mathlib")).parent


def _decl_module_oracle(
    names: set[str],
    *,
    declaration_oracle: Path | None = None,
    ilean_files: tuple[Path, ...] | None = None,
    explicit: bool = False,
) -> dict[str, tuple[str, str | None]]:
    """Reverse index `decl name -> (defining module, kind)` for `names`.

    The module-driven sources (TheoremGraph votes/CSVs, @[wikidata] tag rows)
    only ever cover decls the corpus saw. Decls cited *solely* by a WikiLean
    annotation resolve nowhere, so they used to land at the library ROOT — the
    grey "filed here" ball (567 of them, Jack's report 2026-07-17). This is the
    last-resort fallback for exactly those.

    Two independent, genuine oracles — both keyed on the FULLY-QUALIFIED name,
    which is the whole point. A bare-suffix guess is what produced misfiles like
    `zero_mul -> Mathlib.Data.Holor`: a wrong module silently files a cell into
    the wrong area of mathematics, which is strictly worse than leaving it at
    the root. So: exact hits only, never a suffix/heuristic match.

      1. doc-gen4 `declaration-data.json` (the mathlib4_docs index the
         mathlib-search skill caches). 416k decls incl. structure fields and
         `to_additive`-generated names — the ones NO source-text scan can see,
         because they are elaborator output, not syntax. Also carries `kind`.
      2. the checkout's own `.ilean` files (Lean's language-server index,
         emitted next to the oleans). Syntactic, so it MISSES generated names,
         but it needs nothing beyond the checkout we already read for snippets
         — the floor when (1) is absent (its cache is gitignored).

    Measured 2026-07-17: (1) and (2) agree on all 124 names both know, 0
    conflicts; (1) alone reaches 208/567, (2) alone 124/567. Fail-soft
    throughout: a missing/renamed oracle degrades to the old behaviour.
    """
    found: dict[str, tuple[str, str | None]] = {}
    if not names:
        return found

    if explicit:
        dg = declaration_oracle
    else:
        dg = Path(os.environ.get("BRAIN_DECL_ORACLE", "")) if os.environ.get(
            "BRAIN_DECL_ORACLE") else (
            ROOT / ".claude/skills/mathlib-search/.cache/declaration-data.json")
    if dg is not None and (explicit or dg.exists()):
        try:
            decls = json.loads(dg.read_text(encoding="utf-8")).get("declarations") or {}
            for name in names:
                e = decls.get(name)
                if not e:
                    continue
                # docLink: "./Mathlib/Algebra/Group/Defs.html#AddMonoid.nsmul_succ"
                m = re.match(r"^\./(.+)\.html#", e.get("docLink") or "")
                if m:
                    found[name] = (m.group(1).replace("/", "."), e.get("kind"))
            print(f"  decl module oracle (doc-gen4 {dg.name}): "
                  f"{len(found)}/{len(names)} unresolved decls", file=sys.stderr)
        except (OSError, ValueError) as exc:
            if explicit:
                raise
            print(f"NOTE: doc-gen4 oracle unreadable at {dg} ({exc}) — "
                  f"falling back to .ilean", file=sys.stderr)
    else:
        location = str(dg) if dg is not None else "the sealed input binding"
        print(f"NOTE: doc-gen4 oracle absent at {location} — module fallback is "
              f".ilean only (refresh: python3 .claude/skills/mathlib-search/"
              f"mathlib_search.py decl Nat.succ --live)", file=sys.stderr)

    missing = names - set(found)
    if explicit:
        candidates = tuple(sorted(ilean_files or ()))
    else:
        ilean_root = _mathlib_checkout() / ".lake/build/lib/lean"
        candidates = tuple(sorted(ilean_root.rglob("*.ilean"))) \
            if ilean_root.is_dir() else ()
    if missing and candidates:
        n_il = 0
        # sorted(): a name can appear in >1 module's .ilean (192 measured across
        # Mathlib — re-exports, `export`, stale build products), and first-wins
        # below. rglob order is filesystem-dependent, so without this the same
        # inputs could pick a different module on another machine — this build
        # is contractually deterministic.
        for p in candidates:
            if not missing:
                break
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                if explicit:
                    raise
                continue
            module, decls = d.get("module"), d.get("decls")
            if not module or not isinstance(decls, dict):
                continue
            for name in missing & set(decls):
                found[name] = (module, None)
                n_il += 1
            missing -= set(decls)
        if n_il:
            print(f"  decl module oracle (.ilean, checkout): +{n_il}",
                  file=sys.stderr)
    return found


# ---- Lean source scanning (decl `code` snippets) ----------------------------
# Lean identifier: unicode word chars (covers Greek `α` and subscripts — `E₂` is
# ONE name, and truncating it to `E` invents a name that collides with a real decl)
# plus ' ! ? and «guillemet» names.
_LEAN_IDENT = r"[\w'!?«»]+(?:\.[\w'!?«»]+)*"
_LEAN_KW = (r"(?:theorem|lemma|def|abbrev|structure|class|instance|inductive"
            r"|opaque|axiom)")
_LEAN_MOD = (r"(?:private|protected|public|noncomputable|nonrec|scoped|partial"
             r"|unsafe|local)")
_LEAN_NS = re.compile(rf"^\s*(namespace|section|end)(?:\s+({_LEAN_IDENT}))?\s*$")
_LEAN_DECL = re.compile(rf"^\s*(?:@\[[^\]]*\]\s*)*(?:{_LEAN_MOD}\s+)*{_LEAN_KW}\s+"
                        rf"(_root_\.)?({_LEAN_IDENT})")


def _strip_lean_comments(lines: list[str]):
    """Yield (index, line-with-comments-blanked). Lean's `/- -/` nests."""
    depth = 0
    for i, line in enumerate(lines):
        out: list[str] = []
        j = 0
        while j < len(line):
            two = line[j:j + 2]
            if depth == 0 and two == "--":
                break
            if two == "/-":
                depth += 1
                j += 2
                continue
            if two == "-/" and depth:
                depth -= 1
                j += 2
                continue
            if depth == 0:
                out.append(line[j])
            j += 1
        yield i, "".join(out)


def _lean_decl_lines(lines: list[str]) -> dict[str, int]:
    """`fully-qualified decl name -> line index that declares it`, for one file.

    The decl panel's `code` used to be found by matching the decl's BARE last
    segment (`d.split(".")[-1]`) with an unconstrained namespace prefix, taking
    the first hit. That silently attaches a DIFFERENT declaration's statement:
    measured, 199 decls carried the wrong code — `SimpleGraph.Adj` (a structure
    FIELD) showed `theorem Adj.symm`, `GCDMonoid.gcd` showed
    `protected theorem Associated.gcd`. It ships to readers and agents stamped
    with the mathlib license, asserted as that decl's source, so it is the same
    class of error as a wrong `module`: a wrong fact is worse than no fact.

    So resolve the enclosing `namespace` stack and require the declared name to
    equal the decl EXACTLY (honouring `_root_.`). Fail closed: no exact match ⇒
    no snippet. Elaborator output (structure/class fields, `to_additive` twins)
    is never textually declared, so it correctly yields nothing rather than the
    nearest textual lookalike.

    Validated against Lean's own `.ilean` index over 600 modules: 97% of the
    names this returns are confirmed verbatim by the compiler's index (the
    remainder are `private` decls, whose real names are mangled, and modules whose
    .ilean predates the source). Coverage went UP (6,409 regex hits -> 6,417 exact),
    because the old pattern also missed `public` decls.
    """
    out: dict[str, int] = {}
    stack: list[tuple[str, str | None]] = []
    for i, code in _strip_lean_comments(lines):
        if not code.strip():
            continue
        m = _LEAN_NS.match(code)
        if m:
            kind, name = m.group(1), m.group(2)
            if kind == "namespace" and name:
                stack.append(("ns", name))
            elif kind == "section":
                stack.append(("sec", name))          # sections don't name decls
            elif kind == "end":
                if name:
                    for j in range(len(stack) - 1, -1, -1):
                        if stack[j][1] == name:
                            del stack[j:]
                            break
                elif stack:
                    stack.pop()
            continue
        d = _LEAN_DECL.match(code)
        if not d:
            continue
        root, name = d.group(1), d.group(2).rstrip(".")
        if not name:
            continue
        prefix = ".".join(n for k, n in stack if k == "ns" and n)
        fq = name if root else (f"{prefix}.{name}" if prefix else name)
        out.setdefault(fq, i)
    return out


def _lit_id(arxiv_id: str, ref: str) -> str:
    return f"lit:{arxiv_id}#{ref}" if ref else f"lit:{arxiv_id}"


def _edge(src: str, dst: str, kind: str, source: str, method: str, pin: str,
          confidence: str, evidence: dict) -> dict:
    return {"src": src, "dst": dst, "kind": kind,
            "provenance": {"source": source, "method": method, "pin": pin},
            "confidence": confidence, "evidence": evidence}


_MARKUP = re.compile(r"<[^>]+>")


def _strip_markup(text: str) -> str:
    """Plain-text a title/snippet from an external wiki: drop HTML tags,
    unescape entities, collapse whitespace. Inline $TeX$ passes through."""
    out = html.unescape(_MARKUP.sub(" ", text or ""))
    return re.sub(r"\s+", " ", out).strip()


def _prune(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


# ---- frontier Lean repos: shared minting layer ------------------------------
# One parameterized layer mints every git-harvested Lean repo (the FC corpus,
# TauCeti, user-registered repos) as a first-class library: its own
# path:<Lib>/* container tree, one decl:<Lib>:* node per declaration
# (docstring + code stored per the repo's license, named in the registry
# entry), decl→xref:erdos/oeis edges from verbatim reference URLs, and
# deterministic docstring-citation joins (Wikipedia URLs resolved through the
# universe slug map). `formalizes` is only ever minted under the FC-specific
# gates (research category + single-reference Wikipedia/ file); every other
# repo's citations enter as `mentions` — a formalization claim needs review,
# a citation is a fact.

def _read_frontier_jsonl(path: Path) -> tuple[dict, list[dict]]:
    """(first-line _meta, decl rows) of a brain/ingest Lean-repo harvest file
    (formal_conjectures.py / lean_repo.py); rows missing decl/module/file are
    dropped — they can be neither minted nor placed."""
    meta: dict = {}
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if "_meta" in r:
                meta = r["_meta"]
                continue
            if r.get("decl") and r.get("module") and r.get("file"):
                rows.append(r)
    return meta, rows


def _frontier_stats() -> dict:
    return {"decls": 0, "containers": 0, "xref_erdos": 0, "xref_oeis": 0,
            "formalizes_det": 0, "mentions_det": 0, "agent_links": 0,
            "skipped_unknown_qid": 0, "agent_rows_skipped": 0,
            "duplicate_decls": 0, "invocation_det": 0,
            "invocation_overflow_decls": 0}


# Maximal runs of Lean-identifier characters + dots. Python's \w covers the
# unicode identifier alphabet Mathlib actually uses (Greek, subscripts: α, Gδ,
# h₁ all stay inside one token), so `h₁.Basis` is ONE token and bare `Basis`
# can never be extracted from it — the word-boundary guard is structural.
_FQ_TOKEN = re.compile(r"[\w'!?.]+")


def _scan_fq_names(code: str, universe: dict[str, str],
                   ambiguous: set[str]) -> list[str]:
    """Fully-qualified Mathlib decl names present in a statement snippet.

    Discipline (mathlib decl-oracle memory, non-negotiable): match FQ names
    ONLY, at word boundaries — NEVER a bare suffix. Tokens are maximal
    identifier+dot runs, so `Module.Basis` matches inside `(Module.Basis R M)`
    but never inside `h.Basis` or `Foo.Module.Basis.mk` (those are single
    longer tokens). Dot-leading/trailing tokens (`.symm` dot-notation, `1..n`
    ranges) are namespace-relative or non-names and are skipped. A token that
    is also a dotted suffix of one of the repo's OWN decls (`ambiguous`) is
    skipped: under `open`, it may denote the repo-local decl, and a miss is
    always preferable to a wrong edge.
    """
    out: list[str] = []
    seen: set[str] = set()
    for tok in _FQ_TOKEN.findall(code or ""):
        if tok in seen:
            continue
        seen.add(tok)
        if tok.startswith(".") or tok.endswith(".") or ".." in tok:
            continue
        if tok in universe and tok not in ambiguous:
            out.append(tok)
    return out


def _frontier_repo_layer(*, lib: str, rows: list[dict], n_files, pin: str,
                         source: str, tree_method: str, ref_method: str,
                         containers: dict, decl_nodes: list, edges: list,
                         slug_lookup: dict, ensure_concept, stats: dict,
                         erdos_oeis: dict | None = None,
                         research_gate: bool = False,
                         wikipedia_formalizes: bool = False,
                         node_extra: dict | None = None,
                         agent_links: Path | None = None,
                         agent_links_pin: str | None = None,
                         mathlib_names: dict[str, str] | None = None,
                         ) -> tuple[dict[str, str], set[tuple[str, str]]]:
    """Mint one frontier repo's containers/decls/xrefs/citation joins into the
    shared node/edge accumulators; returns (bare FQ name -> node id,
    (qid, decl id) pairs already joined) for repo-specific follow-ups (the FC
    agent-join fold). `node_extra` rides on every minted node (user repos carry
    {"repo": "<owner>/<repo>"} — the registry's user_lean_repos entry covers
    the class, the node names the concrete repo). `agent_links` names the
    repo's fold-verified repo_link file (catalog/data/<key>_links.jsonl,
    written only by brain/fold_proposals.py); its rows mint decl↔concept
    `mentions` edges — NEVER anything stronger (moderation contract: this
    channel must not emit a kind build_cells fuses cells on; identity claims
    need human review — FC's gated formalizes path is FC-only and separate).
    `mathlib_names` (bare FQ name -> existing decl:Mathlib:* node id) turns on
    deterministic decl→decl `invocation` edges: each decl's statement snippet
    is scanned for full Mathlib FQ names (_scan_fq_names discipline) — also
    mentions-strength, never a merge kind, and only onto nodes that already
    exist (no new minting rule; a name-universe miss = no edge)."""
    root_id = f"path:{lib}"
    if root_id in containers:
        print(f"WARNING: frontier repo lib {lib!r} collides with an existing "
              f"container tree — layer skipped", file=sys.stderr)
        return {}, set()
    erdos_oeis = erdos_oeis or {}
    extra = node_extra or {}

    # container tree from module paths (dir grain — files are the decl's
    # module payload, not containers, matching the depth-capped hierarchy)
    dirs: Counter[str] = Counter()
    for r in rows:
        parts = r["module"].split(".")[:-1]        # drop the file stem
        for i in range(1, len(parts)):
            dirs["/".join(parts[:i + 1])] += 1
    containers[root_id] = _prune({
        "id": root_id, "type": "container", "label": lib,
        "library": lib, "library_kind": "math",
        "n_decls": len(rows), "n_files": n_files, **extra})
    for d in sorted(dirs):
        cid = f"path:{d}"
        parent_id = f"path:{d.rsplit('/', 1)[0]}" if "/" in d else root_id
        containers[cid] = _prune({"id": cid, "type": "container",
                                  "label": d.rsplit("/", 1)[-1], "library": lib,
                                  "library_kind": "math", "n_decls": dirs[d],
                                  **extra})
        edges.append(_edge(parent_id, cid, "contains", source, tree_method,
                           pin, "high", {"n_decls": dirs[d]}))
    stats["containers"] = 1 + len(dirs)

    # Repo-local ambiguity set for invocation matching: every proper dotted
    # suffix of the repo's own decl names. Inside `namespace TauCeti.Algebra`,
    # a snippet's `HopfAlgebra.antipode` could denote the repo's OWN
    # TauCeti.Algebra.HopfAlgebra.antipode rather than Mathlib's — skip.
    repo_suffixes: set[str] = set()
    if mathlib_names:
        for r in rows:
            parts = r["decl"].split(".")
            for i in range(1, len(parts)):
                repo_suffixes.add(".".join(parts[i:]))

    decl_ids: dict[str, str] = {}          # bare FQ name -> node id
    pair_seen: set[tuple[str, str]] = set()  # (qid, decl id) joins
    wiki_prefix = f"{lib}/Wikipedia/"
    for r in sorted(rows, key=lambda r: r["decl"]):
        if r["decl"] in decl_ids:
            stats["duplicate_decls"] += 1
            continue
        did = f"decl:{lib}:{r['decl']}"
        decl_ids[r["decl"]] = did
        decl_nodes.append(_prune({
            "id": did, "type": "decl", "label": r["decl"],
            "library": lib, "module": r["module"], "pin": pin,
            "decl_kind": r.get("kind"), "category": r.get("category"),
            "ams": r.get("ams"), "docstring": r.get("docstring"),
            "code": r.get("code"), **extra}))
        stats["decls"] += 1
        parts = r["module"].split(".")[:-1]
        cur = root_id
        for comp in parts[1:]:
            nxt = f"{cur}/{comp}"
            if nxt not in containers:
                break
            cur = nxt
        edges.append(_edge(cur, did, "contains", source,
                           "module-prefix placement", pin, "high",
                           {"module": r["module"]}))

        refs: dict[str, list[str]] = {}
        for src_key in ("refs", "file_refs"):
            for k, vals in (r.get(src_key) or {}).items():
                acc = refs.setdefault(k, [])
                acc.extend(v for v in vals if v not in acc)
        seen_oeis: set[str] = set()
        for n in refs.get("erdos", []):
            edges.append(_edge(did, f"xref:erdos:{n}", "xref", "erdos",
                               ref_method, pin,
                               "high", {"value": n,
                                        "url": f"https://www.erdosproblems.com/{n}"}))
            stats["xref_erdos"] += 1
            for a in erdos_oeis.get(n, []):
                if a not in seen_oeis:
                    seen_oeis.add(a)
                    edges.append(_edge(did, f"xref:oeis:{a}", "xref", "oeis",
                                       "erdosproblems.com join (problems.yaml)",
                                       pin, "high", {"value": a}))
                    stats["xref_oeis"] += 1
        for a in refs.get("oeis", []):
            if a not in seen_oeis:
                seen_oeis.add(a)
                edges.append(_edge(did, f"xref:oeis:{a}", "xref", "oeis",
                                   ref_method,
                                   pin, "high", {"value": a}))
                stats["xref_oeis"] += 1

        # ---- deterministic Mathlib invocation edges (every row, no gates) ---
        # decl:<Lib>:X -(invocation)-> decl:Mathlib:Y for each full Mathlib FQ
        # name Y in X's statement header. Deduped per (source, target) by
        # construction: rows mint once, _scan_fq_names dedupes tokens, and
        # name -> node id is injective.
        if mathlib_names:
            names = _scan_fq_names(r.get("code") or "", mathlib_names,
                                   repo_suffixes)
            if len(names) > 50:
                # a statement header naming >50 distinct Mathlib decls is a
                # parsing bug, not mathematics — fail closed, loudly
                stats["invocation_overflow_decls"] += 1
                print(f"WARNING: {did} matched {len(names)} Mathlib FQ names "
                      f"in one statement header — invocation edges for this "
                      f"decl skipped (parser sanity cap, investigate)",
                      file=sys.stderr)
            else:
                for name in names:
                    edges.append(_edge(did, mathlib_names[name], "invocation",
                                       source, "fq-name-in-statement", pin,
                                       "medium", {"name": name}))
                    stats["invocation_det"] += 1

        if research_gate and not (r.get("category") or "").startswith("research"):
            continue
        file_wiki = (r.get("file_refs") or {}).get("wikipedia") or []
        is_wiki_single = (wikipedia_formalizes
                          and r["file"].startswith(wiki_prefix)
                          and len(set(file_wiki)) == 1)
        for slug in refs.get("wikipedia", []):
            qid = slug_lookup.get(slug) or slug_lookup.get(
                slug.replace("–", "-"))
            if not qid or not ensure_concept(qid):
                stats["skipped_unknown_qid"] += 1
                continue
            if (qid, did) in pair_seen:
                continue
            pair_seen.add((qid, did))
            url = f"https://en.wikipedia.org/wiki/{slug}"
            if is_wiki_single and slug in file_wiki:
                # the file is this article's conjecture; its research
                # statements formally state it (Apache-2.0 source cites
                # the article verbatim). Single-reference files only —
                # a two-article header must never weld two concepts.
                edges.append(_edge(qid, did, "formalizes",
                                   source,
                                   "wikipedia-reference (module docstring)",
                                   pin, "medium",
                                   {"match_kind": "exact", "url": url,
                                    "verified_by": "verbatim reference URL"}))
                stats["formalizes_det"] += 1
            else:
                edges.append(_edge(qid, did, "mentions",
                                   source,
                                   "wikipedia-citation (docstring)",
                                   pin, "high",
                                   {"role": "citation", "url": url}))
                stats["mentions_det"] += 1

    # ---- agent joins (fold-verified repo_link rows) — mentions ONLY ---------
    # Provenance ai: source = the repo's registry key, method marks the agent
    # channel. Any row whose kind is not exactly "mentions" is skipped here
    # even though fold_proposals already enforces it (defense in depth — a
    # hand-edited links file gets the same gate).
    if agent_links is not None and (agent_links_pin is not None or agent_links.exists()):
        pin_al = agent_links_pin or datetime.fromtimestamp(
            agent_links.stat().st_mtime, tz=timezone.utc
        ).date().isoformat()
        with agent_links.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if "_meta" in rec:
                    continue
                did = decl_ids.get(rec.get("decl") or "")
                qid = rec.get("qid")
                if (rec.get("kind") != "mentions" or not did
                        or not qid or not ensure_concept(qid)):
                    stats["agent_rows_skipped"] += 1
                    continue
                if (qid, did) in pair_seen:
                    continue
                pair_seen.add((qid, did))
                ev = dict(rec.get("evidence") or {})
                ev.setdefault("role", "citation")
                edges.append(_edge(qid, did, "mentions", source,
                                   "agent-join (fold-verified)", pin_al,
                                   rec.get("confidence") or "medium", ev))
                stats["agent_links"] += 1
    return decl_ids, pair_seen


# ---- SCHEMA.md v2: external DB pages → ext nodes / links edges --------------


def _read_external_jsonl(path: Path) -> tuple[dict, list[dict], dict]:
    """Read rows plus optional legacy metadata from one external artifact."""
    meta: dict | None = None
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        before = os.fstat(fh.fileno())
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path.name}:{lineno}: invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(record, dict):
                raise ValueError(
                    f"{path.name}:{lineno}: JSONL row must be an object"
                )
            if "_meta" in record:
                if meta is not None or rows or set(record) != {"_meta"} \
                        or not isinstance(record["_meta"], dict):
                    raise ValueError(
                        f"{path.name}:{lineno}: _meta must be the first and only "
                        "metadata row"
                    )
                meta = record["_meta"]
                continue
            rows.append(record)
        after = os.fstat(fh.fileno())
    identity = lambda item: (item.st_dev, item.st_ino, item.st_size,
                             item.st_mtime_ns, item.st_ctime_ns)
    if identity(before) != identity(after):
        raise ValueError(f"{path.name}: file changed while it was being read")
    snapshot = {
        "bytes": before.st_size,
        "mtime": datetime.fromtimestamp(before.st_mtime, tz=timezone.utc)
        .isoformat(timespec="seconds"),
        "mtime_epoch": before.st_mtime,
    }
    return meta or {}, rows, snapshot


def _read_external_pair(
    db: str,
    pages_path: Path,
    links_path: Path | None,
) -> tuple[dict, list[dict], dict, dict | None, list[dict], dict | None]:
    """Read a complete pair, rejecting mixed or corrupted sealed generations."""
    pages_meta, pages, pages_snapshot = _read_external_jsonl(pages_path)
    if links_path is not None and links_path.exists():
        links_meta, links, links_snapshot = _read_external_jsonl(links_path)
    else:
        links_meta, links, links_snapshot = None, [], None

    validate_external_pair(db, pages_meta, pages, links_meta, links)
    return (pages_meta, pages, pages_snapshot,
            links_meta, links, links_snapshot)


def _load_external_transaction(path: Path, db: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExternalPairError(f"{db}: unreadable external-pair journal") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != EXTERNAL_PAIR_TRANSACTION_SCHEMA
        or payload.get("db") != db
        or not isinstance(payload.get("old_pages"), bool)
        or not isinstance(payload.get("old_links"), bool)
    ):
        raise ExternalPairError(f"{db}: invalid external-pair journal")
    return payload


def _read_stable_external_pair(
    directory: Path,
    db: str,
    pages_path: Path,
    links_path: Path,
) -> tuple[dict, list[dict], dict, dict | None, list[dict], dict | None]:
    """Read under a shared writer lock, falling back after an interrupted commit."""
    controls = external_pair_control_paths(directory, db)
    lock_path = controls["lock"]
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        if not controls["journal"].exists():
            return _read_external_pair(db, pages_path, links_path)
        payload = _load_external_transaction(controls["journal"], db)
        try:
            return _read_external_pair(db, pages_path, links_path)
        except (OSError, ValueError):
            if not payload["old_pages"]:
                raise ExternalPairError(
                    f"{db}: interrupted first publication has no prior generation"
                )
            return _read_external_pair(
                db,
                controls["pages_backup"],
                controls["links_backup"] if payload["old_links"] else None,
            )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _first_external_meta(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            record = json.loads(line)
            if isinstance(record, dict) and isinstance(record.get("_meta"), dict):
                return record["_meta"]
            return {}
    return {}

def external_dir() -> Path:
    """catalog/data/external unless BRAIN_EXTERNAL_DIR overrides (tests)."""
    return Path(os.environ.get("BRAIN_EXTERNAL_DIR", str(DATA / "external")))


def ext_node_cap() -> int:
    return int(os.environ.get("BRAIN_EXT_NODE_CAP", "8000"))


def load_crossref_registry(path: Path | None = None) -> dict[str, dict]:
    """source_registry.json crossref_sources — ext `db` values MUST be keys here."""
    return json.loads((path or REGISTRY).read_text(encoding="utf-8")).get(
        "crossref_sources", {}
    )


def load_external(
    ext_dir: Path | None,
    registry: dict[str, dict],
    *,
    page_files: tuple[BoundInputFile, ...] | None = None,
    link_files: tuple[BoundInputFile, ...] = (),
    anchor_path: Path | None = None,
    anchor_pin: str | None = None,
) -> dict[str, dict]:
    """Read brain/ingest output: <db>_pages.jsonl (+ optional <db>_links.jsonl).

    Returns {db: {"pages": [...], "links": [...], "pin", "path_metadata"}}
    for every db whose files exist AND whose key is in the crossref registry.
    Missing dir / no files → {} — the whole v2 layer degrades to a no-op.
    """
    out: dict[str, dict] = {}
    explicit = page_files is not None
    if not explicit and (ext_dir is None or not ext_dir.is_dir()):
        return out
    page_candidates = (
        tuple(sorted(page_files or (), key=lambda item: item.logical_path))
        if explicit
        else tuple(ext_dir.glob("*_pages.jsonl"))
    )
    links_by_name = {item.path.name: item for item in link_files}
    page_names = {
        (item.path if explicit else item).name
        for item in page_candidates
    }
    orphan_links = (
        tuple(item.path for item in link_files)
        if explicit
        else tuple(ext_dir.glob("*_links.jsonl"))
    )
    if not explicit:
        for journal in ext_dir.glob(".wikilean-pair-*.transaction.json"):
            db = external_pair_db_from_journal(journal)
            if db is None:
                raise ExternalPairError("external-pair journal has an invalid path")
            controls = external_pair_control_paths(ext_dir, db)
            if journal.resolve() != controls["journal"]:
                raise ExternalPairError(
                    f"{db}: external-pair journal path does not match its payload"
                )
            controls["lock"].parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                controls["lock"], os.O_RDWR | os.O_CREAT, 0o600
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH)
                if not journal.exists():
                    continue
                _load_external_transaction(journal, db)
                if not (ext_dir / f"{db}_pages.jsonl").exists():
                    raise ExternalPairError(
                        f"{db}: interrupted publication has no visible pages file"
                    )
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
    for orphan in orphan_links:
        db = orphan.name[: -len("_links.jsonl")]
        if f"{db}_pages.jsonl" in page_names:
            continue
        if explicit:
            raise ExternalPairError(
                f"{db}: bound links input has no matching bound pages input"
            )
        else:
            controls = external_pair_control_paths(ext_dir, db)
            controls["lock"].parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(controls["lock"], os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH)
                if (ext_dir / f"{db}_pages.jsonl").exists():
                    continue
                meta = _first_external_meta(orphan)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
        if EXTERNAL_PAIR_META_FIELDS.intersection(meta):
            raise ExternalPairError(
                f"{db}: generated links file has no matching pages file"
            )
    for page_item in page_candidates:
        pp = page_item.path if explicit else page_item
        db = pp.name[: -len("_pages.jsonl")]
        if db not in registry:
            if explicit:
                raise ValueError(
                    f"{pp.name} is bound but source_registry has no "
                    f"crossref_sources key {db!r}"
                )
            print(f"WARNING: {pp.name} has no source_registry crossref_sources "
                  f"key '{db}' — file skipped", file=sys.stderr)
            continue
        link_item = links_by_name.get(f"{db}_links.jsonl") if explicit else None
        lp = (
            link_item.path
            if link_item is not None
            else (None if explicit else ext_dir / f"{db}_links.jsonl")
        )
        if explicit:
            pair = _read_external_pair(db, pp, lp)
        else:
            assert ext_dir is not None and lp is not None
            pair = _read_stable_external_pair(ext_dir, db, pp, lp)
        (pages_meta, page_rows, page_snapshot,
         links_meta, link_rows, link_snapshot) = pair
        pages: list[dict] = []
        for r in page_rows:
            # contract-violating legacy rows are never minted (fail-soft per
            # row); generated pairs have already passed the stricter envelope.
            if r.get("db") != db or not r.get("id") or not r.get("title") \
                    or not r.get("url"):
                continue
            # Reducer-owned fields: only the separately bound,
            # fold-verified anchor file may assign anchor provenance.
            r.pop("qid_source", None)
            r.pop("qid_confidence", None)
            r.pop("qid_pin", None)
            pages.append(r)
        links: list[dict] = []
        link_present = links_meta is not None
        for r in link_rows:
            if not r.get("src") or not r.get("dst") or r["src"] == r["dst"]:
                continue
            links.append(r)
        pin = (
            page_item.pin
            if explicit
            else pages_meta.get("pair_generation")
            or page_snapshot["mtime"][:10]
        )
        link_pin = (
            link_item.pin
            if link_item is not None
            else ((links_meta or {}).get("pair_generation") or pin)
        )
        path_metadata = {pp.name: page_snapshot}
        if link_present and lp is not None and link_snapshot is not None:
            path_metadata[lp.name] = link_snapshot
        out[db] = {"pages": pages, "links": links, "pin": pin,
                   "link_pin": link_pin,
                   "anchor_pin": anchor_pin,
                   "path_metadata": path_metadata}
    # Agent-verified anchors (fold_proposals action:"xref" output): stamp the
    # proposed qid onto pages that have none of their own — the ingest's CC0
    # Wikidata qid always wins over an agent-proposed anchor. Stamped pages
    # feed the same minting/xref/projection logic; qid_source marks the edge
    # provenance as propose-then-approve rather than CC0 fact.
    if not explicit:
        anchor_path = BRAIN_DATA / "ext_anchor_links.jsonl"
    if (anchor_path is not None
            and (explicit or anchor_path.exists())
            and out):
        by_db: dict[str, dict[str, dict]] = {
            db: {str(p["id"]): p for p in rec["pages"]} for db, rec in out.items()}
        n_stamped = 0
        with anchor_path.open(encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                if "_meta" in r:
                    continue
                page = by_db.get(r.get("db"), {}).get(str(r.get("id")))
                if page is not None and not page.get("qid"):
                    page["qid"] = r["qid"]
                    page["qid_source"] = "ext_anchor"
                    page["qid_confidence"] = r.get("confidence", "medium")
                    n_stamped += 1
        if n_stamped:
            print(f"  ext anchors: {n_stamped} agent-verified page qids stamped "
                  f"(brain/data/ext_anchor_links.jsonl)", file=sys.stderr)
    return out


def external_layer(ext_data: dict[str, dict], *, concept_qids: set[str],
                   xref_dsts: set[str], concept_anchor: dict[str, set],
                   xref_pairs: set[tuple[str, str]], registry: dict[str, dict],
                   cap: int) -> tuple[list[dict], list[dict], dict]:
    """Mint ext nodes + links edges + concept projections per SCHEMA v2.

    Minting policy: anchored pages (the page's `xref:<db>:<id>` is an xref dst
    of some graph node, or its CC0 qid is a graph concept) plus pages <=1
    link-hop from an anchored page, capped per db (anchored first, then the
    frontier by inbound-link count). Snippets are stored ONLY where the
    registry's ingest.snippets says the license permits — enforced here again
    regardless of what the ingest emitted. Returns (ext_nodes, new_edges,
    stats); ext node ids reproduce the historical xref edge dst string
    byte-for-byte so existing xref edges resolve to the new nodes.
    """
    ext_nodes: list[dict] = []
    new_edges: list[dict] = []
    stats = {"minted": {}, "capped": {}, "links_page": 0, "links_projected": 0,
             "xref_from_page_qid": 0, "anchors_outside_graph": 0}
    for db in sorted(ext_data):
        rec = ext_data[db]
        snippets_ok = bool((registry[db].get("ingest") or {}).get("snippets"))
        pin = rec["pin"]
        link_pin = rec.get("link_pin") or pin
        pages = {p["id"]: p for p in rec["pages"]}

        def eid(pid: str) -> str:
            return f"xref:{db}:{pid}"

        anchored = {pid for pid, p in pages.items()
                    if eid(pid) in xref_dsts or p.get("qid") in concept_qids}
        # fold-verified agent anchors pointing at QIDs the graph doesn't have
        # are legal (they extend the universe) but do nothing here — count
        # them so the drop is visible instead of silent
        stats["anchors_outside_graph"] += sum(
            1 for p in pages.values()
            if p.get("qid_source") == "ext_anchor"
            and p.get("qid") not in concept_qids)
        inbound = Counter(l["dst"] for l in rec["links"])
        frontier: set[str] = set()
        for l in rec["links"]:
            s, d = l["src"], l["dst"]
            if s in anchored and d in pages and d not in anchored:
                frontier.add(d)
            if d in anchored and s in pages and s not in anchored:
                frontier.add(s)
        order = sorted(anchored) + sorted(frontier, key=lambda p: (-inbound[p], p))
        minted_set = set(order[:cap])
        stats["minted"][db] = len(minted_set)
        if len(order) > cap:
            stats["capped"][db] = len(order) - cap

        for pid in sorted(minted_set):
            p = pages[pid]
            # titles/snippets from external wikis can carry raw HTML (Kerodon
            # cite spans, nLab TOC chrome) — strip to plain text here so no
            # markup reaches labels.json or the panel (rendered escaped there:
            # not XSS, but garbled visible tags)
            node = {"id": eid(pid), "type": "ext", "db": db,
                    "label": _strip_markup(p["title"]), "url": p["url"]}
            if snippets_ok and p.get("snippet"):
                node["snippet"] = _strip_markup(p["snippet"])
                node["snippet_license"] = p.get("snippet_license")
            node["kind_hint"] = p.get("kind_hint")
            node["qid"] = p.get("qid")
            if p.get("qid_source"):
                # agent-proposed anchor (fold-verified) — must stay
                # distinguishable from a CC0 ingest qid on the node itself
                node["qid_source"] = p["qid_source"]
            ext_nodes.append(_prune(node))
            # a page whose CC0 qid is a graph concept gets the concept→ext
            # xref edge when no pipeline emitted one (join completeness)
            q = p.get("qid")
            if q in concept_qids and (q, eid(pid)) not in xref_pairs:
                stats["xref_from_page_qid"] += 1
                if p.get("qid_source") == "ext_anchor":
                    method = "sync-agents ext-anchor (fold-verified)"
                    conf = p.get("qid_confidence", "medium")
                    edge_pin = rec.get("anchor_pin") or pin
                else:
                    method, conf = "external-ingest page qid", "high"
                    edge_pin = pin
                new_edges.append(_edge(q, eid(pid), "xref", db, method, edge_pin,
                                       conf, {"value": pid}))

        # page-level links between MINTED nodes, deduped to the best context
        best: dict[tuple[str, str], str] = {}
        for l in rec["links"]:
            s, d = l["src"], l["dst"]
            if s in minted_set and d in minted_set:
                ctx = l.get("context") or "body"
                cur = best.get((s, d))
                if cur is None or CONTEXT_RANK.get(ctx, 9) < CONTEXT_RANK.get(cur, 9):
                    best[(s, d)] = ctx
        for (s, d), ctx in sorted(best.items()):
            new_edges.append(_edge(eid(s), eid(d), "links", db, "internal_link",
                                   link_pin, "high", {"context": ctx}))
        stats["links_page"] += len(best)

        # concept projection: page A → page B where both anchor to graph
        # concepts becomes concept→concept, deduped on (src, dst, via=db)
        def anchors(pid: str) -> list[str]:
            qs = set(concept_anchor.get(eid(pid), ()))
            p = pages.get(pid)
            if p and p.get("qid") in concept_qids:
                qs.add(p["qid"])
            return sorted(qs)

        seen_proj: set[tuple[str, str]] = set()
        for l in sorted(rec["links"], key=lambda l: (l["src"], l["dst"])):
            for qa in anchors(l["src"]):
                for qb in anchors(l["dst"]):
                    if qa == qb or (qa, qb) in seen_proj:
                        continue
                    seen_proj.add((qa, qb))
                    new_edges.append(_edge(qa, qb, "links", db,
                                           "internal_link (projected)", link_pin,
                                           "medium",
                                           {"projected": True, "via": db,
                                            "src_page": l["src"],
                                            "dst_page": l["dst"]}))
        stats["links_projected"] += len(seen_proj)
    return ext_nodes, new_edges, stats


def literature_layer(lit_title: dict[str, str], lic_open: dict[str, bool],
                     cit_path: Path | None, pin_stmt: str,
                     citation_pin: str | None = None,
                     ) -> tuple[list[dict], list[dict], dict]:
    """Mint paper-level literature nodes + containment + bibliography links
    (SCHEMA: `lit:<arxiv_id>` = paper, `lit:<arxiv_id>#<ref>` = statement).

    Papers: one node per distinct arXiv id over the statement ids in
    lit_title. An empty-ref TheoremGraph row already owns the paper id — that
    node IS the paper (same durable key), so nothing new is minted for it.
    `contains`: paper → each ref-bearing statement, mechanically derived from
    the id prefix (statements previously had no parent, so SCHEMA's strict
    single-parent containment holds).
    `links`: paper → paper rows from cit_path (arxiv_citations.jsonl —
    OpenAlex referenced_works, CC0; brain/ingest/openalex_citations.py),
    evidence.context="bibliography", re-filtered to endpoints whose paper
    exists here (defense in depth over the adapter's both-endpoints-ours
    guarantee — a bad row must not dangle). Missing file ⇒ ZERO links edges
    (the citation layer degrades to an exact no-op); papers + contains still
    mint — they derive from the statement layer alone.

    Returns (paper_nodes, edges, stats); deterministic, byte-stable.
    """
    paper_title: dict[str, str] = {}            # paper id -> label
    by_paper: dict[str, list[str]] = defaultdict(list)
    for lid in sorted(lit_title):
        pid = f"lit:{lid[4:].split('#', 1)[0]}"
        paper_title.setdefault(pid, lit_title[lid])
        if lid != pid:
            by_paper[pid].append(lid)
    paper_nodes = [_prune({
        "id": pid, "type": "literature",
        "label": paper_title[pid] or pid,
        "arxiv_id": pid[4:],
        "license_open": lic_open.get(pid[4:]),
    }) for pid in sorted(paper_title) if pid not in lit_title]
    edges: list[dict] = []
    for pid in sorted(by_paper):
        for lid in by_paper[pid]:
            edges.append(_edge(pid, lid, "contains", "theoremgraph",
                               "arxiv-id prefix (paper→statement)", pin_stmt,
                               "high", {"arxiv_id": pid[4:]}))
    n_contains = len(edges)
    n_links = n_dropped = 0
    if cit_path is not None and (citation_pin is not None or cit_path.exists()):
        pin_c = citation_pin or datetime.fromtimestamp(
            cit_path.stat().st_mtime, tz=timezone.utc
        ).date().isoformat()
        seen: set[tuple[str, str]] = set()
        with cit_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                if "_meta" in r:
                    continue
                s, d = f"lit:{r.get('src')}", f"lit:{r.get('dst')}"
                if (s not in paper_title or d not in paper_title or s == d
                        or (s, d) in seen):
                    n_dropped += 1
                    continue
                seen.add((s, d))
                edges.append(_edge(s, d, "links", "openalex",
                                   "referenced_works", pin_c, "high",
                                   {"context": "bibliography"}))
        n_links = len(seen)
    stats = {"papers": len(paper_title), "papers_new": len(paper_nodes),
             "contains": n_contains, "citations": n_links,
             "citation_rows_dropped": n_dropped}
    return paper_nodes, edges, stats


def assemble_units(nodes: list[dict], edges: list[dict],
                   descriptions: dict[str, str],
                   registry: dict[str, dict]) -> None:
    """Attach the SCHEMA v2 atomic-unit card to every concept node (mutates).

    decls/containers from formalizes edges; xrefs from xref edges (label from
    the minted ext node when present, url from the registry url_template);
    article from slug + article_annotations; description from
    wikidata_descriptions.json (universe description as fallback).
    """
    decl_mod = {n["id"]: n.get("module") for n in nodes if n["type"] == "decl"}
    ext_label = {n["id"]: n.get("label") for n in nodes if n["type"] == "ext"}
    ext_url = {n["id"]: n.get("url") for n in nodes if n["type"] == "ext"}
    fz_decls: dict[str, dict[str, dict]] = defaultdict(dict)
    fz_conts: dict[str, list[str]] = defaultdict(list)
    xrefs: dict[str, dict[str, dict[str, dict]]] = \
        defaultdict(lambda: defaultdict(dict))
    for e in edges:
        src = e["src"]
        if e["kind"] == "formalizes":
            dst = e["dst"]
            if dst.startswith("decl:"):
                bare = dst.split(":", 2)[2]
                entry = _prune({
                    "name": bare,
                    "module": e["evidence"].get("module") or decl_mod.get(dst),
                    "match_kind": e["evidence"].get("match_kind"),
                    "confidence": e["confidence"]})
                cur = fz_decls[src].get(bare)
                if cur is None or (not cur.get("match_kind")
                                   and entry.get("match_kind")):
                    fz_decls[src][bare] = entry
            elif dst.startswith("path:") and dst not in fz_conts[src]:
                fz_conts[src].append(dst)
        elif e["kind"] == "xref" and src.startswith("Q"):
            db = e["provenance"]["source"]
            val = str(e["evidence"].get("value") or e["dst"].split(":", 2)[2])
            tmpl = (registry.get(db) or {}).get("url_template") or ""
            # ids can carry spaces (nlab) — the template join must stay a
            # valid URL; prefer the minted ext node's adapter-encoded url
            url = ext_url.get(e["dst"]) or (
                tmpl.replace("{id}", urllib.parse.quote(val, safe="/:().,'-+~"))
                if tmpl else None)
            xrefs[src][db].setdefault(val, _prune({
                "id": val, "label": ext_label.get(e["dst"]), "url": url}))
    for n in nodes:
        if n["type"] != "concept":
            continue
        qid = n["id"]
        unit: dict = {"qid": qid, "label": n.get("label")}
        desc = descriptions.get(qid) or n.get("description")
        if desc:
            unit["description"] = desc
        if n.get("slug") and n.get("article_annotations"):
            unit["article"] = {"slug": n["slug"],
                               "annotations": n["article_annotations"]}
        unit["decls"] = [fz_decls[qid][k] for k in sorted(fz_decls.get(qid, {}))]
        unit["containers"] = sorted(fz_conts.get(qid, []))
        unit["xrefs"] = {db: [xrefs[qid][db][v] for v in sorted(xrefs[qid][db])]
                         for db in sorted(xrefs.get(qid, {}))}
        n["unit"] = unit


def apply_facets(nodes: list[dict], edges: list[dict],
                 tag_rows: list[dict]) -> None:
    """Set the SCHEMA v2 `f` facet bitmask on every node (mutates; omit at 0)."""
    tag_decls: dict[str, set[str]] = defaultdict(set)
    for r in tag_rows:
        tag_decls[r["db"]].add(r["decl"])
    xref_touch: set[str] = set()
    db_bits: dict[str, int] = defaultdict(int)
    lit: set[str] = set()
    # bits 0-2 PROPAGATE from a tagged decl to the concepts it formalizes —
    # otherwise the bits are decl-only while labels.json/filter enumerate
    # concepts, making the documented masks (f=1, f=17) unsatisfiable
    concept_tag_bits: dict[str, int] = defaultdict(int)
    tagged_all = tag_decls["wikidata"] | tag_decls["stacks"] | tag_decls["kerodon"]
    for e in edges:
        k = e["kind"]
        if k == "xref":
            xref_touch.add(e["src"])
            xref_touch.add(e["dst"])
            db_bits[e["src"]] |= F_DB_BIT.get(e["provenance"]["source"], 0)
        elif k in ("cites", "matches"):
            lit.add(e["src"])
        elif k == "formalizes" and e["dst"].startswith("decl:"):
            bare = e["dst"].split(":", 2)[2]
            if bare in tagged_all:
                bits = 0
                if bare in tag_decls["wikidata"]:
                    bits |= F_GOLD_WIKIDATA
                if bare in tag_decls["stacks"]:
                    bits |= F_STACKS_ATTR
                if bare in tag_decls["kerodon"]:
                    bits |= F_KERODON_ATTR
                concept_tag_bits[e["src"]] |= bits
    for n in nodes:
        f = 0
        t, nid = n["type"], n["id"]
        if t == "decl":
            bare = n["label"]
            if bare in tag_decls["wikidata"]:
                f |= F_GOLD_WIKIDATA
            if bare in tag_decls["stacks"]:
                f |= F_STACKS_ATTR
            if bare in tag_decls["kerodon"]:
                f |= F_KERODON_ATTR
        elif t == "concept":
            st = (n.get("display") or {}).get("status")
            if st == "formalized":
                f |= F_FORMALIZED
            elif st == "partial":
                f |= F_PARTIAL
            if n.get("article_annotations"):
                f |= F_ARTICLE
            f |= concept_tag_bits.get(nid, 0)
        elif t == "ext":
            f |= F_EXT | F_DB_BIT.get(n["db"], 0)
            if n.get("snippet"):
                f |= F_HAS_SNIPPET
        elif t == "literature" and "#" not in nid:
            # paper-level lit nodes (lit:<arxiv_id>) anchor the literature
            # facet natively; statement nodes stay bare
            f |= F_LITERATURE
        if nid in xref_touch:
            f |= F_ANY_XREF
        f |= db_bits.get(nid, 0)
        if nid in lit:
            f |= F_LITERATURE
        if f:
            n["f"] = f


def aggregate_facets(nodes: list[dict], edges: list[dict]) -> None:
    """Set `fa` (subtree-aggregate facet bits) on container nodes (mutates).

    A container "contains" a facet when any decl/sub-container in its contains
    subtree carries it, or when a concept whose dot renders inside it does
    (concepts attach via formalizes → decl-in-subtree or → the container
    itself). Without this, level views can't filter: containers carry no tag
    bits of their own, so a facet chip would dim every folder ("showing 0 of
    N" + a grey canvas — the 2026-07-10 bug report).
    """
    parent = {e["dst"]: e["src"] for e in edges if e["kind"] == "contains"}
    node_f = {n["id"]: n.get("f", 0) for n in nodes}
    fa: dict[str, int] = defaultdict(int)

    def up(start: str | None, bits: int) -> None:
        cur = start
        while cur is not None and bits:
            if fa[cur] & bits == bits:
                return  # ancestors already carry these bits
            fa[cur] |= bits
            cur = parent.get(cur)

    for n in nodes:
        f = n.get("f", 0)
        if f and n["type"] in ("decl", "container"):
            up(parent.get(n["id"]), f)
    for e in edges:
        if e["kind"] != "formalizes":
            continue
        f = node_f.get(e["src"], 0)
        if not f:
            continue
        dst = e["dst"]
        up(parent.get(dst) if dst.startswith("decl:") else dst, f)
    for n in nodes:
        if n["type"] == "container" and fa.get(n["id"]):
            n["fa"] = fa[n["id"]]


def _build(
    *,
    source_set: ContextBuildInputs | None = None,
    local_hf_metadata: Mapping[str, dict[str, object]] | None = None,
) -> tuple[list[dict], list[dict], dict]:
    """Returns (nodes, edges, meta) — both lists fully sorted, byte-deterministic."""
    if source_set is None:
        inputs = INPUTS
        optional_inputs: dict[str, Path | None] = dict(OPTIONAL_INPUTS)

        def source_pin(name: str, _input_id: str) -> str:
            if local_hf_metadata is not None and name in local_hf_metadata:
                metadata = local_hf_metadata[name]
                return (
                    f"hf:{metadata['dataset']}@{metadata['revision']}"
                    f"#sha256:{metadata['sha256']}"
                )
            return _pin(name)

        annotations = tuple(sorted((ROOT / "site" / "annotations").glob("[!.]*.json"))) \
            if (ROOT / "site" / "annotations").is_dir() else ()
        user_repo_files = tuple(sorted(USER_REPOS_DIR.glob("*.jsonl"))) \
            if USER_REPOS_DIR.is_dir() else ()
        declaration_oracle = None
        ilean_files = None
        mathlib_root = _mathlib_checkout()
        mathlib_source_files: dict[str, Path] | None = None
        ext_root = external_dir()
        external_pages = None
        external_links: tuple[BoundInputFile, ...] = ()
        anchor_bound: BoundInputFile | None = None
        external_anchor = None
        external_cap = ext_node_cap()
        citation_file: BoundInputFile | None = None
        context_generation = None
    else:
        inputs = {
            name: source_set.require_one(input_id).path
            for name, input_id in _REQUIRED_INPUT_KEYS.items()
        }
        optional_bound = {
            name: source_set.optional_one(input_id)
            for name, input_id in _OPTIONAL_INPUT_KEYS.items()
        }
        optional_inputs = {
            name: item.path if item is not None else None
            for name, item in optional_bound.items()
        }

        def source_pin(_name: str, input_id: str) -> str:
            binding = source_set.bindings[input_id]
            if binding.requirement == "required":
                return source_set.require_one(input_id).pin
            item = source_set.optional_one(input_id)
            if item is None:
                raise ValueError(f"cannot derive a pin for absent input {input_id!r}")
            return item.pin

        annotations = tuple(item.path for item in source_set.members("annotations"))
        user_repo_bound = source_set.members("user-repos")
        user_repo_files = tuple(item.path for item in user_repo_bound)
        declaration_bound = source_set.optional_one("declaration-oracle")
        declaration_oracle = declaration_bound.path if declaration_bound else None
        ilean_files = tuple(item.path for item in source_set.members("mathlib-ilean-tree"))
        mathlib_root = None
        mathlib_source_files = {
            item.logical_path: item.path
            for item in source_set.members("mathlib-source-tree")
        }
        external_pages = source_set.members("external-pages")
        external_links = source_set.members("external-links")
        ext_root = None
        anchor_bound = source_set.optional_one("brain-ext-anchor-links")
        external_anchor = anchor_bound.path if anchor_bound else None
        external_cap = source_set.external_node_cap
        citation_file = source_set.optional_one("external-arxiv-citations")
        context_generation = source_set.generation_id

    def available(path: Path | None) -> bool:
        return path is not None and (source_set is not None or path.exists())

    graph = json.loads(inputs["concept_graph_v2.json"].read_text(encoding="utf-8"))
    grounding = json.loads(inputs["rebuild_grounding.json"].read_text(encoding="utf-8"))
    hierarchy = json.loads(inputs["hierarchy.json"].read_text(encoding="utf-8"))
    roles = json.loads(inputs["decl_qid_roles_v2.json"].read_text(encoding="utf-8"))
    links_doc = json.loads(inputs["theoremgraph_links.json"].read_text(encoding="utf-8"))
    links, links_meta = links_doc["links"], links_doc["_meta"]
    if source_set is None and local_hf_metadata is not None:
        statement_metadata = local_hf_metadata["statement_formal.csv"]
        hierarchy_meta = hierarchy.get("meta") or {}
        if (
            hierarchy_meta.get("source_revision")
            != statement_metadata["revision"]
            or hierarchy_meta.get("source_sha256")
            != statement_metadata["sha256"]
        ):
            raise SystemExit(
                "hierarchy.json is not derived from the reviewed math-graph "
                "pin; rerun python3 catalog/build_hierarchy.py"
            )
        theorem_metadata = local_hf_metadata["theorem_matching.csv"]
        if (
            links_meta.get("source_revision")
            != theorem_metadata["revision"]
            or links_meta.get("source_sha256")
            != theorem_metadata["sha256"]
        ):
            raise SystemExit(
                "theoremgraph_links.json is not derived from the reviewed "
                "theorem-matching pin; rerun "
                "python3 catalog/ingest_theorem_graph.py"
            )

    qids = {n["qid"] for n in graph["nodes"]}

    # ---- decl universe + module/library resolution -------------------------
    # id = decl:<Library>:<FQ name>; the library must be fixed before ANY edge
    # is emitted, so resolution runs over every source first.
    fdecl_qids: dict[str, list[str]] = defaultdict(list)   # formalization role
    mod_votes: dict[str, Counter] = defaultdict(Counter)
    lib_votes: dict[str, Counter] = defaultdict(Counter)
    for n in graph["nodes"]:
        for f in n.get("formalizations") or []:
            fdecl_qids[f["decl"]].append(n["qid"])
            if f.get("module"):
                mod_votes[f["decl"]][f["module"]] += 1
            if f.get("library"):
                lib_votes[f["decl"]][f["library"]] += 1
    fdecls = set(fdecl_qids)
    mention_pairs = sorted((q, d) for d, m in roles.items()
                           for q, r in m.items() if r == "citation")
    ldecls = {l["decl"] for ls in links.values() for l in ls}

    # per-annotation evidence from the article corpus (site/annotations/*.json —
    # the D1 cache): each mentions edge carries how many annotations cite the
    # decl (statuses, labels, deep-linkable ids); each annotated concept gets an
    # article_annotations summary. Articles whose concept is NOT in the graph
    # fall back to the universe slug→QID map and are MINTED as concepts — every
    # annotated article must reach the brain. Fail-soft: no corpus, bare edges.
    ann_ev: dict[tuple[str, str], dict] = {}
    ann_summary: dict[str, dict] = {}
    ann_new_concepts: set[str] = set()
    ann_extra_pairs: list[tuple[str, str]] = []
    slug2qid_local = {n.get("slug"): n["qid"] for n in graph["nodes"] if n.get("slug")}
    uni_slug2qid: dict[str, str] = {}
    for name in ("wikidata_universe.jsonl", "universe_extension.jsonl"):
        if source_set is not None or inputs[name].exists():
            with inputs[name].open(encoding="utf-8") as fh:
                for line in fh:
                    r = json.loads(line)
                    sl = r.get("enwiki_slug")
                    if sl and r.get("qid"):
                        uni_slug2qid.setdefault(sl, r["qid"])
                        # WikiLean slugs hyphenate en-dashes (Curry–Howard)
                        uni_slug2qid.setdefault(sl.replace("\u2013", "-")
                                                .replace("–", "-"), r["qid"])
    if annotations:
        for f in annotations:
            if f.name.endswith(".agent1.json"):
                continue
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                if source_set is not None:
                    raise
                continue
            slug = doc.get("slug")
            anns = doc.get("annotations") or []
            if not slug or not anns:
                continue
            qid = slug2qid_local.get(slug)
            if not qid:
                qid = uni_slug2qid.get(slug)
                if not qid:
                    print(f"WARNING: annotated article {slug} has no QID in the "
                          f"universe — invisible to the brain", file=sys.stderr)
                    continue
                ann_new_concepts.add(qid)
            summ = {"total": len(anns), "formalized": 0, "partial": 0,
                    "not_formalized": 0}
            for a in anns:
                st = a.get("status")
                if st in summ:
                    summ[st] += 1
                dec = (a.get("mathlib") or {}).get("decl")
                if not dec:
                    continue
                if qid in ann_new_concepts:
                    ann_extra_pairs.append((qid, dec))
                ev = ann_ev.setdefault((qid, dec), {
                    "role": "citation", "n_annotations": 0, "statuses": {},
                    "sample": []})
                ev["n_annotations"] += 1
                ev["statuses"][st] = ev["statuses"].get(st, 0) + 1
                if len(ev["sample"]) < 3:
                    ev["sample"].append({"id": a.get("id"),
                                         "label": (a.get("label") or "")[:80],
                                         "status": st})
            ann_summary[qid] = summ
        if ann_new_concepts:
            print(f"  annotated articles outside the graph, minted as concepts: "
                  f"{len(ann_new_concepts)} (+{len(ann_extra_pairs)} mention pairs)",
                  file=sys.stderr)
        mention_pairs = sorted(set(mention_pairs) | set(ann_extra_pairs))
    else:
        print("NOTE: site/annotations missing — mentions edges stay bare",
              file=sys.stderr)
    # @[stacks]/@[kerodon]/@[wikidata] attributes harvested from the mathlib4
    # checkout — loaded before the decl universe is fixed so every gold
    # @[wikidata]-tagged decl becomes a brain node even when no agent pipeline
    # found it independently (27/121 were otherwise absent, per the harvest
    # verifier). Fail-soft: without the harvest the build just loses this layer.
    tag_rows: list[dict] = []
    p = optional_inputs["mathlib_tag_xrefs.jsonl"]
    if available(p):
        with p.open(encoding="utf-8") as fh:
            tag_rows = [r for line in fh if line.strip()
                        for r in [json.loads(line)] if "decl" in r]
    else:
        print("NOTE: catalog/data/mathlib_tag_xrefs.jsonl missing — "
              "@[stacks]/@[kerodon] xref edges + @[wikidata] source-tag "
              "provenance skipped", file=sys.stderr)
    source_tagged = {(r["tag"], r["decl"]) for r in tag_rows
                     if r["db"] == "wikidata"}
    # module from the tag row's source file (Mathlib/…/Foo.lean → Mathlib.….Foo)
    # — the ONLY module signal for tagged decls absent from the TheoremGraph
    # corpus; without it they resolve to no module and land at the library
    # ROOT (36 @[stacks]/@[kerodon] decls surfaced as loose children of
    # path:Mathlib, Jack's bug report 2026-07-12).
    tag_mod: dict[str, str] = {}
    for r in tag_rows:
        f, d = r.get("file"), r.get("decl")
        if f and d and f.endswith(".lean") and d not in tag_mod:
            tag_mod[d] = f[:-5].replace("/", ".")
    # @[stacks]/@[kerodon]-tagged decls join the universe too — without a node
    # the tag-xref edge can't mint, which left the whole Kerodon corpus
    # unanchored (its only join to the brain is these attributes).
    attr_tagged = {r["decl"] for r in tag_rows if r["db"] in ("stacks", "kerodon")}
    decl_set = (set(roles) | fdecls | ldecls | {d for _, d in source_tagged}
                | attr_tagged | {d for _, d in mention_pairs})
    # Annotation citations occasionally carry junk like
    # "MonoidAlgebra.instIsSemisimpleModule (Maschke)" — whitespace is never
    # legal in a Lean identifier, so such names can't resolve anywhere. Drop
    # them (and their mention pairs) rather than mint unreachable decl nodes.
    bad_names = {d for d in decl_set if any(c.isspace() for c in d)}
    if bad_names:
        print(f"WARNING: dropping {len(bad_names)} whitespace-bearing decl "
              f"name(s) from annotation citations: {sorted(bad_names)[:3]}",
              file=sys.stderr)
        decl_set -= bad_names
        mention_pairs = [(q, d) for q, d in mention_pairs if d not in bad_names]

    # grounding evidence text, joined by (qid, decl) — the immutable audit trail
    # (match_kind/status overrides are already applied inside concept_graph_v2).
    grounding_note = {(r["qid"], f["decl"]): f.get("evidence")
                      for r in grounding for f in r.get("formalizations") or []}

    # ---- one streaming pass over theorem_matching.csv ----------------------
    csv_mod: dict[str, str] = {}
    slogans: dict[str, str] = {}
    lic_open: dict[str, bool] = {}          # per paper (arxiv_id)
    lit_title: dict[str, str] = {}          # per lit id
    lit_sids: dict[str, dict] = {}          # TheoremGraph UUIDs = session keys only
    match_rows: list[dict] = []             # both-judges-affirmed, grounded decls
    with inputs["theorem_matching.csv"].open(
        encoding="utf-8", newline=""
    ) as fh:
        for row in csv.DictReader(fh):
            d = row["formal_decl"]
            if d in decl_set:
                if row["formal_module"] and d not in csv_mod:
                    csv_mod[d] = row["formal_module"]
            if row["arxiv_id"] and row["arxiv_id"] not in lic_open:
                lic_open[row["arxiv_id"]] = row["license_open"] == "True"
            if (row["gpt54_label"] in AFFIRM and row["deepseek_label"] in AFFIRM
                    and d in fdecls):
                lid = _lit_id(row["arxiv_id"], row["informal_ref"])
                lit_title.setdefault(lid, row["paper_title"])
                lit_sids.setdefault(lid, {"query_sid": row["query_sid"],
                                          "cand_sid": row["cand_sid"]})
                match_rows.append({
                    "decl": d, "lit": lid, "arxiv_id": row["arxiv_id"],
                    "ref": row["informal_ref"], "title": row["paper_title"],
                    "sim": float(row["sim"]), "gpt54": row["gpt54_label"],
                    "deepseek": row["deepseek_label"],
                })

    # statement_formal.csv: module backstop for decls the matching sample never
    # saw, plus kind + docstring (the snapshot's `body` column is empty, so the
    # code itself comes from the live checkout below)
    unresolved = {d for d in decl_set if d not in mod_votes and d not in csv_mod}
    sf_mod: dict[str, str] = {}
    decl_code: dict[str, dict] = {}
    sid2decl: dict[str, str] = {}
    with inputs["statement_formal.csv"].open(
        encoding="utf-8", newline=""
    ) as fh:
        for row in csv.DictReader(fh):
            d = row["decl_name"]
            if d in decl_set and row.get("statement_id"):
                sid2decl.setdefault(row["statement_id"], d)
            if d in unresolved and row["module"] and d not in sf_mod:
                sf_mod[d] = row["module"]
            if d in decl_set and d not in decl_code:
                rec = _prune({
                    "decl_kind": row.get("kind") or None,
                    "docstring": (row.get("docstring") or "")[:280] or None,
                })
                if rec:
                    decl_code[d] = rec

    # decl slogans from math-graph slogan.csv (CC-BY-4.0) — NOT from
    # theorem_matching.csv's formal_slogan: that dataset's license is
    # contested upstream (CC-BY-SA card vs CC-BY-NC-SA paper, BRAIN.md:452),
    # so it stays link-facts-only. Fail-soft: no file, no slogans.
    slogan_csv = (
        CACHE / "slogan.csv"
        if source_set is None
        else (
            source_set.optional_one("slogan").path
            if source_set.optional_one("slogan") is not None
            else None
        )
    )
    if available(slogan_csv):
        with slogan_csv.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                d = sid2decl.get(row["statement_id"])
                if (d and d not in slogans and row.get("slogan")
                        and row.get("insufficient_context") != "True"):
                    slogans[d] = row["slogan"][:500]
        print(f"  slogans from slogan.csv (CC-BY-4.0): {len(slogans)}/{len(decl_set)} decls",
              file=sys.stderr)
    else:
        print(f"NOTE: {slogan_csv or 'sealed slogan input'} missing — decl slogans skipped "
              f"(catalog/fetch_math_graph.py)", file=sys.stderr)

    # ---- containers from hierarchy.json ------------------------------------
    lib_meta = hierarchy["libraries"]
    containers: dict[str, dict] = {}
    contains_edges: list[dict] = []
    pin_h = source_pin("hierarchy.json", "hierarchy")
    snapshot_pin = (
        hierarchy["meta"]["source_sha256"]
        if source_set is None
        else source_set.require_one("hierarchy").pin
    )
    tag_input = (
        source_set.optional_one("mathlib-tag-xrefs")
        if source_set is not None else None
    )
    tag_xref_pin = tag_input.pin if tag_input is not None else snapshot_pin

    def walk(lib: str, kind: str, name: str, node: dict, parent: str, inherited: bool):
        cid = f"{parent}/{name}"
        superseded = inherited or node.get("superseded", False)
        containers[cid] = _prune({
            "id": cid, "type": "container", "label": name, "library": lib,
            "library_kind": kind, "n_decls": node["n_decls"],
            "n_direct": node.get("n_direct"),
            "superseded": True if superseded else None,
            "superseded_note": node.get("superseded_note"),
        })
        contains_edges.append(_edge(parent, cid, "contains", "theoremgraph",
                                    "hierarchy.json file-tree", pin_h, "high",
                                    {"n_decls": node["n_decls"]}))
        for child, sub in node.get("sub", {}).items():
            walk(lib, kind, child, sub, cid, superseded)

    # library roots that ARE Wikidata items get their identity on the node
    # (rendered as a Wikidata chip on the container panel) — extend as more
    # libraries gain items
    LIBRARY_QIDS = {"Mathlib": "Q140128421"}
    for lib, L in lib_meta.items():
        root = f"path:{lib}"
        containers[root] = _prune({"id": root, "type": "container", "label": lib,
                                   "library": lib, "library_kind": L["kind"],
                                   "qid": LIBRARY_QIDS.get(lib),
                                   "n_decls": L["n_decls"], "n_files": L["n_files"]})
        for name, node in L["modules"].items():
            walk(lib, L["kind"], name, node, root, False)

    # ---- decl nodes + their containment placement --------------------------
    def _voted(d: str) -> str | None:
        return (_majority(mod_votes[d]) or csv_mod.get(d) or sf_mod.get(d)
                or tag_mod.get(d))

    # Verified renames: a cited name that no longer exists, mapped to the decl's
    # CURRENT fully-qualified name (catalog/data/decl_renames.jsonl — written only
    # after an agent read the declaration in the checkout AND an adversarial
    # verifier upheld it; a suffix-heuristic guess never lands here). The cited
    # organ keeps its identity; it just files where the current decl lives, and
    # the node carries `renamed_to` so the card can say so. Fail-soft: no file,
    # no renames.
    decl_renames: dict[str, str] = {}
    _renames = source_set.optional_one("decl-renames") if source_set is not None else None
    _renames_path = (
        DATA / "decl_renames.jsonl"
        if source_set is None
        else (_renames.path if _renames is not None else None)
    )
    if available(_renames_path):
        for line in _renames_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("cited") and row.get("current"):
                decl_renames[row["cited"]] = row["current"]

    # Last-resort module for decls no corpus source covers (annotation-only
    # citations). Built once, over just the names that still need it — plus the
    # rename TARGETS, whose module is what a renamed citation files under.
    _need_mod = {d for d in decl_set if not _voted(d)}
    _need_mod |= set(decl_renames.values())
    oracle_mod = _decl_module_oracle(
        _need_mod,
        declaration_oracle=declaration_oracle,
        ilean_files=ilean_files,
        explicit=source_set is not None,
    )

    def resolve(d: str) -> tuple[str, str | None]:
        module = _voted(d)
        lib = _majority(lib_votes[d])
        if not lib:
            root = module.split(".", 1)[0] if module else None
            lib = root if root in lib_meta else "Mathlib"
        if not module and d in decl_renames:
            cur = decl_renames[d]
            module = _voted(cur) or (oracle_mod.get(cur) or (None, None))[0]
            if module and module.split(".", 1)[0] != lib:
                module = None   # same-library rule, as below
        if not module:
            cand = (oracle_mod.get(d) or (None, None))[0]
            # Accept the oracle ONLY inside the decl's own library. A
            # cross-library hit is real but useless here (`And.left` really does
            # live in `Init.Prelude`, `Counterexample.*` in `Counterexamples.*`):
            # this hierarchy has no container tree for those libraries, so
            # placement would fail at `path:Init` and drop the decl out of the
            # graph entirely — an orphan is worse than a cell at the root.
            # Keeping lib untouched also keeps `decl:<lib>:<name>` ids stable.
            if cand and cand.split(".", 1)[0] == lib:
                module = cand
        return lib, module

    # ---- Lean source snippets from the live checkout ------------------------
    # The snapshot CSV ships no statement bodies, so the decl panel's code
    # comes from the live mathlib4 checkout (Apache-2.0, attribution in _meta;
    # read-only; fail-soft on drift — a renamed file just means no snippet).
    mathlib_src = mathlib_root
    by_file: dict[str, list[str]] = defaultdict(list)
    for d in decl_set:
        lib, module = resolve(d)
        if lib == "Mathlib" and module:
            by_file[module].append(d)
    n_snippets = 0
    if mathlib_source_files is not None or (
        mathlib_src is not None and mathlib_src.exists()
    ):
        for module, decls in by_file.items():
            logical_source = module.replace(".", "/") + ".lean"
            fp = (
                mathlib_source_files.get(logical_source)
                if mathlib_source_files is not None
                else mathlib_src / logical_source
            )
            if fp is None:
                continue
            try:
                lines = fp.read_text(encoding="utf-8").splitlines()
            except OSError:
                if source_set is not None:
                    raise
                continue
            declared = _lean_decl_lines(lines)   # FQ name -> line, exact
            for d in decls:
                # a renamed citation's file holds the CURRENT name, not the cited
                # one — show the current decl's code (the node's renamed_to says
                # whose it is; never attribute it to the dead name silently)
                i = declared.get(d)
                if i is None and d in decl_renames:
                    i = declared.get(decl_renames[d])
                if i is None:
                    continue                     # fail closed — see _lean_decl_lines
                snip: list[str] = []
                for l in lines[i:i + 12]:
                    s = l.rstrip()
                    if snip and not s:
                        break                # blank line = statement header over
                    snip.append(l)
                    if (s.endswith(":=") or s.endswith(":= by") or s.endswith(" by")
                            or s.endswith("where") or s.endswith(":= fun")):
                        break
                decl_code.setdefault(d, {})["code"] = "\n".join(snip)[:700]
                n_snippets += 1
    else:
        print(f"WARNING: mathlib checkout missing at {mathlib_src} — decl code "
              f"snippets skipped (BRAIN_MATHLIB_CHECKOUT to override)", file=sys.stderr)
    print(f"  decl code snippets from the checkout: {n_snippets}/{len(decl_set)}",
          file=sys.stderr)

    decl_id: dict[str, str] = {}
    decl_nodes: list[dict] = []
    n_unplaced = 0
    # oracle outcome, counted the same way resolve() decides it
    n_oracle_module = len([d for d in _need_mod if resolve(d)[1]])
    n_oracle_cross_lib = len([d for d in _need_mod
                              if d in oracle_mod and not resolve(d)[1]])
    print(f"  decl module oracle accepted (same-library): {n_oracle_module}"
          f" | cross-library, declined: {n_oracle_cross_lib}"
          f" | still unresolved: {len(_need_mod) - n_oracle_module}",
          file=sys.stderr)

    n_oracle_kind = 0
    for d in sorted(decl_set):
        lib, module = resolve(d)
        did = f"decl:{lib}:{d}"
        decl_id[d] = did
        gloss = dict(decl_code.get(d, {}))
        # statement_formal.csv only carries kind for corpus decls; doc-gen4 knows
        # it for annotation-only ones too (it is a true fact about the decl even
        # where we declined its cross-library module above).
        if not gloss.get("decl_kind"):
            kind = (oracle_mod.get(d) or (None, None))[1]
            if kind:
                gloss["decl_kind"] = kind
                n_oracle_kind += 1
        decl_nodes.append(_prune({
            "id": did, "type": "decl", "label": d, "library": lib,
            "module": module, "slogan": slogans.get(d), "pin": snapshot_pin,
            # the cited name is HISTORY: the decl now goes by another name, and
            # the card must say so rather than presenting a dead name as current
            "renamed_to": decl_renames.get(d),
            **gloss,
        }))
        # placement: deepest hierarchy container prefixing the decl's module
        # (the tree is depth-capped, so this is the file container when the
        # file node exists and the nearest enclosing dir otherwise)
        parts = module.split(".") if module else [lib]
        cur = f"path:{parts[0]}"
        if cur not in containers:
            n_unplaced += 1
            continue
        for comp in parts[1:]:
            if f"{cur}/{comp}" not in containers:
                break
            cur = f"{cur}/{comp}"
        contains_edges.append(_edge(cur, did, "contains", "theoremgraph",
                                    "module-prefix placement", pin_h, "high",
                                    _prune({"module": module})))

    # ---- mathlib source cross-reference tags --------------------------------
    n_source_tagged = 0
    emitted_formalizes: set[tuple[str, str]] = set()   # (qid, bare decl name)

    # ---- ontology edges -----------------------------------------------------
    edges: list[dict] = list(contains_edges)
    pin_g = source_pin("concept_graph_v2.json", "concept-graph")

    for n in graph["nodes"]:
        for f in n.get("formalizations") or []:
            gold = (n["qid"], f["decl"]) in source_tagged
            n_source_tagged += gold
            emitted_formalizes.add((n["qid"], f["decl"]))
            edges.append(_edge(n["qid"], decl_id[f["decl"]], "formalizes",
                               "mathlib",
                               "@[wikidata] attribute (mathlib4 source)" if gold
                               else "agent+oracle",
                               tag_input.pin if gold and tag_input is not None else pin_g,
                               f.get("confidence") or "medium",
                               _prune({"match_kind": f.get("match_kind"),
                                       "module": f.get("module"),
                                       "source_tagged": True if gold else None,
                                       "grounding_note": grounding_note.get(
                                           (n["qid"], f["decl"])),
                                       "verified_by": "build_graph_v2 oracle+checkout"})))

    pin_r = source_pin("decl_qid_roles_v2.json", "decl-qid-roles")
    for q, d in mention_pairs:
        edges.append(_edge(q, decl_id[d], "mentions", "annotations",
                           "annotation-citation (decl_qid_roles_v2)", pin_r,
                           "high", ann_ev.get((q, d)) or {"role": "citation"}))

    for e in graph["edges"]:
        if e.get("source") != "mathlib":
            continue
        w = e.get("weight", 0)
        conf = "high" if w >= 5 else "medium" if w >= 2 else "low"
        edges.append(_edge(e["from"], e["to"], "depends", "mathlib_deps",
                           "lift_formal_edges (formal_dependency.csv)", pin_g, conf,
                           {"weight": w, "w_types": e.get("w_types"),
                            "witnesses": e.get("decls") or []}))

    pin_w = source_pin("wikidata_edges.jsonl", "wikidata-edges")
    rel_props: dict[tuple[str, str], list] = defaultdict(list)
    with inputs["wikidata_edges.jsonl"].open(encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r["s"] in qids and r["o"] in qids:
                rel_props[(r["s"], r["o"])].append({"p": r["p"], "label": r["p_label"]})
    for (s, o), props in sorted(rel_props.items()):
        props = sorted(props, key=lambda p: int(p["p"][1:]))
        edges.append(_edge(s, o, "relates", "wikidata_props", "wikidata-claims",
                           pin_w, "high", {"properties": props}))

    # One edge per (concept, source, page): the dst is the external PAGE id, so
    # two concepts sharing a MathWorld/nLab/LMFDB page become graph-discoverable
    # (the dst is an external identifier, not a node — see the P5d check below).
    pin_x = source_pin("wikidata_crossrefs.json", "wikidata-crossrefs")
    n_xref_skipped_keys = 0
    seen_xref_dst: set[tuple[str, str]] = set()
    for n in graph["nodes"]:
        for key, values in sorted((n.get("xrefs") or {}).items()):
            if key not in XREF_KEYS:
                n_xref_skipped_keys += 1
                continue
            for v in sorted(values):
                # DLMF P11497 values are often equation-granular ('1.2.E34',
                # '25.12#ii') but the ingest mints SECTION pages ('1.2') — key
                # the dst at section level so the edge lands on a real node;
                # the raw value stays in evidence. Dedup: several equation
                # values can normalize onto one section.
                dst_id = v
                if key == "dlmf":
                    m = re.match(r"^(\d+\.\d+)(?:[.#]|$)", v)
                    if m:
                        dst_id = m.group(1)
                dst = f"xref:{key}:{dst_id}"
                if (n["qid"], dst) in seen_xref_dst:
                    continue
                seen_xref_dst.add((n["qid"], dst))
                edges.append(_edge(n["qid"], dst, "xref", key,
                                   "wikidata-property", pin_x, "high",
                                   {"property": XREF_KEYS[key], "value": v}))

    # decl → Stacks/Kerodon tag xrefs, only for decls that are already brain
    # nodes (rows for untracked decls are counted, never minted into nodes)
    n_tag_xref = n_tag_skipped = 0
    seen_tag: set[tuple[str, str]] = set()
    for r in tag_rows:
        if r["db"] not in ("stacks", "kerodon"):
            continue
        if r["decl"] not in decl_id:
            n_tag_skipped += 1
            continue
        key = (decl_id[r["decl"]], f"xref:{r['db']}:{r['tag']}")
        if key in seen_tag:
            continue
        seen_tag.add(key)
        n_tag_xref += 1
        edges.append(_edge(key[0], key[1], "xref", r["db"],
                           f"@[{r['db']}] attribute (mathlib4 source)",
                           tag_xref_pin, "high",
                           {"tag": r["tag"], "value": r["tag"], "file": r["file"]}))
    if tag_rows:
        print(f"  mathlib tag xrefs: {n_tag_xref} stacks/kerodon edges "
              f"({n_tag_skipped} rows skipped — decl has no brain node); "
              f"{n_source_tagged} formalizes edges source-tagged @[wikidata]",
              file=sys.stderr)

    # ---- cites + matches (TheoremGraph links + transitive join) ------------
    pin_l = source_pin("theoremgraph_links.json", "theoremgraph-links")
    pin_m = source_pin("theorem_matching.csv", "theorem-matching")

    def judge_conf(g: str, d: str) -> str:
        return "high" if g == "exact" and d == "exact" else "medium"

    cites: dict[tuple[str, str], dict] = {}
    for q in sorted(links):
        for l in links[q]:
            lid = _lit_id(l["arxiv_id"], l["ref"])
            lit_title.setdefault(lid, l["title"])
            key = (q, lid)
            if key in cites:
                vd = cites[key]["evidence"]["via_decls"]
                if l["decl"] not in vd and len(vd) < 8:
                    vd.append(l["decl"])
                continue
            cites[key] = _edge(q, lid, "cites", "theoremgraph",
                               "theoremgraph_links", pin_l,
                               judge_conf(l["gpt54"], l["deepseek"]),
                               {"via_decls": [l["decl"]], "gpt54": l["gpt54"],
                                "deepseek": l["deepseek"], "sim": l["sim"],
                                "primary": l["primary"],
                                "license_open": lic_open.get(l["arxiv_id"])})
    n_cites_links = len(cites)
    match_rows.sort(key=lambda r: (r["decl"], r["lit"],
                                   -(r["gpt54"] == "exact" and r["deepseek"] == "exact"),
                                   -r["sim"]))
    matches: dict[tuple[str, str], dict] = {}
    for r in match_rows:
        mkey = (decl_id[r["decl"]], r["lit"])
        if mkey not in matches:
            matches[mkey] = _edge(mkey[0], r["lit"], "matches", "theoremgraph",
                                  "theorem_matching dual-judge", pin_m,
                                  judge_conf(r["gpt54"], r["deepseek"]),
                                  {"gpt54": r["gpt54"], "deepseek": r["deepseek"],
                                   "sim": r["sim"],
                                   "license_open": lic_open.get(r["arxiv_id"])})
        for q in sorted(set(fdecl_qids[r["decl"]])):  # transitive join, concept side
            key = (q, r["lit"])
            if key in cites:
                vd = cites[key]["evidence"]["via_decls"]
                if r["decl"] not in vd and len(vd) < 8:
                    vd.append(r["decl"])
                continue
            cites[key] = _edge(q, r["lit"], "cites", "theoremgraph",
                               "theorem_matching transitive-join", pin_m,
                               judge_conf(r["gpt54"], r["deepseek"]),
                               {"via_decls": [r["decl"]], "gpt54": r["gpt54"],
                                "deepseek": r["deepseek"], "sim": r["sim"],
                                "license_open": lic_open.get(r["arxiv_id"])})
    # links-file matches: every affirmed link row is also a decl→lit match
    for q in sorted(links):
        for l in links[q]:
            mkey = (decl_id[l["decl"]], _lit_id(l["arxiv_id"], l["ref"]))
            if mkey not in matches:
                matches[mkey] = _edge(mkey[0], mkey[1], "matches", "theoremgraph",
                                      "theoremgraph_links", pin_l,
                                      judge_conf(l["gpt54"], l["deepseek"]),
                                      {"gpt54": l["gpt54"], "deepseek": l["deepseek"],
                                       "sim": l["sim"],
                                       "license_open": lic_open.get(l["arxiv_id"])})
    edges.extend(cites[k] for k in sorted(cites))
    edges.extend(matches[k] for k in sorted(matches))

    # ---- fail-soft layers ---------------------------------------------------
    # Container links and discovery rows may introduce BRAND-NEW concepts (QIDs
    # outside the graph — fold_proposals fetched their labels/P31 into
    # universe_extension.jsonl) and brand-new decls (oracle/checkout-verified by
    # the fold). Create their nodes here so these layers genuinely GROW the
    # brain rather than only linking it.
    new_concepts: dict[str, dict] = {}
    new_decls: dict[str, dict] = {}
    universe_rec: dict[str, dict] = {}
    for name in ("wikidata_universe.jsonl", "universe_extension.jsonl"):
        if source_set is not None or inputs[name].exists():
            with inputs[name].open(encoding="utf-8") as fh:
                for line in fh:
                    r = json.loads(line)
                    universe_rec.setdefault(r["qid"], r)

    def ensure_concept(qid: str) -> bool:
        if qid in qids or qid in new_concepts:
            return True
        u = universe_rec.get(qid)
        if not u:
            return False
        new_concepts[qid] = _prune({
            "id": qid, "type": "concept", "label": u.get("label"),
            "slug": u.get("enwiki_slug"),
            "description": u.get("description"),
            "article_annotations": ann_summary.get(qid),
            "altitude_evidence": {"p31": u.get("classes") or [],
                                  "module_span": [], "match_kinds": []},
            "display": {"status": "partial"},
        })
        return True

    for _q in sorted(ann_new_concepts):   # every annotated article reaches the brain
        ensure_concept(_q)

    p = optional_inputs["container_links.jsonl"]
    if available(p):
        pin_c = source_pin("container_links.jsonl", "brain-container-links")
        n_container_skipped_review = 0
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("skeptic") != "accept":
                n_container_skipped_review += 1
                continue
            path = rec["path"].removeprefix("path:").replace(".", "/")
            cid = f"path:{path}"
            if not ensure_concept(rec["qid"]) or cid not in containers:
                print(f"WARNING: container_links row skipped (unknown qid/path): "
                      f"{rec.get('qid')} -> {rec.get('path')}", file=sys.stderr)
                continue
            edges.append(_edge(rec["qid"], cid, "formalizes", "mathlib",
                               "container_links", pin_c,
                               rec.get("confidence") or "medium",
                               {"match_kind": rec.get("match_kind", "field"),
                                "note": rec.get("evidence")}))
        if n_container_skipped_review:
            print(f"WARNING: skipped {n_container_skipped_review} container_links "
                  f"row(s) without skeptic=accept", file=sys.stderr)
    else:
        print("NOTE: brain/data/container_links.jsonl missing — "
              "concept→container formalizes layer skipped", file=sys.stderr)

    p = optional_inputs["discovery_proposals.jsonl"]
    if available(p):
        pin_d = source_pin("discovery_proposals.jsonl", "brain-discovery-proposals")
        known = set(decl_id.values()) | set(containers) | qids
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            # only verifier-passed rows fold; rejected/unverified rows stay put
            if rec.get("rejected_reason") or rec.get("verified") is not True:
                continue
            src, dst = rec.get("src"), rec.get("dst")
            if rec.get("kind") not in KIND_ORDER:
                print(f"WARNING: discovery proposal skipped (unknown kind): "
                      f"{src} -{rec.get('kind')}-> {dst}", file=sys.stderr)
                continue
            if src not in known and not ensure_concept(src):
                print(f"WARNING: discovery src QID {src} has no universe "
                      f"record — row skipped", file=sys.stderr)
                continue
            if dst.startswith("decl:"):
                # The fold hardcodes lib=Mathlib; if resolve() already placed
                # this decl under another library (TheoremGraph module vote),
                # remap onto the existing node instead of forking a duplicate.
                bare = dst.split(":", 2)[2]
                if dst not in known and bare in decl_id:
                    dst = decl_id[bare]
            if dst not in known and dst not in new_decls and dst.startswith("decl:"):
                lib, d = dst.split(":", 2)[1:]
                module = rec.get("module")
                new_decls[dst] = _prune({
                    "id": dst, "type": "decl", "label": d, "library": lib,
                    "module": module, "slogan": slogans.get(d),
                    "pin": snapshot_pin,
                    **decl_code.get(d, {}),
                })
                parts = module.split(".") if module else [lib]
                cur = f"path:{parts[0]}"
                if cur in containers:
                    for comp in parts[1:]:
                        if f"{cur}/{comp}" not in containers:
                            break
                        cur = f"{cur}/{comp}"
                    edges.append(_edge(cur, dst, "contains", "theoremgraph",
                                       "module-prefix placement", pin_h, "high",
                                       _prune({"module": module})))
            ev = rec.get("evidence") or {}
            mk = ev.get("match_kind")
            if src in new_concepts and mk:
                ae = new_concepts[src]["altitude_evidence"]
                if mk not in ae["match_kinds"]:
                    ae["match_kinds"].append(mk)
                span = "/".join((rec.get("module") or "").split(".")[:2])
                if span and span not in ae["module_span"]:
                    ae["module_span"].append(span)
            if rec["kind"] == "formalizes" and dst.startswith("decl:"):
                bare = dst.split(":", 2)[2]
                emitted_formalizes.add((src, bare))
                if (src, bare) in source_tagged:   # gold pair found by another path
                    ev = {**ev, "source_tagged": True}
            edges.append(_edge(src, dst, rec["kind"], "mathlib",
                               "discovery_proposals (verified)", pin_d,
                               rec.get("confidence") or "medium", ev))
    else:
        print("NOTE: brain/data/discovery_proposals.jsonl missing — "
              "discovery layer skipped", file=sys.stderr)

    # Gold @[wikidata] pairs no pipeline found independently get minted here —
    # a maintainer-reviewed source tag IS a formalizes edge, the strongest kind
    # we have. Their decls joined decl_set above, so the decl node exists; the
    # QID must at least be known to the universe (else counted, never guessed).
    n_gold_minted = n_gold_unknown_qid = 0
    for qid, d in sorted(source_tagged - emitted_formalizes):
        if d not in decl_id:
            continue   # whitespace-filtered or unresolvable name
        if not ensure_concept(qid):
            n_gold_unknown_qid += 1
            continue
        n_gold_minted += 1
        n_source_tagged += 1
        edges.append(_edge(qid, decl_id[d], "formalizes", "mathlib",
                           "@[wikidata] attribute (mathlib4 source)",
                           tag_input.pin if tag_input is not None else snapshot_pin,
                           "high",
                           {"match_kind": "exact", "source_tagged": True}))
    if source_tagged:
        print(f"  gold @[wikidata] pairs minted as new formalizes edges: "
              f"{n_gold_minted} ({n_gold_unknown_qid} skipped — QID not in the "
              f"universe)", file=sys.stderr)

    # ---- frontier Lean repos: decl:<Lib>:* organs ---------------------------
    # The parameterized _frontier_repo_layer mints every git-harvested Lean
    # repo. Call #1 is the unsolved-problems corpus (google-deepmind/
    # formal-conjectures, harvested by brain/ingest/formal_conjectures.py) with
    # its two extra join paths: the teorth/erdosproblems YAML join table and
    # AGENT joins fold-verified into brain/data/fc_links.jsonl (plus the
    # research-category + Wikipedia/-file formalizes gates). Call #2 is TauCeti
    # (brain/ingest/lean_repo.py tauceti); then every user-registered repo
    # under catalog/data/user_repos/. All run BEFORE the external layer so
    # xref:erdos/oeis dsts anchor those pages.
    slug_lookup = dict(uni_slug2qid)
    slug_lookup.update(slug2qid_local)
    frontier_stats: dict[str, dict] = {}

    # Mathlib FQ-name universe for the frontier repos' deterministic
    # `invocation` joins: bare FQ name -> decl:Mathlib:* node id, restricted to
    # names that denote a CURRENT Mathlib decl already minted in this build.
    # Excluded on purpose: renamed cited names ('Basis' is stale history and
    # must never be a target), module-less names (the stale/hallucinated
    # annotation-citation residue that files at the library root, plus
    # cross-library decls the oracle declined), and dotless names that are
    # also the trailing segment of a dotted one ('Basis' vs 'Module.Basis' —
    # under `open` a bare token is ambiguous). Referenced-but-unminted decls
    # get NO edge — there is no referenced-decl minting path in this layer and
    # we do not invent one; a universe miss means a missing edge, never a
    # wrong one.
    inv_trailing: set[str] = set()
    for _d, _did in decl_id.items():
        if _did.startswith("decl:Mathlib:") and "." in _d:
            inv_trailing.add(_d.rsplit(".", 1)[1])
    mathlib_fq_ids: dict[str, str] = {}
    for _d, _did in decl_id.items():
        if (_did.startswith("decl:Mathlib:") and _d not in decl_renames
                and resolve(_d)[1]
                and ("." in _d or _d not in inv_trailing)):
            mathlib_fq_ids[_d] = _did
    for _did, _nd in new_decls.items():
        _d = _nd.get("label") or ""
        if (_did.startswith("decl:Mathlib:") and _nd.get("module") and _d
                and _d not in decl_renames
                and ("." in _d or _d not in inv_trailing)):
            mathlib_fq_ids.setdefault(_d, _did)
    print(f"  invocation name universe: {len(mathlib_fq_ids)} current "
          f"decl:Mathlib:* FQ names", file=sys.stderr)

    fc_stats = _frontier_stats()
    p = optional_inputs["formal_conjectures.jsonl"]
    if available(p):
        fc_meta, fc_rows = _read_frontier_jsonl(p)
        pin_fc = (
            source_pin("formal_conjectures.jsonl", "formal-conjectures")
            if source_set is not None
            else (
                fc_meta.get("commit")
                or source_pin("formal_conjectures.jsonl", "formal-conjectures")
            )[:12]
        )

        erdos_oeis: dict[str, list[str]] = {}
        ej = optional_inputs["erdos_joins.jsonl"]
        if available(ej):
            with ej.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    if "_meta" in r or not r.get("erdos"):
                        continue
                    erdos_oeis[str(r["erdos"])] = [str(a) for a in r.get("oeis") or []]

        fc_decl_ids, fc_pair_seen = _frontier_repo_layer(
            lib="FormalConjectures", rows=fc_rows,
            n_files=fc_meta.get("n_files"), pin=pin_fc,
            source="formal_conjectures",
            tree_method="file-tree (formal_conjectures.jsonl)",
            ref_method="formal-conjectures reference URL",
            containers=containers, decl_nodes=decl_nodes, edges=edges,
            slug_lookup=slug_lookup, ensure_concept=ensure_concept,
            stats=fc_stats, erdos_oeis=erdos_oeis,
            research_gate=True, wikipedia_formalizes=True,
            mathlib_names=mathlib_fq_ids)

        # agent joins, fold-verified (brain/fold_proposals.py fc_link rows)
        p = optional_inputs["fc_links.jsonl"]
        if available(p):
            pin_fcl = source_pin("fc_links.jsonl", "brain-fc-links")
            with p.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if "_meta" in rec:
                        continue
                    did = fc_decl_ids.get(rec.get("decl") or "")
                    kind = rec.get("kind")
                    qid = rec.get("qid")
                    if (not did or kind not in ("formalizes", "mentions")
                            or not qid or not ensure_concept(qid)):
                        fc_stats["agent_rows_skipped"] += 1
                        continue
                    if (qid, did) in fc_pair_seen:
                        continue
                    fc_pair_seen.add((qid, did))
                    ev = dict(rec.get("evidence") or {})
                    if kind == "formalizes":
                        ev.setdefault("match_kind", rec.get("match_kind") or "exact")
                    else:
                        ev.setdefault("role", "citation")
                    edges.append(_edge(qid, did, kind, "formal_conjectures",
                                       "fc-agent (fold-verified)", pin_fcl,
                                       rec.get("confidence") or "medium", ev))
                    fc_stats["agent_links"] += 1
        else:
            print("NOTE: brain/data/fc_links.jsonl missing — agent "
                  "formal-conjectures joins skipped", file=sys.stderr)
        print(f"  formal-conjectures layer: {fc_stats['decls']} decls in "
              f"{fc_stats['containers']} containers; xrefs erdos="
              f"{fc_stats['xref_erdos']} oeis={fc_stats['xref_oeis']}; "
              f"deterministic joins formalizes={fc_stats['formalizes_det']} "
              f"mentions={fc_stats['mentions_det']} "
              f"invocation={fc_stats['invocation_det']} "
              f"({fc_stats['invocation_overflow_decls']} overflow-skipped); "
              f"agent joins "
              f"{fc_stats['agent_links']} ({fc_stats['agent_rows_skipped']} "
              f"rows skipped, {fc_stats['skipped_unknown_qid']} unknown QIDs)",
              file=sys.stderr)
    else:
        print("NOTE: catalog/data/formal_conjectures.jsonl missing — "
              "formal-conjectures layer skipped "
              "(brain/ingest/formal_conjectures.py)", file=sys.stderr)

    # ---- TauCeti (call #2 of the frontier layer) ----------------------------
    p = optional_inputs["tauceti.jsonl"]
    if available(p):
        tc_meta, tc_rows = _read_frontier_jsonl(p)
        tc_stats = _frontier_stats()
        tauceti_links = optional_inputs["tauceti_links.jsonl"]
        _frontier_repo_layer(
            lib="TauCeti", rows=tc_rows, n_files=tc_meta.get("n_files"),
            pin=(
                source_pin("tauceti.jsonl", "tauceti")
                if source_set is not None
                else (
                    tc_meta.get("commit")
                    or source_pin("tauceti.jsonl", "tauceti")
                )[:12]
            ),
            source="tauceti",
            tree_method="file-tree (tauceti.jsonl)",
            ref_method="tauceti reference URL",
            containers=containers, decl_nodes=decl_nodes, edges=edges,
            slug_lookup=slug_lookup, ensure_concept=ensure_concept,
            stats=tc_stats,
            agent_links=tauceti_links,
            agent_links_pin=(
                source_pin("tauceti_links.jsonl", "tauceti-links")
                if source_set is not None and tauceti_links is not None else None
            ),
            mathlib_names=mathlib_fq_ids)
        frontier_stats["TauCeti"] = tc_stats
        print(f"  tauceti layer: {tc_stats['decls']} decls in "
              f"{tc_stats['containers']} containers; deterministic joins "
              f"mentions={tc_stats['mentions_det']} "
              f"invocation={tc_stats['invocation_det']} "
              f"({tc_stats['skipped_unknown_qid']} unknown QIDs, "
              f"{tc_stats['invocation_overflow_decls']} overflow-skipped); "
              f"agent joins "
              f"mentions={tc_stats['agent_links']} "
              f"({tc_stats['agent_rows_skipped']} rows skipped)",
              file=sys.stderr)
    else:
        print("NOTE: catalog/data/tauceti.jsonl missing — tauceti layer "
              "skipped (brain/ingest/lean_repo.py tauceti)", file=sys.stderr)

    # ---- user-registered Lean repos (frontier layer loop) -------------------
    # One harvest file per enabled repo (brain/ingest/lean_repo.py
    # --user-repos); ONE provenance source (user_lean_repos) covers the class,
    # each minted node's `repo` names the concrete <owner>/<repo>. _meta
    # repo/lib are re-validated here (the /api/repos pinned contract) so a
    # hand-dropped file gets the same gates as a harvested one.
    if len(user_repo_files) > USER_REPO_BUILD_CAP:
        print(f"WARNING: {len(user_repo_files)} user-repo harvests exceed the "
              f"{USER_REPO_BUILD_CAP}-repo cap — minting the first "
              f"{USER_REPO_BUILD_CAP} only", file=sys.stderr)
        user_repo_files = user_repo_files[:USER_REPO_BUILD_CAP]
    n_user_repos_skipped = 0
    for up in user_repo_files:
        u_meta, u_rows = _read_frontier_jsonl(up)
        owner_repo = str(u_meta.get("repo") or "")
        u_lib = str(u_meta.get("lib") or "")
        owner, _, repo = owner_repo.partition("/")
        if (not _REPO_NAME_RE.match(owner) or owner.startswith(".")
                or repo in (".", "..") or not _REPO_NAME_RE.match(repo)
                or not _LEAN_LIB_RE.match(u_lib)):
            n_user_repos_skipped += 1
            print(f"WARNING: user_repos/{up.name} skipped — bad _meta "
                  f"repo/lib ({owner_repo!r}, {u_lib!r})", file=sys.stderr)
            continue
        if len(u_rows) > USER_REPO_DECL_CAP:
            n_user_repos_skipped += 1
            print(f"WARNING: user_repos/{up.name} skipped — {len(u_rows)} "
                  f"decls exceeds the {USER_REPO_DECL_CAP} per-repo cap",
                  file=sys.stderr)
            continue
        if f"path:{u_lib}" in containers:
            n_user_repos_skipped += 1
            print(f"WARNING: user_repos/{up.name} skipped — lib {u_lib!r} "
                  f"collides with an existing container tree", file=sys.stderr)
            continue
        if source_set is None:
            fallback_pin = datetime.fromtimestamp(
                up.stat().st_mtime, tz=timezone.utc
            ).date().isoformat()
            pin_u = (u_meta.get("commit") or fallback_pin)[:12]
        else:
            pin_u = next(item.pin for item in user_repo_bound if item.path == up)
        u_stats = _frontier_stats()
        _frontier_repo_layer(
            lib=u_lib, rows=u_rows, n_files=u_meta.get("n_files"), pin=pin_u,
            source="user_lean_repos",
            tree_method=f"file-tree (user_repos/{up.name})",
            ref_method=f"user-repo reference URL ({owner_repo})",
            containers=containers, decl_nodes=decl_nodes, edges=edges,
            slug_lookup=slug_lookup, ensure_concept=ensure_concept,
            stats=u_stats, node_extra={"repo": owner_repo},
            mathlib_names=mathlib_fq_ids)
        frontier_stats[u_lib] = u_stats
        print(f"  user repo {owner_repo} ({u_lib}): {u_stats['decls']} decls "
              f"in {u_stats['containers']} containers; "
              f"mentions={u_stats['mentions_det']} "
              f"invocation={u_stats['invocation_det']}", file=sys.stderr)
    if user_repo_files or n_user_repos_skipped:
        print(f"  user Lean repos: {len(user_repo_files)} harvest files, "
              f"{n_user_repos_skipped} skipped", file=sys.stderr)

    # ---- external DB pages → ext nodes + links edges (SCHEMA v2) ------------
    # Runs after every xref-emitting layer so anchoring sees the full dst set.
    # No catalog/data/external/ files → exact no-op (zero nodes, zero edges).
    registry_path = (
        REGISTRY
        if source_set is None
        else source_set.require_one("source-registry").path
    )
    registry = load_crossref_registry(registry_path)
    ext_data = load_external(
        ext_root,
        registry,
        page_files=external_pages,
        link_files=external_links,
        anchor_path=external_anchor,
        anchor_pin=anchor_bound.pin if anchor_bound is not None else None,
    )
    all_qids = qids | set(new_concepts)
    xref_dsts: set[str] = set()
    concept_anchor: dict[str, set] = defaultdict(set)
    xref_pairs: set[tuple[str, str]] = set()
    for e in edges:
        if e["kind"] == "xref":
            xref_dsts.add(e["dst"])
            xref_pairs.add((e["src"], e["dst"]))
            if e["src"].startswith("Q"):
                concept_anchor[e["dst"]].add(e["src"])
    ext_nodes, ext_edges, ext_stats = external_layer(
        ext_data, concept_qids=all_qids, xref_dsts=xref_dsts,
        concept_anchor=concept_anchor, xref_pairs=xref_pairs,
        registry=registry, cap=external_cap)
    edges.extend(ext_edges)
    if ext_data:
        print(f"  external layer: {sum(ext_stats['minted'].values())} ext nodes "
              f"({', '.join(f'{db}={n}' for db, n in sorted(ext_stats['minted'].items()))}); "
              f"{ext_stats['links_page']} page links, "
              f"{ext_stats['links_projected']} projected, "
              f"{ext_stats['xref_from_page_qid']} page-qid xrefs"
              + (f"; {ext_stats['anchors_outside_graph']} agent anchors point "
                 f"at QIDs outside the concept graph (inert until the concept "
                 f"is minted)" if ext_stats.get('anchors_outside_graph') else ""),
              file=sys.stderr)
    else:
        print("NOTE: catalog/data/external/ empty — ext nodes / links edges "
              "skipped (brain/ingest adapters not run)", file=sys.stderr)

    # ---- literature papers: lit:<arxiv_id> + containment + bibliography -----
    cit_path = (
        ext_root / "arxiv_citations.jsonl"
        if source_set is None
        else (citation_file.path if citation_file is not None else None)
    )
    paper_nodes, lit_edges, lit_stats = literature_layer(
        lit_title,
        lic_open,
        cit_path,
        pin_l,
        citation_pin=citation_file.pin if citation_file is not None else None,
    )
    edges.extend(lit_edges)
    print(f"  literature papers: {lit_stats['papers']} "
          f"({lit_stats['papers_new']} minted — the rest double as empty-ref "
          f"statements), {lit_stats['contains']} contains, "
          f"{lit_stats['citations']} bibliography links "
          f"({lit_stats['citation_rows_dropped']} rows dropped)", file=sys.stderr)
    if not available(cit_path):
        print("NOTE: catalog/data/external/arxiv_citations.jsonl missing — "
              "paper→paper bibliography links skipped "
              "(brain/ingest/openalex_citations.py)", file=sys.stderr)

    # ---- concept nodes -------------------------------------------------------
    p31: dict[str, list[str]] = {}
    for name in ("wikidata_universe.jsonl", "universe_extension.jsonl"):
        with inputs[name].open(encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                merged = p31.setdefault(r["qid"], [])
                merged.extend(c for c in r.get("classes") or [] if c not in merged)

    concept_nodes = []
    for n in graph["nodes"]:
        span = sorted({"/".join((f.get("module") or "").split(".")[:2])
                       for f in n.get("formalizations") or [] if f.get("module")})
        concept_nodes.append(_prune({
            "id": n["qid"], "type": "concept", "label": n.get("label"),
            "slug": n.get("slug"),
            "article_annotations": ann_summary.get(n["qid"]),
            # Google KG is a hub id (never an xref edge — SCHEMA) but a useful
            # "Also in" chip; carried on the node payload instead
            "kgmid": ((n.get("xrefs") or {}).get("kgmid") or [None])[0],
            "altitude_evidence": {
                "p31": p31.get(n["qid"], []),
                "module_span": span,
                "match_kinds": sorted({f.get("match_kind")
                                       for f in n.get("formalizations") or []
                                       if f.get("match_kind")}),
                "msc": sorted((n.get("xrefs") or {}).get("msc", [])),
            },
            "display": _prune({"primary_decl": n.get("primary_decl"),
                               "status": n.get("status"),
                               "importance": n.get("importance")}),
        }))

    concept_nodes.extend(new_concepts[q] for q in sorted(new_concepts))
    decl_nodes.extend(new_decls[d] for d in sorted(new_decls))

    lit_nodes = [_prune({
        "id": lid, "type": "literature",
        "label": lit_title.get(lid) or lid,
        "arxiv_id": lid[4:].split("#", 1)[0],
        "ref": lid.split("#", 1)[1] if "#" in lid else "",
        "license_open": lic_open.get(lid[4:].split("#", 1)[0]),
        "session_keys": lit_sids.get(lid),
    }) for lid in sorted(lit_title)]
    # paper-level nodes interleave in id order (ids are disjoint from the
    # statement set by construction — literature_layer never re-mints an
    # empty-ref statement's id)
    lit_nodes = sorted(lit_nodes + paper_nodes, key=lambda n: n["id"])

    nodes = (sorted(concept_nodes, key=lambda n: int(n["id"][1:]))
             + [containers[k] for k in sorted(containers)]
             + decl_nodes + lit_nodes
             + sorted(ext_nodes, key=lambda n: n["id"]))

    # ---- v2 unit cards + facet bitmasks (need the complete node/edge sets) ---
    descriptions: dict[str, str] = {}
    p = optional_inputs["wikidata_descriptions.json"]
    if available(p):
        raw = json.loads(p.read_text(encoding="utf-8"))
        # ingest writes {_meta, descriptions:{qid: text}}; tolerate the old
        # flat {qid: text} shape too
        raw = raw.get("descriptions", raw) if isinstance(raw, dict) else {}
        descriptions = {k: v for k, v in raw.items()
                        if k.startswith("Q") and isinstance(v, str)}
    else:
        print("NOTE: catalog/data/wikidata_descriptions.json missing — "
              "unit.description falls back to universe descriptions",
              file=sys.stderr)
    assemble_units(nodes, edges, descriptions, registry)
    apply_facets(nodes, edges, tag_rows)
    aggregate_facets(nodes, edges)

    edges.sort(key=lambda e: (KIND_ORDER.index(e["kind"]), e["src"], e["dst"]))

    # every non-xref endpoint must be a real node (xref dst is the external DB)
    ids = {n["id"] for n in nodes}
    dangling = [e for e in edges
                if e["src"] not in ids or (e["kind"] != "xref" and e["dst"] not in ids)]
    if dangling:
        raise SystemExit(f"BUG: {len(dangling)} edges with dangling endpoints, "
                         f"first: {dangling[0]}")

    if source_set is None:
        present = {
            **INPUTS,
            **{k: v for k, v in OPTIONAL_INPUTS.items() if v.exists()},
        }
        external_input_metadata: dict[str, dict] = {}
        external_mtimes: list[float] = []
        for rec in ext_data.values():
            for name, snapshot in rec["path_metadata"].items():
                external_input_metadata[f"external/{name}"] = {
                    "mtime": snapshot["mtime"],
                    "bytes": snapshot["bytes"],
                }
                external_mtimes.append(snapshot["mtime_epoch"])
        if cit_path is not None and cit_path.exists():
            present[f"external/{cit_path.name}"] = cit_path
        for up in user_repo_files:
            present[f"user_repos/{up.name}"] = up
        newest = max(
            [v.stat().st_mtime for v in present.values()] + external_mtimes
        )
        generated_at = datetime.fromtimestamp(newest, tz=timezone.utc).isoformat(
            timespec="seconds"
        )
        input_metadata = {
            k: {
                "mtime": datetime.fromtimestamp(v.stat().st_mtime, tz=timezone.utc)
                .isoformat(timespec="seconds"),
                "bytes": v.stat().st_size,
            }
            for k, v in sorted(present.items())
        }
        input_metadata.update(external_input_metadata)
        if local_hf_metadata is not None:
            for name, metadata in local_hf_metadata.items():
                input_metadata[name] = {
                    "bytes": metadata["size"],
                    "sha256": metadata["sha256"],
                    "source_revision": metadata["revision"],
                    "source_url": metadata["file_url"],
                }
        input_metadata = dict(sorted(input_metadata.items()))
    else:
        generated_at = source_set.generation_id
        input_metadata = source_set.metadata()
    if source_set is None:
        code_license = (
            "decl `code` snippets are statement headers read from the live "
            "mathlib4 checkout — Apache-2.0 (mathlib4 contributors), render "
            "with source credit; `docstring`/`decl_kind` from TheoremGraph "
            "statement_formal.csv (CC-BY-4.0); for decls the corpus never "
            "saw, `decl_kind` + the last-resort `module` come from the "
            "doc-gen4 declaration index / the checkout's .ilean files "
            "(Apache-2.0, mathlib4 contributors — where a declaration "
            "lives, not its text)"
        )
    else:
        code_license = (
            "decl `code` snippets are statement headers read from sealed Mathlib "
            "source-tree inputs — Apache-2.0 (mathlib4 contributors), render with "
            "source credit; `docstring`/`decl_kind` from the sealed TheoremGraph "
            "statement_formal.csv input (CC-BY-4.0); for decls the corpus never "
            "saw, `decl_kind` + the last-resort `module` come from sealed "
            "declaration-oracle / Mathlib .ilean inputs (Apache-2.0, mathlib4 "
            "contributors — where a declaration lives, not its text)"
        )
    meta = {
        "schema": "brain/SCHEMA.md",
        "generated_at": generated_at,
        **({"generation_id": context_generation} if context_generation else {}),
        "inputs": input_metadata,
        "licenses": {
            "brain": "CC0-1.0 (WikiLean's own node/edge data)",
            "theoremgraph": links_meta["attribution"],
            "slogans": "decl slogans are currently ABSENT by policy: "
                       "theorem_matching.csv's formal_slogan is license-contested "
                       "upstream (CC-BY-SA card vs CC-BY-NC-SA paper, BRAIN.md:452) "
                       "so it stays link-facts-only, and slogan.csv (CC-BY-4.0) "
                       "turned out to cover informal statements exclusively "
                       "(0/2.57M rows reference a formal id). Decl gloss = "
                       "docstring + code, both cleanly licensed.",
            "code": code_license,
            "arxiv": "arXiv statement text is never redistributed — ids/titles/labels only",
            "wikidata": "CC0-1.0",
            "mathlib_tags": "@[stacks]/@[kerodon]/@[wikidata] cross-reference tags "
                            "harvested from the mathlib4 source (Apache-2.0, mathlib4 "
                            "contributors) — human-reviewed gold links",
            "formal_conjectures": "decl:FormalConjectures:* docstrings/code are "
                                  "Apache-2.0 (The Formal Conjectures Authors, "
                                  "google-deepmind/formal-conjectures), stored "
                                  "with attribution; erdos pages/joins derive "
                                  "from teorth/erdosproblems problems.yaml "
                                  "(Apache-2.0) — erdosproblems.com prose is "
                                  "never stored",
            "tauceti": "decl:TauCeti:* docstrings/code are Apache-2.0 "
                       "(TauCeti contributors, TauCetiProject/TauCeti), "
                       "stored with attribution",
            "user_lean_repos": "decl nodes from owner-registered public Lean "
                               "repos (GET /api/repos/enabled); docstrings/"
                               "code stored with per-node `repo` attribution — "
                               "each repo retains its own license (named in "
                               "the harvest _meta)",
            "external": "ext node ids/titles/urls/links are CC0 link facts; "
                        "stored snippets carry a per-node snippet_license and "
                        "exist ONLY for license-permitting sources "
                        "(source_registry ingest.snippets) — no-content sources "
                        "(mathworld/dlmf/eom/kerodon) ship ids+titles+links only",
        },
        "counts": {
            "nodes": dict(sorted(Counter(n["type"] for n in nodes).items())),
            "edges": {k: c for k, c in
                      sorted(Counter(e["kind"] for e in edges).items(),
                             key=lambda kv: KIND_ORDER.index(kv[0]))},
        },
        "notes": {
            "decls_without_module": len([d for d in decl_set
                                         if not resolve(d)[1]]),
            # of decls no corpus source covered, how many the decl-module oracle
            # rescued into their real folder (see _decl_module_oracle). The
            # remainder are names that do NOT exist in current mathlib — stale
            # renames + hallucinated citations, an annotation-quality problem
            # (manage/decl_existence_sweep.py), not a placement one. They stay
            # at the library root on purpose: no module is better than a wrong one.
            "decls_module_from_oracle": n_oracle_module,
            "decls_module_oracle_cross_library": n_oracle_cross_lib,
            "decl_kind_from_oracle": n_oracle_kind,
            "decls_unplaced": n_unplaced,
            "cites_from_links": n_cites_links,
            "cites_from_transitive_join": len(cites) - n_cites_links,
            "xref_values_skipped_nonschema_keys": n_xref_skipped_keys,
            "mathlib_tag_xref_edges": n_tag_xref,
            "mathlib_tag_rows_skipped_no_decl_node": n_tag_skipped,
            "formalizes_source_tagged": n_source_tagged,
            "ext_nodes_minted": dict(sorted(ext_stats["minted"].items())),
            "ext_nodes_capped": dict(sorted(ext_stats["capped"].items())),
            "links_page_edges": ext_stats["links_page"],
            "links_projected_edges": ext_stats["links_projected"],
            "xref_edges_from_page_qids": ext_stats["xref_from_page_qid"],
            "fc_decls": fc_stats["decls"],
            "fc_containers": fc_stats["containers"],
            "fc_xref_erdos": fc_stats["xref_erdos"],
            "fc_xref_oeis": fc_stats["xref_oeis"],
            "fc_formalizes_deterministic": fc_stats["formalizes_det"],
            "fc_mentions_deterministic": fc_stats["mentions_det"],
            "fc_agent_links": fc_stats["agent_links"],
            "fc_agent_rows_skipped": fc_stats["agent_rows_skipped"],
            "fc_invocation_deterministic": fc_stats["invocation_det"],
            "fc_invocation_overflow_decls": fc_stats["invocation_overflow_decls"],
            "fc_unknown_qids_skipped": fc_stats["skipped_unknown_qid"],
            "fc_duplicate_decls": fc_stats["duplicate_decls"],
            # generic frontier Lean repos (TauCeti + user_lean_repos), keyed by
            # lib — the FC corpus keeps its dedicated fc_* keys above
            "frontier_repos": {k: frontier_stats[k]
                               for k in sorted(frontier_stats)},
            "user_repo_files_skipped": n_user_repos_skipped,
            "lit_papers": lit_stats["papers"],
            "lit_paper_nodes_minted": lit_stats["papers_new"],
            "lit_contains_edges": lit_stats["contains"],
            "links_bibliography_edges": lit_stats["citations"],
            "lit_citation_rows_dropped": lit_stats["citation_rows_dropped"],
        },
    }
    return nodes, edges, meta


def build(
    *, source_set: ContextBuildInputs | None = None
) -> tuple[list[dict], list[dict], dict]:
    """Build from a sealed replay context or a locked, reviewed local cache."""
    if source_set is not None:
        return _build(source_set=source_set)
    # Keep local acquisition policy outside the sealed reducer's import
    # closure. Replay passes source_set and must remain runnable from only its
    # v2-declared code inventory.
    sys.path.insert(0, str(ROOT / "catalog"))
    from huggingface_download import (
        HuggingFaceArtifactError,
        verified_reviewed_dataset,
    )

    try:
        with ExitStack() as stack:
            _, mathgraph_metadata = stack.enter_context(
                verified_reviewed_dataset(
                    "uw-math-ai/math-graph",
                    {"statement_formal.csv": INPUTS["statement_formal.csv"]},
                    optional_files={"slogan.csv": CACHE / "slogan.csv"},
                )
            )
            _, theorem_metadata = stack.enter_context(
                verified_reviewed_dataset(
                    "uw-math-ai/theorem-matching",
                    {"theorem_matching.csv": INPUTS["theorem_matching.csv"]},
                )
            )
            local_hf_metadata = {
                "statement_formal.csv": mathgraph_metadata[
                    "statement_formal.csv"
                ],
                "theorem_matching.csv": theorem_metadata[
                    "theorem_matching.csv"
                ],
                **(
                    {"slogan.csv": mathgraph_metadata["slogan.csv"]}
                    if "slogan.csv" in mathgraph_metadata
                    else {}
                ),
            }
            return _build(
                source_set=None,
                local_hf_metadata=local_hf_metadata,
            )
    except HuggingFaceArtifactError as exc:
        raise SystemExit(f"FATAL: {exc}") from exc


def write_jsonl(out: Path, meta: dict, rows: list[dict]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"_meta": meta}, ensure_ascii=False,
                            separators=(",", ":")) + "\n")
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(out)


def write_edges(edges: list[dict], meta: dict,
                out: Path = EDGES_OUT, out_links: Path = EDGES_LINKS_OUT) -> dict:
    """Write the edge set split across two files.

    `out` gets every kind EXCEPT `links`, with the FULL build meta unchanged —
    byte-compatible with the historical single file minus its links rows
    (links sort last in KIND_ORDER, so the non-links rows are its exact
    prefix; _meta.counts still describes the whole edge set). `out_links`
    gets only kind=='links' rows under a small _meta of its own. Both writes
    are atomic; the links file is always (re)written — a zero-links build
    leaves an empty-row file rather than a stale one.
    """
    main_rows = [e for e in edges if e["kind"] != "links"]
    links_rows = [e for e in edges if e["kind"] == "links"]
    write_jsonl(out, meta, main_rows)
    notes = meta.get("notes") or {}
    links_meta = {
        "schema": meta.get("schema", "brain/SCHEMA.md"),
        "generated_at": meta.get("generated_at"),
        **({"generation_id": meta["generation_id"]}
           if meta.get("generation_id") else {}),
        **({"snapshot_id": meta["snapshot_id"]} if meta.get("snapshot_id") else {}),
        "split_from": out.name,
        "note": "kind=='links' rows split out of edges.jsonl (GitHub 100 MB "
                "per-file limit). Gitignored — rebuild deterministically with "
                "`python3 brain/build_edges.py` from the committed "
                "catalog/data/external/ inputs. Readers treat a missing file "
                "as empty; row schema is identical to edges.jsonl.",
        "counts": {"edges": {"links": len(links_rows)},
                   "page_level": notes.get("links_page_edges"),
                   "projected": notes.get("links_projected_edges"),
                   # paper→paper bibliography links (openalex); key absent
                   # on pre-literature-layer metas
                   **({"bibliography": notes["links_bibliography_edges"]}
                      if "links_bibliography_edges" in notes else {})},
    }
    write_jsonl(out_links, links_meta, links_rows)
    return {"main": len(main_rows), "links": len(links_rows)}
