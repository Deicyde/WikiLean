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
| **2** | Run **your own agent** against the tag API | merged with release 1; waiting on its deployment |
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
- it is public and attributed. R1 supports changing a tag and removing it with a reason;
  full history, diffs and revert are planned for R2 and are not available in R1.

---

## 2. Run an agent against the tag API — **with release 1**

> **Merged, not deployed.** Agent accounts (PR #126) and the reader's two tag-write
> routes (PR #138) are merged. They need migrations 0017–0019, the Turnstile setup and
> an approved Worker release before they are available on the live site.

When deployed:

- you mint an **agent account yourself** at `/agents`. No asking a maintainer for a
  token;
- the token is shown **once**, and only its SHA-256 is stored;
- the agent is **its own account**, named "<your name> (agent)", recording **you** as
  its owner. Every edit it makes says so and you are accountable for it — which is also
  how an agent can agree to the licence: its owner does;
- **5 live agents** per person, 25 minted, and all of yours share **30 requests a
  minute**;
- you revoke any of them from the same page.

Then you point your agent at the tag routes documented in
[ARTICLES.md section 10a](ARTICLES.md). The overlay MCP tool is later work (R4).
**Your model, your spend, your cap.** WikiLean never sees your model key, and stores
only the SHA-256 of the token it issues you.

---

## 3. Run the whole locate pipeline on your machine — **ask first**

> **Built, tested, and not yet open.** Please open an issue before you start. The
> remaining prerequisites are listed below.

This is how an article gets checked end to end: every passage cut out, every candidate
declaration retrieved, every match judged, and a second pass that tries to refute the
first. It is the most work and it produces the most.

### What it costs you

**We do not know yet, and we are not going to guess.** What an article costs out of a
Claude subscription's weekly allowance is **unmeasured** — nobody has run it on a
subscription and counted. The figures that exist are API prices for the model calls a
batch made, which is not the number you would actually feel. The procedure is in
[CONTRIBUTE-MAC-CHECKS.md](CONTRIBUTE-MAC-CHECKS.md), and this page will carry
the answer when there is one. The runner records a requested spend cap but does
not enforce it; no contributor watchdog runs. Its per-call timeout is a time
limit, not a money limit.

### What you need

| | What | Why |
|---|---|---|
| OS | macOS (Apple silicon or Intel) | the pinned SDK/client boundary has native evidence on Apple silicon; other platforms need their own check — [docs/CONTRIBUTE-MAC-CHECKS.md](CONTRIBUTE-MAC-CHECKS.md) |
| Python | 3.12 | every pipeline module; `scripts/ci-python.sh` is the gate |
| Git + a GitHub account | read/write on the private work repo | the transport |
| A WikiLean checkout | `git clone https://github.com/Deicyde/WikiLean` | the pipeline and the verifier |
| Claude Code, logged in | **your own** Pro/Max subscription or API key | the three model stages |
| Python dependencies | install the pinned dependencies listed in [PIPELINE.md](PIPELINE.md) | the runner uses the Claude Agent SDK; no separate `tend` installation is needed |
| The declaration index **and** the search corpus | about **0.5 GB per universe**, one download; signed bucket access is not available yet | stage 9c's retrieval joins the index; the worker greps the search corpus |
| Disk | about 2 GB per universe | the index, the search corpus and the fetched sources |
| **Not** needed | an Anthropic key held by WikiLean, any WikiLean credential beyond the work-repo access, a Lean or Mathlib checkout | — |

### What is outstanding

1. **Fresh source plans and a subscription measurement.** Native worker
   permissions and the offline suite have passed on macOS. Real Wikipedia
   refetch reproduced one of three pinned sources; the other two were correctly
   refused and need new plans. One article's subscription usage remains unmeasured.
   [CONTRIBUTE-MAC-CHECKS.md](CONTRIBUTE-MAC-CHECKS.md) records the evidence and
   repeatable checks. The scripted client fixture calls no model.
2. **Publishing the index.** The signed destination is a bucket behind short-lived
   signed URLs, with a per-library licence manifest. The offline packager is built;
   uploading the bundle and issuing access are still operator steps.
3. **The submission endpoint and the claims API.** They are specified
   ([docs/INTAKE.md](INTAKE.md)) and the verifier half is built and runs offline today;
   the HTTP half is a later release. Until it exists, a set comes back through the work
   repo and the maintainer promotes it by hand.

The paid three-article proof is complete: all 51 tasks passed the gate, and derived
status agreed on 172 of 203 unchanged passages (84.7%, against the signed 80% bar).
This measures agreement with the earlier run, not mathematical accuracy. It does not
measure subscription usage or replace the Mac checks.

### The sequence

Claims, model runs and result pushes require **`--execute`**. Start without it.
A run preview writes local staging files and checks the prompts; a results preview
writes and verifies a local result bundle. Neither calls a model or pushes.
**`--stage seal` always writes the local sealed set**, with or without `--execute`;
it is free and sends nothing. No omitted flag starts a paid model run.

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

### What the transport carries

- **Source layers are refetched locally.** A package carries the plan, source pins and
  universe; it omits full text layers, HTML, candidates and the coordinator's staged
  working directories. Your machine stages its own inputs. Returned findings and
  sealed records do contain the passage quotations needed to identify their claims.
  The result copier refuses the named source-layer files; it is not a general filter
  for arbitrary text placed in other files.
- **We never see your model credentials.** WikiLean collects and stores no API key and
  no subscription credential, anywhere. The only credential WikiLean would ever *issue*
  you is a submit-only machine key, bound to your machine, quota'd and revocable on its
  own.

---

## What happens to your work

In R1, a tag saved through the website or tag API is a live community edit, attributed
to its author. An agent has its own account and a named human owner. History, recent
changes and general revert belong to R2; the trust ladder belongs to R3.

A contributed record set follows a different path today: the maintainer verifies and
reviews it offline. The importer writes pending JSON by default; it generates live
SQL only with explicit approval (`--trusted`). Applying that SQL is a separate
operator action after a backup. Accepted sets do not publish themselves.

None of this makes a link *correct* — only that more than one party looked and did not
object. The reader's "these links have not yet had an independent audit" notice stays,
the community badge is kept visually distinct from the audited figure, and the audit
summary in the store remains the only place a measured accuracy number appears.

## The licence, in one sentence

Code is MIT, the mapping you contribute (which passage is formalized where) is **CC0**,
and Wikipedia's text stays CC BY-SA 4.0 — by contributing you agree to that, and if you
are running an agent, you are agreeing on its behalf.

One thing that follows from it: **do not paste Lean source into a note.** The libraries'
signatures and docstrings are under their own licences, not CC0; check their terms
for the intended use. A tag stores a
declaration's *name*; the reader renders its statement from the library's own data.
Check the bundle's per-library licence manifest before using its source text.

## Being useful

Prefer **central statements** over setup sentences — the page marks which is which. A
wrong link is worse than no link. Say why in the note. And if you are not sure, press
**Report** rather than guessing: somebody will look.

## Where to ask

Open an issue on [Deicyde/WikiLean](https://github.com/Deicyde/WikiLean). For path 3,
open one **before** you start: it is the path with the outstanding prerequisites above, and we
would rather tell you where they stand than have you find out at the end of a weekend.
