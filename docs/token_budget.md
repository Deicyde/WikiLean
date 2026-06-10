# Token-budget memo: is "primarily AI-moderated" feasible solo?

> P1 Wave A deliverable. Gates the "AI-moderated" claim and sizes the donations ask.
> All dollar figures are **API-equivalent** (`cost_usd_equiv` from claude-agent-sdk's
> `total_cost_usd`); under Max-plan auth no per-token dollars are billed — it is a
> usage proxy at published API prices. Model: `claude-opus-4-7` (batch_annotate.py:210),
> currently $5 / $25 per MTok input/output (cache reads ~0.1×, cache writes 1.25×,
> Batches API −50%).

## 1. Measured per-article cost (regen pipeline)

Source: `site/cache/.batch_run.log` (JSONL, created 2026-05-27, last write 2026-06-02
— a ~6-day run window). **n = 610** success records with full telemetry (all
regen-style; no `moderate` mode exists yet), plus 46 older cost-only records
(mean $1.65, total $75.87). Caveat: the `tokens` field is **input+output only** —
it excludes cache-read/cache-creation tokens, which dominate agentic loops (implied
$47/MTok vs the $5/$25 list rate proves this). Treat `tokens` as a floor; budget in
dollars.

| Metric (per article)            | median | mean   | p90    | total (n=610) |
|---------------------------------|-------:|-------:|-------:|--------------:|
| Cost, USD-equiv                 | $1.13  | $1.34  | $2.60  | $818          |
| — Agent 1 (enumerate, tool-free)| $0.26  | $0.33  | $0.64  | $199 (24%)    |
| — Agent 2 (Mathlib grep, tools) | $0.81  | $1.02  | $2.04  | $620 (76%)    |
| Tokens (in+out floor)           | 22.8k  | 28.4k  | 56.2k  | 17.3M         |
| Agent 2 tool calls              | 24     | 31.7   | 60     | 19,316        |
| Wall-clock, s                   | 818    | 1,229  | 2,791  | 750k (~8.7 d) |
| Annotations produced            | 39     | 44.1   | 79     | 26,905        |

Failures: 21 `extract_failed`, 5 `agent2_no_json`, 5 max-turns among terminal errors.
The log also contains **11,258** `"error result: success"` lines across 1,310 slugs —
SDK/rate-limit churn, not real failures (wall-clock mean 20 min/article vs ~4.5 min
of agent compute is the same throttling signature). 713 slugs appear only as errors
in this log; most were completed in runs/pipelines not captured here, so a true
terminal-failure rate **cannot be computed from this log** — re-measure (see §5).

## 2. Scenario table

Corpus sizes (verified 2026-06-10): **709** D1 rows (canonical; 662 disk
`site/annotations/*.json` non-`.agent1`); concept-tagged catalog **1,377**
(`pilot_tagged.jsonl` 354 + `tier2_tagged.jsonl` 1,023); Wikidata universe
**11,681** (`wikidata_universe.jsonl`); full WikiProject Math snapshot **29,135**
(`articles.jsonl`).

**One-time annotation** at measured mean $1.34/article (regen):

| Target                          | new articles | USD-equiv | tokens (floor) |
|---------------------------------|-------------:|----------:|---------------:|
| 709 → concept layer (1,377)     | 668          | ~$0.9k    | ~19M           |
| 709 → Wikidata universe (11,681)| 10,972       | ~$14.7k   | ~312M          |
| 709 → full catalog (29,135)     | 28,426       | ~$38.1k   | ~807M          |

**Steady-state review loop.** Review cost is *unmeasured* (moderate.py not shipped);
assume **0.5×–1.0× of regen** = $0.67–$1.34/article (review re-reads the article +
existing annotations but should grep less). Tokens/day uses the 28.4k floor.

| Corpus × cadence        | articles/day | tokens/day (floor) | USD-equiv/day | /month        |
|-------------------------|-------------:|-------------------:|--------------:|--------------:|
| 709 every 30 d          | 23.6         | 0.67M              | $16–32        | $475–950      |
| 709 every 90 d          | 7.9          | 0.22M              | $5–11         | $160–320      |
| 709 every 180 d         | 3.9          | 0.11M              | $2.60–5.30    | $79–158       |
| 1,377 every 90 d        | 15.3         | 0.43M              | $10–21        | $310–620      |
| 11,681 every 90 d       | 130          | 3.7M               | $87–174       | $2.6k–5.2k    |
| 29,135 every 90 d       | 324          | 9.2M               | $217–434      | $6.5k–13k     |

## 3. Subscription reality

**Empirical anchor:** the measured run burned ~$894 API-equiv in ~6 days on Jack's
one Max plan — **~$150/day burst**, achieved *with* visible throttling (the 11k
retry lines; 4× wall-clock vs compute time).

**Assumptions (stated as assumptions — exact Max quotas are unpublished and shift):**
Max plans meter usage in 5-hour rolling session windows plus a weekly cap; the
6-day burst above may have leaned on headroom that a weekly cap won't sustain
indefinitely, and the same plan also covers Jack's interactive dev work. Bracket
sustainable steady-state at **$20/day (low) – $100/day (high)** API-equivalent of
Opus usage from one Max plan.

**Conclusions:**
- **Fits one Max plan comfortably:** 709 @ 90 d or 180 d ($3–11/day) — even at the
  low bracket, with room left for dev. The "primarily AI-moderated" claim is
  **feasible solo at quarterly cadence for the current corpus.**
- **Fits at the high bracket / marginal at low:** 709 @ 30 d ($16–32/day) and
  1,377 @ 90 d ($10–21/day). Doable solo but eats a large share of the plan.
- **Needs donated compute or a cheaper pipeline:** anything at Wikidata-universe
  scale or beyond — $2.6k–13k/month quarterly review, plus $15k–38k one-time
  annotation.
- **Headline for the donations ask:** *"Reviewing all 709 live articles quarterly
  ≈ $160–320/month API-equivalent; the 1,377-article concept layer ≈ $310–620/month;
  the full 29k-article WikiProject Math universe ≈ $6.5k–13k/month."*

## 4. Levers (multipliers on the $1.34 mean)

1. **Agent 1 on a smaller model + Batches API.** Agent 1 is tool-free
   (`run_agent(..., [], 12)`, batch_annotate.py:349) → trivially portable to a
   direct Messages/Batches call. Sonnet 4.6 ($3/$15) = 0.6×, Haiku 4.5 ($1/$5) =
   0.2×, Batches −50% on top → Agent 1 at 0.3× (Sonnet+batch) or 0.1×
   (Haiku+batch). Saves $0.23–0.30/article (**17–22% of total**). Note: Batches
   does not apply to the current interactive agent-SDK loop as-is.
2. **Seed-decl hints for Agent 2.** Agent 2 is 76% of cost; median 24 grep turns
   over mathlib4. Feeding prior decls / the @[wikidata]-tag index / the ~300k-name
   decl index as candidates should cut grep turns substantially; halving Agent 2
   saves ~$0.51/article (**~38%**). Review mode gets this for free (existing
   annotations are the hints) — the basis for the 0.5× bracket.
3. **Stage-0 deterministic wp-update: zero tokens.** Re-pin + dry-run wrap handles
   anchor-preserving Wikipedia drift with no model call. If most drift is stage-0
   (re-measure!), operation (3) is nearly free and the review cadence dominates.
4. **Combined optimistic floor:** ~$0.50/article review → 709 quarterly ≈
   **$120/month**, 29k quarterly ≈ **$4.9k/month**.

## 5. Numbers to re-measure after moderate.py ships

run_id-stamped telemetry replaces this memo's estimates. Per record, log:

- [ ] `run_id`, ISO timestamp, `mode` (new|review|wp-update), model, prompt_sha,
      mathlib_sha (already planned in P1)
- [ ] **Full usage dict** incl. `cache_read_input_tokens` / cache-creation — fixes
      the in+out-only floor (batch_annotate.py:234)
- [ ] **Review-mode cost vs regen** — replaces the assumed 0.5×–1.0× bracket
- [ ] **wp-update stage-0 hit rate** — fraction of drift resolved with 0 tokens
- [ ] **True terminal failure rate** + retries (net of the SDK "success" churn)
- [ ] **Max-plan duty cycle:** sustained $/day over ≥1 full week — validates the
      $20–100/day bracket and the solo-feasibility conclusion
- [ ] Agent 2 tool-call count with vs without seed-decl hints (lever 2)
- [ ] Per-agent cost split once Agent 1 moves off Opus (lever 1)
