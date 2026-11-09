#!/usr/bin/env python3
"""Validate queued contributions using WikiLean's existing offline intake checker.

Operator service, not contributor software. PIPELINE_TOKEN stays on the operator
host. Each job has a short lease; only its matching digest and lease may commit
a verdict. A rejected set is never retried. This process publishes no live data.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import submission_transport as transport


def process_one(*, base_url: str, token: str) -> dict:
    code, data = transport.request(base_url, "/api/submissions/claim", token, body=b"{}")
    claimed = transport.response_json(code, data)
    if "job" not in claimed:
        raise transport.TransportError("validator service returned an invalid claim")
    job = claimed["job"]
    if job is None:
        return {"ok": True, "idle": True}
    if (not isinstance(job, dict) or not isinstance(job.get("id"), str)
            or not re.fullmatch(r"sub-[0-9a-f]{64}", job["id"])
            or not isinstance(job.get("lease_id"), str)
            or not re.fullmatch(r"[0-9a-f-]{36}", job["lease_id"])
            or not isinstance(job.get("agent_id"), str)
            or not isinstance(job.get("received_at"), int)
            or not isinstance(job.get("bytes"), int) or not 1 <= job["bytes"] <= transport.MAX_BYTES
            or any(not isinstance(job.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", job[key])
                   for key in ("body_sha256", "seal_sha256"))):
        raise transport.TransportError("validator service returned an invalid claim")
    headers = {"X-Intake-Lease": job["lease_id"]}
    code, body = transport.request(base_url, f"/api/submissions/{job['id']}/body", token, headers=headers)
    if code != 200:
        transport.response_json(code, body)
        raise transport.TransportError("validator could not download the leased upload")
    if len(body) != job["bytes"] or transport.sha(body) != job["body_sha256"]:
        raise transport.TransportError("leased upload digest mismatch")
    received = datetime.fromtimestamp(job["received_at"] / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        result = transport.verify_upload(body, machine_id=job["agent_id"], received_at=received,
                                         body_sha256=job["body_sha256"], seal_sha256=job["seal_sha256"])
        if isinstance(result, transport.intake.Refusal):
            state = "rejected"
            verdict = {"verifier": transport.intake.VERIFIER, "code": result.code,
                       "error": result.reason[:2000], "retryable": False}
        else:
            state = "accepted"
            verdict = {"verifier": transport.intake.VERIFIER, "code": "verified",
                       "submission": result.submission, "records": len(result.records),
                       "files": len(result.files), "published": False}
    except Exception as exc:
        # Unexpected checker failures are visible operational failures, not
        # mathematical rejections and not automatic repeated validation runs.
        state = "failed"
        verdict = {"verifier": transport.intake.VERIFIER, "code": "validator_error",
                   "error": "intake checker failed: " + type(exc).__name__, "retryable": False}
    payload = {"state": state, "body_sha256": job["body_sha256"],
               "seal_sha256": job["seal_sha256"], "verdict": verdict}
    code, data = transport.request(base_url, f"/api/submissions/{job['id']}/verdict", token,
                                   body=json.dumps(payload, separators=(",", ":")).encode(), headers=headers)
    result = transport.response_json(code, data)
    receipt = transport.receipt(result)
    if receipt["id"] != job["id"] or receipt["state"] != state or receipt["body_sha256"] != job["body_sha256"] or receipt["seal_sha256"] != job["seal_sha256"]:
        raise transport.TransportError("validator receipt does not match the completed job")
    return {"ok": True, "id": job["id"], "state": receipt["state"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=transport.BASE_URL)
    parser.add_argument("--once", action="store_true", help="process at most one job and exit")
    parser.add_argument("--poll-seconds", type=float, default=10)
    args = parser.parse_args(argv)
    problem = transport.intake.build_store.interpreter_problem()
    if problem:
        print(problem, file=sys.stderr)
        return 3
    if not 1 <= args.poll_seconds <= 300:
        parser.error("--poll-seconds must be between 1 and 300")
    token = os.environ.get("PIPELINE_TOKEN", "")
    if not token:
        print("PIPELINE_TOKEN is required on the validator host", file=sys.stderr)
        return 3
    transport.origin(args.base_url)
    while True:
        try:
            result = process_one(base_url=args.base_url, token=token)
            if not result.get("idle") or args.once:
                print(json.dumps(result), flush=True)
            if args.once:
                return 0
            if not result.get("idle"):
                continue
        except transport.TransportError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr, flush=True)
            if args.once:
                return 2
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    sys.exit(main())
