# Token-budget memo: is "primarily AI-moderated" feasible solo?

> Refreshed 2026-08-04 with live telemetry (supersedes the 2026-06-10 estimate memo).
> All dollar figures are **API-equivalent** (`cost_usd_equiv` from claude-agent-sdk's
> `total_cost_usd`); under Max-plan auth no per-token dollars are billed — it is a
> usage proxy at published API prices. Review cohort pinned to `claude-opus-4-7`;
> newtags configured for `claude-sonnet-5` (nightly.env) but measured runs were opus.
> **Calibration (confirmed, sharper):** the binding constraint is the **Max 5-hour
> window** shared by the 03:20 moderate job, manual runs, and Jack's daytime usage —
> not dollars. The retry fix (`site/ops/retry-lib.sh`) makes limit-nights re-queue
> cleanly instead of burning batches; 3–4 of the last 19 review nights were
> zero-token limit nights that cost nothing.

## 1. Measured per-article cost (run_id-stamped, replaces §1 estimates)

Source: `site/cache/.decisions.jsonl` (4,822 records, 2026-06-14 → 2026-08-04),
cross-checked against live `/stats` pipeline_runs (2026-08-04: review 54 runs /
816 articles / 14.07M tokens / $751.51; new 7 / 355 / 1.07M / $71.06; wp-update
37 / 3,603 / 0 tokens / $0.00). Caveat **confirmed**: `tokens` is still the
in+out floor excluding cache traffic (implied $53/MTok vs the $5/$25 list rate);
budget in dollars or budget-tokens, not list-price arithmetic.

| Mode (posted records)   | n     | tokens/article (mean / median) | cost/article | 30-day mean      |
|-------------------------|------:|-------------------------------:|-------------:|-----------------:|
| review (opus)           | 498   | 28.9k / 26.9k                  | $1.54        | 29.6k / $1.57    |
| new (newtags one-offs)  | 55    | 19.7k / 16.8k                  | $1.32        | — (none in 30 d) |
| wp-update (stage-0)     | 768   | **0 / 0**                      | $0.00        | 0 / $0.00        |

Errors are near-free: review errors average 329 tokens, new/wp-update errors 0 —
failures cost retriable time, not window budget. **Stage-0 wp-update re-measure is
answered: 3,603 / 3,603 drift records ran deterministic (0 tokens), 768 re-pins
posted, 2,624 noop.** The old 0.5×–1.0× review-vs-regen bracket was optimistic:
review ($1.54) runs ≈ **1.15× the old regen mean** ($1.34) — seed-decl hints did
not beat regen cost, they bought correction quality instead.

## 2. What a steady-state night costs vs what we observe

Configured night (nightly.env + nightly-moderate.sh defaults):

| Step                      | budget/limit                | expected tokens             | USD-equiv |
|---------------------------|-----------------------------|-----------------------------:|----------:|
| review, 15 articles       | `WIKILEAN_BUDGET_TOKENS` 700k (default) | 15 × 29.6k ≈ **443k** | ~$23.60 |
| wp-update drift sweep     | limit 300                   | **0** (stage-0)             | $0.00     |
| formalize backlog drain   | `FORMALIZE_BUDGET` 800k cap | ≤800k while backlog exists (~14–16 art/night); backlog ~25 as of 2026-07-01, essentially drained → **~0 steady-state** | ~$0–43 |
| brain agents (OFF)        | 500k cap, `AGENTS=0`        | 0                           | $0.00     |

Steady-state total ≈ **0.44M tokens/night (~$24)**; worst case with a full
formalize batch ≈ 1.24M (~$66). **Observed** (last 30 d): 19 review nights;
clean 15-article nights averaged **386k tokens / $20.60** (range 285–535k) —
the model matches reality within ~15%, and review is ~100% of nightly spend.

## 3. Draining the 610-article annotate backlog (sizes the newtags-launchd decision)

The 2026-08-04 annotate worklist held **610 candidates**. Newtags caps at 40
articles/night under a 2M-token budget. The repository now renders its newtags,
moderation, and Brain plists from `site/ops/launchd-plist.template`; operational
installation is host-local and must be verified with `launchctl print` rather
than inferred from a checked-in plist. The last telemetry used by this memo had
a new-mode record on 2026-07-03, so all backlog/yield numbers below remain a
historical planning baseline, not a claim about current launchd state.

- Nights: 610 ÷ 40 = 15.25 → **≥16 nightly runs (~3 weeks)**.
- Tokens: 610 × 19.7k ≈ **12.0M total ≈ $806 equiv**; per night 40 × 19.7k ≈
  789k — **the 40-article LIMIT binds, not the 2M budget**. Raising LIMIT to
  ~100 saturates the budget (100 × 19.7k ≈ 1.97M) and drains in **~7 nights**,
  if the shared Max window tolerates +2M on top of review's ~0.4M.
- Caveats: the 19.7k figure is opus-calibrated (only 4 sonnet records); and
  historical new-mode yield was low (55 posted / 375 attempts — errors are
  token-free but each failed candidate stays unannotated). Installing the job is
  cheap in tokens; watch yield, not spend.

## 4. Corpus-wide 30-day review freshness

Corpus (live 2026-08-04): **778 articles; 552 fresh (≤30 d), 126 stale, 100
never reviewed** — the current 15/night cadence covers 450/month = 58% of the
corpus, which is exactly why the stale+never pool exists.

- Required rate: 778 ÷ 30 = **25.9 articles/night** (set `WIKILEAN_REVIEW_LIMIT≈26`).
- Tokens: 25.9 × 29.6k ≈ **768k/night → ~23.0M/month**.
- USD-equiv: 778 × $1.57 ≈ **$1,221/month** (~$41/night). At a 90-day cadence:
  778 × $1.57 ÷ 3 ≈ **$407/month** — the old memo's "quarterly ≈ $160–320/month"
  bracket was ~1.5× too low because review landed at 1.15× regen, not 0.5–1.0×.

**Conclusion (confirmed, updated):** solo feasibility holds. Today's 15/night
uses ~0.4M of the night's window with a ~20% limit-night rate; full 30-day
freshness needs ~0.77M/night — plausible on one Max plan, but expect more
re-queue nights, which the retry fix now makes safe rather than wasteful.

## 5. The donations ask (API dollars, if someone donated compute)

At api-key pricing the standing bill is: **~$1.2k/month** for corpus-wide 30-day
freshness (778 × $1.57), **+~$0.8k one-time** to drain the 610-article annotate
backlog, + ~$0.9k one-time per further ~670 new articles (concept-layer scale).
Quarterly-cadence fallback: **~$410/month**. Wikidata-universe scale (≈11.7k
articles) remains out of solo reach: ≈ $18.4k/month at 30-day freshness.

**One donor, one Max plan:** the runner is local and self-contained — a donor
running the 03:20 job overnight on their own Max plan contributes 15–25 reviewed
articles/night ≈ 0.44–0.74M tokens ≈ **$24–39/night ($700–1,200/month
API-equivalent)** without touching their daytime usage. One donor closes the
30-day-freshness gap (Jack covers 15/night; the corpus needs 26); a second could
own the newtags drain (≥16 nights, then new-article growth). The pitch: *"one
spare overnight Max window keeps 778 articles of formalized mathematics fresh."*

## 6. Numbers still to re-measure

- [x] Review-mode cost vs regen — **1.15×**, replaces the 0.5×–1.0× bracket (§1)
- [x] wp-update stage-0 hit rate — **100%** of 3,603 drift records (§1)
- [x] Max-plan duty cycle at 15/night — ~0.4M/night with ~20% limit-nights (§2)
- [ ] Sonnet-5 newtags tokens+cost/article (only 4 records; §3 uses opus figures)
- [ ] New-mode terminal yield net of limit-night churn (55/375 looks worse than it is)
- [ ] Full usage dict (cache read/creation) — `tokens` remains an in+out floor
- [ ] Duty cycle at 26/night before recommending `REVIEW_LIMIT=26` permanently
