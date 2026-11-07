# Contributor checks on macOS

The contributor runner uses the pinned Claude Agent SDK through
`scripts/pipeline/run/agent.py`. It stages a separate directory for each task.
Workers receive only `Read`, `Grep`, `Glob` and `Write`; they have no shell,
`Edit`, settings files or MCP servers. A host-side hook permits `Write` only
with an absolute path resolving inside that task's findings directory. The
runner runs the gate after the worker finishes and supplies `TEND_TASK_ID`.

The older `contribute/bridge.sh` and `sandbox.py` settings recipes are retained
for compatibility tests. They are not the current `run_package.py` execution
path. No separate `tend` installation or worker-run Git command is needed.

## Evidence recorded on 2026-11-07

The runtime checked was main commit
`9057eeddf92521e60eb062c30f45a4cca86b289c`, on macOS 14.4 arm64 with
Python 3.12.13, Node 22.23.4 and Git 2.48.1. The pinned
`claude-agent-sdk==0.2.168` used its bundled Claude Code 2.1.301.
A globally installed `claude` can have a different version; record the client
the SDK actually launches.

| Check | Evidence | Status |
|---|---|---|
| SDK settings and permissions | Exact worker tool list, `dontAsk`, `setting_sources=[]`; permissive project and user settings do not widen access | passed |
| Writes stay in the task | Absolute inside write succeeds; outside, relative, parent-relative and symlink writes fail; targets remain unchanged | passed |
| Native client read scope | Outside and symlink reads/searches reveal no outside file or content; inside reads/searches succeed | passed |
| Runner-owned gate and task identity | Task-runner tests pass on macOS; the worker does not run the gate or Git | passed |
| macOS paths and retained bridge | Physical `/private/var` paths, bridge tests and the complete offline Python suite pass | passed |
| Real pinned-source refetch | All three fetched at the requested revisions; Group homomorphism matched, Distributive property and Gradient were refused for changed HTML/layer hashes | ran; two sources need re-planning |
| Worker tool allowlist | Actual client refuses scripted `Bash` and `Edit` calls; no shell marker appears | passed |
| One article's subscription usage | Record weekly allowance before and after an authorized one-article run | pending |

The complete offline Python gate passed on both Linux and macOS: 130 commands
before the manual fixture's four safety tests were added to CI. These tests
make no model calls. The manual native-client check covers 24 scripted tool
calls, including positive controls. It uses a local fake Messages API,
an isolated client profile and a dummy bearer value, with outbound networking
restricted to loopback by macOS. No model or subscription is used.

This is evidence for this pinned SDK/client and platform. It does not measure
mathematical accuracy, article quality or subscription usage, and it is not a
general operating-system sandbox for arbitrary programs.

## Repeat the native client check without a model

Use Python 3.12 with the pinned dependencies installed, from a clean checkout.
Choose a **fresh directory outside the checkout**. Do not reuse a prior result
directory.

```sh
unset ANTHROPIC_API_KEY
python3 scripts/pipeline/contribute/mac_client_fixture.py \
    --root <fresh-directory-outside-the-checkout>
```

The command refuses a non-Mac host or an API-key environment variable. It then
re-executes under `sandbox-exec`, drops credential environment variables,
creates a fresh client profile and starts a loopback-only scripted provider.
It supplies hostile tool requests directly so the check does not depend on a
model deciding to try them. The real pinned SDK, CLI and WikiLean permission
hooks handle those requests.

An empty search result that reveals no outside filename or content is a scoped
result; an explicit permission error is also acceptable. Positive controls
must still read, search and write inside the task. Unlisted tools and invalid
writes must be refused. The command exits nonzero if a check fails.

Keep `report.json` and the fixture directory private: they contain local paths
and client receipts. The printed summary contains no credentials or article
text. Record the source commit, fixture commit, platform, SDK and bundled CLI
version when sharing a result. A report against uncommitted code needs its
fixture's file hash as well.

Only the import/entry-point safety tests run in hermetic CI:

```sh
python3 scripts/pipeline/contribute/test_mac_client_harness.py
```

The native fixture remains a manual Mac check, not a token-consuming CI job.

## Real refetch result and repeat procedure

The 2026-11-07 check made three real Wikipedia requests with the pinned
request parameters. The server returned the requested revision in every case:

| Article | Revision | Pinned layer characters | Refetched layer characters | Result |
|---|---|---|---|---|
| Group homomorphism | 1367964474 | 10,124 | 10,124 | HTML and layer hashes identical |
| Distributive property | 1369824603 | 20,831 | 13,697 | both hashes differ; package refused |
| Gradient | 1355918370 | 41,413 | 31,670 | both hashes differ; package refused |

Re-rendering the three retained original bodies with the same current renderer
reproduced all original layer hashes. The request-parameter hashes also
matched. This isolates the difference to the newly returned source bytes;
it is not a macOS rendering discrepancy. The check does not establish which
upstream template or rendering change caused it.

The unchanged Group homomorphism source is available for the pending
one-article measurement. The other two old packages must be re-planned from
freshly pinned source bytes before a contributor runs them. Their original
proof records and pins stay unchanged. **Do not open path 3 on the assumption
that every old revision can be reproduced.**

A work package deliberately omits full source layers. The contributor must
refetch its Wikipedia revisions and render them with the package's producer.
An old revision can expand templates as they are now, so an unchanged `oldid`
does not guarantee an identical rendered layer.

Compare every regenerated `layer_sha256` to the package before any model call.
`package.check_sources()` and the run preview refuse mismatches by source name.
Record the article, pinned revision, expected hash and actual hash; the
coordinator must re-plan a mismatched source. Do not replace a pinned hash to
make an old package pass.

After fetching the sources and installing the index/search corpus, run:

```sh
python3 scripts/pipeline/contribute/run_package.py \
    --repo <work-repo-clone> --workdir <directory> --stage run
```

This stages and checks the prompts for free. It does not fetch missing inputs
or call a model. Record how many real sources reproduced exactly and how many
were refused. Synthetic recorded responses do not answer this question.

## Measure one article on a subscription

This measurement is still pending. API-priced receipts are not a measurement
of a subscription's weekly allowance, and a rate-limit refusal is not a
successful article run.

With the operator's agreed article/package and your own available subscription:

1. Record the plan tier, time, reset time and weekly allowance reading.
2. Unset `ANTHROPIC_API_KEY` and run the package preview first.
3. Execute the one-article run only within its agreed scope.
4. Record the allowance again, elapsed time, worker/reviewer calls, retries,
   gate results and any limit encountered. Retain attempt and result receipts.
5. Publish a sanitized per-article summary in
   [START-CONTRIBUTING.md](START-CONTRIBUTING.md), identifying the tier and
   measurement conditions.

The contributor runner records `max_cost` but **does not enforce a spend cap**;
there is no contributor watchdog. The per-call timeout is enforced, but it is
not a money limit. A missing dollar price remains unpriced, never evidence that
a subscription call was free.

Do not switch to an API key, another account or a larger run to work around an
unavailable subscription without the owner's authorization.
