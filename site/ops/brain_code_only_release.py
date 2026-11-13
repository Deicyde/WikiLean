#!/usr/bin/env python3
"""Decide, by code, whether a release may reuse the live Brain release unchanged.

A full promotion rebuilds the Brain from raw inputs and then promotes the Worker and
the public asset tree that were built at the same commit. When a release changes only
website code, that rebuild buys nothing: the frozen Brain release production already
serves is exactly the release the new Worker should serve. A **code-only release**
reuses that frozen release byte for byte and rebuilds everything else at the new
commit ``C``, through the same promoter, journal, canary and rollback.

The decision is mechanical, and it has two independent layers.

**Layer 1 — eligibility by path.** Every path that differs between the reused
release's authority commit ``A`` and ``C`` must be release-neutral
(:mod:`brain_release_authority`) or *website-only*: a path that feeds the Worker, the
non-Brain public baseline, or the build/test toolchain, and never the frozen release.
Website-only paths are listed here explicitly and deny-by-default applies, so a new
top-level file is never silently eligible. The reducer's code and curated inputs stay
blocking, and so does ``site/build_brain_page.py``: the ``/brain`` page is frozen into
the release as ``site/out/brain.html``, so a change to its builder cannot ship through
a path that reuses the release.

**Layer 2 — the Brain surface fence.** Layer 1 is a list, and a list can be wrong.
The fence turns "the Brain is unchanged" from a classification into a measurement: the
Brain-owned bytes the promoter is about to publish are fetched back from production
and required to be identical, path for path. The one byte-level exception is the
selector's ``audited_at`` stamp, which every promotion rewrites. Any change in the
staging code, the shard layout, the selector schema or the immutable namespace shows
up here as a diff or a 404 and refuses the promotion before Wrangler is invoked.

Nothing in this module mutates anything. ``evaluate`` makes read-only Git and HTTPS
calls and prints one canonical record; the promoter and the activation bundle freezer
re-derive every claim in that record rather than trusting it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))

import brain_release_authority as release_authority  # noqa: E402
from brain_http import (  # noqa: E402
    TransportError,
    fetch_bounded,
    require_https_base_url,
    trusted_urlopen,
)

SCHEMA = "wikilean.code-only-release/v1"
DOMAIN = "wikilean.code-only-release.v1"
POLICY = "wikilean.code-only-release-paths/v1"

RELEASE_ID_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

PRODUCTION_ORIGIN = "https://wikilean.jackmccarthy.org"
ATTESTATION_PATH = "wiki/public-asset-source-attestation.json"
DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024

# Paths that feed the frozen Brain release: the reducer, its curated inputs, the
# generated Brain asset surface, and the builder of the frozen /brain page. These are
# blocking even when another rule would make them eligible, so widening the
# website-only lists below can never accidentally expose a reducer input. The
# exception is a path the full promotion path already treats as release-neutral (a
# `test_*.py` file, `brain/authority/fixtures/**`): those are checked first and stay
# neutral here for exactly the reason they are neutral there.
BRAIN_INPUT_DIRECTORIES: tuple[str, ...] = (
    "brain",
    "catalog",
    "site/annotations",
    "site/annotations_v0_hand",
    "site/assets/brain",
    "site/cache",
    "site/data",
)
# site/build_brain_page.py renders site/out/brain.html, which is a release artifact
# (brain/tools/authority_contracts.py REQUIRED_RELEASE_PATHS). The promoter serves the
# frozen copy, so a code-only release can never ship a change to it.
BRAIN_INPUT_FILES: tuple[str, ...] = (
    "site/build_brain_page.py",
    "site/test_frontier_page.py",
)

# Worker source, configuration and build/test toolchain. All of it is rebuilt,
# typechecked, unit-tested, bundled and canaried at C exactly as in a full release.
WEBSITE_DIRECTORIES: tuple[str, ...] = (
    "wiki/assets",
    "wiki/migrations",
    "wiki/migrations-brain",
    "wiki/scripts",
    "wiki/src",
)
WEBSITE_FILES: tuple[str, ...] = (
    "requirements-ci.txt",
    "wiki/.npmrc",
    "wiki/package-lock.json",
    "wiki/package.json",
    "wiki/playwright.config.ts",
    "wiki/tsconfig.json",
    "wiki/vitest.config.ts",
    "wiki/vitest.corpus.config.ts",
    "wiki/wrangler.jsonc",
    ATTESTATION_PATH,
)
# Committed sources of non-Brain public assets. A byte change here always changes an
# attested file in the public baseline, so `require_attestation_consistency` can insist
# the committed attestation moved with them.
ASSET_SOURCE_DIRECTORIES: tuple[str, ...] = ("site/assets", "wiki/assets")
# Generators of non-Brain public assets. Their output is re-frozen and re-attested at
# C, so a change that alters any asset byte is caught by the attestation; a change that
# alters nothing ships as a no-op. Neither case can reach the frozen Brain release.
PUBLIC_ASSET_GENERATORS: tuple[str, ...] = (
    "site/build_concepts_page.py",
    "site/build_static_pages.py",
    "site/export_w3c.py",
    "site/export_wikidata_rdf.py",
    "site/render.py",
)
# Changes an operator must look at by hand even though they are eligible: Worker
# routing and asset configuration decide how the unchanged Brain bytes are served.
REVIEW_REQUIRED_FILES: tuple[str, ...] = ("wiki/wrangler.jsonc",)

GIT_DIFF_ARGUMENTS = release_authority.GIT_DIFF_ARGUMENTS

# The Brain-owned public paths the fence compares against production. `releases/*` and
# the deterministic cell shard are added at run time from the release identities and
# the staged cell manifest.
FENCE_STATIC_PATHS: tuple[str, ...] = (
    "assets/brain/sources.json",
    "assets/brain/xref_index.json",
    "assets/brain/cells/manifest.json",
    "assets/brain/cells/aliases.json",
    "assets/brain/cells/labels.json",
    "assets/brain/cells/supercells.json",
    "assets/brain/cells/explorer.json",
    "assets/brain/cells/frontier_graph.json",
)
SELECTOR_PUBLIC_PATH = "assets/brain/current.json"
BRAIN_PAGE_PUBLIC_PATH = "brain.html"
BRAIN_PAGE_ROUTE = "/brain"
# Every promotion stamps a fresh audit time into the selector; it is the only
# Brain-owned byte a code-only release is allowed to change.
SELECTOR_VOLATILE_KEYS = frozenset({"audited_at"})


class CodeOnlyError(RuntimeError):
    """Raised when a code-only release is ineligible or its evidence is invalid."""


def canonical_json_bytes(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CodeOnlyError(f"record contains unsupported JSON: {exc}") from exc
    return (rendered + "\n").encode("utf-8")


def record_id(identity: Mapping[str, object]) -> str:
    payload = (
        b"wikilean\0"
        + DOMAIN.encode("ascii")
        + b"\0canonical-json-v1\0"
        + canonical_json_bytes(identity).removesuffix(b"\n")
    )
    return "sha256:" + hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------- paths


def _is_inside(path: str, directory: str) -> bool:
    return path.startswith(directory + "/")


def _well_formed(path: str) -> bool:
    if not isinstance(path, str) or not path or path.startswith("/"):
        return False
    pure = PurePosixPath(path)
    if pure.as_posix() != path or any(part in {"", ".", ".."} for part in pure.parts):
        return False
    return "\\" not in path and "\0" not in path


def classify_path(path: str) -> str:
    """Classify one repository-relative path for the code-only policy.

    Returns ``"release-neutral"``, ``"website"``, ``"asset-source"``,
    ``"public-asset-generator"`` or ``"blocking"``. Only the first four are eligible.
    """
    if not _well_formed(path):
        return "blocking"
    # A path the full promotion path already accepts past an authority commit is
    # accepted here for the same reason; this also keeps the two policies consistent
    # for test files and fixtures that live under an otherwise blocking directory.
    if release_authority.is_release_neutral(path):
        return "release-neutral"
    if path in BRAIN_INPUT_FILES or any(
        _is_inside(path, directory) for directory in BRAIN_INPUT_DIRECTORIES
    ):
        return "blocking"
    if any(_is_inside(path, directory) for directory in ASSET_SOURCE_DIRECTORIES):
        return "asset-source"
    if path in PUBLIC_ASSET_GENERATORS:
        return "public-asset-generator"
    if path in WEBSITE_FILES or any(
        _is_inside(path, directory) for directory in WEBSITE_DIRECTORIES
    ):
        return "website"
    return "blocking"


def is_code_only_eligible(path: str) -> bool:
    """Return whether one repository-relative path may change in a code-only release."""
    return classify_path(path) != "blocking"


def blocking_changes(paths: Iterable[str]) -> list[str]:
    """Return the sorted ineligible subset of ``paths``."""
    return sorted({path for path in paths if not is_code_only_eligible(path)})


def describe_blocking(
    brain_authority_commit: str, code_commit: str, blocking: Sequence[str]
) -> str:
    shown = ", ".join(blocking[:20])
    more = f" (+{len(blocking) - 20} more)" if len(blocking) > 20 else ""
    return (
        f"commit {code_commit} changed paths that feed the frozen Brain release since "
        f"its authority {brain_authority_commit}: {shown}{more}. This release must take "
        "the full path (rebuild and requalify the Brain), not the code-only path."
    )


@dataclass(frozen=True)
class ChangeClassification:
    """Every path that differs between the Brain authority commit and the code commit."""

    neutral: tuple[str, ...] = ()
    website: tuple[str, ...] = ()
    asset_sources: tuple[str, ...] = ()
    generators: tuple[str, ...] = ()
    blocking: tuple[str, ...] = ()

    @property
    def eligible(self) -> bool:
        return not self.blocking

    @property
    def changed(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    *self.neutral,
                    *self.website,
                    *self.asset_sources,
                    *self.generators,
                    *self.blocking,
                }
            )
        )

    def document(self) -> dict[str, object]:
        return {
            "release_neutral": list(self.neutral),
            "website": list(self.website),
            "asset_sources": list(self.asset_sources),
            "public_asset_generators": list(self.generators),
        }


_CLASSIFICATION_FIELDS = {
    "release-neutral": "neutral",
    "website": "website",
    "asset-source": "asset_sources",
    "public-asset-generator": "generators",
    "blocking": "blocking",
}


def classify_changes(paths: Iterable[str]) -> ChangeClassification:
    buckets: dict[str, list[str]] = {name: [] for name in _CLASSIFICATION_FIELDS.values()}
    for path in sorted(set(paths)):
        buckets[_CLASSIFICATION_FIELDS[classify_path(path)]].append(path)
    return ChangeClassification(**{name: tuple(values) for name, values in buckets.items()})


def require_attestation_consistency(classification: ChangeClassification) -> None:
    """Refuse an asset-source change that never reached the committed attestation.

    ``site/assets`` and ``wiki/assets`` are copied verbatim into the public baseline, so
    a byte change in either must change a digest in
    ``wiki/public-asset-source-attestation.json``. If it did not, the operator rebuilt
    nothing: the change would be reviewed as shipped and silently not ship.
    """
    if not classification.asset_sources:
        return
    if ATTESTATION_PATH in classification.website:
        return
    raise CodeOnlyError(
        "asset sources changed ("
        + ", ".join(classification.asset_sources[:10])
        + f") but {ATTESTATION_PATH} did not; regenerate the public assets, re-attest "
        "them and commit the new attestation, or the change cannot ship"
    )


def review_required(classification: ChangeClassification) -> list[str]:
    """Eligible paths an operator must still read before approving the window."""
    return sorted(
        path
        for path in classification.changed
        if path in REVIEW_REQUIRED_FILES
    )


def validate_eligible_changes(value: object, label: str) -> list[str]:
    """Validate a recorded change list: sorted, unique, code-only-eligible paths."""
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CodeOnlyError(f"{label} must be an array of paths")
    if value != sorted(set(value)):
        raise CodeOnlyError(f"{label} must be sorted and unique")
    blocking = blocking_changes(value)
    if blocking:
        raise CodeOnlyError(
            f"{label} names paths that feed the frozen Brain release: "
            + ", ".join(blocking[:20])
        )
    return list(value)


def changed_paths(run_git: Callable[[Sequence[str]], str], base: str, head: str) -> list[str]:
    return release_authority.changed_paths(run_git, base, head)


# ---------------------------------------------------------------------- the fence


def _require_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CodeOnlyError(f"{label} must be a JSON object")
    return value


def selector_identity(body: bytes, label: str) -> dict[str, object]:
    """Decode a selector and drop the fields a promotion is allowed to restamp."""
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodeOnlyError(f"{label} is not valid JSON: {exc}") from exc
    document = _require_object(value, label)
    return {key: item for key, item in document.items() if key not in SELECTOR_VOLATILE_KEYS}


def _deterministic_shard_path(staged_public: Path) -> str:
    manifest_path = staged_public / "assets" / "brain" / "cells" / "manifest.json"
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise CodeOnlyError(f"staged cell manifest is unreadable: {exc}") from exc
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodeOnlyError(f"staged cell manifest is not valid JSON: {exc}") from exc
    shards = _require_object(value, "staged cell manifest").get("shards")
    if not isinstance(shards, dict) or not shards:
        raise CodeOnlyError("staged cell manifest has no shard inventory")
    for key in shards:
        if not isinstance(key, str) or not key or "/" in key or key in {".", ".."}:
            raise CodeOnlyError("staged cell manifest has an invalid shard key")
    # The canary picks the same deterministic shard, so a promotion and its canary
    # cover the same bytes.
    return f"assets/brain/cells/{sorted(shards)[0]}.json"


def _staged_shard_keys(staged_public: Path) -> list[str]:
    manifest_path = staged_public / "assets" / "brain" / "cells" / "manifest.json"
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise CodeOnlyError(f"staged cell manifest is unreadable: {exc}") from exc
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodeOnlyError(f"staged cell manifest is not valid JSON: {exc}") from exc
    shards = _require_object(value, "staged cell manifest").get("shards")
    if not isinstance(shards, dict) or not shards:
        raise CodeOnlyError("staged cell manifest has no shard inventory")
    for key in shards:
        if not isinstance(key, str) or not key or "/" in key or key in {".", ".."}:
            raise CodeOnlyError("staged cell manifest has an invalid shard key")
    return sorted(shards)


def require_brain_surface_shape(
    staged_public: Path,
    *,
    current_manifest_sha256: str,
    previous_manifest_sha256: str | None,
) -> list[str]:
    """Require the staged Brain surface to hold exactly the expected files.

    The fence compares bytes at known paths; this closes the complementary gap, an
    *extra* Brain-owned file that staging code at ``C`` learned to write and that no
    comparison would notice. The mutable surface is fixed by the cell manifest's own
    shard inventory, and only the current and previous immutable namespaces may exist.
    """
    root = staged_public / "assets" / "brain"
    expected = {
        SELECTOR_PUBLIC_PATH.removeprefix("assets/brain/"),
        *(path.removeprefix("assets/brain/") for path in FENCE_STATIC_PATHS),
        *(f"cells/{key}.json" for key in _staged_shard_keys(staged_public)),
    }
    namespaces = {
        digest
        for digest in (current_manifest_sha256, previous_manifest_sha256)
        if digest is not None
    }
    actual: set[str] = set()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise CodeOnlyError(
                f"staged Brain surface contains a symlink: {path.relative_to(root)}"
            )
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        head = relative.split("/", 2)
        if head[0] == "releases":
            if len(head) < 3 or head[1] not in namespaces:
                raise CodeOnlyError(
                    f"staged Brain surface holds an unexpected immutable namespace: {relative}"
                )
            continue
        actual.add(relative)
    unexpected = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unexpected or missing:
        raise CodeOnlyError(
            "staged Brain surface file set differs from the reused release's "
            f"(unexpected={unexpected}, missing={missing})"
        )
    releases = root / "releases"
    present = {entry.name for entry in releases.iterdir()} if releases.is_dir() else set()
    if present != namespaces:
        raise CodeOnlyError(
            "staged Brain surface retains the wrong immutable release namespaces: "
            f"{sorted(present)}"
        )
    return sorted(actual)


def fence_targets(
    staged_public: Path,
    *,
    current_manifest_sha256: str,
    previous_manifest_sha256: str | None,
) -> list[tuple[str, str]]:
    """Return the ordered ``(staged relative path, production route)`` pairs to compare."""
    for digest in (current_manifest_sha256, previous_manifest_sha256):
        if digest is not None and DIGEST_RE.fullmatch(digest) is None:
            raise CodeOnlyError("release manifest digest must be 64 lowercase hex characters")
    paths = [SELECTOR_PUBLIC_PATH, f"assets/brain/releases/{current_manifest_sha256}/release.json"]
    if previous_manifest_sha256 is not None:
        paths.append(f"assets/brain/releases/{previous_manifest_sha256}/release.json")
    paths.extend(FENCE_STATIC_PATHS)
    paths.append(_deterministic_shard_path(staged_public))
    targets = [(path, "/" + path) for path in paths]
    # The frozen page is served at /brain; /brain.html is only an alias the canary
    # checks after the deploy.
    targets.append((BRAIN_PAGE_PUBLIC_PATH, BRAIN_PAGE_ROUTE))
    return targets


def production_fetcher(
    base_url: str,
    *,
    request_timeout: float = 20.0,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    opener: Callable[..., object] | None = None,
    nonce: str | None = None,
) -> Callable[[str], bytes]:
    """Build a bounded, redirect-free, cache-busting GET for one production origin."""
    base = require_https_base_url(base_url)
    transport = trusted_urlopen() if opener is None else None
    open_request = transport if transport is not None else opener
    assert open_request is not None
    token = nonce or "fence"

    def fetch(route: str) -> bytes:
        query = urllib.parse.urlencode({"__brain_code_only": token})
        request = urllib.request.Request(
            f"{base}{route}?{query}",
            headers={
                # Never advertise text/html: Cloudflare's HTML rewriting keys on it
                # and would make /brain differ from the frozen page (see the canary).
                "Accept": "application/json,*/*;q=0.1",
                "Cache-Control": "no-cache",
                "User-Agent": "WikiLean-Brain-Code-Only-Fence/1",
            },
            method="GET",
        )
        result = fetch_bounded(
            request,
            opener=open_request,
            timeout=request_timeout,
            max_response_bytes=max_response_bytes,
        )
        return result.body

    return fetch


def brain_surface_fence(
    staged_public: Path,
    *,
    current_manifest_sha256: str,
    previous_manifest_sha256: str | None,
    fetch: Callable[[str], bytes],
) -> dict[str, object]:
    """Require the staged Brain surface to equal the bytes production serves today.

    This is the measurement that makes "code only" true rather than asserted. Every
    compared path must exist on both sides with identical bytes; the selector is
    compared after dropping ``audited_at``, which every promotion restamps.
    """
    surface = require_brain_surface_shape(
        staged_public,
        current_manifest_sha256=current_manifest_sha256,
        previous_manifest_sha256=previous_manifest_sha256,
    )
    targets = fence_targets(
        staged_public,
        current_manifest_sha256=current_manifest_sha256,
        previous_manifest_sha256=previous_manifest_sha256,
    )
    checked: list[dict[str, object]] = []
    response_bytes = 0
    for relative, route in targets:
        staged_path = staged_public.joinpath(*PurePosixPath(relative).parts)
        if staged_path.is_symlink() or not staged_path.is_file():
            raise CodeOnlyError(f"staged public tree omitted the Brain-owned file {relative}")
        staged_bytes = staged_path.read_bytes()
        try:
            live_bytes = fetch(route)
        except TransportError as exc:
            raise CodeOnlyError(
                f"Brain surface fence could not read {route} from production: {exc}"
            ) from exc
        response_bytes += len(live_bytes)
        if relative == SELECTOR_PUBLIC_PATH:
            staged_identity = selector_identity(staged_bytes, "staged selector")
            live_identity = selector_identity(live_bytes, "production selector")
            if staged_identity != live_identity:
                raise CodeOnlyError(
                    "the staged release selector differs from production outside "
                    "audited_at; the Brain surface is not unchanged"
                )
            compared = "identity-without-audited-at"
        else:
            if staged_bytes != live_bytes:
                raise CodeOnlyError(
                    f"staged {relative} differs from the bytes production serves at "
                    f"{route}; this is not a code-only release"
                )
            compared = "bytes"
        checked.append(
            {
                "path": relative,
                "route": route,
                "compared": compared,
                "bytes": len(staged_bytes),
                "sha256": hashlib.sha256(staged_bytes).hexdigest(),
            }
        )
    return {
        "schema": "wikilean.code-only-brain-surface-fence/v1",
        "checked": checked,
        "paths": len(checked),
        "mutable_surface": surface,
        "response_bytes": response_bytes,
    }


# ---------------------------------------------------------------------- the record


@dataclass(frozen=True)
class CodeOnlyRelease:
    """A validated code-only eligibility record."""

    record_id: str
    brain_release_id: str
    brain_manifest_sha256: str
    brain_authority_commit: str
    brain_reducer_commit: str
    previous_release_id: str | None
    previous_manifest_sha256: str | None
    code_commit: str
    selector_sha256: str
    classification: ChangeClassification
    review_required: tuple[str, ...] = ()
    document: Mapping[str, object] = field(default_factory=dict)

    def summary(self) -> dict[str, object]:
        return {
            "ok": True,
            "schema": SCHEMA,
            "record_id": self.record_id,
            "policy": POLICY,
            "brain_release_id": self.brain_release_id,
            "brain_authority_commit": self.brain_authority_commit,
            "code_commit": self.code_commit,
            "changed_paths": len(self.classification.changed),
            "review_required": list(self.review_required),
        }


def build_record(
    *,
    brain_release_id: str,
    brain_manifest_sha256: str,
    brain_authority_commit: str,
    brain_reducer_commit: str,
    previous_release_id: str | None,
    previous_manifest_sha256: str | None,
    code_commit: str,
    selector_sha256: str,
    classification: ChangeClassification,
) -> dict[str, object]:
    """Render the canonical eligibility record for a verified, eligible code-only release."""
    if classification.blocking:
        raise CodeOnlyError(
            describe_blocking(brain_authority_commit, code_commit, classification.blocking)
        )
    require_attestation_consistency(classification)
    identity: dict[str, object] = {
        "schema": SCHEMA,
        "policy": POLICY,
        "neutral_policy": release_authority.POLICY,
        "brain_release": {
            "release_id": brain_release_id,
            "manifest_sha256": brain_manifest_sha256,
            "authority_git_commit": brain_authority_commit,
            "reducer_git_commit": brain_reducer_commit,
        },
        "previous_release": (
            None
            if previous_release_id is None
            else {
                "release_id": previous_release_id,
                "manifest_sha256": previous_manifest_sha256,
            }
        ),
        "code_git_commit": code_commit,
        "live_selector_sha256": selector_sha256,
        "changes": classification.document(),
        "review_required": review_required(classification),
    }
    validate_record_document({**identity, "record_id": record_id(identity)})
    return {**identity, "record_id": record_id(identity)}


_RECORD_KEYS = frozenset(
    {
        "schema",
        "record_id",
        "policy",
        "neutral_policy",
        "brain_release",
        "previous_release",
        "code_git_commit",
        "live_selector_sha256",
        "changes",
        "review_required",
    }
)
_BRAIN_RELEASE_KEYS = frozenset(
    {"release_id", "manifest_sha256", "authority_git_commit", "reducer_git_commit"}
)
_PREVIOUS_RELEASE_KEYS = frozenset({"release_id", "manifest_sha256"})
_CHANGE_KEYS = frozenset(
    {"release_neutral", "website", "asset_sources", "public_asset_generators"}
)


def _exact_keys(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        raise CodeOnlyError(
            f"{label} key mismatch (missing: {missing or 'none'}, unknown: {unknown or 'none'})"
        )


def _require_release_id(value: object, label: str) -> str:
    if not isinstance(value, str) or RELEASE_ID_RE.fullmatch(value) is None:
        raise CodeOnlyError(f"{label} must be sha256:<64 lowercase hex>")
    return value


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise CodeOnlyError(f"{label} must be 64 lowercase hexadecimal characters")
    return value


def _require_commit(value: object, label: str) -> str:
    if not isinstance(value, str) or GIT_COMMIT_RE.fullmatch(value) is None:
        raise CodeOnlyError(f"{label} must be a full 40-character Git commit")
    return value


def _require_path_list(value: object, label: str, expected: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CodeOnlyError(f"{label} must be an array of paths")
    if value != sorted(set(value)):
        raise CodeOnlyError(f"{label} must be sorted and unique")
    for path in value:
        actual = classify_path(path)
        if actual != expected:
            raise CodeOnlyError(
                f"{label} lists {path!r}, which this policy classifies as {actual!r}"
            )
    return tuple(value)


def validate_record_document(document: Mapping[str, object]) -> CodeOnlyRelease:
    """Validate an eligibility record's shape, identity digest and internal consistency."""
    _exact_keys(document, _RECORD_KEYS, "code-only eligibility record")
    if document.get("schema") != SCHEMA:
        raise CodeOnlyError("code-only eligibility record schema mismatch")
    if document.get("policy") != POLICY:
        raise CodeOnlyError(
            f"code-only eligibility record was written under policy "
            f"{document.get('policy')!r}, not {POLICY!r}"
        )
    if document.get("neutral_policy") != release_authority.POLICY:
        raise CodeOnlyError(
            "code-only eligibility record was written against a different "
            "release-neutral policy"
        )
    brain = _require_object(document.get("brain_release"), "brain_release")
    _exact_keys(brain, _BRAIN_RELEASE_KEYS, "brain_release")
    release_id = _require_release_id(brain.get("release_id"), "brain_release.release_id")
    manifest_sha256 = _require_digest(
        brain.get("manifest_sha256"), "brain_release.manifest_sha256"
    )
    authority_commit = _require_commit(
        brain.get("authority_git_commit"), "brain_release.authority_git_commit"
    )
    reducer_commit = _require_commit(
        brain.get("reducer_git_commit"), "brain_release.reducer_git_commit"
    )
    if reducer_commit != authority_commit:
        raise CodeOnlyError("brain_release authority and reducer commits must agree")
    previous = document.get("previous_release")
    previous_release_id: str | None = None
    previous_manifest_sha256: str | None = None
    if previous is not None:
        previous_object = _require_object(previous, "previous_release")
        _exact_keys(previous_object, _PREVIOUS_RELEASE_KEYS, "previous_release")
        previous_release_id = _require_release_id(
            previous_object.get("release_id"), "previous_release.release_id"
        )
        previous_manifest_sha256 = _require_digest(
            previous_object.get("manifest_sha256"), "previous_release.manifest_sha256"
        )
        if previous_manifest_sha256 == manifest_sha256:
            raise CodeOnlyError("previous release manifest must differ from the reused release")
    code_commit = _require_commit(document.get("code_git_commit"), "code_git_commit")
    selector_sha256 = _require_digest(
        document.get("live_selector_sha256"), "live_selector_sha256"
    )
    changes = _require_object(document.get("changes"), "changes")
    _exact_keys(changes, _CHANGE_KEYS, "changes")
    classification = ChangeClassification(
        neutral=_require_path_list(
            changes.get("release_neutral"), "changes.release_neutral", "release-neutral"
        ),
        website=_require_path_list(changes.get("website"), "changes.website", "website"),
        asset_sources=_require_path_list(
            changes.get("asset_sources"), "changes.asset_sources", "asset-source"
        ),
        generators=_require_path_list(
            changes.get("public_asset_generators"),
            "changes.public_asset_generators",
            "public-asset-generator",
        ),
    )
    require_attestation_consistency(classification)
    if code_commit == authority_commit and classification.changed:
        raise CodeOnlyError("a record at the Brain authority commit must list no changes")
    if code_commit != authority_commit and not classification.changed:
        raise CodeOnlyError("a record past the Brain authority commit must list its changes")
    declared_review = document.get("review_required")
    expected_review = review_required(classification)
    if declared_review != expected_review:
        raise CodeOnlyError("review_required does not match the recorded changes")
    identity = {key: value for key, value in document.items() if key != "record_id"}
    expected_id = record_id(identity)
    if document.get("record_id") != expected_id:
        raise CodeOnlyError("code-only eligibility record_id does not match its contents")
    return CodeOnlyRelease(
        record_id=expected_id,
        brain_release_id=release_id,
        brain_manifest_sha256=manifest_sha256,
        brain_authority_commit=authority_commit,
        brain_reducer_commit=reducer_commit,
        previous_release_id=previous_release_id,
        previous_manifest_sha256=previous_manifest_sha256,
        code_commit=code_commit,
        selector_sha256=selector_sha256,
        classification=classification,
        review_required=tuple(expected_review),
        document=dict(document),
    )


def load_record(path: Path) -> CodeOnlyRelease:
    """Read and validate a canonical eligibility record from disk."""
    if not path.is_absolute():
        raise CodeOnlyError("code-only eligibility record path must be absolute")
    if path.is_symlink() or not path.is_file():
        raise CodeOnlyError("code-only eligibility record must be a regular file")
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodeOnlyError(f"code-only eligibility record is not valid JSON: {exc}") from exc
    document = _require_object(value, "code-only eligibility record")
    if canonical_json_bytes(document) != raw:
        raise CodeOnlyError("code-only eligibility record is not canonical JSON")
    return validate_record_document(document)


def require_record_matches(
    record: CodeOnlyRelease,
    *,
    release_id: str,
    manifest_sha256: str,
    authority_commit: str,
    previous_release_id: str | None,
    previous_manifest_sha256: str | None,
    code_commit: str,
    selector_sha256: str,
) -> None:
    """Re-derive every claim in a record against independently observed state."""
    if record.brain_release_id != release_id or record.brain_manifest_sha256 != manifest_sha256:
        raise CodeOnlyError(
            "code-only record names a different Brain release than the one being promoted"
        )
    if record.brain_authority_commit != authority_commit:
        raise CodeOnlyError("code-only record names a different Brain authority commit")
    if record.previous_release_id != previous_release_id or (
        record.previous_manifest_sha256 != previous_manifest_sha256
    ):
        raise CodeOnlyError("code-only record names a different retained previous release")
    if record.code_commit != code_commit:
        raise CodeOnlyError(
            f"code-only record was decided at {record.code_commit}, not the promotion "
            f"commit {code_commit}"
        )
    if record.selector_sha256 != selector_sha256:
        raise CodeOnlyError(
            "production's release selector changed since the code-only record was "
            "decided; re-evaluate eligibility"
        )


# ------------------------------------------------------------------------ the CLI


def _git_text(git: Path, repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - explicit reviewed executable and arguments
        [str(git), "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise CodeOnlyError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return result.stdout.decode("utf-8").strip()


def _clean_checkout_commit(git: Path, repo: Path) -> str:
    head = _git_text(git, repo, "rev-parse", "HEAD")
    main = _git_text(git, repo, "rev-parse", "refs/heads/main")
    dirty = _git_text(git, repo, "status", "--porcelain=v1", "--untracked-files=all")
    if dirty:
        raise CodeOnlyError(f"checkout is dirty:\n{dirty}")
    if head != main:
        raise CodeOnlyError(f"checkout HEAD must equal refs/heads/main (HEAD={head}, main={main})")
    return _require_commit(head, "promotion commit")


def _release_identity(manifest_path: Path) -> tuple[str, str, str, str]:
    raw = manifest_path.read_bytes()
    value = _require_object(json.loads(raw), "release manifest")
    authority = _require_object(value.get("authority"), "release manifest authority")
    reducer = _require_object(value.get("reducer"), "release manifest reducer")
    return (
        _require_release_id(value.get("release_id"), "release manifest release_id"),
        hashlib.sha256(raw).hexdigest(),
        _require_commit(authority.get("git_commit"), "release authority commit"),
        _require_commit(reducer.get("git_commit"), "release reducer commit"),
    )


def _evaluate(args: argparse.Namespace) -> int:
    repo = args.repo_root.resolve(strict=True)
    git = args.git.resolve(strict=True)
    code_commit = _clean_checkout_commit(git, repo)

    probe_body: bytes
    if args.selector_file is not None:
        probe_body = args.selector_file.resolve(strict=True).read_bytes()
    else:
        fetch = production_fetcher(args.base_url, request_timeout=args.request_timeout)
        probe_body = fetch("/" + SELECTOR_PUBLIC_PATH)
    selector = _require_object(json.loads(probe_body), "production selector")
    selector_sha256 = hashlib.sha256(probe_body).hexdigest()

    release_id, manifest_sha256, authority_commit, reducer_commit = _release_identity(
        args.release_manifest.resolve(strict=True)
    )
    if selector.get("release_id") != release_id or (
        selector.get("manifest_sha256") != manifest_sha256
    ):
        raise CodeOnlyError(
            "production does not currently serve the release this record would reuse; "
            "a code-only release may only reuse the live release"
        )
    previous_release_id = selector.get("previous_release_id")
    previous_manifest_sha256 = selector.get("previous_manifest_sha256")
    if previous_release_id is not None:
        previous_release_id = _require_release_id(
            previous_release_id, "selector previous_release_id"
        )
        previous_manifest_sha256 = _require_digest(
            previous_manifest_sha256, "selector previous_manifest_sha256"
        )

    if code_commit != authority_commit:
        if (
            subprocess.run(  # noqa: S603 - explicit reviewed executable
                [str(git), "-C", str(repo), "merge-base", "--is-ancestor", authority_commit, code_commit],
                capture_output=True,
                check=False,
            ).returncode
            != 0
        ):
            raise CodeOnlyError(
                f"the reused release's authority {authority_commit} is not an ancestor of "
                f"{code_commit}; main only moves forward past a released Brain"
            )
    classification = classify_changes(
        changed_paths(lambda arguments: _git_text(git, repo, *arguments), authority_commit, code_commit)
        if code_commit != authority_commit
        else []
    )
    document = build_record(
        brain_release_id=release_id,
        brain_manifest_sha256=manifest_sha256,
        brain_authority_commit=authority_commit,
        brain_reducer_commit=reducer_commit,
        previous_release_id=previous_release_id,
        previous_manifest_sha256=previous_manifest_sha256,
        code_commit=code_commit,
        selector_sha256=selector_sha256,
        classification=classification,
    )
    sys.stdout.buffer.write(canonical_json_bytes(document))
    return 0


def _verify(args: argparse.Namespace) -> int:
    record = load_record(args.record.resolve(strict=True))
    if args.expected_record_id is not None and record.record_id != args.expected_record_id:
        raise CodeOnlyError("code-only eligibility record ID does not match the expected ID")
    if args.expected_code_commit is not None and record.code_commit != args.expected_code_commit:
        raise CodeOnlyError("code-only eligibility record names a different code commit")
    print(json.dumps(record.summary(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _classify(args: argparse.Namespace) -> int:
    classification = classify_changes(args.paths)
    print(
        json.dumps(
            {
                "ok": classification.eligible,
                "policy": POLICY,
                **classification.document(),
                "blocking": list(classification.blocking),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if classification.eligible else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    evaluate = sub.add_parser(
        "evaluate",
        help="decide code-only eligibility and print the canonical record",
    )
    evaluate.add_argument("--repo-root", type=Path, required=True)
    evaluate.add_argument("--git", type=Path, required=True)
    evaluate.add_argument("--release-manifest", type=Path, required=True)
    evaluate.add_argument("--base-url", default=PRODUCTION_ORIGIN)
    evaluate.add_argument(
        "--selector-file",
        type=Path,
        help="use these exact selector bytes instead of fetching production (rehearsal only)",
    )
    evaluate.add_argument("--request-timeout", type=float, default=20.0)
    evaluate.set_defaults(handler=_evaluate)

    verify = sub.add_parser("verify", help="validate a canonical eligibility record")
    verify.add_argument("--record", type=Path, required=True)
    verify.add_argument("--expected-record-id")
    verify.add_argument("--expected-code-commit")
    verify.set_defaults(handler=_verify)

    classify = sub.add_parser("classify", help="classify repository-relative paths")
    classify.add_argument("paths", nargs="+")
    classify.set_defaults(handler=_classify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (CodeOnlyError, TransportError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
