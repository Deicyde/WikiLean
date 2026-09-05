"""Shared plumbing for brain/ingest/<db>.py external-source adapters.

Contract (brain/SCHEMA.md "External-source ingest contract"):
  catalog/data/external/<db>_pages.jsonl   {"db","id","title","url","snippet"?,"aliases"?,"qid"?,"kind_hint"?}
  catalog/data/external/<db>_links.jsonl   {"db","src","dst","context"}
First line of each file is {"_meta": {...}}. The two files share a content-bound
generation envelope. Links publish first and pages last (the commit point), so
a crash can leave a detectable mismatch but cannot expose a mixed generation
through the validating reader. Writes fail-soft rather than truncating a good file.

Adapters must be deterministic (no LLM), honor source rate limits, and set `qid`
only from CC0 Wikidata property values — never guessed.
"""
from __future__ import annotations

import hashlib
import json
import fcntl
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

_BRAIN_DIR = Path(__file__).resolve().parents[1]
if str(_BRAIN_DIR) not in sys.path:
    sys.path.insert(0, str(_BRAIN_DIR))

from build_context import (  # noqa: E402
    EXTERNAL_PAIR_META_FIELDS,
    EXTERNAL_PAIR_SCHEMA,
    EXTERNAL_PAIR_TRANSACTION_SCHEMA,
    ExternalPairError,
    external_pair_control_paths,
    external_pair_db_from_journal,
    seal_external_pair_meta,
    validate_external_pair,
)

REPO = Path(__file__).resolve().parents[2]
EXTERNAL_DIR = REPO / "catalog" / "data" / "external"
CACHE_DIR = REPO / "catalog" / ".cache" / "external"
CROSSREFS = REPO / "catalog" / "data" / "wikidata_crossrefs.json"

USER_AGENT = "WikiLean-brain/2.0 (https://wikilean.jackmccarthy.org; contact via GitHub Deicyde/WikiLean)"

# Sources whose licenses permit storing short snippets (SCHEMA.md ext payload rules).
SNIPPET_OK = {"nlab", "stacks", "lmfdb_knowl", "proofwiki", "planetmath", "oeis"}
SNIPPET_LICENSE = {
    "nlab": "nLab (attribution, no formal license)",
    "stacks": "GFDL (Stacks Project)",
    "lmfdb_knowl": "CC-BY-SA-4.0 (LMFDB)",
    "proofwiki": "CC-BY-SA-3.0 (ProofWiki)",
    "planetmath": "CC-BY-SA (PlanetMath)",
    "oeis": "CC-BY-SA-4.0 (OEIS)",
}
SNIPPET_MAX = 600  # chars, hard cap after cleanup

# Normalized pair metadata is source/content description, not run telemetry.
# Keep this allowlist explicit so a new adapter cannot accidentally make pack
# bytes depend on clocks, retry counts, cache warmth, or environment knobs.
EXTERNAL_PAIR_EXTRA_META_FIELDS = frozenset({
    "n_aliases",
    "n_anchored",
    "n_chapters",
    "n_collapsed",
    "n_junk_skipped",
    "n_links_unresolved",
    "n_names_inventory",
    "n_qid_joined",
    "n_refs_raw",
    "n_refs_unresolved",
    "n_redirects",
    "n_sections_enumerated",
    "n_skipped",
    "n_snippets",
    "n_tex",
    "n_with_qid",
    "sitemap_inventory",
    "source_license",
    "source_pin",
})

# ``write_jsonl`` is shared only by the standalone normalized replay inputs
# below. Keep their metadata closed for the same reason as external pairs:
# clocks, request counts, cache state, and run-local statistics belong in
# acquisition receipts/audit logs rather than content-addressed input bytes.
STANDALONE_JSONL_META_FIELDS = frozenset({
    "commit",
    "db",
    "lib",
    "license",
    "meaning",
    "n_arxiv_ids",
    "n_decls",
    "n_files",
    "n_links",
    "n_problems",
    "n_research",
    "n_skipped_non_arxiv",
    "repo",
    "source",
    "source_pin",
})

_WS = re.compile(r"\s+")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_sha256_pin(path: Path) -> str:
    """Return a content-derived source pin that is independent of path/mtime."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def clean_snippet(text: str, limit: int = SNIPPET_MAX) -> str:
    """Whitespace-normalize and hard-cap a snippet; keep inline $TeX$ as-is."""
    text = _WS.sub(" ", text or "").strip()
    if len(text) > limit:
        cut = text[:limit]
        # cut at a sentence or word boundary when possible; spaceless text
        # (URLs, data: blobs) must still land under the cap
        dot = cut.rfind(". ")
        if dot > limit // 2:
            text = cut[: dot + 1]
        else:
            head = cut.rsplit(" ", 1)[0] if " " in cut else cut[: limit - 1]
            text = head[: limit - 1] + "…"
    return text


def fetch(url: str, *, timeout: int = 60, delay: float = 0.0, retries: int = 3) -> bytes:
    """Polite GET with our UA. `delay` sleeps BEFORE the request (rate limiting)."""
    last: Exception | None = None
    for attempt in range(retries):
        if delay:
            time.sleep(delay)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001 — retry then re-raise
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"fetch failed after {retries} tries: {url}: {last}")


def curl_fetch(url: str, *, timeout: int = 120) -> bytes:
    """curl fallback (the system Python SSL trust store is broken on this machine
    for some hosts — same workaround as catalog/mathlib_deps/fetch_crossrefs.py)."""
    out = subprocess.run(
        ["curl", "-sfL", "--max-time", str(timeout), "-A", USER_AGENT, url],
        capture_output=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"curl failed ({out.returncode}): {url}: {out.stderr[:200]!r}")
    return out.stdout


def qid_map(db_key: str) -> dict[str, str]:
    """external-id -> QID from catalog/data/wikidata_crossrefs.json (CC0 seeds).

    db_key is the crossrefs xref key (e.g. 'nlab', 'mathworld', 'lmfdb_knowl').
    Multi-valued xrefs map each id; on collision the first (lowest QID) wins.
    """
    data = json.loads(CROSSREFS.read_text())
    out: dict[str, str] = {}
    for qid in sorted(data.get("xrefs", {}), key=lambda q: (len(q), q)):
        for ext_id in data["xrefs"][qid].get(db_key, []):
            out.setdefault(str(ext_id), qid)
    return out


def strip_controls(s: str) -> str:
    """Drop every Unicode category-C codepoint (Cc/Cf/Cs/Co/Cn — control chars,
    zero-widths like U+200B/U+200E/U+200F, BOM). The Worker's BRAIN_ID_RE
    (wiki/src/brain.ts) rejects any \\p{C} character, so an id carrying one
    would be unreachable as a node."""
    return "".join(c for c in s if not unicodedata.category(c).startswith("C"))


def _read_jsonl_artifact(path: Path) -> tuple[dict, list[dict]]:
    """Read a JSONL artifact while retaining its optional legacy metadata."""
    meta: dict | None = None
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ExternalPairError(
                    f"{path.name}:{lineno}: invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(record, dict):
                raise ExternalPairError(
                    f"{path.name}:{lineno}: JSONL row must be an object"
                )
            if "_meta" in record:
                if meta is not None or rows or set(record) != {"_meta"} \
                        or not isinstance(record["_meta"], dict):
                    raise ExternalPairError(
                        f"{path.name}:{lineno}: _meta must be the first and only "
                        "metadata row"
                    )
                meta = record["_meta"]
                continue
            rows.append(record)
    return meta or {}, rows


def read_external_pair(
    db: str,
    pages_path: Path,
    links_path: Path | None,
) -> tuple[dict, list[dict], dict | None, list[dict]]:
    """Read and validate one external pair before exposing either artifact."""
    pages_meta, pages = _read_jsonl_artifact(pages_path)
    if links_path is not None and links_path.exists():
        links_meta, links = _read_jsonl_artifact(links_path)
    else:
        links_meta, links = None, []
    validate_external_pair(db, pages_meta, pages, links_meta, links)
    return pages_meta, pages, links_meta, links


def _prev_rows(path: Path) -> int | None:
    """Data-row count (first-line _meta excluded) of a previous output file,
    or None when the file does not exist (volume guard skipped)."""
    if not path.exists():
        return None
    n = 0
    first = True
    with path.open() as f:
        for line in f:
            if not line.strip():
                continue
            if first:
                first = False
                if '"_meta"' in line[:12]:
                    continue
            n += 1
    return n


def _volume_guard(path: Path, kind: str, new_n: int) -> None:
    """Volume sanity floor: refuse to clobber a known-good dataset with a
    suspiciously small one (the signature of a partial-success ingest that
    would otherwise pass the 0-row refusal). Floor = max(50, prev//2), capped
    at prev so a small-but-legitimate dataset (e.g. oeis' ~38 anchored pages)
    can still re-emit at the same size. Override with BRAIN_INGEST_FORCE=1."""
    prev = _prev_rows(path)
    if prev is None:
        return
    floor = min(max(50, prev // 2), prev)
    if new_n < floor and os.environ.get("BRAIN_INGEST_FORCE") != "1":
        raise RuntimeError(
            f"refusing to overwrite {path.name}: new {kind} count {new_n} is below "
            f"the sanity floor {floor} (previous file has {prev} rows) — looks like "
            f"a partial-success ingest; set BRAIN_INGEST_FORCE=1 to override")


def write_jsonl(path: Path, meta: dict, rows: list[dict], *,
                allow_empty: bool = False) -> None:
    """Atomic jsonl write, first line _meta. Refuses to clobber with an empty
    set unless allow_empty (deliberate meta-only links files)."""
    if not isinstance(meta, dict):
        raise ValueError("normalized JSONL metadata must be an object")
    unsupported = sorted(set(meta) - STANDALONE_JSONL_META_FIELDS)
    if unsupported:
        raise ValueError(
            "metadata fields are not normalized-data fields: "
            + ", ".join(unsupported)
        )
    if not any(
        isinstance(meta.get(field), str) and bool(meta[field])
        for field in ("source", "db")
    ):
        raise ValueError("normalized JSONL metadata requires nonempty source or db")
    if not isinstance(meta.get("license"), str) or not meta["license"]:
        raise ValueError("normalized JSONL metadata requires nonempty license")
    for field in ("db", "lib", "meaning", "repo", "source", "source_pin"):
        if field in meta and (
            not isinstance(meta[field], str) or not meta[field]
        ):
            raise ValueError(f"normalized JSONL metadata {field} must be nonempty text")
    if "commit" in meta and (
        not isinstance(meta["commit"], str)
        or re.fullmatch(r"[0-9a-f]{40}", meta["commit"]) is None
    ):
        raise ValueError("normalized JSONL metadata commit must be a full Git commit")
    for field, value in meta.items():
        if field.startswith("n_") and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError(f"normalized JSONL metadata {field} must be a nonnegative integer")
    if not rows and not allow_empty:
        raise RuntimeError(f"refusing to write 0 rows to {path} (fail-soft)")
    tmp = _stage_jsonl(path, meta, rows)
    try:
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _stage_jsonl(path: Path, meta: dict, rows: list[dict]) -> Path:
    """Write and fsync a uniquely named sibling without publishing it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fd = -1
            fh.write(json.dumps(
                {"_meta": meta}, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ) + "\n")
            for row in rows:
                fh.write(json.dumps(
                    row, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False,
                ) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _fsync_directory(path: Path) -> None:
    """Durably order sibling renames across a power loss."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def external_pair_lock(directory: Path, db: str, *, exclusive: bool):
    """Serialize publishers and give direct readers a stable pair snapshot."""
    controls = external_pair_control_paths(directory, db)
    lock_path = controls["lock"]
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield controls
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_transaction(path: Path, payload: dict) -> None:
    """Atomically and durably install a small publication journal."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fd = -1
            json.dump(payload, fh, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise


def _load_transaction(path: Path, db: str) -> dict:
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
        or not isinstance(payload.get("new_generation"), str)
    ):
        raise ExternalPairError(f"{db}: invalid external-pair journal")
    return payload


def _backup_regular_file(path: Path, backup: Path) -> bool:
    if not path.exists():
        return False
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ExternalPairError(f"refusing to publish over non-regular file {path}")
    backup.unlink(missing_ok=True)
    os.link(path, backup, follow_symlinks=False)
    return True


def _restore_backup(target: Path, backup: Path, was_present: bool) -> None:
    if not was_present:
        target.unlink(missing_ok=True)
        return
    if not backup.exists() or not stat.S_ISREG(backup.lstat().st_mode):
        raise ExternalPairError(f"missing prior-generation backup for {target.name}")
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.restore.",
        suffix=".tmp",
    )
    os.close(fd)
    tmp = Path(tmp_name)
    tmp.unlink()
    try:
        os.link(backup, tmp, follow_symlinks=False)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _cleanup_transaction(controls: dict[str, Path]) -> None:
    controls["pages_backup"].unlink(missing_ok=True)
    controls["links_backup"].unlink(missing_ok=True)
    _fsync_directory(controls["journal"].parent)
    controls["journal"].unlink(missing_ok=True)
    _fsync_directory(controls["journal"].parent)


def _restore_transaction(
    db: str,
    pages_path: Path,
    links_path: Path,
    controls: dict[str, Path],
) -> None:
    payload = _load_transaction(controls["journal"], db)
    _restore_backup(links_path, controls["links_backup"], payload["old_links"])
    _fsync_directory(links_path.parent)
    _restore_backup(pages_path, controls["pages_backup"], payload["old_pages"])
    _fsync_directory(pages_path.parent)
    if payload["old_pages"]:
        read_external_pair(
            db,
            pages_path,
            links_path if payload["old_links"] else None,
        )
    elif links_path.exists():
        raise ExternalPairError(f"{db}: rollback left an orphan links file")
    _cleanup_transaction(controls)


def recover_external_pair(
    db: str,
    pages_path: Path,
    links_path: Path,
    controls: dict[str, Path],
) -> None:
    """Finalize a complete interrupted commit or restore its prior generation."""
    if not controls["journal"].exists():
        controls["pages_backup"].unlink(missing_ok=True)
        controls["links_backup"].unlink(missing_ok=True)
        return
    _load_transaction(controls["journal"], db)
    try:
        read_external_pair(db, pages_path, links_path)
    except (OSError, ValueError):
        _restore_transaction(db, pages_path, links_path, controls)
    else:
        _cleanup_transaction(controls)


def _cleanup_stale_pair_temps(
    directory: Path,
    db: str,
    controls: dict[str, Path],
) -> None:
    """Remove only this locked source's unpublished crash remnants."""
    for pattern in (
        f".{db}_pages.jsonl.*.tmp",
        f".{db}_links.jsonl.*.tmp",
        f".{controls['journal'].name}.*.tmp",
    ):
        for path in directory.glob(pattern):
            path.unlink(missing_ok=True)


def read_stable_external_pair(
    db: str,
    pages_path: Path,
    links_path: Path,
) -> tuple[dict, list[dict], dict | None, list[dict]]:
    """Read current bytes or the durable previous generation after a crash."""
    with external_pair_lock(pages_path.parent, db, exclusive=False) as controls:
        if not controls["journal"].exists():
            return read_external_pair(db, pages_path, links_path)
        payload = _load_transaction(controls["journal"], db)
        try:
            return read_external_pair(db, pages_path, links_path)
        except (OSError, ValueError):
            if not payload["old_pages"]:
                raise ExternalPairError(
                    f"{db}: interrupted first publication has no prior generation"
                )
            return read_external_pair(
                db,
                controls["pages_backup"],
                controls["links_backup"] if payload["old_links"] else None,
            )


def _first_meta(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            record = json.loads(line)
            if isinstance(record, dict) and isinstance(record.get("_meta"), dict):
                return record["_meta"]
            return {}
    return {}


def validate_external_directory(
    directory: Path | None = None,
) -> dict[str, tuple[dict, list[dict], dict | None, list[dict]]]:
    """Validate every pair and reject generated link orphans."""
    directory = Path(directory or EXTERNAL_DIR)
    page_paths = {
        path.name[: -len("_pages.jsonl")]: path
        for path in directory.glob("*_pages.jsonl")
    }
    link_paths = {
        path.name[: -len("_links.jsonl")]: path
        for path in directory.glob("*_links.jsonl")
    }
    for journal in directory.glob(".wikilean-pair-*.transaction.json"):
        db = external_pair_db_from_journal(journal)
        if db is None:
            raise ExternalPairError("external-pair journal has an invalid path")
        controls = external_pair_control_paths(directory, db)
        if journal.resolve() != controls["journal"]:
            raise ExternalPairError(
                f"{db}: external-pair journal path does not match its payload"
            )
        with external_pair_lock(directory, db, exclusive=False):
            if not journal.exists():
                continue
            _load_transaction(journal, db)
            if not (directory / f"{db}_pages.jsonl").exists():
                raise ExternalPairError(
                    f"{db}: interrupted publication has no visible pages file"
                )
    for db, link_path in sorted(link_paths.items()):
        if db in page_paths:
            continue
        with external_pair_lock(directory, db, exclusive=False):
            if (directory / f"{db}_pages.jsonl").exists():
                continue
            if EXTERNAL_PAIR_META_FIELDS.intersection(_first_meta(link_path)):
                raise ExternalPairError(
                    f"{db}: generated links file has no matching pages file"
                )
    return {
        db: read_stable_external_pair(
            db,
            pages_path,
            directory / f"{db}_links.jsonl",
        )
        for db, pages_path in sorted(page_paths.items())
    }


def _publish_external_pair(
    pages_path: Path,
    links_path: Path,
    meta: dict,
    pages: list[dict],
    links: list[dict],
    controls: dict[str, Path],
) -> None:
    """Publish a sealed pair with pages as the durable commit point.

    A durable journal and hard-linked prior generation allow a reader or the
    next publisher to recover after SIGKILL. The directory fsync before the
    pages rename guarantees that durable new pages cannot outlive their links.
    """
    pages_tmp = _stage_jsonl(pages_path, meta, pages)
    links_tmp: Path | None = None
    try:
        links_tmp = _stage_jsonl(links_path, meta, links)
        old_pages = _backup_regular_file(pages_path, controls["pages_backup"])
        old_links = _backup_regular_file(links_path, controls["links_backup"])
        _fsync_directory(pages_path.parent)
        _write_transaction(controls["journal"], {
            "schema": EXTERNAL_PAIR_TRANSACTION_SCHEMA,
            "db": meta["db"],
            "new_generation": meta["pair_generation"],
            "old_pages": old_pages,
            "old_links": old_links,
        })
        os.replace(links_tmp, links_path)
        _fsync_directory(links_path.parent)
        os.replace(pages_tmp, pages_path)
        _fsync_directory(pages_path.parent)
        _cleanup_transaction(controls)
    except BaseException as exc:
        if controls["journal"].exists():
            try:
                _restore_transaction(
                    meta["db"], pages_path, links_path, controls
                )
            except BaseException as rollback_exc:
                raise RuntimeError(
                    f"{meta['db']}: publication failed and rollback also failed: "
                    f"{rollback_exc}"
                ) from exc
        else:
            controls["pages_backup"].unlink(missing_ok=True)
            controls["links_backup"].unlink(missing_ok=True)
        raise
    finally:
        if links_tmp is not None:
            links_tmp.unlink(missing_ok=True)
        pages_tmp.unlink(missing_ok=True)


def emit(db: str, pages: list[dict], links: list[dict], extra_meta: dict | None = None) -> None:
    """Validate rows and publish both files as one readable generation.

    Normalizes page/link ids and alias keys (all Unicode category-C codepoints
    stripped — the Worker rejects them, see strip_controls), drops pages whose
    id becomes empty (counted in _meta), and enforces a volume sanity floor
    against the previous files (_volume_guard) before any write.
    """
    seen: set[str] = set()
    norm_changed: set[str] = set()  # ids altered by normalization
    kept_pages: list[dict] = []
    n_pages_dropped_bad_id = 0
    for p in pages:
        if "_meta" in p or p.get("db") != db or not p.get("id") \
                or not p.get("title") or not p.get("url"):
            raise ValueError(f"bad page row: {json.dumps(p)[:200]}")
        raw_id = str(p["id"])
        pid = strip_controls(raw_id)
        if not pid.strip():
            n_pages_dropped_bad_id += 1
            continue
        if pid in seen:
            if pid != raw_id or pid in norm_changed:
                # collision minted by normalization — the zero-width twin is junk
                n_pages_dropped_bad_id += 1
                continue
            raise ValueError(f"duplicate page id {pid!r}")
        if pid != raw_id:
            norm_changed.add(pid)
        p["id"] = pid
        seen.add(pid)
        if p.get("aliases"):
            aliases: list[str] = []
            for a in p["aliases"]:
                a = strip_controls(str(a))
                if a.strip() and a not in aliases:
                    aliases.append(a)
            if aliases:
                p["aliases"] = aliases
            else:
                del p["aliases"]
        if "snippet" in p:
            if db not in SNIPPET_OK:
                raise ValueError(f"{db} may not store snippets (license)")
            p["snippet"] = clean_snippet(p["snippet"])
            p["snippet_license"] = SNIPPET_LICENSE[db]
        kept_pages.append(p)
    if not kept_pages:
        raise RuntimeError(
            f"refusing to write 0 pages for {db} (fail-soft)"
        )
    page_ids = seen
    kept_links = []
    n_links_dropped_bad_id = 0
    for e in links:
        if "_meta" in e or e.get("db") != db or not e.get("src") \
                or not e.get("dst"):
            raise ValueError(f"bad link row: {json.dumps(e)[:200]}")
        src = strip_controls(str(e["src"]))
        dst = strip_controls(str(e["dst"]))
        if not src.strip() or not dst.strip():
            n_links_dropped_bad_id += 1
            continue
        if src == dst:
            continue
        e["src"], e["dst"] = src, dst
        e.setdefault("context", "body")
        kept_links.append(e)
    # links may reference pages we did not keep as rows (e.g. anchored-subset OEIS);
    # record how many resolve for the meta, but do not drop them — build_common
    # decides minting.
    resolved = sum(1 for e in kept_links if e["src"] in page_ids and e["dst"] in page_ids)
    if extra_meta is not None and not isinstance(extra_meta, dict):
        raise ValueError("external metadata must be an object")
    extra = dict(extra_meta or {})
    writer_meta_fields = {
        "db", "fetched_at", "n_pages", "n_links", "n_links_resolved",
        "n_pages_dropped_bad_id", "n_links_dropped_bad_id",
        *EXTERNAL_PAIR_META_FIELDS,
    }
    conflicts = sorted(writer_meta_fields.intersection(extra))
    if conflicts:
        raise ValueError(
            "external metadata fields are writer-owned: " + ", ".join(conflicts)
        )
    unsupported = sorted(set(extra) - EXTERNAL_PAIR_EXTRA_META_FIELDS)
    if unsupported:
        raise ValueError(
            "external metadata fields are not normalized-data fields: "
            + ", ".join(unsupported)
        )
    source_pin = extra.get("source_pin")
    if not isinstance(source_pin, str) or not source_pin:
        raise ValueError("external metadata requires a nonempty source_pin")
    if "source_license" in extra and (
        not isinstance(extra["source_license"], str) or not extra["source_license"]
    ):
        raise ValueError("external metadata source_license must be a nonempty string")
    for field, value in extra.items():
        if field.startswith("n_") and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError(f"external metadata {field} must be a nonnegative integer")
    if "sitemap_inventory" in extra and (
        extra["sitemap_inventory"] is not None
        and (
            isinstance(extra["sitemap_inventory"], bool)
            or not isinstance(extra["sitemap_inventory"], int)
            or extra["sitemap_inventory"] < 0
        )
    ):
        raise ValueError(
            "external metadata sitemap_inventory must be null or a nonnegative integer"
        )
    meta = {
        "db": db,
        "n_pages": len(kept_pages),
        "n_links": len(kept_links),
        "n_links_resolved": resolved,
        "n_pages_dropped_bad_id": n_pages_dropped_bad_id,
        "n_links_dropped_bad_id": n_links_dropped_bad_id,
        **extra,
    }
    pages_path = EXTERNAL_DIR / f"{db}_pages.jsonl"
    links_path = EXTERNAL_DIR / f"{db}_links.jsonl"
    meta = seal_external_pair_meta(meta, kept_pages, kept_links)
    validate_external_pair(db, meta, kept_pages, meta, kept_links)
    with external_pair_lock(EXTERNAL_DIR, db, exclusive=True) as controls:
        recover_external_pair(db, pages_path, links_path, controls)
        _cleanup_stale_pair_temps(EXTERNAL_DIR, db, controls)
        if pages_path.exists():
            read_external_pair(
                db,
                pages_path,
                links_path if links_path.exists() else None,
            )
        elif links_path.exists():
            raise ExternalPairError(
                f"{db}: refusing to publish over an orphan links file"
            )
        _volume_guard(pages_path, "page", len(kept_pages))
        _volume_guard(links_path, "link", len(kept_links))
        # A generated pair always has a links artifact, including a meta-only
        # one, so stale legacy links cannot survive beside newly built pages.
        _publish_external_pair(
            pages_path,
            links_path,
            meta,
            kept_pages,
            kept_links,
            controls,
        )
    print(f"[{db}] wrote {len(kept_pages)} pages, {len(kept_links)} links "
          f"({resolved} resolved) -> {EXTERNAL_DIR}", file=sys.stderr)


def read_pages(db: str) -> list[dict]:
    """Read pages only after validating their optional links partner."""
    _meta, rows, _links_meta, _links = read_stable_external_pair(
        db,
        EXTERNAL_DIR / f"{db}_pages.jsonl",
        EXTERNAL_DIR / f"{db}_links.jsonl",
    )
    return rows


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """tmp+rename byte write for per-page cache files: cached files are trusted
    unconditionally on later runs, so a killed run must never be able to leave
    a truncated file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.rename(path)


def cache_path(db: str, *parts: str) -> Path:
    p = CACHE_DIR / db
    for part in parts:
        p = p / part
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def stale(path: Path, max_age_hours: float) -> bool:
    """True if `path` is missing or older than max_age_hours (adapter cadence gate)."""
    if not path.exists():
        return True
    age = time.time() - path.stat().st_mtime
    return age > max_age_hours * 3600
