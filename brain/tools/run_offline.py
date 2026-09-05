#!/usr/bin/env python3
"""Verify and execute an offline pack.

Version 1 retains its cooperative single-reducer network guard. Versions 2 and 3
prepare a sealed workspace and execute the same complete DAG through the mandatory
OS-isolated runner; v3 additionally requires the authority verifier to accept its
complete acquisition-evidence closure before workspace materialization.
"""
from __future__ import annotations

import os
import sys
import argparse
import json
import subprocess
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import prepare_replay_v2
import run_replay_v2

from authority_contracts import (
    PACK_SCHEMA,
    PACK_SCHEMA_V2,
    PACK_SCHEMA_V3,
    VerificationError,
    load_canonical_json,
    validate_offline_pack,
    verify_offline_pack_files,
)


def run(
    manifest_path: Path,
    *,
    root: Path | None = None,
    arguments: list[str] | None = None,
    workspace: Path | None = None,
    authority_git_commit: str | None = None,
    authority_root: str | None = None,
    semantic_epoch: str | None = None,
    prior_state_root: str | None = None,
    interpreter: Path | None = None,
    stage_timeout_seconds: float | None = None,
) -> int:
    manifest_path = manifest_path.resolve(strict=True)
    verification_root = (root or manifest_path.parent).resolve(strict=True)
    document, _ = load_canonical_json(manifest_path)
    replay_schema = document.get("schema") if isinstance(document, dict) else None
    if replay_schema in (
        PACK_SCHEMA_V2,
        PACK_SCHEMA_V3,
    ):
        run_replay_v2.require_isolated_startup()
        if arguments:
            if replay_schema == PACK_SCHEMA_V2:
                raise VerificationError(
                    "offline-pack/v2 stage arguments are sealed by its reducer inventory"
                )
            raise VerificationError(
                "offline-pack/v3 stage arguments are sealed by its reducer inventory"
            )
        missing = [
            name
            for name, value in (
                ("--workspace", workspace),
                ("--authority-git-commit", authority_git_commit),
                ("--authority-root", authority_root),
                ("--semantic-epoch", semantic_epoch),
            )
            if value is None
        ]
        if missing:
            version = (
                "offline-pack/v2"
                if replay_schema == PACK_SCHEMA_V2
                else "offline-pack/v3"
            )
            raise VerificationError(version + " requires " + ", ".join(missing))
        replay_options = {}
        if stage_timeout_seconds is not None:
            replay_options["stage_timeout_seconds"] = (
                run_replay_v2._validated_stage_timeout_seconds(
                    stage_timeout_seconds
                )
            )
        prepared = prepare_replay_v2.prepare_replay_v2(
            manifest_path,
            workspace,
            pack_root=verification_root,
            expected_pack_schema=replay_schema,
            authority_git_commit=authority_git_commit,
            authority_root=authority_root,
            semantic_epoch=semantic_epoch,
            prior_state_root=prior_state_root,
        )
        run_replay_v2.run_replay_v2(
            prepared.context_path,
            reducer_files=prepared.reducer_files,
            expected_generation_id=prepared.generation_id,
            expected_offline_pack_id=prepared.offline_pack_id,
            expected_source_set_root=prepared.source_set_root,
            expected_reducer_inventory_id=prepared.reducer_inventory_id,
            expected_reducer_git_commit=prepared.reducer_git_commit,
            expected_configuration_sha256=prepared.configuration_sha256,
            expected_environment_sha256=prepared.environment_sha256,
            interpreter=interpreter or Path(sys.executable),
            **replay_options,
        )
        return 0
    if not isinstance(document, dict) or document.get("schema") != PACK_SCHEMA:
        schema = document.get("schema") if isinstance(document, dict) else None
        raise VerificationError(f"$.schema: unknown schema/version {schema!r}")
    if any(
        value is not None
        for value in (
            workspace,
            authority_git_commit,
            authority_root,
            semantic_epoch,
            prior_state_root,
            stage_timeout_seconds,
        )
    ):
        raise VerificationError(
            "offline-pack/v2 replay options are not valid for offline-pack/v1"
        )
    pack = validate_offline_pack(document)
    verify_offline_pack_files(pack, verification_root, manifest_path=manifest_path)

    reducer_relative = PurePosixPath(pack["reducer"]["path"])
    reducer_path = verification_root.joinpath(*reducer_relative.parts)
    if reducer_path.suffix != ".py":
        raise VerificationError(
            "$.reducer.path: cooperative offline runner v1 requires a Python reducer"
        )

    guard_dir = str(Path(__file__).resolve().parent)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": guard_dir,
        "WIKILEAN_OFFLINE": "1",
        "WIKILEAN_OFFLINE_PACK": str(manifest_path),
        "WIKILEAN_OFFLINE_ROOT": str(verification_root),
        "WIKILEAN_REDUCER_CONFIG": str(
            verification_root.joinpath(*PurePosixPath(pack["configuration"]["path"]).parts)
        ),
    }
    command = [
        sys.executable,
        "-I",
        "-S",
        "-B",
        "-c",
        (
            "import runpy,sys; "
            f"sys.path[:0]=[{guard_dir!r},{str(reducer_path.parent)!r}]; "
            "import offline_guard; "
            f"sys.argv={[str(reducer_path), *(arguments or [])]!r}; "
            f"runpy.run_path({str(reducer_path)!r},run_name='__main__')"
        ),
    ]
    process = subprocess.run(command, cwd=verification_root, env=environment, check=False)
    return process.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--root",
        type=Path,
        help="offline-pack root (default: directory containing --manifest)",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        help="fresh replay workspace (required for offline-pack/v2 or v3)",
    )
    parser.add_argument("--authority-git-commit")
    parser.add_argument("--authority-root")
    parser.add_argument("--semantic-epoch")
    parser.add_argument("--prior-state-root")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--stage-timeout-seconds", type=float)
    parser.add_argument("reducer_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    reducer_args = args.reducer_args
    if reducer_args[:1] == ["--"]:
        reducer_args = reducer_args[1:]
    try:
        return run(
            args.manifest,
            root=args.root,
            arguments=reducer_args,
            workspace=args.workspace,
            authority_git_commit=args.authority_git_commit,
            authority_root=args.authority_root,
            semantic_epoch=args.semantic_epoch,
            prior_state_root=args.prior_state_root,
            interpreter=args.python,
            stage_timeout_seconds=args.stage_timeout_seconds,
        )
    except (
        OSError,
        VerificationError,
        prepare_replay_v2.ReplayPreparationError,
        run_replay_v2.ReplayExecutionError,
        run_replay_v2.build_context.BuildContextError,
    ) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
