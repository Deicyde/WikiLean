#!/usr/bin/env python3
"""Pending mapping drafts and explicitly pinned standalone coverage reports."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import provenance_coverage as core

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('command',choices=('draft','check'));ap.add_argument('--pack',required=True,type=Path);ap.add_argument('--release',required=True,type=Path)
    ap.add_argument('--mapping',type=Path);ap.add_argument('--expected-mapping-id');ap.add_argument('--private-review',type=Path);ap.add_argument('--expected-private-id');ap.add_argument('--private-attachments',type=Path)
    ap.add_argument('--public-review',type=Path);ap.add_argument('--expected-public-id');ap.add_argument('--public-attachments',type=Path)
    args=ap.parse_args()
    try:
        if args.command=='draft':
            if any((args.mapping,args.expected_mapping_id,args.private_review,args.expected_private_id,args.public_review,args.expected_public_id,args.private_attachments,args.public_attachments)):ap.error('draft accepts only pack and release')
            result=core.draft_mapping(args.pack,args.release)
        else:
            if not all((args.mapping,args.expected_mapping_id,args.private_review,args.expected_private_id)):ap.error('check requires mapping/private review documents and independent expected IDs')
            result=core.check(core.policy.load_document(args.mapping),args.pack,args.release,core.policy.load_document(args.private_review),expected_mapping_id=args.expected_mapping_id,expected_private_id=args.expected_private_id,private_attachment_root=args.private_attachments,public_review=core.policy.load_document(args.public_review) if args.public_review else None,expected_public_id=args.expected_public_id,public_attachment_root=args.public_attachments)
        sys.stdout.buffer.write(core.canonical(result));return 0 if args.command=='draft' or result['provenance_coverage_ready'] else 2
    except (ValueError,OSError,KeyError,TypeError,RecursionError) as exc:
        print('Provenance coverage refused: '+str(exc),file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())
