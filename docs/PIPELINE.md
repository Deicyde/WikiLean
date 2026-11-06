# WikiLean's own locate pipeline

**Status (2026-11-06): the plan.** Section 4's table is the order the stages are built in and
says, per stage, whether it is built yet; it is updated as each one lands, and section 10
lists what is not done. Nothing here changes a published result: section 7 is the argument,
and section 8 is how each stage is proved against batch 1.

## 0. Why this document exists

The formalization reader ([ARTICLES.md](ARTICLES.md)) takes **sealed locate record sets**.
Until now every one of them was produced by the formal-companion skill's locate pipeline,
and WikiLean held only the consumer half: `scripts/articles/build_store.py`, which reads a
set, re-derives every status in it and writes the static store.

The maintainer's ruling of 2026-11-06:

> "Formal-companion is for making Lean companions/informalizations. It is related, but not
> the same as WikiLean which has it's own independent pipeline (or at least it should). The
> only connection is that they both use Literature Links."

and, earlier, "Contribute mode is for wikilean, not for formal companion skill."

So WikiLean grows the **producer** half. The rules that follow from the ruling, and that
every section below is held to:

1. **No import of formal-companion, and no formal-companion command.** Not as a library, not
   as a vendored copy, not as a subprocess. A stage that cannot be built here without one is
   not built here yet, and says so.
2. **One shared piece: Literature Links.** Both sides vendor it; neither vendors the other.
   Section 6 is the pin.
3. **The record-set format stays the contract both sides implement.** It is not
   formal-companion's property: it is written down in ARTICLES.md section 3, checked by
   `build_store.py`, and now written by WikiLean's own export. A set from either producer
   builds the same store.
4. **No published result moves.** Batch 1's exports, their receipts and the live store are
   bytes; the move adds a second producer of those bytes and changes none of them
   (section 7).

## 1. What was already WikiLean's

This is worth stating first, because it is most of the hard part and it is done.

`scripts/articles/build_store.py` is **not** a formal-companion client. It is a WikiLean
implementation of the record-set contract that already:

- reads a sealed `cf-locate-recordset/1` or `/2` set and checks the seal, every file's
  sha256, and that nothing unnamed is present;
- holds the universe, the sources, every record and every locate-extension key to the
  contract, refusing by name;
- validates every record against the **vendored** Literature Links schema, through
  `site/native_annotations.validate_link_record`, with the schema's sha256 checked against
  `wiki/src/vendor/literature-links/provenance.json` first;
- recomputes `passage_key`, `pair_key` and `bundle_key` and refuses a record whose keys
  disagree;
- **re-derives every passage's status** under status rules 1, 2 and 3 and refuses the set
  when a stamped status differs, in either direction;
- is pinned to the producer's own answers by six fixtures in `scripts/articles/fixtures/`:
  `producer_derive_status{,-2,-3}.json` (1679 + 3197 + 3197 cases) and
  `status_parity{,-2,-3}.json` (118 + 58 + 81 hand-written shared cases).

So the *judgement* in a record set is already checked independently of its producer. What
WikiLean lacked was everything that **makes** one: the sources, the cut, the locate and
challenge runs, the gate, the tally, the carry-over, the export that seals a set, and the
transport that brings work back from a machine ssh cannot reach.

## 2. Where the new code lives

**`scripts/pipeline/`**, beside `scripts/articles/`.

Not a new top-level `pipeline/`. `site/ops/brain_release_authority.py` makes every path
release-affecting by default and lists the neutral directories explicitly; `scripts` is one
of them and a new top-level directory is not. A release-affecting pipeline would block
`main` from moving past a frozen release's authority commit for a change that never reaches
a Worker byte. `scripts/` is where the articles plane already keeps its release-neutral
tools, and ARTICLES.md section 5 says so in as many words ("`scripts/` is release-neutral,
so the builder can change without a release"). The producer belongs next to the consumer.

```
scripts/pipeline/README.md          what the pipeline is, and the stage order
scripts/pipeline/links.py           stage 1: Literature Links, pinned and validated
scripts/pipeline/keys.py            stage 2: the revision-free keys, canonical JSON
scripts/pipeline/vocab.py           the locate vocabulary (relations, verdicts, rules)
scripts/pipeline/status.py          stage 3: the derived status, rules 1, 2 and 3
scripts/pipeline/readback.py        stage 4: a sealed set read back into outcome rows
scripts/pipeline/records.py         stage 5: the Literature Links records of one passage
scripts/pipeline/export_records.py  stage 6: the sealed set, written and verified
scripts/pipeline/gate/              stage 7: the out-of-tree gate (TODO)
scripts/pipeline/tally.py           stage 8: outcomes and the challenge (TODO)
scripts/pipeline/sources.py         stage 9a: sourcesets and the text layer (TODO)
scripts/pipeline/cut.py             stage 9b: the statement-level cut (TODO)
scripts/pipeline/locate.py          stage 9c: the locate run (TODO)
scripts/pipeline/challenge.py       stage 9d: the challenge run (TODO)
scripts/pipeline/run/               stage 10: run, harvest, watchdog (TODO)
scripts/pipeline/contribute/        stage 11: the work-repo transport
scripts/pipeline/test_*.py          the tests, each named by scripts/ci-python.sh
scripts/pipeline/fixtures/          synthetic fixtures only
```

**CLI idiom.** One module per job, run by path from the repository root with spelled-out
flags, as `scripts/articles/build_store.py`, `brain/fold_proposals.py` and `manage/status.py`
are. No `cf`-style subcommand tree, no PATH shim, no workspace object. Python 3.12 (the gate
`scripts/ci-python.sh` runs), standard library plus what `requirements-ci.txt` already pins.

**Fixtures are synthetic.** WikiLean is public: no article text, no host names, no paths from
an operator machine, no tokens (AGENTS.md; ARTICLES.md section 5's fixture rule). Batch 1's
record sets are *not* committed; they are read from a path the operator passes.

## 3. The pipeline, stage by stage

The data flow, as WikiLean now owns it:

```
  sources ──► the cut ──► locate ──► the gate ──► challenge ──► the tally ──► the export
   (9a)        (9b)        (9c)        (7)          (9d)          (8)           (5,6)
     │                        │                                     │              │
     └── the universe ────────┴─────────────────────────────────────┘              ▼
         (the declaration index)                                        sealed record set
                                                                                   │
   carry-over (8) re-uses a prior run's verdicts across a re-cut                    ▼
                                                                    scripts/articles/build_store.py
   contribute (11) is a transport for 9c/9d/7 on a machine ssh cannot reach   ──►  the store
```

Each stage below gives: what it is, its source in formal-companion, what changes in the
move, and what proves it. Section 8 collects the proofs; section 4 gives the order.

### Stage 1 — Literature Links, pinned (`scripts/pipeline/links.py`)

**What it is.** The one piece both repositories vendor: the record format, its JSON schema
and its checker. Every record this pipeline writes is validated against it before it is
sealed.

**Source in formal-companion.** `vendor/literature_links/` (the Python package at tag
`v1.0.1`, commit `789af9af`), used through `records.check` / `check_all`.

**What changes.** Nothing is copied from formal-companion. WikiLean already vendors the same
release at the same commit — `wiki/src/vendor/literature-links/`, whose `provenance.json`
names tag `v1.0.1`, commit `789af9af85ae7590d7149cb8312f09cfac0752b4`, and the schema's
sha256 `9387537f…` — and already has a Python checker over it,
`site/native_annotations.validate_link_record`, which refuses to run until the schema's bytes
match that provenance. `links.py` is a thin, explicit front door to that pair:
`validate(record) -> [problem, ...]`, `validate_all(records)`, and `pin()` returning the
vendored version and schema sha so the export and a receipt can record which checker passed a
set. Section 6 is the pinning rule in full.

**Cost.** None; deterministic, offline.

### Stage 2 — the keys (`scripts/pipeline/keys.py`)

**What it is.** The revision- and commit-free identities a `/2` record carries, and the one
JSON spelling of a record:

- `passage_key = sha256("<pageid>\n<span_sha256>")`
- `pair_key = sha256("<passage_key>\n<lean end uri>\n<declaration>")`
- `bundle_key = sha256` of the passage key and the parts' pair keys, sorted, one per line
- `canonical(obj)` — sorted keys, no spaces, UTF-8, the spelling every sealed file uses
- `sha256_text`, `sha256_file`

**Source in formal-companion.** `scripts/cflib/hashing.py` (`passage_key`, `pair_key`,
`bundle_key`), restated in lockstep in `scripts/locate/records.py` so the gate can carry it to
a run host.

**What changes.** WikiLean already computes all three inside `build_store.py` (to check them);
`keys.py` is the producer's copy, in its own module, and a test pins the two implementations
to each other on every record of batch 1 and on the refusal cases. Spelling, argument order
and refusals are unchanged, because the hash must be the same byte string: a change here
re-keys every vote in `atlas_feedback` and every row a promotion would write.

**Cost.** None; deterministic, offline.

### Stage 3 — the status rules (`scripts/pipeline/status.py`)

**What it is.** The derived status of a passage, from its kind, its locations' relations,
proof statuses and `statement` flags, its absence and its coverage claim — under status rule
1 (D1), 2 (the bundle) or 3 (R-BROAD). The producer stamps it on every record of the passage;
the consumer re-derives it and refuses a disagreement.

**Source in formal-companion.** `scripts/cflib/vocab.py` `derive_status`, with
`decide_proof_status` and the vocabulary beside it.

**What changes.** The function is already in WikiLean twice over: `build_store.derive_status`
implements all three rules, and the six fixtures in `scripts/articles/fixtures/` are the
producer's own answers (3197 cases per rule at commit `d85385aa`, which is batch 1's
`skill_commit`) and the hand-written shared tables. `status.py` is the **producer-side**
entry: `derive(kind, locations, absent, coverage, status_rule)` taking the producer's
argument shape, so the tally can stamp what the consumer will re-derive.

It is a second implementation in one repository, deliberately. The alternative — making
`build_store.py` import `scripts/pipeline/status`— would make the store builder, which
`verify_submission.py` delegates to and which a contributor runs by hand against a checkout,
depend on a package; that is a change to a working, operator-facing tool for no gain today.
The two are held together by a parity test that runs both over all three producer tables, all
three shared tables, and every passage of batch 1. **Question for Jack (Q1, section 11):**
whether to consolidate them later, with `build_store.py` importing the pipeline's.

**Cost.** None; deterministic, offline.

### Stage 4 — the record set, read back (`scripts/pipeline/readback.py`)

**What it is.** A sealed record set read back into the rows the tally produced: one row per
passage (`source_id`, `item`, `passage_kind`, `span`, `section`, `prominence`, `challenge`,
`status`, `outcome`, `locations[]`, `absence`, `coverage`) plus the sources, the universe and
the seal. It checks the seal while it reads, and refuses a set whose records of one passage
disagree.

**Source in formal-companion.** None — this is the inverse of
`scripts/locate/records.passage_records`, and formal-companion has no need of it: its rows
come from its own workspace.

**What changes.** It is new code, and it is not only a test harness. It is what a promotion
into `brain/proposals/*.jsonl` will read (INTAKE.md section 6 step 1), what lets an operator
diff two sets of one page, and what makes the re-export of stage 6 possible without the
producer's workspace. It is also the honest boundary of the batch-1 proof: a layer-derived
value (the capped quote and `span_sha256`) cannot be recomputed from a record, so readback
carries it through as the **cut** the record already states (stage 5).

**Cost.** None; deterministic, offline.

### Stage 5 — the records (`scripts/pipeline/records.py`)

**What it is.** The three record shapes of one passage outcome — a location record, a checked
absence with its receipt, an unresolved record — their subject end, their locate extension,
the coverage bundle, the absence's minimal lean ends, and the capped quote.

**Source in formal-companion.** `scripts/locate/records.py` (908 lines), which imports no
`cflib` and is already standard-library-only.

**What changes.**

| formal-companion | WikiLean |
|---|---|
| `locate/records.py`, imported by the export and copied into the gate directory | `scripts/pipeline/records.py`, imported by the export and by the gate |
| `passage_records(source, passage, layer, universe, status_rule=)` | `passage_records(source, row, cut, universe, status_rule=)` — the **cut** is explicit |
| `cap_quote(layer, start, end)` cut inside the record builder | `cut_passage(layer, span) -> Cut(quote, span_sha256)`, a stage of its own |
| `locate_text.find_quote` on `sys.path` by file copy | `scripts/pipeline/links.py` has no part in it; the quote finder is `records.find_quote`, inlined |
| `RecordError` | `RecordProblem` (WikiLean names a refusal for what it is, as `build_store` does) |
| the vendored `literature_links.check` | `links.validate` (stage 1) |

Splitting the cut out is the one real interface change, and it is what makes stage 5
provable: the layer is the article's text, which never enters this repository and is not in
a sealed set, so a record can only be rebuilt from a record if the layer's two outputs travel
with the row. `cut_passage` is still the only thing that may produce a `Cut` from a layer, and
it is held to the same caps (12 words of `exact`, 32 characters of prefix and of suffix,
widened only until the quote is unique).

**Cost.** None; deterministic, offline.

### Stage 6 — the sealed set (`scripts/pipeline/export_records.py`)

**What it is.** The writer: outcome rows, sources, a universe and a seal in, a directory of
`SEAL.json`, `universe.json`, `sources.jsonl`, `records/<source-id>.jsonl` and
`receipts/<sha256>.json` out, byte for byte reproducible. And the reader's half,
`verify_set(dir)`, which is what a consumer checks.

**Source in formal-companion.** `scripts/locate/export.py` (`build_set`, `_universe_json`,
`check_target`, `_write_set`, `verify_seal`, `campaign_token`, `check_token`,
`licence_problem`).

**What changes.**

| formal-companion | WikiLean |
|---|---|
| `cf export literature-links --run <r> --set-id <s>` | `python3 scripts/pipeline/export_records.py --rows <file> --sources <file> --universe <file> --seal <file> --out <dir>` |
| inputs come from a workspace (`WorkspacePaths`, `outcomes.build`, `Sourceset.pinned`, `Registry`) | inputs are **files**, named on the command line: the rows the tally wrote, the sources, the universe, and the seal's pins |
| the campaign token is derived from `run.yaml` | the campaign token is a flag, `--campaign`, held to the same grammar |
| `skill_commit()` reads this checkout's git HEAD | `--producer-commit <40 hex>`: the seal names the commit that produced the set, and WikiLean's producer is a different repository from formal-companion's, so the value is given, never guessed |
| `created` is the newest harvest time of the runs | `--created <ISO-Z>`, for the same reason, with the same rule written down (the newest harvest, so a re-export is byte-identical) |
| refuses a target inside the skill repo or a companion | refuses a target inside this checkout, a symlink, or a non-empty directory that is not an earlier set |
| the licence check reads `registry/sources.yaml` | the licence check reads the source row's own `licence_class` against `scripts/pipeline/sources.py`'s table (stage 9a); until that stage lands, `--licence-class` states it and the export refuses a class that may not be quoted |
| writes `reports/export.<set-id>.json` into the workspace | writes the same report to `--report <file>`, or stdout |

The seal's `schema` stays `cf-locate-recordset/1` or `/2` and its keys stay exactly the agreed
ones. **The format is the contract, not the producer's name**: a seal that said
`wikilean-locate-recordset/1` would refuse to build in `build_store.py`, would not be the set
INTAKE.md section 1 specifies, and would make batch 1's sets unreproducible. Nothing in the
seal names formal-companion; `skill_commit` is a 40-hex commit of whatever produced the set,
and the field keeps its name because renaming it is a format change on both sides at once
(ARTICLES.md contract note 10). **Question for Jack (Q2):** whether to rename it
`producer_commit` in a future `/3`.

**Cost.** None; deterministic, offline.

### Stage 7 — the gate (`scripts/pipeline/gate/`) — TODO

**What it is.** The out-of-tree check of a worker's documents: rules 1–10 over one task's own
documents, run by absolute path from a directory the worker cannot edit, with the exit
vocabulary 0 / 1 (documents rejected) / 3 (the gate could not run).

**Source in formal-companion.** `scripts/gate/verify_locate.py`, `verify_findings.py`,
`locate_text.py`, `scripts/locate/passage_ids.py`.

**What changes.** The rules are the gate's own and move whole; what changes is where they read
their pins from (a WikiLean `gate.json` written by stage 9c, not `cf plan-locate`'s) and that
`locate_text.py`'s text layer is **WikiLean's own already**: formal-companion's
`render_html.py` says it is a port of `site/anchor_resolve.text_layer`, pinned by a parity
test. So the move returns the layer rule to where it came from, and
`scripts/pipeline/gate/locate_text.py` is a stdlib-only restatement of
`site/anchor_resolve.text_layer` with the head rule and the quote finder beside it, pinned to
`site/test_anchor_resolve.py`'s own answers.

**Why it is TODO.** The rules are checked against worker documents, and no batch-1 worker
document is on this host — only the sealed exports are. The gate can be built and proved on
synthetic documents, and that is worth doing, but it cannot be proved against batch 1, so it
sits after the stages that can. Its deterministic record-shaped half is already in the tree
twice (`build_store.py`'s checks and stage 5's `*_problem` functions).

**Cost.** None at run time; the gate is model-free by rule.

### Stage 8 — the tally and carry-over (`scripts/pipeline/tally.py`, `carry.py`) — TODO

**What it is.** Harvested documents → finding rows → the challenge's verdicts applied → the
derived status, as outcome rows. And the carry-over map: what a re-cut of a page does to each
passage of the old cut, so a verdict, a gold label or a known answer carries where the
passage's text is unchanged.

**Source in formal-companion.** `scripts/locate/outcomes.py` (1630 lines), `carry.py` (484),
`scripts/harvest/tally.py`, `scripts/harvest/harvest.py`.

**What changes.** The status half is stage 3, done. What remains is the row machinery: the
document reader, the challenge semantics (confirm/uphold/downgrade/refute/overturn, the
reopen-wins ordering of review four, `confirm_parts`), the proof-status join from the
declaration index, and the counts. Carry-over is computed from spans and span text only —
never from an item, a statement or a model — so it is deterministic and portable as it
stands; it uses stage 2's `passage_key`, which WikiLean already owns.

**Why it is TODO.** Both read a workspace shape (harvested documents, `corpus.sqlite`,
`planned.json`, `challenge-plan.json`) that does not exist on this host for batch 1, and both
are large. Stage 4 (`readback.py`) gives the half the export needs today; the rest waits on
stage 9c's run shape, because the row is whatever the documents of a WikiLean locate run say.

**Cost.** None; deterministic, offline. The documents it reads cost model time (stage 9c).

### Stage 9 — the model stages — TODO

These are the four that spend money. Each is specified here and built later; none may be run
from this host (no paid model runs).

**9a — sources (`scripts/pipeline/sources.py`).** A sourceset: `manifest-<n>.jsonl`,
append-only and versioned, one directory per source, pinned by `(sourceset id, manifest
sha)`; the `web-html/3` renderer that computes the Literature Links text layer, the reading
copy, the offset map and the sections; the one Wikipedia client, behind an explicit
`--execute`, with a fixed parameter set and each revision fetched once.
*Source:* `scripts/sources/{sourceset,render_html,websource,wikinet}.py`.
*What changes:* the layer is already WikiLean's (`site/anchor_resolve.text_layer`) and the
renderer must be written as a use of it, not a second copy; the client is the one place in
this pipeline that touches the network and takes the same `--execute` shape
`brain/acquire-*.sh` takes. Batch 1's sourceset is `wpm3`, manifest sha `04abae03…`.
*Cost:* network only, no model.

**9b — the cut (`scripts/pipeline/cut.py`).** The statement-level inventory: which spans of a
revision state something, each one's kind, prominence and scope, keyed by the frozen
`In-text #<hex>` head rule. One model call per article.
*Source:* `docs/proposals/statement-cut.md`, `scripts/locate/plan.py` (the
`locate-inventory` preset), `passage_ids.py`, `templates/prompts/locate-inventory*`.
*What changes:* the prompts become WikiLean's own under `scripts/pipeline/prompts/`, and the
agent call goes through `site/agent_sandbox.py` — WikiLean's hard invariant for every SDK
agent (Read/Grep/Glob at most, never Bash, never `bypassPermissions`, cwd outside every
checkout), enforced by `site/test_agent_sandbox.py`. formal-companion's agentbox is a run
host's tool and does not move.
*Cost:* one worker call and one reviewer call per article. Batch 1's 48 articles were cut by
an inventory run; **a re-run would be a paid run and is not part of this work.** Tested on
synthetic fixtures only.

**9c — locate (`scripts/pipeline/locate.py`).** The run: candidate retrieval over the
declaration index, tasks of at most N passages of a frozen inventory, the task manifests, the
prompts, `gate.json`, the deterministic plan.
*Source:* `scripts/locate/{plan,candidates,prompts,presets,inputs,tend_root}.py`.
*Cost:* the batch itself. Batch 1's two locate runs are `loc-tierA-b1a` and `loc-tierA-b1b`.
Not re-run.

**9d — challenge (`scripts/pipeline/challenge.py`).** Which records a challenge judges —
every location and absence on a subject or central passage, every coverage claim whatever
the prominence, every subject prominence, and a seeded sample of the supporting passages —
and the verdict vocabulary.
*Source:* `scripts/locate/challenge_plan.py`.
*Cost:* the challenge run. Batch 1's is `chal-tierA-b1`. Not re-run.

**The declaration index** that 9c retrieves over (formal-companion's `scripts/corpus/`, the
Lean probe and `corpus.sqlite`) is the one piece whose home is genuinely arguable: it is a
map of Lean libraries, which is what the Brain is. **Question for Jack (Q3):** whether
WikiLean's copy should be a new `scripts/pipeline/universe.py` + SQLite, or should be served
out of the Brain's own declaration data.

### Stage 10 — the run, the harvest and the watchdog — TODO

**What it is.** Driving the agents on a host, bringing the results home with a receipt that
proves the corpus is whole, and the model-free spend watchdog that drains a shard before it
overspends.

**Source in formal-companion.** `scripts/run/` (the bridges, `agent_call.py`,
`attempt_cap.py`, `watchdog.py`, `campaign.py`), `scripts/harvest/`, `templates/tend/`,
`templates/campaign/`.

**What changes, and what does not.** The *substrate* — tend, agentbox, the run host — is not
formal-companion's either; it is a separate tool a run host has. WikiLean's pipeline drives
it rather than vendoring it, exactly as the contribute transport (stage 11) does. What moves
is the harvest's reconciliation rule (list the source, transfer, list the destination, diff,
write `RECEIPT.json`, refuse on any mismatch) and the watchdog's caps, both model-free. What
does not move is agentbox, which is a Linux host's sandbox and the thing contribute mode
exists to do without.

**Cost.** None of its own; it is what spends stage 9's budget, and the watchdog is the cap.

### Stage 11 — contribute mode (`scripts/pipeline/contribute/`)

**Where it fits.** Inside WikiLean's pipeline, as a **transport for stages 9c, 9d and 7** — the
three that run agents — on a machine `ssh` cannot reach. It replaces the run host's rsync +
ssh leg and agentbox, and nothing else: the plan is still the coordinator's (9c), the gate is
still the gate (7), the tally, the export and the store are unchanged (8, 6,
`build_store.py`). The maintainer ruled it WikiLean's, and this is why it is coherent there:
the only reason to run locate work on a stranger's Mac is to fill WikiLean's articles plane,
and the thing that comes back is a WikiLean record set.

**Source in formal-companion.** The unmerged branch `locate/contribute` (`c8859fa`):
`docs/contribute.md`, `scripts/contribute/{workrepo,package,materialise,results,sandbox}.py`,
`scripts/cfcmd/contribute.py`, `scripts/run/bridge-claude-p.sh`.

**What changes.**

| formal-companion | WikiLean |
|---|---|
| `cf contribute publish\|run\|collect` | `python3 scripts/pipeline/contribute/publish.py`, `run.py`, `collect.py` |
| `scripts/contribute/workrepo.py` | `scripts/pipeline/contribute/workrepo.py` — the git transport, unchanged in substance |
| `scripts/contribute/sandbox.py` | `scripts/pipeline/contribute/sandbox.py` — the per-call Claude Code settings, with the macOS real-path fix |
| `scripts/run/bridge-claude-p.sh` | `scripts/pipeline/contribute/bridge.sh` — the per-call settings write, `pwd -P`, exit 78 |
| a result is the gated documents and the run's records | **and**, new: the contributor may seal and verify its own set |
| the results leg's "designed replacement" is WikiLean's endpoint | the verifier is already here: `scripts/articles/verify_submission.py` is wired in as the contributor's last step and the coordinator's first |

**The wiring to `verify_submission.py`** is the piece that makes this WikiLean's rather than a
copy. INTAKE.md section 7 step 4 already says a contributor should run the verifier against a
checkout before posting; in formal-companion's version that is prose, because the producer
cannot run WikiLean's code. Here it is a step of the command:

- `run.py --stage results` refuses to push a result whose documents the pipeline's own checks
  reject, and, when the contributor sealed a set, runs `verify_submission.verify()` over it
  and puts the verdict in `RESULT.json`;
- `collect.py` runs `verify_submission.verify()` again on the coordinator's side, because a
  result is untrusted input — the refusal codes and HTTP statuses are INTAKE.md section 3's,
  so the work-repo leg and the future endpoint refuse the same sets for the same reasons.

**Three invariants that must not be lost in the move**, each with its reason:

1. **A claim no two machines can share.** `claim/<run-id>` holds a single commit whose one
   file names the machine and a random nonce. Git answers a push of a commit the remote
   already holds with "Everything up-to-date" and exit 0, so a claim branch that is merely the
   clone's HEAD double-books every package two contributors race for. The push carries
   `--force-with-lease=refs/heads/claim/<run-id>:` — an *empty* lease, "only if the remote has
   no such ref", which overwrites nothing — and the contributor then fetches the ref back and
   starts only if it is its own commit. The lease is not the mechanism; the unique commit plus
   the read-back is.
2. **The sandbox is written per agent call.** The staged settings file is a template naming
   the task worktree as `{{CF_TASK_WORKTREE}}`; the bridge replaces it with the directory tend
   ran the call in and writes `<that worktree>/.tend/claude-settings.json`. A file written at
   stage time could only name the whole entrypoint repository every worktree sits under, and a
   worker for one task could then write into another's. The bridge exits 78 rather than run an
   agent unsandboxed.
3. **The macOS path fix.** The sandbox matches the *real* path a write lands on, and on macOS
   `/var` and `/tmp` are symlinks into `/private`. So the bridge names the worktree by
   `pwd -P` and `settings_json()` passes every other root through `os.path.realpath`.

**No article text travels, either way.** A package carries no layer, no canonical body, no
raw response and no candidates file (the contributor refetches and regenerates, and every sha
is checked); a result carries the gated documents and the run's own receipts, and refuses by
name any file that is article text.

**Cost.** The contributor's own Claude subscription. Nothing is spent from this host, and the
spend cap is recorded, not enforced: no watchdog runs on a contributor machine.

## 4. The order, and what is built

Stages are implemented in the order that each can be **proved**, not in data-flow order.
That is why the export comes before the run that fills it: the export can be proved against
batch 1 today, and the run cannot be proved at all without paying for one.

| # | Stage | Module | Built | Proof on batch 1 |
|---|---|---|---|---|
| 1 | Literature Links, pinned | `links.py` | yes | all 7,583 records validate (R1) |
| 2 | the keys | `keys.py` | yes | all keys recomputed (R2) |
| 3 | the status rules | `status.py` | yes | all 4,285 passages + 3 producer tables + 3 shared tables (R3) |
| 4 | the record set, read back | `readback.py` | yes | both sets read, seals checked (R4) |
| 5 | the records | `records.py` | yes | all 7,583 records rebuilt byte for byte (R5) |
| 6 | the sealed set | `export_records.py` | yes | both sets re-exported byte for byte, both SEAL shas (R6) |
| 7 | the gate | `gate/` | TODO | not provable here (no worker documents) |
| 8 | the tally, carry-over | `tally.py`, `carry.py` | TODO | not provable here (no harvest) |
| 9 | sources, cut, locate, challenge | `sources.py`, … | TODO | fixtures only; a run costs money |
| 10 | the run, harvest, watchdog | `run/` | TODO | not provable here |
| 11 | contribute mode | `contribute/` | — | synthetic end-to-end (R7); a Mac is section 9 |

The **Built** column is `—` until the stage lands and `yes` after; `TODO` marks a stage this
round does not reach at all.

## 5. What stays in formal-companion

Companion work, and nothing of it moves:

- **the faithfulness stages** — onboarding, discovery and inference over a companion
  repository, the adapters, `pair`, `detect`, `route`, `recheck`, `score`, `share`, the
  ledger, the reports: the forward question, "is this companion faithful to its source?";
- **the PDF overlay** (`scripts/overlay/`, `templates/overlay/`) and the `pdf-to-companion`
  proposal;
- **the legacy annotation format** (`vendor/formal_companion_annotations/`) and the exporters
  around it;
- **the companion registry** (`registry/sources.yaml`) — facts about editions of books;
- **agentbox**, the run host's Linux sandbox, and the run-host runbook;
- **its own copy of the locate pipeline.** The ruling does not ask formal-companion to delete
  anything. Two producers of one format is the normal state of an agreed format, and it is
  also the safety margin: batch 1 was produced by one of them and is reproduced by the other
  (section 8).

What formal-companion stops being is **WikiLean's dependency**. Nothing under
`scripts/pipeline/` or `scripts/articles/` imports it, invokes it, or needs it installed; the
only thing that must agree is the format, and the format is written down in ARTICLES.md
section 3 and checked by two implementations.

## 6. The one vendored piece, and how WikiLean pins it

**Literature Links 1.0.1** — and nothing else.

WikiLean's pin is `wiki/src/vendor/literature-links/provenance.json`:

```
repository  https://github.com/mathlib-initiative/literature-links
tag         v1.0.1
commit      789af9af85ae7590d7149cb8312f09cfac0752b4
version     1.0.1
schema      schema/companion.schema.json
sha256      9387537f21e69dc368357fcc8939fffa3c6846d33ab2b3a3b7c2a20a4350a797
```

The rules:

1. **The pin is the provenance file, and the schema's bytes are checked against it before any
   record is validated.** `site/native_annotations.py` reads the schema from the vendored
   path; `build_store.schema_validator()` compares its sha256 with `provenance.json` and
   refuses the build on a mismatch. `scripts/pipeline/links.py` goes through the same pair, so
   there is one checker and one pin in this repository, not two.
2. **A sealed set records which checker passed it.** The export's report carries
   `links_version` and `links_schema_sha256` from `links.pin()`, so a set can be told from a
   set validated against another release. The seal itself does not carry it: the seal's keys
   are exactly the agreed ones, and adding one is a format change on both sides.
3. **WikiLean does not take formal-companion's copy.** The two happen to be the same release
   at the same commit today, which is why batch 1 reproduces. Formal-companion's unmerged
   `locate/contribute` branch has already moved its vendor to an unreleased `format-2.0`
   branch (`2bb328c`, a different schema sha); WikiLean stays on `v1.0.1` until the version
   it pins is a release, and a set validated against anything else is a different format.
4. **Upgrading the pin is its own change**: a new `provenance.json`, a new
   `companion.schema.json` and `validate.js`, and both the Worker tests and
   `scripts/ci-python.sh` green on the same commit — never a side effect of a pipeline change.
   `scripts/pipeline/test_links.py` asserts the pin's values, so an upgrade fails until it is
   deliberate.
5. **Nothing else is vendored in either direction.** Not `cflib`, not the gate, not the
   prompts, not `corpus.sqlite`'s schema.

## 7. Batch 1 stays valid

Batch 1 is two sealed sets, `tierA-b1a` (SEAL `f1e67e43…`, 5 sources, 768 records) and
`tierA-b1b` (SEAL `23710337…`, 43 sources, 6,815 records), both
`cf-locate-recordset/2`, status rule 3, sourceset `wpm3` at manifest `04abae03…`, universe
`u1` at `12e9eacf…`, skill commit `d85385aa…`, created `2026-11-01T14:25:17Z`.

Why the move changes no published result:

1. **The sets are bytes, and the bytes do not move.** Nothing in this plan rewrites, re-seals
   or re-exports a published set. The re-export of section 8 writes into a scratch directory
   and is compared, never installed.
2. **A seal is a statement about bytes, not about a producer.** `SEAL.json` names the schema,
   the set, the campaign, the universe, the sourceset, the producing commit, the time and
   every file's sha256. None of those changes meaning because a second implementation can also
   write such a file. `skill_commit` on batch 1 is and stays `d85385aa…`, the commit that
   really produced it.
3. **The receipts are content-addressed.** `receipts/<sha256>.json` is named by the sha256 of
   its own bytes, and an absence references it by that sha and the lengths of its two lists.
   1,959 of them are in batch 1; a re-export of the same rows reproduces every name.
4. **The live store is already built from those bytes by WikiLean's own code.** The store's
   `record_set` is the seal's sha256, each article's `records_sha256` is its records file's,
   and the uploader checks every object against `manifest.json`. None of that reads the
   producer. A store built today from batch 1 is byte for byte the store built before this
   work (section 8, test R0).
5. **The feedback rows keep their keys.** `atlas_feedback` rows carry `location_key`,
   `pair_key`, `record_set` and `campaign`. `location_key` is the producer's opaque key, which
   the pipeline copies and never recomputes (ARTICLES.md contract note 4); `pair_key` is
   recomputed, and stage 2 reproduces it on every one of batch 1's 5,571 location records. A
   verdict stays attached to the judgment it was cast on.
6. **The store's contract did not change.** No new schema string, no new store field, no
   change to `build_store.py`. The plane is not touched; `wiki/` is not touched.

## 8. The reproduction tests

All run offline, from the sealed sets alone, under the Python 3.12 of `scripts/ci-python.sh`.
Batch 1's exports are **not** committed (public repository, article text): each test that
needs them is skipped unless `WIKILEAN_LOCATE_SETS` names a directory holding them, and the
committed tests run on synthetic fixtures. The batch-1 run is a command an operator runs, and
its results are recorded in the report.

| id | stage | what it proves |
|---|---|---|
| R0 | — | `build_store.py` builds batch 1 to the same 114 files before and after this work |
| R1 | 1 | every record of batch 1 validates against the vendored checker, and `links.pin()` is the provenance file |
| R2 | 2 | `passage_key` recomputed on all 7,583 records, `pair_key` on all location records, `bundle_key` on all bundles, each equal to the record's; and equal to `build_store`'s |
| R3 | 3 | `status.derive` equals the stamped status on all 4,285 passages; equals `build_store.derive_status` and the producer's three tables, case for case |
| R4 | 4 | both sets read back; the seal checks; no passage's records disagree; the row count is the passage count |
| R5 | 5 | every one of the 7,583 records rebuilt from its read-back row is byte-identical to the sealed line, under `keys.canonical` |
| R6 | 6 | a re-export of both sets into a scratch directory is byte-identical to the sealed set, file for file, including `SEAL.json` and its sha256 (`f1e67e43…`, `23710337…`) |
| R7 | 11 | a synthetic end-to-end contribute run: publish → claim → a second machine refused → run → results → collect → `verify_submission` accepts |

**What R5 and R6 do and do not prove.** The record and the set are rebuilt from the rows that
`readback.py` extracts from the sealed records. Three kinds of value are in play:

- **derived** — recomputed from scratch and compared: every key, the absence's lean-end set,
  the canonical JSON spelling and key order, the record's shape and the locate extension's
  keys at each of its three places, the receipt's sidecar bytes and its name, the seal's file
  list and shas, `universe.json`'s sort order and display-name rule, the per-source record
  order (by span start, then item) and the per-passage location order (by rank, library,
  declaration). A mistake in any of these is a byte difference.
- **carried** — taken from the record and written back: the capped quote, `span_sha256`, the
  note, the confidence, `location_key`, the receipt's queries and candidates. These come from
  the article's text layer or from a model, neither of which is in a sealed set or in this
  repository, so they can be round-tripped but not re-derived. `cut_passage` (the one function
  that makes them from a layer) is tested on synthetic layers instead, against the caps and
  the uniqueness rule.
- **stated** — the seal's pins, given on the command line: `--campaign`, `--created`,
  `--producer-commit`, `--set-id`, the sourceset's id and manifest sha. These are an input to
  any export; the test gives batch 1's.

So R6 is a strong test of the writer and a weak test of the workspace it would normally read.
That is the honest boundary, and it is exactly where stages 8, 9 and 10 sit.

**Fixture tests, for the stages batch 1 cannot reach.** Every model stage is tested on
synthetic fixtures under `scripts/pipeline/fixtures/`: a two-article synthetic record set
(the one `scripts/articles/fixtures/recordset/` already holds, which this pipeline can now
*write* as well as read), a synthetic layer for the cut, and recorded agent answers for the
prompt renderers. No test calls a model; `scripts/ci-python.sh` unsets every credential
variable before it runs.

## 9. What only a real Mac can prove (stage 11)

Everything in `scripts/pipeline/contribute/` is tested on Linux, including the macOS real-path
substitution (`os.path.realpath`, `pwd -P`) on a symlinked temporary directory that stands in
for `/var` → `/private/var`. Seven things remain, and none of them is a thing a Linux box can
answer:

1. **The settings keys are this build's.** `sandbox.enabled`,
   `sandbox.filesystem.disabled`, `allowWrite` / `denyWrite` and
   `sandbox.autoAllowBashIfSandboxed` were read out of a Claude Code binary. Run
   `claude /sandbox` on the Mac with the per-call file a staged run writes and confirm it
   reports a confining filesystem sandbox. A renamed key leaves the agents unsandboxed with
   only the prompt stopping them.
2. **Whether `--settings` contributes `allowWrite` at all.** The binary's own telemetry note
   says project settings contribute none. One `claude --settings <that file> -p '<write a
   file outside the worktree>'` settles it. If it is ignored, the confinement rests on the
   prompt and the allowlist.
3. **macOS's sandbox backend.** Linux uses `bwrap`; macOS has its own. Check that a worker can
   still write its document and run `git` inside its worktree (the entrypoint's `.git` is in
   `allowWrite` for exactly that), and whether a call needs to write Claude Code's state under
   `$HOME/.claude`, which no root admits.
4. **tend on macOS.** `tend --version`, a one-task smoke run, and whether `TEND_TASK_ID`
   reaches the validation command; without it the gate's ownership check falls back to the
   weaker rule.
5. **bash 3.2.** macOS ships it. `bash -n` over `bridge.sh` and the staged wrappers; the
   bridge avoids heredocs inside `$(...)` for this reason.
6. **A refetch may not reproduce the layer.** An old revision's parse output expands templates
   as they are *now*, so a page can render to another layer than the coordinator's. The
   command refuses such a source by name and the coordinator re-plans; a package carries no
   bytes by rule, so there is no way to avoid the refetch. A synthetic run replays recorded
   responses and cannot rule this out.
7. **The agents' tool allowlist.** The worker's allowlist names host tools by absolute path;
   the paths are staged and right by construction, but whether Claude Code's allowlist
   matching behaves the same on macOS is unverified.

## 10. What is not done

Marked TODO in the tree, each with the section that specifies it:

- stage 7, the gate (`scripts/pipeline/gate/`);
- stage 8, the tally and carry-over;
- stage 9, sources, the cut, locate and challenge — the four model stages, and the
  declaration index they retrieve over;
- stage 10, the run, the harvest and the watchdog;
- the contributor's own seal: stage 11 wires `verify_submission.py` in, but a contributor can
  only seal a set once stage 8 produces rows on their machine. Until then a result is the
  gated documents, as formal-companion's is, and the coordinator seals.

Until stages 7–10 land, **a batch is still produced by formal-companion and consumed here.**
That is a true statement about today and it is why nothing published moves. The pipeline is
independent in the direction that matters first: WikiLean can read, check, re-derive, re-key
and re-seal a set without formal-companion on the machine.

## 11. Questions for Jack

1. **One `derive_status` or two?** `build_store.py` and `scripts/pipeline/status.py` both
   implement the three status rules, held together by a parity test (section 3). Consolidate —
   `build_store.py` imports the pipeline's — or keep the store builder self-contained for
   contributors and the intake verifier? *Recommendation:* keep two until stage 8 lands, then
   consolidate in one change with the parity test as the gate.
2. **`skill_commit`.** The seal's field name says "skill". WikiLean's producer is not a skill.
   Rename it `producer_commit` in a future `/3`, or leave it — it is a 40-hex commit of
   whatever produced the set, and renaming is a change on both sides at once. *Recommendation:*
   leave it; note it for `/3`.
3. **Where the declaration index lives** (section 3, stage 9). A new SQLite under
   `scripts/pipeline/`, or served out of the Brain, which is already a map of Lean libraries?
   This is the one piece whose home is genuinely arguable, and it decides whether the Brain's
   nightly and the locate run share an input.
4. **Does formal-companion keep its locate pipeline?** The ruling does not say. Two producers
   of one format is a safety margin (section 5) and costs nothing here, but it is two things to
   keep in step. *Recommendation:* keep both; the shared parity tables are the join.
5. **Which stage is next.** Stages 7–10 are each a real piece of work. The order that buys the
   most is 8 (the tally) → 7 (the gate) → 9 → 10, because the tally is what makes the export
   reachable from real documents. Or 11-first, if contributors are the trigger.
6. **Contribute mode's trigger.** ROADMAP's P3 gates the contributions queue on "first real
   compute donor asks". The transport is built; is a contributor expected, and on which Mac,
   so section 9's seven checks can be run?
