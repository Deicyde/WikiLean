# bench/ — the Wikibrain benchmark (BRAIN v2 axis 5's referee)

Measures whether giving an agent **Wikibrain MCP access** improves outcomes on
informal ↔ formal math tasks, versus the same model with no tools. Spec:
`docs/BRAIN-V2.md` "Benchmark (axis 5's referee)".

**The design target: T1/T2 lift is the number the Wikibrain API design
optimizes.** If `brain_transfer`/`brain_unit` don't move T1/T2, the API is not
earning its keep — re-design the tools, not the benchmark.

## Quick start

```bash
python3 bench/generate_tasks.py                  # rebuild bench/data/tasks.jsonl (deterministic)
python3 bench/run_benchmark.py --arm no_tools    # baseline arm (~150 eval tasks)
python3 bench/run_benchmark.py --arm wikibrain   # MCP arm (same model, same prompts)
python3 bench/score.py                           # table on stdout + bench/data/summary.json
```

Useful flags: `--limit 5` (smoke), `--split dev` (30-task dev split for prompt
iteration — never tune on eval), `--resume` (skip already-completed task ids),
`--model`, `--concurrency` (default 2), `--dry-run` (print the exact CLI command).
Local Worker: `WIKIBRAIN_MCP_URL=http://localhost:8787/mcp` overrides the URL in
`bench/mcp-config.json` (a resolved copy is written to
`bench/data/.mcp-config.resolved.json`).

**Cost**: the runner shells the `claude` CLI on **Max auth** (`ANTHROPIC_API_KEY`
is removed from the child env — the Max-auth gotcha in CLAUDE.md). ~150 tasks ×
2 arms on the default `claude-haiku-4-5-20251001` is cheap on Max; runs are
opt-in and resumable, nothing here is on the nightly path.

## How tasks are derived (`generate_tasks.py`)

Three gold sources, three task types, 180 tasks (150 eval / 30 dev, stratified,
seed 20260710, rows sorted by id — regeneration is byte-identical):

| type | question | gold sources |
|---|---|---|
| T1 (60) | concept (+statement) → fully-qualified Mathlib decl | `@[wikidata]` tag harvest (`catalog/data/mathlib_tag_xrefs.jsonl`, human-merged into mathlib4 — strongest); `rebuild_grounding.json` `match_kind=exact & confidence=high`; `site/annotations/*.json` formalized statements |
| T2 (60) | decl → Wikidata QID + enwiki slug | tag harvest; grounding exact+high |
| T3 (60) | formalized in Mathlib? YES+witness / NO | YES: annotation statements + grounding-formalized concepts; NO: grounding `status=not_formalized` (balanced 30/30) |

Guard rails: every gold/witness decl is verified against the local decl oracle
(`.claude/skills/mathlib-search/.cache/declaration-data.json`) at generation
time, so no task carries a stale/renamed name; gold accept sets are
multi-to-multi (a QID may accept several decls, a decl several QID/slug pairs);
dedup by QID (T1) / decl (T2); ≤2 statements per article; per-task `provenance`
records exactly which source produced it.

Answers are STRICT final-line formats parsed mechanically (`tasklib.py`):
`ANSWER: <Decl>` (T1) · `ANSWER: <QID> <slug>` (T2) · `ANSWER: YES <Decl>` or
`ANSWER: NO` (T3). Slug comparison is sanitized on both sides (WikiLean slugs
drop apostrophes/parens/en-dashes: `Group_(mathematics)` ≡ `Group_mathematics`).

## Arms (`run_benchmark.py`)

Same model, same prompt — the ONLY difference is tool availability:

- `no_tools` — `--tools ""` (the CLI's allowlist-only form: disables the
  ENTIRE built-in tool set) **plus** the `--disallowedTools` deny-list, plus
  `--strict-mcp-config` with no MCP config.
- `wikibrain` — the deny-list + `--strict-mcp-config`, plus
  `--mcp-config bench/mcp-config.json` and `--allowedTools` for the eight
  `mcp__wikibrain__*` tools
  (`brain_search/node/unit/transfer/neighborhood/snippets/filter`, `decl_exists`).

**Wikibrain preflight + degradation canary.** The claude CLI degrades
*silently* to a no-tools run when the MCP server is unreachable — which would
corrupt the wikibrain arm into a second baseline. So before any task runs, the
wikibrain arm POSTs a JSON-RPC `initialize` to the resolved MCP URL (urllib,
15s timeout) and **aborts the whole run (exit 2)** on non-200 / invalid
JSON-RPC, naming the URL. `--dry-run` skips the preflight but prints what it
would do. The preflight only checks the START of the run, so a post-run canary
WARNs when every completed wikibrain task finished in ≤1 turn (= 0 tool calls
— the mid-run-degradation signature).

### Isolation (both arms)

Every CLI child runs from a **freshly-created empty temp dir OUTSIDE the repo**
(`tempfile.mkdtemp`, removed after the run). This matters because the claude
CLI keys almost all ambient context by cwd: it discovers `CLAUDE.md` by walking
up from cwd, loads `.claude/` settings/hooks/skills from the cwd project, and
loads the user-level auto-memory from `~/.claude/projects/<mangled-cwd>/` —
keyed by cwd, so moving cwd out of the repo also detaches the WikiLean project
memory (a temp cwd gets a fresh, empty project key; there is no user-global
`~/.claude/CLAUDE.md` on this machine). Running from inside the repo would
contaminate BOTH arms with repo instructions, memory, and SessionStart hooks.

`no_tools` isolation is therefore **cwd isolation + tool allowlist/deny-list
together**: the deny-list alone can never be exhaustively closed (new built-in
tools appear), `--tools ""` closes the built-in set allowlist-style, and even
if a file tool slipped through it would see only an empty directory. (The CLI's
`--bare` flag would be stronger still, but it disables OAuth/keychain auth and
the runner depends on Max auth — don't use it here.)

Results stream to `bench/data/results_<arm>_<model>.jsonl` (one row per task:
`task_id, answer_raw, answer_parsed, latency_s, n_turns, cost_usd, error?`).
`n_turns` is the tool-use proxy (also feeds the degradation canary);
per-tool-call logs would need `--output-format stream-json` (not wired up —
keep the runner dumb).

## Scoring (`score.py`)

- **T1**: `exact_gold` (primary) plus `exists_but_different` / `not_found` via
  the decl-existence oracle (`mathlib_search.py decl <name> --json`, cached in
  `bench/data/.decl_cache.json`) — separating "picked a different real decl"
  from hallucination.
- **T2**: `qid_exact`, `slug_exact`, `pair_exact` (primary).
- **T3**: `accuracy` (primary) + `witness_gold` / `witness_valid` over YES answers.
- **Lift** = wikibrain − no_tools on each primary metric, computed on the
  intersection of task ids answered by both arms, with a **95% paired-bootstrap
  CI** (resample task ids present in both arms, 10,000 resamples, fixed seed
  20260710, stdlib `random` — the arms answer the *same* tasks, so an
  independent-samples CI would overstate the variance). Per-arm k/n rates are
  reported as before. `--no-oracle` scores offline (existence metrics degrade
  gracefully).
- **Splits**: `score.py` scores only `split=="eval"` rows by default (dev is
  for prompt iteration — never quote dev numbers); `--include-dev` overrides.
  It prints which split(s) were scored and how many rows it skipped.
- Answer parsing (`tasklib.py`) normalizes curly quotes to ASCII and strips
  surrounding backtick/bold wrappers (`` `x` ``, `**x**`) from the ANSWER
  payload; `python3 bench/tasklib.py` runs the parser self-test.

## Contamination & caveats (read before quoting numbers)

- **Models may know the gold pairs from training** — `@[wikidata]` tags are
  public in mathlib4, grounding descends from public data. Absolute scores are
  therefore inflated and NOT the point; **the benchmark measures LIFT between
  arms on the same model**, which contamination affects equally.
- T3 has a shape asymmetry: statement-level items are all gold-YES (the NO side
  is concept-level, from grounding `not_formalized`). Concept-level items appear
  on both sides (10 YES / 30 NO) so shape alone doesn't decide the answer, but
  don't read T3 sub-slices as calibrated.
- Grounding `not_formalized` is the best available negative gold; a genuinely
  formalized concept the grounding pass missed would mis-score a correct YES.
- Annotation-derived gold is AI-written (provenance recorded per task) — weaker
  than the human-merged tag rows; T1's primary signal deliberately leans on the
  tag harvest (30/60).
- The generation-time oracle filter drops real-but-undocumented decls the
  doc-gen4 cache misses (see `mathlib_decl_oracle_incomplete` memory) — a purity
  trade: every kept gold decl definitely exists.
