# Retrieval provenance — how the gold name entered the transcript

REVIEW-2 §5b. For every QR-810 agent hit (gold in final top-10), the
chronologically FIRST entry of the gold declaration name:
**surfaced** (a: appeared in a tool result before the model wrote it),
**guessed_verified** (b: model wrote it in a tool arg, then confirmed),
**memory** (c: never in any tool input/result), plus in_query /
written_unconfirmed / undecidable (truncated-trace conservative pass).

Data: `bench/v2/runs/agent/qr810/{W,WF,F}/claude-sonnet-5/<qid>.json (tool_trace, result_head cap 4000) + <qid>.stream.jsonl.gz (full tool results)`
Gold/hit: `bench/v2/data/MathlibQR.json restricted to MathlibQR_shared171.json (fair-810), exact-name top-10 hit as in bench/v2/score_retrieval.py`

Conservative pass = tool_trace only (4000-char result_head; a
truncated result preceding the first definite occurrence =>
undecidable). Resolved pass = full tool results from the
stream transcripts (no truncation).

## Conservative pass

| arm | hits | surfaced | guessed_verified | memory | in_query | written_unconfirmed | undecidable |
|---|---|---|---|---|---|---|---|
| W | 661 | 63 (9.5%) | 470 (71.1%) | 2 (0.3%) | 8 (1.2%) | 0 | 118 (17.8%) |
| WF | 717 | 262 (36.5%) | 313 (43.6%) | 6 (0.8%) | 8 (1.1%) | 0 | 128 (17.8%) |
| F | 673 | 151 (22.4%) | 166 (24.7%) | 314 (46.7%) | 8 (1.2%) | 0 | 34 (5.1%) |

## Resolved pass

| arm | hits | surfaced | guessed_verified | memory | in_query | written_unconfirmed | undecidable |
|---|---|---|---|---|---|---|---|
| W | 661 | 69 (10.4%) | 582 (88.0%) | 2 (0.3%) | 8 (1.2%) | 0 | 0 |
| WF | 717 | 273 (38.1%) | 424 (59.1%) | 12 (1.7%) | 8 (1.1%) | 0 | 0 |
| F | 673 | 151 (22.4%) | 170 (25.3%) | 344 (51.1%) | 8 (1.2%) | 0 | 0 |

## Resolved detail

### W
- surfaced by tool: `{"brain_bridge": 58, "brain_search": 7, "decl_exists": 3, "brain_transfer": 1}`
- surfaced by channel: `{"brain": 66, "verify": 3}`
- guessed_verified first written in: `{"decl_exists": 576, "brain_search": 4, "brain_bridge": 2}`
- guessed_verified with a decl_exists exists:true for the gold: 582
- guessed_verified where the oracle also REJECTED >=1 candidate (exists:false): 471
- conservative undecidable resolved to: `{"guessed_verified": 112, "surfaced": 6}`
- trace-vs-stream agreement: `{"agree": 543, "disagree": 0, "undecidable_resolved": 118}`

| style | surfaced | guessed_verified | memory | in_query | written_unconfirmed | undecidable |
|---|---|---|---|---|---|---|
| q1a_lean | 4 | 134 | 1 | 4 | 0 | 0 |
| q1b_latex | 16 | 121 | 0 | 1 | 0 | 0 |
| q1c_natural | 13 | 129 | 1 | 0 | 0 | 0 |
| q2_slogan | 17 | 116 | 0 | 0 | 0 | 0 |
| q3_nickname | 16 | 71 | 0 | 3 | 0 | 0 |
| q4_special_case | 3 | 11 | 0 | 0 | 0 | 0 |

### WF
- surfaced by tool: `{"decl_grep": 115, "brain_bridge": 99, "loogle": 37, "decl_read": 18, "decl_exists": 4}`
- surfaced by channel: `{"formal": 170, "brain": 99, "verify": 4}`
- guessed_verified first written in: `{"decl_exists": 282, "decl_grep": 100, "loogle": 39, "brain_bridge": 3}`
- guessed_verified with a decl_exists exists:true for the gold: 421
- guessed_verified where the oracle also REJECTED >=1 candidate (exists:false): 138
- conservative undecidable resolved to: `{"guessed_verified": 111, "surfaced": 11, "memory": 6}`
- trace-vs-stream agreement: `{"agree": 589, "disagree": 0, "undecidable_resolved": 128}`

| style | surfaced | guessed_verified | memory | in_query | written_unconfirmed | undecidable |
|---|---|---|---|---|---|---|
| q1a_lean | 50 | 101 | 0 | 4 | 0 | 0 |
| q1b_latex | 54 | 94 | 5 | 1 | 0 | 0 |
| q1c_natural | 59 | 91 | 2 | 0 | 0 | 0 |
| q2_slogan | 61 | 82 | 3 | 0 | 0 | 0 |
| q3_nickname | 42 | 48 | 1 | 3 | 0 | 0 |
| q4_special_case | 7 | 8 | 1 | 0 | 0 | 0 |

### F
- surfaced by tool: `{"decl_grep": 95, "loogle": 36, "decl_read": 20}`
- surfaced by channel: `{"formal": 151}`
- guessed_verified first written in: `{"loogle": 103, "decl_grep": 66, "Bash": 1}`
- guessed_verified with a decl_exists exists:true for the gold: 0
- guessed_verified where the oracle also REJECTED >=1 candidate (exists:false): 0
- conservative undecidable resolved to: `{"memory": 30, "guessed_verified": 4}`
- trace-vs-stream agreement: `{"agree": 639, "disagree": 0, "undecidable_resolved": 34}`

| style | surfaced | guessed_verified | memory | in_query | written_unconfirmed | undecidable |
|---|---|---|---|---|---|---|
| q1a_lean | 36 | 25 | 88 | 4 | 0 | 0 |
| q1b_latex | 32 | 41 | 62 | 1 | 0 | 0 |
| q1c_natural | 33 | 41 | 74 | 0 | 0 | 0 |
| q2_slogan | 28 | 34 | 77 | 0 | 0 | 0 |
| q3_nickname | 19 | 28 | 36 | 3 | 0 | 0 |
| q4_special_case | 3 | 1 | 7 | 0 | 0 | 0 |

N context: 513/810 hits, N has zero tool calls; all hits are memory by construction.
