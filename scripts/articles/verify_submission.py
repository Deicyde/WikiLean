#!/usr/bin/env python3
"""Verify a contributed sealed record set against the intake contract, offline.

New code. Design and contracts: docs/INTAKE.md section 3, which this module is the
normative statement of — an endpoint written in another language re-implements these
checks and this module is what it is tested against. The input contract itself is
docs/ARTICLES.md section 3, and every check that is about the *record set* rather than
about the *submission* is delegated to scripts/articles/build_store.py so the two cannot
drift: a set the articles store builder refuses is a set intake refuses, by construction.

What this module adds over the builder is the submission envelope and the key:

    1. the posted body's size and its sha256 against the Content-Digest header;
    2. Idempotency-Key is the seal's own sha256, so a retry is the same submission;
    3. cf-locate-recordset/1 is refused — it carries no passage_key, so nothing
       downstream can key a vote on it (docs/INTAKE.md section 2);
    4. the key's scope: its machine, its campaigns, its universes and its quota.

Every refusal is a reason and a stable code, never a warning, and nothing here judges
whether a location is *right*: that is the challenge's job and the Brain's gate's
(docs/INTAKE.md section 3, "What intake does not check").

Usage (CPython 3.12.13 or a later 3.12, as build_store needs; the vendored Literature
Links checker rides site/native_annotations.py and the CI's pinned jsonschema):

    python3 scripts/articles/verify_submission.py --record-set <dir> --machine-id <id>
    python3 scripts/articles/verify_submission.py --record-set <dir> --machine-id <id> \
        --campaign run:loc-7 --universe <universe-sha256> \
        --max-submissions-per-day 20 --max-bytes-per-day 1073741824 \
        --submissions-today 3 --bytes-today 400000 \
        --body <posted-body> --content-digest <sha256> --idempotency-key <sha256> \
        --received-at 2026-11-06T00:00:00Z --out <staging-rows.json>

Exit codes: 0 accepted, 2 refused (the code and the reason on stderr), 3 the verifier
itself failed. Nothing here touches the network, D1, R2 or any Worker binding, and
nothing here writes a live table: an accepted submission is staging rows and nothing
else (docs/INTAKE.md section 4).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build_store  # noqa: E402

# Intake takes the /2 record set only (docs/INTAKE.md section 2); build_store takes /1 too.
INTAKE_SCHEMA = build_store.SEAL_SCHEMA_BUNDLE
VERIFIER = "scripts/articles/verify_submission.py/1"
# How a refusal reaches a contributor, as docs/INTAKE.md section 3 fixes it: a defect in the
# submission is 422 and must never be retried, a quota is 429 and may be, a key that may not
# post this set is 403, and a body over the endpoint's maximum is 413.
UNPROCESSABLE = 422
FORBIDDEN = 403
TOO_LARGE = 413
TOO_MANY = 429


@dataclass(frozen=True)
class Envelope:
    """The three things outside the set (docs/INTAKE.md section 2.1), as the endpoint read them."""

    body_bytes: int
    body_sha256: str
    content_digest: str
    idempotency_key: str


@dataclass(frozen=True)
class KeyScope:
    """One contributor machine's key (docs/INTAKE.md section 5). Empty tuple = this key is not
    narrowed on that axis; a limit of 0 = no limit, as the export's own --byte-budget reads 0."""

    machine_id: str
    campaigns: tuple[str, ...] = ()
    universes: tuple[str, ...] = ()
    max_submissions_per_day: int = 0
    max_bytes_per_day: int = 0
    max_body_bytes: int = 0


@dataclass(frozen=True)
class Usage:
    """What this key has already spent today, read from the staging table by the caller."""

    submissions: int = 0
    bytes: int = 0


@dataclass(frozen=True)
class Refusal:
    """Why a submission was not accepted: a stable code, an HTTP status, and the reason."""

    code: str
    status: int
    retryable: bool
    reason: str

    def report(self) -> str:
        retry = "may be retried" if self.retryable else "must not be retried"
        return f"intake: refused ({self.code}, HTTP {self.status}, {retry})\n{self.reason}"


@dataclass(frozen=True)
class Accepted:
    """The staging rows an accepted submission becomes, and nothing else (docs/INTAKE.md section 4)."""

    submission: dict
    files: list[dict] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"submission": self.submission, "submission_file": self.files,
                "submission_record": self.records}


def _refuse(code: str, status: int, reason: str, *, retryable: bool = False) -> Refusal:
    return Refusal(code=code, status=status, retryable=retryable, reason=reason)


def _envelope_refusal(envelope: Envelope, scope: KeyScope) -> Refusal | None:
    """Check 1 and the body's size: what an endpoint settles before it unpacks anything."""
    if scope.max_body_bytes and envelope.body_bytes > scope.max_body_bytes:
        return _refuse("body_too_large", TOO_LARGE,
                       f"the body is {envelope.body_bytes} bytes, over this endpoint's maximum of "
                       f"{scope.max_body_bytes}; export per shard (the seal's campaign token already "
                       f"names one, 'run:<run>[+<challenge run>]')")
    if envelope.body_sha256 != envelope.content_digest:
        return _refuse("body_digest", UNPROCESSABLE,
                       f"the body hashes to {envelope.body_sha256}, and Content-Digest says "
                       f"{envelope.content_digest}")
    return None


def _scope_refusal(seal: dict, scope: KeyScope) -> Refusal | None:
    """Check 8: a key scoped to one campaign and one universe cannot post anything else."""
    if scope.campaigns and seal["campaign"] not in scope.campaigns:
        return _refuse("scope_campaign", FORBIDDEN,
                       f"this key may post campaign {', '.join(scope.campaigns)}, and the seal's "
                       f"campaign is {seal['campaign']}")
    if scope.universes and seal["universe"]["universe_sha256"] not in scope.universes:
        return _refuse("scope_universe", FORBIDDEN,
                       f"this key may post universe {', '.join(scope.universes)}, and the seal's "
                       f"universe is {seal['universe']['universe_sha256']}")
    return None


def _quota_refusal(scope: KeyScope, usage: Usage, body_bytes: int) -> Refusal | None:
    """Check 8's quota. The refusal names the quota; it never silently drops."""
    if scope.max_submissions_per_day and usage.submissions + 1 > scope.max_submissions_per_day:
        return _refuse("quota_submissions", TOO_MANY, retryable=True,
                       reason=f"this key has posted {usage.submissions} submissions today and its quota "
                              f"is {scope.max_submissions_per_day} a day; retry after the quota resets")
    if scope.max_bytes_per_day and usage.bytes + body_bytes > scope.max_bytes_per_day:
        return _refuse("quota_bytes", TOO_MANY, retryable=True,
                       reason=f"this key has posted {usage.bytes} bytes today and this submission is "
                              f"{body_bytes} more, over its quota of {scope.max_bytes_per_day} a day; "
                              f"retry after the quota resets")
    return None


def staging_records(files: dict[str, bytes]) -> list[dict]:
    """One submission_record row per record, its payload the posted line verbatim.

    A row that re-encoded its record could not be shown to be the bytes the seal signed
    (docs/INTAKE.md section 4), so `payload` is the line as it arrived and the four indexed
    columns are read off it. The set has already passed build_store, so every line parses
    and every record carries its locate extension and its passage_key.
    """
    rows: list[dict] = []
    for path in sorted(p for p in files if p.startswith("records/") and p.endswith(".jsonl")):
        source_id = path[len("records/"):-len(".jsonl")]
        for line in files[path].decode("utf-8").splitlines():
            record = json.loads(line)
            locate = record["extensions"][build_store.LOCATE]
            rows.append({"source_id": source_id, "passage_key": locate["passage_key"],
                         "pair_key": locate.get("pair_key"), "relation": record.get("relation"),
                         "status": record.get("status"), "payload": line})
    return rows


def verify(set_dir: Path, *, scope: KeyScope, received_at: str, envelope: Envelope | None = None,
           usage: Usage = Usage()) -> Accepted | Refusal:
    """Run docs/INTAKE.md section 3 over one unpacked submission; the rows, or the first refusal.

    `envelope` is the posted body as the endpoint read it; without one (an operator or a
    contributor checking a set before it is posted) checks 1 and 2 cannot run and are skipped,
    and the byte quota counts the set's own bytes. `received_at` is the caller's clock, passed
    in so the answer is a pure function of its arguments.
    """
    set_dir = Path(set_dir)
    if not build_store.CREATED_RE.match(received_at):
        raise ValueError("received_at must be an ISO 8601 UTC time ending in Z")
    if envelope is not None:
        refusal = _envelope_refusal(envelope, scope)
        if refusal is not None:
            return refusal

    # Checks 2, 3 and 7: the seal is a seal, every file it names is there with its sha256 and
    # nothing else is, and skill_commit is a commit. build_store.read_record_set is all three.
    try:
        seal, seal_sha, files = build_store.read_record_set(set_dir)
    except build_store.ArticlesError as exc:
        return _refuse("seal", UNPROCESSABLE, str(exc))

    if seal["schema"] != INTAKE_SCHEMA:
        return _refuse("schema_not_accepted", UNPROCESSABLE,
                       f"intake takes {INTAKE_SCHEMA!r} only, and this set is {seal['schema']!r}: a "
                       f"{build_store.SEAL_SCHEMA} set carries no passage_key, so nothing downstream "
                       f"can key a vote on it")
    if envelope is not None and envelope.idempotency_key != seal_sha:
        return _refuse("idempotency_key", UNPROCESSABLE,
                       f"Idempotency-Key must be the seal's own sha256 {seal_sha}, and it is "
                       f"{envelope.idempotency_key}")

    refusal = _scope_refusal(seal, scope)
    if refusal is not None:
        return refusal

    set_bytes = sum(len(data) for data in files.values()) + (set_dir / "SEAL.json").stat().st_size
    body_bytes = envelope.body_bytes if envelope is not None else set_bytes
    refusal = _quota_refusal(scope, usage, body_bytes)
    if refusal is not None:
        return refusal

    # Checks 4, 5 and 6, and the whole of the input contract with them: every record through the
    # vendored Literature Links checker, the identity pins (universe_sha256, the sourceset's
    # manifest_sha256, each source's revision and layer_sha256), the quote cap and the licence
    # class. The builder refuses all of it by name, and intake refuses exactly what it refuses.
    try:
        validate, schema_error, provenance = build_store.schema_validator()
        build_store.build_set(set_dir, validate, schema_error)
    except build_store.ArticlesError as exc:
        return _refuse("record_set", UNPROCESSABLE, str(exc))

    submission = {
        "seal_sha256": seal_sha,
        "set_id": seal["set_id"],
        "campaign": seal["campaign"],
        "universe_sha256": seal["universe"]["universe_sha256"],
        "sourceset": seal["sourceset"]["id"],
        "status_rule": build_store.seal_status_rule(seal),
        "skill_commit": seal["skill_commit"],
        "machine_id": scope.machine_id,
        "received_at": received_at,
        "state": "accepted",
        "checker_version": provenance["version"],
        "byte_budget": seal["byte_budget"],
        "bytes": body_bytes,
        "verifier": VERIFIER,
    }
    rows = [{"path": path, "sha256": seal["files"][path], "bytes": len(data)}
            for path, data in sorted(files.items())]
    return Accepted(submission=submission, files=rows, records=staging_records(files))


def build_scope(args: argparse.Namespace) -> KeyScope:
    return KeyScope(machine_id=args.machine_id, campaigns=tuple(args.campaign),
                    universes=tuple(args.universe),
                    max_submissions_per_day=args.max_submissions_per_day,
                    max_bytes_per_day=args.max_bytes_per_day, max_body_bytes=args.max_body_bytes)


def build_envelope(args: argparse.Namespace) -> Envelope | None:
    """The envelope the three options describe, or None when none of them was given."""
    given = [name for name, value in (("--body", args.body), ("--content-digest", args.content_digest),
                                      ("--idempotency-key", args.idempotency_key)) if value is not None]
    if not given:
        return None
    if len(given) < 3:
        raise SystemExit("intake: --body, --content-digest and --idempotency-key go together "
                         f"(given: {', '.join(given)})")
    data = Path(args.body).read_bytes()
    return Envelope(body_bytes=len(data), body_sha256=hashlib.sha256(data).hexdigest(),
                    content_digest=args.content_digest, idempotency_key=args.idempotency_key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--record-set", required=True, help="the unpacked submission")
    parser.add_argument("--machine-id", required=True, help="the machine the key is bound to")
    parser.add_argument("--campaign", action="append", default=[],
                        help="a campaign token the key may post (repeatable; none = any)")
    parser.add_argument("--universe", action="append", default=[],
                        help="a universe_sha256 the key may post (repeatable; none = any)")
    parser.add_argument("--max-submissions-per-day", type=int, default=0, help="0 = no quota")
    parser.add_argument("--max-bytes-per-day", type=int, default=0, help="0 = no quota")
    parser.add_argument("--max-body-bytes", type=int, default=0, help="0 = no maximum")
    parser.add_argument("--submissions-today", type=int, default=0)
    parser.add_argument("--bytes-today", type=int, default=0)
    parser.add_argument("--received-at", default="1970-01-01T00:00:00Z",
                        help="the endpoint's clock, recorded on the staging row")
    parser.add_argument("--body", help="the posted body, for the Content-Digest check")
    parser.add_argument("--content-digest", help="the sha256 the Content-Digest header carried")
    parser.add_argument("--idempotency-key", help="the sha256 the Idempotency-Key header carried")
    parser.add_argument("--out", help="write the staging rows here as JSON")
    args = parser.parse_args(argv)

    problem = build_store.interpreter_problem()
    if problem is not None:
        print(f"intake: {problem}; nothing was checked", file=sys.stderr)
        return 3
    outcome = verify(Path(args.record_set), scope=build_scope(args), received_at=args.received_at,
                     envelope=build_envelope(args),
                     usage=Usage(submissions=args.submissions_today, bytes=args.bytes_today))
    if isinstance(outcome, Refusal):
        print(outcome.report(), file=sys.stderr)
        return 2
    text = json.dumps(outcome.as_dict(), sort_keys=True, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    print(f"intake: accepted {outcome.submission['set_id']} "
          f"({len(outcome.records)} records, {len(outcome.files)} files); staged, not live",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
