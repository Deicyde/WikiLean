#!/usr/bin/env python3
"""Print pending policy drafts or validate operator-pinned review documents.

No approval, deployment, source relabeling, or existing readiness change occurs.
Output is canonical JSON on stdout; this command never writes an input or store.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_policy_reviews as core


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("draft-private", "validate-private", "draft-public", "validate-public"):
        command = commands.add_parser(name)
        command.add_argument("--pack", type=Path, required=True)
        if name != "draft-private":
            command.add_argument("--private-review", type=Path, required=True)
            command.add_argument("--expected-private-id", required=True)
            command.add_argument("--private-attachments", type=Path)
        if "public" in name:
            command.add_argument("--release", type=Path, required=True)
        if name == "validate-public":
            command.add_argument("--public-review", type=Path, required=True)
            command.add_argument("--expected-public-id", required=True)
            command.add_argument("--public-attachments", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "draft-private":
            result = core.draft_private(args.pack.absolute())
        else:
            private = core.load_document(args.private_review.absolute())
            if args.command == "validate-private":
                result = core.validate_private(private, args.pack.absolute(), expected_id=args.expected_private_id,
                    attachment_root=args.private_attachments)
            elif args.command == "draft-public":
                result = core.draft_public(args.pack.absolute(), args.release.absolute(), private,
                    expected_private_id=args.expected_private_id, attachment_root=args.private_attachments)
            else:
                public = core.load_document(args.public_review.absolute())
                result = core.validate_public(public, args.pack.absolute(), args.release.absolute(), private,
                    expected_id=args.expected_public_id, expected_private_id=args.expected_private_id,
                    attachment_root=args.public_attachments, private_attachment_root=args.private_attachments)
        # Draft output is itself a reusable canonical control document.
        sys.stdout.buffer.write(core.canonical(result))
        if args.command.startswith("validate-"):
            return 0 if any(result.get(key) is True for key in ("private_replay_policy_ready", "public_release_policy_ready")) else 2
        return 0
    except (core.PolicyReviewError, core.contracts.VerificationError, OSError, ValueError, KeyError, TypeError) as exc:
        print("Policy review failed: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
