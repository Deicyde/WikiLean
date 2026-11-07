# The Mac checks: a runnable checklist

Eight things about contribute mode cannot be answered from Linux. Seven are about
Claude Code's own sandbox on macOS (`docs/PIPELINE.md` section 9); the eighth is what
an article actually costs out of a subscription's weekly allowance, which nobody has
measured and which is the number a contributor actually cares about
(`docs/START-CONTRIBUTING.md`, path 3).

**They are the only unmeasured gate on path 3, and they cost one afternoon on one Mac.**
Nothing on path 3 should be promised to anybody until checks 1–7 are done.

This page is for whoever sits at that Mac. Every step is a command to run and a line to
write down. Work top to bottom; later checks assume earlier ones passed.

---

## Before you start

| | |
|---|---|
| Machine | a Mac — Apple silicon or Intel; record which |
| Python | 3.12 (`python3 --version`) |
| Claude Code | logged in to a subscription you are willing to spend from; record `claude --version` |
| `tend` | installed and on `PATH` |
| Checkout | a clone of this repository, on the branch you are testing |

```sh
cd <your WikiLean clone>
git rev-parse HEAD                      # write this down: it is what you tested
claude --version                        # and this
tend --version                          # and this
sw_vers                                 # and this
python3 --version
./scripts/ci-python.sh                  # must pass before anything below means anything
```

> **`ANTHROPIC_API_KEY` must be unset** for every `claude` command below. With one set,
> a subscription call fails *silently* — "error result: success", zero tokens — and you
> will spend the afternoon debugging a sandbox that was never reached. `bridge.sh`
> unsets it; a command you type yourself does not.
>
> ```sh
> unset ANTHROPIC_API_KEY && echo "${ANTHROPIC_API_KEY-unset}"   # must print: unset
> ```

### Write the answers down here

Copy this table into the issue or the commit message, and fill the right-hand column as
you go. An unfilled row is an unanswered question, not a pass.

| # | Check | Result |
|---|---|---|
| 1 | Settings keys are this build's | |
| 2 | `--settings` contributes `allowWrite` | |
| 3 | macOS sandbox backend: write + `git` inside the worktree | |
| 4 | `tend` and `TEND_TASK_ID` | |
| 5 | bash 3.2 parses the scripts | |
| 6 | A refetch reproduces the layer | |
| 7 | The agents' tool allowlist | |
| 8 | What one article costs out of a subscription | |

---

## Make a settings file to test with

Every check below needs the real thing: the per-call settings file a staged run writes,
with the placeholder replaced by a real directory, exactly as `bridge.sh` does it.

```sh
mkdir -p /tmp/wl-mac/worktree/findings/t1 /tmp/wl-mac/entrypoint/.git /tmp/wl-mac/gate
python3 - <<'PY'
import sys
sys.path.insert(0, "scripts/pipeline/contribute")
import sandbox
template = sandbox.write_settings(
    "/tmp/wl-mac/claude-settings.template.json",
    write_roots=[sandbox.WORKTREE_PLACEHOLDER, "/tmp/wl-mac/entrypoint/.git"],
    deny_write=["/tmp/wl-mac/gate"]).read_text()
# what bridge.sh writes per call, with `pwd -P` in place of the placeholder
import os
real = os.path.realpath("/tmp/wl-mac/worktree")
open("/tmp/wl-mac/claude-settings.json", "w").write(
    sandbox.call_settings(template, real))
print(open("/tmp/wl-mac/claude-settings.json").read())
PY
```

Read what it printed. **On macOS the `allowWrite` globs should say `/private/tmp/...`,
not `/tmp/...`** — `/tmp` is a symlink into `/private`, and the sandbox matches the real
path a write lands on. If they say `/tmp`, the real-path fix is not working and
**nothing below will mean anything**; stop and report that.

---

## 1. The settings keys are this build's

`sandbox.enabled`, `sandbox.filesystem.disabled`, `sandbox.filesystem.allowWrite` /
`denyWrite` and `sandbox.autoAllowBashIfSandboxed` were read out of a Claude Code binary
(2.1.311). **A renamed key leaves the agents unsandboxed with only the prompt stopping
them**, and it fails silently — a settings file with an unknown key is just a settings
file with an unknown key.

```sh
cd /tmp/wl-mac/worktree
claude --settings /tmp/wl-mac/claude-settings.json /sandbox
```

**Record:** does it report a filesystem sandbox that is **on** and **confining**, and
does the writable set it names match the `allowWrite` globs above? Paste its output.

If it reports no sandbox, or does not mention the file: run `claude --version`, and
check the current docs for what the keys are now. The one place to change them is
`scripts/pipeline/contribute/sandbox.py` — `settings_json()` is the only function that
writes them.

---

## 2. Does `--settings` contribute `allowWrite` at all?

The binary's own telemetry note says *project* settings contribute no `allowWrite`. If
that is also true of `--settings`, the confinement rests on the prompt and the tool
allowlist and not on the sandbox, and §3.2's invariant 2 is weaker than it reads.

One call settles it. It asks the agent to write **outside** every allowed root:

```sh
cd /tmp/wl-mac/worktree
unset ANTHROPIC_API_KEY
claude --settings /tmp/wl-mac/claude-settings.json \
       --permission-mode acceptEdits \
       --allowedTools "Read,Glob,Grep,Edit,Write" \
       --output-format json \
       -p 'Write the single word "escaped" to the file /tmp/wl-mac/OUTSIDE.txt. Do not write anywhere else.'
ls -l /tmp/wl-mac/OUTSIDE.txt 2>&1
```

**Record:** did the file appear?

- **Not created, and the agent said it was refused** → `--settings` contributes
  `allowWrite`. This is the answer we want.
- **Created** → it does not. Write that down loudly: the sandbox is not the boundary,
  and the worker's single scoped `Write` (the in-process `PreToolUse` hook on the run
  host's side) is what is actually holding. Path 3 can still open, but §3.2's invariant
  2 has to be restated and the decision is the maintainer's.

Then check the *inside* case, which must work:

```sh
claude --settings /tmp/wl-mac/claude-settings.json \
       --permission-mode acceptEdits --allowedTools "Read,Glob,Grep,Edit,Write" \
       --output-format json \
       -p 'Write the single word "inside" to findings/t1/probe.json, relative to the directory you are in.'
cat /tmp/wl-mac/worktree/findings/t1/probe.json
```

**Record:** a worker can write its own document. If it cannot, nothing can run at all.

---

## 3. macOS's sandbox backend

Linux uses `bwrap`; macOS has its own. Two things to confirm, plus one to discover.

```sh
cd /tmp/wl-mac
git init -q entrypoint
git -C entrypoint -c user.email=t@example.invalid -c user.name=T commit -q --allow-empty -m init
git -C entrypoint worktree add -q /tmp/wl-mac/wt1 -b probe
cd /tmp/wl-mac/wt1
python3 - <<'PY'
import os, sys
sys.path.insert(0, "<your WikiLean clone>/scripts/pipeline/contribute")
import sandbox
t = sandbox.write_settings("/tmp/wl-mac/wt1-template.json",
        write_roots=[sandbox.WORKTREE_PLACEHOLDER, "/tmp/wl-mac/entrypoint/.git"],
        deny_write=[]).read_text()
open("/tmp/wl-mac/wt1-settings.json", "w").write(
    sandbox.call_settings(t, os.path.realpath("/tmp/wl-mac/wt1")))
PY
unset ANTHROPIC_API_KEY
claude --settings /tmp/wl-mac/wt1-settings.json \
       --permission-mode acceptEdits \
       --allowedTools "Read,Glob,Grep,Edit,Write,Bash(git:*)" --output-format json \
       -p 'Write "x" to a new file called probe.txt here, then run: git add probe.txt && git -c user.email=t@example.invalid -c user.name=T commit -m probe'
git -C /tmp/wl-mac/wt1 log --oneline -1
```

**Record three things:**

- can the agent **write its document** in the worktree? (yes / no)
- can it **run `git` and commit** inside it? A linked worktree's index, refs and objects
  live in the *entrypoint* repository's `.git`, which is why that path is in
  `allowWrite` — confirm that is enough on macOS. (yes / no)
- **does the call need to write Claude Code's own state under `$HOME/.claude`?** No root
  in the settings admits it. Watch for a permission error or a refusal mentioning
  `$HOME`. If it needs it, say so: the settings have to admit a narrow path under
  `$HOME/.claude` and `sandbox.py` is where that goes.

---

## 4. `tend` on macOS

The gate's ownership check (rule 7, "the changed files are this task's own") reads
`TEND_TASK_ID`. Without it the check falls back to a weaker rule, and nothing says so
at the time.

```sh
tend --version
```

Then run a one-task smoke run the way the pipeline does — a task whose **validation
command** prints the variable — and confirm `TEND_TASK_ID` reaches it:

```sh
# in whatever form your tend install takes a task; the point is the validation command
printf 'TEND_TASK_ID=[%s]\n' "${TEND_TASK_ID:-UNSET}"
```

**Record:** `tend --version`, whether a one-task run completed, and whether
`TEND_TASK_ID` was set **in the validation command** (not only in the agent call).

If it is not set there: `scripts/pipeline/run/task.py` passes `TEND_TASK_ID` into the
gate's environment itself, so the runner-driven path is fine — but say so, because the
gate's fallback is then the only thing holding for any path that does not go through
`task.py`.

---

## 5. bash 3.2

macOS ships bash 3.2. `bridge.sh` avoids heredocs inside `$(...)`, associative arrays
and `${var^^}` for exactly this reason.

```sh
/bin/bash --version | head -1          # expect 3.2.x
/bin/bash -n scripts/pipeline/contribute/bridge.sh && echo "bridge.sh: parses"
for f in $(git ls-files '*.sh'); do /bin/bash -n "$f" || echo "FAILS: $f"; done
```

**Record:** the bash version, and that `bridge.sh` and every tracked `.sh` parse under
it.

Then check the bridge's own refusal path, which costs nothing and calls no model:

```sh
WIKILEAN_CLAUDE_SETTINGS=/nonexistent /bin/bash scripts/pipeline/contribute/bridge.sh worker
echo "exit=$?"     # must be 78: it refuses to run an agent unsandboxed
```

---

## 6. A refetch may not reproduce the layer

This is the one check that is not about the sandbox, and it is the one most likely to
bite in practice. **An old revision's parse output expands templates as they are
*now***, so refetching a page at its pinned `oldid` can render to a different text layer
than the coordinator's. A package carries no bytes by rule, so there is no way to avoid
the refetch.

`contribute/package.py`'s `check_sources()` refuses such a source by name and the
coordinator re-plans. What a synthetic run cannot tell us is **how often it happens on
real Wikipedia pages.**

With a real package in hand:

```sh
# refetch and render the package's pinned revisions into <workdir>/root/sources/,
# then let the middle check them:
python3 scripts/pipeline/contribute/run_package.py \
        --repo <work-repo clone> --workdir <dir> --stage run
```

**Record:** of the sources in the package, how many rendered to the pinned
`layer_sha256` and how many did not — and for any that did not, the page and how far
apart the two layers are. One mismatch out of a package is a nuisance; a third of them
is a design problem and the coordinator should hear about it today.

---

## 7. The agents' tool allowlist

The worker's allowlist names host tools; the paths are staged and right by
construction, but whether Claude Code's allowlist matching behaves the same on macOS is
unverified.

```sh
cd /tmp/wl-mac/worktree
unset ANTHROPIC_API_KEY
claude --settings /tmp/wl-mac/claude-settings.json \
       --permission-mode acceptEdits \
       --allowedTools "Read,Glob,Grep" --output-format json \
       -p 'Create a file called notallowed.txt here containing "x".'
ls notallowed.txt 2>&1      # must NOT exist: Write was not in the allowlist
```

**Record:** whether an allowlist that omits `Write` actually stops a write, and whether
the reviewer's read-only set (`Read,Glob,Grep,Bash(git diff:*),Bash(git show:*),Bash(git
log:*),Bash(git status:*)`) admits those four `git` forms and nothing else.

---

## 8. What one article costs out of a subscription

**This is the number a contributor actually cares about and nobody has measured it.**
What exists today is an API-price derivation from a batch's measured unit prices, which
is not what a subscription's weekly allowance feels like. Until this is filled in,
`docs/START-CONTRIBUTING.md` quotes **no** dollar figure as a subscription cost, and it
should stay that way.

Run **one article** end to end, on the subscription, and record the allowance it ate —
not dollars:

```sh
# before
date -u +%Y-%m-%dT%H:%M:%SZ
#   note the weekly-allowance reading Claude Code shows you, and the reset time

python3 scripts/pipeline/contribute/run_package.py \
        --repo <work-repo clone> --workdir <dir> --stage run --execute

# after
date -u +%Y-%m-%dT%H:%M:%SZ
#   note the weekly-allowance reading again

# what the run itself recorded: one file per task, and the harvest's cost block
python3 -c '
import json, sys
from pathlib import Path
calls = seconds = 0
for path in sorted(Path(sys.argv[1], "root", "attempts").glob("*.json")):
    record = json.loads(path.read_text())
    calls += len(record["attempts"]) + (1 if record.get("reviewer") else 0)
    print(path.stem, len(record["attempts"]), "attempt(s), gate exit",
          (record.get("gate") or {}).get("exit"))
print("worker + reviewer calls:", calls)
receipt = json.loads(Path(sys.argv[1], "corpus", sys.argv[2], "RECEIPT.json").read_text())
print("cost block:", json.dumps(receipt.get("cost"), indent=1))
' <dir> <run-id>
```

**Record, per article:**

| | |
|---|---|
| passages in the article | |
| tasks, and worker calls (including retries) | |
| reviewer calls (0 if you passed `--no-reviewer`) | |
| wall-clock time | |
| **allowance consumed**, as a share of the weekly limit | |
| the plan tier (Pro / Max 5× / Max 20×) | |
| whether you hit a limit, and where | |

The receipt's `cost` block counts calls and seconds; on a subscription it prices them at
`$0.00` and reports them as `unpriced_calls`, which is correct and is why the allowance
reading has to be written down by hand.

**Then put the answer in `docs/START-CONTRIBUTING.md`** in place of the paragraph that
currently says it is unmeasured, and say which plan tier it was measured on.

---

## When you are done

- Fill in every row of the table at the top and attach it to an issue.
- Update `docs/PIPELINE.md` section 9 to say the checks ran, on what machine, at what
  commit, with what `claude --version` — a result without those three is not reusable.
- If check 1 or check 2 came back the wrong way, **say so before anything on path 3 is
  promised to anybody.** That is what these checks are for.
