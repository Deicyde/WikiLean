# `wikilean` — the contributor CLI

WikiLean asks one question about every statement in Wikipedia's mathematics
articles: **is this formalized in Lean, and where?** Most of it is still
unanswered, and the two answers its own blind audit measured weakest are
exactly the two a second pair of eyes is best at — a link the pipeline's
refuting pass downgraded, and a passage it marked "not formalized".

This is a command-line tool for answering those, one at a time, on your own
machine, with your own Claude subscription and your own WikiLean agent token.
It is standard library Python; it installs with one command and holds one
credential.

```sh
uv tool install "git+https://github.com/Deicyde/WikiLean#subdirectory=worker"
wikilean login        # paste the token you minted at /agents
wikilean doctor       # check the machine; this spends nothing
wikilean work --dry-run   # one unit of work, printing what it would send
wikilean work         # one unit of work, for real
wikilean work --loop  # one unit a round, until you stop it
```

Run bare `wikilean` for a dashboard of what is open and a launcher. Piped, it
prints the same survey and exits, so it is safe in a script.

Requires Python 3.12 or later and [Claude Code](https://claude.com/claude-code),
logged in. Read [docs/START-CONTRIBUTING.md](../docs/START-CONTRIBUTING.md)
first: it is the honest account of what is open and what is not.

## Getting a token

Sign in at <https://wikilean.jackmccarthy.org/agents>, accept the CC0
contribution terms **for yourself** (an agent cannot agree to a licence; the
person who mints it does), and press **Mint token**. You get it once; WikiLean
stores only its SHA-256.

```sh
wikilean login                    # hidden prompt, nothing echoed
printf '%s' "$TOKEN" | wikilean login --stdin   # or from a script
wikilean logout                   # removes it
```

It is kept in the macOS login Keychain where there is one, and otherwise in
`~/.config/wikilean/agent-token`, mode 0600 in a 0700 directory. It is never a
command-line argument, never printed, and never written to the journal — the
journal redacts anything token-shaped on its way in. `wikilean doctor` shows a
12-character fingerprint, never the token.

If `WIKILEAN_AGENT_TOKEN` is set in your shell, that is used in preference to
the stored one, which is how the same token can serve this CLI and
`scripts/articles/submit.py`.

## What a round does

A round does **exactly one unit of work**: the first of these that has a
candidate, in this order.

| unit | what it judges | what it sends |
|---|---|---|
| `review` | a link the refuting pass **downgraded**, or a **checked absence** that pass confirmed | a confirming tag, a corrected relation, or a removal with a reason |
| `tag` | a formal passage with **no location and no judged absence** | one new tag, or nothing |
| `locate` | a whole article through the contributor pipeline | a sealed record set to the intake |

Candidates come from the published data alone — `/api/articles/current`, the
index shards, `/api/articles/article/<pageid>` and
`/api/articles/overlay/<pageid>`. `review` and `tag` never offer the same
passage, so `tag` runs only when there is no review work in the articles the
round looked at.

`--unit review|tag|locate` pins the unit. `--dry-run` prints the exact request
and sends nothing. `--loop` keeps going, waiting between rounds and backing off
when the site says to.

### What it sends, exactly

Two routes, both documented in
[docs/ARTICLES.md §10a](../docs/ARTICLES.md) and
[docs/AGENT-CONTRIBUTIONS.md](../docs/AGENT-CONTRIBUTIONS.md):

```
POST /api/articles/tag                  {pageid, campaign, passage_key, library,
                                         declaration, relation, note?, base_version,
                                         model?, run_id?}
POST /api/articles/tag/<pair>/state     {pageid, campaign, state, reason, base_version}
```

and nothing else. Every other request it makes is a public `GET`.

`POST /api/articles/feedback` — the reader's **Confirm** and **Report** — is
deliberately not used: it refuses an agent token, because those rows are a
human reader's verdict. An agent that agrees with a link says so by writing the
overlay row that confirms it, which is what this CLI does.

A **confirmed absence sends nothing at all.** There is no row for "there is
still nothing here", so the round records it locally and moves on. That is the
honest outcome, and it is why `wikilean work` sometimes reports a round with no
write.

## Safety

These are the rules the code keeps, each with a test in `worker/tests/`.

**The model never sees your WikiLean token.** The prompt carries a quotation
from a Wikipedia revision — text anybody may edit — so the `claude` child gets
no tools, no project or user settings, no MCP servers and no session
(`--tools "" --setting-sources "" --strict-mcp-config --no-session-persistence
--safe-mode`, the form `bot/triage.py` already uses). Its environment is an
**allowlist** that cannot contain `WIKILEAN_AGENT_TOKEN`, and the prompt goes
in on stdin, so no quoted text is visible in `ps` either.

**WikiLean never sees your model credentials.** This CLI never asks for a model
key and has nowhere to put one. Your Claude subscription is yours.

**The CLI validates the model's answer before anything is sent.** The relation
must be in the locate vocabulary; the library must be one the article's own
universe lists; the declaration must pass Lean's name shape **and** be found by
the site's own existence oracle (`GET /decl/<name>`); a note must be inside the
server's 500 characters and must not look like pasted Lean source (the
libraries' text is under their own licences, not CC0). An answer that fails any
of these is skipped, written to the journal and **never sent**.

**One write per round, and the site's "wait" is obeyed.** A 429 or a 503 —
a rate limit, or the site-wide write brake during maintenance — ends the round
and sets a local pause from the server's `Retry-After`; nothing is retried
behind your back. All of one person's agents share 30 writes a minute, so
`--loop` paces itself well under that.

**Redirects are never followed**, so a credentialed request is never repeated
against some other host.

## The journal

Every round and every request is appended to
`~/.local/state/wikilean/journal.jsonl` (0600) before anything else happens:

```sh
wikilean journal -n 20
```

It is local. Nothing uploads it. Tokens are redacted on the way in.

## Files

| | |
|---|---|
| `~/.config/wikilean/config.json` | settings (below), 0600 |
| `~/.config/wikilean/agent-token` | the token, when the Keychain is not used |
| `~/.local/state/wikilean/journal.jsonl` | every round and request |
| `~/.local/state/wikilean/state.json` | the cursor, and what has been judged |
| `~/.cache/wikilean/index.json` | the article index, re-read hourly |

`config.json` is plain JSON; every field has a working default.

```json
{
  "base_url": "https://wikilean.jackmccarthy.org",
  "model": "",
  "interval": 90.0,
  "model_timeout": 300.0,
  "skip_pageids": [],
  "locate_enabled": false,
  "locate_checkout": "",
  "locate_repo": "",
  "locate_workdir": ""
}
```

## The locate unit, and what is still missing

`locate` runs the whole pipeline on one article — every passage cut out, every
candidate retrieved, every match judged, a second pass that tries to refute the
first — and submits the sealed result through the intake. It is **off unless
you turn it on twice**: `"locate_enabled": true` in the config *and*
`--enable-locate` on the command line. docs/START-CONTRIBUTING.md asks you to
**open an issue before you start**, and that still holds.

What it drives, in order (`wikilean work --unit locate --enable-locate
--dry-run` prints the exact commands):

1. `run_package.py --stage claim`, then the same with `--execute`
2. `run_package.py --stage run` — free: stages every task and checks the prompts
3. `run_package.py --stage run --execute` — **the paid model run**; no watchdog
   runs on your machine
4. `run_package.py --stage seal` — free, local, and checked offline
5. `submit.py --record-set <workdir>/set`, then the same with `--execute`

Only the submission steps are given your agent token, in the environment, never
on a command line.

**Not finished, and needed before it runs end to end:**

- **The seal arguments.** `--stage seal` also takes `--set-id`, `--campaign`,
  `--producer-commit`, `--created` and `--challenge-run`. Those identify the
  claimed package, and this CLI does not yet read them back out of the work
  directory the claim wrote.
- **A work repository.** Claims still travel through a work repo clone you have
  to be given; there is no claims API yet.
- **The declaration index and search corpus** — about 0.5 GB per universe,
  behind signed bucket access that is not issued yet.
- **The source layers**, refetched locally at their pinned revisions; a package
  deliberately does not carry article text.
- **macOS.** The pinned client boundary has native evidence on Apple silicon
  only; see docs/CONTRIBUTE-MAC-CHECKS.md.
- **What it costs.** Nobody has measured an article against a subscription's
  weekly allowance.

## Development

```sh
cd worker
python3.12 tests/test_token.py
python3.12 tests/test_model_boundary.py
python3.12 tests/test_round.py
python3.12 tests/test_locate_and_cli.py
```

All four are standard-library `unittest`, run by `scripts/ci-python.sh`, and
offline: the site is a loopback `http.server` and `claude` is a stub script
that calls nothing and fails loudly if it is handed a credential.

`worker/` is release-neutral (`site/ops/brain_release_authority.py`): nothing
in the Worker, the public assets or a Brain release reads a byte of it.

## Licence

MIT, like the rest of the repository. The mapping you contribute — which
passage is formalized where — is CC0, and by contributing you agree to that on
your agent's behalf.
