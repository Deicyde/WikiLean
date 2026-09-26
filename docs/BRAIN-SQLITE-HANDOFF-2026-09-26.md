# Brain SQLite continuation — 2026-09-26

The user requested resumption and fixes toward full deployment. Work continues in
`/Users/jack/Desktop/LEAN/WikiLean-completion`, branch
`codex/brain-migration-completion`, with PR #35. The original `WikiLean` checkout
remains on old local `main` with its pre-existing data changes; preserve it.

The [September 23 checkpoint](BRAIN-SQLITE-HANDOFF-2026-09-23.md) remains the source
for the exact verified pack, source evidence, prepared public assets, runtime,
and pending policy records. New local verification logs are retained separately
under `/Users/jack/.local/share/wikilean-migration/completion-20260926`.

## Publication bug fixed

`build_replay_release.py` produces `brain-offline-replay-v1`. The Worker, canary,
and normative Python verifier already supported that profile, but
`wiki/scripts/brain-release-public.ts` rejected it. Consequently a successful
full replay could not reach public staging.

The stager now accepts both existing release profiles. Offline replay requires
its exact five-field replay binding, valid content identities, and exactly one
build and one validation attestation reference. Compatibility-profile behavior
is unchanged. Shared Python/TypeScript fixtures cover malformed and missing
bindings; staging tests also cover tampering, mixed-profile current/previous
retention, and the actual external `build-public.ts` path. Manifest bytes and
logical release identities are preserved. SQLite and raw private inputs remain
excluded from the public asset selection. Full independent artifact and
attestation verification remains the promoter's prerequisite.

The final Python CI page test also exposed an interpreter mismatch: it invoked
ambient `python3` even when the gate selected Python 3.12. macOS's system Python
failed to import the page builder's slotted dataclasses. The test now launches
`sys.executable`; all nine page-contract checks pass with the selected interpreter.
This changes test execution only, not generated page bytes.

## Fresh observations

- PR #35's prior head `c6207d6c` passed every hosted CI job. Independent review of
  its preflight, exporter, profile-registry, and public-inventory changes found
  no actionable issue; fresh focused checks passed.
- All 2,699 retained public assets were rehashed against the tracked inventory:
  96,337,594 bytes match, including required payload and index closure.
- The full Worker gate passes typechecking and 916 tests across 38 files. The
  initial full Python run passed 89 commands before the final page test exposed
  the interpreter issue above; its focused rerun passed. Final complete-run
  results are retained in `python-ci-fixed.log` and the PR's exact-head CI.
- Cloudflare's latest deployment is still `d2926a43-483b-4447-b570-d067c4d77d98`,
  deployed 2026-08-28T02:55:59Z. The live API reports snapshot time
  `2026-08-28T02:46:02+00:00` and no release ID; `/assets/brain/current.json`
  returns 404. No production operation has occurred during this continuation.
- Initial host free space was about 19 GiB, below the retained 30 GiB host
  allowance for the native sequence. No external volume was mounted. The user
  was asked to free 15–20 GiB or provide an external path; remeasure before work.
- The 11 runner and 12 reducer files still match the qualified `7627ccba` bytes.
  The OCI image does not embed its packaging Git commit. Reuse can therefore be
  evaluated against the final commit, exact packaging closure, and fresh native
  observations; the old environment/pack IDs must never be relabeled as new evidence.
- The exact-pack private-policy record is still pending for all 113 sources.
  `private_replay_gate.py` rejects that pending record before launching. Code CI,
  the source pack's verification, and a general resumption request do not supply
  missing per-source evidence or an invented operator approval.

## Next execution

1. Complete review/checks of the staging fix and land the preparation branch to
   establish final main commit C. Keep the prepared public asset bytes unchanged.
2. Freeze those assets against C. Prepare a separate clean promotion checkout;
   its local `main` must equal C. A linked worktree alone shares the original
   checkout's stale local `main`, so do not update that ref beneath its dirty tree.
3. Resolve storage and remeasure both host and guest capacity. Preserve source
   captures, private evidence, frozen packs, and prior runtime observations.
4. Recheck the packaging closure and actual native runtime for C, seal a new
   environment binding, compile and independently verify a new C-bound pack,
   and retain the matching preparation evidence. No full run can be reused by
   merely changing a recorded commit or pack ID.
5. Complete the exact-pack private-use review; run the seven-stage same-input
   legacy baseline, review its semantic/provenance differences, then perform
   both isolated candidate replays and finalize their measured evidence.
6. Follow the [release runbook](BRAIN-RELEASE-RUNBOOK.md) for the public-field
   review, retained P1B dry run and activation bundle, then exact P1C promotion,
   live canaries, and rollback exercise.

The nightly remains shadow-only. A plain deployment of the new Worker with old
assets would leave Brain requests without their required release selector.
