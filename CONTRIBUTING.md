# Contributing to WikiLean

## Improve the mathematics

Use the [article reader](https://wikilean.jackmccarthy.org/articles) to inspect a
passage and its Lean matches. Sign in to confirm or report a match, or add and
correct tags after accepting the CC0 contribution agreement.

The [contributor guide](docs/START-CONTRIBUTING.md) explains the current workflow,
match relations, agent accounts, and limits. The old `/:slug` annotation editor
is disabled; new public edits belong in the reader.

Check assumptions as well as conclusions. Label partial or related matches
honestly, cite a declaration that resolves, and add a reason when correcting a
link. A missing match is useful evidence about a particular search, not proof
that no formalization exists.

## Contribute from another computer

Create your own agent account at [/agents](https://wikilean.jackmccarthy.org/agents)
for the tag API. For maintenance, article-pipeline work, and Brain proposals,
follow [START-CONTRIBUTING.md](docs/START-CONTRIBUTING.md) and the linked setup guides.
Keep model credentials on your own machine and enforce your own spending limits.
WikiLean never collects your model API key or subscription credentials.

Sealed article results can be submitted with your agent token through the
[self-service intake client](docs/INTAKE.md#submit-from-your-laptop). The same
offline checker runs on your machine and on the server. The receipt distinguishes
queued, accepted, rejected and failed submissions; acceptance does not publish
the set or approve its mathematical claims.

## Develop and test

Use **Node 22** and **Python 3.12**, matching `.github/workflows/ci.yml`. Install the locked JavaScript dependencies from the package directory; use `npm ci`, not `npm install`, when reproducing CI:

```sh
cd wiki
npm ci
```

The named Worker commands are:

```sh
npm test              # alias for the hermetic Vitest suite (`test:unit`)
npm run test:unit     # Worker tests that need no generated/private corpus
npm run typecheck     # TypeScript, including the browser harness
npm run test:ci       # required Worker gate: typecheck + hermetic tests
```

The deterministic Python gate runs from the repository root. It installs the exact packages in `requirements-ci.txt`, asserts Python 3.12, removes credential variables from its environment, and runs the moderation, parity, offline-evaluation, and hermetic Brain fixture checks:

```sh
python3.12 -m pip install --requirement requirements-ci.txt
./scripts/ci-python.sh
```

The offline moderation evaluation uses planted fixtures and no agents, network, or tokens. Never run its `--live` mode in CI.

#### Browser tests

The Playwright suite runs Chromium against a loopback-only server backed by the same in-memory D1/KV fixture as the Worker tests; both the browser and server reject external requests. Install the pinned npm dependencies first, then install and run Chromium:

```sh
cd wiki
npm ci
npx playwright install chromium
npm run test:e2e
```

Linux CI uses `npx playwright install --with-deps chromium` to install system dependencies too. The loopback server builds the `/brain` page by importing `site/build_brain_page.py` with the `python3` on your PATH, which must be Python 3.10 or newer. Failures retain traces and screenshots under `wiki/test-results/`; CI also writes the HTML report under `wiki/playwright-report/` and uploads both directories as the seven-day `browser-failure-artifacts` artifact.

The browser job runs on every CI trigger and is **required**: the aggregate `required` job covers the Worker, Python, browser and attribution jobs, and branch protection requires only that aggregate.

#### Corpus-dependent checks

Corpus checks are opt-in because their large/generated or private inputs are not available in a fresh checkout. From `wiki/`, run:

```sh
npm run test:corpus
```

Its preflight requires more than 200 cached Wikipedia pages in `site/cache/*.html`, more than 200 Python renderer outputs in `site/out/*.html`, more than 250 annotations in `site/annotations/*.json`, and the generated declaration-index manifest at `wiki/public/assets/decl-index/manifest.json`. The command then runs the render golden, declaration-index, and seed corpus suites.

`python3 brain/test_fold_xref.py` is also corpus-dependent, not part of required Python CI. It uses the real catalog plus the private declaration cache at `.claude/skills/mathlib-search/.cache/declaration-data.json` (and may fall back to the Mathlib checkout configured by `BRAIN_MATHLIB_CHECKOUT`). Run it only after those local inputs exist.

### Pull requests
- Branch from `main`, push, open a PR against `Deicyde/WikiLean`.
- For substantive changes, open an issue first to discuss scope.
- `.github/workflows/ci.yml` runs on pull requests, pushes to `main`, and manual dispatch. It has read-only repository permissions, checks out without persisted credentials, receives no secrets, and contains no deployment, production write, or token-consuming step.
- The `worker` and `browser` jobs use Node 22, and the `python` job uses Python 3.12. The `attribution` job fails when a commit in the history names Claude as an author (a `Co-Authored-By: Claude` line). The `required` aggregate needs all four and is the branch-protection target.
- Dependabot (`.github/dependabot.yml`) opens weekly npm (`wiki/`) and pip (`requirements-ci.txt`) PRs and monthly GitHub Actions PRs, each gated by `required`. npm and pip PRs change release-affecting files, so merge them at the start of a release cycle, never between a release build and its promotion; Actions PRs are release-neutral. `wrangler` arrives in its own PR: it is the deploy, backup and rollback tool and no CI job runs it, so merge it only at the start of a release cycle. In the same PR, run `python3 scripts/backup_wrangler_lock.py --write` to regenerate `.github/backup-wrangler` (CI's check fails until you do), re-pin the D1 snapshot acquirer (the `WRANGLER_*` pins in `brain/acquire_d1_snapshot.py` and `brain/d1_snapshot_bundle.py`, a new acquirer generation there and a new profile in `brain/d1_source_export_profiles.json`; `brain/test_acquire_d1_snapshot.py` fails until the pins match the lockfile), and update the release runbook's Wrangler version notes. Any npm PR that moves a package Wrangler depends on (esbuild is one) needs the same `--write` and the same acquirer re-pin. If an `ajv` or `esbuild` PR fails `check:companion-validator`, run `node scripts/generate-companion-validator.mjs` in `wiki/` and commit the regenerated `validate.js` to that PR. After merging an Actions PR that bumps `upload-artifact`, dispatch Backup D1 once and check the run: no pull request exercises it.

---

## Upstream contributions

WikiLean is partly a bet that math-formalization metadata belongs on Wikidata and in Mathlib itself.

### Wikidata
- Wikidata property **[P14534 (Mathlib Declaration ID)](https://www.wikidata.org/wiki/Property:P14534)**, an external identifier mapping a concept item to its Mathlib4 declaration name, was created on 2026-06-17 from WikiLean's proposal ([docs/wikidata_property_proposal.md](docs/wikidata_property_proposal.md)). It had 115 statements on 2026-10-13. Adding or checking P14534 statements on concept items helps.

### Mathlib4
- Mathlib4 has an `@[wikidata]` attribute (defined in `Mathlib/Tactic/CrossRefAttribute.lean`) that tags declarations with their Wikidata QID — the inverse of that property. The WikiLean concept layer is being used to propose batch additions of this tag.

---

## Data & research notice

WikiLean is, in part, an experiment in **human + AI database moderation**. Like Wikipedia, edits are public and attributed: your GitHub display name, the article you edited, the timestamp, and your edit comment are all public.

Beyond the visible edit, we may analyze edit *metadata* — which fields changed, whether a change was AI- or human-authored (provenance), and timing — and publish findings **in aggregate** to study how humans and AI moderate a shared database together. Research exports use salted-hash pseudonyms by default and never include emails or IP addresses. If you edit, you're contributing to that record; that's the whole point, and we'd rather you know up front.

## Code of conduct

Be kind, be precise about math, prefer evidence over assertion. Substantive disagreements are good; condescending ones are not.

## License recap

By contributing you agree your contributions are licensed as: code → MIT, annotation data → CC0. The article text on the site remains CC BY-SA from the upstream Wikipedia source.
