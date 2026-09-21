#!/usr/bin/env python3
"""Durable, append-only evidence journal for Brain release promotion.

The promoter owns the deployment state machine.  This module owns only the
filesystem contract: a process-wide lock, immutable hash-chained event files,
and content-addressed evidence blobs.  Journal writes require a held
``PromotionLock`` for the receipt root; validation and inspection are read-only.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import threading
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any, Iterator, Mapping

EVENT_SCHEMA = "wikilean.brain-deployment-event/v1"
BLOB_REF_SCHEMA = "wikilean.brain-deployment-blob-ref/v1"
EVENT_DOMAIN = "wikilean.brain-deployment-event.v1"
FINAL_EVENT_KIND = "final_state"
RECEIPT_ROOT_SCHEMA = "wikilean.brain-deployment-receipt-root/v1"
RECEIPT_ROOT_MARKER = ".wikilean-brain-receipts.json"

MAX_SAFE_INTEGER = 9_007_199_254_740_991
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
BLOB_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MEDIA_TYPE_RE = re.compile(
    r'^[^/\s;]+/[^\s;]+(?:\s*;\s*[^=;\s]+=(?:"[^"]*"|[^;\s]+))*$'
)
EVENT_FILE_RE = re.compile(
    r"^(?P<sequence>[0-9]{6})-(?P<kind>[a-z][a-z0-9_]{0,63})-"
    r"(?P<digest>[0-9a-f]{64})\.json$"
)
RECORDED_AT_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z$"
)

_EVENT_KEYS = {
    "schema",
    "event_id",
    "attempt_id",
    "sequence",
    "previous_event_id",
    "recorded_at",
    "kind",
    "payload",
}
_BLOB_REF_KEYS = {"schema", "name", "path", "sha256", "bytes", "media_type"}
_HELD_LOCKS: dict[Path, tuple[int, object]] = {}
_HELD_LOCKS_GUARD = threading.Lock()


class JournalError(RuntimeError):
    """Base class for journal failures."""


class ReceiptRootError(JournalError):
    """The configured durable receipt root is unsafe."""


class JournalLockError(JournalError):
    """The promotion lock is already owned or cannot be acquired."""


class JournalValidationError(JournalError):
    """Committed journal evidence is malformed or has been modified."""


class JournalStateError(JournalError):
    """An append would violate the journal state machine."""


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _lstat(path: Path, label: str) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise JournalValidationError(f"cannot inspect {label} {path}: {exc}") from exc
    if stat.S_ISLNK(value.st_mode):
        raise JournalValidationError(f"{label} must not be a symlink: {path}")
    return value


def _validate_private_directory(path: Path, label: str) -> os.stat_result:
    value = _lstat(path, label)
    if not stat.S_ISDIR(value.st_mode):
        raise JournalValidationError(f"{label} is not a directory: {path}")
    if value.st_uid != os.geteuid():
        raise JournalValidationError(
            f"{label} must be owned by uid {os.geteuid()}, got {value.st_uid}: {path}"
        )
    mode = stat.S_IMODE(value.st_mode)
    if mode & 0o077 or mode & 0o700 != 0o700:
        raise JournalValidationError(
            f"{label} permissions must be owner-only rwx (0700), got {mode:04o}: {path}"
        )
    return value


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(descriptor: int) -> None:
    os.fsync(descriptor)
    full_fsync = getattr(fcntl, "F_FULLFSYNC", None)
    if full_fsync is not None:
        fcntl.fcntl(descriptor, full_fsync)


def _mkdir_durable(path: Path, parent: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        _validate_private_directory(path, "journal directory")
        return
    _validate_private_directory(path, "journal directory")
    _fsync_directory(parent)


def validate_receipt_root(path: os.PathLike[str] | str, repo_root: os.PathLike[str] | str) -> Path:
    """Validate or create an absolute, private receipt directory outside the checkout."""

    supplied = Path(path)
    if not supplied.is_absolute():
        raise ReceiptRootError("receipt root must be an absolute path")
    repository = Path(repo_root)
    if not repository.is_absolute():
        raise ReceiptRootError("repository root must be an absolute path")
    try:
        repository = repository.resolve(strict=True)
    except OSError as exc:
        raise ReceiptRootError(f"cannot resolve repository root {repository}: {exc}") from exc
    if not repository.is_dir():
        raise ReceiptRootError(f"repository root is not a directory: {repository}")

    # Reject a link at the configured root itself.  Parent links (notably macOS
    # /var -> /private/var) are resolved before the external-path comparison and
    # the physical path is returned to the caller.
    if supplied.is_symlink():
        raise ReceiptRootError(f"receipt root must not be a symlink: {supplied}")
    try:
        parent = supplied.parent.resolve(strict=True)
    except OSError as exc:
        raise ReceiptRootError(
            f"receipt root parent must already exist: {supplied.parent}"
        ) from exc
    physical = parent / supplied.name
    resolved_candidate = physical.resolve(strict=False)
    if (
        resolved_candidate == repository
        or _is_relative_to(resolved_candidate, repository)
        or _is_relative_to(repository, resolved_candidate)
    ):
        raise ReceiptRootError(
            f"receipt root must be outside the repository checkout: {resolved_candidate}"
        )

    if not physical.exists():
        try:
            physical.mkdir(mode=0o700)
        except OSError as exc:
            raise ReceiptRootError(f"cannot create receipt root {physical}: {exc}") from exc
        try:
            _fsync_directory(parent)
        except OSError as exc:
            raise ReceiptRootError(f"cannot durably create receipt root {physical}: {exc}") from exc
    try:
        _validate_private_directory(physical, "receipt root")
    except JournalValidationError as exc:
        raise ReceiptRootError(str(exc)) from exc
    if not os.access(physical, os.R_OK | os.W_OK | os.X_OK, effective_ids=True):
        raise ReceiptRootError(f"receipt root is not readable and writable by this uid: {physical}")
    return physical.resolve(strict=True)


def _validate_existing_root(root: Path) -> Path:
    if not root.is_absolute():
        raise ReceiptRootError("receipt root must be an absolute path")
    if root.is_symlink():
        raise ReceiptRootError(f"receipt root must not be a symlink: {root}")
    try:
        resolved = root.resolve(strict=True)
        _validate_private_directory(resolved, "receipt root")
    except (OSError, JournalValidationError) as exc:
        if isinstance(exc, ReceiptRootError):
            raise
        raise ReceiptRootError(str(exc)) from exc
    return resolved


class PromotionLock:
    """Nonblocking exclusive lock held for a promoter's full process window."""

    def __init__(self, root: os.PathLike[str] | str) -> None:
        self.root = _validate_existing_root(Path(root))
        self._descriptor: int | None = None
        self._token = object()

    def __enter__(self) -> "PromotionLock":
        with _HELD_LOCKS_GUARD:
            held = _HELD_LOCKS.get(self.root)
            if held is not None and held[0] == os.getpid():
                raise JournalLockError(f"promotion lock is already held for {self.root}")

        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.root / ".promotion.lock", flags, 0o600)
        except OSError as exc:
            raise JournalLockError(f"cannot open promotion lock in {self.root}: {exc}") from exc
        try:
            lock_stat = os.fstat(descriptor)
            if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.geteuid():
                raise JournalLockError("promotion lock must be a regular file owned by this uid")
            mode = stat.S_IMODE(lock_stat.st_mode)
            if mode != 0o600:
                raise JournalLockError(
                    f"promotion lock permissions must be 0600, got {mode:04o}"
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(descriptor)
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise JournalLockError(f"another promotion process holds {self.root}") from exc
            raise JournalLockError(f"cannot acquire promotion lock in {self.root}: {exc}") from exc
        except Exception:
            os.close(descriptor)
            raise

        with _HELD_LOCKS_GUARD:
            held = _HELD_LOCKS.get(self.root)
            if held is not None and held[0] == os.getpid():
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
                raise JournalLockError(f"promotion lock is already held for {self.root}")
            _HELD_LOCKS[self.root] = (os.getpid(), self._token)
        self._descriptor = descriptor
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        descriptor, self._descriptor = self._descriptor, None
        with _HELD_LOCKS_GUARD:
            held = _HELD_LOCKS.get(self.root)
            if held == (os.getpid(), self._token):
                del _HELD_LOCKS[self.root]
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _require_lock(root: Path) -> None:
    with _HELD_LOCKS_GUARD:
        held = _HELD_LOCKS.get(root)
    if held is None or held[0] != os.getpid():
        raise JournalLockError(f"a held PromotionLock is required to write {root}")


def _validate_canonical_type(
    value: Any,
    location: str = "$",
    ancestors: set[int] | None = None,
) -> None:
    if ancestors is None:
        ancestors = set()
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        if unicodedata.normalize("NFC", value) != value:
            raise JournalValidationError(f"{location} contains a non-NFC string")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise JournalValidationError(f"{location} contains an invalid Unicode scalar") from exc
        return
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise JournalValidationError(f"{location} integer exceeds the portable JSON range")
        return
    if isinstance(value, list):
        identity = id(value)
        if identity in ancestors:
            raise JournalValidationError(f"{location} contains a cyclic value")
        ancestors.add(identity)
        try:
            for index, item in enumerate(value):
                _validate_canonical_type(item, f"{location}[{index}]", ancestors)
        finally:
            ancestors.remove(identity)
        return
    if isinstance(value, dict):
        identity = id(value)
        if identity in ancestors:
            raise JournalValidationError(f"{location} contains a cyclic value")
        ancestors.add(identity)
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    raise JournalValidationError(f"{location} object keys must be strings")
                _validate_canonical_type(key, f"{location}.<key>", ancestors)
                _validate_canonical_type(item, f"{location}.{key}", ancestors)
        finally:
            ancestors.remove(identity)
        return
    raise JournalValidationError(f"{location} has unsupported type {type(value).__name__}")


def _canonical_json_bytes(value: Any) -> bytes:
    _validate_canonical_type(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _reject_number(text: str) -> None:
    raise JournalValidationError(f"non-integer JSON number is forbidden: {text}")


def _parse_integer(text: str) -> int:
    if text == "-0":
        raise JournalValidationError("negative zero is forbidden")
    value = int(text)
    if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
        raise JournalValidationError("integer exceeds the portable JSON range")
    return value


def _object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise JournalValidationError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _parse_canonical_json(data: bytes, location: Path) -> Any:
    try:
        text = data.decode("utf-8")
        value = json.loads(
            text,
            parse_int=_parse_integer,
            parse_float=_reject_number,
            parse_constant=_reject_number,
            object_pairs_hook=_object_from_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JournalValidationError(f"invalid JSON in {location}: {exc}") from exc
    _validate_canonical_type(value, str(location))
    if _canonical_json_bytes(value) != data:
        raise JournalValidationError(f"event is not canonical JSON: {location}")
    return value


def _domain_hash(value: Mapping[str, Any]) -> str:
    prefix = f"wikilean\0{EVENT_DOMAIN}\0canonical-json-v1\0".encode("ascii")
    return "sha256:" + hashlib.sha256(prefix + _canonical_json_bytes(dict(value))).hexdigest()


def _now() -> str:
    value = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")
    return value.replace("+00:00", "Z")


def _validate_recorded_at(value: Any) -> str:
    if not isinstance(value, str) or RECORDED_AT_RE.fullmatch(value) is None:
        raise JournalValidationError("recorded_at must be a UTC RFC3339 timestamp ending in Z")
    try:
        dt.datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise JournalValidationError(f"invalid recorded_at timestamp: {value}") from exc
    return value


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise OSError("short write while publishing journal evidence")
        offset += written


def _publish_immutable(directory: Path, filename: str, data: bytes) -> Path:
    _validate_private_directory(directory, "journal evidence directory")
    directory_fd = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    temporary = f".pending-{os.getpid()}-{secrets.token_hex(16)}"
    descriptor: int | None = None
    linked = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        _write_all(descriptor, data)
        os.fchmod(descriptor, 0o400)
        _fsync_file(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(
            temporary,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        linked = True
        os.fsync(directory_fd)
        os.unlink(temporary, dir_fd=directory_fd)
        os.fsync(directory_fd)
        return directory / filename
    except FileExistsError as exc:
        raise JournalStateError(
            f"immutable journal destination already exists: {directory / filename}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError:
            # An unpublished temporary is not evidence.  Leave it for operator
            # inspection rather than allowing cleanup failure to hide the
            # original publication exception.
            if linked:
                pass
        os.close(directory_fd)


def _normalized_target_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ReceiptRootError(
            "receipt target origin must be an absolute HTTPS URL without credentials, query, or fragment"
        )
    return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _read_receipt_target(root: Path) -> str:
    marker = root / RECEIPT_ROOT_MARKER
    try:
        info = _validate_evidence_file(marker, "receipt target marker")
        if info.st_size > 4096:
            raise JournalValidationError("receipt target marker is unexpectedly large")
        value = _parse_canonical_json(marker.read_bytes(), marker)
    except (OSError, JournalValidationError) as exc:
        raise ReceiptRootError(f"invalid receipt target marker: {exc}") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "target_origin"}
        or value.get("schema") != RECEIPT_ROOT_SCHEMA
        or not isinstance(value.get("target_origin"), str)
    ):
        raise ReceiptRootError("receipt target marker has an invalid schema")
    return _normalized_target_origin(value["target_origin"])


def initialize_target_receipt_root(
    path: os.PathLike[str] | str,
    repo_root: os.PathLike[str] | str,
    target_origin: str,
) -> Path:
    """Create/pin one receipt root to one production origin."""

    root = validate_receipt_root(path, repo_root)
    target = _normalized_target_origin(target_origin)
    with PromotionLock(root):
        marker = root / RECEIPT_ROOT_MARKER
        if marker.exists() or marker.is_symlink():
            existing = _read_receipt_target(root)
            if existing != target:
                raise ReceiptRootError(
                    f"receipt root is pinned to {existing}, not requested target {target}"
                )
            return root
        attempts = root / "attempts"
        if attempts.exists() and any(attempts.iterdir()):
            raise ReceiptRootError(
                "refusing to pin a receipt root that already contains deployment attempts"
            )
        _publish_immutable(
            root,
            RECEIPT_ROOT_MARKER,
            _canonical_json_bytes(
                {"schema": RECEIPT_ROOT_SCHEMA, "target_origin": target}
            ),
        )
    return root


def validate_target_receipt_root(
    path: os.PathLike[str] | str,
    repo_root: os.PathLike[str] | str,
    target_origin: str,
) -> Path:
    """Require a pre-existing receipt root pinned to the requested target."""

    supplied = Path(path)
    if not supplied.exists() and not supplied.is_symlink():
        raise ReceiptRootError(
            "receipt root must be initialized explicitly before promotion"
        )
    root = validate_receipt_root(supplied, repo_root)
    if not (root / RECEIPT_ROOT_MARKER).exists():
        raise ReceiptRootError(
            "receipt root must be initialized explicitly before promotion"
        )
    expected = _normalized_target_origin(target_origin)
    actual = _read_receipt_target(root)
    if actual != expected:
        raise ReceiptRootError(
            f"receipt root is pinned to {actual}, not promotion target {expected}"
        )
    return root


def _validate_evidence_file(path: Path, label: str) -> os.stat_result:
    value = _lstat(path, label)
    if not stat.S_ISREG(value.st_mode):
        raise JournalValidationError(f"{label} is not a regular file: {path}")
    if value.st_uid != os.geteuid():
        raise JournalValidationError(f"{label} is not owned by this uid: {path}")
    mode = stat.S_IMODE(value.st_mode)
    if mode != 0o400:
        raise JournalValidationError(f"{label} permissions must be 0400, got {mode:04o}: {path}")
    return value


def _validate_unpublished_temporary(path: Path, label: str) -> None:
    value = _lstat(path, label)
    if not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid():
        raise JournalValidationError(f"unsafe {label}: {path}")
    mode = stat.S_IMODE(value.st_mode)
    if mode not in {0o400, 0o600}:
        raise JournalValidationError(f"unsafe {label} permissions {mode:04o}: {path}")


def _digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _blob_refs(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _blob_refs(item)
    elif isinstance(value, dict):
        if value.get("schema") == BLOB_REF_SCHEMA:
            yield value
        for item in value.values():
            yield from _blob_refs(item)


def _validate_attempt_id(attempt_id: object) -> str:
    if not isinstance(attempt_id, str) or ATTEMPT_ID_RE.fullmatch(attempt_id) is None:
        raise JournalStateError(
            "attempt_id must be 1-128 safe filename characters and not start with punctuation"
        )
    return attempt_id


def _event_value(
    attempt_id: str,
    sequence: int,
    previous_event_id: str | None,
    kind: str,
    payload: Mapping[str, Any],
    recorded_at: str | None,
) -> dict[str, Any]:
    if not isinstance(kind, str) or KIND_RE.fullmatch(kind) is None:
        raise JournalStateError(f"invalid event kind: {kind!r}")
    if sequence == 0 and kind != "intent":
        raise JournalStateError("the first deployment event must be intent")
    if sequence > 0 and kind == "intent":
        raise JournalStateError("intent may appear only as the first deployment event")
    if not isinstance(payload, Mapping):
        raise JournalStateError("event payload must be an object")
    payload_value = copy.deepcopy(dict(payload))
    _validate_canonical_type(payload_value, "$.payload")
    timestamp = recorded_at or _now()
    _validate_recorded_at(timestamp)
    event: dict[str, Any] = {
        "schema": EVENT_SCHEMA,
        "attempt_id": attempt_id,
        "sequence": sequence,
        "previous_event_id": previous_event_id,
        "recorded_at": timestamp,
        "kind": kind,
        "payload": payload_value,
    }
    event["event_id"] = _domain_hash(event)
    return event


def _event_filename(event: Mapping[str, Any]) -> str:
    sequence = event["sequence"]
    kind = event["kind"]
    event_id = event["event_id"]
    assert isinstance(sequence, int) and isinstance(kind, str) and isinstance(event_id, str)
    return f"{sequence:06d}-{kind}-{event_id.removeprefix('sha256:')}.json"


def _create_attempt_tree(attempt_dir: Path, attempts_dir: Path) -> tuple[Path, Path]:
    attempt_dir.mkdir(mode=0o700)
    _validate_private_directory(attempt_dir, "attempt directory")
    _fsync_directory(attempts_dir)
    events = attempt_dir / "events"
    blobs = attempt_dir / "blobs"
    blob_store = blobs / "sha256"
    _mkdir_durable(events, attempt_dir)
    _mkdir_durable(blobs, attempt_dir)
    _mkdir_durable(blob_store, blobs)
    return events, blob_store


class EventJournal:
    """One deployment attempt's immutable event chain and evidence blobs."""

    def __init__(self, root: Path, attempt_dir: Path, events: list[dict[str, Any]]) -> None:
        self._root = root
        self._attempt_dir = attempt_dir
        self._attempt_id = attempt_dir.name
        self._events = events

    @classmethod
    def create_with_intent(
        cls,
        root: os.PathLike[str] | str,
        attempt_id: str,
        payload: Mapping[str, Any],
        *,
        recorded_at: str | None = None,
    ) -> "EventJournal":
        """Atomically publish a new attempt whose durable first event is intent."""

        root_path = _validate_existing_root(Path(root))
        _require_lock(root_path)
        attempt_id = _validate_attempt_id(attempt_id)
        attempts = root_path / "attempts"
        _mkdir_durable(attempts, root_path)
        final = attempts / attempt_id
        if final.exists() or final.is_symlink():
            raise JournalStateError(f"deployment attempt already exists: {attempt_id}")

        pending = attempts / f".pending-attempt-{os.getpid()}-{secrets.token_hex(16)}"
        events, blob_store = _create_attempt_tree(pending, attempts)
        event = _event_value(attempt_id, 0, None, "intent", payload, recorded_at)
        for reference in _blob_refs(event["payload"]):
            cls._validate_blob_ref(reference, pending, blob_store)
        _publish_immutable(events, _event_filename(event), _canonical_json_bytes(event))
        _fsync_directory(events)
        _fsync_directory(blob_store)
        _fsync_directory(pending / "blobs")
        _fsync_directory(pending)
        try:
            os.rename(pending, final)
        except OSError as exc:
            raise JournalStateError(f"cannot publish deployment attempt {attempt_id}: {exc}") from exc
        _fsync_directory(attempts)
        return cls.load(final)

    @classmethod
    def load(cls, attempt_dir: os.PathLike[str] | str) -> "EventJournal":
        supplied = Path(attempt_dir)
        if not supplied.is_absolute():
            raise JournalValidationError("attempt directory must be an absolute path")
        if supplied.is_symlink():
            raise JournalValidationError(f"attempt directory must not be a symlink: {supplied}")
        attempt_dir_path = supplied.resolve(strict=True)
        if attempt_dir_path.parent.name != "attempts":
            raise JournalValidationError(
                f"attempt directory must live under attempts/: {attempt_dir_path}"
            )
        root = _validate_existing_root(attempt_dir_path.parent.parent)
        if ATTEMPT_ID_RE.fullmatch(attempt_dir_path.name) is None:
            raise JournalValidationError(f"unsafe attempt directory name: {attempt_dir_path.name}")
        _validate_private_directory(attempt_dir_path.parent, "attempts directory")
        _validate_private_directory(attempt_dir_path, "attempt directory")
        events_dir = attempt_dir_path / "events"
        blobs_dir = attempt_dir_path / "blobs"
        blob_store = blobs_dir / "sha256"
        _validate_private_directory(events_dir, "events directory")
        _validate_private_directory(blobs_dir, "blobs directory")
        _validate_private_directory(blob_store, "blob store")
        expected_entries = {events_dir, blobs_dir}
        for child in attempt_dir_path.iterdir():
            if child not in expected_entries:
                raise JournalValidationError(f"unexpected entry in attempt directory: {child}")
        for child in blobs_dir.iterdir():
            if child != blob_store:
                raise JournalValidationError(f"unexpected entry in blobs directory: {child}")
        cls._validate_blob_store(blob_store)
        events = cls._load_events(attempt_dir_path, events_dir, blob_store)
        return cls(root, attempt_dir_path, events)

    @staticmethod
    def _validate_blob_store(blob_store: Path) -> None:
        for child in blob_store.iterdir():
            if child.name.startswith(".pending-"):
                _validate_unpublished_temporary(child, "unpublished blob temporary")
                continue
            if re.fullmatch(r"[0-9a-f]{64}", child.name) is None:
                raise JournalValidationError(f"unexpected entry in blob store: {child}")
            file_stat = _validate_evidence_file(child, "deployment blob")
            digest, size = _digest_file(child)
            if digest != child.name or size != file_stat.st_size:
                raise JournalValidationError(f"deployment blob filename/digest mismatch: {child}")

    @staticmethod
    def _load_events(
        attempt_dir: Path,
        events_dir: Path,
        blob_store: Path,
    ) -> list[dict[str, Any]]:
        event_paths: list[Path] = []
        for child in events_dir.iterdir():
            if child.name.startswith(".pending-"):
                _validate_unpublished_temporary(child, "unpublished event temporary")
                continue
            if EVENT_FILE_RE.fullmatch(child.name) is None:
                raise JournalValidationError(f"unexpected entry in events directory: {child}")
            event_paths.append(child)
        event_paths.sort(key=lambda path: path.name)

        events: list[dict[str, Any]] = []
        previous: str | None = None
        terminal_seen = False
        for expected_sequence, path in enumerate(event_paths):
            match = EVENT_FILE_RE.fullmatch(path.name)
            assert match is not None
            _validate_evidence_file(path, "event file")
            value = _parse_canonical_json(path.read_bytes(), path)
            if not isinstance(value, dict) or set(value) != _EVENT_KEYS:
                raise JournalValidationError(f"event has an invalid field set: {path}")
            if value.get("schema") != EVENT_SCHEMA:
                raise JournalValidationError(f"event schema mismatch: {path}")
            if value.get("attempt_id") != attempt_dir.name:
                raise JournalValidationError(f"event attempt_id mismatch: {path}")
            sequence = value.get("sequence")
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence != expected_sequence
            ):
                raise JournalValidationError(f"event sequence is not contiguous at {path}")
            if int(match.group("sequence")) != sequence:
                raise JournalValidationError(f"event filename sequence mismatch: {path}")
            kind = value.get("kind")
            if (
                not isinstance(kind, str)
                or KIND_RE.fullmatch(kind) is None
                or match.group("kind") != kind
            ):
                raise JournalValidationError(f"event kind or filename kind is invalid: {path}")
            if sequence == 0 and kind != "intent":
                raise JournalValidationError("the first deployment event must be intent")
            if sequence > 0 and kind == "intent":
                raise JournalValidationError("intent may appear only as the first deployment event")
            if terminal_seen:
                raise JournalValidationError("events exist after final_state")
            if value.get("previous_event_id") != previous:
                raise JournalValidationError(f"event previous_event_id mismatch: {path}")
            _validate_recorded_at(value.get("recorded_at"))
            if not isinstance(value.get("payload"), dict):
                raise JournalValidationError(f"event payload must be an object: {path}")
            event_id = value.get("event_id")
            if not isinstance(event_id, str) or HASH_RE.fullmatch(event_id) is None:
                raise JournalValidationError(f"invalid event_id: {path}")
            projection = dict(value)
            del projection["event_id"]
            if _domain_hash(projection) != event_id:
                raise JournalValidationError(f"event hash mismatch: {path}")
            if match.group("digest") != event_id.removeprefix("sha256:"):
                raise JournalValidationError(f"event filename hash mismatch: {path}")
            for reference in _blob_refs(value["payload"]):
                EventJournal._validate_blob_ref(reference, attempt_dir, blob_store)
            events.append(value)
            previous = event_id
            terminal_seen = kind == FINAL_EVENT_KIND
        return events

    @staticmethod
    def _validate_blob_ref(reference: dict[str, Any], attempt_dir: Path, blob_store: Path) -> None:
        if set(reference) != _BLOB_REF_KEYS:
            raise JournalValidationError("deployment blob reference has an invalid field set")
        name = reference.get("name")
        digest = reference.get("sha256")
        path = reference.get("path")
        size = reference.get("bytes")
        media_type = reference.get("media_type")
        if not isinstance(name, str) or BLOB_NAME_RE.fullmatch(name) is None:
            raise JournalValidationError("deployment blob reference has an invalid name")
        if not isinstance(digest, str) or HASH_RE.fullmatch(digest) is None:
            raise JournalValidationError("deployment blob reference has an invalid digest")
        digest_hex = digest.removeprefix("sha256:")
        expected_path = f"blobs/sha256/{digest_hex}"
        if path != expected_path:
            raise JournalValidationError("deployment blob reference path does not match its digest")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_SAFE_INTEGER:
            raise JournalValidationError("deployment blob reference has an invalid byte count")
        if not isinstance(media_type, str) or MEDIA_TYPE_RE.fullmatch(media_type) is None:
            raise JournalValidationError("deployment blob reference has an invalid media type")
        blob_path = attempt_dir / expected_path
        if blob_path.parent != blob_store:
            raise JournalValidationError("deployment blob escaped the attempt blob store")
        file_stat = _validate_evidence_file(blob_path, "deployment blob")
        if file_stat.st_size != size:
            raise JournalValidationError(f"deployment blob byte count mismatch: {blob_path}")
        actual_digest, actual_size = _digest_file(blob_path)
        if actual_size != size or actual_digest != digest_hex:
            raise JournalValidationError(f"deployment blob digest mismatch: {blob_path}")

    def _reload(self) -> None:
        loaded = self.load(self.attempt_dir)
        self._events = loaded._events

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(self._events))

    @property
    def root(self) -> Path:
        return self._root

    @property
    def attempt_dir(self) -> Path:
        return self._attempt_dir

    @property
    def attempt_id(self) -> str:
        return self._attempt_id

    @property
    def chain_tip(self) -> str | None:
        if not self._events:
            return None
        return str(self._events[-1]["event_id"])

    @property
    def terminal(self) -> bool:
        return bool(self._events and self._events[-1]["kind"] == FINAL_EVENT_KIND)

    @property
    def incomplete(self) -> bool:
        return not self.terminal

    def append(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        _require_lock(self.root)
        self._reload()
        if self.terminal:
            raise JournalStateError(f"deployment attempt {self.attempt_id} is already final")
        sequence = len(self._events)
        if sequence > 999_999:
            raise JournalStateError("deployment attempt exceeds the event sequence limit")
        event = _event_value(
            self.attempt_id,
            sequence,
            self.chain_tip,
            kind,
            payload,
            recorded_at,
        )
        blob_store = self.attempt_dir / "blobs" / "sha256"
        for reference in _blob_refs(event["payload"]):
            self._validate_blob_ref(reference, self.attempt_dir, blob_store)
        _publish_immutable(
            self.attempt_dir / "events",
            _event_filename(event),
            _canonical_json_bytes(event),
        )
        self._reload()
        return copy.deepcopy(self._events[-1])

    def append_blob(
        self,
        name: str,
        data: bytes,
        media_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        _require_lock(self.root)
        self._reload()
        if self.terminal:
            raise JournalStateError(f"deployment attempt {self.attempt_id} is already final")
        if not isinstance(name, str) or BLOB_NAME_RE.fullmatch(name) is None:
            raise JournalStateError(f"invalid blob name: {name!r}")
        if not isinstance(data, bytes):
            raise JournalStateError("blob data must be bytes")
        if not isinstance(media_type, str) or MEDIA_TYPE_RE.fullmatch(media_type) is None:
            raise JournalStateError(f"invalid blob media type: {media_type!r}")
        digest_hex = hashlib.sha256(data).hexdigest()
        destination = self.attempt_dir / "blobs" / "sha256" / digest_hex
        if destination.exists() or destination.is_symlink():
            _validate_evidence_file(destination, "deployment blob")
            existing_digest, existing_size = _digest_file(destination)
            if existing_digest != digest_hex or existing_size != len(data):
                raise JournalValidationError(f"existing deployment blob is corrupt: {destination}")
        else:
            _publish_immutable(destination.parent, digest_hex, data)
        return {
            "schema": BLOB_REF_SCHEMA,
            "name": name,
            "path": f"blobs/sha256/{digest_hex}",
            "sha256": f"sha256:{digest_hex}",
            "bytes": len(data),
            "media_type": media_type,
        }


def _attempt_directories(root: Path) -> list[Path]:
    attempts = root / "attempts"
    if not attempts.exists():
        return []
    _validate_private_directory(attempts, "attempts directory")
    result: list[Path] = []
    for child in attempts.iterdir():
        if child.name.startswith(".pending-attempt-"):
            _validate_private_directory(child, "unpublished attempt temporary")
            continue
        if child.is_symlink() or not child.is_dir():
            raise JournalValidationError(f"unexpected entry in attempts directory: {child}")
        result.append(child)
    return sorted(result, key=lambda path: path.name)


def list_incomplete_attempts(root: os.PathLike[str] | str) -> list[EventJournal]:
    """Load and validate every attempt, returning only non-terminal chains."""

    root_path = _validate_existing_root(Path(root))
    journals = [EventJournal.load(path) for path in _attempt_directories(root_path)]
    return [journal for journal in journals if journal.incomplete]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "verify"):
        command = subparsers.add_parser(name)
        command.add_argument("--receipt-dir", type=Path, required=True)
        command.add_argument("--repo-root", type=Path, required=True)
        command.add_argument("--target-origin", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            root = initialize_target_receipt_root(
                args.receipt_dir, args.repo_root, args.target_origin
            )
        else:
            root = validate_target_receipt_root(
                args.receipt_dir, args.repo_root, args.target_origin
            )
        incomplete = [journal.attempt_id for journal in list_incomplete_attempts(root)]
    except (OSError, JournalError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "schema": RECEIPT_ROOT_SCHEMA,
                "receipt_dir": str(root),
                "target_origin": _read_receipt_target(root),
                "incomplete_attempts": incomplete,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
