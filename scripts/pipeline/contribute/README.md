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
a release asset of the work repo, uploaded once per universe. No command here does that
— it is the coordinator's own decision.

## The three commands

```bash
# the coordinator
python3 scripts/pipeline/contribute/publish_package.py --run-id loc-7 \
    --plan <planned-run dir> --sources <sources.jsonl> --universe <universe.json> \
    --repo <work-repo clone> --max-cost 20

# the contributor, on the Mac (a dry run until --execute)
python3 scripts/pipeline/contribute/run_package.py --repo <clone> --workdir <dir> \
    --stage claim --execute
#   ... run the work (see "What is not built yet") ...
python3 scripts/pipeline/contribute/run_package.py --repo <clone> --workdir <dir> \
    --stage results --documents <dir> --host <dir> [--record-set <dir>] --execute

# the coordinator
python3 scripts/pipeline/contribute/collect_results.py --run-id loc-7 \
    --repo <clone> --out <dir> --fetch
```

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

- **Into a package**: no text layer, no canonical or raw HTML, no candidates file. The
  contributor refetches each page at its pinned revision and every sha is checked. No
  path of the coordinator's machine either — a package that names one is refused, never
  rewritten.
- **Out of a result**: no article text. `results.py` refuses the listed names rather than
  copying them, and nothing is filtered silently.

## The intake contract

A result may carry a sealed record set. It is checked by
`scripts/articles/verify_submission.py` — docs/INTAKE.md section 3, offline — **on both
sides**: by the contributor before it pushes, so a defect is seen there and not first on
the coordinator's machine, and by the coordinator on collect, because a result is
untrusted input whoever pushed it. The refusal codes and HTTP statuses are the ones the
submission endpoint will answer with, so the work-repo leg and the endpoint that will
replace it refuse the same sets for the same reasons.

## What is not built yet

Between `claim` and `results`: fetching the package's sources at their pinned revisions,
placing the declaration index, regenerating the candidates, staging the run and driving
the agents. Those are pipeline stages 9 and 10 (docs/PIPELINE.md section 10), and a
contributor does that part by hand until they land. The transport at both ends, the
claim, the sandbox, the bridge and the result's checks are built and tested.

## What only a real Mac can prove

Seven things, listed in docs/PIPELINE.md section 9. Everything here is tested on Linux,
including the real-path substitution over a symlinked directory standing in for
`/var` → `/private/var` and `bash -n` over `bridge.sh` for bash 3.2 — but no test here
can tell you that Claude Code's own sandbox honours this settings file on macOS.
