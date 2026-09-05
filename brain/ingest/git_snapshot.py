#!/usr/bin/env python3
"""Read UTF-8 files from one immutable Git commit without using the worktree.

The ingest harvesters consume public repositories whose working trees may be
dirty or may move while a harvest is running.  This module captures
``HEAD^{commit}`` once, enumerates an exact scope from that object, and reads
the selected blobs through one local ``git cat-file --batch`` process.  It
never consults the index or working-tree file bytes.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence

_REGULAR_MODES = frozenset({"100644", "100755"})
# WikiLean's source-plan and normalized-ingest contracts currently require
# SHA-1 Git commit pins.  Reject a SHA-256 repository here instead of emitting
# a snapshot that its consumers cannot record.
_OID = re.compile(r"[0-9a-f]{40}\Z")
_HEADER_LIMIT = 256
MAX_FILES = 50_000
MAX_BLOB_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024


class GitSnapshotError(RuntimeError):
    """The requested immutable Git snapshot could not be read safely."""


@dataclass(frozen=True, slots=True)
class GitTextFile:
    """One repository-relative UTF-8 file from the captured commit."""

    path: str
    text: str


@dataclass(frozen=True, slots=True)
class GitTextSnapshot:
    """A captured commit and its ordered, requested text files."""

    commit: str
    files: tuple[GitTextFile, ...]


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    path: str
    mode: str
    object_type: str
    oid: str


def _git_environment() -> dict[str, str]:
    """Use a minimal deterministic environment with no inherited selectors."""
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "TMPDIR", "TMP", "TEMP")
        if key in os.environ
    }
    return {
        **environment,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
        "LC_ALL": "C",
    }


def _validated_git(git: str | os.PathLike[str]) -> str:
    value = os.fspath(git)
    if not value or "\0" in value:
        raise GitSnapshotError("git executable must be a nonempty path or command")
    candidate = shutil.which(value) if os.sep not in value else value
    if candidate is None:
        raise GitSnapshotError(f"Git executable is unavailable: {value}")
    try:
        resolved = Path(candidate).resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError) as exc:
        raise GitSnapshotError(f"Git executable is inaccessible: {value}") from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise GitSnapshotError(f"Git executable is not a regular executable: {resolved}")
    return str(resolved)


def _command(git: str, repository: Path, arguments: Sequence[str]) -> list[str]:
    return [git, "--no-replace-objects", "-C", str(repository), *arguments]


def _run_git(
    git: str,
    repository: Path,
    arguments: Sequence[str],
    *,
    allow_exit_one: bool = False,
) -> bytes:
    try:
        process = subprocess.run(
            _command(git, repository, arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(),
            check=False,
        )
    except OSError as exc:
        raise GitSnapshotError(f"cannot run Git: {exc}") from exc
    if allow_exit_one and process.returncode == 1:
        return b""
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", "replace").strip()[-500:]
        suffix = f": {detail}" if detail else ""
        raise GitSnapshotError(
            f"Git command failed with status {process.returncode}{suffix}"
        )
    return process.stdout


def _validated_repository(repository: str | os.PathLike[str]) -> Path:
    try:
        resolved = Path(repository).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GitSnapshotError(f"repository is not accessible: {repository}") from exc
    if not resolved.is_dir():
        raise GitSnapshotError(f"repository is not a directory: {resolved}")
    return resolved


def _validate_scope(scope: str) -> str:
    if (
        not isinstance(scope, str)
        or not scope
        or "\\" in scope
        or unicodedata.normalize("NFC", scope) != scope
        or any(unicodedata.category(character).startswith("C") for character in scope)
    ):
        raise GitSnapshotError(
            "scope must be a normalized repository-relative POSIX path"
        )
    path = PurePosixPath(scope)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in scope.split("/")):
        raise GitSnapshotError("scope must be a normalized repository-relative POSIX path")
    normalized = path.as_posix()
    if normalized != scope:
        raise GitSnapshotError("scope must be a normalized repository-relative POSIX path")
    return normalized


def _validate_suffixes(suffixes: Sequence[str] | None) -> tuple[str, ...] | None:
    if suffixes is None:
        return None
    if isinstance(suffixes, (str, bytes)):
        raise GitSnapshotError("suffixes must be a nonempty sequence of strings")
    result = tuple(suffixes)
    if not result or any(
        not isinstance(suffix, str)
        or not suffix
        or "/" in suffix
        or "\\" in suffix
        or unicodedata.normalize("NFC", suffix) != suffix
        or any(unicodedata.category(character).startswith("C") for character in suffix)
        for suffix in result
    ):
        raise GitSnapshotError("suffixes must be a nonempty sequence of strings")
    if len(set(result)) != len(result):
        raise GitSnapshotError("suffixes must not contain duplicates")
    return result


def _decode_single_line(raw: bytes, description: str, encoding: str) -> str:
    try:
        value = raw.decode(encoding, "strict").strip()
    except UnicodeDecodeError as exc:
        raise GitSnapshotError(f"Git returned an invalid {description}") from exc
    if not value or "\n" in value or "\r" in value:
        raise GitSnapshotError(f"Git returned an invalid {description}")
    return value


def _reject_partial_clone(git: str, repository: Path) -> None:
    raw = _run_git(
        git,
        repository,
        ["config", "--includes", "--local", "--null", "--list"],
    )
    for record in raw.split(b"\0"):
        if not record:
            continue
        encoded_key, separator, _value = record.partition(b"\n")
        if not separator:
            raise GitSnapshotError("Git returned malformed local configuration")
        try:
            key = encoded_key.decode("utf-8", "strict").lower()
        except UnicodeDecodeError as exc:
            raise GitSnapshotError("Git returned a non-UTF-8 configuration key") from exc
        if key == "extensions.partialclone" or (
            key.startswith("remote.")
            and (key.endswith(".promisor") or key.endswith(".partialclonefilter"))
        ):
            raise GitSnapshotError(
                "promisor and partial-clone repositories are not supported"
            )


def _capture_commit(git: str, repository: Path) -> str:
    raw = _run_git(git, repository, ["rev-parse", "--verify", "HEAD^{commit}"])
    commit = _decode_single_line(raw, "commit object ID", "ascii")
    if _OID.fullmatch(commit) is None:
        raise GitSnapshotError(f"Git returned an invalid commit object ID: {commit!r}")
    return commit


def _require_top_level(git: str, repository: Path) -> None:
    raw = _run_git(git, repository, ["rev-parse", "--show-toplevel"])
    top_text = _decode_single_line(raw, "worktree root", "utf-8")
    try:
        top = Path(top_text).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GitSnapshotError("Git returned an inaccessible worktree root") from exc
    if top != repository:
        raise GitSnapshotError(
            f"repository must be the worktree top level ({top}), not {repository}"
        )


def _tree_entries(
    git: str,
    repository: Path,
    commit: str,
    scope: str,
) -> tuple[_TreeEntry, ...]:
    raw = _run_git(
        git,
        repository,
        [
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            commit,
            "--",
            f":(literal){scope}",
        ],
    )
    entries: list[_TreeEntry] = []
    seen: set[str] = set()
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            header, encoded_path = record.split(b"\t", 1)
            mode, object_type, oid = header.decode("ascii", "strict").split(" ")
            path = encoded_path.decode("utf-8", "strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise GitSnapshotError("Git returned malformed ls-tree output") from exc
        if _OID.fullmatch(oid) is None:
            raise GitSnapshotError(f"Git returned an invalid object ID for {path!r}")
        if path in seen:
            raise GitSnapshotError(f"Git returned duplicate tree path {path!r}")
        if path != scope and not path.startswith(scope + "/"):
            raise GitSnapshotError(
                f"Git returned a path outside the requested scope: {path!r}"
            )
        if (
            unicodedata.normalize("NFC", path) != path
            or any(unicodedata.category(character).startswith("C") for character in path)
        ):
            raise GitSnapshotError(f"Git returned an unsafe path: {path!r}")
        seen.add(path)
        entries.append(_TreeEntry(path, mode, object_type, oid))
    return tuple(sorted(entries, key=lambda entry: entry.path))


def _select_entries(
    entries: tuple[_TreeEntry, ...],
    scope: str,
    suffixes: tuple[str, ...] | None,
) -> tuple[_TreeEntry, ...]:
    if not entries:
        raise GitSnapshotError(f"scope is absent from the captured commit: {scope}")
    for entry in entries:
        if entry.object_type != "blob" or entry.mode not in _REGULAR_MODES:
            raise GitSnapshotError(
                f"Git path is not a regular blob: {entry.path} "
                f"(mode={entry.mode}, type={entry.object_type})"
            )
    if suffixes is None:
        if len(entries) != 1 or entries[0].path != scope:
            raise GitSnapshotError(f"scope is not one exact file: {scope}")
        return entries
    if any(entry.path == scope for entry in entries):
        raise GitSnapshotError(f"scope is not a directory: {scope}")
    selected = tuple(entry for entry in entries if entry.path.endswith(suffixes))
    if len(selected) > MAX_FILES:
        raise GitSnapshotError(
            f"snapshot selects {len(selected)} files, above the {MAX_FILES} file limit"
        )
    return selected


def _read_exact(stream: object, size: int, path: str) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(min(remaining, 1024 * 1024))  # type: ignore[attr-defined]
        if not chunk:
            raise GitSnapshotError(f"Git ended inside blob payload for {path}")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_text_blobs(
    git: str,
    repository: Path,
    entries: tuple[_TreeEntry, ...],
) -> tuple[GitTextFile, ...]:
    if not entries:
        return ()
    try:
        process = subprocess.Popen(
            _command(git, repository, ["cat-file", "--batch"]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(),
        )
    except OSError as exc:
        raise GitSnapshotError(f"cannot start Git batch reader: {exc}") from exc

    files: list[GitTextFile] = []
    total_size = 0
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        for entry in entries:
            process.stdin.write(entry.oid.encode("ascii") + b"\n")
            process.stdin.flush()
            header = process.stdout.readline(_HEADER_LIMIT + 1)
            if not header or not header.endswith(b"\n") or len(header) > _HEADER_LIMIT:
                raise GitSnapshotError(
                    f"Git returned an invalid batch header for {entry.path}"
                )
            fields = header[:-1].split(b" ")
            if len(fields) == 2 and fields[1] in {b"missing", b"ambiguous"}:
                raise GitSnapshotError(f"Git object is unavailable for {entry.path}")
            if len(fields) != 3:
                raise GitSnapshotError(
                    f"Git returned a malformed batch header for {entry.path}"
                )
            encoded_oid, object_type, encoded_size = fields
            try:
                actual_oid = encoded_oid.decode("ascii", "strict")
                size = int(encoded_size.decode("ascii", "strict"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise GitSnapshotError(
                    f"Git returned an invalid object identity or size for {entry.path}"
                ) from exc
            if actual_oid != entry.oid or object_type != b"blob" or size < 0:
                raise GitSnapshotError(
                    f"Git returned the wrong object, type, or size for {entry.path}"
                )
            if size > MAX_BLOB_BYTES:
                raise GitSnapshotError(
                    f"Git blob exceeds the {MAX_BLOB_BYTES}-byte limit: {entry.path}"
                )
            total_size += size
            if total_size > MAX_TOTAL_BYTES:
                raise GitSnapshotError(
                    f"Git snapshot exceeds the {MAX_TOTAL_BYTES}-byte aggregate limit"
                )
            payload = _read_exact(process.stdout, size, entry.path)
            if len(payload) != size or process.stdout.read(1) != b"\n":
                raise GitSnapshotError(
                    f"Git returned an invalid blob payload for {entry.path}"
                )
            try:
                text = payload.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise GitSnapshotError(f"Git blob is not UTF-8: {entry.path}") from exc
            files.append(GitTextFile(entry.path, text))

        process.stdin.close()
        process.stdin = None
        remaining_stdout, stderr = process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()[-500:]
            suffix = f": {detail}" if detail else ""
            raise GitSnapshotError(
                f"Git batch reader failed with status {process.returncode}{suffix}"
            )
        if remaining_stdout:
            raise GitSnapshotError("Git batch reader returned unexpected trailing output")
    except OSError as exc:
        if process.poll() is None:
            process.kill()
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
            process.stdin = None
        process.communicate()
        raise GitSnapshotError(f"Git batch reader I/O failed: {exc}") from exc
    except BaseException:
        if process.poll() is None:
            process.kill()
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
            process.stdin = None
        process.communicate()
        raise
    return tuple(files)


def read_text_snapshot(
    repository: str | os.PathLike[str],
    *,
    scope: str,
    suffixes: Sequence[str] | None = None,
    git: str | os.PathLike[str] = "git",
) -> GitTextSnapshot:
    """Return selected UTF-8 files from one captured ``HEAD`` commit.

    With ``suffixes=None``, ``scope`` must name one exact regular file.  With
    a nonempty suffix sequence, ``scope`` must name a directory and every
    regular file below it whose path has one of those suffixes is returned.
    Any symlink, gitlink, or non-blob entry inside the enumerated scope is a
    hard failure, including entries that do not have a requested suffix.
    """
    root = _validated_repository(repository)
    normalized_scope = _validate_scope(scope)
    normalized_suffixes = _validate_suffixes(suffixes)
    git_command = _validated_git(git)

    _require_top_level(git_command, root)
    _reject_partial_clone(git_command, root)
    commit = _capture_commit(git_command, root)
    entries = _tree_entries(git_command, root, commit, normalized_scope)
    selected = _select_entries(entries, normalized_scope, normalized_suffixes)
    files = _read_text_blobs(git_command, root, selected)
    return GitTextSnapshot(commit=commit, files=files)


__all__ = [
    "GitSnapshotError",
    "GitTextFile",
    "GitTextSnapshot",
    "read_text_snapshot",
]
