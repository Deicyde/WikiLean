# External review 1 of the Bridge Experiment report

> Received 2026-07-27 via the project owner. Preserved verbatim as a study artifact.
> Independent verification of its factual claims: `verification-of-review-1.json`
> (29 confirmed, 1 partial, 1 refuted — the MathlibMPR PR-clustering claim; MPR has
> one task per PR).

---

## Overall assessment

This is a promising and unusually transparent study, but I would recommend major revision before treating its headline claims as established.

The experiments currently support: The bundled WikiLean tool condition produces more typechecking, non-hallucinated declarations than the tested unjoined-tool condition.

They do not yet cleanly establish: The informal↔formal join itself improves semantically correct autoformalization.

The distinction matters because the primary outcome does not test semantic faithfulness, and several arms differ in ways besides joining.

## Major methodological concerns

### 1. Tier-1 "success" does not measure whether the statement is correct

The preregistered primary endpoint was equivalence to the gold statement. In the implementation, however, the equivalence grader is still a stub, and success is: produced declaration ∧ no extracted hallucinated name ∧ typechecks. See score_bridge.py line 198 and the success construction at line 219.

A declaration can satisfy all three conditions while being weaker, stronger, vacuous, or simply about the wrong mathematical object. Consequently, the 42% versus 16% result is not yet an autoformalization-accuracy result. This also conflicts with the paper's repeated assertion that "typechecking alone never counts as success": adding a name-existence filter does not establish semantic correctness.

Fix: Restore semantic faithfulness as the primary endpoint. Since every task has a gold formal statement: compare elaborated proposition structures after normalization; mechanically accept definitionally equivalent statements; send unresolved cases to two experts blinded to arm, with third-rater adjudication; report strict and permissive equivalence, inter-rater agreement, and sensitivity analyses. The existing 500 fresh outputs can be graded this way before running anything new. Until then, rename the outcome to "grounded typechecking-output rate."

### 2. D versus E does not isolate the join

Arm D contains both the join and the unique decl_exists oracle. Arm E lacks decl_exists. But having zero hallucinated citations is itself part of the success definition. Thus D possesses a tool designed to optimize a component of its primary score.

On the 100 fresh tasks, D made roughly 715 decl_exists calls. E had no equivalent verifier. The paper's trace analysis itself attributes much of the grounding benefit to this verifier.

Therefore, D>E identifies the effect of the entire WikiBrain package, not the join specifically.

Fix: Run a factorial ablation — unjoined baseline (no join, no decl_exists); existence control (no join, decl_exists); join only (join, no decl_exists); full Brain (join, decl_exists). All four conditions should use the same number of exposed tools, response schema, descriptions, token limits, and source corpus. The strongest control would return the same informal and formal information as D but suppress or shuffle the alignments.

### 3. The unjoined control failed its manipulation check

In the fresh runs, arm E used the informal Wikipedia/nLab tools only four times across 100 tasks. Arm B used them 345 times. E therefore behaved almost entirely like a formal-search agent with a larger, more confusing manifest — not like an agent actively consulting both corpora side by side.

This weakens both interpretations: D>E may reflect integration and tool discoverability rather than the correctness of the join; E's 31 missing declarations may reflect interface overload rather than harm from "unjoined information."

Fix: Build a yoked unjoined control with a single integrated interface returning equally concise informal and formal panels without declaring which items correspond. At minimum, preregister a manipulation criterion for actual use of both corpora.

### 4. The "fresh" set was not isolated from all formal-search sources

The formal MCP reads a mutable local checkout and queries the live, unversioned public Loogle service; see formal_mcp.py line 49.

The checkout reflog indicates that during the July 19 fresh runs it was at 61a5e4f338. I found the exact gold declaration basename in that checkout for 60 of the 100 fresh tasks. Moreover, the live Loogle index was not snapshotted. The metadata's held-out check certifies absence from the Brain declaration universe and Brain nodes, but not from these formal-search sources.

This does not necessarily explain D's advantage — if anything, direct source access should benefit C/E — but it invalidates "newer than every arm's index" and "contamination-proof."

Fix: Rerun on a read-only Mathlib checkout strictly predating every test theorem; a locally snapshotted Loogle index from the same revision; per-run hashes for the source tree, search index, Brain snapshot, prompts, and tool manifests. Until then, call this set "post-Brain-index," not contamination-proof.

### 5. The WF retrieval headline is test-set tuned

The N/F/W benchmark results were committed at 22:27. The manual was then created at 00:30, and WF results followed at 01:15. The manual explicitly incorporates results from the same evaluation set: exact nDCG values, tool failures, routing behavior, and the 143/810 format failures. See AGENT_MANUAL.md line 3.

WF is therefore a post-hoc, benchmark-informed intervention. Its score is interesting as development evidence, but it is not an untouched test result or defensible SOTA comparison. It also combines two changes — union tools and the manual.

Fix: Label the present WF result "post-hoc tuned." Freeze the manual using a disjoint development set. Split MathlibQR by its 171 target declarations, not individual paraphrase rows. Evaluate on untouched targets or a new external benchmark. Run bare union, F+manual, W+manual, and union+manual. Give every arm the same generic format reminder or apply an identical format-only repair pass.

### 6. Statistical independence and execution need tightening

Several analyses treat correlated observations as independent: MathlibQR has 810 query rows but only 171 underlying targets; fresh theorems cluster by commit, file, and theorem family; MathlibMPR tasks cluster by PR; SorryDB tasks cluster strongly by repository.

McNemar tests over all 810 QR rows therefore overstate the effective sample size. The study also uses one stochastic run per task/arm, one model per phase, and no confidence intervals for most headline effects.

The fresh arms ran in sequential blocks A→B→C→D→E. The nominal 30-turn budget was not enforced: the logs show 50 C runs, 38 D runs, and 32 E runs exceeding 30 CLI turns, with maxima of 80, 88, and 72.

Fix: Randomize and interleave arms within task and time block. Enforce turns or tool calls mechanically. Run at least 3 seeds per task and two model families. Use cluster-aware paired bootstrap or permutation tests. Report effect sizes and 95% confidence intervals, not primarily p-values. Treat target, PR, commit, or repository as the resampling unit.

### 7. The preregistered primary experiment was not completed

The preregistration required faithful equivalence, two model classes, reseeds, pass@k curves, and tokens-to-solve. Its success criterion was explicitly D>E on faithful@budget and D≤E on tokens-to-solve; see BRIDGE-EXPERIMENT.md line 78.

Those components were not executed, yet the paper discusses the result as if the preregistered hypothesis test had succeeded. The seven listed deviations do not include all these omitted primary elements. Bridge v2 is a valuable exploratory follow-up, but it is not a substitute for the preregistered primary endpoint.

Fix: Add a complete deviations table with every planned component, whether completed, when the decision changed, whether outcomes had been observed, and its inferential consequence. Separate all results into: confirmatory; preregistered but incomplete; exploratory/post-hoc.

### 8. SorryDB provides little evidence about the join

SorryDB compares N, F, and WF, but has no W-only or bare-union condition. WF routes 94% of calls to formal search. The 10 versus 9 proof difference between WF and F is far too small to attribute anything to the Brain, especially with repository clustering and eight missing verdicts.

Cost per proof is also unstable with only 2, 9, and 10 successes.

Fix: Complete every verdict, include W-only and bare-union arms, analyze by repository, and report uncertainty. A second experiment with identical in-loop compiler access would have much greater external validity than the current one-shot setting.

## Recommended claim revisions

- "The join carries the effect" → "The bundled WikiBrain condition outperformed the tested unjoined-tool condition; join-specific attribution remains to be isolated."
- "42% success" → "42% grounded, typechecking-output rate" until equivalence grading is complete.
- "Contamination-proof" → "post-Brain-index" until all formal sources are frozen before the tasks.
- "Sets the best rows we know" → "A post-hoc benchmark-informed union-plus-manual condition obtained the highest observed score."
- Remove "adding the join never made the task more expensive per success"; the proving comparison is 10 versus 9 successes without a join-only arm or meaningful uncertainty.

## Highest-value next steps

1. Blindly equivalence-grade the existing 200 D/E fresh outputs.
2. Reanalyze all benchmarks with cluster-aware confidence intervals.
3. Rerun Tier 1 on a truly frozen post-cutoff set with the 2×2 join × existence design.
4. Freeze the manual on development data and evaluate WF on unseen declaration targets.
5. Only then expand the proving experiment.

The paper has a real contribution hiding inside it: the integrated system appears to change agent behavior substantially, and the existence-verification result may be independently important. The strongest revision would narrow the claims, repair causal identification, and make semantic correctness — not syntactic validity — the center of the study.
