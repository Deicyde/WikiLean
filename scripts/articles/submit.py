#!/usr/bin/env python3
"""Submit a sealed pipeline result to WikiLean; preview by default, --execute uploads.

WIKILEAN_AGENT_TOKEN is read only when uploading or checking a receipt. It is
never passed as a command-line argument, saved in a receipt, or printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import submission_transport as transport


def submit(set_dir: Path, *, base_url: str = transport.BASE_URL, execute: bool = False,
           token: str | None = None, wait_seconds: float = 0) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body, seal = transport.prepare(set_dir, received_at=now)
    if not execute:
        return {"ok": True, "dry_run": True, "bytes": len(body), "seal_sha256": seal,
                "body_sha256": transport.sha(body), "destination": transport.origin(base_url),
                "message": "verified locally; add --execute to upload"}
    token = token if token is not None else os.environ.get("WIKILEAN_AGENT_TOKEN", "")
    status, data = transport.request(base_url, "/api/submissions", token, body=body,
                                    headers={"Content-Digest": transport.sha(body), "Idempotency-Key": seal})
    result = transport.response_json(status, data)
    receipt = transport.receipt(result)
    if receipt.get("seal_sha256") != seal or receipt.get("body_sha256") != transport.sha(body):
        raise transport.TransportError("server receipt does not match this upload")
    return wait(result, base_url=base_url, token=token, seconds=wait_seconds)


def status(submission_id: str, *, base_url: str, token: str) -> dict:
    if not re.fullmatch(r"sub-[0-9a-f]{64}", submission_id):
        raise transport.TransportError("invalid submission id")
    code, data = transport.request(base_url, "/api/submissions/" + submission_id, token)
    result = transport.response_json(code, data)
    if transport.receipt(result)["id"] != submission_id:
        raise transport.TransportError("server returned a different submission")
    return result


def wait(result: dict, *, base_url: str, token: str, seconds: float) -> dict:
    deadline = time.monotonic() + seconds
    while result["submission"]["state"] in ("queued", "validating") and time.monotonic() < deadline:
        time.sleep(min(2, max(0, deadline - time.monotonic())))
        result = status(result["submission"]["id"], base_url=base_url, token=token)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--record-set", type=Path)
    choice.add_argument("--status", help="check a prior sub-… receipt")
    parser.add_argument("--base-url", default=transport.BASE_URL)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--wait", type=float, default=0, help="wait up to this many seconds for validation (maximum 300)")
    parser.add_argument("--out", type=Path, help="save the receipt (contains no token)")
    args = parser.parse_args(argv)
    if not 0 <= args.wait <= 300:
        parser.error("--wait must be between 0 and 300 seconds")
    problem = transport.intake.build_store.interpreter_problem()
    if problem:
        print(problem, file=sys.stderr)
        return 3
    try:
        if args.status:
            token = os.environ.get("WIKILEAN_AGENT_TOKEN", "")
            result = wait(status(args.status, base_url=args.base_url, token=token),
                          base_url=args.base_url, token=token, seconds=args.wait)
        else:
            result = submit(args.record_set, base_url=args.base_url,
                            execute=args.execute, wait_seconds=args.wait)
        text = json.dumps(result, indent=2) + "\n"
        if args.out:
            args.out.write_text(text, encoding="utf-8")
        print(text, end="")
        return 2 if result.get("submission", {}).get("state") in ("rejected", "failed") else 0
    except (transport.TransportError, transport.intake.build_store.ArticlesError, OSError, UnicodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
