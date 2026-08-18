# WP measurement — brain_premises on MathlibMPR (one-shot)

**Honesty header.** One-shot held-out evaluation, run once post-deploy (2026-08-18) and scored as-is — no tuning against MPR, no reruns for a better number (brain_premises was developed on LeanDojo Benchmark-4's premise split only). Gold pinned to frenzymath/LeanSearch-v2@94f4888cbaf9 (CC BY 4.0). Comparison arms are the frozen rows in `bench/v2/runs/agent/mpr/` (same prompt, model claude-sonnet-5, turn budget); W's toolset is pinned to 8 explicit names, WP adds only `mcp__wikibrain__brain_premises`. The live Brain index drifts nightly and run dates differ across arms — that drift is uncontrolled and shared by every remote arm. Scoring is byte-identical to `bench/v2/score_retrieval.py`.

Reproduce: `python3 bench/analysis/wp_measurement.py` (seed 20260727, B=10,000).

## Arms — group-Recall@10 over the 69 MPR tasks

| arm | gR@10 (per-task mean) | boot 95% CI | pooled groups | Wilson 95% CI (pooled) |
|---|---|---|---|---|
| N | 0.2025 | [0.1290, 0.2832] | 31/204 | [0.1092, 0.2076] |
| F | 0.5468 | [0.4610, 0.6317] | 98/204 | [0.4128, 0.5487] |
| W | 0.2721 | [0.1943, 0.3539] | 48/204 | [0.1823, 0.2981] |
| WF | 0.5569 | [0.4693, 0.6425] | 103/204 | [0.4368, 0.5728] |
| U | 0.5485 | [0.4633, 0.6345] | 100/204 | [0.4224, 0.5583] |
| WP | 0.3191 | [0.2412, 0.4006] | 55/204 | [0.2134, 0.3343] |

## Paired contrasts (per-task, n=69)

| contrast | mean diff | boot 95% CI | sign (+/−) | sign p | Wilcoxon p |
|---|---|---|---|---|---|
| WP − W | +0.0470 | [-0.0033, +0.0998] | 15/8 | 0.21 | 0.0731 |
| WP − F | -0.2277 | [-0.3200, -0.1387] | 6/30 | 6.96e-05 | 3.37e-05 |
| WP − U | -0.2294 | [-0.3189, -0.1449] | 4/31 | 3.47e-06 | 1.64e-05 |
| WP − WF | -0.2379 | [-0.3327, -0.1431] | 6/35 | 4.87e-06 | 3.13e-05 |

## brain_premises usage (WP arm)

- calls per run: mean 1.4058, median 1, range 0-4, total 97; 65/69 runs made >=1 call (0-call runs: ['mathlibmpr_033', 'mathlibmpr_044', 'mathlibmpr_052', 'mathlibmpr_055'])
- results containing gold: 31/97 brain_premises calls returned >=1 gold-group member; 28/69 tasks saw a gold member in a brain_premises result
- cost: $31.60 total, $0.458/task, mean wall 52s
- all tool calls: `{"mcp__wikibrain__decl_exists": 225, "mcp__wikibrain__brain_search": 126, "mcp__wikibrain__brain_bridge": 125, "mcp__wikibrain__brain_cell": 123, "mcp__wikibrain__brain_premises": 97, "mcp__wikibrain__brain_snippets": 43, "mcp__wikibrain__brain_neighborhood": 28, "mcp__wikibrain__brain_filter": 7, "Bash": 2, "mcp__wikibrain__brain_transfer": 1}`

## Hit provenance (resolved pass, chronological first entry)

For every covered gold group, the class of its top-ranked hit name (retrieval_provenance.py method, full stream transcripts):

### WP — 55 covered-group hits
- by class: `{"guessed_verified": {"n": 41, "frac": 0.7455}, "surfaced": {"n": 14, "frac": 0.2545}}`
- surfaced by tool: `{"brain_premises": 6, "decl_exists": 6, "brain_neighborhood": 2}`

### W — 48 covered-group hits
- by class: `{"guessed_verified": {"n": 44, "frac": 0.9167}, "surfaced": {"n": 4, "frac": 0.0833}}`
- surfaced by tool: `{"brain_bridge": 2, "brain_search": 1, "decl_exists": 1}`

## Per-task table

| qid | groups | N | F | W | WF | U | WP | WP−W |
|---|---|---|---|---|---|---|---|---|
| mathlibmpr_003 | 1 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | +0.000 |
| mathlibmpr_005 | 2 | 1.000 | 0.500 | 1.000 | 0.500 | 0.500 | 1.000 | +0.000 |
| mathlibmpr_006 | 3 | 0.000 | 0.333 | 0.000 | 0.333 | 0.667 | 0.000 | +0.000 |
| mathlibmpr_008 | 1 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | +0.000 |
| mathlibmpr_009 | 3 | 0.333 | 0.667 | 0.667 | 0.667 | 0.667 | 0.333 | -0.333 |
| mathlibmpr_010 | 1 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | +0.000 |
| mathlibmpr_011 | 5 | 0.200 | 0.600 | 0.200 | 0.600 | 0.600 | 0.600 | +0.400 |
| mathlibmpr_012 | 1 | 1.000 | 1.000 | 0.000 | 1.000 | 1.000 | 1.000 | +1.000 |
| mathlibmpr_014 | 3 | 0.000 | 0.333 | 0.333 | 0.333 | 0.333 | 0.333 | +0.000 |
| mathlibmpr_015 | 2 | 0.500 | 1.000 | 0.500 | 0.500 | 1.000 | 0.500 | +0.000 |
| mathlibmpr_016 | 3 | 0.000 | 0.333 | 0.333 | 0.333 | 0.333 | 0.667 | +0.333 |
| mathlibmpr_017 | 3 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | +0.000 |
| mathlibmpr_018 | 2 | 0.000 | 0.500 | 0.000 | 0.000 | 0.000 | 0.000 | +0.000 |
| mathlibmpr_019 | 2 | 0.000 | 0.500 | 0.500 | 0.000 | 0.500 | 0.500 | +0.000 |
| mathlibmpr_020 | 2 | 0.000 | 1.000 | 0.000 | 1.000 | 1.000 | 0.000 | +0.000 |
| mathlibmpr_021 | 1 | 0.000 | 1.000 | 0.000 | 1.000 | 1.000 | 0.000 | +0.000 |
| mathlibmpr_022 | 7 | 0.000 | 0.286 | 0.000 | 0.286 | 0.286 | 0.000 | +0.000 |
| mathlibmpr_023 | 4 | 0.000 | 0.250 | 0.500 | 0.250 | 0.250 | 0.250 | -0.250 |
| mathlibmpr_024 | 2 | 0.500 | 1.000 | 1.000 | 0.000 | 1.000 | 1.000 | +0.000 |
| mathlibmpr_025 | 3 | 0.667 | 0.667 | 0.333 | 0.667 | 0.667 | 0.667 | +0.333 |
| mathlibmpr_027 | 3 | 0.667 | 0.667 | 0.667 | 1.000 | 1.000 | 0.667 | +0.000 |
| mathlibmpr_028 | 2 | 0.500 | 1.000 | 0.500 | 1.000 | 1.000 | 0.500 | +0.000 |
| mathlibmpr_029 | 3 | 0.000 | 0.000 | 0.333 | 0.667 | 0.333 | 0.333 | +0.000 |
| mathlibmpr_030 | 2 | 0.500 | 0.500 | 0.000 | 0.500 | 0.500 | 0.500 | +0.500 |
| mathlibmpr_031 | 2 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | +0.000 |
| mathlibmpr_032 | 2 | 0.000 | 1.000 | 0.500 | 0.500 | 1.000 | 0.000 | -0.500 |
| mathlibmpr_033 | 3 | 0.000 | 0.333 | 0.000 | 0.333 | 0.333 | 0.000 | +0.000 |
| mathlibmpr_034 | 6 | 0.000 | 0.667 | 0.167 | 0.500 | 0.500 | 0.167 | +0.000 |
| mathlibmpr_035 | 5 | 0.400 | 0.200 | 0.200 | 0.400 | 0.400 | 0.000 | -0.200 |
| mathlibmpr_036 | 4 | 0.500 | 0.500 | 0.500 | 0.500 | 0.500 | 0.500 | +0.000 |
| mathlibmpr_037 | 3 | 0.000 | 0.000 | 0.000 | 0.333 | 0.333 | 0.000 | +0.000 |
| mathlibmpr_038 | 3 | 0.000 | 0.333 | 0.000 | 0.333 | 0.333 | 0.000 | +0.000 |
| mathlibmpr_039 | 3 | 0.333 | 0.333 | 0.333 | 0.667 | 0.667 | 0.667 | +0.333 |
| mathlibmpr_040 | 1 | 0.000 | 1.000 | 0.000 | 1.000 | 1.000 | 0.000 | +0.000 |
| mathlibmpr_042 | 1 | 0.000 | 1.000 | 0.000 | 1.000 | 0.000 | 0.000 | +0.000 |
| mathlibmpr_043 | 4 | 0.250 | 0.750 | 0.500 | 0.750 | 1.000 | 0.250 | -0.250 |
| mathlibmpr_044 | 1 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | +0.000 |
| mathlibmpr_045 | 3 | 0.000 | 1.000 | 0.000 | 1.000 | 0.333 | 0.333 | +0.333 |
| mathlibmpr_046 | 3 | 0.000 | 1.000 | 0.333 | 1.000 | 1.000 | 0.333 | +0.000 |
| mathlibmpr_048 | 1 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | +0.000 |
| mathlibmpr_049 | 4 | 0.250 | 0.500 | 0.500 | 0.500 | 0.500 | 0.500 | +0.000 |
| mathlibmpr_050 | 4 | 0.000 | 0.000 | 0.000 | 0.250 | 0.000 | 0.000 | +0.000 |
| mathlibmpr_051 | 8 | 0.125 | 0.500 | 0.000 | 0.250 | 0.250 | 0.125 | +0.125 |
| mathlibmpr_052 | 2 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | +0.000 |
| mathlibmpr_053 | 1 | 0.000 | 1.000 | 0.000 | 0.000 | 1.000 | 0.000 | +0.000 |
| mathlibmpr_055 | 3 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | +0.000 |
| mathlibmpr_056 | 4 | 0.000 | 0.750 | 0.000 | 0.750 | 0.750 | 0.000 | +0.000 |
| mathlibmpr_057 | 3 | 0.333 | 0.667 | 0.000 | 1.000 | 0.667 | 0.333 | +0.333 |
| mathlibmpr_058 | 4 | 0.000 | 0.250 | 0.000 | 1.000 | 1.000 | 0.250 | +0.250 |
| mathlibmpr_060 | 3 | 0.333 | 0.333 | 0.333 | 0.000 | 0.333 | 0.333 | +0.000 |
| mathlibmpr_062 | 1 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 0.000 | +0.000 |
| mathlibmpr_063 | 7 | 0.000 | 0.143 | 0.000 | 0.143 | 0.143 | 0.000 | +0.000 |
| mathlibmpr_064 | 3 | 0.333 | 0.333 | 0.333 | 1.000 | 0.333 | 0.333 | +0.000 |
| mathlibmpr_065 | 3 | 0.000 | 0.667 | 0.333 | 0.000 | 0.667 | 0.000 | -0.333 |
| mathlibmpr_066 | 4 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.250 | +0.250 |
| mathlibmpr_068 | 4 | 0.250 | 0.500 | 0.250 | 0.500 | 0.250 | 0.250 | +0.000 |
| mathlibmpr_069 | 2 | 0.000 | 0.000 | 0.000 | 0.500 | 0.500 | 0.000 | +0.000 |
| mathlibmpr_070 | 1 | 0.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | +0.000 |
| mathlibmpr_071 | 4 | 0.000 | 0.500 | 0.750 | 0.500 | 0.500 | 0.500 | -0.250 |
| mathlibmpr_072 | 2 | 0.000 | 0.500 | 0.000 | 0.500 | 0.500 | 0.500 | +0.500 |
| mathlibmpr_073 | 8 | 0.000 | 0.500 | 0.375 | 0.500 | 0.500 | 0.125 | -0.250 |
| mathlibmpr_075 | 6 | 0.000 | 0.833 | 0.000 | 0.833 | 0.667 | 0.167 | +0.167 |
| mathlibmpr_076 | 4 | 0.000 | 0.250 | 0.000 | 0.250 | 0.250 | 0.250 | +0.250 |
| mathlibmpr_077 | 1 | 0.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | +0.000 |
| mathlibmpr_078 | 2 | 0.000 | 0.500 | 0.000 | 0.500 | 0.000 | 0.500 | +0.500 |
| mathlibmpr_079 | 4 | 0.000 | 0.750 | 0.000 | 0.750 | 0.250 | 0.000 | +0.000 |
| mathlibmpr_080 | 4 | 0.000 | 0.000 | 0.500 | 0.750 | 0.750 | 0.500 | +0.000 |
| mathlibmpr_081 | 1 | 0.000 | 1.000 | 0.000 | 1.000 | 0.000 | 0.000 | +0.000 |
| mathlibmpr_082 | 1 | 1.000 | 1.000 | 0.000 | 1.000 | 1.000 | 0.000 | +0.000 |

