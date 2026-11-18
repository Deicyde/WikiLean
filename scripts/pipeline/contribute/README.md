# Contribute mode

Locate work on a machine this project cannot reach over ssh — a contributor's Mac,
behind a NAT, logged in to GitHub and to Claude Code. Design:
[docs/PIPELINE.md](../../../docs/PIPELINE.md) section 3, stage 11; the intake contract
the results are checked against is [docs/INTAKE.md](../../../docs/INTAKE.md).

It is a **transport for pipeline stages 9c, 9d and 7** — the three that run agents — and
nothing else. The plan is still the coordinator's, the gate is still the gate, and the
tally, the export and the store are unchanged.

| | a run host | a contributor machine |
|---|---|---|
| transport | rsync + ssh | a private **work repo** on GitHub |
| sandbox | the host's own (Linux) | Claude Code's own, per agent call |
| results | fetched over ssh | `collect_results.py` |

## The work repo

One private repository, cloned on the coordinator's machine and on every contributor's:

```
packages/<run-id>/                 a planned locate run (the coordinator writes it)
results/<run-id>/<machine-id>/     what a contributor sends back
```

Branches: the clone's own branch holds the packages, `claim/<run-id>` is a contributor's
claim on one, and `result/<run-id>/<machine-id>` is what it sends back. Nothing is force
pushed and nothing is rewritten.

The declaration index does **not** go in git: it is hundreds of megabytes and travels as
a bucket download behind a short-lived signed URL, with a per-library licence
manifest. The operator uploads it; no command here publishes the bundle.

## The commands

**A dry run is the default, and it is how to start.** Without `--execute`, nothing is
pushed and no model is called. Run previews write local staging files; results previews
write and verify local result bundles. `--stage seal` always writes its local sealed
set and sends nothing, with or without `--execute`.

```bash
# the coordinator
python3 scripts/pipeline/contribute/publish_package.py --run-id loc-7 \
    --plan <planned-run dir> --sources <sources.jsonl> --universe <universe.json> \
    --repo <work-repo clone> --max-cost 20

# the contributor, on the Mac. Each of these is a dry run until --execute.
python3 scripts/pipeline/contribute/run_package.py --repo <clone> --workdir <dir> \
    --stage claim --execute

#   ... put the declaration index and the search corpus under <workdir>/root/index/.
#   ... A package that ships its sources' fetched bytes (--sources-dir) is rendered and
#   ... checked by `--stage run`; for one that ships none, refetch the pinned revisions
#   ... into <workdir>/root/sources/ and the candidates into <workdir>/root/candidates/.
#   ... `--stage run` names whatever is missing.

# free: stage every task's working directory and check every path the prompts name
python3 scripts/pipeline/contribute/run_package.py --repo <clone> --workdir <dir> \
    --stage run
# then, when that is clean, the paid run
python3 scripts/pipeline/contribute/run_package.py --repo <clone> --workdir <dir> \
    --stage run --execute [--model <name>] [--no-reviewer]

# seal it here: stage 8 and stage 6 on this machine, then the intake contract
python3 scripts/pipeline/contribute/run_package.py --repo <clone> --workdir <dir> \
    --stage seal --set-id <token> --campaign 'run:loc-7+chl-7' \
    --challenge-run chl-7 --producer-commit "$(git rev-parse HEAD)" \
    --created "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# or, with no challenge run: every record reads "Not re-checked"
python3 scripts/pipeline/contribute/run_package.py --repo <clone> --workdir <dir> \
    --stage seal --set-id <token> --campaign 'run:loc-7' --unchallenged \
    --producer-commit "$(git rev-parse HEAD)" --created "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

python3 scripts/pipeline/contribute/run_package.py --repo <clone> --workdir <dir> \
    --stage results --documents <dir> --host <dir> --record-set <dir> --execute

# the coordinator
python3 scripts/pipeline/contribute/collect_results.py --run-id loc-7 \
    --repo <clone> --out <dir> --fetch
```

## Staging: the contributor builds its own working directories

A planned root carries `work/<task-id>/` — the manifest, the article's rendered views,
the candidates and the declaration search, and the one directory the worker may write
in. **A package deliberately leaves them behind** (`package.NOT_PACKAGED`): they hold
the coordinator's own copy of the article text. So `--stage run` stages them again here,
through `run/workdir.py`, out of this machine's own rendering of the sources.

**What a package can ship instead of a refetch** (Jack's ruling of 2026-11-13): each
pinned source's fetched bytes at `sources/<sid>/source.raw.html` (the raw
`action=parse` response), its candidates at `candidates/<sid>.json`, and the harvested
inventory run at `inputs/inventory/<run>/`. A refetch of an old revision expands
templates as they are now, and two of three in the 2026-11-07 Mac check rendered
another layer (docs/CONTRIBUTE-MAC-CHECKS.md). The publisher checks all three before it
ships them (`publish_package.py --sources-dir --candidates-dir --inventory-corpus`): each
source renders to the plan's pins, its candidates were made from that rendering and the
gate's index, and the inventory is a complete, gated harvest of the plan's inventory run
over the same renderings. Before staging anything, `--stage run` installs the inventory
where the seal reads it (`work.install_inventory`), renders the shipped bytes with the
frozen renderer and holds every view to the pins before it is written
(`work.render_sources`), and makes the gate's own sha comparisons of the layer, the
pinned views and the index, and of the candidates when the plan pins them
(`work.check_pins`). A mismatch stops the run: the coordinator re-plans, and nobody
changes a pin to make a package pass. A package never carries a rendered view, nor
anything a run writes in its root (`index/`, `work/`, `findings/`, `gate-log/`,
`attempts/`, `usage/`).

**The claims API's packages** travel as one envelope (`envelope.py`,
`wikilean.contribute.package-transport/1`): `package.json` and every file it names,
base64, in canonical JSON, stored in the private bucket under the envelope's sha256.
`publish_package.py --envelope` builds one, `scripts/articles/upload_contrib.py package`
uploads it, and `publish_package.py --register` publishes it (`--set-state` moves it).
A contributor's machine unpacks one only after checking every path, key and sha, into
a fresh directory moved into place last.

`--stage run` then drives each task through `run/task.py`, which is the same module the
coordinator's run host uses: the worker call, the mechanical passage ids, **the runner's
own gate call** out of tree, one retry that hands the gate's refusal list back, and the
promotion of a gated document into the corpus. The worker never runs the gate — it has
no shell.

Two checks that cost nothing and are worth running before any paid call:

* `--stage run` with no `--execute` stages everything and then reads the rendered
  prompts, pulling every path out of them and asking whether staging put it there. A
  prompt naming a file that is not there is what stops a paid run halfway;
* `--stage results` with no `--execute` runs the intake contract over a sealed set and
  pushes nothing.

## The three things that must not be lost

1. **A claim no two machines can share.** `claim/<run-id>` holds a single commit whose
   one file names the machine and a random nonce. git answers a push of a commit the
   remote already holds with "Everything up-to-date" and exit 0, so a claim branch that
   is merely the clone's HEAD double-books every package two contributors race for. The
   push carries `--force-with-lease=refs/heads/claim/<run-id>:` — an *empty* lease,
   "only if the remote has no such ref", which overwrites nothing — and the contributor
   then fetches the ref back and starts only if it is its own commit. **The lease is not
   the mechanism; the unique commit plus the read-back is.**
2. **The sandbox is written per agent call.** The staged settings file is a template
   naming the task worktree by `{{WIKILEAN_TASK_WORKTREE}}`; `bridge.sh` replaces it with
   the directory the call was run in and writes `<that worktree>/.tend/claude-settings.json`.
   A file written at stage time could only name the whole entrypoint repository every
   worktree sits under, and a worker for one task could then write into another's. The
   bridge exits **78** rather than run an agent unsandboxed.
3. **The macOS path fix.** The sandbox matches the *real* path a write lands on, and on
   macOS `/var` and `/tmp` are symlinks into `/private`. So `sandbox.py` passes every
   root through `os.path.realpath` and `bridge.sh` names the call's worktree by `pwd -P`.

## What never travels

- **Into a package**: no rendered view (text layer, canonical HTML and the rest), and
  a source's fetched bytes and candidates only at their own paths, checked by the
  publisher; the contributor renders the bytes against the pins again, and
  `package.json`'s shas pin every file on the way. No path of the
  coordinator's machine in the plan either — a package that names one is refused, never
  rewritten. (The shipped inventory's receipt keeps the paths it was harvested at: it
  travels byte for byte, because the tally pins its sha.)
- **Out of a result**: the named full source-layer files are refused by `results.py`.
  Gated findings and sealed records contain passage quotations. Filename refusal is
  not a general content filter for arbitrary files.

## The intake contract

A result may carry a sealed record set. It is checked by
`scripts/articles/verify_submission.py` — docs/INTAKE.md section 3, offline — **on both
sides**: by the contributor before it pushes, so a defect is seen there and not first on
the coordinator's machine, and by the coordinator on collect, because a result is
untrusted input whoever pushed it. The refusal codes and HTTP statuses are the ones the
submission endpoint will answer with, so the work-repo leg and the endpoint that will
replace it refuse the same sets for the same reasons.

## Sealing it here

`--stage seal` is the command docs/PIPELINE.md section 10's last bullet said was
missing. Three steps of WikiLean's own pipeline, on the contributor's machine:

1. **stage 8** (`tally.py`): the harvested documents become outcome rows under the
   challenge's verdicts, then the rows the exporter takes — each passage's quote cut
   out of *this machine's* rendered layer;
2. **stage 6** (`export_records.py`): those rows become `SEAL.json`, `universe.json`,
   `sources.jsonl`, `records/*.jsonl` and `receipts/*.json`;
3. **the intake contract** (`verify_submission.py`), right there, before anything is
   pushed.

Every pin comes from the package, not from this machine: the universe, the sourceset's
manifest sha and the status rule are the coordinator's. The set id, the campaign, the
producer commit and the creation time are given on the command line, because a seal is
a statement about bytes and about who made them.

Two preconditions worth knowing before you try it:

* **a challenge run, or `--unchallenged`.** Nothing leaves this pipeline without a
  challenge value, so a locate run with neither stage-9d verdicts nor `--unchallenged`
  exports no record, and `--stage seal` refuses to write an empty set and says so.
  `--unchallenged` stamps every record `not_required`, which the pages show as not
  re-checked; a coverage claim then upgrades nothing, a subject passage's records still
  carry the article's Wikidata QID, and the campaign names no challenge run (`run:<run>`);
* **the inventory run** the plan was cut from, in the same corpus. It is your own stage
  9b over the same pinned revisions, unless the package carries the coordinator's
  harvest of it, which `--stage run` installs there.

## What is not built yet

For a package that ships no fetched bytes, refetching its sources at their pinned
revisions and rendering them (stage 9a), and regenerating the candidates (9c's
retrieval), are still by hand: `--stage run` names what is missing but does not go and
get it. Installing the declaration index is by hand too. Everything else between
`claim` and `results` — rendering shipped bytes, staging, the agent calls, the gate, the
retry, the harvest and the seal — is built and tested.

## What only a real Mac can prove

Seven things, listed in docs/PIPELINE.md section 9. Everything here is tested on Linux,
including the real-path substitution over a symlinked directory standing in for
`/var` → `/private/var` and `bash -n` over `bridge.sh` for bash 3.2 — but no test here
can tell you that Claude Code's own sandbox honours this settings file on macOS.
