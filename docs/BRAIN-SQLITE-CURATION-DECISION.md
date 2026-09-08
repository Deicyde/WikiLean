# ADR — Preserve curated contributions across proposal replay

**Status:** Proposed; no data or production authority changed.
**Date:** 2026-09-08
**Deciders:** Jack, after reviewing the complete source plan and semantic comparison.

## Context

P0-R requires folded inputs to resolve to their actual proposal/source evidence.
The committed `container_links.jsonl` and `fc_links.jsonl` are not simply the
result of replaying today's proposal folder. A private diagnostic replay of all
452 proposal files at commit `c64438d2`, using the verified staged catalog and
Mathlib/oracle, exposes two different causes. Its exact Wikidata request plan is
empty, so no further entity lookup is needed for this proposal generation.

Five deliberate curated container contributions were added directly in commit
`d5749a1681acb7a3adbd9230445502eb8d10d2fd`:

| Concept | Curated home | Match kind |
|---|---|---|
| Q11348, function | `Mathlib/Logic/Function` | primitive |
| Q11205, arithmetic | `Mathlib/NumberTheory` | scope |
| Q11563, number | `Mathlib/Data/Nat` | primitive |
| Q395, mathematics | `Mathlib` | scope |
| Q903783, naive set theory | `Mathlib/SetTheory` | survey |

The proposal fold emits 99 container rows, losing these five if its output is
blindly substituted for the existing 104. These are committed curated inputs,
not failed proposals. Their original evidence, confidence and actor strings must
remain attached to the contributions.

The same replay removes 15 of 2,115 FC links. Every removed fully qualified
`(qid, decl)` resolves to an explicit recorded rejection through the existing
`_completed_retract_key` rule. Their original proposal strings omit an enclosing
namespace; a prior folded file retained the completed names despite the rejected
proposal. The current corrected fold respects those decisions. All 2,100 retained
FC rows and all 326 discovery rows are semantically equal to the prior rows;
discovery differences are serialization order only.

Private evidence is retained under
`/Users/jack/.local/share/wikilean-migration/proposal-fold-plan-20260908/`:
`planning-diagnostic.json`, `replay-diagnostic.json`, `legacy-diff.json`, and
`compatibility-findings.json`. This is diagnostic evidence, not a sealed source
export or approved graph baseline. The active draft still binds the original
committed files.

## Decision proposed

Represent the five direct additions as explicit Git-backed curated contributions
alongside the proposal-derived contribution stream. Preserve their exact source
bytes and history. Derive the proposal stream from its sealed input generation,
including the empty entity-request plan and existing rejection decisions. Keep
the 15 FC retractions visible in the semantic comparison; do not classify them
as provenance-only edits or silently relax the P0-R parity gate.

The later assertion ledger must preserve independently sourced equivalent
assertions and target retraction identity. A blanket union of old and new edges
would resurrect rejected claims and is unsuitable.

## Options considered

| Option | Benefit | Consequence |
|---|---|---|
| Keep every committed folded file as an opaque Git input | Preserves current graph bytes | Does not establish the claimed proposal-to-fold derivation; retains the 15 rejected links |
| Replace every file with the fresh fold | Reproduces current proposal logic | Loses five deliberate curated contributions |
| Separate curated contributions and proposal replay | Preserves curation and respects recorded decisions | Needs explicit lineage and a reviewed graph change for the FC retractions |

The third option follows the architecture's separation of curated authority from
derived observations. It introduces more explicit source bookkeeping, but avoids
claiming that a generated file is either entirely curated or entirely replayable
when its history shows both.

## Consequences and action items

- [ ] Encode and independently verify the exact curated/proposal contribution
  split, with no production writer change.
- [ ] Seal the fold's complete source dependencies and original decisions.
- [ ] Add regression coverage preserving the five curated rows and retracting
  the 15 completed identities without collapsing independent assertions.
- [ ] Prepare both the pre-refactor-code comparison on identical verified
  inputs and the separate source/decision drift report.
- [ ] Review the concrete graph delta and baseline before asserting P0-R
  compatibility or accepting genesis. This ADR grants no deployment approval.
