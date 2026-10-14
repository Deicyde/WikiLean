# Brain SQLite operational handoff

**Production state:** the [2026-10-04 activation record](BRAIN-ACTIVATION-2026-10-04.md).
It covers what production serves, the promotion attempt (reconciled 2026-10-08), rollback,
the release-neutral constraints, and the values the operator must copy from the journal.

**Latest checkpoint before activation:** [2026-09-26](archive/BRAIN-SQLITE-HANDOFF-2026-09-26.md), with
the retained pack and source details in the [2026-09-23 checkpoint](archive/BRAIN-SQLITE-HANDOFF-2026-09-23.md).
Both predate the promotion, so their "selector returns 404" and "not activated" lines are
history.

The canonical completion criteria remain in [ROADMAP.md](ROADMAP.md), especially P0-R and
P1C. [BRAIN-SQLITE-PR-REVIEW.md](archive/BRAIN-SQLITE-PR-REVIEW.md) is the review map for the merged
stack.

The [2026-09-19 checkpoint](archive/BRAIN-SQLITE-HANDOFF-2026-09-19.md),
[2026-09-09 handoff](archive/BRAIN-SQLITE-HANDOFF-2026-09-09.md) and
[2026-09-08 continuation](archive/BRAIN-SQLITE-CONTINUATION-2026-09-08.md) are historical
records. Their private paths, process IDs, live-capture instructions, quota dates, and test
counts describe the old laptop at that time. Do **not** use them as current operator
instructions. In particular, PID 11762 is gone and the old private evidence root is not
present on this laptop.

No document in this series authorizes a production deployment, D1 write, Wikidata
submission, source-policy approval, authority cutover, or Brain release promotion.
