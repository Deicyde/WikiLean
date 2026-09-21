#!/usr/bin/env python3
"""Private stdio bootstrap for launch_replay_oci.py's inspected container.

This input is not a caller-supplied attestation file and its output is not launch
authority. Only the outer launcher's engine observations establish that execution
took place in the requested image. There is no trust claim against hostile host
code capable of replacing Python modules or fabricating unsigned operator records.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import execution_environment as environment
import oci_runtime
import run_replay_v2 as runner

CHANNEL_SCHEMA = "wikilean.oci-launch-channel/v1"
CHANNEL_LIMIT = 64 * 1024


def main() -> int:
    runner.require_isolated_startup()
    if sys.argv[1:] or not sys.platform.startswith("linux") or sys.version_info[:2] != (3, 12):
        raise RuntimeError("OCI entrypoint requires isolated CPython 3.12 on Linux and a private stdin channel")
    raw = sys.stdin.buffer.read(CHANNEL_LIMIT + 1)
    if len(raw) > CHANNEL_LIMIT:
        raise RuntimeError("OCI launch channel exceeds size limit")
    value = oci_runtime._json(raw, "OCI launch channel")
    oci_runtime._keys(value, {"schema", "nonce", "runtime", "policy", "runner_arguments"}, "launch channel")
    if raw != environment.canonical_json_bytes(value) or value["schema"] != CHANNEL_SCHEMA:
        raise RuntimeError("invalid canonical OCI launch channel")
    if not isinstance(value["nonce"], str) or re.fullmatch(r"[0-9a-f]{32}", value["nonce"]) is None:
        raise RuntimeError("invalid OCI launch nonce")
    arguments = value["runner_arguments"]
    if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
        raise RuntimeError("invalid OCI runner arguments")
    policy = oci_runtime.validate_policy(value["policy"])
    oci_runtime.verify_numerical_runtime(policy)
    evidence = {"schema": environment.TRUSTED_RUNTIME_EVIDENCE_SCHEMA,
                "profile": environment.AUTHORITATIVE_OCI_PROFILE, "runtime": value["runtime"]}
    environment.validate_trusted_runtime_evidence(evidence)
    result = runner.main(arguments, _trusted_runtime_evidence=evidence, _numerical_policy=policy)
    if result == 0:
        print(environment.canonical_json_bytes({"schema": CHANNEL_SCHEMA, "nonce": value["nonce"], "ok": True}).decode())
    return result


if __name__ == "__main__":
    raise SystemExit(main())
