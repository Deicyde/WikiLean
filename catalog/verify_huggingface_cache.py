#!/usr/bin/env python3
"""Verify reviewed Hugging Face cache files and optionally hold their read lock."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from huggingface_download import (
    HuggingFaceArtifactError,
    load_reviewed_pin,
    verified_artifact_set,
)


def parse_file(value: str) -> tuple[str, Path]:
    remote_path, separator, local_path = value.partition("=")
    if not separator or not remote_path or not local_path:
        raise argparse.ArgumentTypeError(
            "--file must have the form REMOTE_PATH=LOCAL_PATH"
        )
    return remote_path, Path(local_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True)
    parser.add_argument(
        "--file",
        action="append",
        required=True,
        type=parse_file,
        metavar="REMOTE_PATH=LOCAL_PATH",
    )
    parser.add_argument(
        "--hold",
        action="store_true",
        help="hold the shared lock until stdin reaches EOF",
    )
    args = parser.parse_args(argv)
    try:
        pin = load_reviewed_pin(args.dataset)
        files = dict(args.file)
        if len(files) != len(args.file):
            raise HuggingFaceArtifactError("duplicate --file remote path")
        requests = [
            pin.request(remote_path, local_path)
            for remote_path, local_path in sorted(files.items())
        ]
        with verified_artifact_set(
            dataset=pin.dataset,
            revision=pin.revision,
            requests=requests,
        ) as metadata:
            print(
                json.dumps(
                    {
                        "dataset": pin.dataset,
                        "revision": pin.revision,
                        "files": {
                            request.remote_path: item
                            for request, item in zip(
                                requests, metadata, strict=True
                            )
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                flush=True,
            )
            if args.hold:
                sys.stdin.buffer.read()
    except HuggingFaceArtifactError as exc:
        raise SystemExit(f"FATAL: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
