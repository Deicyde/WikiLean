# External review 2 — of the v2 DRAFT (12-page, two slots open)

> Received 2026-08-01 via the project owner, who asked the reviewer for a deep
> peer-review-style pass on draft-v2.pdf. Preserved verbatim as a study artifact.
> Companion artifacts: REVIEW-1.md (of v1), verification-of-review-1.json.

---

I reviewed the complete 12-page V2 draft, including the open judge and union slots, appendices, tables, and figures.

## Peer-review verdict

Major revision; not ready for submission with the two slots open.

V2 is dramatically more credible than V1. It acknowledges the missing primary endpoint, repairs the arm-E outage, corrects clustered retrieval inference, retracts unsupported claims, and distinguishes exploratory from confirmatory evidence. That intellectual honesty will help with reviewers.

The remaining problem is focus: after the corrections, the manuscript reads more like an audit of an experiment than a paper organized around a clear positive scientific contribution. A strong submission needs to decide what it is: a systems paper about the WikiLean Brain (then it needs a clean causal ablation, semantic evaluation, and characterization of the knowledge graph), or a methodological paper about evaluating tool-augmented formalization agents (then the failures, leakage, infrastructure effects, clustering, and post-hoc tool instruction become the central contribution and must be generalized beyond one system). I recommend the first route, with the methodological lessons retained as secondary contributions.

## What must be fixed or added

### 1. Lead with the repaired full-100 result, not the significant 69-task subset

The abstract currently presents the completed-69 contrast first (40.6% versus 23.2%, p=.023), then the repaired full-100 result (42% versus 30%, p=.073). That ordering will look like significance selection. The 69 tasks are a contiguous, infrastructure-selected subset — not a scientifically defined analysis population. Once the missing E rows have been repaired, the corrected full-100 estimate should be the headline: "D exceeded E by 12 percentage points, but the matched comparison was inconclusive at this sample size." The completed-69 analysis belongs in a sensitivity paragraph or supplement.

There is also an inferential mismatch: the paired Wald risk-difference interval barely excludes zero while the exact McNemar p-value is .073. Add one coherent paired inferential method — preferably a cluster-aware paired bootstrap for the risk difference, with its corresponding interval and p-value. Do not mix an exact McNemar test with a Wald interval and interpret both against .05.

### 2. Cluster the fresh-set analysis by theorem family or source commit

The retrieval and SorryDB analyses now respect clustering, but the paper still treats the 100 fresh theorems as independent. They come from roughly 44 commits and contain conspicuous sibling families: several AntitoneOn integral results, bounded-variation variants, monotonicity variants, and so on. That is pseudoreplication. Add: a source-commit or source-file clustered paired bootstrap; a sensitivity analysis that collapses each theorem family to one unit; counts of distinct commits, files, and families. This may widen the D–E uncertainty substantially and should happen before any headline p-value is retained.

### 3. Complete human semantic evaluation; an LLM judge alone is insufficient

This remains the central scientific gap. An uncalibrated Sonnet judge is useful diagnostic evidence but does not turn grounded typechecking into autoformalization accuracy. It may recognize stylistic features of different arms even when explicit arm labels are removed. Minimum acceptable addition: human-grade all 200 D/E outputs, blinded and randomly ordered; two Lean-capable graders with an explicit equivalence rubric and adjudication; report strict equivalence, acceptable-equivalence, kappa/agreement, and disagreement examples. Use mechanical normalization or ProofNetVerif-style symbolic equivalence first, sending only unresolved cases to humans. If full human grading is impossible, oversample every D/E discordant case and report corrected estimates incorporating judge sensitivity and specificity.

### 4. The causal intervention still needs a factorial ablation

The manuscript correctly admits that D bundles the join, decl_exists, a compressed integrated interface, additional curated metadata and external databases, and different tool descriptions and response schemas. The abstract nevertheless says the controls hold "the same corpora unjoined" — not literally true: the Brain contains external sources and curation not exposed through B+C. The bare-union U arm only separates "union tools" from "union tools plus manual." The minimum publishable ablation is the 2×2: unjoined / unjoined+verifier / join-without-verifier / full Brain, with one yoked interface, equivalent payload lengths, the same underlying corpora, identical generic instructions, arms run concurrently and randomized by task. The existing E repair, conducted later than D, cannot remove the time confound. If this experiment cannot be completed, retitle the paper around the bundled WikiBrain intervention, not the join.

### 5. Characterize the Brain as a scientific artifact

The system under test gets only about half a page. Add a compact artifact table: number of cells, declarations, concepts, and bonds; bond types and provenance; human-curated versus automated proportions; Mathlib coverage overall and by domain; benchmark target coverage; snapshot date and construction pipeline; independent audit precision for a random sample of joins; known false-positive and false-negative modes. This also enables a crucial retrieval decomposition: was the correct declaration actually represented and surfaced by the Brain, merely guessed by the model and checked with decl_exists, or produced from model memory without either? Without that trace analysis, the claim that "the content is in the graph; the entry point is the gap" is not demonstrated by W's 0.816 score.

### 6. Add a real Related Work section

Position against at least: TheoremGraph's statement-level informal/formal graph; LeanSearch-v2's concept and global-premise retrieval; concept-driven autoformalization systems such as CRAMF; dependency-graph autoformalization agents such as Aria. CRAMF and Aria are particularly important omissions because they overlap directly with the claimed mechanism. A comparison table should distinguish: unit of retrieval; curated versus learned join; library coverage; system retrieval versus agent use; semantic grader; downstream evaluation; whether a no-join control exists.

### 7. Correct two remaining overclaims

"Memorization quantification": the 59.8%→20% comparison changes benchmark, difficulty, domain, and recency simultaneously — consistent with benchmark familiarity, but not a clean quantification. "Clean independent existence-verifier finding": the hallucination difference is robust descriptively, but not independent or causal — D is the only arm with the verifier, D differs in other ways, and the verifier queries the same oracle used by the grader. Call it "strong mechanism evidence consistent with a verifier effect." Validate the regex/oracle on a blinded sample and give a paired run-level comparison for "any hallucination" (the current "roughly 3×" applies to citation-level rates, and citations cluster within runs).

### 8. Repair the retrieval evaluation before emphasizing agent gains

N has 143/810 format failures — retrieval ability is confounded with output compliance. WF's manual explicitly teaches the observed format failure and benchmark-specific behavior. Published retrievers are single-system calls whereas F/W/WF are multi-call agents. Add or redo: a deterministic extraction pass or identical format-repair turn for every arm; bare U plus F+manual and W+manual if manual effects matter; a held-out target split for any manual result; cost, latency, and calls/query in the results table; separate "retriever" and "agent" blocks; Brain-gold coverage and correct-answer provenance. Until then, WF should remain an engineering demonstration, not a scientific headline.

### 9. Add reproducibility and conflict disclosures

Public or anonymized artifact access; archived prompts, manifests, model identifiers, CLI version, tool descriptions, transcripts; frozen Mathlib and Loogle snapshots; environment instructions; randomization and retry policies; an AI-use statement; a disclosure that the author built and operates the evaluated system.

## What should be cut for page length

Cut from the main paper entirely: §9 response-to-review (a response letter, not a scientific section); §8's retired-claim table (replace with a short Discussion/Conclusion); Appendix A full tool manual (hashed supplementary artifact); Appendix B complete execution inventory (supplement; keep a six-row deviations table in Methods); Appendix C file map (concise artifact-availability paragraph); all footnotes explaining what V1 used to say.

Move to the supplement: the three-basis arm-E table (keep only repaired full-100 in the main text; outage + completed-pair sensitivity in one paragraph); turn-budget sensitivity; full exposure-strata tables; full tool-call census; detailed snapshot-rot narrative; individual per-arm Wilson intervals when the paired contrast is the relevant quantity.

Compress: SorryDB to the F-versus-WF null + tools-versus-no-tools descriptive result + one snapshot-rot sentence; Figure 1 → one compact forest plot of paired effect estimates and CIs; abstract → lead with corrected full-100, semantic-status caveat, verifier association, scope boundary.

## Recommended final structure (~8 pages main)

Introduction and contributions (¾p) · Related work (¾p) · WikiLean artifact and experimental design (1½p) · Statement formalization results, including human equivalence (2p) · Retrieval scope and ablations (1–1½p) · Downstream proving null (½p) · Limitations and discussion (¾p) · Conclusion (¼p). Everything about prior versions, review verification, execution status, manuals, traces, exposure strata, and file maps goes into the supplement.

## Bottom line

The best paper hiding in this draft is: a controlled study of which components of an informal–formal knowledge interface help Lean agents, showing that existence verification improves grounding, concept retrieval and premise retrieval require different signals, and bundled bridge tooling may improve faithful statement formalization — but only after semantic human grading and factorial isolation. Right now, the manuscript proves that the original study was audited responsibly. The next revision needs to spend fewer pages proving that and more pages establishing the artifact, mechanism, and semantic outcome.
