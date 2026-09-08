#!/usr/bin/env python3
"""Install/verify the three legacy mirrors of one sealed observation.

An advisory lock and durable rollback journal cover the whole file family.
Readers take that lock, reject unresolved journals, verify all mirror bytes,
then consume the immutable bundle. The marker is installed last; a hard-killed
installer is rolled back by the next installer before any new publication.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import stat
import sys
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
import stage_io  # noqa: E402
import wikidata_observation as observation  # noqa: E402

SCHEMA = "wikilean.installed-wikidata-observation/v1"
JOURNAL_SCHEMA = "wikilean.wikidata-observation-install-journal/v1"
MIRRORS = {
    "wikidata-universe": "catalog/data/wikidata_universe.jsonl",
    "wikidata-edges": "catalog/mathlib_deps/wikidata_edges.jsonl",
    "wikidata-descriptions": "catalog/data/wikidata_descriptions.json",
}
STATE = "catalog/.cache/wikidata/installed-observation"
MARKER = STATE + "/installed.json"


def _state(repo: Path, *, create=False) -> Path:
    if not repo.is_absolute() or repo.is_symlink() or not repo.is_dir():
        raise observation.ObservationError("installation root must be an absolute real directory")
    state = repo / STATE
    if ".." in repo.parts or any(path.is_symlink() for path in [state, *state.parents]):
        raise observation.ObservationError("linked installation state or ancestor")
    if create:
        stage_io.ensure_private_directory(repo, state)
    if state.exists():
        metadata = state.lstat()
        if state.is_symlink() or not stat.S_ISDIR(metadata.st_mode) \
                or stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.getuid():
            raise observation.ObservationError("installation state must be current-user-owned mode 0700")
    return state


@contextmanager
def _lock(state: Path, *, exclusive: bool):
    path = state / "installed.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 \
                or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise observation.ObservationError("invalid installed-observation lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        current = path.lstat()
        if (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino):
            raise observation.ObservationError("installed-observation lock changed")
        yield
    finally:
        os.close(descriptor)


@contextmanager
def _parent_descriptor(path: Path):
    """Pin each real ancestor; subsequent writes never re-traverse path strings."""
    if not path.is_absolute() or ".." in path.parts:
        raise observation.ObservationError("installation target must be an absolute literal path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for component in path.parent.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _replace_at(descriptor: int, name: str, raw: bytes) -> None:
    temporary = "." + name + ".observation-" + uuid.uuid4().hex
    try:
        output = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o644, dir_fd=descriptor)
        with os.fdopen(output, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass


def _replace(path: Path, raw: bytes) -> None:
    with _parent_descriptor(path) as descriptor:
        _replace_at(descriptor, path.name, raw)


def _recover(repo: Path, state: Path) -> None:
    journal_path = state / "journal.json"
    if not journal_path.exists() and not journal_path.is_symlink():
        return
    journal = observation.parse(observation.read_regular(journal_path), "installation journal", canonical_required=True)
    observation.exact(journal, {"schema", "transaction", "previous"}, "installation journal")
    if journal["schema"] != JOURNAL_SCHEMA or not isinstance(journal["transaction"], str) \
            or not re.fullmatch(r"transaction-[0-9a-f]{32}", journal["transaction"]):
        raise observation.ObservationError("invalid installation transaction")
    expected = [*MIRRORS.values(), MARKER]
    if not isinstance(journal["previous"], list) or [x.get("path") for x in journal["previous"]] != expected:
        raise observation.ObservationError("installation journal has a foreign target set")
    transaction = state / journal["transaction"]
    restored = []
    for index, item in enumerate(journal["previous"]):
        observation.exact(item, {"path", "sha256", "bytes"}, "previous mirror")
        if item["sha256"] is None:
            if item["bytes"] != 0:
                raise observation.ObservationError("invalid absent previous mirror")
            restored.append((repo / item["path"], None))
        else:
            raw = observation.read_regular(transaction / f"previous-{index}")
            if observation.sha(raw) != item["sha256"] or len(raw) != item["bytes"]:
                raise observation.ObservationError("installation recovery copy changed")
            restored.append((repo / item["path"], raw))
    # Validate every byte and pin every no-follow parent before the first repair.
    # A redirected ancestor must never turn rollback deletion into an external
    # file deletion. Backups remain until interrupted recovery is idempotent.
    with ExitStack() as stack:
        targets = [(path, raw, stack.enter_context(_parent_descriptor(path))) for path, raw in restored]
        for path, raw, descriptor in targets:
            if raw is None:
                try:
                    os.unlink(path.name, dir_fd=descriptor)
                except FileNotFoundError:
                    pass
                os.fsync(descriptor)
            else:
                _replace_at(descriptor, path.name, raw)
    journal_path.unlink()
    stage_io.fsync_directory(state)
    for index in range(len(restored)):
        (transaction / f"previous-{index}").unlink(missing_ok=True)
    transaction.rmdir()
    stage_io.fsync_directory(state)


def _load_locked(repo: Path, state: Path, *, plan_bytes: bytes | None = None) -> dict:
    if (state / "journal.json").exists() or (state / "journal.json").is_symlink():
        raise observation.ObservationError("unfinished observation installation; run installer recovery")
    marker = observation.parse(observation.read_regular(repo / MARKER), "installed observation", canonical_required=True)
    observation.exact(marker, {"schema", "bundle_id", "bundle_path", "plan_sha256", "mirrors"}, "installed observation")
    if marker["schema"] != SCHEMA or marker["mirrors"] != MIRRORS:
        raise observation.ObservationError("invalid installed observation mapping")
    bundle = observation.verify_bundle(Path(marker["bundle_path"]), expected_id=marker["bundle_id"])
    if observation.sha(observation.canonical(bundle["plan"])) != marker["plan_sha256"]:
        raise observation.ObservationError("installed observation plan mismatch")
    if plan_bytes is not None and observation.canonical(bundle["plan"]) != plan_bytes:
        raise observation.ObservationError("installed observation differs from reviewed request plan")
    for name, relative in MIRRORS.items():
        if observation.read_regular(repo / relative) != bundle["normalized"][name]:
            raise observation.ObservationError("installed Wikidata mirror generation mismatch")
    bundle["path"] = Path(marker["bundle_path"])
    return bundle


def load_installed(repo: Path, *, plan_bytes: bytes | None = None, required: bool = False) -> dict | None:
    state = _state(repo)
    expected = os.environ.get("WIKILEAN_WIKIDATA_OBSERVATION_BUNDLE")
    required = required or bool(expected)
    if not state.exists():
        if required:
            raise observation.ObservationError("sealed Wikidata installation is required")
        return None
    with _lock(state, exclusive=False):
        bundle = _load_locked(repo, state, plan_bytes=plan_bytes)
        if expected and str(bundle["path"]) != expected:
            raise observation.ObservationError("installed Wikidata bundle differs from this run's generation")
        return bundle


def install(repo: Path, bundle_path: Path, *, plan_bytes: bytes, after_replace=None) -> dict:
    bundle = observation.verify_bundle(bundle_path)
    observation.validate_plan(observation.parse(plan_bytes, "reviewed plan", canonical_required=True))
    if observation.canonical(bundle["plan"]) != plan_bytes:
        raise observation.ObservationError("bundle differs from reviewed request plan")
    state = _state(repo, create=True)
    with _lock(state, exclusive=True):
        _recover(repo, state)
        marker = observation.canonical({"schema": SCHEMA, "bundle_id": bundle["bundle_id"],
            "bundle_path": str(bundle_path), "plan_sha256": observation.sha(plan_bytes), "mirrors": MIRRORS})
        candidates = [(repo / relative, bundle["normalized"][name]) for name, relative in MIRRORS.items()]
        candidates.append((repo / MARKER, marker))
        transaction = state / ("transaction-" + uuid.uuid4().hex)
        transaction.mkdir(mode=0o700)
        previous = []
        journal_published = False
        try:
            for index, (path, _raw) in enumerate(candidates):
                if path.exists() or path.is_symlink():
                    raw = observation.read_regular(path)
                    stage_io.write_bytes_exclusive(transaction / f"previous-{index}", raw, mode=0o644)
                    previous.append({"path": path.relative_to(repo).as_posix(), "sha256": observation.sha(raw), "bytes": len(raw)})
                else:
                    previous.append({"path": path.relative_to(repo).as_posix(), "sha256": None, "bytes": 0})
            stage_io.fsync_directory(transaction)
            stage_io.fsync_directory(state)
            _replace(state / "journal.json", observation.canonical({"schema": JOURNAL_SCHEMA,
                     "transaction": transaction.name, "previous": previous}))
            journal_published = True
            for index, (path, raw) in enumerate(candidates):
                _replace(path, raw)
                if after_replace is not None:
                    after_replace(index)
            # All output bytes and the marker are complete before readers can
            # observe success. The journal remains the recovery authority until then.
            for path, raw in candidates:
                if observation.read_regular(path) != raw:
                    raise observation.ObservationError("Wikidata mirror changed during installation")
            (state / "journal.json").unlink()
            stage_io.fsync_directory(state)
            journal_published = False
        except BaseException:
            if journal_published:
                _recover(repo, state)
            raise
        finally:
            if not journal_published and transaction.exists():
                for child in transaction.iterdir():
                    child.unlink()
                transaction.rmdir()
                stage_io.fsync_directory(state)
        return _load_locked(repo, state, plan_bytes=plan_bytes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("install", "verify"))
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--bundle", type=Path)
    args = parser.parse_args()
    try:
        plan_bytes = observation.read_regular(args.plan, max_bytes=16 * 1024 * 1024)
        observation.validate_plan(observation.parse(plan_bytes, "reviewed plan", canonical_required=True))
        if args.mode == "install":
            if args.bundle is None:
                parser.error("install requires --bundle")
            bundle = install(args.repo_root, args.bundle, plan_bytes=plan_bytes)
        else:
            if args.bundle is not None:
                parser.error("verify reads the installed generation, do not pass --bundle")
            bundle = load_installed(args.repo_root, plan_bytes=plan_bytes, required=True)
        print(bundle["path"])
        return 0
    except (observation.ObservationError, observation.contracts.VerificationError, OSError) as exc:
        print(f"Wikidata observation install/verify failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
