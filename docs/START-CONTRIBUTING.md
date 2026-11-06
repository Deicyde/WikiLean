# Start contributing to WikiLean

WikiLean answers one question for every statement in Wikipedia's mathematics articles:
**is this formalized in Lean, and where?** Most of it is still unanswered.

This page is the honest version of what you can do about that **today**, and what is
coming. Where something is not open yet, it says so and says what it is waiting on —
every "coming" below is a release that has been scoped and signed off, not a wish.

| | What | Status |
|---|---|---|
| **0** | Read an article, and **Confirm** or **Report** a link | **live now** |
| **1** | **Tag a passage** yourself — add, change or remove a link | **coming in the next release** |
| **2** | Run **your own agent** against the API | after release 1; needs agent accounts (PR #126) merged first |
| **3** | Run **WikiLean's whole locate pipeline** on your own machine | built, not yet open — **ask first** |

---

## 0. Confirm or report a link (one click, live today)

1. Open **[/articles](https://wikilean.jackmccarthy.org/articles)** and pick an article.
2. Click a passage. If a Lean declaration already formalizes it, you will see it, with
   the relation the pipeline judged. If not, you will see that it found nothing.
3. Sign in (GitHub or Google — **identity only**; WikiLean never asks for repository
   access and never sees your code).
4. Press **Confirm** if the link is right, or **Report** if it is wrong or you are not
   sure. That is it.

**This is worth more than it looks, and it is worth most in two specific places.** The
blind audit run before batch 1 went up met four of its six bars and missed two: how
often the pipeline's second, refuting pass correctly changed a record (49.8% against a
68% bar), and how well a "not formalized" claim is actually supported (59.6% against
85%). So the two judgements a second pair of eyes is best at are exactly the two the
data is weakest on:

- **a link that looks downgraded or hedged** — was that right?
- **a passage marked not formalized** — is it really absent?

A report on a claimed absence is the single most valuable thing you can send us, because
it is the label the project deliberately launched without.

Your feedback is attributed to your account, append-only, and exported for research with
salted-hash pseudonyms (see "Data & research notice" in
[CONTRIBUTING.md](../CONTRIBUTING.md)).

---

## 1. Tag a passage — **coming in the next release**

> **Not live yet.** The reader is read-only today: its one write is the Confirm/Report
> above. The write surface below ships in the next Worker release, with migration 0019.
> Nothing on this page asks you to wait for it to do something useful — section 0 is
> live now, and this section is here so you know what it will look like.

When it ships:

1. Click a passage on **[/articles](https://wikilean.jackmccarthy.org/articles)**.
2. **Add a tag**: pick the library, type the declaration name (it autocompletes), pick
   how closely it matches, and save. It is live immediately — no queue, no approval.
3. **Or fix one**: if a link is wrong, change its relation or remove it with a reason.

**How closely does it match?** The vocabulary is the pipeline's own, so your row and a
pipeline row are comparable:

| relation | means |
|---|---|
| `exactMatch` | the Lean statement says the same thing |
| `closeMatch` | the same up to a convention (say why, in the note) |
| `broadMatch` | Lean's is more general |
| `narrowMatch` | Lean's is a special case |
| `hasPart` | it formalizes part of the passage |
| `relatedMatch` | related, but it does not state this |

**"Not formalized" is a fine and useful answer.** A checked absence is data.

Three things that will be true of every tag:

- it is keyed by the passage and the declaration, not by a revision, so it survives a
  rebuild of the store and collides with the pipeline's own row on the same pair;
- it stores the declaration **name and location, never a pasted Lean signature** — the
  libraries' source text is under the libraries' own licences, and the reader renders
  signatures from the universe's own data;
- it is public, attributed, in full history, and anybody can undo it.

---

## 2. Run an agent against the API — **after release 1**

> **Not open yet,** and it is waiting on two things, both named: PR #126 (agent
> accounts) has to merge, and the reader's write surface has to be opened to agents,
> which is a later release. Today the reader explicitly refuses agent writes.

When it opens, the shape is already decided and is PR #126's:

- you mint an **agent account yourself** at `/agents`. No asking a maintainer for a
  token;
- the token is shown **once**, and only its SHA-256 is stored;
- the agent is **its own account**, named "<your name> (agent)", recording **you** as
  its owner. Every edit it makes says so and you are accountable for it — which is also
  how an agent can agree to the licence: its owner does;
- **5 live agents** per person, 25 minted, and all of yours share **30 requests a
  minute**;
- you revoke any of them from the same page.

Then you point your agent at the tag route, or at the MCP tool beside the nine already
at `POST /mcp`. **Your model, your spend, your cap.** WikiLean never sees your model
key, and stores only the SHA-256 of the one it issues you.

---

## 3. Run the whole locate pipeline on your machine — **ask first**

> **Built, tested, and not yet open.** Please open an issue before you start. Four
> things are outstanding and each is named below, so you can see exactly where it
> stands rather than being told "soon".

This is how an article gets checked end to end: every passage cut out, every candidate
declaration retrieved, every match judged, and a second pass that tries to refute the
first. It is the most work and it produces the most.

### What it costs you

**We do not know yet, and we are not going to guess.** What an article costs out of a
Claude subscription's weekly allowance is **unmeasured** — nobody has run it on a
subscription and counted. The figures that exist are API prices for the model calls a
batch made, which is not the number you would actually feel. Measuring it is the first
thing on [docs/CONTRIBUTE-MAC-CHECKS.md](CONTRIBUTE-MAC-CHECKS.md), and this page will
carry the answer when there is one. Until then: it is a weekend of setup and a real
amount of your own model time, and you cap your own spend.

### What you need

| | What | Why |
|---|---|---|
| OS | macOS (Apple silicon or Intel) | Claude Code's sandbox is the confinement, and it is unverified elsewhere — [docs/CONTRIBUTE-MAC-CHECKS.md](CONTRIBUTE-MAC-CHECKS.md) |
| Python | 3.12 | every pipeline module; `scripts/ci-python.sh` is the gate |
| Git + a GitHub account | read/write on the private work repo | the transport |
| A WikiLean checkout | `git clone https://github.com/Deicyde/WikiLean` | the pipeline and the verifier |
| Claude Code, logged in | **your own** Pro/Max subscription or API key | the three model stages |
| `tend` | the task runner the gate's ownership check reads `TEND_TASK_ID` from | stage 10 |
| The declaration index **and** the search corpus | about **0.5 GB per universe**, one download; where it is published is not decided yet | stage 9c's retrieval joins the index; the worker greps the search corpus |
| Disk | about 2 GB per universe | the index, the search corpus and the fetched sources |
| **Not** needed | an Anthropic key held by WikiLean, any WikiLean credential beyond the work-repo access, a Lean or Mathlib checkout | — |

### What is outstanding

1. **One afternoon on one Mac.** Seven things about Claude Code's sandbox on macOS are
   tested on Linux and unproven there: [docs/CONTRIBUTE-MAC-CHECKS.md](CONTRIBUTE-MAC-CHECKS.md)
   is the runnable checklist. **Nothing on this path should be promised to anybody until
   those are done.**
2. **Where the index is published.** About half a gigabyte per universe, too big for git
   and too big for a Worker asset. The decision is made but not executed.
3. **The submission endpoint and the claims API.** They are specified
   ([docs/INTAKE.md](INTAKE.md)) and the verifier half is built and runs offline today;
   the HTTP half is a later release. Until it exists, a set comes back through the work
   repo and the maintainer promotes it by hand.
4. **A verdict on the paid proof** of the model stages.

### The sequence

Each command below is a **dry run until you pass `--execute`**. That is the documented
way to use them: run each one without it, read what it says it would do, and add
`--execute` when you agree. Nothing spends your money by omission.

```sh
git clone https://github.com/Deicyde/WikiLean && cd WikiLean

# 1. Clone the work repo you were given, and claim a package.
python3 scripts/pipeline/contribute/run_package.py \
        --repo <work-repo clone> --workdir <dir> --stage claim
#    ... read it, then:
python3 scripts/pipeline/contribute/run_package.py \
        --repo <work-repo clone> --workdir <dir> --stage claim --execute

# 2. Fill what the package cannot carry, because it is article text or a Lean library:
#      <dir>/root/sources/<source-id>/   the pages refetched at their pinned revisions
#      <dir>/root/candidates/            the retrieval's hits
#      <dir>/root/index/                 the declaration index
#      <dir>/root/index/search/          the search corpus
#    `--stage run` names any that are missing, by name, before it runs anything.

# 3. FREE: stage every task's working directory and check it. No model is called.
python3 scripts/pipeline/contribute/run_package.py \
        --repo <work-repo clone> --workdir <dir> --stage run

# 4. The paid run. Watch the cost: no watchdog runs on your machine.
python3 scripts/pipeline/contribute/run_package.py \
        --repo <work-repo clone> --workdir <dir> --stage run --execute

# 5. Seal it here, and check it against WikiLean's own rules, offline, before you
#    send anything.
python3 scripts/pipeline/contribute/run_package.py \
        --repo <work-repo clone> --workdir <dir> --stage seal \
        --set-id <token> --campaign 'run:<run>+<challenge run>' \
        --challenge-run <challenge run> \
        --producer-commit "$(git rev-parse HEAD)" \
        --created "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# 6. Send it.
python3 scripts/pipeline/contribute/run_package.py \
        --repo <work-repo clone> --workdir <dir> --stage results \
        --documents <dir>/corpus/<run-id> --host <dir>/root --record-set <dir>/set \
        --execute
```

**Step 3 and step 5 are the two to run early and often, because both are free.**

- Step 3 stages each task's working directory and then reads the rendered prompts,
  pulls every path out of them, and asks whether staging put it there. A prompt that
  names a file nothing staged is what stops a paid run halfway through. Finding it
  costs nothing; finding it later costs a task.
- Step 5 ends by running `scripts/articles/verify_submission.py` over the set it just
  wrote. That is the **same code** the server will run, so a refusal here is a refusal
  there, with the same code, the same HTTP status and the name of the rule you broke.
  Exit 0 means it would be accepted. You can also run it on its own:

  ```sh
  python3 scripts/articles/verify_submission.py --record-set <dir>/set --machine-id <you>
  ```

### Two promises this path keeps mechanically, not by convention

- **No article text ever leaves your machine.** A package carries the plan, the sources'
  pins and the universe — no text layer, no HTML, no candidates. It also deliberately
  leaves the coordinator's own staged working directories behind, so your machine stages
  its own out of the pages *you* refetched. A result carries the gated documents and the
  run's receipts, and refuses by name any file that is article text.
- **We never see your model credentials.** WikiLean collects and stores no API key and
  no subscription credential, anywhere. The only credential WikiLean would ever *issue*
  you is a submit-only machine key, bound to your machine, quota'd and revocable on its
  own.

---

## What happens to your work

Everything you contribute becomes an ordinary community edit on the article pages:
attributed to you (and, for an agent, to you as its owner), visible in recent changes,
and revertible by anybody. Nothing is approved first. After a few accepted sets you
become a **trusted** contributor and your sets go live without review.

None of this makes a link *correct* — only that more than one party looked and did not
object. The reader's "these links have not yet had an independent audit" notice stays,
the community badge is kept visually distinct from the audited figure, and the audit
summary in the store remains the only place a measured accuracy number appears.

## The licence, in one sentence

Code is MIT, the mapping you contribute (which passage is formalized where) is **CC0**,
and Wikipedia's text stays CC BY-SA 4.0 — by contributing you agree to that, and if you
are running an agent, you are agreeing on its behalf.

One thing that follows from it: **do not paste Lean source into a note.** The libraries'
signatures and docstrings are under their own licences, not CC0, and at least one
library in the current universe has terms that restrict evaluation use. A tag stores a
declaration's *name*; the reader renders its statement from the library's own data.

## Being useful

Prefer **central statements** over setup sentences — the page marks which is which. A
wrong link is worse than no link. Say why in the note. And if you are not sure, press
**Report** rather than guessing: somebody will look.

## Where to ask

Open an issue on [Deicyde/WikiLean](https://github.com/Deicyde/WikiLean). For path 3,
open one **before** you start: it is the path with four outstanding items above, and we
would rather tell you where they stand than have you find out at the end of a weekend.
